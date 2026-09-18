"""The Agent Index reporter's wiring, as the image ships it."""
import pathlib
import stat

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVICE = ROOT / "image/s6-overlay/s6-rc.d/agent-index"


def test_the_service_is_in_the_user_bundle():
    """A service definition s6-rc never starts is a file, not a service."""
    assert (ROOT / "image/s6-overlay/s6-rc.d/user/contents.d/agent-index").exists()
    assert SERVICE.joinpath("type").read_text().strip() == "longrun"
    assert SERVICE.joinpath("dependencies.d/plow-init").exists()


def test_the_run_script_is_executable():
    """s6-supervise runs the file; a non-executable one is a service that
    never comes up, and git is where that mode is recorded."""
    assert SERVICE.joinpath("run").stat().st_mode & stat.S_IXUSR


def test_no_agent_id_stands_down_rather_than_exiting():
    """A longrun that exits is respawned, so standing down has to block.

    Most images built from this base set no AGENT_ID, so this is the ordinary
    path and not the exotic one.
    """
    run = SERVICE.joinpath("run").read_text()
    assert 'if [ -z "$AGENT_ID" ]' in run
    assert "exec sleep 86400" in run


def test_every_invocation_is_told_where_plow_is():
    """The client's own fallback is a compiled-in https://api.plow.co.

    A cloud agent holds a placeholder token and reaches Plow through a proxy
    that swaps the real one in. Left to the fallback, the register pass sends
    that placeholder straight past the proxy to production, is refused, and the
    agent never registers -- while the service logs one refused pass and looks
    like an ordinary bad network.
    """
    run = SERVICE.joinpath("run").read_text()
    invocations = run.count("/opt/plow/agent-index-client.py")
    assert invocations == 3
    assert run.count('PLOW_API_BASE="$PLOW_API_BASE"') == invocations
    assert "container_environment/PLOW_API_BASE" in run


def test_the_pass_interval_is_five_minutes():
    """Every stand-off sleeps the same interval as a good pass.

    Three of them -- no key, unreadable state, and the ordinary loop -- and a
    longer one on a failure path is a reporter that falls behind exactly when
    something is already wrong.
    """
    run = SERVICE.joinpath("run").read_text()
    assert run.count("/bin/sleep 300") == 3
    assert "sleep 3600" not in run


def test_opt_plow_is_traversable():
    """`COPY --chmod=` applies that mode to the parent directories it creates,
    so the mode of /opt/plow has to be set on its own.

    Without this the directory is 0644: the reporter drops to `hermes`, cannot
    traverse it, and every pass dies on `Permission denied` opening the client
    -- which the service reads as unreadable state and stands off from, so the
    container stays up and healthy and never reports anything.
    """
    dockerfile = (ROOT / "Dockerfile").read_text()
    make = dockerfile.index("install -d -m 0755 /opt/plow")
    copy = dockerfile.index("COPY --chmod=0644 vendor/client.pin /opt/plow/")
    assert make < copy


def test_the_client_is_pinned_by_commit_and_checksum():
    """A moving reference substitutes unreviewed code under an agent holding a
    live credential; a sha in a URL is only as good as the host serving it."""
    pin = dict(
        line.split("=", 1)
        for line in (ROOT / "vendor/client.pin").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert len(pin["sha"]) == 40
    assert len(pin["sha256"]) == 64
    assert pin["path"].endswith(".py")
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "agent-index client is $got, pin says $want" in dockerfile

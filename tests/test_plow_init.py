"""What `plow-init` decides, without booting anything.

    uv run --with pydantic --with pydantic-settings --with python-dotenv --with pyyaml pytest

Covers the three decisions the image cannot afford to get wrong: which
credential files it will read, what it does with each answer from Plow, and
which settings it writes into the agent's config.
"""

import contextlib
import http.client
import importlib.util
import io
import json
import os
import pathlib
import stat
import sys
import types
import urllib.error
import urllib.request
import urllib.response

import pytest
import yaml

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "image/s6-overlay/scripts/plow-init.py"
spec = importlib.util.spec_from_file_location("plow_init", SOURCE)
plow_init = importlib.util.module_from_spec(spec)
sys.modules["plow_init"] = plow_init
spec.loader.exec_module(plow_init)


@pytest.fixture
def owned_by_root(monkeypatch):
    """These tests do not run as root, and the file they write is theirs.

    The mode is real; only the owner is pretended, so the mode table below
    still exercises the code that reads it.
    """

    class RootOwned:
        def __init__(self, info):
            self.st_mode, self.st_uid, self.st_gid = info.st_mode, 0, 0

    real = os.lstat
    monkeypatch.setattr(plow_init.os, "lstat", lambda path: RootOwned(real(path)))


def credential(tmp_path, body="PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=t\n", mode=0o600):
    path = tmp_path / "credentials"
    path.write_text(body)
    path.chmod(mode)
    plow_init.CREDENTIALS = str(path)
    return path


def test_the_process_environment_cannot_outrank_the_file(tmp_path, owned_by_root, monkeypatch):
    """A settings model reads the environment first unless told not to.

    `docker run -e PLOW_AGENT_TOKEN=...` would otherwise outrank the credential
    the image was given, which is a rotation silently not taking.
    """
    monkeypatch.setenv("PLOW_API_BASE", "https://elsewhere.invalid")
    monkeypatch.setenv("PLOW_AGENT_TOKEN", "inherited-not-the-credential")
    credential(tmp_path)
    read = plow_init.read_credentials()
    assert (read.plow_api_base, read.plow_agent_token) == ("https://api.plow.co", "t")


@pytest.mark.parametrize("mode", [0o644, 0o620, 0o602, 0o666])
def test_a_credential_anyone_else_can_reach_is_refused(tmp_path, owned_by_root, parking, mode):
    credential(tmp_path, mode=mode)
    with pytest.raises(Parked):
        plow_init.read_credentials()
    assert "expected root:root at 600 or 400" in parking.read_text()


def test_a_refused_credential_does_not_print_the_token(tmp_path, owned_by_root, parking):
    """This message goes to s6's stderr, which is the container's log.

    Pydantic quotes the offending input by default, and for a missing key that
    input is everything it did parse -- so the file's own token rides along.
    """
    credential(tmp_path, "PLOW_AGENT_TOKEN=sk-the-real-one\n")
    with pytest.raises(Parked):
        plow_init.read_credentials()
    reason = parking.read_text()
    assert "plow_api_base" in reason
    assert "sk-the-real-one" not in reason


def test_the_agent_id_third_key_is_read(tmp_path, owned_by_root):
    credential(tmp_path, "PLOW_API_BASE=x\nPLOW_AGENT_TOKEN=t\nAGENT_ID=life\n")
    assert plow_init.read_credentials().agent_id == "life"


def test_an_unknown_third_key_is_refused(tmp_path, owned_by_root, parking):
    credential(tmp_path, "PLOW_API_BASE=x\nPLOW_AGENT_TOKEN=t\nPLOW_HOME_CHANNEL=cht_host_says\n")
    with pytest.raises(Parked):
        plow_init.read_credentials()
    assert "only the documented keys" in parking.read_text()


class Parked(Exception):
    """Stands in for `signal.pause()` blocking forever, which a test cannot wait out."""


@pytest.fixture(autouse=True)
def parking(monkeypatch, tmp_path):
    """Let `park` run for real up to the point where it would block.

    Autouse because parking is now the only way this script refuses anything:
    a refusal test that did not arrange for this would hang instead of failing.
    Yields the marker path, so a test can assert the reason a human with a
    shell would find.
    """
    marker = tmp_path / "plow-init.parked"
    monkeypatch.setattr(plow_init.signal, "pause", lambda: (_ for _ in ()).throw(Parked()))
    monkeypatch.setattr(plow_init, "PARK_MARKER", str(marker))
    return marker


def test_a_missing_credential_parks_rather_than_exiting(tmp_path, parking):
    """The warm pool's normal life, and the path that started all this.

    Exiting here is what panics the microVM: plow-init's non-zero exit takes
    /init with it, and /init is PID 1.
    """
    plow_init.CREDENTIALS = str(tmp_path / "absent")
    plow_init.CREDENTIALS_WAIT_S = 1
    with pytest.raises(Parked):
        plow_init.read_credentials()
    assert "no credential at" in parking.read_text()


def test_parking_says_why_on_stderr_as_well_as_in_the_marker(parking, capsys):
    with pytest.raises(Parked):
        plow_init.park("no credential at /var/lib/plow/credentials after 60s")
    assert "no credential at" in capsys.readouterr().err
    assert parking.read_text() == "no credential at /var/lib/plow/credentials after 60s\n"


def test_an_unwritable_marker_still_parks(monkeypatch, tmp_path, capsys):
    """The marker is for a human with a shell. Parking is the behaviour."""
    monkeypatch.setattr(plow_init, "PARK_MARKER", str(tmp_path / "no-such-dir" / "parked"))
    with pytest.raises(Parked):
        plow_init.park("no credential")
    assert "could not write" in capsys.readouterr().err


def test_nothing_in_plow_init_exits_on_a_failure_path():
    """A spinning vCPU is never an acceptable outcome, so there is no exit left.

    Read off the source rather than exercised, because the property is the
    absence of a call -- one a future edit could reintroduce anywhere.
    """
    source = SOURCE.read_text()
    assert "sys.exit" not in source
    assert "raise SystemExit" not in source


@pytest.fixture
def image_user(request, monkeypatch, tmp_path):
    """The account and home a healthy image already has.

    These tests do not run in the image, so the two checks that come before
    the one under test are satisfied rather than exercised; each has its own
    test below. The uid is whatever the base's stage2 hook left -- 10000 as
    built, or the host operator's uid once `HERMES_UID` remaps it -- so the
    default is a remapped one, which is what the fleet boots.
    """

    class Hermes:
        pw_uid = pw_gid = getattr(request, "param", 1000)

    monkeypatch.setattr(plow_init.pwd, "getpwnam", lambda name: Hermes())
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(plow_init, "HOME_DIR", str(home))


def test_a_stale_credential_beside_an_unpromoted_one_parks(tmp_path, parking, monkeypatch, image_user):
    """The rotation-not-taking case, and the reason this gate exists.

    00-plow-sanitize promotes a bind-mounted credential into the path this
    script reads. Under S6_BEHAVIOUR_IF_STAGE2_FAILS=1 a failed cont-init is
    carried past, so an aborted promotion leaves the previous boot's
    credential in place -- valid-looking, and belonging to someone else.
    """
    monkeypatch.setattr(plow_init, "CREDENTIALS", str(tmp_path / "credentials"))
    monkeypatch.setattr(plow_init, "HOST_CREDENTIALS", str(tmp_path / "credentials.host"))
    (tmp_path / "credentials").write_text("PLOW_AGENT_TOKEN=stale\n")
    (tmp_path / "credentials.host").write_text("PLOW_AGENT_TOKEN=fresh\n")
    with pytest.raises(Parked):
        plow_init.verify_boot_preconditions()
    assert "the promotion did not run, and this credential is stale" in parking.read_text()


def test_a_host_credential_that_is_not_a_regular_file_parks(tmp_path, parking, monkeypatch, image_user):
    """Docker makes a DIRECTORY at the mount point when the source is missing.

    00-plow-sanitize's `-f` test skips that silently, so the boot most likely
    to leave the previous tenant's credential in place is also the one that
    looks like nothing happened.
    """
    monkeypatch.setattr(plow_init, "CREDENTIALS", str(tmp_path / "credentials"))
    monkeypatch.setattr(plow_init, "HOST_CREDENTIALS", str(tmp_path / "credentials.host"))
    (tmp_path / "credentials").write_text("PLOW_AGENT_TOKEN=stale\n")
    (tmp_path / "credentials.host").mkdir()
    with pytest.raises(Parked):
        plow_init.verify_boot_preconditions()
    assert "is not a regular file -- nothing was promoted" in parking.read_text()


def test_a_promoted_credential_passes(tmp_path, parking, monkeypatch, image_user):
    monkeypatch.setattr(plow_init, "CREDENTIALS", str(tmp_path / "credentials"))
    monkeypatch.setattr(plow_init, "HOST_CREDENTIALS", str(tmp_path / "credentials.host"))
    (tmp_path / "credentials").write_text("PLOW_AGENT_TOKEN=fresh\n")
    (tmp_path / "credentials.host").write_text("PLOW_AGENT_TOKEN=fresh\n")
    plow_init.verify_boot_preconditions()


def test_no_host_credential_means_nothing_to_promote(tmp_path, parking, monkeypatch, image_user):
    """A VM has no bind mount. Its absence is the normal case, not a failure."""
    monkeypatch.setattr(plow_init, "CREDENTIALS", str(tmp_path / "credentials"))
    monkeypatch.setattr(plow_init, "HOST_CREDENTIALS", str(tmp_path / "absent.host"))
    plow_init.verify_boot_preconditions()


def test_a_missing_agent_account_parks(parking, monkeypatch):
    """What a failed inherited uid-remap step leaves behind."""
    monkeypatch.setattr(plow_init.pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    with pytest.raises(Parked):
        plow_init.verify_boot_preconditions()
    assert "no `hermes` account" in parking.read_text()


@pytest.mark.parametrize("image_user", [10000, 1000, 501], indirect=True)
def test_the_agent_account_is_taken_at_whatever_uid_the_remap_left(parking, image_user):
    """Refusing a uid other than the build-time 10000 parked the whole fleet.

    `agent_mgr/local.py` sets `HERMES_UID` to the host operator's uid, and it
    is mandatory, because the agent home is a bind mount and the container
    user has to match its owner. The base honours that -- stage2-hook.sh runs
    `usermod -u "$HERMES_UID" hermes` and chowns the home behind it -- so the
    remap succeeding is the setup completing, not failing. 10000 is the
    build-time uid, 1000 a Linux operator, 501 a macOS one.
    """
    plow_init.verify_boot_preconditions()
    assert not parking.exists()


def test_an_unhandled_exception_parks_too():
    """`park` covers every anticipated failure; this covers the rest.

    An uncaught exception exits the script, exits /init, and panics the VM, so
    a crash and a refusal have to end the same way.
    """
    source = SOURCE.read_text()
    assert "except Exception:" in source
    assert "plow-init raised an unhandled exception" in source


def test_stage_two_neither_exits_nor_deadlines():
    """Both halves of the Dockerfile's side of this.

    FAILS=2 exits /init on any stage-2 failure, which on a microVM is the
    panic. MAXTIME non-zero would call a parked oneshot such a failure.
    """
    dockerfile = (SOURCE.parents[3] / "Dockerfile").read_text()
    assert "ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=1" in dockerfile
    assert "ENV S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0" in dockerfile


def chat(uid, status="active", roles=("owner",), agents=("self",)):
    participants = [{"type": "agent", "relationship": rel} for rel in agents]
    participants += [{"type": "member", "uid": f"m{n}", "role": r} for n, r in enumerate(roles)]
    return {"uid": uid, "status": status, "participants": participants}


def identity(*chats, mcp_url=None):
    return plow_init.Identity.model_validate({"chats": list(chats), "mcp_url": mcp_url})


@pytest.mark.parametrize("missing", ["chats", "mcp_url"])
def test_an_answer_missing_a_key_is_not_an_identity(missing):
    body = {"chats": [], "mcp_url": None}
    del body[missing]
    with pytest.raises(Exception, match="[Vv]alidation"):
        plow_init.Identity.model_validate(body)


def test_the_home_chat_is_the_owner_alone_with_this_agent():
    assert plow_init.home_chat(identity(chat("cht_home"), chat("cht_group", roles=("owner", "member")))).uid == "cht_home"


@pytest.mark.parametrize(
    "chats",
    [
        (),                                             # nothing at all
        (chat("a", roles=("owner", "member")),),        # a group
        (chat("a", status="pending"),),                 # not active yet
        (chat("a"), chat("b")),                         # two candidates
        (chat("a", roles=("member",)),),                # nobody is the owner
        (chat("a", agents=("self", "peer")),),          # another assistant is here too
    ],
)
def test_an_unclear_home_chat_refuses_and_says_what_it_saw(chats, parking):
    with pytest.raises(Parked):
        plow_init.home_chat(identity(*chats))
    assert "cannot tell which chat is home" in parking.read_text()


@pytest.mark.parametrize("mcp_url", [None, "https://relay.invalid/mcp"])
def test_a_relay_is_optional_however_it_is_spelled(mcp_url):
    assert identity(mcp_url=mcp_url).mcp_url == mcp_url


def test_a_symlinked_dotenv_is_refused_not_followed(tmp_path):
    """Root writes this file into a directory the agent can create entries in,
    and the runtime hands the file to the agent on every boot. A symlink left
    in its place must not be opened."""
    victim = tmp_path / "victim"
    victim.write_text("root-owned target\n")
    dotenv = tmp_path / ".env"
    dotenv.symlink_to(victim)
    plow_init.HOME_DOTENV = str(dotenv)
    with pytest.raises(Parked):
        plow_init.own_home_dotenv("a-key")
    assert victim.read_text() == "root-owned target\n"


def test_the_dotenv_carries_the_key_and_nothing_else(tmp_path, monkeypatch):
    """The tenant's credential is published to the environment; the one name
    that has to agree with a file is the only one written to it."""
    dotenv = tmp_path / ".env"
    plow_init.HOME_DOTENV = str(dotenv)
    # No `hermes` user here, and this test is not root.
    monkeypatch.setattr(plow_init.pwd, "getpwnam", lambda _: types.SimpleNamespace(pw_uid=0, pw_gid=0))
    monkeypatch.setattr(plow_init.os, "fchown", lambda *a, **k: None)
    plow_init.own_home_dotenv("a-key")
    assert dotenv.read_text() == "API_SERVER_KEY=a-key\n"


def test_the_dotenv_replaces_an_existing_foreign_owned_file(tmp_path, monkeypatch):
    """protected_regular=2 refuses O_CREAT on this existing path; rename works."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("API_SERVER_KEY=old\n")
    plow_init.HOME_DOTENV = str(dotenv)
    real_open = os.open

    def protected_open(path, flags, *args, **kwargs):
        if os.fspath(path) == str(dotenv) and flags & os.O_CREAT:
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(plow_init.os, "open", protected_open)
    monkeypatch.setattr(plow_init.pwd, "getpwnam", lambda _: types.SimpleNamespace(pw_uid=0, pw_gid=0))
    monkeypatch.setattr(plow_init.os, "fchown", lambda *a, **k: None)
    plow_init.own_home_dotenv("new")
    assert dotenv.read_text() == "API_SERVER_KEY=new\n"


@pytest.mark.parametrize(
    "template",
    ["{name}={value}", "export {name}={value}", "  {name}={value}", "'{name}'={value}", "\ufeff{name}={value}"],
    ids=["plain", "export", "leading-whitespace", "quoted-key", "byte-order-mark"],
)
def test_unrelated_keys_survive_but_a_stale_identity_does_not(tmp_path, monkeypatch, template):
    """Hermes loads this file OVER its process environment, so a persisted
    PLOW_AGENT_TOKEN would win over the one this boot just authenticated --
    a stale credential outliving its rotation, or a reused fleet home
    answering as the previous tenant. That holds for every spelling Hermes'
    own dotenv loader binds a name from -- plain, `export`-prefixed, leading
    whitespace, a single-quoted key, or a leading byte-order mark its
    utf-8-sig decode eats -- so this image has to recognise the name under
    all of them, not just the plain form. An operator's own key survives
    untouched in the same spelling (a mid-file BOM is not stripped and makes
    a different name, which is exactly why it survives). The same holds for
    the credential's legacy spellings: PLOW_CHAT_TOKEN and PLOW_CHAT_BASE_URL
    go, while PLOW_CHAT_CHAT_UID -- chat-directory data with live consumers,
    wrong rather than revoked when stale -- stays. A value holding `=`
    and quotes round-trips unparsed, and so does a quoted value spanning
    several lines whose continuation opens with an owned name -- one binding
    of the operator's key, not an assignment of the runtime's. Position is
    not asserted for API_SERVER_KEY: the merge drops every existing
    assignment of it and appends one, so the file never holds more than one
    regardless of where a reader would look."""
    dotenv = tmp_path / ".env"
    stale_token = template.format(name="PLOW_AGENT_TOKEN", value="stale-token")
    operator_key = template.format(name="AGENT_TZ", value="America/Los_Angeles")
    spanning = 'PLOW_CHAT_PROMPT="ask me\nPLOW_AGENT_TOKEN=is not a binding here\nabout it"'
    dotenv.write_text(
        f"{stale_token}\n"
        "PLOW_API_BASE=https://api.plow.co\n"
        "PLOW_CHAT_TOKEN=revoked\n"
        "PLOW_CHAT_BASE_URL=https://old.example\n"
        "PLOW_CHAT_CHAT_UID=cht_keep\n"
        "API_SERVER_KEY=old\n"
        f"{operator_key}\n"
        'PLOW_CHAT_FILTER=name="a=b" other=value\n'
        f"{spanning}\n",
        encoding="utf-8",
    )
    plow_init.HOME_DOTENV = str(dotenv)
    monkeypatch.setattr(plow_init.pwd, "getpwnam", lambda _: types.SimpleNamespace(pw_uid=0, pw_gid=0))
    monkeypatch.setattr(plow_init.os, "fchown", lambda *a, **k: None)
    plow_init.own_home_dotenv("new")
    assert dotenv.read_text(encoding="utf-8") == (
        "PLOW_CHAT_CHAT_UID=cht_keep\n"
        f"{operator_key}\n"
        'PLOW_CHAT_FILTER=name="a=b" other=value\n'
        f"{spanning}\n"
        "API_SERVER_KEY=new\n"
    )


BASE = "# Plow assistant\n\nbase rules\n"


def _seed(tmp_path, monkeypatch, base=BASE, persona=None):
    seed = tmp_path / "plow-seed"
    seed.mkdir()
    (seed / "SOUL.md").write_text(base)
    if persona is not None:
        (seed / "persona.md").write_text(persona)
    home = tmp_path / "hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(plow_init, "HOME_DIR", str(home))
    monkeypatch.setattr(plow_init, "SEED_SOUL", str(seed / "SOUL.md"))
    monkeypatch.setattr(plow_init, "SEED_PERSONA", str(seed / "persona.md"))
    monkeypatch.setattr(plow_init.pwd, "getpwnam", lambda _: types.SimpleNamespace(pw_uid=0, pw_gid=0))
    monkeypatch.setattr(plow_init.os, "fchown", lambda *a, **k: None)
    return home


def test_a_skills_directory_the_agent_replaced_with_a_link_is_refused(tmp_path, monkeypatch):
    """`os.chmod` follows a symlink even where the `os.chown` beside it does
    not, so root's mode change lands on whatever the agent pointed at."""
    home = _seed(tmp_path, monkeypatch)
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o700)
    (home / "skills").rmdir()
    (home / "skills").symlink_to(victim)
    with pytest.raises(Parked):
        plow_init.harden_home()
    assert victim.stat().st_mode & 0o7777 == 0o700


@pytest.mark.parametrize(
    "persona, stale, expected",
    [
        ("variant\n", None, BASE + "\nvariant\n"),
        (None, None, BASE),
        ("new\n", "old\n", BASE + "\nnew\n"),
    ],
    ids=["base+variant", "base-only", "replaces-stale-home"],
)
def test_identity_composition(tmp_path, monkeypatch, persona, stale, expected):
    """The base persona, then the variant's if it ships one -- and whatever an
    older image left in a volume home never shadows either
    (life-assistant-hermes-agent#168)."""
    home = _seed(tmp_path, monkeypatch, persona=persona)
    if stale is not None:
        (home / "SOUL.md").write_text(stale)
    plow_init.harden_home()
    soul = home / "SOUL.md"
    assert soul.read_text() == expected
    assert stat.S_IMODE(soul.stat().st_mode) == 0o644


def test_a_soul_the_agent_turned_into_a_link_is_replaced_not_written_through(tmp_path, monkeypatch):
    home = _seed(tmp_path, monkeypatch, persona="p\n")
    victim = tmp_path / "victim"
    victim.write_text("untouched\n")
    (home / "SOUL.md").symlink_to(victim)
    plow_init.harden_home()
    assert victim.read_text() == "untouched\n"
    assert not (home / "SOUL.md").is_symlink()
    assert (home / "SOUL.md").read_text().endswith("\np\n")


def test_a_base_image_without_its_persona_parks(tmp_path, monkeypatch, parking):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(plow_init, "SEED_SOUL", str(tmp_path / "missing" / "SOUL.md"))
    with pytest.raises(Parked):
        plow_init.harden_home()
    assert "SOUL.md" in parking.read_text()


def test_a_persona_this_image_cannot_read_parks_rather_than_raising(tmp_path, monkeypatch, parking):
    """An exception escaping the composition exits plow-init and panics the
    microVM, so a variant that shipped its persona in another encoding parks."""
    _seed(tmp_path, monkeypatch)
    (tmp_path / "plow-seed" / "persona.md").write_bytes(b"caf\xe9\n")
    with pytest.raises(Parked):
        plow_init.harden_home()
    assert "could not be composed" in parking.read_text()


INSTRUCTIONS = {"jsonrpc": "2.0", "id": 1, "result": {"instructions": "Use these.", "protocolVersion": "2025-06-18"}}
WRITTEN = "# Your owner's Mac, in Latch's own words\n\nUse these.\n"
PRIOR = "whatever a prior boot left here\n"  # HERMES.md is plow-init's, like SOUL.md; a prior file is overwritten


@pytest.mark.parametrize(
    "mcp_url, answer, before, after",
    [
        ("https://relay.invalid/mcp", json.dumps(INSTRUCTIONS), None, WRITTEN),
        ("https://relay.invalid/mcp", f"event: message\ndata: {json.dumps(INSTRUCTIONS)}\n\n", PRIOR, WRITTEN),
        ("https://relay.invalid/mcp", OSError("Mac is off"), PRIOR, None),
        ("https://relay.invalid/mcp", OSError("Mac is off"), None, None),
        ("https://relay.invalid/mcp", json.dumps({"result": {}}), PRIOR, None),
        (None, json.dumps(INSTRUCTIONS), PRIOR, None),
        (None, json.dumps(INSTRUCTIONS), None, None),
    ],
    ids=["writes-whole", "overwrites-any-prior", "failed-fetch-removes", "offline-first-boot",
         "no-instructions-removes", "no-mac-removes", "no-mac-nothing"],
)
def test_latch_instructions_become_hermes_md(tmp_path, monkeypatch, mcp_url, answer, before, after):
    """Latch's `initialize.instructions` is the routing rule Hermes drops on
    connect (#72). HERMES.md is plow-init's, like SOUL.md: a Mac writes it whole
    (overwriting any prior file), and no Mac or a failed fetch removes it so no
    stale routing survives. The boot never stops."""
    home = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(plow_init, "HERMES_MD", str(home / "HERMES.md"))
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        if isinstance(answer, Exception):
            raise answer
        return contextlib.nullcontext(types.SimpleNamespace(read=lambda: answer.encode()))

    monkeypatch.setattr(plow_init._no_redirect_opener, "open", urlopen)
    if before is not None:
        (home / "HERMES.md").write_text(before)
    plow_init.write_latch_instructions(identity(chat("cht_home"), mcp_url=mcp_url), "tok")
    written = (home / "HERMES.md").read_text() if (home / "HERMES.md").exists() else None
    assert written == after
    assert not list(home.glob(".HERMES.md.*"))
    if mcp_url is None:
        assert requests == []
    else:
        assert json.loads(requests[0].data)["method"] == "initialize"
        assert requests[0].get_header("Authorization") == "Bearer tok"
    if after == WRITTEN:
        assert stat.S_IMODE((home / "HERMES.md").stat().st_mode) == 0o644


@pytest.mark.parametrize("before", [None, PRIOR], ids=["no-prior-file", "prior-file-removed"])
def test_a_write_that_fails_leaves_no_hermes_md(tmp_path, monkeypatch, capsys, before):
    """A write that fails after a successful fetch leaves neither a staged temp
    nor a HERMES.md: a prior file is removed rather than left active with stale
    Mac-routing, and the failure is logged whether or not a prior file existed."""
    home = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(plow_init, "HERMES_MD", str(home / "HERMES.md"))
    if before is not None:
        (home / "HERMES.md").write_text(before)
    monkeypatch.setattr(plow_init._no_redirect_opener, "open",
                        lambda request, timeout: contextlib.nullcontext(
                            types.SimpleNamespace(read=lambda: json.dumps(INSTRUCTIONS).encode())))

    def replace(*_):
        raise OSError("disk full")

    monkeypatch.setattr(plow_init.os, "replace", replace)
    plow_init.write_latch_instructions(identity(chat("cht_home"), mcp_url="https://relay.invalid/mcp"), "tok")
    assert not (home / "HERMES.md").exists()  # fell to absent, not a stale prior file
    assert not list(home.glob(".HERMES.md.*"))
    assert "not written" in capsys.readouterr().err  # the failure is logged either way


class _Relay302(urllib.request.BaseHandler):
    """A relay that answers the POST with a cross-host 302, and records every
    URL it is asked for -- so a test can prove the redirect target was never
    requested (which is where the bearer token would have gone)."""

    handler_order = 100  # ahead of the real HTTP(S) handlers, so no socket opens

    def __init__(self):
        self.requested = []

    def _answer(self, req):
        self.requested.append(req.full_url)
        headers = http.client.HTTPMessage()
        headers["Location"] = "https://attacker.invalid/steal"
        response = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
        response.msg = "Found"
        return response

    http_open = https_open = _answer


def test_a_stale_hermes_md_that_cannot_be_removed_does_not_stop_the_boot(tmp_path, monkeypatch, capsys):
    """The no-Mac cleanup is non-fatal: an unlink that fails (a permission
    error, not merely an absent file) logs and lets the boot continue."""
    home = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(plow_init, "HERMES_MD", str(home / "HERMES.md"))
    (home / "HERMES.md").write_text(PRIOR)

    def denied(*_):
        raise PermissionError("denied")

    monkeypatch.setattr(plow_init.os, "unlink", denied)
    plow_init.write_latch_instructions(identity(chat("cht_home"), mcp_url=None), "tok")
    assert "could not remove" in capsys.readouterr().err
    assert (home / "HERMES.md").read_text() == PRIOR  # unlink failed -> file untouched


def test_instructions_with_an_unpaired_surrogate_do_not_stop_the_boot(tmp_path, monkeypatch):
    """A lone surrogate in the fetched instructions cannot be written to a
    UTF-8 file; that is a non-fatal write failure, not a boot crash."""
    home = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(plow_init, "HERMES_MD", str(home / "HERMES.md"))
    answer = '{"result": {"instructions": "\\ud800 lone surrogate"}}'  # json escapes it; loads to a real one
    monkeypatch.setattr(plow_init._no_redirect_opener, "open",
                        lambda request, timeout: contextlib.nullcontext(
                            types.SimpleNamespace(read=lambda: answer.encode())))
    plow_init.write_latch_instructions(identity(chat("cht_home"), mcp_url="https://relay.invalid/mcp"), "tok")
    assert not (home / "HERMES.md").exists()
    assert not list(home.glob(".HERMES.md.*"))


def test_a_relay_error_reason_phrase_cannot_forge_the_boot_log(tmp_path, monkeypatch, capsys):
    """A hostile relay's HTTP error reason phrase reaches the fetch-failure log
    line; terminal-control bytes in it must be stripped before they do."""
    home = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(plow_init, "HERMES_MD", str(home / "HERMES.md"))
    (home / "HERMES.md").write_text(PRIOR)
    esc, reason = chr(27), "boom" + chr(27) + "[2J" + chr(10) + "forged log line"  # ESC + embedded newline
    crafted = plow_init.urllib.error.HTTPError(
        "https://relay.invalid/mcp", 500, reason, http.client.HTTPMessage(), None)

    def raise_crafted(request, timeout):
        raise crafted

    monkeypatch.setattr(plow_init._no_redirect_opener, "open", raise_crafted)
    plow_init.write_latch_instructions(identity(chat("cht_home"), mcp_url="https://relay.invalid/mcp"), "tok")
    err = capsys.readouterr().err
    assert "the fetch failed" in err and "forged log line" in err  # the text survives, sanitized
    assert esc not in err                # the real ESC byte was stripped
    assert err.count(chr(10)) == 1       # one log line -- the embedded newline did not forge a second
    assert not (home / "HERMES.md").exists()  # the stale managed file was still removed


def test_a_relay_redirect_never_forwards_the_bearer_token(monkeypatch):
    """A compromised Mac answering the transparent relay with a 302 must not
    make plow-init re-send the line-scoped token to the redirect target."""
    relay = _Relay302()
    monkeypatch.setattr(plow_init, "_no_redirect_opener",
                        urllib.request.build_opener(plow_init._RefuseRedirects, relay))
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        plow_init.fetch_latch_instructions("https://relay.invalid/mcp", "line-token")
    assert relay.requested == ["https://relay.invalid/mcp"]
    assert "attacker.invalid" not in str(excinfo.value)  # the Mac-controlled Location never reaches the log


SEED = {
    "model": {"provider": "plow", "default": "seeded/model",
              "base_url": "${PLOW_API_BASE}/v1", "key_env": "HERMES_CUSTOM_PLOW_API_KEY"},
    "providers": {"plow": {"name": "plow", "base_url": "${PLOW_API_BASE}/v1",
                           "key_env": "HERMES_CUSTOM_PLOW_API_KEY", "stale_timeout_seconds": 55,
                           "models": {"seeded/model": {}}}},
    "mcp_servers": {"plow": {"url": "${PLOW_MCP_URL}", "headers": {"Authorization": "Bearer ${PLOW_AGENT_TOKEN}"},
                             "enabled": False},
                    "theirs": {"enabled": True}},
    "platforms": {"plow_chat": {"enabled": True}},
    "agent": {"api_max_retries": 9},
    "cron": {"model_drift_guard": False},
    "display": {"busy_ack_enabled": False, "platforms": {"plow_chat": {"tool_progress": "off"}}},
    "tools": {"tool_search": {"enabled": "off"}},
    "terminal": {"backend": "local", "cwd": "/var/lib/hermes"},
}


def configure(tmp_path, mcp_url=None, env=None):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(SEED))
    plow_init.CONFIG = str(config)
    os.environ.pop("HERMES_PROVIDER", None)
    os.environ.pop("HERMES_MODEL", None)
    os.environ.update(env or {})
    plow_init.configure(identity(mcp_url=mcp_url), SEED)
    return yaml.safe_load(config.read_text())


def test_it_writes_the_settings_it_owns_and_nothing_else(tmp_path):
    after = configure(tmp_path, mcp_url="https://relay.invalid/mcp",
                      env={"HERMES_PROVIDER": "anthropic", "HERMES_MODEL": "claude-sonnet-4-5"})
    assert after["model"]["provider"] == "anthropic"
    assert after["model"]["default"] == "claude-sonnet-4-5"
    assert after["mcp_servers"]["plow"] == {**SEED["mcp_servers"]["plow"], "enabled": True}
    # Somebody else's MCP server, and everything else, untouched.
    assert after["mcp_servers"]["theirs"] == SEED["mcp_servers"]["theirs"]
    assert after["platforms"] == SEED["platforms"]


def test_a_home_that_predates_a_seed_change_takes_the_seeds_invariants(tmp_path, monkeypatch):
    # cont-init seeds only an absent config.yaml, so an existing home carries
    # whatever it was seeded with -- a retry budget of 3, the gateway's noisy
    # display defaults, and no tool_search switch, before 2026-09-03 -- until
    # configure() reconciles it on boot.
    monkeypatch.setenv("PLOW_API_BASE", "https://api.test.invalid")
    config = tmp_path / "config.yaml"
    stale = {**{k: v for k, v in SEED.items() if k not in ("tools", "cron")}, "agent": {"api_max_retries": 3},
             "mcp_servers": {"plow": {"enabled": True}, "theirs": SEED["mcp_servers"]["theirs"]},
             "providers": {"plow": {"name": "plow", "base_url": "${PLOW_API_BASE}/v1",
                                    "model": "seeded/model", "models": {SEED["model"]["default"]: {}}},
                           "theirs": {"base_url": "https://elsewhere.invalid"}},
             "display": {"busy_ack_enabled": True, "platforms": {"plow_chat": {"tool_progress": "all"}}}}
    config.write_text(yaml.safe_dump(stale))
    plow_init.CONFIG = str(config)
    plow_init.configure(identity(), SEED)
    after = yaml.safe_load(config.read_text())
    assert after["agent"]["api_max_retries"] == 9
    assert after["display"] == SEED["display"]
    assert after["tools"]["tool_search"]["enabled"] == "off"
    assert after["cron"]["model_drift_guard"] is False
    assert after["terminal"]["cwd"] == "/var/lib/hermes"
    # Prompt caching: Hermes matches the declaration on the endpoint and the
    # model id, and the seed's `${PLOW_API_BASE}` reference never equals the URL
    # the agent dials -- an entry carrying it is one the match cannot find.
    # The entry is the seed's, whole: the credential a pre-key_env home never
    # got (the 2026-09-04 outage), minus the entry-level `model` an older seed
    # wrote (a second selector Hermes' auxiliary path preferred), plus the
    # expanded endpoint and the caching flag under the selected model.
    assert after["providers"]["plow"] == {**SEED["providers"]["plow"],
                                          "base_url": "https://api.test.invalid/v1",
                                          "models": {"seeded/model": {"prompt_caching": True}}}
    assert after["providers"]["theirs"] == {"base_url": "https://elsewhere.invalid"}
    # The relay entry the same way (#45): a home with only `enabled` gets the
    # seed's `url` and `headers` back, and `theirs` is untouched.
    assert after["mcp_servers"]["plow"] == {**SEED["mcp_servers"]["plow"], "enabled": False}
    assert after["mcp_servers"]["theirs"] == SEED["mcp_servers"]["theirs"]


def test_a_model_is_written_only_when_one_is_asked_for(tmp_path):
    assert configure(tmp_path)["model"]["default"] == "seeded/model"


def test_switching_away_from_plow_takes_plows_endpoint_with_it(tmp_path):
    """`base_url` and `key_env` describe Plow. Left behind under another
    provider they send every call back to Plow with Plow's credential."""
    after = configure(tmp_path, env={"HERMES_PROVIDER": "anthropic", "HERMES_MODEL": "claude-sonnet-4-5"})
    assert "base_url" not in after["model"]
    assert "key_env" not in after["model"]


def test_switching_back_restores_it_from_the_seed(tmp_path):
    configure(tmp_path, env={"HERMES_PROVIDER": "anthropic", "HERMES_MODEL": "m"})
    config = tmp_path / "config.yaml"
    os.environ.update({"HERMES_PROVIDER": "plow", "HERMES_MODEL": "seeded/model"})
    plow_init.configure(identity(), SEED)
    after = yaml.safe_load(config.read_text())
    assert after["model"]["base_url"] == SEED["model"]["base_url"]
    assert after["model"]["key_env"] == SEED["model"]["key_env"]


def test_a_switch_back_takes_the_other_providers_model_with_it(tmp_path):
    """A model id belongs to the provider it was written for. Restoring Plow's
    endpoint and leaving somebody else's model is the pair the two-knob
    contract exists to prevent."""
    configure(tmp_path, env={"HERMES_PROVIDER": "anthropic", "HERMES_MODEL": "claude-sonnet-4-5"})
    config = tmp_path / "config.yaml"
    os.environ.pop("HERMES_PROVIDER", None)
    os.environ.pop("HERMES_MODEL", None)
    plow_init.configure(identity(), SEED)
    after = yaml.safe_load(config.read_text())
    assert after["model"]["provider"] == "plow"
    assert after["model"]["default"] == "seeded/model"


def test_the_config_is_published_by_rename(tmp_path):
    """An interrupted dump must not be able to leave a truncated config.yaml
    that the next boot then keeps."""
    after = configure(tmp_path, env={"HERMES_PROVIDER": "anthropic", "HERMES_MODEL": "m"})
    assert after["model"]["provider"] == "anthropic"
    assert not (tmp_path / "config.yaml.tmp").exists()
    assert (tmp_path / "config.yaml").stat().st_mode & 0o777 == 0o640


def test_an_unchanged_config_is_not_rewritten(tmp_path):
    config = tmp_path / "config.yaml"
    configure(tmp_path)
    before = config.stat().st_mtime_ns
    plow_init.configure(identity(), SEED)
    assert config.stat().st_mtime_ns == before


def test_upstream_main_hermes_waits_for_plow_init():
    dependency = SOURCE.parents[1] / "s6-rc.d/main-hermes/dependencies.d/plow-init"
    assert dependency.is_file()

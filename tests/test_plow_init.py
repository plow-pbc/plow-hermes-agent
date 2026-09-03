"""What `plow-init` decides, without booting anything.

    uv run --with pydantic --with pydantic-settings --with pyyaml pytest

Covers the three decisions the image cannot afford to get wrong: which
credential files it will read, what it does with each answer from Plow, and
which settings it writes into the agent's config.
"""

import importlib.util
import os
import pathlib
import stat
import sys
import types

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
def test_a_credential_anyone_else_can_reach_is_refused(tmp_path, owned_by_root, mode):
    credential(tmp_path, mode=mode)
    with pytest.raises(SystemExit, match="expected root:root at 600 or 400"):
        plow_init.read_credentials()


def test_a_refused_credential_does_not_print_the_token(tmp_path, owned_by_root):
    """This message goes to s6's stderr, which is the container's log.

    Pydantic quotes the offending input by default, and for a missing key that
    input is everything it did parse -- so the file's own token rides along.
    """
    credential(tmp_path, "PLOW_AGENT_TOKEN=sk-the-real-one\n")
    with pytest.raises(SystemExit) as refusal:
        plow_init.read_credentials()
    assert "plow_api_base" in str(refusal.value)
    assert "sk-the-real-one" not in str(refusal.value)


def test_a_third_key_is_refused(tmp_path, owned_by_root):
    credential(tmp_path, "PLOW_API_BASE=x\nPLOW_AGENT_TOKEN=t\nPLOW_HOME_CHANNEL=cht_host_says\n")
    with pytest.raises(SystemExit, match="not the two lines this image reads"):
        plow_init.read_credentials()


def test_a_missing_credential_is_refused(tmp_path):
    plow_init.CREDENTIALS = str(tmp_path / "absent")
    plow_init.CREDENTIALS_WAIT_S = 1
    with pytest.raises(SystemExit, match="no credential at"):
        plow_init.read_credentials()


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
def test_an_unclear_home_chat_refuses_and_says_what_it_saw(chats):
    with pytest.raises(SystemExit, match="cannot tell which chat is home"):
        plow_init.home_chat(identity(*chats))


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
    with pytest.raises(SystemExit, match="not a regular file this image can write"):
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


@pytest.mark.parametrize("entry", ["skills", "SOUL.md"])
def test_a_home_entry_the_agent_replaced_with_a_link_is_refused(tmp_path, monkeypatch, entry):
    """`os.chmod` follows a symlink even where the `os.chown` beside it does
    not, so root's mode change lands on whatever the agent pointed at."""
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "SOUL.md").write_text("identity\n")
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o700)
    replaced = home / entry
    replaced.unlink() if replaced.is_file() else replaced.rmdir()
    replaced.symlink_to(victim)
    plow_init.HOME_DIR = str(home)
    # Ownership needs root; the modes below are real, and they are the point.
    monkeypatch.setattr(plow_init.pwd, "getpwnam", lambda _: types.SimpleNamespace(pw_uid=0, pw_gid=0))
    for call in ("chown", "fchown"):
        monkeypatch.setattr(plow_init.os, call, lambda *a, **k: None)
    with pytest.raises(SystemExit, match="not the file this image left there"):
        plow_init.harden_home()
    assert victim.stat().st_mode & 0o7777 == 0o700


def test_a_permissive_soul_is_taken_back_to_0644(tmp_path, monkeypatch):
    """Ownership alone leaves a 0666 SOUL.md writable by the agent it is meant
    to constrain, so the mode is asserted rather than inherited."""
    home = tmp_path / "hermes"
    (home / "skills").mkdir(parents=True)
    soul = home / "SOUL.md"
    soul.write_text("the identity\n")
    soul.chmod(0o666)
    monkeypatch.setattr(plow_init, "HOME_DIR", str(home))
    monkeypatch.setattr(plow_init.pwd, "getpwnam", lambda _: types.SimpleNamespace(pw_uid=0, pw_gid=0))
    monkeypatch.setattr(plow_init.os, "fchown", lambda *a, **k: None)
    plow_init.harden_home()
    assert stat.S_IMODE(soul.stat().st_mode) == 0o644


SEED = {
    "model": {"provider": "plow", "default": "seeded/model",
              "base_url": "${PLOW_API_BASE}/v1", "key_env": "HERMES_CUSTOM_PLOW_API_KEY"},
    "mcp_servers": {"plow": {"enabled": False}, "theirs": {"enabled": True}},
    "platforms": {"plow_chat": {"enabled": True}},
    "agent": {"api_max_retries": 9},
    "display": {"busy_ack_enabled": False, "platforms": {"plow_chat": {"tool_progress": "off"}}},
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
    assert after["mcp_servers"]["plow"]["enabled"] is True
    # Somebody else's MCP server, and everything else, untouched.
    assert after["mcp_servers"]["theirs"] == SEED["mcp_servers"]["theirs"]
    assert after["platforms"] == SEED["platforms"]


def test_a_home_that_predates_a_seed_change_takes_the_seeds_invariants(tmp_path):
    # cont-init seeds only an absent config.yaml, so an existing home carries
    # whatever it was seeded with -- a retry budget of 3 and the gateway's
    # noisy display defaults, before 2026-09-03 -- until configure()
    # reconciles it on boot.
    config = tmp_path / "config.yaml"
    stale = {**SEED, "agent": {"api_max_retries": 3},
             "display": {"busy_ack_enabled": True, "platforms": {"plow_chat": {"tool_progress": "all"}}}}
    config.write_text(yaml.safe_dump(stale))
    plow_init.CONFIG = str(config)
    plow_init.configure(identity(), SEED)
    after = yaml.safe_load(config.read_text())
    assert after["agent"]["api_max_retries"] == 9
    assert after["display"] == SEED["display"]


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

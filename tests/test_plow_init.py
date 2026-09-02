"""What `plow-init` decides, without booting anything.

    uv run --with pydantic --with pydantic-settings --with pyyaml pytest

Covers the three decisions the image cannot afford to get wrong: which
credential files it will read, what it does with each answer from Plow, and
which settings it writes into the agent's config.
"""

import importlib.util
import os
import pathlib
import sys

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


def test_a_credential_at_0600_is_read(tmp_path, owned_by_root):
    credential(tmp_path)
    assert plow_init.read_credentials().plow_agent_token == "t"


@pytest.mark.parametrize("mode", [0o644, 0o620, 0o602, 0o666])
def test_a_credential_anyone_else_can_reach_is_refused(tmp_path, owned_by_root, mode):
    credential(tmp_path, mode=mode)
    with pytest.raises(SystemExit, match="expected root:root at 600 or 400"):
        plow_init.read_credentials()


def test_a_third_key_is_refused(tmp_path, owned_by_root):
    credential(tmp_path, "PLOW_API_BASE=x\nPLOW_AGENT_TOKEN=t\nPLOW_HOME_CHANNEL=cht_host_says\n")
    with pytest.raises(SystemExit, match="not the two lines this image reads"):
        plow_init.read_credentials()


def test_a_missing_credential_is_refused(tmp_path):
    plow_init.CREDENTIALS = str(tmp_path / "absent")
    plow_init.CREDENTIALS_WAIT_S = 1
    with pytest.raises(SystemExit, match="no credential at"):
        plow_init.read_credentials()


def chat(uid, status="active", roles=("owner",), selves=1):
    agents = [{"type": "agent", "relationship": "self"} for _ in range(selves)]
    members = [{"type": "member", "uid": f"m{n}", "role": r} for n, r in enumerate(roles)]
    return {"uid": uid, "status": status, "participants": agents + members}


def identity(*chats, mcp_url=None):
    return plow_init.Identity.model_validate({"chats": list(chats), "mcp_url": mcp_url})


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
    ],
)
def test_an_unclear_home_chat_refuses_and_says_what_it_saw(chats):
    with pytest.raises(SystemExit, match="cannot tell which chat is home"):
        plow_init.home_chat(identity(*chats))


@pytest.mark.parametrize("mcp_url", [None, "https://relay.invalid/mcp"])
def test_a_relay_is_optional_however_it_is_spelled(mcp_url):
    assert identity(mcp_url=mcp_url).mcp_url == mcp_url


SEED = {
    "model": {"provider": "plow", "default": "seeded/model", "base_url": "${PLOW_API_BASE}/v1"},
    "mcp_servers": {"plow": {"enabled": False}, "theirs": {"enabled": True}},
    "platforms": {"plow_chat": {"enabled": True}},
}


def configure(tmp_path, mcp_url=None, env=None):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(SEED))
    plow_init.CONFIG = str(config)
    os.environ.pop("HERMES_PROVIDER", None)
    os.environ.pop("HERMES_MODEL", None)
    os.environ.update(env or {})
    plow_init.configure(plow_init.Identity.model_validate({"chats": [], "mcp_url": mcp_url}))
    return yaml.safe_load(config.read_text())


def test_it_writes_the_three_settings_it_owns_and_nothing_else(tmp_path):
    after = configure(tmp_path, mcp_url="https://relay.invalid/mcp",
                      env={"HERMES_PROVIDER": "anthropic", "HERMES_MODEL": "claude-sonnet-4-5"})
    assert after["model"]["provider"] == "anthropic"
    assert after["model"]["default"] == "claude-sonnet-4-5"
    assert after["mcp_servers"]["plow"]["enabled"] is True
    # Somebody else's MCP server, and everything else, untouched.
    assert after["mcp_servers"]["theirs"] == SEED["mcp_servers"]["theirs"]
    assert after["model"]["base_url"] == SEED["model"]["base_url"]
    assert after["platforms"] == SEED["platforms"]


def test_a_model_is_written_only_when_one_is_asked_for(tmp_path):
    assert configure(tmp_path)["model"]["default"] == "seeded/model"


def test_an_unchanged_config_is_not_rewritten(tmp_path):
    config = tmp_path / "config.yaml"
    configure(tmp_path)
    before = config.stat().st_mtime_ns
    plow_init.configure(plow_init.Identity.model_validate({"chats": [], "mcp_url": None}))
    assert config.stat().st_mtime_ns == before

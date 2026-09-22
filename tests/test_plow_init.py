"""What `plow-init` decides, without booting anything.

    uv run --with pydantic --with pydantic-settings --with python-dotenv --with pyyaml pytest

Covers the three decisions the image cannot afford to get wrong: where it
reads Plow's endpoint from, what it does with each answer from Plow, and
which settings it writes into the agent's config.
"""

import asyncio
import contextlib
import http.client
import importlib.util
import io
import json
import os
import pathlib
import stat
import sys
import threading
import time
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

    The mode is real; only the owner is pretended, so the mode check still
    exercises the code that reads it.
    """

    class RootOwned:
        def __init__(self, info):
            self.st_mode, self.st_uid, self.st_gid = info.st_mode, 0, 0

    real = os.lstat
    monkeypatch.setattr(plow_init.os, "lstat", lambda path: RootOwned(real(path)))


@pytest.fixture
def bearer_sent(monkeypatch):
    """What `ask_plow` puts in the Authorization header."""
    sent = []

    def answer(request, timeout):
        sent.append(request.get_header("Authorization"))
        return io.BytesIO(json.dumps({"line": {"uid": "ln_own"}, "chats": [], "mcp_url": None}).encode())

    monkeypatch.setattr(plow_init.urllib.request, "urlopen", answer)
    return lambda: (plow_init.ask_plow(plow_init.read_credentials()), sent)[1]


@pytest.fixture
def no_host_file(monkeypatch, tmp_path):
    monkeypatch.setattr(plow_init, "HOST_SETUP", str(tmp_path / "no-setup"))
    monkeypatch.setattr(plow_init, "CREDENTIALS", str(tmp_path / "no-credentials"))
    monkeypatch.setattr(plow_init, "CREDENTIALS_WAIT_S", 1)


@pytest.mark.parametrize("token, bearer", [(None, "Bearer proxied"), ("sk-real", "Bearer sk-real")])
def test_the_environment_token_is_used_and_only_an_absent_one_is_the_placeholder(
    monkeypatch, bearer_sent, no_host_file, token, bearer
):
    """exe.dev sets no token and its proxy adds one; a developer's compose sets
    the real one, which the placeholder must never replace."""
    monkeypatch.setenv("PLOW_API_BASE", "https://api.plow.co")
    if token is None:
        monkeypatch.delenv("PLOW_AGENT_TOKEN", raising=False)
    else:
        monkeypatch.setenv("PLOW_AGENT_TOKEN", token)
    assert bearer_sent() == [bearer]


def test_without_plow_api_base_the_pre_2007_file_is_read(monkeypatch, tmp_path, owned_by_root, bearer_sent):
    monkeypatch.delenv("PLOW_API_BASE", raising=False)
    monkeypatch.setenv("PLOW_AGENT_TOKEN", "sk-env-not-the-file")
    monkeypatch.setattr(plow_init, "HOST_SETUP", str(tmp_path / "no-setup"))
    path = tmp_path / "credentials"
    path.write_text("PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=sk-file\nAGENT_ID=life\n")
    path.chmod(0o600)
    monkeypatch.setattr(plow_init, "CREDENTIALS", str(path))
    assert plow_init.read_credentials().agent_id == "life"
    assert bearer_sent() == ["Bearer sk-file"]


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


def test_no_plow_api_base_and_no_file_parks_rather_than_exiting(monkeypatch, parking, no_host_file):
    """The warm pool's normal life, and the path that started all this.

    Exiting here is what panics the microVM: plow-init's non-zero exit takes
    /init with it, and /init is PID 1.
    """
    monkeypatch.delenv("PLOW_API_BASE", raising=False)
    with pytest.raises(Parked):
        plow_init.read_credentials()
    assert "no PLOW_API_BASE in the environment" in parking.read_text()


def test_parking_says_why_on_stderr_as_well_as_in_the_marker(parking, capsys):
    with pytest.raises(Parked):
        plow_init.park("no PLOW_API_BASE in the environment")
    assert "no PLOW_API_BASE" in capsys.readouterr().err
    assert parking.read_text() == "no PLOW_API_BASE in the environment\n"


def test_an_unwritable_marker_still_parks(monkeypatch, tmp_path, capsys):
    """The marker is for a human with a shell. Parking is the behaviour."""
    monkeypatch.setattr(plow_init, "PARK_MARKER", str(tmp_path / "no-such-dir" / "parked"))
    with pytest.raises(Parked):
        plow_init.park("no PLOW_API_BASE")
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


def chat(uid, status="active", roles=("owner",), agents=("self",), display_name=None, provider_key=None, line="ln_own"):
    participants = [{"type": "agent", "relationship": rel, "line": {"uid": line}} for rel in agents]
    participants += [{"type": "member", "uid": f"m{n}", "role": r, "display_name": display_name,
                      "provider_key": provider_key} for n, r in enumerate(roles)]
    return {"uid": uid, "status": status, "participants": participants}


def identity(*chats, mcp_url=None):
    return plow_init.Identity.model_validate({"line": {"uid": "ln_own"}, "chats": list(chats), "mcp_url": mcp_url})


@pytest.fixture
def unhurried(monkeypatch):
    """Let the boot's waiting run at no cost: every sleep moves the clock
    ask_plow reads, so a 120s window is a few hundred iterations, not 120s."""
    clock = [0.0]
    monkeypatch.setattr(plow_init.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(plow_init.time, "monotonic", lambda: clock[0])


def refusing(monkeypatch, answers):
    """Answer the identity call from `answers` -- an int is that HTTP status,
    None is a healthy identity, "malformed" is an invalid one. The last entry repeats."""
    def answer(request, timeout):
        code = answers.pop(0) if len(answers) > 1 else answers[0]
        if code is None:
            return io.BytesIO(json.dumps({"line": {"uid": "ln_own"}, "chats": [], "mcp_url": None}).encode())
        if code == "malformed":
            return io.BytesIO(b"{}")
        raise urllib.error.HTTPError(request.full_url, code, "refused", {}, None)

    monkeypatch.setattr(plow_init.urllib.request, "urlopen", answer)


@pytest.mark.parametrize("waiting", [False, True])
@pytest.mark.parametrize("code", [401, 403])
def test_a_credential_that_has_not_taken_yet_is_waited_out(monkeypatch, unhurried, code, capsys, waiting):
    """A rotated bearer is refused for about a minute before it answers, and
    the restart that follows a rotation is the boot that asks. Parking on the
    first 401 leaves that agent down until somebody notices."""
    monkeypatch.setenv("PLOW_API_BASE", "https://api.plow.co")
    refusing(monkeypatch, [code, code, None])
    assert plow_init.ask_plow(plow_init.read_credentials(), waiting=waiting).line.uid == "ln_own"
    assert capsys.readouterr().err.count("waiting for the credential to take") == 2


@pytest.mark.parametrize("waiting", [False, True])
@pytest.mark.parametrize("code", [401, 403])
def test_a_credential_refused_for_the_whole_window_still_parks(monkeypatch, unhurried, parking, code, waiting):
    """Waited out, not believed: a revoked credential is still terminal, and an
    agent that cannot be told who it is must not come up as whoever it was."""
    monkeypatch.setenv("PLOW_API_BASE", "https://api.plow.co")
    refusing(monkeypatch, [code])
    with pytest.raises(Parked):
        plow_init.ask_plow(plow_init.read_credentials(), waiting=waiting)
    assert plow_init.time.monotonic() == plow_init.AUTH_WAIT_S
    assert f"answered {code} for {plow_init.AUTH_WAIT_S}s" in parking.read_text()


@pytest.mark.parametrize("waiting", [False, True])
@pytest.mark.parametrize("code, reason", [(404, "answered 404"), ("malformed", "not an identity")])
def test_a_missing_agent_or_malformed_identity_parks_immediately(monkeypatch, unhurried, parking, code, reason, waiting):
    monkeypatch.setenv("PLOW_API_BASE", "https://api.plow.co")
    refusing(monkeypatch, [code])
    with pytest.raises(Parked):
        plow_init.ask_plow(plow_init.read_credentials(), waiting=waiting)
    assert plow_init.time.monotonic() == 0
    assert reason in parking.read_text()


@pytest.mark.parametrize("missing", ["line", "chats", "mcp_url"])
def test_an_answer_missing_a_key_is_not_an_identity(missing):
    body = {"line": {"uid": "ln_own"}, "chats": [], "mcp_url": None}
    del body[missing]
    with pytest.raises(Exception, match="[Vv]alidation"):
        plow_init.Identity.model_validate(body)


def test_the_home_chat_is_the_owner_alone_with_this_agent_on_its_own_line():
    """A mailbox carrying this agent's persona is another line the credential
    opens, and the owner alone with it reads as owner-plus-self too."""
    chats = (chat("cht_home"), chat("cht_group", roles=("owner", "member")), chat("cht_mail", line="ln_mailbox"))
    assert plow_init.home_chat(identity(*chats)).uid == "cht_home"


@pytest.mark.parametrize(
    "chats",
    [
        (),                                             # nothing at all
        (chat("a", roles=("owner", "member")),),        # a group
        (chat("a", status="pending"),),                 # not active yet
        (chat("a", roles=("member",)),),                # nobody is the owner
        (chat("a", agents=("self", "peer")),),          # another assistant is here too
        (chat("a", line="ln_mailbox"),),                # only the persona's mailbox, not this line
    ],
)
def test_without_a_qualifying_home_chat_returns_none(chats, parking):
    assert plow_init.home_chat(identity(*chats)) is None
    assert not parking.exists()


def test_multiple_home_chats_park_and_say_what_they_saw(parking):
    with pytest.raises(Parked):
        plow_init.home_chat(identity(chat("a"), chat("b")))
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
    with pytest.raises(OSError):
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


@pytest.mark.parametrize(
    "before, display_name, provider_key, name_line",
    [
        (None, "Ada", "+15555550123", "The owner is Ada."),
        (None, None, "+15555550123", None),
        (None, "+15555550123", "+15555550123", None),          # Plow's handle fallback is not a name
        (None, "Ada\n§\nBob", "m0", "The owner is Ada § Bob."),  # a name cannot spell the delimiter
        ("what the agent made of it", "Ada", "m0", None),      # an edited file is left alone
    ],
    ids=["named", "no-name", "handle-fallback-is-not-a-name", "name-cannot-spell-the-delimiter", "edited-since"],
)
def test_the_first_user_profile_carries_the_owners_own_facts(tmp_path, monkeypatch, before, display_name, provider_key, name_line):
    """A fresh agent's USER.md is read as its own knowledge on every turn
    (#73): the owner's real name (never the phone/email handle Plow falls back
    to), that this server holds only its own work, and that other lines and
    earlier agents left theirs on the Mac. Written
    in the store's own shape, and never over one that exists. Where the world
    lives is HERMES.md's job (#74), not restated here."""
    home = _seed(tmp_path, monkeypatch)
    user_md = home / "memories" / "USER.md"
    if before is not None:
        user_md.parent.mkdir()
        user_md.write_text(before)
    owner_chat = chat("cht_home", display_name=display_name, provider_key=provider_key)
    plow_init.seed_user_profile(plow_init.home_chat(identity(owner_chat)))
    text = user_md.read_text()
    if before is not None:
        assert text == before  # untouched
        return
    entries = text.split("\n§\n")
    assert text == "\n§\n".join(e.strip() for e in entries)  # the store's own round-trip, or it backs the file up as drift
    assert len(text) <= 1375  # user_char_limit is over the whole file
    for fact in ("This server holds only", "other Plow lines", "Messages and mail", "Mac"):
        assert fact in text
    if name_line:
        assert entries[0] == name_line
        assert len(entries) == 2
    else:
        assert "The owner is" not in text
        assert len(entries) == 1
    assert provider_key not in text  # the handle never reaches the prompt


def test_a_failed_user_profile_write_leaves_no_file(tmp_path, monkeypatch):
    """The profile is published by hard-link only once its contents are
    written, so a failure mid-publish leaves no partial USER.md a later boot
    would then preserve."""
    home = _seed(tmp_path, monkeypatch)

    def disk_full(*_):
        raise OSError("disk full")

    monkeypatch.setattr(plow_init.os, "link", disk_full)
    with pytest.raises(OSError):
        plow_init.seed_user_profile(plow_init.home_chat(identity(chat("cht_home", display_name="Ada"))))
    assert not (home / "memories" / "USER.md").exists()
    assert not list((home / "memories").glob(".USER.md.*"))
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
    "gateway": {"message_timestamps": {"enabled": True}},
    "compression": {"threshold_tokens": 128000},
    "auxiliary": {"vision": {"provider": "plow", "model": "anthropic/claude-sonnet-5"}},
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
    assert after["cron"]["model_provider"] == "anthropic"
    assert after["model"]["default"] == "claude-sonnet-4-5"
    assert after["mcp_servers"]["plow"] == {**SEED["mcp_servers"]["plow"], "enabled": True}
    # Somebody else's MCP server, and everything else, untouched.
    assert after["mcp_servers"]["theirs"] == SEED["mcp_servers"]["theirs"]
    assert after["platforms"] == SEED["platforms"]


def test_an_owners_message_timestamps_off_is_turned_back_on(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({**SEED, "gateway": {"message_timestamps": {"enabled": False}}}))
    plow_init.CONFIG = str(config)
    plow_init.configure(identity(), SEED)
    assert yaml.safe_load(config.read_text())["gateway"]["message_timestamps"] == {"enabled": True}


def zone_for_boot(monkeypatch, env):
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return plow_init.default_timezone()


def test_an_agent_with_no_zone_anywhere_gets_the_pacific_default(monkeypatch):
    assert zone_for_boot(monkeypatch, {}) == {"HERMES_TIMEZONE": "America/Los_Angeles"}


@pytest.mark.parametrize("env", [
    # A variant's explicit UTC, set before its owner has said where they live.
    {"TZ": "UTC"},
    {"TZ": "America/Chicago"},
    {"HERMES_TIMEZONE": "Europe/Berlin"},
])
def test_a_zone_already_named_beats_the_default(monkeypatch, env):
    assert zone_for_boot(monkeypatch, env) == {}


def test_a_home_that_predates_a_seed_change_takes_the_seeds_invariants(tmp_path, monkeypatch):
    # cont-init seeds only an absent config.yaml, so an existing home carries
    # whatever it was seeded with -- a retry budget of 3, the gateway's noisy
    # display defaults, and no tool_search switch, before 2026-09-03; no message
    # timestamps before 2026-09-16 -- until configure() reconciles it on boot.
    monkeypatch.setenv("PLOW_API_BASE", "https://api.test.invalid")
    # This is the Plow boot. Said out loud because `configure()` leaves whatever
    # provider its caller asked for in os.environ, so a test that switched away
    # earlier in the file would otherwise decide what this one reconciles.
    monkeypatch.delenv("HERMES_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    config = tmp_path / "config.yaml"
    stale = {**{k: v for k, v in SEED.items() if k not in ("tools", "cron", "gateway", "compression", "auxiliary")},
             "agent": {"api_max_retries": 3},
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
    assert after["cron"]["model_provider"] == after["model"]["provider"]
    assert after["terminal"]["cwd"] == "/var/lib/hermes"
    assert after["gateway"]["message_timestamps"] == {"enabled": True}
    # The ceiling is worth nothing seeded: every agent already has a config.
    assert after["compression"]["threshold_tokens"] == 128000
    # A text-only main model needs somewhere to send a photo.
    assert after["auxiliary"]["vision"]["model"] == "anthropic/claude-sonnet-5"
    assert "timezone" not in after
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
    # The vision route names Plow too, and an owner's photos are the traffic.
    assert "vision" not in after.get("auxiliary", {})


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


def session_reset(tmp_path, existing=None):
    path = tmp_path / "gateway.json"
    if existing is not None:
        path.write_text(json.dumps(existing))
    plow_init.GATEWAY_JSON = str(path)
    plow_init.own_session_reset()
    return json.loads(path.read_text())


def test_a_dm_stops_carrying_last_months_instructions(tmp_path):
    """A session that never ends reads its own history as current fact -- a
    42-day-old DM re-ran an August credential recipe and reported the 401 to
    its owner as a revoked token. DMs alone: the whole dict is asserted, so a
    policy leaking onto groups (whose pending drafts a reset would strand)
    fails here rather than in somebody's approval thread.
    """
    assert session_reset(tmp_path) == {"reset_by_type": {"dm": {"mode": "idle", "idle_minutes": 1440}}}


def test_what_an_operator_put_in_that_file_survives_the_boot(tmp_path):
    """Only the one chat type is this image's. A policy an operator wrote for
    groups is a decision, not drift -- merged beside, never through."""
    after = session_reset(tmp_path, {"max_concurrent_sessions": 4,
                                     "reset_by_type": {"group": {"mode": "daily", "at_hour": 4}}})
    assert after["max_concurrent_sessions"] == 4
    assert after["reset_by_type"]["group"] == {"mode": "daily", "at_hour": 4}
    assert after["reset_by_type"]["dm"] == plow_init.SESSION_RESET_POLICY


def test_an_unchanged_policy_is_not_rewritten(tmp_path):
    """Published by rename, like config.yaml: an interrupted dump must not
    leave a half-written file the gateway then reads as its base layer."""
    session_reset(tmp_path)
    path = tmp_path / "gateway.json"
    before = path.stat().st_mtime_ns
    plow_init.own_session_reset()
    assert path.stat().st_mtime_ns == before
    assert path.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob(".plow-gateway.*"))


def test_a_gateway_json_this_image_cannot_read_parks(tmp_path, parking):
    """Rewriting a file whose shape we cannot parse would drop whatever it
    holds. Parking leaves it for a human with a shell."""
    (tmp_path / "gateway.json").write_text("{ not json")
    plow_init.GATEWAY_JSON = str(tmp_path / "gateway.json")
    with pytest.raises(Parked):
        plow_init.own_session_reset()
    assert "not a file this image can rewrite" in parking.read_text()


def test_upstream_main_hermes_waits_for_plow_init():
    dependency = SOURCE.parents[1] / "s6-rc.d/main-hermes/dependencies.d/plow-init"
    assert dependency.is_file()


class Slept(Exception):
    """Stands in for the guard's sleep, so a single pass can be observed."""


def test_the_home_guard_puts_a_chmodded_home_back_and_says_what_it_found(image_user, monkeypatch, capsys):
    """A root `docker exec` running Hermes code chmods the home 0700 (2026-09-15).
    The guard restores it on its next pass and says what it found; a healthy home passes without a word."""
    home = pathlib.Path(plow_init.HOME_DIR)
    (home / "skills").mkdir()
    for path in (home, home / "skills"):
        path.chmod(0o700)
    real_fstat = os.fstat
    monkeypatch.setattr(plow_init.os, "fstat", lambda fd: types.SimpleNamespace(
        st_mode=real_fstat(fd).st_mode, st_uid=0, st_gid=1000))
    monkeypatch.setattr(plow_init.os, "fchown", lambda fd, uid, gid: None)
    monkeypatch.setattr(plow_init.time, "sleep", lambda seconds: (_ for _ in ()).throw(Slept()))

    with pytest.raises(Slept):
        plow_init.guard_home()
    for path in (home, home / "skills"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o3770
    assert f"{home} was 0:1000 700" in capsys.readouterr().err

    with pytest.raises(Slept):
        plow_init.guard_home()
    assert capsys.readouterr().err == ""


def test_the_home_guard_is_a_longrun_that_waits_for_plow_init():
    service = SOURCE.parents[1] / "s6-rc.d/home-guard"
    assert (service / "type").read_text().strip() == "longrun"
    assert (service / "dependencies.d/plow-init").is_file()
    assert (SOURCE.parents[1] / "s6-rc.d/user/contents.d/home-guard").is_file()
    assert os.access(service / "run", os.X_OK)


@pytest.fixture
def boot(monkeypatch, tmp_path, image_user):
    """Run main with real home selection and checkpoint IO, without root operations."""
    for name in ("verify_boot_preconditions", "harden_home", "write_latch_instructions",
                 "own_home_dotenv", "seed_user_profile", "configure", "own_session_reset"):
        monkeypatch.setattr(plow_init, name, lambda *args: None)
    for name in ("setgroups", "setgid"):
        monkeypatch.setattr(plow_init.os, name, lambda value: None)
    dropped = []
    monkeypatch.setattr(plow_init.os, "setuid", dropped.append)
    monkeypatch.setattr(plow_init, "read_credentials", lambda: types.SimpleNamespace(
        plow_api_base="https://plow.invalid", bearer="test", agent_id=None))
    monkeypatch.setattr(plow_init, "SEED_CONFIG", str(SOURCE.parents[2] / "seed/config.yaml"))
    exported = {}
    monkeypatch.setattr(plow_init, "export", exported.update)
    # Keep main's environment publication local to this test.
    monkeypatch.setattr(plow_init.os, "environ", dict(os.environ))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    return pathlib.Path(plow_init.HOME_DIR) / "plow_chat_last_uid", dropped, exported


@pytest.mark.parametrize("existing", [None, "", "msg_already_handled\n"])
def test_boot_waits_reasks_then_seeds_only_an_absent_checkpoint(
    boot, monkeypatch, capsys, parking, existing
):
    checkpoint, dropped, exported = boot
    if existing is not None:
        checkpoint.write_text(existing)
    elapsed = 0
    waiting_logs = []

    def answer(credentials, *, waiting=False):
        assert not dropped
        return identity(chat("cht_home")) if elapsed >= 7200 else identity()

    def sleep(seconds):
        nonlocal elapsed
        assert seconds > 0
        assert not exported
        assert not dropped
        assert checkpoint.exists() == (existing is not None)
        if capsys.readouterr().err:
            waiting_logs.append(elapsed)
        elapsed += seconds

    real_open = open

    def checked_open(path, *args, **kwargs):
        if pathlib.Path(path) == checkpoint:
            assert dropped, "checkpoint must be created after dropping privileges"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(plow_init, "open", checked_open, raising=False)
    monkeypatch.setattr(plow_init, "ask_plow", answer)
    monkeypatch.setattr(plow_init.time, "sleep", sleep)
    # The wait is a socket with a sleep behind it; this test is about the loop
    # that surrounds both, so it stands in for the pair with the sleep alone.
    # `test_chat_event_wait_falls_back_to_sleeping` covers the socket itself.
    monkeypatch.setattr(plow_init, "wait_for_chat_event",
                        lambda credentials, fallback_sleep, settled=None:
                        plow_init.time.sleep(fallback_sleep))
    monkeypatch.setattr(plow_init.time, "monotonic", lambda: elapsed)
    plow_init.main()
    assert not parking.exists()
    assert exported["PLOW_HOME_CHANNEL"] == "cht_home"
    assert checkpoint.read_text() == (existing or "")
    assert waiting_logs[0] == 0
    assert 1 < len(waiting_logs) <= 3
    assert all(b - a >= 3600 for a, b in zip(waiting_logs, waiting_logs[1:]))


def test_a_failed_ask_re_asks_soon_rather_than_holding_a_socket(boot, monkeypatch):
    """An outage over the owner's first text must not cost ten minutes.

    The socket ends this loop by carrying a chat being born. If the ask fails
    while that frame is in flight, the frame is spent and the next window has
    nothing left to announce -- so a boot that opened one would sit quiet over
    a home chat that already existed. The ask is what retries here.
    """
    _checkpoint, _dropped, exported = boot
    answers = iter([identity(), None, identity(chat("cht_home"))])
    monkeypatch.setattr(plow_init, "ask_plow", lambda credentials, **kwargs: next(answers))
    slept = []
    monkeypatch.setattr(plow_init.time, "sleep", slept.append)
    waits = []
    monkeypatch.setattr(plow_init, "wait_for_chat_event", _records(waits, held=True))

    plow_init.main()

    assert exported["PLOW_HOME_CHANNEL"] == "cht_home"
    assert waits == [plow_init.HOME_POLL_INTERVAL_S], "only the answered pass holds a socket"
    assert slept == [plow_init.HOME_POLL_INTERVAL_S], "the unanswered pass re-asks on the short wait"


@pytest.mark.parametrize(("held", "expected"), [
    (False, [3, 6, 12, 24, 48] + [plow_init.HOME_POLL_MAX_INTERVAL_S] * 3),
    (True, [plow_init.HOME_POLL_INTERVAL_S] * 8),
], ids=["no-socket-backs-off-to-the-cap", "held-socket-never-leaves-the-floor"])
def test_the_backoff_counts_consecutive_failures_not_loop_passes(boot, monkeypatch, held, expected):
    """What grows is a run of failures, not a count of passes.

    A transport that is not coming back must stop costing three seconds a
    time, which is the first row. But every HELD window is a working
    transport, so the fallback it would use if that transport died next is the
    one a fresh boot would use -- an agent that waited an hour must not reach
    the cap before its first actual failure. That is the second row, and
    growing on success is how it was wrong.
    """
    _checkpoint, _dropped, exported = boot
    answers = iter([identity()] * 8 + [identity(chat("cht_home"))])
    monkeypatch.setattr(plow_init, "ask_plow", lambda credentials, **kwargs: next(answers))
    fallbacks = []
    monkeypatch.setattr(plow_init, "wait_for_chat_event", _records(fallbacks, held=held))

    plow_init.main()

    assert exported["PLOW_HOME_CHANNEL"] == "cht_home"
    assert fallbacks == expected


def _records(fallbacks, *, held):
    """Stand in for the wait, recording the fallback it was handed.

    `held` is the socket's verdict -- what the real wait returns when the
    window carried it (True) or when it fell through to the sleep (False) --
    and is the only thing the caller's backoff reads."""
    def wait(_credentials, fallback_sleep, _settled=None):
        fallbacks.append(fallback_sleep)
        return held
    return wait


@pytest.mark.parametrize("existing", [None, "", "msg_already_handled\n"])
def test_immediate_home_leaves_checkpoint_untouched(boot, monkeypatch, existing):
    checkpoint, dropped, exported = boot
    if existing is not None:
        checkpoint.write_text(existing)
    monkeypatch.setattr(plow_init, "ask_plow", lambda credentials, **kwargs: identity(chat("cht_home")))
    plow_init.main()
    assert exported["PLOW_HOME_CHANNEL"] == "cht_home"
    if existing is None:
        assert not checkpoint.exists()
    else:
        assert checkpoint.read_text() == existing


def test_boot_parks_on_ambiguous_home_before_publishing_or_seeding(boot, monkeypatch):
    checkpoint, dropped, exported = boot
    monkeypatch.setattr(plow_init, "ask_plow", lambda credentials, **kwargs: identity(chat("a"), chat("b")))
    with pytest.raises(Parked):
        plow_init.main()
    assert not checkpoint.exists()
    assert not exported
    assert not dropped


@pytest.mark.parametrize("failure", [429, 503, "unreachable", "timeout"])
def test_transient_outage_mid_wait_recovers_without_parking(boot, monkeypatch, parking, failure):
    checkpoint, dropped, exported = boot
    failures = 0
    requests = 0

    def answer(request, timeout):
        nonlocal failures, requests
        requests += 1
        if requests == 1:
            return io.BytesIO(identity().model_dump_json().encode())
        if failures <= plow_init.RETRIES:
            failures += 1
            if failure == "unreachable":
                raise urllib.error.URLError("offline")
            if failure == "timeout":
                raise TimeoutError("timed out")
            raise urllib.error.HTTPError(request.full_url, failure, "unavailable", {}, None)
        return io.BytesIO(identity(chat("cht_home")).model_dump_json().encode())

    monkeypatch.setattr(plow_init.urllib.request, "urlopen", answer)
    monkeypatch.setattr(plow_init.time, "sleep", lambda seconds: None)
    plow_init.main()
    assert failures > plow_init.RETRIES
    assert not parking.exists()
    assert exported["PLOW_HOME_CHANNEL"] == "cht_home"
    assert checkpoint.read_bytes() == b""


@pytest.mark.parametrize("home_override", ["", "custom-home"])
def test_checkpoint_uses_the_plugins_hermes_home(boot, monkeypatch, tmp_path, home_override):
    checkpoint, dropped, exported = boot
    if home_override:
        home = tmp_path / home_override
        home.mkdir()
        checkpoint = home / "plow_chat_last_uid"
        monkeypatch.setenv("HERMES_HOME", str(home))
    else:
        monkeypatch.setenv("HERMES_HOME", "")
    answers = iter([identity(), identity(chat("cht_home"))])
    monkeypatch.setattr(plow_init, "ask_plow", lambda credentials, **kwargs: next(answers))
    monkeypatch.setattr(plow_init.time, "sleep", lambda seconds: None)
    plow_init.main()
    assert checkpoint.read_bytes() == b""
    if home_override:
        assert not (pathlib.Path(plow_init.HOME_DIR) / "plow_chat_last_uid").exists()


class _Creds:
    """The two fields the wait reads. `Credentials` is a settings model that
    validates against the process environment, and this test is about the
    fallback, not about how a credential is loaded."""

    plow_api_base = "https://api.example.test"
    bearer = "tok"  # pragma: allowlist secret — synthetic test credential


def _credentials():
    return _Creds()


def test_chat_event_wait_falls_back_to_sleeping(monkeypatch, capsys):
    """A socket that will not open costs the interval, never the boot.

    Every way this can fail -- no ticket, a refused upgrade, no aiohttp in the
    venv -- lands here, and the contract is the one the poll had before it:
    return after `timeout`, having said why once.
    """
    slept = []
    # The throttle is module state, so say which side of it this test is on
    # rather than inheriting whatever ran before it.
    monkeypatch.setattr(plow_init, "_next_socket_log", 0.0, raising=False)
    monkeypatch.setattr(plow_init.time, "sleep", slept.append)
    _socket(monkeypatch, raises=RuntimeError("no socket"))

    plow_init.wait_for_chat_event(_credentials(), 3)

    assert slept == [3]
    assert "falling back to the poll" in capsys.readouterr().err


def _socket(monkeypatch, *, says=None, raises=None):
    """Stand in for the socket itself, not for the asyncio that drives it.

    Replacing `_await_chat_frame` leaves `asyncio.run` and `wait_for` real, so
    the control flow under test is the real one and no coroutine is created
    only to be closed unawaited."""

    async def frame(_base, _bearer, _settled=None):
        if raises is not None:
            raise raises
        return says

    monkeypatch.setattr(plow_init, "_await_chat_frame", frame)
    monkeypatch.setattr(plow_init.time, "monotonic", lambda: 0)


@pytest.mark.parametrize(("frame_received", "expected_sleep"), [
    (True, []),
    (False, [3]),
], ids=["frame-ends-wait", "closed-socket-sleeps-remaining-interval"])
def test_chat_event_wait_result_controls_sleep(monkeypatch, frame_received, expected_sleep):
    """A frame ends the wait; a clean close still owes the polling interval."""
    slept = []
    monkeypatch.setattr(plow_init.time, "sleep", slept.append)
    _socket(monkeypatch, says=frame_received)

    plow_init.wait_for_chat_event(_credentials(), 3)

    assert slept == expected_sleep


def test_a_quiet_socket_is_held_rather_than_timed_out(monkeypatch):
    """There is no window: a socket that says nothing is held, not abandoned.

    Holding one socket is the whole saving -- it is what makes a single ticket
    cover a quiet agent instead of one per interval. A wait that gave up on a
    silent socket and re-asked is what minted a row and a connection every
    three seconds for the life of a VM whose owner never texts.
    """
    slept = []
    monkeypatch.setattr(plow_init.time, "sleep", slept.append)
    running = []

    async def quiet(_base, _bearer, _settled=None):
        running.append(True)
        await asyncio.sleep(3600)

    monkeypatch.setattr(plow_init, "_await_chat_frame", quiet)

    def hold():
        plow_init.wait_for_chat_event(_credentials(), 3)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    thread.join(timeout=1.5)

    assert thread.is_alive(), "a quiet socket ended the wait -- something still expires"
    assert running and slept == [], "held on the socket, not sleeping the fallback"


def test_a_setup_timeout_is_a_transport_failure_not_a_held_socket(monkeypatch):
    """aiohttp's timeouts subclass `asyncio.TimeoutError` -- the caller's word
    for "the window expired with a socket held open".

    Nothing is held when setup times out, so letting that through would skip
    the fallback and reset the backoff, and a timing-out endpoint would be
    re-dialled every ten seconds with a committed ticket each time -- the
    churn this whole change removes, arriving during an incident.
    """
    def session(**_kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setitem(sys.modules, "aiohttp", types.SimpleNamespace(
        ClientSession=session, ClientTimeout=lambda **_kwargs: None))

    with pytest.raises(ConnectionError):
        asyncio.run(plow_init._await_chat_frame("https://api.example.test", "tok"))


def test_the_socket_failure_is_said_once_not_every_interval(monkeypatch, capsys):
    """An agent whose owner has no Mac waits forever; it must not narrate it.

    First failure speaks, the rest are silent until the wait log's own hourly
    cadence comes round.
    """
    monkeypatch.setattr(plow_init, "_next_socket_log", 0.0, raising=False)
    monkeypatch.setattr(plow_init.time, "sleep", lambda seconds: None)
    _socket(monkeypatch, raises=RuntimeError("no socket"))
    clock = [0.0]
    monkeypatch.setattr(plow_init.time, "monotonic", lambda: clock[0])

    spoke = []
    for _ in range(40):
        plow_init.wait_for_chat_event(_credentials(), 3)
        if capsys.readouterr().err:
            spoke.append(clock[0])
        clock[0] += 3

    assert spoke[0] == 0
    assert all(b - a >= plow_init.HOME_WAIT_LOG_INTERVAL_S for a, b in zip(spoke, spoke[1:]))
    assert len(spoke) == 1, "two minutes of failures is one line, not forty"


def _frames(*payloads):
    """A socket that yields `payloads` as TEXT frames, then closes."""
    class Frame:
        type = "TEXT"
        def __init__(self, payload): self._payload = payload
        def json(self): return self._payload
    class Socket:
        def __init__(self): self.frames = [Frame(p) for p in payloads]
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        def __aiter__(self): return self._iter()
        async def _iter(self):
            for frame in self.frames:
                yield frame
    return Socket()


def _aiohttp(monkeypatch, socket, *, ws_hangs=False):
    """Stand in for aiohttp. `socket` is the socket, or a factory called per
    connect so a reconnect gets a fresh one."""
    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        def raise_for_status(self): return None
        async def json(self, **_kwargs): return {"ticket": "tkt"}
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        def post(self, *_a, **_k): return Response()
        async def ws_connect(self, *_a, **_k):
            if ws_hangs:
                await asyncio.sleep(3600)   # accepted, never upgraded
            return socket() if callable(socket) else socket
    module = types.SimpleNamespace(
        ClientSession=lambda **_kwargs: Session(),
        ClientTimeout=lambda **_kwargs: None,
        WSMsgType=types.SimpleNamespace(TEXT="TEXT"))
    monkeypatch.setitem(sys.modules, "aiohttp", module)


def test_a_home_chat_born_before_the_socket_subscribed_ends_the_wait(monkeypatch):
    """The caller asks, then opens a socket. A chat created in that gap gets no
    frame -- the API pushes to sockets registered at the time and replays
    nothing -- so without asking again the wait sits quiet over a home chat
    that already exists, for the whole window."""
    asked = []
    _aiohttp(monkeypatch, _frames({"type": "connected"}))

    def settled():
        asked.append(True)
        return True

    assert asyncio.run(plow_init._await_chat_frame("https://api.example.test", "tok", settled)) is True
    assert asked == [True], "asked once, at the handshake, not per frame"


def test_the_handshake_is_re_asked_on_every_connection_and_alone_is_not_news(monkeypatch):
    """The re-ask is what closes the race, so it belongs to every subscribe --
    a reconnect after a deploy opens the same gap the first connect did. A
    handshake whose re-ask says no is not news either: the socket keeps
    waiting rather than reporting a frame that says nothing about a home chat.
    """
    asked = []
    opened = []

    def socket():
        opened.append(True)
        return _frames({"type": "connected"})     # handshake, then close

    _aiohttp(monkeypatch, socket)
    monkeypatch.setattr(plow_init, "HOME_SOCKET_RECONNECT_S", 0.01)
    monkeypatch.setattr(plow_init, "HOME_SOCKET_RECONNECT_MAX_S", 0.01)

    async def hold():
        return await plow_init._await_chat_frame(
            "https://api.example.test", "tok", lambda: asked.append(True) or False)

    async def bounded():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(hold(), 1)

    asyncio.run(bounded())

    assert len(opened) > 1, "a closed socket was not re-opened"
    assert len(asked) == len(opened), "identity is asked once per subscribe, reconnects included"


def test_a_chat_that_arrives_after_a_reconnect_still_ends_the_wait(monkeypatch):
    """The socket a deploy closed is re-opened, and the chat born meanwhile is
    found by that connection's own re-ask -- not by a timer, which is gone."""
    answers = iter([False, True])
    _aiohttp(monkeypatch, lambda: _frames({"type": "connected"}))
    monkeypatch.setattr(plow_init, "HOME_SOCKET_RECONNECT_S", 0.01)

    assert asyncio.run(plow_init._await_chat_frame(
        "https://api.example.test", "tok", lambda: next(answers))) is True


def test_an_upgrade_that_never_answers_is_bounded_by_setup_not_the_hold(monkeypatch):
    """`connect` bounds acquiring the connection, not the upgrade response. A
    server that accepts TCP and then says nothing would otherwise spend the
    whole hold here -- and the window ending would report as a held socket and
    reset the backoff."""
    _aiohttp(monkeypatch, None, ws_hangs=True)
    monkeypatch.setattr(plow_init, "TIMEOUT_S", 0.05)

    started = time.monotonic()
    with pytest.raises(ConnectionError):
        asyncio.run(plow_init._await_chat_frame("https://api.example.test", "tok"))
    assert time.monotonic() - started < 1, "setup timed out on its own bound"


def test_the_boot_hands_the_wait_only_its_fallback(monkeypatch, boot):
    """No window reaches the wait any more: the hold ends on news, and the only
    number the loop still owns is the sleep-only fallback."""
    spent = []
    answers = iter([identity(), identity(chat("cht_home"))])
    monkeypatch.setattr(plow_init, "ask_plow", lambda credentials, **kwargs: next(answers))
    monkeypatch.setattr(plow_init.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(plow_init, "wait_for_chat_event",
                        lambda _credentials, fallback, _settled=None:
                        (spent.append(fallback), True)[1])

    plow_init.main()

    assert spent == [plow_init.HOME_POLL_INTERVAL_S]
    assert not hasattr(plow_init, "HOME_SOCKET_WAIT_S"), "the window is gone, not merely unused"

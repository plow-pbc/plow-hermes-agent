"""Configure this agent from its credential, then let the gateway start.

Runs once, as root, before any service. Everything downstream declares this as
a dependency, and s6-rc starts none of it until this oneshot completes -- which
is the point: an agent whose setup half ran serves its local API, answers every
probe, and cannot be reached by the person it belongs to. So a refusal here
parks rather than exits, and the completion never comes.

A host tells this image where Plow is, what to present to it, and optionally
which Agent Index id it reports as, at /var/lib/plow/credentials. The rest of the agent's identity is asked of Plow
with that credential. Nothing falls back -- no credential, a credential Plow
will not answer for, or no answer at all, and nothing starts.
"""

from __future__ import annotations

import json
import os
import pwd
import secrets
import signal
import stat
import subprocess
import sys
import traceback
import tempfile
import time
import typing
import urllib.error
import urllib.request

import yaml
from dotenv.parser import parse_stream
from pydantic import BaseModel, Field, ValidationError
from typing import Annotated, Literal, Union
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

CREDENTIALS = "/var/lib/plow/credentials"
CONFIG = "/var/lib/hermes/config.yaml"
CONTAINER_ENV = "/run/s6/container_environment"
HOST_SETUP = "/exe.dev/setup"
PARK_MARKER = "/run/plow-init.parked"

CREDENTIALS_WAIT_S = 60
RETRIES = 10
RETRY_DELAY_S = 3
TIMEOUT_S = 10
# The one entry in `mcp_servers` this image manages. Any other belongs to
# whoever added it and is left exactly as it is.
RELAY_SERVER = "plow"
HOME_DIR = "/var/lib/hermes"
HOME_DOTENV = "/var/lib/hermes/.env"
SEED_CONFIG = "/opt/hermes/plow-seed/config.yaml"
# The identity, composed on every boot: the base persona this image ships,
# then the variant's own, if it ships one. Composed rather than COPYed into the
# home because a populated volume shadows the image layer forever, and because
# a variant that replaced the file whole silently dropped every base rule
# (plow-hermes-agent#66, life-assistant-hermes-agent#168).
SEED_SOUL = "/opt/hermes/plow-seed/SOUL.md"
SEED_PERSONA = "/opt/hermes/plow-seed/persona.md"
HOST_CREDENTIALS = f"{CREDENTIALS}.host"
# Latch's `initialize.instructions`, which Hermes drops on connect, written
# where it reads them instead: the context tier, via `terminal.cwd` (#72).
HERMES_MD = os.path.join(HOME_DIR, "HERMES.md")
INSTRUCTIONS_TIMEOUT_S = 5


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse any 3xx on the authenticated initialize request. The relay is
    transparent to the owner's Mac, so a compromised Mac answering with a
    cross-host redirect would otherwise have urllib re-send the agent's
    line-scoped bearer token to the redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A fixed message: newurl is Mac-controlled and can carry terminal-control
        # bytes, and this exception ends up in the boot log verbatim.
        raise urllib.error.HTTPError(req.full_url, code, "refusing redirect", headers, fp)


# Latch's server never redirects, so refusing costs nothing and closes the
# token-forwarding path. The plow_chat plugin's own relay client refuses the
# same way; the two clients are kept in step by hand, not shared -- see the PR.
_no_redirect_opener = urllib.request.build_opener(_RefuseRedirects)

# The agent's first USER.md (#73). Hermes injects memories/USER.md into every
# prompt and the model reads it as its own knowledge, so this is where a fresh
# agent learns that "nothing in my stores" is not "nothing in the owner's
# world". WHERE the owner's world lives and how to reach it is Latch's own
# routing, carried once by HERMES.md (#74); this file carries only the owner's
# own facts, so the two do not restate each other. Entries in the memory
# store's own on-disk shape (hermes-agent tools/memory_tool_store.py:
# ENTRY_DELIMITER "\n§\n", user_char_limit 1375 over the whole file), well
# under budget. General to every Plow agent.
USER_PROFILE = (
    # Only facts true regardless of the home's age: an existing agent upgraded
    # to this image has real prior sessions, so a "sessions begin today /
    # nothing earlier" claim (keyed on USER.md absence) would be false for it.
    # The line below already tells the model its empty store is not the whole
    # picture -- check the Mac -- which is the actual goal.
    "This server holds only this agent's own work. The owner may have other Plow lines and earlier "
    "agents, whose work is in Messages and mail on their Mac, not in this agent's sessions or memory.",
)


def park(reason: str) -> typing.NoReturn:
    """Refuse, loudly, without letting PID 1 exit.

    Exiting is fail-closed on a host that stops a container and leaves it
    stopped. exe.dev is not one: this image's CMD *is* PID 1 in a microVM, so a
    non-zero exit is `Attempted to kill init` -- a panicked kernel spinning a
    full vCPU with no sshd, which is how the warm pool billed two cores for a
    day. Powering off instead only trades that for a reboot loop; exe.dev has
    no stopped state and boots a halted guest straight back up (measured: down
    ~45s, then up with uptime=1). A spinning vCPU is never an acceptable
    outcome, so nothing in this script exits on a failure path.

    Fail-closed is not weakened by that, because it never rested on the exit
    code: the gateway and main-hermes declare this oneshot a dependency, and
    s6-rc starts neither until it COMPLETES. Parking means it never does. The
    agent stays unreachable; what changes is that the box stays alive and
    shell-able, which is what makes a failure diagnosable at all.

    The reason goes to stderr for the container log and to a marker file for
    whoever opens that shell.
    """
    print(f"plow-init: {reason} -- parking; no gateway will start", file=sys.stderr, flush=True)
    # stderr first, and unconditionally: after configure() this process has
    # dropped to the agent's uid and can no longer write into /run, so the log
    # is the only channel that survives the whole script. The marker is a
    # convenience for the boot's root half, which is where every refusal that
    # names a cause lives.
    try:
        with open(PARK_MARKER, "w") as marker:
            marker.write(f"{reason}\n")
    except OSError as exc:
        # Load-bearing, not defensive. park() is now the only way this script
        # refuses anything, so an exception escaping it exits plow-init, exits
        # /init, and panics the VM -- the precise outcome the function exists
        # to prevent, reached from every failure path at once. The marker is a
        # convenience for a human with a shell; parking is the behaviour, and
        # losing the first must never cost the second.
        print(f"plow-init: could not write {PARK_MARKER}: {exc}", file=sys.stderr, flush=True)
    while True:
        signal.pause()


class Credentials(BaseSettings):
    """The two required lines and optional Agent Index id the host writes.

    `extra="forbid"` refuses a provisioner that has drifted ahead of this
    image, rather than half-obeying it.

    The file is the ONLY source. A settings model reads the process
    environment first by default, which would let `docker run -e
    PLOW_AGENT_TOKEN=...` outrank the credential the image was actually given
    -- and since the token decides what is sent to Plow, that is a rotation
    silently not taking, or an agent presenting somebody else's credential.
    So every other source is dropped below.
    """

    model_config = SettingsConfigDict(extra="forbid")

    plow_api_base: str
    plow_agent_token: str
    agent_id: str | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (dotenv_settings,)


class LineRef(BaseModel):
    uid: str


class AgentParticipant(BaseModel):
    """One of Plow's own lines in a chat. Exactly one is this agent."""

    type: Literal["agent"]
    relationship: Literal["self", "peer"]
    line: LineRef


class MemberParticipant(BaseModel):
    """A person in a chat. `owner` is the one the line belongs to."""

    type: Literal["member"]
    uid: str
    role: Literal["owner", "member"]
    display_name: str | None = None
    # Plow fills display_name with this handle (the phone number / email) when
    # the account carries no name, so the two are compared to tell a real name
    # from the fallback -- a handle must never be written into every prompt.
    provider_key: str | None = None


# `Union[...]` rather than `A | B`: this is evaluated at import, and the
# tests import this module on whatever Python the developer has.
Participant = Annotated[Union[AgentParticipant, MemberParticipant], Field(discriminator="type")]


class Chat(BaseModel):
    uid: str
    status: str
    participants: list[Participant] = []


class Identity(BaseModel):
    """Plow's answer about the agent holding that credential.

    Every key is always present here, and a nullable one arrives as null rather
    than being left out -- which is NOT how the general chat and line endpoints
    serialize, so do not assume the two are byte-identical. `None` covers both
    spellings either way. Extra keys are ignored on purpose: Plow may add to
    this answer, and an image that refused one could not be told about anything
    new without being rebuilt first.
    """

    line: LineRef
    chats: list[Chat]
    mcp_url: str | None


def home_chat(identity: Identity) -> Chat:
    """The one chat that is this agent talking to the person it belongs to.

    Plow does not name it, so the image picks it, by the same rule Plow uses:
    an active chat on this agent's own line holding exactly one member -- the
    owner -- and this agent. Anything else in a chat makes it a group, or
    somebody else's. The line check is load-bearing: a mailbox carrying this
    agent's persona is another line whose threads the credential also opens,
    and an owner alone with the mailbox reads as owner-plus-self too. Zero
    matches or several is not a thing to guess at: the home channel is where
    the agent answers, and the wrong one is an agent talking to the wrong people.
    """
    def is_home(chat: Chat) -> bool:
        members = [p for p in chat.participants if isinstance(p, MemberParticipant)]
        agents = [p for p in chat.participants if isinstance(p, AgentParticipant)]
        # Exactly one agent, not merely exactly one `self`: another Plow line
        # in the thread is a second assistant, which makes this a group rather
        # than the owner's one-to-one chat with this agent.
        return (
            chat.status == "active"
            and len(agents) == 1
            and agents[0].relationship == "self"
            and agents[0].line.uid == identity.line.uid
            and len(members) == 1
            and members[0].role == "owner"
        )

    matches = [chat for chat in identity.chats if is_home(chat)]
    if len(matches) != 1:
        seen = "; ".join(
            f"{chat.uid} status={chat.status} "
            + ",".join(
                f"{p.relationship}@{p.line.uid}" if isinstance(p, AgentParticipant) else p.role
                for p in chat.participants
            )
            for chat in identity.chats
        ) or "no chats at all"
        park(f"cannot tell which chat is home -- {len(matches)} of {len(identity.chats)} qualify: {seen}")
    return matches[0]


def verify_boot_preconditions() -> None:
    """Check what cont-init was supposed to leave behind, before trusting it.

    PID 1 cannot be wrapped to catch a failed cont-init script: s6-overlay's
    `/init` execs `s6-overlay-suexec`, which refuses to run unless it IS pid 1
    (`s6-overlay-suexec: fatal: can only run as pid 1` -- measured, it exits
    100 on every boot including healthy ones). And S6_BEHAVIOUR_IF_STAGE2_FAILS
    cannot be 2, because 2 exits /init and on a microVM that is a kernel panic
    pinning a vCPU. So it is 1: a failed cont-init script is warned about and
    the boot carries on.

    Which makes this the gate. Every service the owner can reach depends on
    this oneshot, so what the gateway needs has to be true HERE rather than
    assumed to have been established earlier. A cont-init failure in something
    the gateway does not depend on stays a warning -- correctly, since nothing
    it touched is in the path to serving anyone.

    Each check is a state a failed cont-init actually produces, not a
    hypothetical: no agent account (the inherited uid remap did not run), a
    bind-mounted credential still sitting unpromoted beside a stale one (the
    promotion aborted -- the rotation-not-taking case), and a home that is not
    a directory this image can work in.
    """
    try:
        pwd.getpwnam("hermes")
    except KeyError:
        park("no `hermes` account -- the image's user setup did not complete")

    # The promotion is the one cont-init step whose failure is silent AND
    # serves a tenant on the wrong credential: a stale file from an earlier
    # boot is a valid-looking credential, so nothing downstream would notice.
    # `lexists`, not `isfile`: presence is what says a promotion was owed, and
    # the shape that matters most is not a regular file. Docker creates a
    # DIRECTORY at the mount point when the host source is missing, and
    # 00-plow-sanitize's `-f` test skips a directory silently -- so the boot
    # most likely to leave a stale credential in place is also the one that
    # looks like nothing happened. A symlink is refused for the same reason.
    if os.path.lexists(HOST_CREDENTIALS):
        if not stat.S_ISREG(os.lstat(HOST_CREDENTIALS).st_mode):
            park(f"{HOST_CREDENTIALS} is not a regular file -- nothing was promoted, and {CREDENTIALS} cannot be trusted")
        try:
            with open(HOST_CREDENTIALS, "rb") as host, open(CREDENTIALS, "rb") as promoted:
                same = host.read() == promoted.read()
        except OSError as error:
            park(f"{HOST_CREDENTIALS} was never promoted to {CREDENTIALS}: {error}")
        if not same:
            park(f"{CREDENTIALS} is not the {HOST_CREDENTIALS} beside it -- the promotion did not run, and this credential is stale")

    if not os.path.isdir(HOME_DIR) or os.path.islink(HOME_DIR):
        park(f"{HOME_DIR} is not a directory -- the agent has no home to start in")


def read_credentials() -> Credentials:
    """Judge the file before parsing it, on facts a parser cannot see.

    It decides where the agent's own bearer token is sent, so anyone else
    owning or reading it chooses both. Two exact modes rather than a rule about
    bits: one merely forbidding the write bits would admit 0644, which hands
    the credential to every account in the container.
    """
    # Waited for, not merely required: a host may write this file after the
    # container is already running. Present at boot costs nothing -- the first
    # look succeeds.
    for _ in range(CREDENTIALS_WAIT_S):
        if os.path.lexists(CREDENTIALS):
            break
        time.sleep(1)
    try:
        info = os.lstat(CREDENTIALS)
    except OSError:
        park(f"no credential at {CREDENTIALS} after {CREDENTIALS_WAIT_S}s")
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISREG(info.st_mode):
        park(f"{CREDENTIALS} is not a regular file")
    if (info.st_uid, info.st_gid) != (0, 0) or mode not in (0o600, 0o400):
        park(f"{CREDENTIALS} is {info.st_uid}:{info.st_gid} mode {mode:04o} -- expected root:root at 600 or 400")
    try:
        # The path is passed rather than baked into the class, so this module
        # names it once.
        return Credentials(_env_file=CREDENTIALS)
    except ValidationError as error:
        # `include_input=False`: the default rendering quotes the offending
        # input back, and for a missing key that input is the whole parsed
        # file -- so a credential lacking PLOW_API_BASE would print the token
        # it does have to s6's stderr, where every log reader can see it.
        park(f"{CREDENTIALS} does not contain only the documented keys:\n{error.errors(include_input=False)}")


def ask_plow(credentials: Credentials) -> Identity:
    """Ask Plow who this agent is, retrying only what waiting could fix.

    A VM's network is not always up when its first service is: worth retrying,
    not worth surviving. An agent that cannot be told who it is must not come
    up as whoever it was last time -- a home volume outlives its tenant, and
    the failure that hides is a new tenant answering in the previous one's chat.
    """
    url = credentials.plow_api_base.rstrip("/") + "/v1/agents/cloud/me"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {credentials.plow_agent_token}", "Accept": "application/json"},
    )
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            # 401, 403 and 404 are Plow saying this credential is not this
            # agent's -- revoked, or naming an agent that is gone.
            if not (error.code == 429 or 500 <= error.code < 600):
                park(f"{url} answered {error.code} -- Plow refused this credential")
            reason = f"answered {error.code}"
        except OSError as error:
            reason = f"unreachable: {error}"
        else:
            try:
                return Identity.model_validate_json(raw)
            except ValidationError as error:
                # Same reason as the credential above: the raw answer is a
                # roster of real people.
                park(f"{url} answered something that is not an identity:\n{error.errors(include_input=False)}")
        print(f"plow-init: attempt {attempt} to reach Plow failed, retrying ({reason})", file=sys.stderr)
        time.sleep(RETRY_DELAY_S)
    park(f"gave up asking Plow who this agent is after {RETRIES} attempts -- refusing to start")


def fetch_latch_instructions(url: str, token: str) -> str:
    """One JSON-RPC `initialize` through the stateless relay, answered as
    JSON or one SSE frame. Raises on anything but instructions."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "plow-init", "version": "0"}},
    }).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with _no_redirect_opener.open(request, timeout=INSTRUCTIONS_TIMEOUT_S) as response:
        raw = response.read().decode()
    if raw.lstrip().startswith(("event:", "data:")) or "\ndata:" in raw:
        raw = "\n".join(line[5:].strip() for line in raw.splitlines() if line.startswith("data:"))
    instructions = json.loads(raw)["result"].get("instructions")
    if not instructions:
        raise ValueError("initialize answered without instructions")
    return instructions


def _loggable(text: object) -> str:
    """A control-character-free rendering of relay-controlled text for the boot
    log. An HTTP error's reason phrase (or a redirect's Location) is chosen by
    the same untrusted party the relay reaches, so newlines and terminal-control
    bytes in it must not forge or mutate log lines."""
    return "".join(c if c.isprintable() else " " for c in str(text))


def _remove_hermes_md(why: str) -> None:
    """Remove HERMES.md so a boot without a Mac leaves no stale routing behind.
    Non-fatal, like every step here."""
    try:
        os.unlink(HERMES_MD)
    except FileNotFoundError:
        return
    except OSError as error:
        print(f"plow-init: could not remove {HERMES_MD} ({_loggable(error)})", file=sys.stderr)
        return
    print(f"plow-init: removed {HERMES_MD} -- {why}", file=sys.stderr)


def write_latch_instructions(identity: Identity, token: str) -> None:
    """Write $HOME/HERMES.md from Latch's instructions, whole every boot -- a
    plow-init-managed file, the way compose_identity() writes SOUL.md, not the
    agent's to author. exe.dev unpacks a fresh rootfs per agent, so there is no
    prior tenant's file to preserve; agents keep their own notes in MEMORY.md.
    No Mac, or a fetch that fails, removes it so no stale routing survives. The
    boot goes on either way."""
    if identity.mcp_url is None:
        _remove_hermes_md("the account has no Mac")
        return
    try:
        instructions = fetch_latch_instructions(identity.mcp_url, token)
    except Exception as error:  # noqa: BLE001 -- a Mac that is off is the ordinary case
        _remove_hermes_md(f"the fetch failed ({_loggable(error)})")
        return
    staged = None
    try:
        descriptor, staged = tempfile.mkstemp(prefix=".HERMES.md.", dir=HOME_DIR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchown(handle.fileno(), 0, 0)
            os.fchmod(handle.fileno(), 0o644)
            handle.write(f"# Your owner's Mac, in Latch's own words\n\n{instructions}\n")
        os.replace(staged, HERMES_MD)
    except (OSError, UnicodeError) as error:  # a surrogate in the fetched text encodes no better than a full disk
        if staged is not None:
            try:
                os.unlink(staged)
            except OSError:
                pass
        print(f"plow-init: {HERMES_MD} not written ({_loggable(error)})", file=sys.stderr)
        # Fail to absent, not stale: a prior HERMES.md left active would boot
        # Hermes with stale Mac-routing. Absent is safe -- the plugin's manifest
        # section is the primary routing lever -- and not worth panicking a VM.
        _remove_hermes_md("the earlier write failed")
        return
    print(f"plow-init: wrote {HERMES_MD} from Latch's instructions", file=sys.stderr)


def export(values: dict[str, str]) -> None:
    """Publish the tenant's values the way s6 reads them: one file per name.

    Every service starts with these in its environment, so nothing parses a
    dotenv as root and nothing outlives the boot.
    """
    os.makedirs(CONTAINER_ENV, mode=0o755, exist_ok=True)
    for name, value in values.items():
        descriptor = os.open(os.path.join(CONTAINER_ENV, name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(value)


def configure(identity: Identity, seed: dict) -> None:
    """Point the agent at its inference provider and its relay.

    A structured edit: it writes the settings it owns and touches nothing else,
    and is skipped when they already hold these values, so a second boot leaves
    the file as it found it. A model id belongs to the provider it was written
    for, so the two move together: HERMES_MODEL when one is named, the seed's
    otherwise, and only under Plow -- another provider's model is nothing this
    image knows how to guess. The provider entry, the display section, the
    retry budget, the tool_search switch and the working directory are the seed's on every boot:
    cont-init seeds only an absent config.yaml, so a home that predates a seed
    change would otherwise keep the old shape for good.
    """
    with open(CONFIG) as handle:
        config = yaml.safe_load(handle) or {}

    seed_model = seed.get("model", {})
    provider = os.environ.get("HERMES_PROVIDER", "plow")
    wanted: dict[tuple[str, ...], object] = {
        # The relay entry is the image's too, so it is written whole (#45):
        # a home seeded before it carried `url`/`headers` dispatched to stdio.
        ("mcp_servers", RELAY_SERVER): {**seed["mcp_servers"][RELAY_SERVER], "enabled": identity.mcp_url is not None},
        ("model", "provider"): provider,
        ("agent", "api_max_retries"): seed["agent"]["api_max_retries"],
        ("cron", "model_drift_guard"): seed["cron"]["model_drift_guard"],
        ("display",): seed["display"],
        ("tools", "tool_search", "enabled"): seed["tools"]["tool_search"]["enabled"],
        ("terminal", "cwd"): seed["terminal"]["cwd"],
    }
    # Prompt caching, declared here and nowhere else.
    #
    # Plow's `/v1/chat/completions` is a LiteLLM proxy in front of Anthropic and
    # honours `cache_control`, but Hermes grants caching on the OpenAI wire only
    # to a route whose provider id or hostname reads as LiteLLM -- and a
    # config-defined provider is `custom` at runtime whatever the config calls
    # it, so neither signal can ever match. The per-model declaration is the
    # other door: Hermes matches it on the ENDPOINT and the MODEL ID, not on a
    # name. Both halves of that match are written here.
    #
    # The endpoint must be the expanded one. The match is against a normalized
    # URL and the seed's `${PLOW_API_BASE}` reference is never equal to the URL
    # the agent dials, so a seed-side declaration is unreachable while reading
    # as set -- which is why the seed does not carry one and this is the single
    # owner.
    #
    # The model id must be the one actually selected. `HERMES_MODEL` replaces
    # `model.default` a few lines below, and a flag filed under the seed's model
    # is a flag Hermes never looks up: caching silently off for anyone who sets
    # that variable.
    #
    # Re-asserted every boot rather than left to a first-boot seed: cont-init
    # seeds only an ABSENT config.yaml, so a home from before this change keeps
    # a registry with neither key and would cache nothing, for good.
    provider_key = seed_model.get("provider")
    if provider_key:
        plow_model = os.environ.get("HERMES_MODEL") if provider == provider_key else None
        # The whole entry is the image's, so it is written whole: a key the
        # seed gained reaches a home seeded before it (2026-09-04: `key_env`
        # missing, every call sent `Bearer no-key-required`), and a key the
        # seed dropped leaves. Two keys are the boot's to add on top.
        entry = {**seed["providers"][provider_key], "base_url": os.path.expandvars(seed_model.get("base_url", ""))}
        entry["models"] = {**entry.get("models", {}), plow_model or seed_model.get("default"): {"prompt_caching": True}}
        wanted[("providers", provider_key)] = entry
    if os.environ.get("HERMES_MODEL"):
        wanted[("model", "default")] = os.environ["HERMES_MODEL"]
    elif provider == "plow":
        # No model asked for, and Plow is what you get when nobody says
        # otherwise -- so this is also the boot after a home was switched to
        # another provider and switched back. Its model id came from that
        # provider and does not exist here; restoring the seed's along with
        # the endpoint below is what makes the switch two variables both ways.
        wanted[("model", "default")] = seed_model.get("default")

    # `base_url` and `key_env` describe Plow's endpoint and its credential.
    # Left in place under another provider they point every call back at Plow,
    # so they are removed when switching away and restored from the seed when
    # switching back -- which is what keeps a switch two variables rather than
    # an edit.
    for key in ("base_url", "key_env"):
        wanted[("model", key)] = seed_model.get(key) if provider == "plow" else None

    changed = False
    for path, value in wanted.items():
        section = config
        for key in path[:-1]:
            section = section.setdefault(key, {})
        # `None` means the setting should not be there at all, which is not the
        # same as being present and null.
        if value is None:
            changed = section.pop(path[-1], None) is not None or changed
        elif section.get(path[-1]) != value:
            section[path[-1]] = value
            changed = True
    if changed:
        # A sibling, then a rename: `open(CONFIG, "w")` truncates first, so a
        # boot interrupted mid-dump leaves a half-written config.yaml -- which
        # the next boot keeps, because cont-init only seeds an absent one.
        temporary = CONFIG + ".tmp"
        with open(temporary, "w") as handle:
            os.fchmod(handle.fileno(), 0o640)
            yaml.safe_dump(config, handle, sort_keys=False)
        os.replace(temporary, CONFIG)


# Every name this boot publishes to the process environment (see `values` in
# `main`), never to a file the agent can read on purpose. This boot just
# authenticated a fresh value for each one, so a copy persisted under
# HOME_DOTENV is never anything but a stale shadow -- and the runtime loads
# that file OVER the environment, so the shadow would win the precedence
# fight. That is not mere staleness: a rotated PLOW_AGENT_TOKEN or a fleet
# home reused for a different tenant must not come back up answering on the
# credential or endpoint this boot just replaced.
#
# The two PLOW_CHAT_* names are the legacy spellings of the same credential and
# endpoint, and carry the same hazard under a different key: a rotation leaves
# the old value here answering 401, in a file the agent reads, under a name
# nothing in the runtime consumes. An agent found one and used it. The other
# PLOW_CHAT_* names -- CHAT_UID, GROUP_UIDS, APPROVAL_GROUP -- are directory
# data with live consumers, so a stale one is wrong rather than revoked; they
# stay.
DOTENV_OWNED_NAMES = frozenset({
    "PLOW_API_BASE",
    "PLOW_AGENT_TOKEN",
    "PLOW_CHAT_BASE_URL",
    "PLOW_CHAT_TOKEN",
    "PLOW_HOME_CHANNEL",
    "HERMES_CUSTOM_PLOW_API_KEY",
    "API_SERVER_KEY",
    "AGENT_ID",
    "PLOW_MCP_URL",
})


def own_home_dotenv(api_server_key: str) -> None:
    """Merge this boot's identity into the home's dotenv, without
    reintroducing what it publishes.

    The runtime writes its own API_SERVER_KEY there during cont-init, and it
    loads that file OVER its process environment -- so a key this image
    published would lose to the one persisted in the home, and the per-boot
    key would be decorative. Setting it here settles that: both sources agree.

    The same precedence rule cuts the other way for every other name in
    DOTENV_OWNED_NAMES. A copy of any of them sitting in this file is never
    anything but a stale shadow of what this boot just authenticated, and
    loading it over the environment would let that shadow win -- an old
    credential outliving its rotation, or a reused fleet home answering as
    the tenant before it. So every assignment of an owned name is dropped
    from the file rather than carried across, and API_SERVER_KEY -- the one
    name this function actually sets -- is appended fresh.

    Which assignment is which is asked of the loader's own parser, not of a
    grammar of this image's own -- `export`, leading whitespace and quoted
    keys come free, and so does the case a line-at-a-time filter gets wrong
    in the dangerous direction: a quoted operator value spanning several
    lines whose continuation opens with `PLOW_AGENT_TOKEN=` is one binding of
    the operator's name, and split into lines it reads as an owned one this
    function would then delete.

    A cloud tenant's home holds nothing else in this file, so dropping the
    owned names and appending API_SERVER_KEY is indistinguishable from the
    truncating rewrite this function used to do. The Docker fleet managed by
    agent-mgr is the other consumer of this image, and there the same file IS
    the agent's configuration store -- its Plow Chat, Domo, dashboard and
    timezone keys, whatever its own tooling put there. None of those names
    are ones this boot owns, so they are carried across untouched --
    unparsed and unreformatted, because a rewrite that "tidies" an operator's
    file on the way past is a second version of the same bug.

    Position is not preserved for API_SERVER_KEY, and does not need to be:
    every existing assignment of it is dropped before one is appended, so the
    file never holds more than one regardless of where a reader would look.

    Written rather than left missing when no dotenv exists at all, because the
    runtime seeds a 535-name example into any home it finds without one.

    Write a new inode and rename it into place. Besides keeping the prior file
    intact on failure, this avoids Linux protected_regular refusing O_CREAT on
    an existing agent-owned file in this shared directory. A symlink is still
    refused rather than silently replaced so a tampered home stops at boot.
    """
    try:
        if os.path.lexists(HOME_DOTENV) and not stat.S_ISREG(os.lstat(HOME_DOTENV).st_mode):
            raise OSError("existing path is not a regular file")
        try:
            # utf-8-sig is the encoding the loader opens this file with
            # (`hermes_cli.env_loader`), so a leading byte-order mark is gone
            # before the parser sees it here exactly as it is there.
            with open(HOME_DOTENV, encoding="utf-8-sig") as handle:
                bindings = list(parse_stream(handle))
        except FileNotFoundError:
            bindings = []
        descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(HOME_DOTENV), prefix=".plow-env.")
    except OSError as error:
        park(f"{HOME_DOTENV} is not a regular file this image can write: {error}")
    # `original.string` is the binding's own text, spans and all; a comment or
    # a blank run comes back under a null key and is carried across with it.
    kept = [b.original.string for b in bindings if b.key not in DOTENV_OWNED_NAMES]
    if kept and not kept[-1].endswith(("\n", "\r")):
        kept.append("\n")
    kept.append(f"API_SERVER_KEY={api_server_key}\n")
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchown(handle.fileno(), 0, pwd.getpwnam("hermes").pw_gid)
            os.fchmod(handle.fileno(), 0o640)
            handle.writelines(kept)
        os.replace(temporary, HOME_DOTENV)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def compose_identity() -> None:
    """Write $HOME/SOUL.md from the image's base persona plus the variant's.

    A temp file in the home and os.replace(), so a SOUL.md the agent swapped
    for a symlink is replaced as a directory entry and never written through.
    Root writes it at whatever mode mkstemp chose; harden_home() takes the
    owner and the mode of what root left, through a held descriptor, next.

    Every step is inside the park, not just the read: an exception escaping
    here exits plow-init and panics the microVM, so a full disk or a variant
    that shipped its persona in some other encoding has to park like anything
    else. The staged file is left where it fell -- the boot is over, nothing
    starts, and it is a breadcrumb for whoever opens the shell.
    """
    try:
        with open(SEED_SOUL, encoding="utf-8") as base:
            identity = base.read()
        if os.path.exists(SEED_PERSONA):
            with open(SEED_PERSONA, encoding="utf-8") as persona:
                identity += "\n" + persona.read()
        descriptor, staged = tempfile.mkstemp(prefix=".SOUL.md.", dir=HOME_DIR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(identity)
        os.replace(staged, os.path.join(HOME_DIR, "SOUL.md"))
    except (OSError, UnicodeDecodeError) as error:
        park(f"the identity could not be composed from {SEED_SOUL} + {SEED_PERSONA}: {error}")


def _owner_display_name(owner: "MemberParticipant") -> str | None:
    """The owner's real name, or None. Collapsed to one line so it cannot spell
    the entry delimiter; dropped when it is only Plow's provider_key fallback
    (the phone number / email handle), which is not a name."""
    name = " ".join((owner.display_name or "").split())
    if not name or name == " ".join((owner.provider_key or "").split()):
        return None
    return name


def seed_user_profile(home: Chat) -> None:
    """Write the first memories/USER.md, as the agent; never one that exists.
    The complete file is staged and hard-linked into place, so it only ever
    appears whole -- a crash mid-write leaves the staged temp, not a truncated
    profile -- and the link fails rather than overwrites once one exists: the
    store is the agent's from its first turn on, and a later boot leaves
    whatever it has made of it."""
    owner = next(p for p in home.participants if isinstance(p, MemberParticipant))
    name = _owner_display_name(owner)
    entries = [f"The owner is {name}."] if name else []
    entries += list(USER_PROFILE)
    content = "\n§\n".join(entries)

    memories = os.path.join(HOME_DIR, "memories")
    os.makedirs(memories, exist_ok=True)
    descriptor, staged = tempfile.mkstemp(prefix=".USER.md.", dir=memories)
    linked = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(content)
        try:
            os.link(staged, os.path.join(memories, "USER.md"))
            linked = True
        except FileExistsError:
            pass
    finally:
        try:
            os.unlink(staged)
        except OSError:
            pass
    if linked:
        print("plow-init: seeded memories/USER.md", file=sys.stderr)


def harden_home() -> None:
    """Put the home's ownership back, after the runtime has taken it.

    The runtime bootstraps whatever home it is pointed at and leaves it 0700
    hermes:hermes, and its auth store chmods the same directory on every write.
    Both run before this does, which is what makes this a repair rather than
    setup -- and why it cannot live in cont-init, which runs first.

    At 0700 hermes:hermes the agent owns its own home, and owning the directory
    is what lets it unlink a root-owned SOUL.md whatever the file's mode says.
    The identity is written fresh here first, from the image's seed, so what
    follows is asserting root's own file rather than repairing the agent's.
    """
    compose_identity()
    hermes = pwd.getpwnam("hermes")

    def hold(path: str, flags: int) -> int:
        """Open the path as the shape it is meant to be, or stop the boot.

        Root is working inside a directory the agent can create entries in, so
        every path here is one the agent could have replaced. `O_NOFOLLOW`
        refuses a symlink, `O_DIRECTORY` refuses anything but a directory, and
        `O_NONBLOCK` means a FIFO left in place fails rather than parking root
        on an open that never returns.
        """
        try:
            return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | flags)
        except OSError as error:
            park(f"{path} is not the file this image left there: {error}")

    # Through the descriptor, not the path. `os.chown` can decline to follow a
    # link; `os.chmod` on Linux cannot, so a path-based chmod beside a
    # link-safe chown hands root's mode change to a target the agent chose.
    for path in (HOME_DIR, os.path.join(HOME_DIR, "skills")):
        descriptor = hold(path, os.O_DIRECTORY)
        os.fchown(descriptor, 0, hermes.pw_gid)
        os.fchmod(descriptor, 0o3770)
        os.close(descriptor)
    soul = os.path.join(HOME_DIR, "SOUL.md")
    descriptor = hold(soul, 0)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        park(f"{soul} is not a regular file")
    os.fchown(descriptor, 0, 0)
    # Ownership is not enough: an agent that leaves this file 0666 keeps
    # other-write after root takes it, and the identity stays rewritable by the
    # thing it is supposed to constrain.
    os.fchmod(descriptor, 0o644)
    os.close(descriptor)


def main() -> None:
    # The host's own setup hook, if this provider has one. Removed once run,
    # so a reboot cannot replay it.
    if os.access(HOST_SETUP, os.X_OK):
        subprocess.run([HOST_SETUP], check=True)
        os.unlink(HOST_SETUP)
    verify_boot_preconditions()
    harden_home()

    credentials = read_credentials()
    identity = ask_plow(credentials)
    home = home_chat(identity)
    write_latch_instructions(identity, credentials.plow_agent_token)
    values = {
        "PLOW_API_BASE": credentials.plow_api_base,
        "PLOW_AGENT_TOKEN": credentials.plow_agent_token,
        "PLOW_HOME_CHANNEL": home.uid,
        # Chat and inference are the same credential; the config names the
        # inference key by variable rather than holding a value.
        "HERMES_CUSTOM_PLOW_API_KEY": credentials.plow_agent_token,
        # Fresh every boot. The gateway's loopback API server will not start
        # without one, and nothing reads it from a file.
        "API_SERVER_KEY": secrets.token_hex(32),
    }
    if credentials.agent_id:
        values["AGENT_ID"] = credentials.agent_id
    if identity.mcp_url:
        values["PLOW_MCP_URL"] = identity.mcp_url
    export(values)
    os.environ.update(values)
    own_home_dotenv(values["API_SERVER_KEY"])

    # The config belongs to the agent -- the chat plugin rewrites it on every
    # connect -- so it is edited as the agent, never as root. A symlink or any
    # other shape somebody left at that path then fails as an ordinary
    # permission error from an unprivileged process, loudly, instead of root
    # writing through it.
    # Read while still root: the seed lives outside every home, where the
    # agent cannot reach it -- which is the point of keeping it there.
    with open(SEED_CONFIG) as handle:
        seed = yaml.safe_load(handle) or {}

    hermes = pwd.getpwnam("hermes")
    os.setgroups([])
    os.setgid(hermes.pw_gid)
    os.setuid(hermes.pw_uid)
    seed_user_profile(home)
    configure(identity, seed)
    print(f"plow-init: configured from {CREDENTIALS} as {home.uid}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 -- see below; this is the last stop before PID 1
        # Every *anticipated* failure calls park() itself, with a reason worth
        # reading. This catches the rest -- a bug here, a disk that filled, an
        # OSError nobody predicted -- because an uncaught exception exits this
        # script, exits /init, and panics the VM. On this platform a crash and
        # a refusal have to end the same way; only the message differs, so the
        # traceback goes to the log where it is useful.
        traceback.print_exc()
        park("plow-init raised an unhandled exception -- see the traceback above")

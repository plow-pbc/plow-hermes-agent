"""Configure this agent from its environment, then let the gateway start.

Runs once, as root, before any service. Everything downstream declares this as
a dependency, and s6-rc starts none of it until this oneshot completes -- which
is the point: an agent whose setup half ran serves its local API, answers every
probe, and cannot be reached by the person it belongs to. So a refusal here
parks rather than exits, and the completion never comes.

A host tells this image where Plow is, and optionally which Agent Index id it
reports as, in the process environment. On exe.dev the host also owns the
bearer: it fronts PLOW_API_BASE with an integration that injects the agent's
credential into every request, so no token reaches this image. A host without
one -- a developer's compose -- sets PLOW_AGENT_TOKEN beside it. The rest of
the agent's identity is asked of Plow through that endpoint. Nothing falls
back -- no endpoint, an agent Plow will not answer for, or no answer at all,
and nothing starts.
"""

from __future__ import annotations

import asyncio
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
import urllib.parse
import urllib.request

import yaml
from dotenv.parser import parse_stream
from pydantic import BaseModel, Field, ValidationError
from typing import Annotated, Literal, Union
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

CONFIG = "/var/lib/hermes/config.yaml"
# TRANSITION: the credential file and the setup hook that writes it are how a
# VM provisioned before plow#2007 is told where Plow is. Remove both, with
# read_file_credentials(), once plow#2007 is live and every such VM has been
# re-provisioned.
CREDENTIALS = "/var/lib/plow/credentials"
HOST_SETUP = "/exe.dev/setup"
CREDENTIALS_WAIT_S = 60
CONTAINER_ENV = "/run/s6/container_environment"
# The zone an agent's clock reads when nothing else names one. Plow holds no
# timezone for an owner, and the box's own is UTC -- a day ahead of a Pacific
# owner every evening, in the message stamps and the prompt's date line alike.
DEFAULT_TIMEZONE = "America/Los_Angeles"
PARK_MARKER = "/run/plow-init.parked"

RETRIES = 10
RETRY_DELAY_S = 3
# How long a refused credential is waited out before it is believed. A rotated
# bearer does not reach the endpoint this boot dials the instant it is issued,
# and the restart that follows a rotation is exactly what lands inside that
# window -- measured at about 50 seconds.
AUTH_WAIT_S = 120
# How long one socket is held before the identity endpoint is asked again.
# Plow delivers a chat born on this line to a socket that is already open, so
# holding one IS the mechanism -- the re-ask is a backstop against a socket
# that is up but silent when it should not be, not the thing that finds the
# chat. This was HOME_POLL_INTERVAL_S, which made every interval mint a
# ticket and open a connection: for an agent whose owner never texts, a
# committed row and a socket lifecycle every three seconds for the life of
# the VM. At 75 such agents that was 15 tickets a second against the API.
#
# There is no window any more. One socket is held for as long as it stays up,
# and the wait ends when Plow says something happened -- not when a timer
# expires. A socket that closes (a deploy, most often) is re-opened, paced by
# the delay below so a server that is down is not dialled in a tight loop; the
# heartbeat is what notices a connection that died without saying so.
HOME_SOCKET_RECONNECT_S = 3
HOME_SOCKET_RECONNECT_MAX_S = 60
# How long a connection must last before it counts as healthy, so the backoff
# resets for a socket that worked and not for one that was accepted and closed
# at once. The plugin's own transport draws the same line at 30s.
HOME_SOCKET_HEALTHY_S = 30
HOME_POLL_INTERVAL_S = 3
HOME_POLL_MAX_INTERVAL_S = 60
HOME_WAIT_LOG_INTERVAL_S = 3600
# When the socket's own failure may next be said out loud. Module state, like
# the wait log it borrows its cadence from: one boot, one running commentary.
_next_socket_log = 0.0
TIMEOUT_S = 10
# The one entry in `mcp_servers` this image manages. Any other belongs to
# whoever added it and is left exactly as it is.
RELAY_SERVER = "plow"
HOME_DIR = "/var/lib/hermes"
HOME_MODE = 0o3770
HOME_GUARD_INTERVAL_S = 10
HOME_DOTENV = "/var/lib/hermes/.env"
# Where a per-chat-type reset policy can be said at all. The gateway reads
# `reset_by_type` from this legacy file only -- config.yaml's `session_reset`
# sets the single default for every session -- so the scoping below has no
# expression in the seed, and this is the file that carries it.
GATEWAY_JSON = "/var/lib/hermes/gateway.json"
SEED_CONFIG = "/opt/hermes/plow-seed/config.yaml"
# The identity, composed on every boot: the base persona this image ships,
# then the variant's own, if it ships one. Composed rather than COPYed into the
# home because a populated volume shadows the image layer forever, and because
# a variant that replaced the file whole silently dropped every base rule
# (plow-hermes-agent#66, life-assistant-hermes-agent#168).
SEED_SOUL = "/opt/hermes/plow-seed/SOUL.md"
SEED_PERSONA = "/opt/hermes/plow-seed/persona.md"
# Latch's `initialize.instructions`, which Hermes drops on connect, written
# where it reads them instead: the context tier, via `terminal.cwd` (#72).
HERMES_MD = os.path.join(HOME_DIR, "HERMES.md")
INSTRUCTIONS_TIMEOUT_S = 5
# What goes wherever a bearer has to be present -- the plugin requires
# PLOW_AGENT_TOKEN, the config names the inference key by variable -- when the
# host gave no token: the integration in front of PLOW_API_BASE replaces the
# Authorization header with the agent's real credential, so Plow never sees it.
TOKEN_PLACEHOLDER = "proxied"


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
# One fact, true regardless of the home's age. A "sessions begin today /
# nothing earlier" claim keyed on USER.md absence would be false for an
# existing agent upgraded to this image; this line already tells the model its
# empty store is not the whole picture -- check the Mac -- which is the goal.
USER_PROFILE = (
    "This server holds only this agent's own work. The owner may have other Plow lines and earlier "
    "agents, whose work is in Messages and mail on their Mac, not in this agent's sessions or memory."
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
    """Where Plow is, and the token to present if the host has one.

    The process environment is the ONLY source: with-contenv hands plow-init
    what the host gave the container's CMD, which on exe.dev is
    /exe.dev/etc/env. Every other source is dropped below.
    """

    plow_api_base: str
    plow_agent_token: str | None = None
    agent_id: str | None = None

    @property
    def bearer(self) -> str:
        # A real token is never replaced; the placeholder only fills a gap.
        return self.plow_agent_token or TOKEN_PLACEHOLDER

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (env_settings,)


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


def home_chat(identity: Identity) -> Chat | None:
    """The one chat that is this agent talking to the person it belongs to.

    Plow does not name it, so the image picks it, by the same rule Plow uses:
    an active chat on this agent's own line holding exactly one member -- the
    owner -- and this agent. Anything else in a chat makes it a group, or
    somebody else's. The line check is load-bearing: a mailbox carrying this
    agent's persona is another line whose threads the credential also opens,
    and an owner alone with the mailbox reads as owner-plus-self too. Zero
    matches means the owner has not made contact yet. Several still refuse:
    the wrong home is an agent talking to the wrong people.
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
    if not matches:
        return None
    if len(matches) > 1:
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
    hypothetical: no agent account (the inherited uid remap did not run), and a
    home that is not a directory this image can work in.
    """
    try:
        pwd.getpwnam("hermes")
    except KeyError:
        park("no `hermes` account -- the image's user setup did not complete")

    if not os.path.isdir(HOME_DIR) or os.path.islink(HOME_DIR):
        park(f"{HOME_DIR} is not a directory -- the agent has no home to start in")


class FileCredentials(Credentials):
    """TRANSITION (remove once plow#2007 is live): the file a pre-#2007 host
    writes. `extra="forbid"` refuses a provisioner that has drifted ahead of
    this image, and the file is its only source."""

    model_config = SettingsConfigDict(extra="forbid")

    plow_agent_token: str

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


def read_credentials() -> Credentials:
    """The environment when it names PLOW_API_BASE; the pre-#2007 file otherwise."""
    if "PLOW_API_BASE" not in os.environ:
        return read_file_credentials()
    try:
        return Credentials()
    except ValidationError as error:
        # `include_input=False`: the default rendering quotes the input back,
        # which here is the token.
        park(f"the environment does not name where Plow is:\n{error.errors(include_input=False)}")


def read_file_credentials() -> FileCredentials:
    """TRANSITION (remove once plow#2007 is live): run the host's setup hook,
    then judge the file it wrote before parsing it.

    It decides where the agent's own bearer token is sent, so anyone else
    owning or reading it chooses both. Two exact modes rather than a rule about
    bits: one merely forbidding the write bits would admit 0644, which hands
    the credential to every account in the container.
    """
    # Removed once run, so a reboot cannot replay it.
    if os.access(HOST_SETUP, os.X_OK):
        subprocess.run([HOST_SETUP], check=True)
        os.unlink(HOST_SETUP)
    # Waited for, not merely required: a host may write this file after the
    # container is already running.
    for _ in range(CREDENTIALS_WAIT_S):
        if os.path.lexists(CREDENTIALS):
            break
        time.sleep(1)
    try:
        info = os.lstat(CREDENTIALS)
    except OSError:
        park(f"no PLOW_API_BASE in the environment and no credential at {CREDENTIALS} after {CREDENTIALS_WAIT_S}s")
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISREG(info.st_mode):
        park(f"{CREDENTIALS} is not a regular file")
    if (info.st_uid, info.st_gid) != (0, 0) or mode not in (0o600, 0o400):
        park(f"{CREDENTIALS} is {info.st_uid}:{info.st_gid} mode {mode:04o} -- expected root:root at 600 or 400")
    try:
        return FileCredentials(_env_file=CREDENTIALS)
    except ValidationError as error:
        park(f"{CREDENTIALS} does not contain only the documented keys:\n{error.errors(include_input=False)}")


def ask_plow(credentials: Credentials, *, waiting: bool = False) -> Identity | None:
    """Ask Plow who this agent is, retrying only what waiting could fix.

    A VM's network is not always up when its first service is: worth retrying,
    not worth surviving. An agent that cannot be told who it is must not come
    up as whoever it was last time -- a home volume outlives its tenant, and
    the failure that hides is a new tenant answering in the previous one's chat.

    A refusal is waited out too, for AUTH_WAIT_S. It is still terminal -- what
    changed is that "refused" now means refused for two minutes, because a
    credential that was just rotated answers 401 for around a minute first, and
    the boot after a rotation is the one that asks.
    Once waiting for first contact, return transient failures to the outer
    poll loop instead: that wait can outlast any bounded boot retry budget.
    """
    url = credentials.plow_api_base.rstrip("/") + "/v1/agents/cloud/me"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {credentials.bearer}", "Accept": "application/json"},
    )
    attempts = 0
    refused_until = time.monotonic() + AUTH_WAIT_S
    while True:
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            # 404 is Plow saying this agent is gone, and nothing else here is
            # an answer about the credential at all.
            if error.code in (401, 403):
                if time.monotonic() >= refused_until:
                    park(f"{url} answered {error.code} for {AUTH_WAIT_S}s -- Plow refused this credential")
                print(f"plow-init: {url} answered {error.code}, waiting for the credential to take",
                      file=sys.stderr)
                time.sleep(RETRY_DELAY_S)
                continue
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
        if waiting:
            return None
        attempts += 1
        if attempts >= RETRIES:
            park(f"gave up asking Plow who this agent is after {RETRIES} attempts -- refusing to start")
        print(f"plow-init: attempt {attempts} to reach Plow failed, retrying ({reason})", file=sys.stderr)
        time.sleep(RETRY_DELAY_S)


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


def default_timezone() -> dict[str, str]:
    """HERMES_TIMEZONE for this boot, or nothing when the environment names a zone.

    Any TZ wins, UTC included -- a variant sets that on purpose before its
    owner has said where they live. A `timezone` in config.yaml needs no check
    here: the gateway copies it over HERMES_TIMEZONE when it starts. Published
    per boot and never written to the config, which Hermes reads over TZ.
    """
    if any(os.environ.get(name, "").strip() for name in ("HERMES_TIMEZONE", "TZ")):
        return {}
    return {"HERMES_TIMEZONE": DEFAULT_TIMEZONE}


def configure(identity: Identity, seed: dict) -> None:
    """Point the agent at its inference provider and its relay.

    A structured edit: it writes the settings it owns and touches nothing else,
    and is skipped when they already hold these values, so a second boot leaves
    the file as it found it. A model id belongs to the provider it was written
    for, so the two move together: HERMES_MODEL when one is named, the seed's
    otherwise, and only under Plow -- another provider's model is nothing this
    image knows how to guess. The provider entry, the display section, the
    retry budget, the tool_search switch, the message timestamps and the
    working directory are the seed's on every boot:
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
        # A cron job pins the provider TYPE it resolved at creation, and a
        # `providers:` entry resolves as the bare `custom`, which names no
        # entry and falls through to an empty OpenRouter key: every ld-* job
        # failed "No LLM provider configured" (Hermes 0.21.2,
        # NousResearch/hermes-agent#109765). cron.model_provider outranks that
        # snapshot and is re-read every tick, so it rescues existing jobs too.
        ("cron", "model_provider"): provider,
        ("agent", "api_max_retries"): seed["agent"]["api_max_retries"],
        ("cron", "model_drift_guard"): seed["cron"]["model_drift_guard"],
        ("display",): seed["display"],
        ("tools", "tool_search", "enabled"): seed["tools"]["tool_search"]["enabled"],
        ("terminal", "cwd"): seed["terminal"]["cwd"],
        # Enforced, not seeded, because every agent that matters already has a
        # config.yaml: a key that only lands in a home with none would reach no
        # existing agent, and this one is a spend ceiling. Hermes compresses at
        # 50% of the window it resolves, GLM-5.2's is 1M, and a turn re-reads
        # its whole prefix -- so the cap is what keeps a long conversation from
        # settling at half a million tokens a turn.
        ("compression", "threshold_tokens"): seed["compression"]["threshold_tokens"],
        # Same reason, and the sharper one: without it an owner's photo reaches
        # a text-only model and comes back a 404. It names Plow, so it follows
        # Plow's own endpoint keys below -- removed when the operator switches
        # inference away, or an owner's receipts keep crossing Plow after they
        # deliberately moved off it.
        ("auxiliary", "vision"): seed["auxiliary"]["vision"] if provider == "plow" else None,
        # Enforced even over an owner's `false`: the stamps are how the model knows today.
        ("gateway", "message_timestamps"): seed["gateway"]["message_timestamps"],
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
        # seed dropped leaves. Two keys are the boot's to add on top, and the
        # caching flag joins the selected model's entry rather than replacing
        # it: a derived seed's `context_length` there is Hermes' only source
        # for the window (2026-09-25: dropped, it fell back to 256K).
        entry = {**seed["providers"][provider_key], "base_url": os.path.expandvars(seed_model.get("base_url", ""))}
        model_id = plow_model or seed_model.get("default")
        models = entry.get("models", {})
        entry["models"] = {**models, model_id: {**(models.get(model_id) or {}), "prompt_caching": True}}
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
    entries.append(USER_PROFILE)
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


def _hold(path: str, flags: int) -> int:
    """Open the path as the shape it is meant to be, or raise.

    Root is working inside a directory the agent can create entries in, so
    every path here is one the agent could have replaced. `O_NOFOLLOW`
    refuses a symlink, `O_DIRECTORY` refuses anything but a directory, and
    `O_NONBLOCK` means a FIFO left in place fails rather than parking root
    on an open that never returns.
    """
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | flags)


def restore_home_mode() -> list[str]:
    """Put the home and its skills back to root:hermes 3770 through a descriptor -- a path-based `os.chmod` follows a symlink on Linux -- and say what differed."""
    hermes = pwd.getpwnam("hermes")
    drifted = []
    for path in (HOME_DIR, os.path.join(HOME_DIR, "skills")):
        descriptor = _hold(path, os.O_DIRECTORY)
        found = os.fstat(descriptor)
        mode = stat.S_IMODE(found.st_mode)
        if (found.st_uid, found.st_gid, mode) != (0, hermes.pw_gid, HOME_MODE):
            drifted.append(f"{path} was {found.st_uid}:{found.st_gid} {mode:o}")
        os.fchown(descriptor, 0, hermes.pw_gid)
        os.fchmod(descriptor, HOME_MODE)
        os.close(descriptor)
    return drifted


def guard_home() -> None:
    """Keep the home's mode for the container's whole life, loudly.

    Only root can put it back, so this loop stays root. It prints what it
    found before it repairs anything, so the evidence survives the repair.
    """
    while True:
        drifted = restore_home_mode()
        if drifted:
            print(f"plow-init: home-guard restored root:hermes 3770 -- {'; '.join(drifted)}. "
                  "Something ran as root in this container (Hermes code under `docker exec` "
                  "without `-u hermes` is enough).", file=sys.stderr, flush=True)
        time.sleep(HOME_GUARD_INTERVAL_S)


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
    restore_home_mode()
    soul = os.path.join(HOME_DIR, "SOUL.md")
    descriptor = _hold(soul, 0)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        park(f"{soul} is not a regular file")
    os.fchown(descriptor, 0, 0)
    # Ownership is not enough: an agent that leaves this file 0666 keeps
    # other-write after root takes it, and the identity stays rewritable by the
    # thing it is supposed to constrain.
    os.fchmod(descriptor, 0o644)
    os.close(descriptor)


# One chat type, one policy. A DM resets after a day with nothing said in it;
# every other type keeps the gateway's own default of never.
SESSION_RESET_CHAT_TYPE = "dm"
SESSION_RESET_POLICY = {"mode": "idle", "idle_minutes": 1440}


def own_session_reset() -> None:
    """Stop a DM from carrying last month's instructions into this month.

    Upstream stopped auto-resetting sessions in July 2026 because people
    expected a conversation to persist. Persistence is right; unbounded
    persistence is not. A chat that never ends keeps every recipe it ever
    derived in front of the model while the environment underneath it moves,
    and the model reads its own history as fact. Observed 2026-09-11 on a
    42-day-old DM: asked a routine question, the agent re-ran a credential
    recipe from August against the PLOW_CHAT_TOKEN alias this script has
    stripped since #56, got a 401 for sending an empty bearer, and told its
    owner the token was "still unauthorized" -- while the PLOW_AGENT_TOKEN
    published two functions up answered the same call 200. Nothing was broken
    except what the session remembered.

    DMs only, and that scoping is the whole reason this is written here rather
    than declared in the seed. A group carries work that outlives a quiet day:
    the STR agent's owners' thread holds guest-reply drafts whose approval has
    to send the mirrored wording unchanged, and drafts have no expiry, so a
    reset between a draft and its approval strands it. `reset_by_type` says
    "DMs only" in one line; the seed's `session_reset` cannot say it at all,
    because the gateway reads per-type policy from this file alone.

    Re-asserted every boot, like `configure`'s settings and for the same
    reason: cont-init seeds only an ABSENT config.yaml, so a default shipped
    in the seed reaches new homes and never the ones already running -- which
    are exactly the homes with a months-old session in them.

    Everything else in the file is somebody else's. Another type's policy
    included: an operator who has said something about groups has said it on
    purpose, and this merges beside it rather than through it. A
    `reset_by_type` that is not a mapping raises rather than being replaced.
    """
    try:
        with open(GATEWAY_JSON) as handle:
            gateway = json.load(handle)
    except FileNotFoundError:
        gateway = {}
    except (OSError, ValueError) as error:
        park(f"{GATEWAY_JSON} is not a file this image can rewrite: {error}")
    if not isinstance(gateway, dict):
        park(f"{GATEWAY_JSON} holds {type(gateway).__name__}, not a JSON object")

    by_type = gateway.setdefault("reset_by_type", {})
    if by_type.get(SESSION_RESET_CHAT_TYPE) == SESSION_RESET_POLICY:
        return
    by_type[SESSION_RESET_CHAT_TYPE] = SESSION_RESET_POLICY

    # A sibling then a rename, as `configure` writes config.yaml: a boot
    # interrupted mid-dump must not leave a half-written file behind, since
    # the gateway reads this one as the layer under config.yaml.
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(GATEWAY_JSON), prefix=".plow-gateway.")
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o640)
            json.dump(gateway, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, GATEWAY_JSON)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


async def _await_chat_frame(base: str, bearer: str, settled=None) -> bool:
    """Hold a socket until Plow says something happened on this line.

    A ticket, then the socket. The grant is a line grant, and the API's
    fan-out matches a granted-scope socket on the chat's line as well as its
    frozen chat ids -- so a chat born after this connect is delivered here,
    which is the whole point: the wait ends on the owner's first text rather
    than on a tick. There is no window: the only clock left is the heartbeat,
    which is what notices a connection that died without closing.

    What a frame SAYS is deliberately not read. The identity endpoint is the
    authority on whether a home chat exists; a frame only decides when to ask
    it again, which keeps this function ignorant of a vocabulary that is the
    API's to change.

    `settled` is asked at every subscribe, reconnects included, and it is what
    closes the discovery race: the caller's ask predates this socket, and a
    home chat born in between is announced to nobody, because the API pushes
    to the sockets registered at the time and replays nothing.

    A close is not news -- a deploy, a revoked ticket, a server going away --
    so it reconnects rather than returning, paced so a server that is down is
    not dialled in a tight loop. Only a frame, or `settled`, ends the hold.
    Setup failures propagate: the caller owns the fallback for a boot that
    cannot open a socket at all.
    """
    # Imported here, not at module scope: this whole path is an optimisation
    # with a sleep behind it, and a boot script that will not even start
    # because an optional accelerant is missing is a worse failure than the
    # slower wait it was meant to avoid. The image asserts the import at build
    # (see Dockerfile), so in the image it is always there.
    import aiohttp

    headers = {"Authorization": f"Bearer {bearer}"}

    async def setup(http):
        """Mint a ticket and get an open socket back, or raise trying."""
        async with http.post(
            f"{base}/v1/ws/ticket", json={}, headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)
        ) as resp:
            resp.raise_for_status()
            ticket = (await resp.json(content_type=None))["ticket"]
        url = f"{base.replace('http', 'ws', 1)}/v1/ws?ticket={urllib.parse.quote(ticket, safe='')}"
        return await http.ws_connect(url, heartbeat=30)

    # `total=None` is for the socket, which is held indefinitely by design --
    # capping it would cap the thing this function exists to do. Setup is
    # bounded separately below, because `connect` covers TCP and TLS but NOT
    # the upgrade response: a server that accepts a connection and then says
    # nothing would otherwise hold this open forever with nothing behind it.
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=TIMEOUT_S)) as http:
            pause = HOME_SOCKET_RECONNECT_S
            while True:
                socket = await asyncio.wait_for(setup(http), TIMEOUT_S)
                opened = time.monotonic()
                async with socket:
                    async for frame in socket:
                        if frame.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            handshake = frame.json().get("type") == "connected"
                        except ValueError:
                            handshake = False
                        if handshake:
                            if settled is not None and await asyncio.to_thread(settled):
                                return True
                            continue
                        return True
                # The socket ended without saying anything. Re-open it: that is
                # the normal path across a deploy, not a failure. The backoff
                # resets only for a connection that LASTED -- an upgrade that
                # succeeds and closes at once is the churn this change removes,
                # and resetting on the open alone would dial it every three
                # seconds forever with the cap never reached.
                if time.monotonic() - opened >= HOME_SOCKET_HEALTHY_S:
                    pause = HOME_SOCKET_RECONNECT_S
                await asyncio.sleep(pause)
                pause = min(pause * 2, HOME_SOCKET_RECONNECT_MAX_S)
    except asyncio.TimeoutError as timeout:
        # aiohttp's connect and read timeouts SUBCLASS asyncio.TimeoutError,
        # and a bare one would read to the caller as something this function
        # never means any more. Setup failing is a transport failure, and the
        # caller's fallback is what answers it.
        raise ConnectionError("chat socket setup timed out") from timeout


def wait_for_chat_event(credentials: Credentials, fallback_sleep: float, settled=None) -> bool:
    """Hold a socket until there is news, or sleep `fallback_sleep` without one.

    Returns whether the socket carried the wait. The caller backs off on
    `False`, so what grows is a run of consecutive failures rather than a
    count of loop passes -- an agent whose socket works for an hour and then
    loses it starts its fallback where a fresh boot would, not at the cap.

    `settled` is asked at every subscribe; True from it means the wait is
    already over.

    The socket is an optimisation over the sleep it replaces, never a
    requirement: every failure -- no ticket, a refused upgrade, an image whose
    venv has no aiohttp -- falls back to sleeping out the fallback, so the poll
    above remains the thing that actually decides. A boot must not hang on a
    transport that is merely faster when it works.
    """
    global _next_socket_log
    started = time.monotonic()
    try:
        heard = asyncio.run(_await_chat_frame(
            credentials.plow_api_base.rstrip("/"), credentials.bearer, settled))
    except Exception as error:  # noqa: BLE001 - the poll is the fallback; a socket that will not open must not stop the boot
        heard = False
        # Once, then at the same hourly cadence as the wait itself. A line per
        # interval is a boot that never stops talking about a Mac nobody has,
        # and the one that matters is the first.
        now = time.monotonic()
        if now >= _next_socket_log:
            print(f"plow-init: chat socket unavailable, falling back to the poll ({type(error).__name__})", file=sys.stderr)
            _next_socket_log = now + HOME_WAIT_LOG_INTERVAL_S
    if heard:
        return True
    remaining = fallback_sleep - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)
    return False


def main() -> None:
    verify_boot_preconditions()
    harden_home()

    credentials = read_credentials()
    next_wait_log = time.monotonic()
    waiting_line = None
    fallback = HOME_POLL_INTERVAL_S
    while True:
        identity = ask_plow(credentials, waiting=waiting_line is not None)
        home = home_chat(identity) if identity is not None else None
        if home is not None:
            break
        if identity is not None:
            waiting_line = identity.line.uid
        now = time.monotonic()
        if now >= next_wait_log:
            print(f"plow-init: waiting for a home chat on {waiting_line}", file=sys.stderr)
            next_wait_log = now + HOME_WAIT_LOG_INTERVAL_S
        if identity is None:
            # The ASK failed, not the wait -- and the two want opposite
            # waits. A socket ends this loop by announcing a chat being born,
            # so if the owner's text landed during the outage that frame is
            # already spent: a fresh ten-minute window would sit quiet over a
            # home chat that is right there. Re-ask soon instead, which is the
            # short wait this constant has always been.
            time.sleep(HOME_POLL_INTERVAL_S)
            continue
        # The ask above predates this socket; `settled` is the same question
        # asked again once it is subscribed, and it is the only thing that sees
        # a home chat born in between.
        held = wait_for_chat_event(
            credentials, fallback,
            lambda: home_chat(ask_plow(credentials, waiting=True)) is not None)
        fallback = HOME_POLL_INTERVAL_S if held else min(fallback * 2, HOME_POLL_MAX_INTERVAL_S)
    write_latch_instructions(identity, credentials.bearer)
    values = {
        # Re-published even when the environment already holds it: the
        # pre-#2007 file is the one source the services cannot inherit.
        "PLOW_API_BASE": credentials.plow_api_base,
        "PLOW_AGENT_TOKEN": credentials.bearer,
        "PLOW_HOME_CHANNEL": home.uid,
        # Chat and inference are the same credential; the config names the
        # inference key by variable rather than holding a value.
        "HERMES_CUSTOM_PLOW_API_KEY": credentials.bearer,
        # Fresh every boot. The gateway's loopback API server will not start
        # without one, and nothing reads it from a file.
        "API_SERVER_KEY": secrets.token_hex(32),
    }
    if credentials.agent_id:
        values["AGENT_ID"] = credentials.agent_id
    if identity.mcp_url:
        values["PLOW_MCP_URL"] = identity.mcp_url
    values.update(default_timezone())
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
    # Only a home observed after waiting gets an empty anchor. An immediate
    # home may have history the plugin must newest-anchor rather than replay.
    if waiting_line is not None:
        try:
            with open(os.path.join(os.environ.get("HERMES_HOME") or HOME_DIR, "plow_chat_last_uid"), "x"):
                pass
        except FileExistsError:
            pass
    seed_user_profile(home)
    configure(identity, seed)
    own_session_reset()
    print(f"plow-init: configured from {credentials.plow_api_base} as {home.uid}", file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1:] == ["guard-home"]:
        guard_home()
    else:
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

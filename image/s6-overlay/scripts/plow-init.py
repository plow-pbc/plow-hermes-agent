"""Configure this agent from its credential, then let the gateway start.

Runs once, as root, before any service. Everything downstream declares this as
a dependency, so a non-zero exit starts nothing -- which is the point: an agent
whose setup half ran serves its local API, answers every probe, and cannot be
reached by the person it belongs to.

A host tells this image two lines, at /var/lib/plow/credentials: where Plow is,
and what to present to it. The rest of the agent's identity is asked of Plow
with that credential. Nothing falls back -- no credential, a credential Plow
will not answer for, or no answer at all, and the container stops.
"""

from __future__ import annotations

import os
import pwd
import secrets
import stat
import subprocess
import sys
import time
import typing
import urllib.error
import urllib.request

import yaml
from pydantic import BaseModel, Field, ValidationError
from typing import Annotated, Literal, Union
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

CREDENTIALS = "/var/lib/plow/credentials"
CONFIG = "/var/lib/hermes/config.yaml"
CONTAINER_ENV = "/run/s6/container_environment"
HOST_SETUP = "/exe.dev/setup"

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


def die(message: str) -> typing.NoReturn:
    sys.exit(f"plow-init: {message}")


class Credentials(BaseSettings):
    """The two lines a host writes, and only ever from that file.

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


class AgentParticipant(BaseModel):
    """One of Plow's own lines in a chat. Exactly one is this agent."""

    type: Literal["agent"]
    relationship: Literal["self", "peer"]


class MemberParticipant(BaseModel):
    """A person in a chat. `owner` is the one the line belongs to."""

    type: Literal["member"]
    uid: str
    role: Literal["owner", "member"]


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

    chats: list[Chat]
    mcp_url: str | None


def home_chat(identity: Identity) -> Chat:
    """The one chat that is this agent talking to the person it belongs to.

    Plow does not name it, so the image picks it, by the same rule Plow uses:
    an active chat holding exactly one member -- the owner -- and this agent.
    Anything else in a chat makes it a group, or somebody else's. Zero matches
    or several is not a thing to guess at: the home channel is where the agent
    answers, and the wrong one is an agent talking to the wrong people.
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
            and len(members) == 1
            and members[0].role == "owner"
        )

    matches = [chat for chat in identity.chats if is_home(chat)]
    if len(matches) != 1:
        seen = "; ".join(
            f"{chat.uid} status={chat.status} "
            + ",".join(
                p.relationship if isinstance(p, AgentParticipant) else p.role
                for p in chat.participants
            )
            for chat in identity.chats
        ) or "no chats at all"
        die(f"cannot tell which chat is home -- {len(matches)} of {len(identity.chats)} qualify: {seen}")
    return matches[0]


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
        die(f"no credential at {CREDENTIALS} after {CREDENTIALS_WAIT_S}s -- refusing to start a gateway nobody can reach")
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISREG(info.st_mode):
        die(f"{CREDENTIALS} is not a regular file")
    if (info.st_uid, info.st_gid) != (0, 0) or mode not in (0o600, 0o400):
        die(f"{CREDENTIALS} is {info.st_uid}:{info.st_gid} mode {mode:04o} -- expected root:root at 600 or 400")
    try:
        # The path is passed rather than baked into the class, so this module
        # names it once.
        return Credentials(_env_file=CREDENTIALS)
    except ValidationError as error:
        # `include_input=False`: the default rendering quotes the offending
        # input back, and for a missing key that input is the whole parsed
        # file -- so a credential lacking PLOW_API_BASE would print the token
        # it does have to s6's stderr, where every log reader can see it.
        die(f"{CREDENTIALS} is not the two lines this image reads:\n{error.errors(include_input=False)}")


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
                die(f"{url} answered {error.code} -- Plow refused this credential")
            reason = f"answered {error.code}"
        except OSError as error:
            reason = f"unreachable: {error}"
        else:
            try:
                return Identity.model_validate_json(raw)
            except ValidationError as error:
                # Same reason as the credential above: the raw answer is a
                # roster of real people.
                die(f"{url} answered something that is not an identity:\n{error.errors(include_input=False)}")
        print(f"plow-init: attempt {attempt} to reach Plow failed, retrying ({reason})", file=sys.stderr)
        time.sleep(RETRY_DELAY_S)
    die(f"gave up asking Plow who this agent is after {RETRIES} attempts -- refusing to start")


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
    image knows how to guess. The retry budget and the tool_search switch are
    the seed's on every boot: cont-init seeds only an absent config.yaml, so a
    home that predates a seed change would otherwise keep the old value for good.
    """
    with open(CONFIG) as handle:
        config = yaml.safe_load(handle) or {}

    seed_model = seed.get("model", {})
    provider = os.environ.get("HERMES_PROVIDER", "plow")
    wanted: dict[tuple[str, ...], object] = {
        ("mcp_servers", RELAY_SERVER, "enabled"): identity.mcp_url is not None,
        ("model", "provider"): provider,
        ("agent", "api_max_retries"): seed["agent"]["api_max_retries"],
        ("tools", "tool_search", "enabled"): seed["tools"]["tool_search"]["enabled"],
    }
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


def own_home_dotenv(api_server_key: str) -> None:
    """Make the home's dotenv agree with the environment, on the one name in it.

    The runtime writes its own API_SERVER_KEY there during cont-init, and it
    loads that file OVER its process environment -- so a key this image
    published would lose to the one persisted in the home, and the per-boot key
    would be decorative. Overwriting the file with ours settles it: both
    sources say the same thing.

    Written rather than emptied, because the runtime seeds a 535-name example
    into any home it finds without a dotenv. And this one name only: the
    tenant's credential is published to the environment and has no business in
    a file the agent can read.

    O_NOFOLLOW is the whole of the safety. This is root writing into a
    directory the agent can create entries in, and the runtime hands this file
    to the agent on every boot -- so the agent can unlink it and leave a
    symlink to /etc/shadow in its place. Opening that link as root would
    truncate whatever it points at. `os.open` refuses instead, and the boot
    stops.
    """
    try:
        descriptor = os.open(HOME_DOTENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
    except OSError as error:
        die(f"{HOME_DOTENV} is not a regular file this image can write: {error}")
    with os.fdopen(descriptor, "w") as handle:
        os.fchown(handle.fileno(), 0, pwd.getpwnam("hermes").pw_gid)
        os.fchmod(handle.fileno(), 0o640)
        handle.write(f"API_SERVER_KEY={api_server_key}\n")


def harden_home() -> None:
    """Put the home's ownership back, after the runtime has taken it.

    The runtime bootstraps whatever home it is pointed at and leaves it 0700
    hermes:hermes, and its auth store chmods the same directory on every write.
    Both run before this does, which is what makes this a repair rather than
    setup -- and why it cannot live in cont-init, which runs first.

    At 0700 hermes:hermes the agent owns its own home, and owning the directory
    is what lets it unlink a root-owned SOUL.md whatever the file's mode says.
    """
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
            die(f"{path} is not the file this image left there: {error}")

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
        die(f"{soul} is not a regular file")
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
    harden_home()

    credentials = read_credentials()
    identity = ask_plow(credentials)
    home = home_chat(identity)
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
    configure(identity, seed)
    print(f"plow-init: configured from {CREDENTIALS} as {home.uid}", file=sys.stderr)


if __name__ == "__main__":
    main()

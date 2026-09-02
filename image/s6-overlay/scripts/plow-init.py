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
from pydantic_settings import BaseSettings, DotEnvSettingsSource, PydanticBaseSettingsSource, SettingsConfigDict

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

    line: dict
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
        die(f"{CREDENTIALS} is not the two lines this image reads:\n{error}")


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
                die(f"{url} answered something that is not an identity:\n{error}")
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


def configure(identity: Identity, seed_model: dict) -> None:
    """Point the agent at its inference provider and its relay.

    A structured edit: it writes the three settings it owns and touches nothing
    else, and is skipped when they already hold these values, so a second boot
    leaves the file as it found it. HERMES_MODEL is written only when set --
    a model id belongs to the provider it was written for.
    """
    with open(CONFIG) as handle:
        config = yaml.safe_load(handle) or {}

    provider = os.environ.get("HERMES_PROVIDER", "plow")
    wanted: dict[tuple[str, ...], object] = {
        ("mcp_servers", RELAY_SERVER, "enabled"): identity.mcp_url is not None,
        ("model", "provider"): provider,
    }
    if os.environ.get("HERMES_MODEL"):
        wanted[("model", "default")] = os.environ["HERMES_MODEL"]

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
        with open(CONFIG, "w") as handle:
            yaml.safe_dump(config, handle, sort_keys=False)


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
    """
    with open(HOME_DOTENV, "w") as handle:
        handle.write(f"API_SERVER_KEY={api_server_key}\n")
    os.chown(HOME_DOTENV, 0, pwd.getpwnam("hermes").pw_gid)
    os.chmod(HOME_DOTENV, 0o640)


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
    for path in (HOME_DIR, os.path.join(HOME_DIR, "skills")):
        os.chown(path, 0, hermes.pw_gid)
        os.chmod(path, 0o3770)
    os.chown(os.path.join(HOME_DIR, "SOUL.md"), 0, 0)


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
        seed_model = (yaml.safe_load(handle) or {}).get("model", {})

    hermes = pwd.getpwnam("hermes")
    os.setgroups([])
    os.setgid(hermes.pw_gid)
    os.setuid(hermes.pw_uid)
    configure(identity, seed_model)
    print(f"plow-init: configured from {CREDENTIALS} as {home.uid}", file=sys.stderr)


if __name__ == "__main__":
    main()

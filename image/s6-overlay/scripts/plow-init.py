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
import secrets
import stat
import subprocess
import sys
import time
import typing
import urllib.error
import urllib.request

import yaml
from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

CREDENTIALS = "/var/lib/plow/credentials"
CONFIG = "/var/lib/hermes/config.yaml"
CONTAINER_ENV = "/run/s6/container_environment"
FIRST_BOOT = "/usr/local/lib/plow/first-boot.sh"
HOST_SETUP = "/exe.dev/setup"

RETRIES = 10
RETRY_DELAY_S = 3
TIMEOUT_S = 10
# The one entry in `mcp_servers` this image manages. Any other belongs to
# whoever added it and is left exactly as it is.
RELAY_SERVER = "plow"


def die(message: str) -> typing.NoReturn:
    sys.exit(f"plow-init: {message}")


class Credentials(BaseSettings):
    """The two lines a host writes. `extra="forbid"` refuses a provisioner that
    has drifted ahead of this image, rather than half-obeying it."""

    model_config = SettingsConfigDict(env_file=CREDENTIALS, extra="forbid")

    plow_api_base: str
    plow_agent_token: str


class Identity(BaseModel):
    """Plow's answer about the agent holding that credential. Neither value is
    derivable here. No relay is a supported shape, which is what `None` means."""

    chat_uid: str
    mcp_url: str | None = None


def read_credentials() -> Credentials:
    """Judge the file before parsing it, on facts a parser cannot see.

    It decides where the agent's own bearer token is sent, so anyone else
    owning or reading it chooses both. Two exact modes rather than a rule about
    bits: one merely forbidding the write bits would admit 0644, which hands
    the credential to every account in the container.
    """
    try:
        info = os.lstat(CREDENTIALS)
    except OSError:
        die(f"no credential at {CREDENTIALS} -- refusing to start a gateway nobody can reach")
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISREG(info.st_mode):
        die(f"{CREDENTIALS} is not a regular file")
    if (info.st_uid, info.st_gid) != (0, 0) or mode not in (0o600, 0o400):
        die(f"{CREDENTIALS} is {info.st_uid}:{info.st_gid} mode {mode:04o} -- expected root:root at 600 or 400")
    try:
        return Credentials()
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


def configure(identity: Identity) -> None:
    """Point the agent at its inference provider and its relay.

    A structured edit: it writes the three settings it owns and touches nothing
    else, and is skipped when they already hold these values, so a second boot
    leaves the file as it found it. HERMES_MODEL is written only when set --
    a model id belongs to the provider it was written for.
    """
    with open(CONFIG) as handle:
        config = yaml.safe_load(handle) or {}

    wanted: dict[tuple[str, ...], object] = {
        ("mcp_servers", RELAY_SERVER, "enabled"): identity.mcp_url is not None,
        ("model", "provider"): os.environ.get("HERMES_PROVIDER", "plow"),
    }
    if os.environ.get("HERMES_MODEL"):
        wanted[("model", "default")] = os.environ["HERMES_MODEL"]

    changed = False
    for path, value in wanted.items():
        section = config
        for key in path[:-1]:
            section = section.setdefault(key, {})
        if section.get(path[-1]) != value:
            section[path[-1]] = value
            changed = True
    if changed:
        with open(CONFIG, "w") as handle:
            yaml.safe_dump(config, handle, sort_keys=False)


def main() -> None:
    # The host's own setup hook, if this provider has one. Removed once run,
    # so a reboot cannot replay it.
    if os.access(HOST_SETUP, os.X_OK):
        subprocess.run([HOST_SETUP], check=True)
        os.unlink(HOST_SETUP)
    # Unconditionally, and a second time on a host that already called it:
    # every step is idempotent, and the home-mode repair is better run late.
    subprocess.run([FIRST_BOOT], check=True)

    credentials = read_credentials()
    identity = ask_plow(credentials)
    values = {
        "PLOW_API_BASE": credentials.plow_api_base,
        "PLOW_AGENT_TOKEN": credentials.plow_agent_token,
        "PLOW_HOME_CHANNEL": identity.chat_uid,
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

    configure(identity)
    print(f"plow-init: configured from {CREDENTIALS} as {identity.chat_uid}", file=sys.stderr)


if __name__ == "__main__":
    main()

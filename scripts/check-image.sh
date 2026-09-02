#!/usr/bin/env bash
# Build the image and check that it comes up the way a tenant needs it to.
#
# usage: check-image.sh [image]
#        PLATFORM=linux/arm64 check-image.sh    # the other architecture
#
# Five checks, each for a failure that builds clean and boots looking healthy:
#
#   1. the plugin imports          — the runtime is pinned by digest while the
#                                    plugin moves in its own repository, so a
#                                    plugin reaching for a newer gateway API
#                                    yields an image that loads one platform
#                                    instead of two: healthy-looking and deaf.
#   2. the write guard follows the home — pointed at a directory the agent never
#                                    uses, every write into its own home is
#                                    denied and nothing says so at boot.
#   3. the image actually boots     — s6 as PID 1, and the gateway listening on
#                                    the loopback port the API's readiness
#                                    probe greps for. This is the check that
#                                    replaced reading the init's unit files:
#                                    what matters is the behaviour they were
#                                    there to produce.
#   4. the protected files survive first boot — the runtime bootstraps whatever
#                                    home it is handed and would otherwise give
#                                    the agent its own identity to unlink; and
#                                    a second boot must be a no-op, because on
#                                    the cloud path first boot runs twice.
#   4b. an operator's root shell cannot lock the agent out — the runtime chmods
#                                    its home to 0700 on every auth write, which
#                                    succeeds when the writer is root.
#   5. the restart path still works — plow.git's credential update shells in and
#                                    restarts the gateway; it must come back as a
#                                    NEW process that is listening.
#   6. a failed init starts nothing — an agent whose credential injection died
#                                    must not come up half-configured.
#   7. a credential-free boot starts nothing — the gateway serves its API and
#                                    runs cron with no adapter attached, so
#                                    every other probe passes and no owner can
#                                    reach the agent.
#
# linux/amd64 is the default because that is what the VM host unpacks; PLATFORM
# overrides it so the architecture a developer's Mac runs natively is checked
# with the same script.
set -euo pipefail

cd "$(dirname "$0")/.."

image="${1:-plow-hermes-agent:check}"
platform="${PLATFORM:-linux/amd64}"

docker buildx build --platform "$platform" \
  --build-arg "PLOW_REVISION=${PLOW_REVISION:-$(git rev-parse HEAD)}" \
  --provenance=false --sbom=false \
  --tag "$image" --load . >&2

# --- 1. the plugin imports -------------------------------------------------
#
# As the gateway's own user, against the image's own copy of the plugin. The env
# values are placeholders: a plugin that needs a real credential to be
# importable is itself the bug. `--interactive` is load-bearing — without it
# docker attaches no stdin, `python -` reads an empty program and exits 0, so
# the sentinel below is asserted rather than the exit status trusted.
out="$(docker run --rm --interactive --platform "$platform" --user 10000:10000 \
  --env PLOW_API_BASE=https://api.invalid \
  --env PLOW_HOME_CHANNEL=cht_import_check \
  --env PLOW_AGENT_TOKEN=import-check-not-a-credential \
  --entrypoint /opt/hermes/.venv/bin/python "$image" - <<'PY'
import importlib.util

spec = importlib.util.spec_from_file_location(
    "plow_chat", "/opt/hermes/plugins/plow_chat/__init__.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print("PLUGIN_IMPORT_OK")
PY
)"

printf '%s\n' "$out"
[[ "$out" == *PLUGIN_IMPORT_OK* ]] \
  || { echo "the import probe did not run" >&2; exit 1; }

# --- 2. the write guard follows the home -----------------------------------
#
# The probe is a file rather than a heredoc because bash 3.2 — what macOS still
# ships — mis-parses a heredoc inside a command substitution.
out="$(docker run --rm --interactive --platform "$platform" --user 10000:10000 \
  --entrypoint /opt/hermes/.venv/bin/python "$image" - < scripts/probe-write-safe-root.py)"

printf '%s\n' "$out"
[[ "$out" == *WRITE_SAFE_ROOT_OK* ]] \
  || { echo "the write-safe-root probe did not run" >&2; exit 1; }

# --- the booted container the rest of the checks share ---------------------
#
# The same shape a developer gets: the tenant values as container environment
# and nothing else. API_SERVER_KEY is deliberately NOT among them — the gateway
# will not open its loopback API server without one, and generating it is init's
# job on this path, so leaving it out is what puts that under test.
name="check-image-$$"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "${hookdir:-}"; }
trap cleanup EXIT

docker run -d --name "$name" --platform "$platform" \
  --env PLOW_API_BASE=https://api.invalid \
  --env PLOW_HOME_CHANNEL=cht_boot_check \
  --env PLOW_AGENT_TOKEN=boot-check-not-a-credential \
  --env HERMES_CUSTOM_PLOW_API_KEY=boot-check-not-a-credential \
  "$image" >/dev/null

# The API's own readiness contract, verbatim: a loopback listener on 8642,
# 0x21C2 in /proc/net/tcp, in state 0A — LISTEN, and not a leftover socket in
# some other state that would answer this question wrongly. Emulating a foreign architecture is slow, so the
# budget is generous rather than tuned.
await_gateway() {
  local i
  for i in $(seq 1 150); do
    if docker exec "$name" sh -c "grep -q '0100007F:21C2 [0-9A-F]\{8\}:[0-9A-F]\{4\} 0A ' /proc/net/tcp" 2>/dev/null; then
      echo "gateway listening on 127.0.0.1:8642 after ${i}s"
      return 0
    fi
    docker inspect -f '{{.State.Running}}' "$name" | grep -q true \
      || { echo "the container exited before the gateway came up" >&2; docker logs "$name" >&2; return 1; }
    sleep 1
  done
  echo "no gateway listener after 150s" >&2
  docker logs "$name" >&2
  return 1
}

# --- 3. s6 is PID 1, and the gateway is up ---------------------------------
await_gateway

# Sampled only once the gateway is up, because PID 1 is three different
# programs on the way there — stage0, then s6-linux-init, then the scanner it
# hands off to. s6-svscan is the steady state, and the steady state is the
# claim: a real supervision tree, not an init that ran and exited.
pid1="$(docker exec "$name" sh -c 'tr "\0" " " < /proc/1/cmdline')"
printf 'PID 1: %s\n' "$pid1"
[[ "$pid1" == *s6-svscan* ]] \
  || { echo "PID 1 is not s6" >&2; exit 1; }

printf 'arch: %s\n' "$(docker exec "$name" uname -m)"

# What the *supervised* process actually inherited, which is the half check 2
# cannot see: the image ENV is only useful if s6's with-contenv delivers it.
# As uid 10000, not as root: reading another user's /proc/<pid>/environ needs
# CAP_SYS_PTRACE, which docker drops, and the gateway is its own reader.
env1="$(docker exec --user 10000:10000 "$name" sh -c '
  pid=$(pgrep -f "hermes gateway run" | head -1)
  tr "\0" "\n" < "/proc/$pid/environ" | grep -E "^(HERMES_HOME|HERMES_WRITE_SAFE_ROOT|API_SERVER_PORT)=" | sort')"
printf '%s\n' "$env1"
grep -qx 'HERMES_HOME=/var/lib/hermes' <<<"$env1"            || { echo "the gateway did not inherit the home" >&2; exit 1; }
grep -qx 'HERMES_WRITE_SAFE_ROOT=/var/lib/hermes' <<<"$env1" || { echo "the gateway did not inherit the write guard" >&2; exit 1; }

# --- 4. the protected files survive first boot, twice ----------------------
#
# `docker restart` is the second first boot: the same home, already bootstrapped,
# init running again over it. Cloud provisioning does the same thing within one
# boot, because the host's setup script calls first-boot.sh and so does init.
# The dotenv the VM host writes, written the way it writes it: root, 0600,
# owned by whoever ran the setup script. Init normalizes it, and it is in the
# snapshot because it is what a rendered provider block would be written into —
# an init that rewrote it on every boot would be invisible without this.
# init wrote this file on the first boot; assert it, because a generated
# API_SERVER_KEY that did not persist would give every boot a different one.
docker exec "$name" grep -q '^API_SERVER_KEY=..*' /var/lib/hermes/.env \
  || { echo "init did not persist a generated API_SERVER_KEY" >&2; exit 1; }

# Running state, not the compiled service list: s6-rc lists what the database
# says should be up, which is identical whether a longrun is answering or
# restart-looping. s6-svstat asks the supervisor.
state() {
  docker exec "$name" sh -c '
    stat -c "%a %U:%G %n" /var/lib/hermes /var/lib/hermes/skills \
                          /var/lib/hermes/SOUL.md /var/lib/hermes/config.yaml \
                          /var/lib/hermes/.env
    sha256sum /var/lib/hermes/config.yaml /var/lib/hermes/.env
    for svc in /run/service/*/; do
      case "$svc" in *s6*) continue;; esac
      printf "%s up=%s\n" "${svc%/}" "$(/command/s6-svstat -o up "$svc")"
    done'
}

docker restart "$name" >/dev/null
await_gateway
first="$(state)"
printf '%s\n' "$first"

# The hardening, asserted rather than merely printed: root must own the identity
# and the home, or the sticky bit protects nothing.
grep -q '^3770 root:hermes /var/lib/hermes$'        <<<"$first" || { echo "home lost 3770 root:hermes" >&2; exit 1; }
grep -q '^3770 root:hermes /var/lib/hermes/skills$' <<<"$first" || { echo "skills/ lost 3770 root:hermes" >&2; exit 1; }
grep -q ' root:root /var/lib/hermes/SOUL.md$'       <<<"$first" || { echo "SOUL.md is not root-owned" >&2; exit 1; }
grep -q ' hermes:hermes /var/lib/hermes/config.yaml$' <<<"$first" || { echo "config.yaml is not hermes-owned" >&2; exit 1; }
grep -q '^640 root:hermes /var/lib/hermes/.env$'    <<<"$first" || { echo ".env is not 0640 root:hermes" >&2; exit 1; }
grep -q '^/run/service/hermes-gateway up=true$'     <<<"$first" || { echo "the gateway is not up" >&2; exit 1; }

docker restart "$name" >/dev/null
await_gateway
second="$(state)"

diff <(printf '%s\n' "$first") <(printf '%s\n' "$second") \
  || { echo "init is not idempotent: the second boot changed the listing above" >&2; exit 1; }
echo "second boot left config, ownership, modes and service state byte-identical"

# --- 4b. an operator's root shell cannot lock the agent out --------------
#
# The runtime chmods its own home to 0700 every time it writes the auth store
# (secure_parent_dir), and swallows the failure. As uid 10000 that fails and
# nothing happens; as ROOT it succeeds and takes the group bit with it, so the
# agent can no longer traverse its own home and every turn EPERMs.
#
# `docker exec <c> hermes ...` is root by default, which is exactly how an
# operator reaches the CLI. What saves it is that /opt/hermes/bin/hermes is a
# privilege-drop shim and is first on PATH — a property of the base image that
# nothing here controls, which is why it is asserted rather than assumed.
resolved="$(docker exec "$name" sh -c 'command -v hermes')"
printf 'hermes on PATH: %s\n' "$resolved"
[[ "$resolved" == /opt/hermes/bin/hermes ]] \
  || { echo "the root docker-exec drop shim is not first on PATH" >&2; exit 1; }

docker exec "$name" sh -c 'hermes skills list >/dev/null 2>&1; true'
after="$(docker exec "$name" stat -c '%a %U:%G' /var/lib/hermes)"
printf 'home after a root `hermes` exec: %s\n' "$after"
[[ "$after" == "3770 root:hermes" ]] \
  || { echo "a root exec of the CLI left the home at $after" >&2; exit 1; }
docker exec --user 10000:10000 "$name" sh -c 'ls /var/lib/hermes >/dev/null' \
  || { echo "uid 10000 can no longer traverse its own home" >&2; exit 1; }

# --- 4c. the dotenv is data, not a script ----------------------------------
#
# Init and the gateway service both read /var/lib/hermes/.env as root, before
# any privilege drop. `.` on that file would give every line of it a root
# shell, and the file is written by whoever provisioned the box. A value that
# looks like a command substitution has to arrive as those characters.
# Carried on TZ because the value has to reach the gateway to be checked, and
# only an allowlisted name gets that far — check 9 covers the names.
docker exec "$name" sh -c "printf 'TZ=\$(touch /pwned)\n' >> /var/lib/hermes/.env"
docker restart "$name" >/dev/null
await_gateway
docker exec "$name" test -e /pwned \
  && { echo "a value in the dotenv was executed as a command" >&2; exit 1; }
probe="$(docker exec --user 10000:10000 "$name" sh -c '
  pid=$(pgrep -f "hermes gateway run" | head -1)
  tr "\0" "\n" < "/proc/$pid/environ" | sed -n "s/^TZ=//p" | tail -1')"
[[ "$probe" == '$(touch /pwned)' ]] \
  || { echo "the dotenv value did not reach the gateway verbatim: $probe" >&2; exit 1; }
echo "dotenv: a command substitution arrived as characters, not a command"

# --- 4d. a symlinked config.yaml is hostile --------------------------------
#
# First boot hands config.yaml to uid 10000 -- it is the one file the plugin
# has to rewrite -- and an agent that owns a file in a sticky directory may
# unlink it. Replace it with a symlink and the next root boot follows it:
# `chown` gives away the target, and the config writer writes through it. The
# file is restored from outside the home instead, and the target is left alone.
docker exec "$name" sh -c 'printf "root-owned target\n" > /etc/plowvictim; chmod 0644 /etc/plowvictim'
docker exec --user 10000:10000 "$name" sh -c \
  'cd /var/lib/hermes && rm -f config.yaml && ln -s /etc/plowvictim config.yaml'
docker restart "$name" >/dev/null
await_gateway

victim="$(docker exec "$name" stat -c '%a %U:%G' /etc/plowvictim)"
[[ "$victim" == "644 root:root" ]] \
  || { echo "the symlink target changed hands: $victim" >&2; exit 1; }
[[ "$(docker exec "$name" cat /etc/plowvictim)" == "root-owned target" ]] \
  || { echo "the symlink target was written through" >&2; exit 1; }
restored="$(docker exec "$name" stat -c '%F %U:%G' /var/lib/hermes/config.yaml)"
[[ "$restored" == "regular file hermes:hermes" ]] \
  || { echo "config.yaml was not restored to a regular file: $restored" >&2; exit 1; }
docker exec "$name" grep -q '^model:$' /var/lib/hermes/config.yaml \
  || { echo "the restored config.yaml is not the image's" >&2; exit 1; }
docker exec "$name" rm -f /etc/plowvictim
echo "symlinked config.yaml: target untouched, config restored, gateway up"

# --- 4e. and so are a directory and a FIFO ---------------------------------
#
# The same door as the symlink, two other shapes through it. Upstream's own
# cont-init reaches config.yaml before anything here does, and a FIFO is the
# sharp one: opening it for reading blocks until somebody writes and nobody
# will, so the container hangs in cont-init with no gateway and no error --
# a denial of service the agent arranges for itself with one mkfifo in a
# directory it was given on purpose. Both must boot to a working gateway with
# the image's config back in place.
shape_survives() {   # shape_survives <label> <shell that creates the shape>
  docker exec --user 10000:10000 "$name" sh -c "cd /var/lib/hermes && rm -rf config.yaml && $2"
  docker restart "$name" >/dev/null
  await_gateway || { echo "the container did not come back with a $1 at config.yaml" >&2; exit 1; }
  shape="$(docker exec "$name" stat -c '%F %U:%G' /var/lib/hermes/config.yaml)"
  [[ "$shape" == "regular file hermes:hermes" ]] \
    || { echo "after a $1, config.yaml is $shape" >&2; exit 1; }
  docker exec "$name" grep -q '^model:$' /var/lib/hermes/config.yaml \
    || { echo "after a $1, config.yaml is not the image's" >&2; exit 1; }
  echo "$1: replaced before the runtime saw it, gateway up"
}

shape_survives directory 'mkdir config.yaml && : > config.yaml/decoy'
shape_survives FIFO 'mkfifo config.yaml'
# Deleting it is the quiet one. Upstream seeds its own generated default when it
# finds none -- a config with no `plow_chat` platform in it -- so the agent
# comes back healthy-looking with no phone line and nothing in the log about it.
shape_survives 'deleted config.yaml' ':'   # nothing to create: the shape IS its absence
# A regular file that is not a symlink and is not the config either. Every
# shape guard says yes to this one; only its contents give it away.
shape_survives 'truncated config.yaml' "printf 'model:\\n  provider: plow\\n' > config.yaml"

# ...which is why this asserts the CONTENT, not just the shape. Two things can
# occupy this path and both are regular files: the image's copy, and the
# generated default upstream seeds when it finds the slot empty. The second
# declares no platform and no provider -- an agent with no phone line and no
# inference, healthy to every probe. Tell them apart by what only the image's
# copy contains.
for block in '^platforms:$' '^  plow_chat:$' '^providers:$' '^  plow:$' '^model:$'; do
  docker exec "$name" grep -q "$block" /var/lib/hermes/config.yaml \
    || { echo "the restored config.yaml is missing $block -- upstream's default won the slot" >&2; exit 1; }
done

# Nothing outside the model block differs from the seed, comparing settings
# rather than bytes. The runtime rewrites this file through its own YAML writer
# on the way up -- comments go, quoting changes -- so a byte comparison would
# fail on a config that is semantically the seed. Comments are not the
# contract; the keys are. plow-config rewrites `model.provider` and
# `model.default` by design, and anything else changing means the file that
# came back is not the one the image ships.
drift="$(docker exec "$name" sh -c '
  strip() { grep -vE "^[[:space:]]*(#|$)" "$1"; }
  diff <(strip /opt/hermes/plow-seed/config.yaml) <(strip /var/lib/hermes/config.yaml) || true' \
  | grep -E '^[<>]' | grep -vE '^[<>]   *(provider|default):' || true)"
[[ -z "$drift" ]] \
  || { echo "the restored config.yaml differs from the seed outside the model block:" >&2
       printf '%s\n' "$drift" >&2; exit 1; }
echo "restored config.yaml is the image's: plow_chat platform, plow provider, no drift from the seed"

# --- 5. the restart path plow.git uses -----------------------------------
#
# Credential updates and their rollback shell in and restart the gateway. The
# shim has to reach the supervisor and the supervisor has to hand back a new
# process that is listening, or an update verifies the credential the OLD
# process is still holding.
before_pid="$(docker exec "$name" /command/s6-svstat -o pid /run/service/hermes-gateway)"
docker exec "$name" systemctl restart --no-block hermes-gateway
after_pid="$(docker exec "$name" /command/s6-svstat -o pid /run/service/hermes-gateway)"
[[ "$before_pid" != "$after_pid" ]] \
  || { echo "the restart left the same process running (pid $before_pid)" >&2; exit 1; }
echo "restart: pid $before_pid -> $after_pid, listening again"

# And it refuses everything else rather than returning 0 for work it did not do.
if docker exec "$name" systemctl status hermes-gateway >/dev/null 2>&1; then
  echo "the systemctl shim answered a command it does not implement" >&2
  exit 1
fi
echo "shim: refuses anything but restart"

docker rm -f "$name" >/dev/null

# --- 5b. a restored config keeps the relay the tenant was provisioned ------
#
# Provisioning turns the relay server on once, at first boot, and then deletes
# itself. A config restored from the seed afterwards would come back with it
# OFF -- chat still working, the tenant's Latch tools silently gone. So the
# flag is derived from the dotenv on every boot instead of remembered, and this
# is the shape that proves it: a cloud-shaped agent, its config deleted by the
# agent itself, back with the relay on.
docker rm -f "$name" >/dev/null 2>&1 || true
docker run -d --name "$name" --platform "$platform" \
  --env PLOW_API_BASE=https://api.invalid \
  --env PLOW_HOME_CHANNEL=cht_boot_check \
  --env PLOW_AGENT_TOKEN=boot-check-not-a-credential \
  --env HERMES_CUSTOM_PLOW_API_KEY=boot-check-not-a-credential \
  --env PLOW_MCP_URL=https://api.invalid/v1/relay/devices/dev_check/mcp \
  "$image" >/dev/null
await_gateway

# One named server's flag, not "the first enabled: in the block" -- the whole
# point below is that there is more than one.
mcp_state() {   # mcp_state <server name>
  docker exec "$name" awk -v want="  $1:" '
    /^[^ ]/          { in_mcp = ($0 == "mcp_servers:") }
    in_mcp && /^  [^ ]/ { in_want = ($0 == want) }
    in_mcp && in_want && /^    enabled: / { sub(/^    enabled: /, ""); print; exit }
  ' /var/lib/hermes/config.yaml
}
[[ "$(mcp_state plow)" == true ]] \
  || { echo "a provisioned relay did not come up enabled: $(mcp_state plow)" >&2; exit 1; }

docker exec --user 10000:10000 "$name" rm -f /var/lib/hermes/config.yaml
docker restart "$name" >/dev/null
await_gateway
[[ "$(mcp_state plow)" == true ]] \
  || { echo "the restored config lost the relay: enabled=$(mcp_state plow)" >&2; exit 1; }
echo "relay: enabled through a seed restore, not left behind by the one-shot provisioner"

# An operator's own MCP server, added to the same block. Init writes one flag,
# for one server: anything else in mcp_servers is theirs, on or off, and stays
# as they left it.
#
# The other server always starts on the OPPOSITE value to the one the relay
# flag is about to be written with. That is the whole point of the case: give
# it the same value and an edit that wrote every server in the block would
# leave it looking correct, and the check would pass under the bug it exists
# to catch.
add_other_server() {   # add_other_server <enabled>
  docker exec "$name" sed -i \
    "/^mcp_servers:\$/a\\  other:\\n    url: https://example.invalid\\n    enabled: $1" \
    /var/lib/hermes/config.yaml
  docker restart "$name" >/dev/null
  await_gateway
}
add_other_server false
[[ "$(mcp_state other)" == false ]] \
  || { echo "init switched ON an MCP server it does not own: other=$(mcp_state other)" >&2; exit 1; }
[[ "$(mcp_state plow)" == true ]] \
  || { echo "the relay flag stopped being written once another server was present" >&2; exit 1; }
echo "relay on: another operator's MCP server kept its own enabled: false"

docker rm -f "$name" >/dev/null

# ...and with no relay provisioned, where init writes `false` and must still
# write it to exactly one server.
docker run -d --name "$name" --platform "$platform" \
  --env PLOW_API_BASE=https://api.invalid \
  --env PLOW_HOME_CHANNEL=cht_boot_check \
  --env PLOW_AGENT_TOKEN=boot-check-not-a-credential \
  --env HERMES_CUSTOM_PLOW_API_KEY=boot-check-not-a-credential \
  "$image" >/dev/null
await_gateway
add_other_server true
[[ "$(mcp_state other)" == true ]] \
  || { echo "init switched OFF an MCP server it does not own: other=$(mcp_state other)" >&2; exit 1; }
[[ "$(mcp_state plow)" == false ]] \
  || { echo "the relay came up enabled with no PLOW_MCP_URL: plow=$(mcp_state plow)" >&2; exit 1; }
echo "relay off: another operator's MCP server kept its own enabled: true"

docker rm -f "$name" >/dev/null

# --- 6. a failed init starts nothing ---------------------------------------
#
# A first-boot.d drop-in that exits non-zero is the cheapest stand-in for the
# real failure: the VM host's credential injection dying halfway. Init is a
# oneshot the gateway depends on, so the gateway must never be reached, and
# PID 1 must exit rather than sit there looking alive.
hookdir="$(mktemp -d)"
printf '#!/bin/sh\nexit 9\n' > "$hookdir/99-fail.sh"
chmod 0755 "$hookdir/99-fail.sh"

docker run --name "$name" --platform "$platform" \
  --env PLOW_AGENT_TOKEN=boot-check-not-a-credential \
  --volume "$hookdir:/usr/local/lib/plow/first-boot.d:ro" \
  "$image" >/dev/null 2>&1 || true

out="$(docker logs "$name" 2>&1)"
grep -q 'first-boot hook failed' <<<"$out" || { echo "the failing hook did not run" >&2; printf '%s\n' "$out" >&2; exit 1; }
grep -q 'hermes-gateway: starting' <<<"$out" && { echo "the gateway started despite a failed init" >&2; exit 1; }
code="$(docker inspect -f '{{.State.ExitCode}}' "$name")"
[[ "$code" != 0 ]] || { echo "a failed init left PID 1 exiting 0" >&2; exit 1; }
echo "failed init: gateway never started, PID 1 exited $code"

docker rm -f "$name" >/dev/null

# --- 7. a credential-free boot starts nothing ------------------------------
#
# The gateway comes up without a credential: it serves its loopback API and
# runs the cron scheduler with no adapter attached. Every probe passes and no
# owner can reach the agent, which is the failure this refuses to ship.
docker run --name "$name" --platform "$platform" "$image" >/dev/null 2>&1 || true

out="$(docker logs "$name" 2>&1)"
grep -q 'PLOW_AGENT_TOKEN is unset' <<<"$out" || { echo "a credential-free boot was not refused" >&2; printf '%s\n' "$out" >&2; exit 1; }
grep -q 'hermes-gateway: starting' <<<"$out" && { echo "the gateway started with no credential" >&2; exit 1; }
echo "no credential: gateway never started, PID 1 exited $(docker inspect -f '{{.State.ExitCode}}' "$name")"

# --- 8. the bundled skills are readable by the gateway user -----------------
#
# The bundled tree is what a home without these skills gets them from, and
# where later updates to them come from. The gateway reconciles it into
# $HERMES_HOME/skills as uid 10000, so a skill that uid cannot READ is a skill
# such a home never receives. Checked as that uid, because root can read a tree
# nobody else can, which is the failure this would otherwise miss.
docker run --rm --platform "$platform" --user 10000:10000 \
  --entrypoint /usr/bin/test "$image" \
  -r /opt/hermes/skills/productivity/plow-connectors/SKILL.md \
  || { echo "the bundled skills tree is missing or unreadable by uid 10000" >&2; exit 1; }
echo "bundled skills: readable by the gateway user"

# --- 9. a dotenv cannot name what it is not allowed to ---------------------
#
# Refusing to *run* the file is half of it. A NAME can be as dangerous as a
# value while the reader is still root: PATH sends every later command
# somewhere of the file's choosing, LD_PRELOAD loads a library into the
# gateway. The names are an allowlist, so a dotenv carrying either is refused
# and nothing starts -- placed into the home before first boot, which is where
# a hostile one would come from.
docker rm -f "$name" >/dev/null
docker create --name "$name" --platform "$platform" "$image" >/dev/null
printf 'PLOW_AGENT_TOKEN=boot-check-not-a-credential\nPATH=/var/lib/hermes/bin\nLD_PRELOAD=x\n' \
  > "$hookdir/bad.env"
docker cp "$hookdir/bad.env" "$name:/var/lib/hermes/.env" >/dev/null
docker start "$name" >/dev/null 2>&1 || true
docker wait "$name" >/dev/null 2>&1 || true

out="$(docker logs "$name" 2>&1)"
grep -q 'sets PATH, which this image does not read' <<<"$out" \
  || { echo "a dotenv setting PATH was not refused" >&2; printf '%s\n' "$out" >&2; exit 1; }
grep -q 'hermes-gateway: starting' <<<"$out" \
  && { echo "the gateway started from a dotenv naming PATH" >&2; exit 1; }
code="$(docker inspect -f '{{.State.ExitCode}}' "$name")"
[[ "$code" != 0 ]] || { echo "a refused dotenv left PID 1 exiting 0" >&2; exit 1; }
echo "dotenv: PATH/LD_PRELOAD refused, gateway never started, PID 1 exited $code"

docker rm -f "$name" >/dev/null

# --- 10. a rotated credential takes ----------------------------------------
#
# Re-running the activation writes a new token into the compose dotenv, and the
# agent's home is a volume that outlives the container. If the persisted copy
# won, a recreated agent would keep presenting the token that was just replaced
# -- revoked, and failing in a way that looks like the new one is wrong. The
# environment is the source of truth whenever it carries a credential, and the
# file is rewritten to match.
docker rm -f "$name" >/dev/null 2>&1 || true
vol="check-image-vol-$$"
docker volume create "$vol" >/dev/null
rotate_boot() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" --platform "$platform" \
    --volume "$vol:/var/lib/hermes" \
    --env PLOW_API_BASE=https://api.invalid \
    --env PLOW_HOME_CHANNEL=cht_boot_check \
    --env PLOW_AGENT_TOKEN="$1" \
    --env HERMES_CUSTOM_PLOW_API_KEY="$1" \
    "$image" >/dev/null
  await_gateway
}

rotate_boot token-before
rotate_boot token-after

seen="$(docker exec --user 10000:10000 "$name" sh -c '
  pid=$(pgrep -f "hermes gateway run" | head -1)
  tr "\0" "\n" < "/proc/$pid/environ" | sed -n "s/^PLOW_AGENT_TOKEN=//p"')"
[[ "$seen" == token-after ]] \
  || { echo "the recreated gateway is still holding '$seen'" >&2; exit 1; }
docker exec "$name" grep -qx 'PLOW_AGENT_TOKEN=token-after' /var/lib/hermes/.env \
  || { echo "the persisted dotenv still names the replaced token" >&2; exit 1; }
echo "rotation: the recreated gateway and its dotenv both carry the new token"

docker rm -f "$name" >/dev/null
docker volume rm "$vol" >/dev/null

echo "ok: $image ($platform) builds, imports, boots under s6, keeps its hardening, and fails closed" >&2

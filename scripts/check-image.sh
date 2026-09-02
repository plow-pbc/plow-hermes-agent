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
# There is one shape now, and every check below runs in it: a credential
# drop-in and nothing else. A VM host writes that file; under compose it is a
# bind mount promoted into place. Everything the agent additionally runs on --
# its home channel, its relay endpoint, its inference key alias, its server
# key, its timezone -- the image derives or asks Plow for. API_SERVER_KEY is
# deliberately not among the things it is handed: the gateway will not open its
# loopback API server without one, and generating it is init's job.
#
# The identity endpoint is stubbed rather than mocked away: the retry, the
# bearer header, the JSON shape and the rotation all live in the request, and a
# check that skipped it would be checking the parts that were never in doubt.
name="check-image-$$"
cleanup() {
  docker rm -f "$name" ${stub:+"$stub"} >/dev/null 2>&1 || true
  [[ -n "${net:-}" ]] && docker network rm "$net" >/dev/null 2>&1
  rm -rf ${hookdir:+"$hookdir"} ${cloud_dir:+"$cloud_dir"} ${host_dir:+"$host_dir"}
  return 0
}
trap cleanup EXIT

cloud_dir="$(mktemp -d)"
mkdir -p "$cloud_dir/plow"

net="check-image-net-$$"
docker network create "$net" >/dev/null

stub="check-image-stub-$$"
docker run -d --name "$stub" --platform "$platform" --network "$net" \
  --network-alias stub \
  --volume "$PWD/scripts/stub-agents-cloud-me.py:/stub.py:ro" \
  --entrypoint /opt/hermes/.venv/bin/python "$image" /stub.py >/dev/null

# The drop-in, written the way a host writes it: root-owned 0600. Through a tar
# stream because `docker cp` of a plain file carries the ownership of whoever
# ran it -- a developer's uid, which init refuses on sight, and the refusal
# would read as a bug in the image rather than in the fixture. Ownership is
# then asserted anyway, because a fixture that lands differently makes every
# check after it meaningless.
cred_mode=0600   # what the host writes; a case below varies it deliberately
write_credentials() {   # write_credentials <api base> <token> [extra line...]
  local base="$1" token="$2"; shift 2
  { printf 'PLOW_API_BASE=%s\n' "$base"
    printf 'PLOW_AGENT_TOKEN=%s\n' "$token"
    for line in "$@"; do printf '%s\n' "$line"; done
  } > "$cloud_dir/plow/credentials"
  chmod "$cred_mode" "$cloud_dir/plow/credentials"
}

# The directory too, every time: the image does not ship /var/lib/plow, the
# host creates it, and untarring it again is how a rotation replaces the file.
place_credentials() {
  tar -cf - -C "$cloud_dir" --uid 0 --gid 0 --uname root --gname root plow \
    | docker cp - "$name:/var/lib" >/dev/null
}

# A rotation: the same VM, the file replaced under it.
drop_credentials() {   # drop_credentials <api base> <token> [extra line...]
  write_credentials "$@"
  place_credentials
}

# A provision: a container with no environment at all, and the drop-in placed
# before it ever starts.
cloud_container() {   # cloud_container <api base> <token> [extra line...]
  write_credentials "$@"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker create --name "$name" --platform "$platform" --network "$net" "$image" >/dev/null
  place_credentials
}

cloud_container http://stub:8080 cloud-token-one
docker start "$name" >/dev/null

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

# --- 3. the drop-in is the whole of what the agent was told ----------------
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
owner="$(docker exec "$name" stat -c '%a %U:%G' /var/lib/plow/credentials)"
printf 'credentials: %s\n' "$owner"
[[ "$owner" == "600 root:root" ]] \
  || { echo "the fixture did not land as the host writes it: $owner" >&2; exit 1; }

# The rendered dotenv, in full: every name the contract says the image derives
# for itself, and none it was handed.
rendered="$(docker exec "$name" cat /var/lib/hermes/.env)"
printf '%s\n' "$rendered"
grep -qx 'PLOW_API_BASE=http://stub:8080'                                        <<<"$rendered" || { echo ".env lost the API base" >&2; exit 1; }
grep -qx 'PLOW_AGENT_TOKEN=cloud-token-one'                                      <<<"$rendered" || { echo ".env lost the credential" >&2; exit 1; }
grep -qx 'PLOW_HOME_CHANNEL=cht_cloud_one'                                       <<<"$rendered" || { echo "the home channel did not come from the identity endpoint" >&2; exit 1; }
grep -qx 'HERMES_CUSTOM_PLOW_API_KEY=cloud-token-one'                            <<<"$rendered" || { echo "the inference key alias was not derived from the credential" >&2; exit 1; }
grep -qx 'PLOW_MCP_URL=http://stub:8080/v1/relay/devices/dev_cloud_check/mcp'    <<<"$rendered" || { echo "the relay URL did not come from the identity endpoint" >&2; exit 1; }
grep -qx 'TZ=UTC'                                                                <<<"$rendered" || { echo "TZ did not default to UTC" >&2; exit 1; }
grep -q  '^API_SERVER_KEY=..*'                                                   <<<"$rendered" || { echo "no API_SERVER_KEY was generated" >&2; exit 1; }

mode="$(docker exec "$name" stat -c '%a %U:%G' /var/lib/hermes/.env)"
printf '.env: %s\n' "$mode"
[[ "$mode" == "640 root:hermes" ]] \
  || { echo "the rendered .env is $mode, not 640 root:hermes" >&2; exit 1; }

[[ "$(mcp_state plow)" == true ]] \
  || { echo "a cloud-shaped boot did not enable the relay: $(mcp_state plow)" >&2; exit 1; }
echo "cloud boot: gateway up, identity fetched, relay enabled, .env 0640 root:hermes"


# --- 3b. s6 is PID 1, and the gateway is up --------------------------------

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
# only an allowlisted name gets that far — check 9 covers the names. Replaced
# rather than appended: the file already carries a TZ, and the parser takes the
# first value it sees for a name, so an appended line would never be read.
docker exec "$name" sh -c 'sed -i "s|^TZ=.*|TZ=\$(touch /pwned)|" /var/lib/hermes/.env'
docker exec "$name" grep -qx 'TZ=$(touch /pwned)' /var/lib/hermes/.env \
  || { echo "the hostile value did not land in the dotenv" >&2; exit 1; }
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
# Temp files, not process substitution: `docker exec sh -c` is dash in this
# image, which has no `<(...)`, so that spelling was a syntax error -- and the
# `|| true` on the end swallowed it, leaving an assertion that passed on a
# probe which never ran. The sentinel is the other half: an empty diff and a
# probe that died both look like "no drift" without it.
probe="$(docker exec "$name" sh -c '
  set -e
  strip() { grep -vE "^[[:space:]]*(#|$)" "$1" > "$2" || true; }
  strip /opt/hermes/plow-seed/config.yaml /tmp/seed.stripped
  strip /var/lib/hermes/config.yaml /tmp/live.stripped
  diff /tmp/seed.stripped /tmp/live.stripped || true
  rm -f /tmp/seed.stripped /tmp/live.stripped
  echo DRIFT_PROBE_OK')"
grep -qx DRIFT_PROBE_OK <<<"$probe" \
  || { echo "the config-drift probe did not run to completion:" >&2
       printf '%s\n' "$probe" >&2; exit 1; }
drift="$(grep -E '^[<>]' <<<"$probe" | grep -vE '^[<>]   *(provider|default):' || true)"
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
cloud_container http://stub:8080 cloud-token-one
docker start "$name" >/dev/null
await_gateway

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
cloud_container http://stub:8080 cloud-token-norelay
docker start "$name" >/dev/null
await_gateway
add_other_server true
[[ "$(mcp_state other)" == true ]] \
  || { echo "init switched OFF an MCP server it does not own: other=$(mcp_state other)" >&2; exit 1; }
[[ "$(mcp_state plow)" == false ]] \
  || { echo "the relay came up enabled with no relay in the identity: plow=$(mcp_state plow)" >&2; exit 1; }
echo "relay off: another operator's MCP server kept its own enabled: true"

docker rm -f "$name" >/dev/null

# --- 5d. rotation is a rewrite of that file and a restart ------------------
#
# The host never shells in. It replaces the two lines and restarts the VM, and
# the agent must come back holding the new credential -- in the gateway's own
# environment, not merely on disk -- with whatever else Plow now says about it.
# The persisted dotenv is the thing that would silently win here, which is why
# the *process* is asked rather than the file alone.
cloud_container http://stub:8080 cloud-token-one
docker start "$name" >/dev/null
await_gateway

key_before="$(docker exec "$name" sed -n 's/^API_SERVER_KEY=//p' /var/lib/hermes/.env)"

drop_credentials http://stub:8080 cloud-token-two
docker restart "$name" >/dev/null
await_gateway

seen="$(docker exec --user 10000:10000 "$name" sh -c '
  pid=$(pgrep -f "hermes gateway run" | head -1)
  tr "\0" "\n" < "/proc/$pid/environ" | sed -n "s/^PLOW_AGENT_TOKEN=//p"')"
[[ "$seen" == cloud-token-two ]] \
  || { echo "the restarted gateway is still holding '$seen'" >&2; exit 1; }
docker exec "$name" grep -qx 'PLOW_AGENT_TOKEN=cloud-token-two' /var/lib/hermes/.env \
  || { echo "the persisted dotenv still names the replaced token" >&2; exit 1; }
docker exec "$name" grep -qx 'HERMES_CUSTOM_PLOW_API_KEY=cloud-token-two' /var/lib/hermes/.env \
  || { echo "the inference key alias kept the replaced token" >&2; exit 1; }
docker exec "$name" grep -qx 'PLOW_HOME_CHANNEL=cht_cloud_two' /var/lib/hermes/.env \
  || { echo "the home channel was not re-asked after the rotation" >&2; exit 1; }
key_after="$(docker exec "$name" sed -n 's/^API_SERVER_KEY=//p' /var/lib/hermes/.env)"
[[ -n "$key_after" && "$key_before" == "$key_after" ]] \
  || { echo "the rotation changed the server key: $key_before -> $key_after" >&2; exit 1; }
echo "rotation: new token in the gateway's environment and its dotenv, server key kept"

# A tenant whose relay went away. The dotenv still carries the URL from when it
# had one, and the file fills only what nothing else set -- so nothing but an
# explicit removal keeps a dead relay from coming back enabled.
drop_credentials http://stub:8080 cloud-token-norelay
docker restart "$name" >/dev/null
await_gateway
docker exec "$name" grep -q '^PLOW_MCP_URL=' /var/lib/hermes/.env \
  && { echo "a withdrawn relay survived in the dotenv" >&2; exit 1; }
[[ "$(mcp_state plow)" == false ]] \
  || { echo "a withdrawn relay is still enabled: $(mcp_state plow)" >&2; exit 1; }
echo "withdrawn relay: gone from the dotenv and switched off in the config"

# --- 5d2. an outage does not take a provisioned agent down -----------------
#
# Asking Plow on every boot is what keeps the home channel current, and it is
# also a dependency every restart now has. An agent that has already been told
# who it is must not lose its phone line because Plow was unreachable for the
# ninety seconds its VM was rebooting -- but it must not adopt an identity that
# was never recorded against the credential it is holding either, which is the
# same volume-outlives-the-tenant problem the dotenv rules exist for.
#
# The stub is stopped rather than repointed: the drop-in still names the same
# base, which is what a real outage looks like from inside the VM.
logs_since() {   # logs_since <line count before the boot>
  docker logs "$name" 2>&1 | tail -n +"$(( $1 + 1 ))"
}
log_lines() { docker logs "$name" 2>&1 | wc -l | tr -d ' '; }

# The rule is about who said what, not about whether something went wrong.
# Plow ANSWERING that this credential is not this agent's -- revoked, or an
# agent that no longer exists -- is the case where a recorded identity is
# exactly what must not be reused, and it must refuse even though the token in
# the file is the one that identity was recorded under. The credential does not
# change here; only the answer does.
stub_control() { docker exec "$stub" sh -c "$1"; }

pre="$(log_lines)"
stub_control 'touch /revoked'
docker restart "$name" >/dev/null
docker wait "$name" >/dev/null
out="$(logs_since "$pre")"
grep -q 'not falling back to a persisted identity' <<<"$out" \
  || { echo "a 404 from Plow fell back to the persisted identity" >&2; printf '%s\n' "$out" >&2; exit 1; }
grep -q 'hermes-gateway: starting' <<<"$out" \
  && { echo "the gateway started on a credential Plow refused" >&2; exit 1; }
code="$(docker inspect -f '{{.State.ExitCode}}' "$name")"
[[ "$code" != 0 ]] || { echo "a refused credential left PID 1 exiting 0" >&2; exit 1; }
echo "Plow says 404 for this credential: gateway never started, PID 1 exited $code"

# ...and an answer that is not an identity, which waiting cannot improve either.
stub_control 'rm -f /revoked; touch /garbage'
pre="$(log_lines)"
docker restart "$name" >/dev/null
docker wait "$name" >/dev/null
out="$(logs_since "$pre")"
grep -q 'not falling back to a persisted identity' <<<"$out" \
  || { echo "a malformed answer fell back to the persisted identity" >&2; printf '%s\n' "$out" >&2; exit 1; }
echo "Plow answers with no chat_uid: gateway never started"

# A 5xx is the other half of the same distinction: Plow said nothing usable,
# but it did not say no. Same silence as a dead socket, so the fallback applies.
stub_control 'rm -f /garbage; touch /unavailable'
pre="$(log_lines)"
docker restart "$name" >/dev/null
await_gateway
out="$(logs_since "$pre")"
grep -q 'booting on the identity this home already holds' <<<"$out" \
  || { echo "a 503 did not fall back to the persisted identity" >&2; printf '%s\n' "$out" >&2; exit 1; }
echo "Plow says 503: gateway up on the identity the home already held"
stub_control 'rm -f /unavailable'

docker stop "$stub" >/dev/null

# No answer at all, which is the case the fallback was written for. Same token
# as the boot before it -- `cloud-token-norelay`, whose identity this home
# recorded a moment ago.
pre="$(log_lines)"
docker restart "$name" >/dev/null
await_gateway
out="$(logs_since "$pre")"
grep -q 'booting on the identity this home already holds' <<<"$out" \
  || { echo "an outage did not fall back to the persisted identity" >&2; printf '%s\n' "$out" >&2; exit 1; }
docker exec "$name" grep -qx 'PLOW_HOME_CHANNEL=cht_cloud_three' /var/lib/hermes/.env \
  || { echo "the fallback boot did not keep the recorded home channel" >&2; exit 1; }
echo "outage, same credential: gateway up on the identity the home already held"

# ...and a token this home has never seen is the case that must still refuse.
# A rotation the host performed while Plow was down: adopting the recorded home
# channel here would put the new tenant in the previous one's chat. (A first
# boot is the same shape with no dotenv at all -- that is the `unreachable
# Plow` case below.)
pre="$(log_lines)"
drop_credentials http://stub:8080 cloud-token-two
docker restart "$name" >/dev/null
docker wait "$name" >/dev/null
out="$(logs_since "$pre")"
grep -q 'this home holds no identity for this credential' <<<"$out" \
  || { echo "a rotated token adopted an identity recorded for another one" >&2; printf '%s\n' "$out" >&2; exit 1; }
grep -q 'hermes-gateway: starting' <<<"$out" \
  && { echo "the gateway started on an identity that was not its own" >&2; exit 1; }
code="$(docker inspect -f '{{.State.ExitCode}}' "$name")"
[[ "$code" != 0 ]] || { echo "a refused fallback left PID 1 exiting 0" >&2; exit 1; }
echo "outage, rotated credential: gateway never started, PID 1 exited $code"

docker start "$stub" >/dev/null
docker rm -f "$name" >/dev/null

# --- 5e. a credential drop-in that cannot be used starts nothing -----------
#
# Three ways it goes wrong, all of them ending the same way: no gateway, PID 1
# dead. A half-configured agent passes every other probe in this file.
refused() {   # refused <label> <expected log line> ; container already created
  docker start "$name" >/dev/null 2>&1 || true
  docker wait "$name" >/dev/null 2>&1 || true
  local out; out="$(docker logs "$name" 2>&1)"
  grep -q "$2" <<<"$out" || { echo "$1: expected '$2' in the log" >&2; printf '%s\n' "$out" >&2; exit 1; }
  grep -q 'hermes-gateway: starting' <<<"$out" && { echo "$1: the gateway started anyway" >&2; exit 1; }
  local code; code="$(docker inspect -f '{{.State.ExitCode}}' "$name")"
  [[ "$code" != 0 ]] || { echo "$1: PID 1 exited 0" >&2; exit 1; }
  echo "$1: gateway never started, PID 1 exited $code"
}

# Plow never answers. The retry is what makes this take a moment, and failing
# closed is what makes it safe: a stale home channel is not a fallback.
cloud_container http://stub:9 cloud-token-one
refused "unreachable Plow" 'gave up asking Plow who this agent is'

# A host deciding something the contract does not let it decide. The home
# channel is Plow's answer, not the host's claim, and a file naming it is a
# provisioner out of date with the image -- refused rather than half-obeyed.
cloud_container http://stub:8080 cloud-token-one 'PLOW_HOME_CHANNEL=cht_host_says'
refused "an out-of-contract key" 'sets PLOW_HOME_CHANNEL, which this image does not read'

# A drop-in anyone but root can write is a drop-in anyone but root can point at
# their own API base, and the agent's bearer token goes with it; one anyone can
# READ is the credential itself handed over. Neither is a bit-mask question --
# 0644 and 0620 both pass a rule that only forbids the write bits -- so the
# accepted modes are enumerated, and every neighbour of them is checked.
for bad_mode in 0666 0644 0620 0602; do
  cred_mode="$bad_mode"
  cloud_container http://stub:8080 cloud-token-one
  refused "a drop-in at $bad_mode" 'expected a root:root file with mode 600 or 400'
done
cred_mode=0600

# ...and the one mode besides 0600 that is not a loosening: the same file, with
# root's own write taken away. A host that hardens further must not be refused.
cred_mode=0400
cloud_container http://stub:8080 cloud-token-one
docker start "$name" >/dev/null
await_gateway
docker exec "$name" grep -qx 'PLOW_HOME_CHANNEL=cht_cloud_one' /var/lib/hermes/.env \
  || { echo "a 0400 drop-in did not render the dotenv" >&2; exit 1; }
echo "drop-in at 0400: accepted, gateway up"
cred_mode=0600

# --- 5f. a bind-mounted drop-in is promoted rather than refused ------------
#
# Under compose the credential is a file on someone's own machine, and a bind
# mount carries that machine's ownership and mode in with it -- on a Linux host
# never root's, and the gate above refuses exactly that on sight. So the mount
# lands under a name of its own and cont-init copies it, as root, into the file
# the gate then applies to unchanged. The fixture is therefore the shape that
# would otherwise be refused: uid 10000, mode 0644.
host_dir="$(mktemp -d)"
mkdir -p "$host_dir/plow"
host_credentials() {   # host_credentials <api base> <token>
  write_credentials "$@"
  cp "$cloud_dir/plow/credentials" "$host_dir/plow/credentials.host"
  chmod 0644 "$host_dir/plow/credentials.host"
  tar -cf - -C "$host_dir" --uid 10000 --gid 10000 --uname hermes --gname hermes plow \
    | docker cp - "$name:/var/lib" >/dev/null
}

docker rm -f "$name" >/dev/null 2>&1 || true
docker create --name "$name" --platform "$platform" --network "$net" "$image" >/dev/null
host_credentials http://stub:8080 cloud-token-one
docker start "$name" >/dev/null
await_gateway

promoted="$(docker exec "$name" stat -c '%a %U:%G' /var/lib/plow/credentials)"
printf 'promoted credentials: %s\n' "$promoted"
[[ "$promoted" == "600 root:root" ]] \
  || { echo "the bind mount was not promoted to a root:root 0600 file: $promoted" >&2; exit 1; }
mounted="$(docker exec "$name" stat -c '%a %u:%g' /var/lib/plow/credentials.host)"
[[ "$mounted" == "644 10000:10000" ]] \
  || { echo "the mount itself was changed: $mounted" >&2; exit 1; }
docker exec "$name" grep -qx 'PLOW_HOME_CHANNEL=cht_cloud_one' /var/lib/hermes/.env \
  || { echo "a promoted drop-in did not render the dotenv" >&2; exit 1; }
echo "bind-mounted drop-in: promoted to 600 root:root, mount untouched, gateway up"

# And a rotation through the mount: the same two lines rewritten out here, and
# a restart. Nothing shells in, and the promoted copy must not win over the
# newer mount it was made from.
host_credentials http://stub:8080 cloud-token-two
docker restart "$name" >/dev/null
await_gateway
docker exec "$name" grep -qx 'PLOW_AGENT_TOKEN=cloud-token-two' /var/lib/hermes/.env \
  || { echo "a rewritten mount did not replace the promoted credential" >&2; exit 1; }
docker exec "$name" grep -qx 'PLOW_HOME_CHANNEL=cht_cloud_two' /var/lib/hermes/.env \
  || { echo "the identity was not re-asked after a mount rotation" >&2; exit 1; }
echo "bind-mount rotation: new token promoted, identity re-asked"

docker rm -f "$name" "$stub" >/dev/null
docker network rm "$net" >/dev/null
stub=; net=

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

echo "ok: $image ($platform) builds, imports, boots under s6, keeps its hardening, and fails closed" >&2

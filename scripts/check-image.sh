#!/usr/bin/env bash
# Build the image and check that it comes up the way a tenant needs it to.
#
# usage: check-image.sh [image]
#        PLATFORM=linux/arm64 check-image.sh    # the other architecture
#
# Every check below is for a failure that builds clean and boots looking
# healthy -- the only kind worth a script this slow. They all run in one shape,
# because the image has one: a credential file, and an identity fetched from
# Plow with it. Each section says what it is for.
#
# linux/amd64 is the default because that is what a VM host unpacks; PLATFORM
# overrides it so the architecture a developer's Mac runs natively is checked
# by the same script.
set -euo pipefail

cd "$(dirname "$0")/.."

image="${1:-plow-hermes-agent:check}"
platform="${PLATFORM:-linux/amd64}"

docker buildx build --platform "$platform" \
  --build-arg "PLOW_REVISION=${PLOW_REVISION:-$(git rev-parse HEAD)}" \
  --provenance=false --sbom=false \
  --tag "$image" --load . >&2

# --- 1. the plugin imports --------------------------------------------------
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

# --- 2. the write guard follows the home ------------------------------------
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
  [[ -n "${vol:-}" ]] && docker volume rm "$vol" >/dev/null 2>&1
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
  # Removed before it is written: one case below leaves this fixture at 0400,
  # and truncating a read-only file fails even for its owner -- which would
  # hand the next case the PREVIOUS credential and let it assert against it.
  rm -f "$cloud_dir/plow/credentials"
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
# before it ever starts. `cloud_extra` adds `docker create` arguments for the
# next one only -- an environment to be ignored, a home volume to be kept --
# and is reset by every caller that sets it.
cloud_extra=()
cloud_container() {   # cloud_container <api base> <token> [extra line...]
  write_credentials "$@"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker create --name "$name" --platform "$platform" --network "$net" \
    ${cloud_extra[@]+"${cloud_extra[@]}"} "$image" >/dev/null
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

# What s6 hands every service: one file per name under container_environment.
# Read as `NAME=value` lines so the assertions below stay one grep each.
published() {
  docker exec "$name" sh -c '
    cd /run/s6/container_environment 2>/dev/null || exit 0
    for f in *; do [ -e "$f" ] && printf "%s=%s\n" "$f" "$(cat "$f")"; done' | sort
}

# --- 3. the drop-in is the whole of what the agent was told -----------------
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

# The published environment, in full: every name the contract says the image
# derives for itself, and none it was handed. Nothing is written to a file the
# next boot could read back, so this is the whole of what a service inherits.
rendered="$(published)"
printf '%s\n' "$rendered"
grep -qx 'PLOW_API_BASE=http://stub:8080'                                        <<<"$rendered" || { echo "the API base was not published" >&2; exit 1; }
grep -qx 'PLOW_AGENT_TOKEN=cloud-token-one'                                      <<<"$rendered" || { echo "the credential was not published" >&2; exit 1; }
grep -qx 'PLOW_HOME_CHANNEL=cht_cloud_one'                                       <<<"$rendered" || { echo "the home channel did not come from the identity endpoint" >&2; exit 1; }
grep -qx 'HERMES_CUSTOM_PLOW_API_KEY=cloud-token-one'                            <<<"$rendered" || { echo "the inference key alias was not derived from the credential" >&2; exit 1; }
grep -qx 'PLOW_MCP_URL=http://stub:8080/v1/relay/devices/dev_cloud_check/mcp'    <<<"$rendered" || { echo "the relay URL did not come from the identity endpoint" >&2; exit 1; }
grep -q  '^API_SERVER_KEY=..*'                                                   <<<"$rendered" || { echo "no API_SERVER_KEY was generated" >&2; exit 1; }
grep -q  '^TZ='                                                                  <<<"$rendered" && { echo "the image published a TZ, which belongs to a variant" >&2; exit 1; }

# The credential never lands in the agent's home. The runtime writes a loopback
# key of its own into that dotenv, which nothing here reads -- the gateway takes
# API_SERVER_KEY from its environment -- but a tenant's identity must not be
# there at all: it is a file uid 10000 can read, and it outlives the boot.
for secret in PLOW_AGENT_TOKEN PLOW_API_BASE PLOW_HOME_CHANNEL HERMES_CUSTOM_PLOW_API_KEY; do
  docker exec "$name" grep -q "^$secret=" /var/lib/hermes/.env 2>/dev/null \
    && { echo "$secret was written into the agent's own home" >&2; exit 1; }
done

[[ "$(mcp_state plow)" == true ]] \
  || { echo "a cloud-shaped boot did not enable the relay: $(mcp_state plow)" >&2; exit 1; }
echo "cloud boot: gateway up, identity fetched, relay enabled, .env 0640 root:hermes"


# --- 4. s6 is PID 1, and the gateway is up ----------------------------------

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

# --- 5. the protected files survive first boot, twice -----------------------
#
# `docker restart` is the second first boot: the same home, already bootstrapped,
# init running again over it. Cloud provisioning does the same thing within one
# boot, because the host's setup script calls first-boot.sh and so does init.
# The dotenv the VM host writes, written the way it writes it: root, 0600,
# owned by whoever ran the setup script. Init normalizes it, and it is in the
# snapshot because it is what a rendered provider block would be written into —
# an init that rewrote it on every boot would be invisible without this.
# The loopback key is generated per boot and published, never persisted. A key
# that survived a restart would be a long-lived secret at rest for no reason,
# and the contract downstream is "read it from the environment": asserting it
# CHANGES is what keeps anything from quietly starting to cache it.
key_first="$(published | sed -n 's/^API_SERVER_KEY=//p')"
[[ -n "$key_first" ]] || { echo "no API_SERVER_KEY was published" >&2; exit 1; }

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
key_second="$(published | sed -n 's/^API_SERVER_KEY=//p')"
[[ -n "$key_second" && "$key_second" != "$key_first" ]] \
  || { echo "the API_SERVER_KEY did not change across a restart: $key_first" >&2; exit 1; }
grep -q '^/run/service/hermes-gateway up=true$'     <<<"$first" || { echo "the gateway is not up" >&2; exit 1; }

docker restart "$name" >/dev/null
await_gateway
second="$(state)"

diff <(printf '%s\n' "$first") <(printf '%s\n' "$second") \
  || { echo "init is not idempotent: the second boot changed the listing above" >&2; exit 1; }
echo "second boot left config, ownership, modes and service state byte-identical"

# --- 6. an operator's root shell cannot lock the agent out ------------------
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

# --- 7. the credential is data, not a script --------------------------------
#
# `plow-init` reads this file as root, before anything drops privilege. A value
# that looks like a command substitution has to arrive as those characters --
# it is written by whoever provisions the box, and the whole file is two lines
# of somebody else's text.
drop_credentials http://stub:8080 'cloud-token-one$(touch /pwned)'
docker restart "$name" >/dev/null
docker wait "$name" >/dev/null
docker exec "$name" test -e /pwned 2>/dev/null \
  && { echo "a value in the credential was executed as a command" >&2; exit 1; }
# The token is not one the stub knows, so the boot ends in a refusal -- which
# is the correct outcome and not what is under test here. What is under test is
# that the characters reached the request rather than a shell.
# `docker wait` returns when the container stops, which is not the same moment
# its last stderr line is readable, so the log is polled rather than read once.
for _ in $(seq 1 60); do
  docker logs "$name" 2>&1 | grep -q 'Plow refused this credential' && break
  sleep 1
done
docker logs "$name" 2>&1 | grep -q 'Plow refused this credential' \
  || { echo "the hostile credential did not reach the identity request intact" >&2
       docker logs "$name" 2>&1 | tail -20 >&2; exit 1; }
echo "credential: a command substitution arrived as characters, not a command"

drop_credentials http://stub:8080 cloud-token-one
docker restart "$name" >/dev/null
await_gateway

# --- 8. a symlinked config.yaml is hostile ----------------------------------
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

# --- 9. and so are a directory and a FIFO -----------------------------------
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

# ...and nothing else about it differs from the seed. Compared by setting
# rather than by bytes: the runtime rewrites this file through its own YAML
# writer on the way up -- comments go, quoting changes, the version marker is
# migrated -- so a text comparison fails on a config that is semantically the
# seed, and fails every boot. Comments are not the contract; the keys and their
# values are, and anything changing among them means the file that came back is
# not the one the image ships. What is written on purpose -- the provider, the
# model and the relay flag -- is excluded here and asserted on its own above.
#
# Through the YAML parser, in a file rather than a heredoc: bash 3.2 -- what
# macOS still ships -- mis-parses a heredoc inside a command substitution, and
# `docker exec sh -c` is dash in this image, which has no process substitution.
# The previous spelling used both, so it was a syntax error whose exit status
# the trailing `|| true` swallowed: `drift` came back empty and the assertion
# passed on a probe that had never run. The sentinel is the other half of the
# fix -- an empty diff and a dead probe stop looking alike.
probe="$(docker exec --interactive "$name" /opt/hermes/.venv/bin/python - \
  < scripts/probe-config-drift.py)"
grep -qx DRIFT_PROBE_OK <<<"$probe" \
  || { echo "the config-drift probe did not run to completion:" >&2
       printf '%s\n' "$probe" >&2; exit 1; }
drift="$(grep '^DRIFT ' <<<"$probe" || true)"
[[ -z "$drift" ]] \
  || { echo "the restored config.yaml differs from the seed outside what is written on purpose:" >&2
       printf '%s\n' "$drift" >&2; exit 1; }
echo "restored config.yaml is the image's: plow_chat platform, plow provider, no drift from the seed"

# --- 10. a restored config keeps the relay the tenant was provisioned -------
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

docker rm -f "$name" >/dev/null 2>&1 || true

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

docker rm -f "$name" >/dev/null 2>&1 || true

# --- 11. rotation is a rewrite of that file and a restart -------------------
#
# The host never shells in. It replaces the two lines and restarts the VM, and
# the agent must come back holding the new credential -- in the gateway's own
# environment, not merely on disk -- with whatever else Plow now says about it.
# The persisted dotenv is the thing that would silently win here, which is why
# the *process* is asked rather than the file alone.
cloud_container http://stub:8080 cloud-token-one
docker start "$name" >/dev/null
await_gateway

key_before="$(published | sed -n 's/^API_SERVER_KEY=//p')"

drop_credentials http://stub:8080 cloud-token-two
docker restart "$name" >/dev/null
await_gateway

seen="$(docker exec --user 10000:10000 "$name" sh -c '
  pid=$(pgrep -f "hermes gateway run" | head -1)
  tr "\0" "\n" < "/proc/$pid/environ" | sed -n "s/^PLOW_AGENT_TOKEN=//p"')"
[[ "$seen" == cloud-token-two ]] \
  || { echo "the restarted gateway is still holding '$seen'" >&2; exit 1; }
rotated="$(published)"
grep -qx 'PLOW_AGENT_TOKEN=cloud-token-two'           <<<"$rotated" || { echo "the published environment still names the replaced token" >&2; exit 1; }
grep -qx 'HERMES_CUSTOM_PLOW_API_KEY=cloud-token-two' <<<"$rotated" || { echo "the inference key alias kept the replaced token" >&2; exit 1; }
grep -qx 'PLOW_HOME_CHANNEL=cht_cloud_two'            <<<"$rotated" || { echo "the home channel was not re-asked after the rotation" >&2; exit 1; }
key_after="$(published | sed -n 's/^API_SERVER_KEY=//p')"
[[ -n "$key_after" && "$key_before" != "$key_after" ]] \
  || { echo "the restart reused the previous server key" >&2; exit 1; }
echo "rotation: new token in the gateway's environment and published, server key regenerated"

# A tenant whose relay went away. The dotenv still carries the URL from when it
# had one, and the file fills only what nothing else set -- so nothing but an
# explicit removal keeps a dead relay from coming back enabled.
drop_credentials http://stub:8080 cloud-token-norelay
docker restart "$name" >/dev/null
await_gateway
published | grep -q '^PLOW_MCP_URL=' \
  && { echo "a withdrawn relay was published anyway" >&2; exit 1; }
[[ "$(mcp_state plow)" == false ]] \
  || { echo "a withdrawn relay is still enabled: $(mcp_state plow)" >&2; exit 1; }
echo "withdrawn relay: gone from the dotenv and switched off in the config"

# --- 12. every way of not being told who this agent is starts nothing -------
#
# The identity fetch is a dependency of every boot, and there is no fallback
# behind it: an agent that cannot be told who it is does not start, rather than
# start as whoever it was last time. A recorded identity belongs to the
# credential it was recorded under, and a home volume outlives its tenant, so
# reusing one is how a new tenant lands in the previous one's chat.
#
# The answer is changed without changing the credential -- these are the cases
# where the token in the file is exactly right and the answer is not.
logs_since() {   # logs_since <line count before the boot>
  docker logs "$name" 2>&1 | tail -n +"$(( $1 + 1 ))"
}
log_lines() { docker logs "$name" 2>&1 | wc -l | tr -d ' '; }
stub_control() { docker exec "$stub" sh -c "$1"; }

refused_identity() {   # refused_identity <label> <expected log line>
  local pre out code
  pre="$(log_lines)"
  docker restart "$name" >/dev/null
  docker wait "$name" >/dev/null
  out="$(logs_since "$pre")"
  grep -q "$2" <<<"$out" \
    || { echo "$1: expected '$2' in the log" >&2; printf '%s\n' "$out" >&2; exit 1; }
  grep -q 'hermes-gateway: starting' <<<"$out" \
    && { echo "$1: the gateway started anyway" >&2; exit 1; }
  code="$(docker inspect -f '{{.State.ExitCode}}' "$name")"
  [[ "$code" != 0 ]] || { echo "$1: PID 1 exited 0" >&2; exit 1; }
  echo "$1: gateway never started, PID 1 exited $code"
}

# Plow answering that this credential is not this agent's -- revoked, or an
# agent that is gone.
stub_control 'touch /revoked'
refused_identity "Plow says 404" 'Plow refused this credential'

# ...and an answer that is not an identity, which waiting cannot improve.
stub_control 'rm -f /revoked; touch /garbage'
refused_identity "Plow answers with no chat_uid" 'Plow refused this credential'

# A 5xx is retried, because a VM's network is not always up when its first
# service is -- and then refused, because retrying is not the same as
# surviving. The log carries both halves.
stub_control 'rm -f /garbage; touch /unavailable'
pre="$(log_lines)"
docker restart "$name" >/dev/null
docker wait "$name" >/dev/null
out="$(logs_since "$pre")"
grep -q 'attempt 1 to reach Plow failed, retrying' <<<"$out" \
  || { echo "a 503 was not retried" >&2; printf '%s\n' "$out" >&2; exit 1; }
grep -q 'gave up asking Plow who this agent is' <<<"$out" \
  || { echo "a 503 did not end in a refusal" >&2; printf '%s\n' "$out" >&2; exit 1; }
grep -q 'hermes-gateway: starting' <<<"$out" \
  && { echo "the gateway started without an identity" >&2; exit 1; }
echo "Plow says 503: retried, then gateway never started"
stub_control 'rm -f /unavailable'

# No answer at all, on a home that has run before and holds a full dotenv for
# this very token. That is the case a fallback would have taken; it starts
# nothing too.
docker stop "$stub" >/dev/null
refused_identity "Plow unreachable, same credential, identity on disk" \
  'gave up asking Plow who this agent is'
docker start "$stub" >/dev/null

docker rm -f "$name" >/dev/null 2>&1 || true

# --- 13. a credential drop-in that cannot be used starts nothing ------------
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
# The names it could reach for are not merely unread: PATH would send every
# later command in a root process somewhere of the file's choosing, and
# LD_PRELOAD would load a library into the gateway.
for extra_key in PLOW_HOME_CHANNEL PATH LD_PRELOAD; do
  cloud_container http://stub:8080 cloud-token-one "$extra_key=whatever"
  refused "a credential naming $extra_key" 'is not the two lines this image reads'
done

# A drop-in anyone but root can write is a drop-in anyone but root can point at
# their own API base, and the agent's bearer token goes with it; one anyone can
# READ is the credential itself handed over. Neither is a bit-mask question --
# 0644 and 0620 both pass a rule that only forbids the write bits -- so the
# accepted modes are enumerated, and every neighbour of them is checked.
for bad_mode in 0666 0644 0620 0602; do
  cred_mode="$bad_mode"
  cloud_container http://stub:8080 cloud-token-one
  refused "a drop-in at $bad_mode" 'expected root:root at 600 or 400'
done
cred_mode=0600

# ...and the one mode besides 0600 that is not a loosening: the same file, with
# root's own write taken away. A host that hardens further must not be refused.
cred_mode=0400
cloud_container http://stub:8080 cloud-token-one
docker start "$name" >/dev/null
await_gateway
published | grep -qx 'PLOW_HOME_CHANNEL=cht_cloud_one' \
  || { echo "a 0400 drop-in did not publish the identity" >&2; exit 1; }
echo "drop-in at 0400: accepted, gateway up"
cred_mode=0600

# --- 14. a bind-mounted drop-in is promoted rather than refused -------------
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
published | grep -qx 'PLOW_HOME_CHANNEL=cht_cloud_one' \
  || { echo "a promoted drop-in did not publish the identity" >&2; exit 1; }
echo "bind-mounted drop-in: promoted to 600 root:root, mount untouched, gateway up"

# And a rotation through the mount: the same two lines rewritten out here, and
# a restart. Nothing shells in, and the promoted copy must not win over the
# newer mount it was made from.
host_credentials http://stub:8080 cloud-token-two
docker restart "$name" >/dev/null
await_gateway
published | grep -qx 'PLOW_AGENT_TOKEN=cloud-token-two' \
  || { echo "a rewritten mount did not replace the promoted credential" >&2; exit 1; }
published | grep -qx 'PLOW_HOME_CHANNEL=cht_cloud_two' \
  || { echo "the identity was not re-asked after a mount rotation" >&2; exit 1; }
echo "bind-mount rotation: new token promoted, identity re-asked"

# --- 15. the container environment is not a credential source ---------------
#
# The image renders every one of these names for itself, from the drop-in and
# from Plow's answer about the tenant holding it. `with-contenv` hands both
# init and the gateway whatever `docker run -e` set, and the dotenv rule is
# "a name already set keeps its value" -- so without the drop, a stale
# `-e PLOW_AGENT_TOKEN=...` outranks the file a host just rewrote, and a
# rotation silently does not take.
#
# The API base is the sharp end of it: inherited, it decides where the agent
# sends its bearer token. Pointed at a host that does not resolve, an image
# that honoured it would spend its retries and never come up -- so
# `await_gateway` is itself the assertion here.
cloud_extra=(--env PLOW_API_BASE=https://api.invalid
             --env PLOW_AGENT_TOKEN=inherited-not-the-credential
             --env PLOW_HOME_CHANNEL=cht_inherited
             --env HERMES_CUSTOM_PLOW_API_KEY=inherited-not-the-credential
             --env PLOW_MCP_URL=https://api.invalid/v1/relay/devices/dev_inherited/mcp)
cloud_container http://stub:8080 cloud-token-one
docker start "$name" >/dev/null
cloud_extra=()
await_gateway

rendered="$(published)"
for want in 'PLOW_API_BASE=http://stub:8080' 'PLOW_AGENT_TOKEN=cloud-token-one' \
            'PLOW_HOME_CHANNEL=cht_cloud_one' 'HERMES_CUSTOM_PLOW_API_KEY=cloud-token-one' \
            'PLOW_MCP_URL=http://stub:8080/v1/relay/devices/dev_cloud_check/mcp'; do
  grep -qx "$want" <<<"$rendered" \
    || { echo "an inherited environment reached the published values: wanted $want" >&2
         printf '%s\n' "$rendered" >&2; exit 1; }
done
# ...and the gateway is its own reader of that file, with its own inherited
# environment, so it is asked separately rather than assumed to agree.
seen="$(docker exec --user 10000:10000 "$name" sh -c '
  pid=$(pgrep -f "hermes gateway run" | head -1)
  tr "\0" "\n" < "/proc/$pid/environ" | sed -n "s/^PLOW_AGENT_TOKEN=//p"')"
[[ "$seen" == cloud-token-one ]] \
  || { echo "the gateway came up holding the inherited token: '$seen'" >&2; exit 1; }
echo "inherited PLOW_* environment: ignored by init and by the gateway, drop-in won"

docker rm -f "$name" >/dev/null 2>&1 || true

# --- 16. ...and provider and model are the two names it still owns ----------
#
# Where inference goes is a decision about a container, not a fact about the
# agent, so these two keep environment precedence when everything else lost it.
# The home is a volume across all three boots because that is the case that
# matters: a switch must not reset what the agent has accumulated -- a provider
# login above all, which lives in the home and is what makes switching back
# cheap.
vol="check-image-vol-$$"
docker volume create "$vol" >/dev/null
provider_boot() {   # provider_boot [--env ...]
  cloud_extra=(--volume "$vol:/var/lib/hermes" "$@")
  cloud_container http://stub:8080 cloud-token-one
  docker start "$name" >/dev/null
  cloud_extra=()
  await_gateway
}
# One config section, parsed and flattened, for comparing across a boot. Text
# would not do: the runtime rewrites this file through its own writer, so the
# bytes differ where the settings do not.
config_section() {   # config_section <top-level key>...
  local out
  out="$(docker exec --interactive "$name" /opt/hermes/.venv/bin/python - "$@" \
    < scripts/probe-config-section.py)"
  grep -qx SECTION_PROBE_OK <<<"$out" \
    || { echo "the config-section probe did not run to completion:" >&2
         printf '%s\n' "$out" >&2; exit 1; }
  grep -v '^SECTION_PROBE_OK$' <<<"$out"
}

# What the agent KEEPS, by path, owner and mode -- a provider login above all.
# `config.yaml` and `.env` are excluded because a provider switch is defined as
# rewriting them, and `state/` because it holds the gateway's transient runtime
# sockets, named after a pid that changes every boot.
home_inventory() {
  docker exec "$name" sh -c '
    find /var/lib/hermes -maxdepth 3 \
      -path /var/lib/hermes/state -prune -o \
      ! -name .env ! -name config.yaml \
      -exec stat -c "%n %U:%G %a" {} + | sort'
}

model_key() {   # model_key <provider|default>
  docker exec "$name" awk -v want="  $1: " '
    /^[^ ]/ { in_model = ($0 == "model:") }
    in_model && index($0, want) == 1 { print substr($0, length(want) + 1); exit }
  ' /var/lib/hermes/config.yaml
}

provider_boot
[[ "$(model_key provider)" == plow ]] \
  || { echo "the default provider is not plow: $(model_key provider)" >&2; exit 1; }
seeded_model="$(model_key default)"
[[ -n "$seeded_model" ]] || { echo "no default model was written" >&2; exit 1; }
published | grep -qx 'HERMES_CUSTOM_PLOW_API_KEY=cloud-token-one' \
  || { echo "the plow inference key was not derived from the credential" >&2; exit 1; }
# Stands in for a provider login: something the agent owns, in its own home.
docker exec --user 10000:10000 "$name" sh -c 'printf "a login\n" > /var/lib/hermes/.provider-login'
# Captured AFTER that write, so the login is part of what must survive.
providers_before="$(config_section providers)"
home_before="$(home_inventory)"
# Its contents, not just its presence: a login rewritten in place passes an
# inventory that compares path, owner and mode. Compared after the switch BACK,
# so it covers the whole round trip rather than one leg of it.
login_before="$(docker exec "$name" sha256sum /var/lib/hermes/.provider-login)"
echo "provider default: plow, model $seeded_model, inference key derived from the credential"

provider_boot --env HERMES_PROVIDER=anthropic --env HERMES_MODEL=claude-sonnet-4-5
[[ "$(model_key provider)" == anthropic ]] \
  || { echo "the provider switch did not take: $(model_key provider)" >&2; exit 1; }
[[ "$(model_key default)" == claude-sonnet-4-5 ]] \
  || { echo "the model switch did not take: $(model_key default)" >&2; exit 1; }
# The provider blocks are left ENTIRELY alone -- not merely still present --
# which is what makes switching back one line rather than a restore. Compared
# setting by setting, because "the plow key still exists" would pass on a block
# whose base_url or key_env had been rewritten underneath it.
printf '%s\n' "$providers_before" > "$cloud_dir/providers.before"
config_section providers > "$cloud_dir/providers.after"
diff "$cloud_dir/providers.before" "$cloud_dir/providers.after" \
  || { echo "the provider blocks changed across the switch" >&2; exit 1; }
# ...and nothing the agent had is gone. Not equality: a running gateway writes
# caches of its own, and a check that failed on those would be reporting normal
# operation. What must not happen is a LOSS -- a provider login that did not
# survive the switch is how switching back stops being cheap.
printf '%s\n' "$home_before" > "$cloud_dir/home.before"
home_inventory > "$cloud_dir/home.after"
lost="$(comm -23 "$cloud_dir/home.before" "$cloud_dir/home.after")"
[[ -z "$lost" ]] \
  || { echo "the switch lost files the agent owned:" >&2; printf '%s\n' "$lost" >&2; exit 1; }
[[ "$(mcp_state plow)" == true ]] \
  || { echo "the relay was switched off by a provider change: $(mcp_state plow)" >&2; exit 1; }
echo "provider switch: anthropic/claude-sonnet-4-5; providers.* and the agent's home byte-identical, relay kept"

provider_boot --env HERMES_PROVIDER=plow --env "HERMES_MODEL=$seeded_model"
[[ "$(model_key provider)" == plow && "$(model_key default)" == "$seeded_model" ]] \
  || { echo "switching back left $(model_key provider)/$(model_key default)" >&2; exit 1; }
[[ "$(docker exec "$name" sha256sum /var/lib/hermes/.provider-login)" == "$login_before" ]] \
  || { echo "the round trip changed the provider login's contents" >&2; exit 1; }
echo "switch back: plow/$seeded_model, provider login byte-identical"

docker rm -f "$name" >/dev/null 2>&1 || true
docker volume rm "$vol" >/dev/null
vol=

docker rm -f "$name" "$stub" >/dev/null
docker network rm "$net" >/dev/null
stub=; net=

# --- 17. a failed init starts nothing ---------------------------------------
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

docker rm -f "$name" >/dev/null 2>&1 || true

# --- 18. a boot with no credential starts nothing ---------------------------
#
# The gateway comes up without one: it serves its loopback API and runs the
# cron scheduler with no adapter attached. Every probe passes and no owner can
# reach the agent, which is the failure this refuses to ship. The credential
# file is the only source there is, so its absence is the whole of the case.
docker run --name "$name" --platform "$platform" "$image" >/dev/null 2>&1 || true

out="$(docker logs "$name" 2>&1)"
grep -q 'no credential at /var/lib/plow/credentials' <<<"$out" || { echo "a credential-free boot was not refused" >&2; printf '%s\n' "$out" >&2; exit 1; }
grep -q 'hermes-gateway: starting' <<<"$out" && { echo "the gateway started with no credential" >&2; exit 1; }
echo "no credential: gateway never started, PID 1 exited $(docker inspect -f '{{.State.ExitCode}}' "$name")"

# --- 19. the bundled skills are readable by the gateway user ----------------
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

echo "ok: $image ($platform) builds, imports, boots under s6, keeps its hardening, and fails closed" >&2

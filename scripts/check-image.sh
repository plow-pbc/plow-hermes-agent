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
#   5. a failed init starts nothing — an agent whose credential injection died
#                                    must not come up half-configured.
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
    "plow_chat", "/var/lib/hermes/plugins/plow_chat/__init__.py"
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
# The same shape a tenant gets: the values the VM host's setup script writes
# into the dotenv, here as container environment. API_SERVER_KEY is the one the
# gateway needs to open its loopback API server at all — without it there is no
# listener to find, on any path.
name="check-image-$$"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "${hookdir:-}"; }
trap cleanup EXIT

docker run -d --name "$name" --platform "$platform" \
  --env PLOW_API_BASE=https://api.invalid \
  --env PLOW_HOME_CHANNEL=cht_boot_check \
  --env PLOW_AGENT_TOKEN=boot-check-not-a-credential \
  --env HERMES_CUSTOM_PLOW_API_KEY=boot-check-not-a-credential \
  --env API_SERVER_KEY=0123456789abcdef0123456789abcdef \
  "$image" >/dev/null

# The API's own readiness contract, verbatim: a loopback listener on 8642,
# 0x21C2 in /proc/net/tcp. Emulating a foreign architecture is slow, so the
# budget is generous rather than tuned.
await_gateway() {
  local i
  for i in $(seq 1 150); do
    if docker exec "$name" sh -c "grep -q '0100007F:21C2 ' /proc/net/tcp" 2>/dev/null; then
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
state() {
  docker exec "$name" sh -c '
    stat -c "%a %U:%G %n" /var/lib/hermes /var/lib/hermes/skills \
                          /var/lib/hermes/SOUL.md /var/lib/hermes/config.yaml
    sha256sum /var/lib/hermes/config.yaml
    /command/s6-rc -a list | sort | tr "\n" " "; echo'
}

first="$(state)"
printf '%s\n' "$first"

# The hardening, asserted rather than merely printed: root must own the identity
# and the home, or the sticky bit protects nothing.
grep -q '^3770 root:hermes /var/lib/hermes$'        <<<"$first" || { echo "home lost 3770 root:hermes" >&2; exit 1; }
grep -q '^3770 root:hermes /var/lib/hermes/skills$' <<<"$first" || { echo "skills/ lost 3770 root:hermes" >&2; exit 1; }
grep -q ' root:root /var/lib/hermes/SOUL.md$'       <<<"$first" || { echo "SOUL.md is not root-owned" >&2; exit 1; }
grep -q ' hermes:hermes /var/lib/hermes/config.yaml$' <<<"$first" || { echo "config.yaml is not hermes-owned" >&2; exit 1; }

docker restart "$name" >/dev/null
await_gateway
second="$(state)"

diff <(printf '%s\n' "$first") <(printf '%s\n' "$second") \
  || { echo "init is not idempotent: the second boot changed the listing above" >&2; exit 1; }
echo "second boot left config, ownership, modes and service state byte-identical"

docker rm -f "$name" >/dev/null

# --- 5. a failed init starts nothing ---------------------------------------
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

echo "ok: $image ($platform) builds, imports, boots under s6, keeps its hardening, and fails closed" >&2

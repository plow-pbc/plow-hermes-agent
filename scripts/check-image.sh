#!/usr/bin/env bash
# Build the image and check that the plugin inside it imports.
#
# usage: check-image.sh [image]
#
# The import is the whole check because it is the whole failure mode: the Hermes
# runtime is pinned by digest in the Dockerfile while the plugin moves in its
# own repository, so a plugin reaching for a newer gateway API yields an image
# that builds and boots and then loads one platform instead of two —
# healthy-looking and deaf.
#
# linux/amd64 is named rather than inferred: the VM host unpacks this image onto
# an amd64 VM, so an arm64 developer machine would build something that cannot
# boot there.
set -euo pipefail

cd "$(dirname "$0")/.."

image="${1:-plow-hermes-agent:check}"

docker buildx build --platform linux/amd64 \
  --build-arg "PLOW_REVISION=${PLOW_REVISION:-$(git rev-parse HEAD)}" \
  --provenance=false --sbom=false \
  --tag "$image" --load . >&2

# As the gateway's own user, against the image's own copy of the plugin. The env
# values are placeholders: a plugin that needs a real credential to be
# importable is itself the bug. `--interactive` is load-bearing — without it
# docker attaches no stdin, `python -` reads an empty program and exits 0, so
# the sentinel below is asserted rather than the exit status trusted.
out="$(docker run --rm --interactive --platform linux/amd64 --user 10000:10000 \
  --env PLOW_API_BASE=https://api.invalid \
  --env PLOW_HOME_CHANNEL=cht_import_check \
  --env PLOW_AGENT_TOKEN=import-check-not-a-credential \
  --env HERMES_HOME=/var/lib/hermes \
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

# The gateway unit overrides environment the upstream image set for a different
# home, and a stale override is invisible: the gateway boots, serves, and only
# denies writes once the agent reaches for a file. The probe is a file rather
# than a heredoc because bash 3.2 — what macOS still ships — mis-parses a
# heredoc inside a command substitution.
out="$(docker run --rm --interactive --platform linux/amd64 --user 10000:10000 \
  --entrypoint /opt/hermes/.venv/bin/python "$image" - < scripts/probe-write-safe-root.py)"

printf '%s\n' "$out"
[[ "$out" == *WRITE_SAFE_ROOT_OK* ]] \
  || { echo "the write-safe-root probe did not run" >&2; exit 1; }
echo "ok: $image builds, plow_chat imports, and the agent may write its own home" >&2

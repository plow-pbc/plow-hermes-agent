#!/usr/bin/env bash
# Fail the build when the staged plow_chat plugin cannot import on the pinned
# runtime.
#
# The plugin is staged from its own repository at build time, while the Hermes
# runtime it imports from is pinned by digest in the Dockerfile. Nothing couples
# the two, so a plugin commit that reaches for a newer gateway API produces an
# image that builds, pushes and boots — and then loads one platform instead of
# two. The gateway logs a single WARNING, the readiness probe still passes
# because it only exercises the local API server, and the agent is live with no
# phone line: it answers nothing and says nothing about why.
#
# An import is the whole check because an import is the whole failure. The
# plugin's own suite injects a fake `gateway` package into `sys.modules`, so it
# is green against a runtime that does not have the module; only a real
# interpreter inside a real image can tell you otherwise.
#
# Probes a locally built image rather than a pushed one: the point is to refuse
# to publish, and by the time a digest exists the registry already holds the
# broken image. scripts/check-image.sh builds and then calls this.
#
# usage: check-plugin-import.sh [image]
set -euo pipefail

cd "$(dirname "$0")/.."

image="${1:-${IMAGE:-plow-hermes-agent:check}}"

# As the gateway's own user, against the image's own copy of the plugin: this is
# the same file, interpreter and import path that `hermes gateway run` uses. The
# env values are placeholders — module import reads them, and a plugin that
# needs a real credential to be importable is itself the bug.
#
# `--interactive`, load-bearing: the probe is fed to `python -` on stdin, and
# without it docker attaches no stdin, python reads an empty program and exits
# 0. That is a check that passes on a plugin it never imported — the exact
# failure this script exists to prevent, so the sentinel below is asserted
# rather than trusted.
probe_output="$(docker run --rm --interactive --platform linux/amd64 --user 10000:10000 \
  --env PLOW_API_BASE=https://api.invalid \
  --env PLOW_HOME_CHANNEL=cht_import_check \
  --env PLOW_AGENT_TOKEN=import-check-not-a-credential \
  --env HERMES_HOME=/var/lib/hermes \
  --entrypoint /opt/hermes/.venv/bin/python \
  "$image" - <<'PY'
import importlib.util
import sys

PLUGIN = "/var/lib/hermes/plugins/plow_chat/__init__.py"
spec = importlib.util.spec_from_file_location("plow_chat", PLUGIN)
assert spec and spec.loader, f"no import spec for {PLUGIN}"
module = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(module)
except Exception as exc:
    print(f"plow_chat plugin does not import on this runtime: {type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)
print("PLUGIN_IMPORT_OK")
PY
)"

printf '%s\n' "$probe_output"
[[ "$probe_output" == *PLUGIN_IMPORT_OK* ]] \
  || { echo "the import probe did not run — no PLUGIN_IMPORT_OK from the container" >&2; exit 1; }
echo "plow_chat plugin imports on the pinned runtime" >&2

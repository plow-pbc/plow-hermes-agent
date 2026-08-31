#!/usr/bin/env bash
# Build the image from the already-staged plugin.
#
# usage: build-image.sh [--push] <tag> [<tag>...]
#
# The plugin label comes from scripts/plugin-pin.sh — the same single source the
# staging script read — so the commit in the image and the commit named by the
# label cannot drift apart.
#
# linux/amd64 is named rather than inferred: the VM host unpacks this image onto
# an amd64 VM, and an arm64 developer machine would otherwise build something
# that cannot boot there.
set -euo pipefail

cd "$(dirname "$0")/.."

push=false
if [[ "${1:-}" == "--push" ]]; then push=true; shift; fi
[[ $# -ge 1 ]] || { echo "usage: $0 [--push] <tag> [<tag>...]" >&2; exit 2; }

[[ -f plugins/plow_chat/__init__.py && -f plugins/plow_chat/plugin.yaml ]] \
  || { echo "no staged plugin at plugins/plow_chat — run scripts/stage-plow-chat-plugin.sh first" >&2; exit 1; }

plugin_sha="$(scripts/plugin-pin.sh)"

revision="${PLOW_REVISION:-$(git rev-parse HEAD)}"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] \
  || { echo "PLOW_REVISION is not a full 40-character commit SHA: '$revision'" >&2; exit 1; }

args=(--platform linux/amd64
      --build-arg "PLOW_REVISION=$revision"
      --build-arg "PLOW_CHAT_PLUGIN_SHA=$plugin_sha"
      --provenance=false --sbom=false)
for tag in "$@"; do args+=(--tag "$tag"); done
# --load so the gates can run the image they just built; --push replaces it
# because buildx will not do both in one invocation.
if $push; then args+=(--push); else args+=(--load); fi

docker buildx build "${args[@]}" . >&2
printf '%s\n' "$@"

#!/usr/bin/env bash
# Stage the plow_chat plugin into the build context at the pinned commit.
#
# The plugin's canonical home is plow-pbc/hermes-plow-chat. The commit that
# ships in the image is the one in `plow-chat-plugin.ref` — a full SHA, checked
# in, moved by a reviewed pull request. There is no override: a branch name
# cannot reach this script, and neither can a SHA that is not the pinned one.
#
# A floating `main` once shipped an agent that loaded one platform instead of
# two: it came up healthy, answered nothing, and nothing in the built image
# recorded which plugin commit it had picked up.
#
# No credential is required or used. hermes-plow-chat is public, so this runs
# unauthenticated — which is what lets a fork's pull request build the image in
# a job that holds no secrets. GH_TOKEN, when the environment happens to have
# one, only buys a higher API rate limit.
set -euo pipefail

cd "$(dirname "$0")/.."

sha="$(scripts/plugin-pin.sh)"

auth=()
if [[ -n "${GH_TOKEN:-}" ]]; then
  auth=(--header "Authorization: Bearer ${GH_TOKEN}")
fi

api() {
  # `${auth[@]+...}`: under `set -u`, bash 3.2 (still what macOS ships) treats
  # an empty array's `[@]` as unset and aborts. The unauthenticated path is the
  # normal one here, so it is the one that must not be fragile.
  curl --fail-with-body --silent --show-error --location --retry 3 --retry-delay 2 \
    --header "Accept: $1" ${auth[@]+"${auth[@]}"} "$2"
}

# Resolve it against the canonical repository anyway: this proves the commit
# exists there and catches a SHA pinned from a fork or an abandoned branch.
resolved="$(api application/vnd.github+json \
  "https://api.github.com/repos/plow-pbc/hermes-plow-chat/commits/$sha" \
  | sed -n 's/^  "sha": "\([0-9a-f]\{40\}\)",$/\1/p' | head -n 1)"
[[ "$resolved" == "$sha" ]] \
  || { echo "pinned $sha did not resolve in plow-pbc/hermes-plow-chat (got '${resolved:-nothing}')" >&2; exit 1; }

stage="plugins/plow_chat"
tarball="$(mktemp)"
trap 'rm -f "$tarball"' EXIT

api application/vnd.github+json \
  "https://api.github.com/repos/plow-pbc/hermes-plow-chat/tarball/$sha" > "$tarball"
rm -rf "$stage"
mkdir -p "$stage"
# The archive's top directory carries the SHA; name it, so the member path is
# literal. GNU tar treats a bare glob as literal (BSD tar does not), which left
# every Linux build host staging nothing.
top="$(tar -tzf "$tarball" | cut -d/ -f1 | uniq)"   # one dir; no early close under pipefail
tar -xzf "$tarball" -C "$stage" --strip-components=2 "$top/plow-chat-platform"

grep -q "^name: plow-chat-platform" "$stage/plugin.yaml" \
  || { echo "staged tree is not the plugin (no plugin.yaml name)" >&2; exit 1; }
[[ -f "$stage/__init__.py" ]] \
  || { echo "staged tree has no __init__.py" >&2; exit 1; }

echo "staged plow_chat at $sha" >&2
printf '%s\n' "$sha"

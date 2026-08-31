#!/usr/bin/env bash
# Build the image and run every pre-publish gate against it.
#
# One entry point so a local run and CI gate the same way, and so a gate added
# here is a gate CI picks up without a workflow edit.
set -euo pipefail

cd "$(dirname "$0")/.."

image="${1:-${IMAGE:-plow-hermes-agent:check}}"

scripts/build-image.sh "$image" >/dev/null
scripts/check-plugin-import.sh "$image"
scripts/check-boot-contract.sh "$image"

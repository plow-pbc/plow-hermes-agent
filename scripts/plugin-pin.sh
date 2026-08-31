#!/usr/bin/env bash
# Print the pinned plow_chat commit, or fail.
#
# The one place the pin is read. Staging and the image label both call this, so
# the plugin in the image and the plugin named by the label cannot disagree:
# there is no override to set on one and forget on the other. A developer who
# wants a different plugin commit edits plow-chat-plugin.ref.
#
# The whole file must be exactly 40 lowercase hex characters and one trailing
# newline — 41 bytes, nothing else. Checking the parsed SHA is not enough: a
# line-oriented read accepts a good first line and ignores whatever follows it,
# so `<sha>\ntrailing junk` would pass while the file on disk says something
# other than what was validated. Comparing the file against a reconstruction of
# the SHA it parsed to closes that: what was checked is what is there.
set -euo pipefail

cd "$(dirname "$0")/.."
pin_file="plow-chat-plugin.ref"

[[ -f "$pin_file" ]] || { echo "$pin_file is missing" >&2; exit 1; }

bytes="$(wc -c < "$pin_file" | tr -d ' ')"
[[ "$bytes" == "41" ]] \
  || { echo "$pin_file must be 41 bytes (40 hex + newline), found $bytes" >&2; exit 1; }

pin="$(head -c 40 "$pin_file")"
[[ "$pin" =~ ^[0-9a-f]{40}$ ]] \
  || { echo "$pin_file is not a full 40-character lowercase commit SHA: '$pin'" >&2; exit 1; }

# The file is exactly what parsing it produced — no trailing byte survived.
printf '%s\n' "$pin" | cmp -s - "$pin_file" \
  || { echo "$pin_file holds bytes beyond the commit SHA and its newline" >&2; exit 1; }

printf '%s\n' "$pin"

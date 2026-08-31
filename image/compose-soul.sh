#!/bin/sh
# Compose /var/lib/hermes/soul.d/*.md into SOUL.md. Run at build time, in the
# layer that added the last part: a variant COPYs its persona into soul.d and
# then runs this, so the composed identity reflects whatever that image carries.
#
# Always overwrites. The caller decides what the identity is by deciding whether
# to call this at all — an image that ships its own SOUL.md and never calls this
# has opted out.
set -eu

# The numeric prefixes only order the parts if collation is fixed: the point of
# naming them 00-, 50- is that base identity precedes the variant's persona,
# and a build host's locale must not be able to reorder that.
LC_ALL=C
export LC_ALL

home=/var/lib/hermes
soul=$home/SOUL.md
tmp=$soul.compose.$$

: > "$tmp"
for part in "$home"/soul.d/*.md; do
  # An empty soul.d leaves the pattern unexpanded; -f rejects it.
  [ -f "$part" ] || continue
  # `if`, not `&&`: under `set -e` a false `[ -s ]` is the whole AND-OR list's
  # status, and the very first part would end the build.
  if [ -s "$tmp" ]; then printf '\n' >> "$tmp"; fi
  cat "$part" >> "$tmp"
done

if [ ! -s "$tmp" ]; then
  rm -f "$tmp"
  echo "no soul parts in $home/soul.d — nothing to compose" >&2
  exit 1
fi

chown 10000:10000 "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$soul"

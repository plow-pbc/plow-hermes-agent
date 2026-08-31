#!/bin/sh
# Composed at first boot, not at build time: a downstream image adds its own
# soul.d parts in its own layer, and the assembled SOUL.md has to reflect
# whatever the final image ended up carrying.
#
# Runs from the provisioning setup script (exe-setup.service), which invokes
# /usr/local/lib/plow/first-boot.sh when it exists and is executable. Idempotent
# by construction: an existing SOUL.md is never rewritten, so a re-run — or a
# reboot that replays setup — is a no-op.
set -eu

# The numeric prefixes only order the parts if collation is fixed: the point of
# naming them 00-, 50- is that base identity precedes the variant's persona,
# and a host locale must not be able to reorder that.
LC_ALL=C
export LC_ALL

home=/var/lib/hermes
soul=$home/SOUL.md
soul_d=$home/soul.d

# A literal SOUL.md wins. An image that ships one has opted out of composition
# and means that exact file to be the identity; overwriting it here would make
# the opt-out unreachable.
if [ ! -e "$soul" ] && [ -d "$soul_d" ]; then
  tmp=$soul.compose.$$
  : > "$tmp"
  for part in "$soul_d"/*.md; do
    # An empty soul.d leaves the pattern unexpanded; -f rejects it.
    [ -f "$part" ] || continue
    # `if`, not `&&`: under `set -e` a false `[ -s ]` is the whole AND-OR
    # list's status, and the very first part would end first boot.
    if [ -s "$tmp" ]; then printf '\n' >> "$tmp"; fi
    cat "$part" >> "$tmp"
  done
  if [ -s "$tmp" ]; then
    chown 10000:10000 "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$soul"
  else
    rm -f "$tmp"
  fi
fi

# Downstream images extend first boot by dropping a script here rather than by
# replacing this file — a replacement would take the soul composition with it.
#
# A hook's failure is first boot's failure. exe-setup.service is a oneshot and
# hermes-gateway.service `Requires=` it, so a non-zero exit here keeps the
# gateway from ever starting. That is the point: an agent whose setup half ran
# comes up looking healthy — the readiness probe only exercises the local API
# server — and is wrong in a way nobody sees. Better a VM that visibly never
# came up than one that answers with half its configuration.
if [ -d /usr/local/lib/plow/first-boot.d ]; then
  for hook in /usr/local/lib/plow/first-boot.d/*.sh; do
    # An empty first-boot.d leaves the pattern unexpanded; -x rejects it.
    [ -x "$hook" ] || continue
    "$hook" || {
      status=$?
      echo "first-boot hook failed: $hook (exit $status)" >&2
      exit "$status"
    }
  done
fi

exit 0

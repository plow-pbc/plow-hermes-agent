#!/usr/bin/env bash
# Prove the image's first-boot contract, inside the image, before it is pushed.
#
# usage: check-boot-contract.sh [image]
#
# Everything a downstream variant repository is told it can rely on is asserted
# here: the soul parts compose in prefix order, a literal SOUL.md opts out,
# a second run changes nothing, the composed file belongs to the gateway's uid
# and nobody else can read it, and a first-boot.d hook that fails takes first
# boot down with it.
#
# These are cheap to check and expensive to get wrong. Composition runs once, on
# a VM nobody is watching; a silent misorder ships an agent wearing the wrong
# identity, and a swallowed hook failure ships one that is half configured and
# looks healthy.
#
# Every case asserts a sentinel rather than trusting the exit status, for the
# same reason the plugin gate does: a probe that never ran must not read as a
# probe that passed.
set -euo pipefail

cd "$(dirname "$0")/.."

image="${1:-${IMAGE:-plow-hermes-agent:check}}"

failures=0

# Each case runs as root in a throwaway container off the image under test, fed
# on stdin — `--interactive` is load-bearing, exactly as in the plugin gate:
# without it `sh -s` reads an empty program and exits 0.
run_case() {
  local name="$1" expected_exit="$2" sentinel="$3" output status
  status=0
  output="$(docker run --rm --interactive --platform linux/amd64 --user 0:0 \
    --entrypoint /bin/sh "$image" -s 2>&1)" || status=$?

  if [[ "$status" != "$expected_exit" ]]; then
    printf 'FAIL %s: exit %s, expected %s\n%s\n' "$name" "$status" "$expected_exit" "$output" >&2
    failures=$((failures + 1))
    return
  fi
  if [[ "$output" != *"$sentinel"* ]]; then
    printf 'FAIL %s: no %s in the container output — the case did not run\n%s\n' \
      "$name" "$sentinel" "$output" >&2
    failures=$((failures + 1))
    return
  fi
  printf 'ok   %s\n' "$name"
}

# --- soul.d composes in prefix order, with the right owner and mode ----------
run_case "soul.d composes in prefix order" 0 CONTRACT_ORDER_OK <<'CASE'
set -eu
printf '# Variant\n\nvariant body\n' > /var/lib/hermes/soul.d/50-persona.md
printf '# Late\n' > /var/lib/hermes/soul.d/70-late.md

/usr/local/lib/plow/first-boot.sh

# The base part must lead, then 50, then 70 — the whole reason for the prefixes.
awk '
  /^# Plow assistant$/ { base = NR }
  /^# Variant$/        { mid  = NR }
  /^# Late$/           { last = NR }
  END { exit !(base && mid && last && base < mid && mid < last) }
' /var/lib/hermes/SOUL.md

# Parts are separated, not run together: the line before each later heading is blank.
awk 'prev != "" && /^# (Variant|Late)$/ { exit 1 } { prev = $0 }' /var/lib/hermes/SOUL.md

# The gateway runs as uid 10000 and nothing else on the box may read the identity.
[ "$(stat -c '%u:%g %a' /var/lib/hermes/SOUL.md)" = "10000:10000 600" ]

echo CONTRACT_ORDER_OK
CASE

# --- a literal SOUL.md wins --------------------------------------------------
run_case "literal SOUL.md opts out of composition" 0 CONTRACT_OPTOUT_OK <<'CASE'
set -eu
printf '# Variant\n' > /var/lib/hermes/soul.d/50-persona.md
printf 'LITERAL IDENTITY\n' > /var/lib/hermes/SOUL.md

/usr/local/lib/plow/first-boot.sh

[ "$(cat /var/lib/hermes/SOUL.md)" = "LITERAL IDENTITY" ]
echo CONTRACT_OPTOUT_OK
CASE

# --- a second run is a no-op -------------------------------------------------
run_case "second run changes nothing" 0 CONTRACT_IDEMPOTENT_OK <<'CASE'
set -eu
/usr/local/lib/plow/first-boot.sh
before="$(sha256sum /var/lib/hermes/SOUL.md | cut -d' ' -f1)"

# Even a part added after the first boot must not rewrite a composed identity:
# soul.d is a build-time contract, and an agent's identity does not change
# under it at runtime.
printf '# Added later\n' > /var/lib/hermes/soul.d/90-later.md
/usr/local/lib/plow/first-boot.sh
after="$(sha256sum /var/lib/hermes/SOUL.md | cut -d' ' -f1)"

[ "$before" = "$after" ]
# And it leaves no half-written temp file behind.
if ls /var/lib/hermes/SOUL.md.compose.* >/dev/null 2>&1; then
  echo "composition temp file left behind" >&2
  exit 1
fi
echo CONTRACT_IDEMPOTENT_OK
CASE

# --- a failing hook fails first boot -----------------------------------------
run_case "failing first-boot.d hook fails first boot" 9 CONTRACT_HOOK_FAILS_OK <<'CASE'
set -eu
printf '#!/bin/sh\necho CONTRACT_HOOK_FAILS_OK\nexit 9\n' > /usr/local/lib/plow/first-boot.d/50-bad.sh
chmod +x /usr/local/lib/plow/first-boot.d/50-bad.sh

# Composition happens before the hooks, so it must still have run — but the
# wrapper must carry the hook's failure out to systemd.
/usr/local/lib/plow/first-boot.sh
CASE

# --- a passing hook does not ------------------------------------------------
run_case "passing first-boot.d hooks run in order" 0 CONTRACT_HOOKS_RAN_OK <<'CASE'
set -eu
printf '#!/bin/sh\necho hook-10\n' > /usr/local/lib/plow/first-boot.d/10-a.sh
printf '#!/bin/sh\necho hook-50\n' > /usr/local/lib/plow/first-boot.d/50-b.sh
printf 'not executable\n'          > /usr/local/lib/plow/first-boot.d/20-skipped.sh
chmod +x /usr/local/lib/plow/first-boot.d/10-a.sh /usr/local/lib/plow/first-boot.d/50-b.sh

out="$(/usr/local/lib/plow/first-boot.sh)"
[ "$out" = "hook-10
hook-50" ]
[ -f /var/lib/hermes/SOUL.md ]
echo CONTRACT_HOOKS_RAN_OK
CASE

if [[ "$failures" -ne 0 ]]; then
  echo "$failures boot-contract case(s) failed" >&2
  exit 1
fi
echo "boot contract holds for $image" >&2

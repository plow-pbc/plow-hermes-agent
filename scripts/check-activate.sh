#!/usr/bin/env bash
# Prove `plow-activate` against a Plow stack running on this machine.
#
# The activation handshake needs a human with a phone. A local stack has an
# iMessage twin instead, so this script plays that part: it starts the tool,
# reads the code off its stderr, texts it through the twin, and then checks
# what came back -- that the dotenv carries all five variables, and that the
# credential in it reaches this agent's chats and nothing else on the account.
#
# usage: scripts/check-activate.sh [image]
#
#   PLOW_API_BASE   how the CONTAINER reaches the API   (default http://api:8000)
#   PLOW_API_HOST   how THIS SHELL reaches the same API (default the OrbStack name)
#   TWIN_BASE       the iMessage twin, from this shell
#   NETWORK         the Docker network the stack is on
#   TIMEOUT         seconds to wait for the redeem (default 300)
set -euo pipefail

cd "$(dirname "$0")/.."

image="${1:-plow-hermes-agent:local}"
api_base="${PLOW_API_BASE:-http://api:8000}"
api_host="${PLOW_API_HOST:-https://api.plow.orb.local}"
twin="${TWIN_BASE:-https://dtu-linq.plow.orb.local}"
network="${NETWORK:-plow_default}"
timeout="${TIMEOUT:-300}"
# Outside the twin's managed pool, and unused: a phone that already holds a
# chat on the assigned line sends the activation down the reassignment path,
# which is a different thing to test than this.
member="${MEMBER_PHONE:-+1415555$((RANDOM % 9000 + 1000))}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

docker run --rm --network "$network" "$image" plow-activate --help >/dev/null
echo "ok: plow-activate --help runs in the image" >&2

docker run --rm --network "$network" "$image" \
  plow-activate --api-base "$api_base" --name check-activate --poll-interval 3 --timeout "$timeout" \
  >"$work/env" 2>"$work/err" &
tool=$!

# The code and the number it has to reach are on stderr, by design -- stdout is
# the dotenv. Wait for the line rather than sleeping a guessed interval.
for _ in $(seq 60); do
  line="$(grep -o 'Plow Activate: [A-Z0-9]* *to *+[0-9]*' "$work/err" || true)"
  [ -n "$line" ] && break
  sleep 1
done
[ -n "${line:-}" ] || { echo "FAIL: the tool printed no activation code" >&2; cat "$work/err" >&2; exit 1; }
code="$(echo "$line" | sed -E 's/Plow Activate: ([A-Z0-9]*).*/\1/')"
send_to="$(echo "$line" | sed -E 's/.*(\+[0-9]+)$/\1/')"
echo "texting '$code' to $send_to as $member" >&2

curl -fsS -X POST -H 'Content-Type: application/json' \
  -d "{\"to_phone\":\"$send_to\",\"remote_phone\":\"$member\",\"text\":\"Plow Activate: $code\"}" \
  "$twin/ui/inbound" >/dev/null

wait "$tool" || { echo "FAIL: plow-activate exited non-zero" >&2; cat "$work/err" >&2; exit 1; }
cat "$work/err" >&2

for key in PLOW_API_BASE PLOW_HOME_CHANNEL PLOW_AGENT_TOKEN HERMES_CUSTOM_PLOW_API_KEY HERMES_PROVIDER; do
  grep -q "^$key=..*" "$work/env" || { echo "FAIL: $key missing from the dotenv" >&2; exit 1; }
done
echo "ok: the dotenv carries all five variables" >&2

token="$(sed -n 's/^PLOW_AGENT_TOKEN=//p' "$work/env")"
home="$(sed -n 's/^PLOW_HOME_CHANNEL=//p' "$work/env")"
[ "$token" = "$(sed -n 's/^HERMES_CUSTOM_PLOW_API_KEY=//p' "$work/env")" ] \
  || { echo "FAIL: the inference key is not the agent token" >&2; exit 1; }
case "$(sed -n 's/^PLOW_API_BASE=//p' "$work/env")" in
  */v1) echo "FAIL: PLOW_API_BASE carries a /v1 suffix" >&2; exit 1 ;;
esac
echo "ok: inference key == agent token, and the API base has no /v1" >&2

# The header in a file, never in argv: `$work` is a mktemp -d, so 0700, and a
# curl command line is readable from the process table by every account on the
# machine. This token can read and send the agent's chats and spend its
# inference.
printf 'Authorization: Bearer %s\n' "$token" > "$work/auth"
chmod 600 "$work/auth"

# `000` is curl's answer when it never got one -- an unresolvable host, a
# refused connection. Reported as itself, because "the token can still list
# account keys" is a claim about the credential, and a request that never left
# the machine says nothing about it either way.
status() {
  code="$(curl -sS -o /dev/null -w '%{http_code}' -H @"$work/auth" "$api_host$1" || true)"
  case "$code" in
    ''|000) echo "FAIL: no response from $api_host$1 -- the check proved nothing" >&2; return 1 ;;
  esac
  printf '%s' "$code"
}

# The credential is narrowed, proven by what it can no longer do. An account-wide
# token -- what activation hands back before the tool narrows it -- answers 200
# on both of these.
keys_code="$(status /v1/api-keys)" || exit 1
[ "$keys_code" = 403 ] || { echo "FAIL: the token can still list account keys (HTTP $keys_code)" >&2; exit 1; }
chat_code="$(status /v1/chats/$home/messages?limit=1)" || exit 1
[ "$chat_code" = 200 ] || { echo "FAIL: the token cannot read its own chat (HTTP $chat_code)" >&2; exit 1; }
echo "ok: keys:manage refused, its own chat readable" >&2

# ...and by what it is scoped to: the line of the chat it was actually given,
# which is not always the line the activation was assigned.
granted="$(sed -n 's/^  chats:  //p' "$work/err")"
chat_line="line:$(curl -fsS -H @"$work/auth" "$api_host/v1/chats/$home" \
  | python3 -c 'import json,sys
chat = json.load(sys.stdin)
agents = [p for p in chat["participants"] if p.get("type") == "agent"]
mine = [p for p in agents if p.get("relationship") == "self"] or agents
print(mine[0]["line"]["uid"])')"
[ "$granted" = "$chat_line" ] || { echo "FAIL: granted $granted but the chat is on $chat_line" >&2; exit 1; }
echo "ok: granted exactly $granted, the line the provisioned chat is on" >&2

echo "ok: $image mints, narrows and proves a Plow credential against $api_base" >&2

# Read a dotenv without letting it run.
#
# `.` on this file is how both the init oneshot and the gateway service used to
# load /var/lib/hermes/.env, and both run as root before dropping. A dotenv is
# data written by whoever provisioned this box -- on the cloud path a host
# script, on a developer's machine a file beside a compose file -- and `.` gives
# every line of it a root shell. `PLOW_AGENT_TOKEN=$(id > /tmp/x)` is a command
# substitution, not a token.
#
# A name already set when this runs keeps its value and the file's copy is
# skipped. That rule is what orders the two files this image reads: the host's
# credential drop-in is loaded into the environment first, so a dotenv
# persisted in a volume cannot reinstate the token, the home channel or the
# relay the drop-in just replaced.
#
# It is NOT a rule about the container environment, and every reader calls
# `plow_drop_inherited` below before its first load so that it cannot become
# one. Left in place, a `-e PLOW_AGENT_TOKEN=...` on `docker run` would take
# precedence over the credential file the image was given -- which is the whole
# of what a host is allowed to say about a tenant -- and over the dotenv the
# image renders from it. HERMES_PROVIDER and HERMES_MODEL are the exception,
# and are the only names the container environment may still supply.
#
# `export "$key=$value"` performs one assignment and re-parses nothing, so a
# value that looks like shell is a value that looks like shell. Everything that
# is not `NAME=` followed by a value is refused out loud rather than skipped
# quietly: a dotenv this cannot read is a dotenv nobody should be guessing at.
#
# Not re-parsing a value is not enough on its own, because a NAME can be as
# dangerous as a value: PATH sends every later command somewhere of the file's
# choosing, LD_PRELOAD loads a library into the gateway, and both are read by
# processes that are still root here. So the names are an allowlist, not a
# pattern: exactly what this image renders, plus the two that choose the
# inference provider. A dotenv naming anything else is refused, not filtered --
# a file carrying a variable this image does not set is a file whose author
# expected something that is not going to happen.
#
# The allowlist is a parameter because there are two files and they are not
# equally trusted. The home's dotenv is this image's own record and may carry
# everything below; the host's credential drop-in is written by a provisioner
# that is meant to know nothing about this image, so it is held to the shorter
# list -- anything else it names is a provisioner reaching into a home that is
# not its own, and is refused rather than quietly ignored.

PLOW_DOTENV_ALLOWED='
PLOW_API_BASE
PLOW_HOME_CHANNEL
PLOW_AGENT_TOKEN
HERMES_CUSTOM_PLOW_API_KEY
PLOW_MCP_URL
API_SERVER_KEY
TZ
HERMES_PROVIDER
HERMES_MODEL
'

# What a host may put in /var/lib/plow/credentials. Where to reach Plow and
# what to present when it gets there -- and nothing else. Everything the agent
# additionally needs to run is either asked of Plow with that credential (the
# home channel, the relay endpoint) or derived here (the inference key alias,
# the server key, the timezone), so a host that sets any of them is deciding
# something it was told not to and is refused rather than obeyed.
PLOW_CREDENTIALS_ALLOWED='
PLOW_API_BASE
PLOW_AGENT_TOKEN
'

# The names this image renders for itself, from the credential drop-in and from
# Plow's answer about the tenant holding it. An inherited container environment
# must not supply any of them: it is not a source of truth for who this agent
# is, and a stale one silently outranking the file a host just rewrote is a
# rotation that did not take.
#
# HERMES_PROVIDER and HERMES_MODEL are deliberately absent. Where inference
# goes is a decision about a container rather than a fact about the agent, so
# the environment stays authoritative for exactly those two.
PLOW_ENV_RENDERED='PLOW_API_BASE PLOW_HOME_CHANNEL PLOW_AGENT_TOKEN
                   HERMES_CUSTOM_PLOW_API_KEY PLOW_MCP_URL API_SERVER_KEY TZ'

# Called by every reader before its first `plow_load_dotenv`, so that the "a
# name already set keeps its value" rule above only ever refers to a value this
# image itself exported.
plow_drop_inherited() {
  for plow_inherited in $PLOW_ENV_RENDERED; do
    if printenv "$plow_inherited" >/dev/null 2>&1; then
      echo "plow: ignoring inherited $plow_inherited -- the image renders that name itself" >&2
      unset "$plow_inherited"
    fi
  done
  unset plow_inherited
}

# Strip one layer of matching outer quotes, the shape `shlex.quote` produces.
# Left alone when the quote character appears again inside, because then the
# value carries its own escaping and this is not the code to interpret it.
plow_dotenv_unquote() {
  case $1 in
    \'*\'|\"*\")
      inner=${1#?}; inner=${inner%?}
      case $inner in
        *\'*|*\"*) printf '%s' "$1" ;;
        *) printf '%s' "$inner" ;;
      esac
      ;;
    *) printf '%s' "$1" ;;
  esac
}

# plow_load_dotenv <file> [allowlist]
plow_load_dotenv() {
  plow_dotenv_file=$1
  plow_dotenv_allowed=${2:-$PLOW_DOTENV_ALLOWED}
  [ -f "$plow_dotenv_file" ] || return 0
  plow_dotenv_line=0
  while IFS= read -r plow_dotenv_raw || [ -n "$plow_dotenv_raw" ]; do
    plow_dotenv_line=$((plow_dotenv_line + 1))
    case $plow_dotenv_raw in
      ''|'#'*) continue ;;
      *=*) ;;
      *)
        echo "plow: $plow_dotenv_file:$plow_dotenv_line is not NAME=value -- refusing to load it" >&2
        return 1
        ;;
    esac
    plow_dotenv_key=${plow_dotenv_raw%%=*}
    plow_dotenv_val=${plow_dotenv_raw#*=}
    case $plow_dotenv_key in
      ''|[0-9]*|*[!A-Za-z0-9_]*)
        echo "plow: $plow_dotenv_file:$plow_dotenv_line has no usable variable name -- refusing to load it" >&2
        return 1
        ;;
    esac
    case "
$plow_dotenv_allowed" in
      *"
$plow_dotenv_key
"*) ;;
      *)
        echo "plow: $plow_dotenv_file:$plow_dotenv_line sets $plow_dotenv_key, which this image does not read -- refusing to load it" >&2
        return 1
        ;;
    esac
    if printenv "$plow_dotenv_key" >/dev/null 2>&1; then
      continue
    fi
    export "$plow_dotenv_key=$(plow_dotenv_unquote "$plow_dotenv_val")"
  done < "$plow_dotenv_file"
  unset plow_dotenv_file plow_dotenv_allowed plow_dotenv_line plow_dotenv_raw \
        plow_dotenv_key plow_dotenv_val
  return 0
}

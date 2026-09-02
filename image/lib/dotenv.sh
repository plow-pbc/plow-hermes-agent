# Read a dotenv without letting it run.
#
# `.` on this file is how both the init oneshot and the gateway service used to
# load /var/lib/hermes/.env, and both run as root before dropping. A dotenv is
# data written by whoever provisioned this box -- on the cloud path a host
# script, on a developer's machine a file beside a compose file -- and `.` gives
# every line of it a root shell. `PLOW_AGENT_TOKEN=$(id > /tmp/x)` is a command
# substitution, not a token.
#
# The environment wins. A name already set when this runs keeps its value and
# the file's copy is skipped, and that one rule is what makes the host's
# credential drop-in authoritative: it is read into the environment first, so a
# dotenv persisted in a volume cannot reinstate the token, the home channel or
# the relay the drop-in just replaced. The same rule carries HERMES_PROVIDER
# and HERMES_MODEL in from the container environment, which is where an
# operator switches inference provider.
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
# pattern. It is exactly what the cloud setup script writes
# (api/plow/cloud_agent/exe.py) plus the two that choose the inference
# provider, and a dotenv naming anything else is refused rather than filtered:
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

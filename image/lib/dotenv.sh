# Read a dotenv without letting it run.
#
# `.` on this file is how both the init oneshot and the gateway service used to
# load /var/lib/hermes/.env, and both run as root before dropping. A dotenv is
# data written by whoever provisioned this box -- on the cloud path a host
# script, on a developer's machine a file beside a compose file -- and `.` gives
# every line of it a root shell. `PLOW_AGENT_TOKEN=$(id > /tmp/x)` is a command
# substitution, not a token.
#
# `export "$key=$value"` performs one assignment and re-parses nothing, so a
# value that looks like shell is a value that looks like shell. Everything that
# is not `NAME=` followed by a value is refused out loud rather than skipped
# quietly: a dotenv this cannot read is a dotenv nobody should be guessing at.

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

plow_load_dotenv() {
  plow_dotenv_file=$1
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
    export "$plow_dotenv_key=$(plow_dotenv_unquote "$plow_dotenv_val")"
  done < "$plow_dotenv_file"
  unset plow_dotenv_file plow_dotenv_line plow_dotenv_raw plow_dotenv_key plow_dotenv_val
  return 0
}

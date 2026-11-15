#!/bin/sh
# Update a FuzzPan server from THIS machine: runs `fuzzpan-update` on it over SSH.
#
#   BOARD_SSH=root@board.example.com scripts/update_remote.sh            # update (asks first)
#   BOARD_SSH=root@board.example.com scripts/update_remote.sh --check    # just look
#   BOARD_SSH=root@board.example.com scripts/update_remote.sh --ref v0.3.0 --yes
#
# Settings (environment variables):
#   BOARD_SSH         user@host of the server (required). A user other than root
#                     runs it with sudo, which may ask for that user's password.
#   BOARD_REMOTE_DIR  install folder on the server (default /opt/fuzzpan)
#
# Options are passed to fuzzpan-update: --check, --yes, --force, --ref REF.
# A server installed before fuzzpan-update existed needs install.sh run once more
# by hand (it installs the command); this script says so.
set -eu

fail() { echo "update_remote: $*" >&2; exit 1; }

[ -n "${BOARD_SSH:-}" ] || fail "BOARD_SSH is not set (user@host of the server)."
# These go into a command run on the server, so allow only plain characters.
case "$BOARD_SSH" in -*|*[!A-Za-z0-9_.@:-]*) fail "BOARD_SSH has characters that are not allowed.";; esac
dir="${BOARD_REMOTE_DIR:-/opt/fuzzpan}"
case "$dir" in *[!A-Za-z0-9_./-]*) fail "BOARD_REMOTE_DIR has characters that are not allowed.";; esac

args="--dir $dir"
while [ $# -gt 0 ]; do
    case "$1" in
        --check|--yes|--force) args="$args $1"; shift;;
        --ref)
            [ $# -ge 2 ] || fail "--ref needs a value"
            case "$2" in -*|*..*|*[!A-Za-z0-9._/-]*) fail "'$2' is not a valid release name.";; esac
            args="$args --ref $2"; shift 2;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
        *) fail "unknown option: $1 (try --help)";;
    esac
done

remote="if ! command -v fuzzpan-update >/dev/null 2>&1; then
  echo 'This server has no fuzzpan-update yet. Run the installer from the newest release once, on the server:' >&2
  echo '  curl -fsSLO https://raw.githubusercontent.com/kvtaniguchi/fuzzpan/RELEASE/scripts/install.sh && sudo bash install.sh' >&2
  exit 3
fi
if [ \"\$(id -u)\" -eq 0 ]; then fuzzpan-update $args; else sudo fuzzpan-update $args; fi"

# -t: a terminal on the server, so fuzzpan-update (and sudo) can ask.
exec ssh -t "$BOARD_SSH" "$remote"

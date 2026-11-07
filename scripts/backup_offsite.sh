#!/bin/sh
# Encrypted off-site backup of the board database.
#
#   AGE_RECIPIENT=age1... RCLONE_REMOTE=myremote:board-backups scripts/backup_offsite.sh
#
# 1. takes a consistent copy with `board.admin_cli backup` (safe while the board runs),
# 2. encrypts it with age to AGE_RECIPIENT (a PUBLIC key; the private key stays with you),
# 3. uploads it with `rclone copyto`,
# 4. deletes every plaintext and encrypted local copy, success or failure.
# Exits non-zero if any step fails.
#
# Where the board runs:
#   on this host:   BOARD_DB must be set; the default admin command is
#                   `uv run python -m board.admin_cli` (override with BOARD_ADMIN_CMD).
#   under Docker:   BOARD_COMPOSE_DIR=/path/to/this/repo; the backup is made inside the
#                   board container and copied out with `docker compose cp`.
# Settings: real environment variables win; otherwise AGE_RECIPIENT, RCLONE_REMOTE and
# BACKUP_TMPDIR are read from the repo's .env (or the file named by BOARD_ENV_FILE).
# Runs from any folder, so it is cron-safe: it changes to the repo root itself. Cron's
# PATH is minimal, so /usr/local/bin and /opt/homebrew/bin are added.
# BACKUP_TMPDIR sets where the temporary working folder is made (default $TMPDIR or /tmp).
set -eu

fail() { echo "backup_offsite: $*" >&2; exit 1; }

PATH="$PATH:/usr/local/bin:/opt/homebrew/bin"
REPO="$(cd "$(dirname "$0")/.." && pwd)" || fail "cannot find the repo folder."
ENV_FILE="${BOARD_ENV_FILE:-$REPO/.env}"

# Take NAME from the env file only if it is not already set (no sourcing: the file
# is data, never executed).
from_env_file() {
    eval "current=\${$1:-}"
    [ -n "$current" ] && return 0
    [ -r "$ENV_FILE" ] || return 0
    value="$(grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2-)"
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    [ -n "$value" ] && eval "$1=\$value" && export "$1"
    return 0
}
for name in AGE_RECIPIENT RCLONE_REMOTE BACKUP_TMPDIR; do from_env_file "$name"; done

# Make paths absolute before changing folder.
case "${BOARD_DB:-}" in ""|/*) ;; *) BOARD_DB="$PWD/$BOARD_DB"; export BOARD_DB;; esac
case "${BACKUP_TMPDIR:-}" in ""|/*) ;; *) BACKUP_TMPDIR="$PWD/$BACKUP_TMPDIR";; esac

[ -n "${AGE_RECIPIENT:-}" ] || fail "AGE_RECIPIENT is not set (an age public key, age1...)."
[ -n "${RCLONE_REMOTE:-}" ] || fail "RCLONE_REMOTE is not set (e.g. myremote:board-backups)."
command -v age >/dev/null 2>&1 || fail "age is not installed."
command -v rclone >/dev/null 2>&1 || fail "rclone is not installed."
if [ -z "${BOARD_COMPOSE_DIR:-}" ] && [ -z "${BOARD_DB:-}" ]; then
    fail "BOARD_DB is not set (or set BOARD_COMPOSE_DIR for a Docker install)."
fi

cd "$REPO" || fail "cannot enter $REPO."
umask 077  # every file we create is private to this user
base="${BACKUP_TMPDIR:-${TMPDIR:-/tmp}}"
workdir="$(mktemp -d "${base%/}/board-backup.XXXXXX")" || fail "cannot create a temp folder in $base."
container_copy=""

cleanup() {
    status=$?
    trap - EXIT
    rm -rf "$workdir"
    if [ -n "$container_copy" ]; then
        (cd "$BOARD_COMPOSE_DIR" && docker compose exec -T board rm -f "$container_copy") \
            >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
plain="$workdir/board-$stamp.db"
encrypted="$plain.age"

if [ -n "${BOARD_COMPOSE_DIR:-}" ]; then
    container_copy="/tmp/board-backup-$stamp-$$.db"
    (cd "$BOARD_COMPOSE_DIR" \
        && docker compose exec -T board python -m board.admin_cli backup "$container_copy" \
        && docker compose cp "board:$container_copy" "$plain") \
        || fail "backup from the container failed."
else
    # Unquoted on purpose: the command may be several words.
    # shellcheck disable=SC2086
    ${BOARD_ADMIN_CMD:-uv run python -m board.admin_cli} backup "$plain" || fail "backup failed."
fi
[ -s "$plain" ] || fail "backup produced no file."

age -r "$AGE_RECIPIENT" -o "$encrypted" "$plain" || fail "encryption failed."
[ -s "$encrypted" ] || fail "encryption produced no file."
rm -f "$plain"  # the plaintext is no longer needed

rclone copyto "$encrypted" "${RCLONE_REMOTE%/}/board-$stamp.db.age" || fail "upload failed."
echo "Uploaded board-$stamp.db.age to $RCLONE_REMOTE"

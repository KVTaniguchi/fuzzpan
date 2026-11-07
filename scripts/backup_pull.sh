#!/bin/sh
# Pull a backup of the board's database from the server to THIS machine over SSH.
# No cloud account is needed: the copy goes straight to your own computer.
#
#   BOARD_SSH=deploy@board.example.com BOARD_REMOTE_DIR=/home/deploy/family-board \
#       scripts/backup_pull.sh
#
# 1. asks the server to take a consistent copy inside the board container
#    (`board.admin_cli backup`, safe while the board runs),
# 2. streams it back over SSH into BACKUP_DIR (the SSH connection is the encryption
#    in transit; nothing is left readable on the server),
# 3. checks the copy is a whole SQLite database,
# 4. optionally encrypts it with age when AGE_RECIPIENT is set (a PUBLIC key),
# 5. keeps the newest BACKUP_KEEP copies and deletes older ones.
# Exits non-zero if any step fails. It only runs while this machine is on, so run it
# from a scheduler that catches up after sleep (launchd on a Mac; see README).
#
# A backup holds each admin's TOTP secret and every token hash. The folder is mode
# 0700 and each file 0600. Keep the folder on an encrypted disk (FileVault on a Mac),
# or set AGE_RECIPIENT so each file is encrypted at rest as well.
#
# Settings (environment variables):
#   BOARD_SSH         user@host of the server (required)
#   BOARD_REMOTE_DIR  folder on the server holding docker-compose.yml (required)
#   BACKUP_DIR        where copies go (default ~/Backups/family-board)
#   BACKUP_KEEP       how many copies to keep (default 30)
#   AGE_RECIPIENT     optional age public key (age1...) to encrypt each copy
set -eu

fail() { echo "backup_pull: $*" >&2; exit 1; }

PATH="$PATH:/usr/local/bin:/opt/homebrew/bin"

[ -n "${BOARD_SSH:-}" ] || fail "BOARD_SSH is not set (user@host of the server)."
[ -n "${BOARD_REMOTE_DIR:-}" ] || fail "BOARD_REMOTE_DIR is not set (the server folder with docker-compose.yml)."
# These go into a command run on the server, so allow only plain characters.
case "$BOARD_SSH" in -*|*[!A-Za-z0-9_.@:-]*) fail "BOARD_SSH has characters that are not allowed.";; esac
case "$BOARD_REMOTE_DIR" in *[!A-Za-z0-9_./-]*) fail "BOARD_REMOTE_DIR has characters that are not allowed.";; esac
keep="${BACKUP_KEEP:-30}"
case "$keep" in ""|*[!0-9]*|0) fail "BACKUP_KEEP must be a whole number, 1 or more.";; esac
if [ -n "${AGE_RECIPIENT:-}" ]; then
    command -v age >/dev/null 2>&1 || fail "AGE_RECIPIENT is set but age is not installed."
fi
command -v ssh >/dev/null 2>&1 || fail "ssh is not installed."

umask 077  # every file and folder we create is private to this user
dest="${BACKUP_DIR:-$HOME/Backups/family-board}"
mkdir -p "$dest" || fail "cannot create $dest."
chmod 700 "$dest" || fail "cannot restrict $dest."

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
partial="$dest/.partial-$stamp-$$"
remote_file="/tmp/board-backup-$stamp-$$.db"
remote_made=""

remote() { ssh -o BatchMode=yes -- "$BOARD_SSH" "cd $BOARD_REMOTE_DIR && $1"; }

cleanup() {
    status=$?
    trap - EXIT
    rm -f "$partial" "$partial.age"
    if [ -n "$remote_made" ]; then
        remote "docker compose exec -T board rm -f $remote_file" >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

remote_made=1
remote "docker compose exec -T board python -m board.admin_cli backup $remote_file" \
    || fail "the server could not make a backup."
remote "docker compose exec -T board cat $remote_file" > "$partial" \
    || fail "could not download the backup."
[ -s "$partial" ] || fail "the download was empty."

# A whole SQLite file starts with this 16-byte header; sqlite3, if present, checks the rest.
[ "$(head -c 15 "$partial")" = "SQLite format 3" ] || fail "the download is not a SQLite database."
if command -v sqlite3 >/dev/null 2>&1; then
    [ "$(sqlite3 "$partial" 'PRAGMA integrity_check;')" = "ok" ] \
        || fail "the downloaded database failed its integrity check."
fi

if [ -n "${AGE_RECIPIENT:-}" ]; then
    final="$dest/board-$stamp.db.age"
    age -r "$AGE_RECIPIENT" -o "$partial.age" "$partial" || fail "encryption failed."
    [ -s "$partial.age" ] || fail "encryption produced no file."
    mv "$partial.age" "$final"
else
    final="$dest/board-$stamp.db"
    mv "$partial" "$final"
fi

# Timestamps in the names sort oldest first, so drop everything before the newest $keep.
ls -1 "$dest" | grep -E '^board-[0-9]{8}T[0-9]{6}Z\.db(\.age)?$' | sort -r | tail -n "+$((keep + 1))" \
    | while read -r old; do rm -f "$dest/$old"; done || true

echo "Saved $final"

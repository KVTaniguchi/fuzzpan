#!/bin/bash
# End-to-end check of the Docker packaging. Needs docker (with compose) and curl.
#
#   scripts/smoke_compose.sh
#
# Safe next to other things on the machine: its own compose project name and
# volumes, its own host ports (free ones, or SMOKE_HTTP_PORT / SMOKE_HTTPS_PORT),
# an empty env file so your .env is ignored, and a trap that removes its
# containers, volumes and image on exit. It uses docker-compose.yml only (no override file). It never touches a database outside
# its own volume and never contacts any host port except its own two.
# Final line on success: "smoke test passed".
set -euo pipefail

cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl is not installed" >&2; exit 1; }

# Nothing from the host shell or compose configuration reaches the project.
unset BOARD_DB BOARD_SECURE_COOKIES COMPOSE_FILE COMPOSE_PROFILES COMPOSE_PATH_SEPARATOR COMPOSE_ENV_FILES
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }

PROJECT="boardsmoke-$$-$(date +%s)"
HTTP_PORT="${SMOKE_HTTP_PORT:-$(free_port)}"
HTTPS_PORT="${SMOKE_HTTPS_PORT:-$(free_port)}"
WORK="$(mktemp -d)"
: > "$WORK/empty.env"
export BOARD_DOMAIN=localhost
export BOARD_PUBLIC_URL="https://localhost:$HTTPS_PORT"
export BOARD_HTTP_PORT="$HTTP_PORT"
export BOARD_HTTPS_PORT="$HTTPS_PORT"
BASE="https://localhost:$HTTPS_PORT"

# -f names the one file explicitly, so no docker-compose.override.yml / compose.override.yml
# is merged in; the empty env file means your .env is ignored too.
dc() { docker compose -f docker-compose.yml --project-name "$PROJECT" --env-file "$WORK/empty.env" "$@"; }
cleanup() {
    status=$?
    dc down --volumes --remove-orphans --rmi local >/dev/null 2>&1 || true
    rm -rf "$WORK"
    [ "$status" -eq 0 ] || echo "smoke test FAILED (project $PROJECT removed)" >&2
    exit "$status"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo "== $*"; }
# Prints the HTTP status. Under set -e a failing curl would end the script instead of
# reporting: Caddy may close the connection while a big body is still being sent, and
# curl then exits non-zero (55, 52) even though the answer was 413. So the exit status
# is caught here, and a status curl never got is shown as 000 with curl's exit code.
code_of() {
    local out rc=0
    out="$(curl -ks -o /dev/null -w '%{http_code}' "$@")" || rc=$?
    if [ "$out" = "000" ] && [ "$rc" -ne 0 ]; then echo "000 (curl exit $rc)"; else echo "$out"; fi
}
totp() { dc exec -T board python -c 'import pyotp,sys; print(pyotp.TOTP(sys.argv[1]).now())' "$1"; }

step "build and start ($PROJECT, https port $HTTPS_PORT)"
dc up -d --build --wait

step "healthz answers through Caddy over HTTPS"
ok=""
for _ in $(seq 1 30); do
    if curl -ksf "$BASE/healthz" | grep -q '"status":"ok"'; then ok=1; break; fi
    sleep 1
done
[ -n "$ok" ] || fail "no healthy response from $BASE/healthz"

step "the board's own port is not published, but Caddy can reach it"
board_cid="$(dc ps -q board)"
published="$(docker inspect --format '{{json .NetworkSettings.Ports}}' "$board_cid")"
echo "$published" | grep -q HostPort && fail "board publishes a host port: $published"
dc exec -T caddy wget -q -O - http://board:8787/healthz | grep -q '"status":"ok"' \
    || fail "Caddy cannot reach board:8787 (positive control failed)"

step "a spoofed X-Forwarded-For does not change the rate-limit key"
# 12 attempts, each claiming a different client. Keyed on the real client they
# hit the limit of 10; keyed on the header they would never be limited.
limited=""
for i in $(seq 1 12); do
    c="$(code_of -X POST -H "X-Forwarded-For: 203.0.113.$i" "$BASE/v1/invites/not-a-real-code/redeem")"
    [ "$c" = "429" ] && limited=1
done
[ -n "$limited" ] || fail "12 spoofed attempts were never rate limited"

step "an oversized body is refused with 413, with a length and chunked"
# Over Caddy's max_size, so this checks Caddy's cap. The app's own lower cap is
# covered by tests/test_request_limit.py.
head -c 300000 /dev/zero | tr '\0' 'x' > "$WORK/big.bin"
c="$(code_of -X POST -H 'Content-Type: application/x-www-form-urlencoded' --data-binary "@$WORK/big.bin" "$BASE/login")"
[ "$c" = "413" ] || fail "oversized POST returned $c, expected 413"
c="$(code_of -X POST -H 'Content-Type: application/x-www-form-urlencoded' -H 'Transfer-Encoding: chunked' --data-binary "@$WORK/big.bin" "$BASE/login")"
[ "$c" = "413" ] || fail "oversized chunked POST returned $c, expected 413"

step "provisioning prints setup links"
out="$(dc exec -T board python -m board.admin_cli provision --admin alice:Alice --admin bob:Bob)"
echo "$out"
link="$(echo "$out" | sed -n 's|^  alice: \(https://.*/setup/.*\)$|\1|p')"
[ -n "$link" ] || fail "no setup link for alice in the output"
case "$link" in "$BASE"/setup/*) ;; *) fail "setup link does not start with BOARD_PUBLIC_URL: ${link%/setup/*}";; esac

step "complete setup, log in, and post as a logged-in admin"
page="$(curl -ksf "$link")"
secret="$(echo "$page" | sed -n 's|.*<code>\([A-Z2-7]*\)</code>.*|\1|p' | head -n 1)"
[ -n "$secret" ] || fail "no authenticator key on the setup page"
password="smoke-$(date +%s)-passphrase"
setup_code="$(totp "$secret")"
c="$(code_of -X POST -H "Origin: $BASE" --data-urlencode "password=$password" \
    --data-urlencode "code=$setup_code" "$link")"
[ "$c" = "303" ] || fail "setup returned $c, expected 303"
# A login code must be from a later 30-second step than the one setup used.
login_code="$setup_code"
for _ in $(seq 1 40); do
    login_code="$(totp "$secret")"
    [ "$login_code" != "$setup_code" ] && break
    sleep 1
done
[ "$login_code" != "$setup_code" ] || fail "the authenticator code never advanced"
JAR="$WORK/cookies"
c="$(code_of -c "$JAR" -X POST -H "Origin: $BASE" --data-urlencode "username=alice" \
    --data-urlencode "password=$password" --data-urlencode "code=$login_code" "$BASE/login")"
[ "$c" = "303" ] || fail "login returned $c, expected 303"
grep -q board_session "$JAR" || fail "no session cookie after login"

marker="smoke-post-$$"
# Browsers send Origin with the port when it isn't 443; Caddy must pass the same Host.
c="$(code_of -b "$JAR" -X POST -H "Origin: $BASE" --data-urlencode "body=$marker" \
    --data-urlencode "kind=note" "$BASE/lanes/engineering/post")"
[ "$c" = "303" ] || fail "logged-in post with matching Origin returned $c, expected 303 (Host/Origin mismatch behind Caddy?)"
c="$(code_of -b "$JAR" -X POST -H "Origin: https://evil.example" --data-urlencode "body=nope" \
    "$BASE/lanes/engineering/post")"
[ "$c" = "403" ] || fail "cross-origin post returned $c, expected 403"
curl -ksf -b "$JAR" "$BASE/lanes/engineering" | grep -q "$marker" || fail "the post is not on the lane page"

step "data survives docker compose down && up"
dc down
dc up -d --wait
for _ in $(seq 1 30); do curl -ksf "$BASE/healthz" >/dev/null && break; sleep 1; done
curl -ksf -b "$JAR" "$BASE/lanes/engineering" | grep -q "$marker" \
    || fail "post or session lost across down/up"

step "a backup restores"
dc exec -T board python -m board.admin_cli backup /tmp/smoke-backup.db
mode="$(dc exec -T board stat -c %a /tmp/smoke-backup.db | tr -d '\r')"
[ "$mode" = "600" ] || fail "backup file mode is $mode, expected 600"
if dc exec -T board python -m board.admin_cli restore /tmp/smoke-backup.db >/dev/null 2>&1; then
    fail "restore overwrote a database that holds data without --force"
fi
dc exec -T -e BOARD_DB=/data/restored.db board python -m board.admin_cli restore /tmp/smoke-backup.db
counts="$(dc exec -T board python -c '
import sqlite3
c = sqlite3.connect("/data/restored.db")
print(c.execute("select count(*) from admins").fetchone()[0], c.execute("select count(*) from posts").fetchone()[0])
' | tr -d '\r')"
[ "$counts" = "2 1" ] || fail "restored database has admins/posts '$counts', expected '2 1'"

step "restore into the live database, the documented Docker way"
marker2="after-backup-$$"
c="$(code_of -b "$JAR" -X POST -H "Origin: $BASE" --data-urlencode "body=$marker2" \
    --data-urlencode "kind=note" "$BASE/lanes/engineering/post")"
[ "$c" = "303" ] || fail "second post returned $c"
dc exec -T board cat /tmp/smoke-backup.db > "$WORK/backup.db"
dc stop board
# The file is created inside the volume by the board's own user (not by `docker compose cp`,
# which would make it root-owned), mode 0600.
dc run --rm --no-deps -T board sh -c 'umask 077; cat > /data/restore.db' < "$WORK/backup.db"
dc run --rm --no-deps -T board python -m board.admin_cli restore /data/restore.db --force
dc run --rm --no-deps -T board sh -c 'ls /data/board.db.before-restore-* && rm -f /data/restore.db' \
    || fail "no safety copy was kept"
dc up -d --wait board
for _ in $(seq 1 30); do curl -ksf "$BASE/healthz" >/dev/null && break; sleep 1; done
page="$(curl -ksf -b "$JAR" "$BASE/lanes/engineering")"
echo "$page" | grep -q "$marker" || fail "restored board lost the backed-up post"
echo "$page" | grep -q "$marker2" && fail "restore did not replace the live database"

echo "smoke test passed"

#!/bin/bash
# Install (or upgrade) FuzzPan on a fresh Ubuntu or Debian server.
#
# Run it ON THE SERVER, as root. Download it, read it, then run it:
#
#   curl -fsSLO https://raw.githubusercontent.com/kvtaniguchi/fuzzpan/<release>/scripts/install.sh
#   less install.sh
#   sudo bash install.sh
#
# With no options it asks for what it needs. For an agent, or to skip the questions:
#
#   sudo bash install.sh --yes --domain board.example.com \
#       --admin alice:Alice --admin bob:Bob
#
# Options
#   --domain NAME      the domain whose DNS points at this server, or "auto" for a
#                      free name made from this server's IP address (<ip>.sslip.io),
#                      which needs no DNS setup and suits a first try
#   --admin NAME:Full Name   an admin to create (repeat for each); lowercase name
#   --ref REF          the release (tag) to install. Default: $FUZZPAN_REF, else main
#   --dir DIR          install folder (default /opt/fuzzpan)
#   --yes              never ask; fail if something needed was not given
#   --skip-dns-check   carry on even if the domain does not seem to point here
#   --no-firewall      do not touch the firewall (ufw)
#
# What it does, in order: checks this is a supported server; checks the domain's
# DNS; installs Docker if missing; allows only SSH, 80 and 443 in ufw; turns on
# automatic security updates; downloads FuzzPan into the install folder; writes
# .env; starts it; creates the admins and prints one single-use setup link each;
# runs scripts/doctor.py. Run again, it upgrades: it takes a database backup
# first and keeps your .env and data.
#
# What it never does: buy anything, change DNS, change SSH settings, create or
# print an agent token, ask for a password, publish the board's own port (8787),
# or send anything off this machine except downloads (Docker, the FuzzPan source).
#
# Progress is written to the log (default /var/log/fuzzpan-install.log). The setup
# links are NOT: they are printed once, to the terminal only.
set -euo pipefail

REPO="${FUZZPAN_REPO:-kvtaniguchi/fuzzpan}"
REF="${FUZZPAN_REF:-main}"
DIR="${FUZZPAN_DIR:-/opt/fuzzpan}"
LOG="${FUZZPAN_LOG:-/var/log/fuzzpan-install.log}"
TARBALL_URL="${FUZZPAN_TARBALL_URL:-}"
# Test hooks only: where the OS description is read from, and a prefix for /etc writes.
OS_RELEASE="${FUZZPAN_OS_RELEASE:-/etc/os-release}"
ROOTFS="${FUZZPAN_ROOTFS:-}"

DOMAIN=""
ADMINS=()
ASSUME_YES=0
SKIP_DNS=0
NO_FIREWALL=0

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG" 2>/dev/null || true; }
say() { printf '%s\n' "$*"; log "$*"; }
step() { printf '\n== %s\n' "$*"; log "== $*"; }
die() { printf 'install: %s\n' "$*" >&2; log "FAILED: $*"; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) [ $# -ge 2 ] || die "--domain needs a value"; DOMAIN="$2"; shift 2;;
        --admin) [ $# -ge 2 ] || die "--admin needs name:Full Name"; ADMINS+=("$2"); shift 2;;
        --ref) [ $# -ge 2 ] || die "--ref needs a value"; REF="$2"; shift 2;;
        --dir) [ $# -ge 2 ] || die "--dir needs a value"; DIR="$2"; shift 2;;
        --yes) ASSUME_YES=1; shift;;
        --skip-dns-check) SKIP_DNS=1; shift;;
        --no-firewall) NO_FIREWALL=1; shift;;
        -h|--help) sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
        *) die "unknown option: $1 (try --help)";;
    esac
done

# ---------------------------------------------------------------- helpers

ask() { # ask VARNAME "question" -> sets VARNAME from the terminal
    local __var="$1" __q="$2" __a=""
    [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ] || die "$__q Not given, and no terminal to ask on; pass it as an option."
    read -r -p "$__q " __a || die "no answer"
    printf -v "$__var" '%s' "$__a"
}

is_private_ip() {
    case "$1" in
        10.*|127.*|169.254.*|192.168.*) return 0;;
        172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0;;
        100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) return 0;;
    esac
    return 1
}

server_ips() {
    { hostname -I 2>/dev/null || true; } | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true
}

public_ip() {
    local ip
    for ip in $(server_ips); do is_private_ip "$ip" || { printf '%s' "$ip"; return 0; }; done
    return 1
}

valid_domain() {
    [[ "$1" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] \
        && ! [[ "$1" =~ ^[0-9.]+$ ]]
}

valid_admin() {
    local name="${1%%:*}" display="${1#*:}"
    [[ "$1" == *:* ]] && [[ "$name" =~ ^[a-z][a-z0-9-]{0,31}$ ]] && [ "$name" != shared ] \
        && [ -n "${display//[[:space:]]/}" ]
}

dc() { (cd "$DIR" && docker compose "$@"); }

# ------------------------------------------------------------- preflight

step "Checking this server"
[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash install.sh)."
[ -r "$OS_RELEASE" ] || die "cannot tell what system this is."
OS_ID="$(. "$OS_RELEASE" && echo "${ID:-}")"
OS_CODENAME="$(. "$OS_RELEASE" && echo "${VERSION_CODENAME:-}")"
case "$OS_ID" in ubuntu|debian) ;; *) die "this installer supports Ubuntu and Debian ($OS_ID found). Install by hand: see README.";; esac
command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl ca-certificates; }

UPGRADE=0
[ -f "$DIR/docker-compose.yml" ] && UPGRADE=1

mem_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
if [ "${mem_kb:-0}" -lt 900000 ]; then
    say "warning: this server has under 1 GB of memory. Building the image may fail; adding swap helps."
fi

if [ "$UPGRADE" -eq 0 ] && command -v ss >/dev/null; then
    for port in 80 443; do
        if ss -ltn "( sport = :$port )" 2>/dev/null | grep -q LISTEN; then
            die "port $port is already in use on this server. FuzzPan needs 80 and 443 to itself."
        fi
    done
fi

# ----------------------------------------------------------------- inputs

if [ "$UPGRADE" -eq 1 ]; then
    step "Found an existing install in $DIR: upgrading (your .env and data are kept)"
    [ -f "$DIR/.env" ] || die "$DIR has no .env; this does not look like a finished install."
    DOMAIN="$(sed -n 's/^BOARD_DOMAIN=//p' "$DIR/.env" | head -n 1)"
else
    if [ -z "$DOMAIN" ]; then
        ask DOMAIN "Domain pointing at this server (or 'auto' for an <ip>.sslip.io name):"
    fi
    if [ "$DOMAIN" = auto ]; then
        ip="$(public_ip)" || die "no public IPv4 address found on this server; give a real domain."
        DOMAIN="${ip//./-}.sslip.io"
        say "Using $DOMAIN (a name that always points at $ip)."
    fi
    DOMAIN="$(printf '%s' "$DOMAIN" | tr 'A-Z' 'a-z')"
    valid_domain "$DOMAIN" || die "'$DOMAIN' is not a valid domain name."

    if [ "${#ADMINS[@]}" -eq 0 ]; then
        say "Admins are the people who run the board. Give each a short lowercase name and a full name."
        while :; do
            ask entry "Admin as name:Full Name (empty line when done):" || true
            [ -n "${entry:-}" ] || break
            valid_admin "$entry" || { say "  '$entry' is not valid: use e.g. alice:Alice Smith"; continue; }
            ADMINS+=("$entry")
        done
    fi
    [ "${#ADMINS[@]}" -gt 0 ] || die "at least one admin is required (--admin alice:Alice)."
    for a in "${ADMINS[@]}"; do valid_admin "$a" || die "'$a' is not a valid admin: use name:Full Name, name lowercase letters/digits/hyphens."; done
fi

# -------------------------------------------------------------------- DNS

if [ "$UPGRADE" -eq 0 ] && [ "$SKIP_DNS" -eq 0 ]; then
    step "Checking that $DOMAIN points at this server"
    resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' || true)"
    mine=" $(server_ips | tr '\n' ' ') "
    match=0
    for ip in $resolved; do case "$mine" in *" $ip "*) match=1;; esac; done
    if [ -z "$resolved" ]; then
        die "$DOMAIN does not resolve yet. Create a DNS A record for it pointing at this server's IP ($(public_ip || echo 'see your provider')), wait a few minutes, and run this again."
    elif [ "$match" -eq 0 ]; then
        die "$DOMAIN points at ${resolved}but this server's addresses are:$mine
Fix the DNS record and run this again. (Behind NAT, as on some clouds, this server cannot see its own public address: then check the record yourself and pass --skip-dns-check.) Asking Let's Encrypt for a certificate that cannot work counts against its failure limits, so this stops here."
    fi
    say "ok: $DOMAIN -> $resolved"
fi

# ----------------------------------------------------------------- Docker

step "Docker"
if docker compose version >/dev/null 2>&1; then
    say "Docker with Compose is already installed."
else
    say "Installing Docker from Docker's own apt repository."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl
    mkdir -p "$ROOTFS/etc/apt/sources.list.d"
    install -m 0755 -d "$ROOTFS/etc/apt/keyrings"
    curl -fsSL "https://download.docker.com/linux/$OS_ID/gpg" -o "$ROOTFS/etc/apt/keyrings/docker.asc"
    chmod a+r "$ROOTFS/etc/apt/keyrings/docker.asc"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$OS_ID $OS_CODENAME stable" \
        > "$ROOTFS/etc/apt/sources.list.d/docker.list"
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    docker compose version >/dev/null 2>&1 || die "Docker installed but 'docker compose' does not work."
fi

# --------------------------------------------------------------- firewall

if [ "$NO_FIREWALL" -eq 1 ]; then
    say "Skipping the firewall (--no-firewall)."
elif [ "$UPGRADE" -eq 1 ]; then
    :
else
    step "Firewall: allow only SSH, 80 and 443"
    export DEBIAN_FRONTEND=noninteractive
    command -v ufw >/dev/null || apt-get install -y -qq ufw
    # Allow the port this very session came in on, and the one sshd listens on,
    # before enabling: enabling first would cut you off.
    ssh_ports="22"
    [ -n "${SSH_CONNECTION:-}" ] && ssh_ports="$ssh_ports $(echo "$SSH_CONNECTION" | awk '{print $4}')"
    if command -v ss >/dev/null; then
        ssh_ports="$ssh_ports $( { ss -ltnpH 2>/dev/null | grep sshd | awk '{print $4}' | sed 's/.*://'; } || true)"
    fi
    for p in $(printf '%s\n' $ssh_ports | grep -E '^[0-9]+$' | sort -un); do ufw allow "$p/tcp" >/dev/null; done
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    ufw --force enable >/dev/null
    say "ufw is on: SSH ($(printf '%s ' $(printf '%s\n' $ssh_ports | grep -E '^[0-9]+$' | sort -un))), 80 and 443 only."
fi

# -------------------------------------------------------- security updates

if [ "$UPGRADE" -eq 0 ]; then
    step "Automatic security updates"
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -qq unattended-upgrades >/dev/null
    mkdir -p "$ROOTFS/etc/apt/apt.conf.d"
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
        > "$ROOTFS/etc/apt/apt.conf.d/20auto-upgrades"
    say "on."
fi

# ---------------------------------------------------- back up before upgrade

if [ "$UPGRADE" -eq 1 ] && [ -n "$(dc ps -q board 2>/dev/null)" ]; then
    step "Backing up the database before upgrading"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    dc exec -T board python -m board.admin_cli backup "/data/pre-upgrade-$stamp.db" \
        || die "the backup failed, so nothing was changed. Fix that first."
    say "Saved inside the data volume as /data/pre-upgrade-$stamp.db (mode 600). It holds the same secrets as the database; delete it once the upgrade is confirmed."
fi

# --------------------------------------------------------------- the source

step "Downloading FuzzPan ($REF)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
url="${TARBALL_URL:-https://github.com/$REPO/archive/$REF.tar.gz}"
curl -fsSL "$url" -o "$work/src.tar.gz" || die "could not download $url (is the repository public, and the release name right?)"
mkdir "$work/src"
tar -xzf "$work/src.tar.gz" -C "$work/src" --strip-components=1 || die "the download was not a valid archive."
for f in docker-compose.yml Caddyfile Dockerfile pyproject.toml uv.lock board client scripts/doctor.py; do
    [ -e "$work/src/$f" ] || die "the download does not contain $f; wrong repository or release?"
done
mkdir -p "$DIR"
chmod 755 "$DIR"
if [ "$UPGRADE" -eq 1 ]; then
    rm -rf "$DIR/board" "$DIR/client" "$DIR/scripts" "$DIR/docs"
fi
cp -a "$work/src/." "$DIR/"
say "Installed the files into $DIR."

if [ "$UPGRADE" -eq 0 ]; then
    ( umask 077
      printf 'BOARD_DOMAIN=%s\nBOARD_PUBLIC_URL=https://%s\n' "$DOMAIN" "$DOMAIN" > "$DIR/.env" )
    say "Wrote $DIR/.env (mode 600): your domain and address, no secrets."
fi

# ------------------------------------------------------------------ start

step "Starting the board (the first build takes a few minutes)"
dc up -d --build --wait || die "the stack did not come up healthy. See: cd $DIR && docker compose logs"

ready=0
say "Waiting for https://$DOMAIN to answer (the certificate is fetched on first contact)..."
for _ in $(seq 1 60); do
    if curl -fsS -m 5 "https://$DOMAIN/healthz" 2>/dev/null | grep -q '"status":"ok"'; then ready=1; break; fi
    sleep 2
done
if [ "$ready" -eq 1 ]; then say "ok: the board answers over https."
else say "The board is running but https://$DOMAIN did not answer yet. The doctor below says why."; fi

# ------------------------------------------------------------ the admins

if [ "$UPGRADE" -eq 0 ]; then
    step "Creating the admins"
    args=()
    for a in "${ADMINS[@]}"; do args+=(--admin "$a"); done
    if links="$(dc exec -T board python -m board.admin_cli provision "${args[@]}" 2>&1)"; then
        log "created admins: $(printf '%s ' "${ADMINS[@]%%:*}")"
        printf '%s\n' "$links"
    elif printf '%s' "$links" | grep -q 'already has admins'; then
        say "The admins already exist (an earlier run got this far). A setup link cannot be shown again; to get a new one:"
        say "  cd $DIR && docker compose exec board python -m board.admin_cli reset-admin NAME"
    else
        printf '%s\n' "$links" >&2
        die "creating the admins failed (message above)."
    fi
fi

# ----------------------------------------------------------------- doctor

step "Checking the install"
doctor_rc=0
if command -v python3 >/dev/null; then
    python3 "$DIR/scripts/doctor.py" --dir "$DIR" || doctor_rc=$?
else
    say "python3 is not installed here, so the doctor was skipped. apt-get install -y python3, then: python3 $DIR/scripts/doctor.py"
fi

if [ "$UPGRADE" -eq 1 ]; then
    cat <<EOF

== Upgraded
Your admins, agents, lanes and posts are unchanged. The backup taken before the
upgrade is in the data volume as /data/pre-upgrade-*.db. It holds the same
secrets as the database, so once the board looks right, delete it:
  cd $DIR && docker compose exec board sh -c 'rm /data/pre-upgrade-*.db'
Back up the board to YOUR computer, from a terminal there (not here):
  curl -fsSLO https://raw.githubusercontent.com/$REPO/$REF/scripts/backup_pull.sh
  BOARD_SSH=root@${DOMAIN} BOARD_REMOTE_DIR=$DIR sh backup_pull.sh
To check any time: python3 $DIR/scripts/doctor.py
EOF
    log "done, upgrade (doctor exit $doctor_rc)"
    exit "$doctor_rc"
fi

cat <<EOF

== Next
1. Open your setup link (above) in a browser. Choose a password and add the
   authenticator. Setup links work once and expire after 24 hours.
2. On the welcome page, pick the lanes for your first agent and copy its invite link.
3. Give that link to the agent. The link may pass through chat; a token never should.
4. Back up the board to YOUR computer, from a terminal there (not here):
     curl -fsSLO https://raw.githubusercontent.com/$REPO/$REF/scripts/backup_pull.sh
     BOARD_SSH=root@${DOMAIN} BOARD_REMOTE_DIR=$DIR sh backup_pull.sh
   Without a copy elsewhere, a lost server is a lost board.
To upgrade later: run this script again. To check any time: python3 $DIR/scripts/doctor.py
EOF
log "done (doctor exit $doctor_rc)"
exit "$doctor_rc"

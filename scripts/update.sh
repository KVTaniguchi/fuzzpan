#!/bin/bash
# Update FuzzPan on this server to the newest release.
#
# install.sh puts this script at /usr/local/bin/fuzzpan-update. Run it ON THE
# SERVER:
#
#   sudo fuzzpan-update            # update to the newest release (asks first)
#   fuzzpan-update --check         # only say whether an update is available
#   sudo fuzzpan-update --ref v0.3.0 --yes
#
# From your own computer, scripts/update_remote.sh runs it over SSH.
#
# Options
#   --check       say what is installed and what the newest release is; change nothing
#   --ref REF     the release (tag) to update to. Default: the newest published release
#   --dir DIR     install folder (default /opt/fuzzpan)
#   --yes         do not ask before updating
#   --force       run the update even if REF is already installed
#
# How: it reads the installed release from DIR/.fuzzpan-release, asks GitHub for
# the newest published release, then downloads install.sh FROM THAT RELEASE and
# runs it with --yes --ref. So every update gets that release's own upgrade steps:
# a database backup first, your .env and data kept, and the previous version put
# back automatically if the new one does not start.
#
# What it never does: update on its own schedule, buy anything, or send anything
# off this machine except downloads (the release list, install.sh, the source).
set -euo pipefail

DIR="${FUZZPAN_DIR:-/opt/fuzzpan}"
CHECK=0
ASSUME_YES=0
FORCE=0
TARGET=""

die() { printf 'fuzzpan-update: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK=1; shift;;
        --ref) [ $# -ge 2 ] || die "--ref needs a value"; TARGET="$2"; shift 2;;
        --dir) [ $# -ge 2 ] || die "--dir needs a value"; DIR="$2"; shift 2;;
        --yes) ASSUME_YES=1; shift;;
        --force) FORCE=1; shift;;
        -h|--help) sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
        *) die "unknown option: $1 (try --help)";;
    esac
done

valid_ref() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$ ]] && [[ "$1" != *..* ]]; }
valid_repo() { [[ "$1" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; }

[ -f "$DIR/docker-compose.yml" ] || die "no FuzzPan install in $DIR. Install first (scripts/install.sh), or pass --dir."

# The release record install.sh writes: REPO=owner/name and REF=tag.
REPO="${FUZZPAN_REPO:-$(sed -n 's/^REPO=//p' "$DIR/.fuzzpan-release" 2>/dev/null | head -n 1 || true)}"
REPO="${REPO:-kvtaniguchi/fuzzpan}"
valid_repo "$REPO" || die "'$REPO' is not a GitHub owner/name."
CURRENT="$(sed -n 's/^REF=//p' "$DIR/.fuzzpan-release" 2>/dev/null | head -n 1 || true)"

if [ -z "$TARGET" ]; then
    api="${FUZZPAN_RELEASES_URL:-https://api.github.com/repos/$REPO/releases/latest}"
    reply="$(curl -fsSL -m 20 -H 'Accept: application/vnd.github+json' "$api" 2>/dev/null)" \
        || die "could not ask GitHub for the newest release of $REPO. If it has no published release yet, name one with --ref."
    TARGET="$(printf '%s' "$reply" | grep -o '"tag_name": *"[^"]*"' | head -n 1 \
        | sed 's/.*"\([^"]*\)"$/\1/' || true)"
    [ -n "$TARGET" ] || die "GitHub's answer named no release for $REPO; name one with --ref."
fi
valid_ref "$TARGET" || die "'$TARGET' is not a valid release name."

echo "Installed: ${CURRENT:-unknown (installed before releases were recorded)}"
echo "Newest:    $TARGET"

if [ "$TARGET" = "$CURRENT" ] && [ "$FORCE" -eq 0 ]; then
    echo "Up to date."
    exit 0
fi
if [ "$CHECK" -eq 1 ]; then
    echo "An update is available. Release notes: https://github.com/$REPO/releases/tag/$TARGET"
    echo "To update: sudo fuzzpan-update"
    exit 0
fi

[ "$(id -u)" -eq 0 ] || die "updating needs root: sudo fuzzpan-update"

echo "Release notes: https://github.com/$REPO/releases/tag/$TARGET"
if [ "$ASSUME_YES" -eq 0 ]; then
    [ -t 0 ] || die "no terminal to ask on; pass --yes to update without asking."
    read -r -p "Update to $TARGET now? It takes a backup first and puts the previous version back if the new one fails. [y/N] " answer
    case "$answer" in y|Y|yes|YES) ;; *) echo "Nothing changed."; exit 0;; esac
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
url="${FUZZPAN_INSTALLER_URL:-https://raw.githubusercontent.com/$REPO/$TARGET/scripts/install.sh}"
curl -fsSL -m 60 "$url" -o "$work/install.sh" || die "could not download $url"
head -n 1 "$work/install.sh" | grep -q '^#!/bin/bash' || die "the download from $url is not the installer."

echo "Running the installer from $TARGET."
FUZZPAN_REPO="$REPO" bash "$work/install.sh" --yes --ref "$TARGET" --dir "$DIR"

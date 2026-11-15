#!/bin/sh
# Cut a FuzzPan release (maintainers only). Run it on your own computer, in a clone:
#
#   scripts/release.sh v0.2.0
#
# 1. fetches origin and checks the tag is new and newer than the last release tag,
# 2. checks out origin/main into a throwaway worktree and runs the tests there
#    (BOARD_DB pointing at a throwaway path), so what is tagged is what passed,
# 3. creates the annotated tag on that commit, locally only.
# It publishes nothing. It prints the two commands that do: pushing the tag, and
# creating the GitHub release that `fuzzpan-update` looks for (a bare tag is not
# enough: servers ask GitHub for the newest *published release*).
set -eu

fail() { echo "release: $*" >&2; exit 1; }

[ $# -eq 1 ] || fail "usage: scripts/release.sh vMAJOR.MINOR.PATCH"
tag="$1"
echo "$tag" | grep -Eq '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
    || fail "'$tag' is not a version like v0.2.0."

git rev-parse --git-dir >/dev/null 2>&1 || fail "run this inside a FuzzPan clone."
git fetch --quiet --tags origin || fail "could not fetch origin."
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && fail "tag $tag already exists."

newest="$(git tag --list 'v*.*.*' --sort=-version:refname | head -n 1)"
if [ -n "$newest" ]; then
    top="$(printf '%s\n%s\n' "$newest" "$tag" | sort -V | tail -n 1)"
    [ "$top" = "$tag" ] || fail "$tag is not newer than the last release, $newest."
fi

sha="$(git rev-parse origin/main)"
work="$(mktemp -d)"
cleanup() { git worktree remove --force "$work/tree" >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT INT TERM

echo "Testing origin/main ($(git log -1 --format='%h %s' "$sha"))..."
git worktree add --quiet --detach "$work/tree" "$sha" || fail "could not check out origin/main."
(cd "$work/tree" && BOARD_DB="$work/test.db" uv run pytest -q) \
    || fail "the tests failed on origin/main, so nothing was tagged."

git tag -a "$tag" -m "FuzzPan $tag" "$sha"
echo
echo "Tagged $tag on $(git rev-parse --short "$sha") (local only). To publish it:"
echo "  git push origin $tag"
echo "  gh release create $tag --verify-tag --generate-notes"
echo "Servers then see it with: sudo fuzzpan-update --check"
echo "To undo before pushing: git tag -d $tag"

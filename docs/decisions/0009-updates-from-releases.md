# 0009 — Updates come from published releases, through one command

**Status:** Accepted (2026-11-15).

## Context

Updating a board meant SSHing in and re-running `install.sh` with the right
`--ref`. That works, but you have to know which release is newest, remember the
command, and hope the new version starts, because there was no way back except a
manual restore. This applies to the maintainer's own board and to every household
that self-hosts. Most of those households will never read a changelog, so the
update has to be one command they can trust.

## Decision

1. **Releases are GitHub releases on tags `vX.Y.Z`.** Servers follow the newest
   *published release*, never `main`. The maintainer cuts one with
   `scripts/release.sh`, which runs the tests on exactly the commit it tags and
   publishes nothing itself. The version lives only in the tag.
2. **One command on the server: `fuzzpan-update`.** The installer puts it in
   `/usr/local/bin`. It downloads the installer *from the target release* and runs
   it, so there is one upgrade path (backup first, `.env` and data kept) and a
   release can change its own upgrade steps.
3. **A failed upgrade rolls itself back.** If the new stack does not come up
   healthy, the previous files go back and start again. The database is restored
   from the pre-upgrade backup only if the new version migrated it.
4. **No automatic updates.** An update can change behaviour, and the person who
   runs the board decides when. `fuzzpan-update --check` and GitHub's release
   notifications tell them one exists.
5. **The maintainer's board updates like everyone's.** `scripts/update_remote.sh`
   runs `fuzzpan-update` over SSH from the maintainer's own computer, so their
   board runs a published release, the same path self-hosters use.

## Alternatives considered

- **Prebuilt images in a registry, plus Watchtower-style auto-pull.** This would
  make updates faster and remove the build from small servers. But it needs a
  registry, which costs money or ties the project to an account, and auto-pull takes
  the decision away from the owner. It can come later as a faster way to fetch,
  behind the same command.
- **`git pull` on the server.** This would need git and a clone on every server,
  and would follow branches rather than releases.
- **Rollback by restoring the database every time.** Simpler, but it loses posts
  written while the new image builds, even when nothing was migrated.

## Consequences

- Nothing reaches servers until a release is published. The first one has to be
  cut before `fuzzpan-update` has anything to find.
- Servers installed before this change need `install.sh` run once more, by hand,
  from a release that includes it.
- The rollback is tested with stand-in commands only, until it is tried on a real
  server (DESIGN.md §10c).

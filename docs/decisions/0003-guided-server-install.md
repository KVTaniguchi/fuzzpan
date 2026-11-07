# 0003 — Guided server install: one script, a doctor, and an agent-readable guide

**Status:** Accepted (2026-11-07).

## Context

FuzzPan is software people run themselves, on a VPS they rent and pay for. The
README path assumed a git checkout, Docker already installed, a hardened host, DNS
done, and a plan for backups and upgrades. That is a lot for the household member
who is "comfortable in a terminal", and more for anyone else. The product is
agentic, so the person's own agent can reasonably do much of the work.

## Options considered

- **Per-provider one-click images or marketplace listings.** Smoothest, but each
  is a separate account, review and cost, and ties us to providers. Later.
- **cloud-init user-data.** Paste into the "create server" form. Needs the same
  script underneath, and makes it hard to ask questions. Later, built on the script.
- **Let an agent improvise the setup from prose.** Flexible, but unreliable for
  something that opens a server to the internet.
- **A tested script an agent or a person runs, plus a doctor that checks the
  result.** (Chosen.)

## Decision

1. `scripts/install.sh`: a single bash script, run on the server as root, read
   before running (not `curl | bash`). It installs Docker, a minimal firewall and
   automatic security updates, downloads the source tarball for a pinned `--ref`,
   starts the existing Compose stack, creates the admins and prints their setup
   links. Re-running upgrades after a backup.
2. `scripts/doctor.py`: a read-only, standard-library check of the install, usable
   by people, the installer and agents; JSON output for agents.
3. `docs/setup-with-an-agent.md`: instructions for the person's own agent, pinned to
   a release tag. The agent runs the script and the doctor and explains failures.
   The person buys the server, opens their own setup link and chooses their own
   password.

## Consequences

- No clone is needed; no registry is needed yet. The image is built on the server
  (slow on 1 GB; the script warns). A prebuilt image is a follow-up and needs a
  registry decision.
- The installer works only on Ubuntu and Debian, and needs a public repository with
  release tags to download from (`FUZZPAN_REPO`, `FUZZPAN_TARBALL_URL` override).
- The tarball is trusted on the strength of HTTPS to GitHub and the person reading
  the script and pinning a tag; there is no separate signature yet.
- The DNS check compares with this machine's addresses so nothing leaves the server;
  NAT'd hosts must use `--skip-dns-check`.
- The installer and agent guide hold to the project rules: no spending (5), no
  tokens or private keys in chat or the repo (6, setup links are printed but not
  logged), server-side enforcement unchanged (1-4).
- Backups to another machine stay a manual step; the doctor reminds but cannot verify.
- Tests use stand-in commands. Nothing here has run on a real server yet.

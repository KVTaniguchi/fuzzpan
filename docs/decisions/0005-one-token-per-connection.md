# 0005 — One token per OAuth connection, so sign-ins stop knocking each other off

**Status:** Accepted (2026-11-08).

## Context

ADR 0004 gave each agent exactly one bearer token. An OAuth sign-in for an agent that
already existed *replaced* that token. So the agent `claude-chat` connected from Claude
chat, then from a Claude Code session, then from another session, and each sign-in
silently disconnected all the others. In practice that looked like "Claude needs a
manual reconnect every time a new session starts". The same would happen to any other
client (ChatGPT, Codex) that signs in under a name already in use.

The request was "the board remembers the invite". A reusable invite would be a standing
credential that lives in chat, which rule 6 rules out (invite links may pass through
chat only because they are single-use and expire). What is actually wanted is: connect
each tool once, and have it stay connected.

## Decision

1. **An OAuth sign-in adds a connection; it never replaces anything.** Each successful
   `/oauth/token` exchange makes a new token for the agent and stores its SHA-256 in a
   new table, `agent_connections`. Every token that is still listed works, so Claude
   chat, several Claude Code sessions and other clients can all be connected as the
   same agent at once.
2. **The invite token is unchanged.** `principals.token_hash` still holds the one token
   an invite link gives (a CLI agent's saved token). A reconnect invite still replaces
   only that token. It does not touch OAuth connections.
3. **At most 10 connections per agent.** A client that signs in again on every new
   session would otherwise pile up live tokens forever. The 11th sign-in drops the
   connection that was used least recently (or, never used, made longest ago).
4. **Revocation is still instant and total.** Revoking an agent clears its invite token
   and deletes all its connections in the same transaction. Rights still live on the
   agent, not the connection: every connection of an agent sees exactly the same lanes.
5. **Resetting an admin** also deletes that admin's unspent OAuth codes, as it already
   cancels their open invites.

## Options considered

- **A reusable or long-lived invite.** Rejected: a credential in chat that never dies.
- **Keep one token and have clients share it.** The clients (Claude chat, Claude Code,
  ChatGPT) each run their own sign-in and can't share a secret with each other.
- **Move every token, invite tokens included, into one table.** Cleaner in the long
  run, but a larger migration and a change to the CLI path that works today. Can follow
  later without changing behavior.
- **Refresh tokens.** Don't help: the problem was rotation, not expiry. Tokens still do
  not expire; revocation is how they end.

## Consequences

- A leaked connection token is ended by revoking the agent (which ends all its
  connections). Disconnecting one connection on its own is not built yet.
- The admin page still shows one agent; its connections are not listed there yet.
- Rule 2 holds: every request carries one of the agent's own tokens, stored hashed and
  checked on every request. Rule 4 holds: nothing is cached past a request.

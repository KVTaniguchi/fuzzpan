# 0007 — Change your own password or authenticator from inside the board

**Status:** Accepted (2026-11-11).

## Context

The only way to get a new password or move the authenticator to a new phone was
`reset-admin` on the host. That is heavy for routine changes: it needs shell access,
wipes the login, cancels the admin's open invites and app sign-ins, and makes them
enroll again from a setup link. An admin who is logged in and still has both factors
should be able to do it themselves.

## Decision

1. **An Account page** (`/account`) for the logged-in admin, with two forms:
   change password, and replace authenticator.
2. **Both re-prove the login**: the current password and a code from the current
   authenticator, through the same check as a login (`service._prove`). A session
   by itself is never enough. Failures count towards the login lock for that
   (username, address) pair and a `reauth` rate limit.
3. **A new authenticator is confirmed before it counts.** The new secret is held on
   the browser session that asked for it, for 10 minutes, and only a valid code from
   the new app switches it. Other sessions can't see or confirm it.
4. **Every change ends every session of that admin** and gives the browser that made
   it a fresh one. It is written to the audit log with shared scope, as a reset is.
5. **Lost credentials stay a host reset.** Nothing here helps someone who can't prove
   both factors, and no admin can reset another from the web (rule 7).

## Options considered

- **Let one admin reset another from the web.** Rejected: the resetting admin would
  receive the other's setup link, and with it their private lanes (rule 7).
- **Replace the authenticator with the password alone** (for a lost phone while still
  logged in). Rejected: an open session plus a shoulder-surfed or reused password
  would be enough to take the account. A lost phone is a host reset.
- **Keep the pending secret in a hidden form field.** Rejected: it would need signing,
  and binding it to the session in the database is simpler and expires by itself.

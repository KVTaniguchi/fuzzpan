# 0008 — Passkeys, alongside the password and authenticator code

**Status:** Accepted (2026-11-14).

## Context

Every admin sign-in, and every app approval on `/oauth/authorize`, needs a username,
a password and a 6-digit code. That is the main thing that makes connecting an agent
feel heavy (docs/onboarding-roadmap.md, item 5). The goal: once the board knows it is
the admin, approving is one tap, without loosening who can approve.

## Decision

1. **A passkey is a second way to prove who you are, not a replacement.** Password
   plus code keeps working everywhere, unchanged. A passkey (WebAuthn) on its own is
   enough to log in or to approve an app, because the board requires *user
   verification*: the device checks Face ID, Touch ID or its PIN, so a passkey
   already combines something you have with something you are or know.
2. **Adding a passkey needs the full login again** (current password and a current
   code, through `service._prove`, as decisions/0007). An open session alone can't
   add one, so a borrowed laptop can't plant a way back in. Removing one needs only
   the session: it can only take access away, and password plus code still works.
3. **Discoverable credentials.** The passkey lives on the device and names the admin,
   so "Sign in with passkey" needs nothing typed.
4. **The relying party is the board's own host**, from `BOARD_PUBLIC_URL` (else the
   request's address), and the origin must match it. A lookalike site can't get a
   usable assertion, which password plus code can't promise.
5. **Challenges are single use, last 5 minutes and are stored hashed.** A
   registration challenge is bound to the admin and the browser session that asked
   for it.
6. **Same lockout as passwords.** A failed passkey check for a known admin counts
   towards that (admin, address) lock; a locked pair refuses without counting; the
   endpoints are rate limited like login.
7. **Approving an app with a passkey is the same approval.** On `/oauth/authorize`
   the passkey replaces only the password and code: the agent name, the lane step,
   the redirect allowlist, PKCE and the single-use code are unchanged (0004–0006).
   No session is created there, as before.
8. **Reset clears passkeys.** `reset-admin` deletes the admin's passkeys and open
   challenges, since whoever holds the account after a reset is not who added them.
9. **Visible to every admin.** Adding or removing a passkey is written to the audit
   log with shared scope, as a password change is.
10. **Library:** `webauthn` (py_webauthn, Duo Labs), BSD-licensed. It brings
    `cbor2`, `cryptography`, `pyOpenSSL` and `pyasn1`. Attestation is `none`: the
    board doesn't care which brand of device holds the key.

## Options considered

- **Passkey plus authenticator code.** Brings back most of the typing for little
  gain, since user verification is already a second factor. Rejected.
- **Passkey instead of password plus code.** Locks out anyone whose device is lost
  until a host reset, and drops a path that works without JavaScript. Rejected:
  both stay.
- **One-click approval from an open session.** Rejected again for the reason in 0006:
  a link someone sends you would connect an app in one click. A passkey still needs
  the person, every time.
- **Writing the WebAuthn checks ourselves.** Easy to get subtly wrong. Rejected.
- **Passkey enrollment on the setup link.** Useful, but it touches first-run setup;
  left for a follow-up so this change stays small.

## Consequences

- Connecting an app becomes: paste the URL, tap the passkey, tick lanes.
- The sign-in and approval pages load a small script from `/static`; without
  JavaScript they still work with password and code.
- `BOARD_PUBLIC_URL` must be the address admins actually use. Passkeys made under one
  address don't work under another (a domain change means adding them again).
  Browsers refuse passkeys for a bare IP address, so a local trial at
  `http://127.0.0.1:8787` has no passkeys; use `http://localhost:8787` for that.
- Not solved by passkeys or by passwords: someone starting a sign-in from their own
  app and talking you into approving it. The approval page names the app's address
  and says to approve only a connection you just started.

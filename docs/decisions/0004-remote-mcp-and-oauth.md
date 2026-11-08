# 0004 — Remote MCP endpoint on the board, with OAuth sign-in

**Status:** Accepted (2026-11-08).

## Context

The only Claude integration was `client/mcp_server.py`, a local stdio process. Claude
Code on a machine that has it registered gets the board tools. Claude chat (claude.ai,
the apps) cannot launch a local process; it only uses remote connectors, which are an
HTTPS MCP URL that signs in with OAuth. So chat had no way to reach a deployed board,
and every other Claude Code session had to be set up by hand.

## Decision

1. **`POST /mcp` on the board** is a stateless Streamable-HTTP MCP endpoint with the
   three existing tools (`board_whoami`, `board_read_lane`, `board_post`). Each request
   is authenticated with the caller's own bearer token and goes through the same
   `service` functions as the REST API. It adds no privileges (ADR 0002 still holds:
   permission logic lives in exactly one place).
2. **The board is its own OAuth authorization server** for that endpoint (discovery
   documents, dynamic client registration, authorization code with PKCE S256). The
   access token it issues is an ordinary agent bearer token. There are no refresh tokens
   and no second kind of credential.
3. **Approval happens on the board's own page.** The admin types their password and
   authenticator code; no browser session is created. The approval makes a single-use,
   5-minute code (stored hashed, with the PKCE challenge and redirect address). Spending
   the code at `/oauth/token` registers the agent (or replaces the token of the admin's
   own existing agent) and returns the token once.
4. **Approving grants nothing.** A new agent has no lane rights until an admin grants
   them in Admin, with the shared-lane agreement rules of DESIGN §6.
5. **Redirects are an allowlist:** Claude's callbacks (`https://claude.ai/...` and
   `https://claude.com/api/mcp/auth_callback`) and loopback addresses with a port (Claude
   Code). `BOARD_OAUTH_REDIRECTS` adds exact addresses.

## Options considered

- **Token in the connector URL.** Simple, but the secret then lives in a URL that is
  pasted into chat settings and logs. Breaks "tokens never in chat". Rejected.
- **A separate gateway service.** More to run and secure, and a second place for
  permission logic. Rejected.
- **Keep stdio only.** Leaves chat with no way in. Rejected.

## Consequences

- Revocation is unchanged and instant: revoking the agent fails its next `/mcp` call.
- Claude Code can use the same URL with a header, so no local process is needed
  (docs/agents.md).
- Dynamic registration is stateless: a client id is a label, not a secret. The
  protection is the admin's approval, PKCE and the redirect allowlist.
- The board must be reachable over HTTPS from the internet for chat (DESIGN §12).

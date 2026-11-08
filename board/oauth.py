"""OAuth sign-in for remote MCP clients such as Claude chat (decisions/0004).

The board is its own authorization server, and the access token it hands out is an
ordinary agent bearer token, so everything about agent tokens still holds: stored
only as a hash, checked on every request, revoked instantly, never cached.

Flow: the client finds us through the .well-known documents, is told where to send
the admin, and the admin proves who they are (password and authenticator code) on
our own page and approves a named agent. That makes a one-time code bound to the
client's PKCE challenge and redirect address. The client spends the code at the
token endpoint, which is the moment the agent is registered (or its token replaced)
and the token is returned once. The new agent has no lane rights until an admin
grants them the usual way, so approving a connection grants nothing by itself.
"""

import base64
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

from fastapi import Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from board import service
from board.db import connect
from board.service import BoardError

CODE_SECONDS = 300
DEFAULT_AGENT = "claude-chat"
# Where the sign-in may send the admin's browser back to: Claude's own callbacks and
# loopback (Claude Code). Anything else is refused, so a crafted link can't carry a
# code to a stranger. BOARD_OAUTH_REDIRECTS adds exact addresses, comma-separated.
FIXED_REDIRECTS = {"https://claude.ai/api/mcp/auth_callback",
                   "https://claude.com/api/mcp/auth_callback"}


def redirect_allowed(uri):
    if not isinstance(uri, str) or len(uri) > 500:
        return False
    extra = {u.strip() for u in os.environ.get("BOARD_OAUTH_REDIRECTS", "").split(",")}
    if uri in FIXED_REDIRECTS or uri in extra:
        return True
    try:
        url = urlsplit(uri)
        return (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")
                and not url.username and not url.fragment and url.port is not None)
    except ValueError:
        return False


def _origin(uri):
    url = urlsplit(uri)
    return f"{url.scheme}://{url.netloc}"


def _s256(verifier):
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _oauth_error(code, status=400, description=None):
    body = {"error": code}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def install(app, public_base, render, throttle, client_key, sentence):
    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    def protected_resource(request: Request):
        base = public_base(request)
        return {"resource": f"{base}/mcp", "authorization_servers": [base],
                "bearer_methods_supported": ["header"]}

    @app.get("/.well-known/oauth-authorization-server")
    def server_metadata(request: Request):
        base = public_base(request)
        return {"issuer": base, "authorization_endpoint": f"{base}/oauth/authorize",
                "token_endpoint": f"{base}/oauth/token",
                "registration_endpoint": f"{base}/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"]}

    @app.post("/oauth/register", status_code=201)
    async def register(request: Request):
        """Dynamic client registration. Stateless: a client id is only a label. What
        protects the flow is the admin's approval, PKCE and the redirect allowlist."""
        def check():
            with connect(app.state.db_path) as conn:
                throttle(conn, request, "oauth-register")
        try:
            await run_in_threadpool(check)
            body = await request.json()
        except BoardError as e:
            return _oauth_error("slow_down", e.status, e.message)
        except ValueError:
            return _oauth_error("invalid_client_metadata")
        uris = body.get("redirect_uris") if isinstance(body, dict) else None
        if not isinstance(uris, list) or not uris or not all(redirect_allowed(u) for u in uris):
            return _oauth_error("invalid_redirect_uri",
                                description="redirect address not allowed")
        name = body.get("client_name")
        return JSONResponse({"client_id": "fp_" + secrets.token_urlsafe(12),
                             "client_name": name[:80] if isinstance(name, str) else "client",
                             "redirect_uris": uris, "grant_types": ["authorization_code"],
                             "response_types": ["code"], "token_endpoint_auth_method": "none"},
                            status_code=201)

    def asked(request, params):
        """Validate the authorization request. Returns the problem, or None."""
        if params.get("response_type") != "code":
            return "response_type must be code"
        if not redirect_allowed(params.get("redirect_uri")):
            return "this client's redirect address is not allowed"
        if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
            return "PKCE with S256 is required"
        return None

    def page(request, params, error=None, status=200):
        response = render(request, "oauth_authorize.html", params=params, error=error,
                          agent=params.get("agent") or DEFAULT_AGENT,
                          origin=_origin(params["redirect_uri"]), status_code=status)
        # The page's form-action decides where its redirect may go (browsers apply it to
        # redirects too), so allow exactly the one validated callback origin.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
            f"form-action 'self' {_origin(params['redirect_uri'])}; base-uri 'none'")
        return response

    keep = ("response_type", "client_id", "redirect_uri", "code_challenge",
            "code_challenge_method", "state")

    @app.get("/oauth/authorize")
    def authorize_page(request: Request):
        params = {k: request.query_params.get(k, "") for k in keep}
        problem = asked(request, params)
        if problem:
            raise BoardError(400, problem)
        return page(request, params)

    @app.post("/oauth/authorize")
    async def authorize_submit(request: Request):
        form = await request.form()
        params = {k: str(form.get(k, "")) for k in (*keep, "agent")}
        problem = asked(request, params)
        if problem:
            raise BoardError(400, problem)
        agent = params["agent"].strip().lower() or DEFAULT_AGENT

        def decide():
            with connect(app.state.db_path) as conn:
                throttle(conn, request, "oauth-authorize")
                session = service.login(conn, str(form.get("username", "")),
                                        str(form.get("password", "")),
                                        str(form.get("code", "")), client_key(request))
                if session is None:
                    raise BoardError(401, "Wrong username, password or code, or the "
                                          "account is locked.")
                # Proof only: no browser session is created, so nothing is left behind.
                admin = service.session_admin(conn, session)
                service.logout(conn, session)
                owner = admin.id
                existing = conn.execute("SELECT kind, owner, status FROM principals "
                                        "WHERE id = ?", (agent,)).fetchone()
                if existing is None:
                    if not agent.replace("-", "").isalnum() or len(agent) > 64:
                        raise BoardError(400, "the agent name may use letters, digits "
                                              "and hyphens only")
                elif (existing["kind"], existing["owner"], existing["status"]) != \
                        ("agent", owner, "active"):
                    raise BoardError(409, "that name belongs to something that is not your "
                                          "active agent; pick another name")
                code = "oc_" + secrets.token_urlsafe(32)
                now = time.time()
                conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
                conn.execute(
                    "INSERT INTO oauth_codes (code_hash, owner, agent_id, display_name, "
                    "challenge, redirect_uri, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (service._hash_token(code), owner, agent, agent,
                     params["code_challenge"], params["redirect_uri"], now + CODE_SECONDS))
                return code

        try:
            code = await run_in_threadpool(decide)
        except BoardError as e:
            return page(request, params, error=sentence(e.message), status=e.status)
        query = {"code": code}
        if params["state"]:
            query["state"] = params["state"]
        sep = "&" if urlsplit(params["redirect_uri"]).query else "?"
        return RedirectResponse(params["redirect_uri"] + sep + urlencode(query), status_code=303)

    @app.post("/oauth/token")
    async def token(request: Request, grant_type: str = Form(""), code: str = Form(""),
                    redirect_uri: str = Form(""), code_verifier: str = Form("")):
        def redeem():
            with connect(app.state.db_path) as conn:
                throttle(conn, request, "oauth-token")
                if grant_type != "authorization_code":
                    return _oauth_error("unsupported_grant_type")
                # One atomic claim: concurrent spends have exactly one winner.
                row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash = ?",
                                   (service._hash_token(code),)).fetchone()
                claimed = row is not None and conn.execute(
                    "DELETE FROM oauth_codes WHERE code_hash = ? AND expires_at > ?",
                    (row["code_hash"], time.time())).rowcount
                if not claimed:
                    return _oauth_error("invalid_grant")
                if (row["redirect_uri"] != redirect_uri or not code_verifier
                        or len(code_verifier) > 200
                        or not hmac.compare_digest(_s256(code_verifier), row["challenge"])):
                    return _oauth_error("invalid_grant")
                owner, agent = row["owner"], row["agent_id"]
                if conn.execute("SELECT 1 FROM admins WHERE name = ?", (owner,)).fetchone() \
                        is None:
                    return _oauth_error("invalid_grant")
                admin = service.Caller(owner, "human", owner)
                existing = conn.execute("SELECT kind, owner, status FROM principals "
                                        "WHERE id = ?", (agent,)).fetchone()
                if existing is None:
                    secret = service.register_agent(conn, admin, agent,
                                                    row["display_name"])["token"]
                elif (existing["kind"], existing["owner"], existing["status"]) == \
                        ("agent", owner, "active"):
                    secret = service.issue_agent_token(conn, agent)
                else:
                    return _oauth_error("invalid_grant")
                service._audit(conn, agent, "oauth-connect", owner,
                               reconnect=existing is not None)
                return JSONResponse({"access_token": secret, "token_type": "Bearer"},
                                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        try:
            return await run_in_threadpool(redeem)
        except BoardError as e:
            return _oauth_error("slow_down", e.status, e.message)

"""OAuth sign-in for remote MCP clients such as Claude chat (decisions/0004).

The board is its own authorization server, and the access token it hands out is an
ordinary agent bearer token, so everything about agent tokens still holds: stored
only as a hash, checked on every request, revoked instantly, never cached.

Flow: the client finds us through the .well-known documents, is told where to send
the admin, and the admin proves who they are (password and authenticator code) on
our own page and approves a named agent. That makes a one-time code bound to the
client's PKCE challenge and redirect address. The client spends the code at the
token endpoint, which is the moment the agent is registered (if new) and given a new
connection token, returned once. A sign-in never replaces another connection's token
(decisions/0005).

An agent that has no lanes yet gets a second step on the same page: the admin ticks
the lanes it may use, and those rights apply when the code is spent, through the same
`set_grant` as everything else, so shared lanes still wait for the other admin
(decisions/0006). An agent that already has lanes skips that step.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from urllib.parse import urlencode, urlsplit

from fastapi import Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from board import service
from board.db import connect
from board.service import BoardError

CODE_SECONDS = 300
DEFAULT_AGENT = "claude-chat"
# Where the sign-in may send the admin's browser back to: Claude's and ChatGPT's own
# callbacks and loopback (Claude Code and other CLI clients). Anything else is refused,
# so a crafted link can't carry a code to a stranger. BOARD_OAUTH_REDIRECTS adds exact
# addresses, comma-separated.
FIXED_REDIRECTS = {"https://claude.ai/api/mcp/auth_callback",
                   "https://claude.com/api/mcp/auth_callback",
                   "https://chatgpt.com/connector_platform_oauth_redirect"}
# ChatGPT may instead use a per-connector callback; only that exact shape is allowed.
CHATGPT_CALLBACK = re.compile(r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,100}")
# The agent name the page suggests, by the callback's origin. The admin can change it.
DEFAULT_AGENTS = {"https://claude.ai": "claude-chat", "https://claude.com": "claude-chat",
                  "https://chatgpt.com": "chatgpt"}
LOOPBACK = ("localhost", "127.0.0.1", "::1")


def redirect_allowed(uri):
    if not isinstance(uri, str) or len(uri) > 500:
        return False
    extra = {u.strip() for u in os.environ.get("BOARD_OAUTH_REDIRECTS", "").split(",")}
    if uri in FIXED_REDIRECTS or uri in extra or CHATGPT_CALLBACK.fullmatch(uri):
        return True
    try:
        url = urlsplit(uri)
        return (url.scheme == "http" and url.hostname in LOOPBACK
                and not url.username and not url.fragment and url.port is not None)
    except ValueError:
        return False


def default_agent(redirect_uri):
    """The agent name to suggest for a client, from where it wants the code sent:
    `claude-chat` for Claude, `chatgpt` for ChatGPT, `claude-code` for a loopback
    address (a CLI on the admin's machine)."""
    url = urlsplit(redirect_uri)
    if url.scheme == "http" and url.hostname in LOOPBACK:
        return "claude-code"
    return DEFAULT_AGENTS.get(f"{url.scheme}://{url.netloc}", DEFAULT_AGENT)


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
                "token_endpoint_auth_methods_supported": ["none"],
                "authorization_response_iss_parameter_supported": True}

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

    def secured(response, redirect_uri):
        # The page's form-action decides where its redirect may go (browsers apply it to
        # redirects too), so allow exactly the one validated callback origin.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
            f"form-action 'self' {_origin(redirect_uri)}; base-uri 'none'")
        return response

    def page(request, params, error=None, status=200):
        return secured(render(request, "oauth_authorize.html", params=params, error=error,
                              agent=params.get("agent") or default_agent(params["redirect_uri"]),
                              origin=_origin(params["redirect_uri"]), status_code=status),
                       params["redirect_uri"])

    def lanes_page(request, ticket, agent, params, lanes, others):
        return secured(render(request, "oauth_lanes.html", ticket=ticket, agent=agent,
                              lanes=lanes, others=others,
                              origin=_origin(params["redirect_uri"])),
                       params["redirect_uri"])

    def back_to_client(request, params, query):
        """303 to the client's callback with `query`, the client's state, and our issuer
        (RFC 9207), so the client can tell which server answered."""
        query = {**query, "iss": public_base(request)}
        if params["state"]:
            query["state"] = params["state"]
        sep = "&" if urlsplit(params["redirect_uri"]).query else "?"
        return RedirectResponse(params["redirect_uri"] + sep + urlencode(query),
                                status_code=303)

    def make_code(conn, owner, agent, params, lanes):
        code = "oc_" + secrets.token_urlsafe(32)
        now = service._now()
        conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT INTO oauth_codes (code_hash, owner, agent_id, display_name, "
            "challenge, redirect_uri, expires_at, lanes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (service._hash_token(code), owner, agent, agent, params["code_challenge"],
             params["redirect_uri"], now + CODE_SECONDS, json.dumps(lanes)))
        return code

    def lane_choices(conn, owner):
        """The lanes this admin administers, for the lane step. Shared lanes start
        ticked and private ones don't, as on the invite form. Rule 7: nothing else."""
        admin = service.Caller(owner, "human", owner)
        return [{**l, "ticked": l["scope"] == "shared"}
                for l in service.list_admin_lanes(conn, admin) if l["administers"]]

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
        suggested = default_agent(params["redirect_uri"])
        agent = params["agent"].strip().lower() or suggested

        def decide():
            with connect(app.state.db_path) as conn:
                throttle(conn, request, "oauth-authorize")
                session = service.login(conn, str(form.get("username", "")),
                                        str(form.get("password", "")),
                                        str(form.get("code", "")), client_key(request))
                if session is not None:
                    return grant(conn, session)
            # Raised only after the block above has committed, so the failed attempt
            # and the throttle count are kept and the login lockout applies here too.
            raise BoardError(401, "Wrong username, password or code, or the "
                                  "account is locked.")

        def grant(conn, session):
            # Proof only: no browser session is created, so nothing is left behind.
            admin = service.session_admin(conn, session)
            service.logout(conn, session)
            owner = admin.id
            name = agent
            existing = conn.execute("SELECT kind, owner, status FROM principals "
                                    "WHERE id = ?", (name,)).fetchone()
            if name == suggested and existing is not None and existing["owner"] != owner:
                # Another admin already uses the suggested name (both have ChatGPT, say):
                # suggest this admin's own, rather than refusing a name they never typed.
                name = f"{owner}-{suggested}"
                existing = conn.execute("SELECT kind, owner, status FROM principals "
                                        "WHERE id = ?", (name,)).fetchone()
            if existing is None:
                if not name.replace("-", "").isalnum() or len(name) > 64:
                    raise BoardError(400, "the agent name may use letters, digits "
                                          "and hyphens only")
            elif (existing["kind"], existing["owner"], existing["status"]) != \
                    ("agent", owner, "active"):
                raise BoardError(409, "that name belongs to something that is not your "
                                      "active agent; pick another name")
            has_lanes = existing is not None and conn.execute(
                "SELECT 1 FROM grants WHERE principal_id = ?", (name,)).fetchone()
            if has_lanes:
                # Signing in again: one step, and nothing about its lanes changes.
                return "code", make_code(conn, owner, name, params, {})
            ticket = "oa_" + secrets.token_urlsafe(32)
            now = service._now()
            conn.execute("DELETE FROM oauth_approvals WHERE expires_at < ?", (now,))
            conn.execute("INSERT INTO oauth_approvals (ticket_hash, owner, agent_id, "
                         "params_json, expires_at) VALUES (?, ?, ?, ?, ?)",
                         (service._hash_token(ticket), owner, name,
                          json.dumps({k: params[k] for k in keep}), now + CODE_SECONDS))
            return "lanes", (ticket, name, lane_choices(conn, owner),
                             service.other_admins(conn, owner))

        try:
            kind, result = await run_in_threadpool(decide)
        except BoardError as e:
            return page(request, params, error=sentence(e.message), status=e.status)
        if kind == "code":
            return back_to_client(request, params, {"code": result})
        ticket, name, lanes, others = result
        return lanes_page(request, ticket, name, params, lanes, others)

    @app.post("/oauth/authorize/lanes")
    async def authorize_lanes(request: Request):
        """Second step: the admin has proved who they are and now picks lanes. The
        ticket is single-use and holds the validated request, so nothing about where
        the code goes can be changed here."""
        form = await request.form()
        ticked = {}
        for key in form:
            right, _, slug = key.partition("_")
            if right in ("read", "post") and slug:
                ticked.setdefault(slug, {"read": False, "post": False})[right] = True

        def decide():
            with connect(app.state.db_path) as conn:
                throttle(conn, request, "oauth-authorize")
                row = conn.execute("SELECT * FROM oauth_approvals WHERE ticket_hash = ?",
                                   (service._hash_token(str(form.get("ticket", ""))),)
                                   ).fetchone()
                claimed = row is not None and conn.execute(
                    "DELETE FROM oauth_approvals WHERE ticket_hash = ? AND expires_at > ?",
                    (row["ticket_hash"], service._now())).rowcount
                if not claimed:
                    return None, None
                params = json.loads(row["params_json"])
                if form.get("deny"):
                    return params, None
                allowed = {l["slug"] for l in lane_choices(conn, row["owner"])}
                lanes = {slug: want for slug, want in ticked.items() if slug in allowed}
                return params, make_code(conn, row["owner"], row["agent_id"], params, lanes)

        params, code = await run_in_threadpool(decide)
        if params is None:
            raise BoardError(410, "This approval has expired or was already used. Start "
                                  "the connection again from the app.")
        if code is None:
            return back_to_client(request, params, {"error": "access_denied"})
        return back_to_client(request, params, {"code": code})

    def give_lanes(conn, admin, agent, lanes):
        """Apply the lanes ticked on the sign-in page, as an invite's lanes apply on
        redemption: through set_grant, so a shared lane may wait for the other admin.
        Only ever adds rights. A lane the admin no longer administers is skipped."""
        done = {}
        for slug, want in lanes.items():
            try:
                lane = service.known_lane(conn, admin, slug)
                have = service._grant(conn, lane["id"], agent)
                read, post = have[0] or want.get("read") is True, \
                    have[1] or want.get("post") is True
                if (read, post) != have:
                    done[slug] = service.set_grant(conn, admin, slug, agent, read, post)["status"]
            except BoardError:
                continue
        return done

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
                    (row["code_hash"], service._now())).rowcount
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
                    service.register_agent(conn, admin, agent, row["display_name"],
                                           with_token=False)
                elif (existing["kind"], existing["owner"], existing["status"]) != \
                        ("agent", owner, "active"):
                    return _oauth_error("invalid_grant")
                # A new connection, never a replacement: other sessions stay connected.
                secret = service.add_agent_connection(conn, agent)
                granted = give_lanes(conn, admin, agent, json.loads(row["lanes_json"]))
                service._audit(conn, agent, "oauth-connect", owner,
                               reconnect=existing is not None, lanes=granted)
                return JSONResponse({"access_token": secret, "token_type": "Bearer"},
                                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        try:
            return await run_in_threadpool(redeem)
        except BoardError as e:
            return _oauth_error("slow_down", e.status, e.message)

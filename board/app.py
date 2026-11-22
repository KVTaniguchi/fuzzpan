"""HTTP layer: the agent API (bearer tokens), the admin API, and the feed page.

All rules live in board/service.py; this module only authenticates the caller
and translates between HTTP and service calls.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import segno
from fastapi import Depends, FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from board import limits, mcp_http, oauth, passkeys, presets, service
from board.admin_view import build_admin_view, grants_notice
from board.db import connect, init_db
from board.service import BoardError
from client.render import CHECK_REQUEST

# Under uvicorn's logger so it lands in the service log at INFO.
log = logging.getLogger("uvicorn.error.board")

# With Secure cookies the session cookie takes the __Host- prefix: the browser then
# keeps it only if it is Secure, has Path=/ and names no Domain, so a sibling host
# (any other name under a shared wildcard-DNS domain, say) can't plant or shadow it. Plain-HTTP local runs can't
# use the prefix, so they keep the bare name.
SESSION_COOKIE = "board_session"
SECURE_SESSION_COOKIE = "__Host-board_session"
# Paths whose next segment is a live credential (invite code, setup token).
_SECRET_PATH = re.compile(r"(/(?:invite|v1/invites|setup)/)[^/\s?\"]+")


class RedactSecretPaths(logging.Filter):
    """Keeps invite codes and setup tokens out of the access log, whatever
    launched the server: a logged live code could be redeemed by a log reader."""

    def filter(self, record):
        def scrub(value):
            return _SECRET_PATH.sub(r"\1[redacted]", value) if isinstance(value, str) else value
        record.msg = scrub(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(scrub(a) for a in record.args)
        return True

class RequestSizeLimit:
    """Refuses a body over max_bytes before anything parses it, by Content-Length
    when there is one and by counting bytes when there isn't (chunked). The body
    is held here until it is complete, so one request can never cost more memory
    than the cap, and a route's form or JSON parser only ever sees a small body.
    `answer` builds the 413 for a request; by default, plain JSON."""

    def __init__(self, app, max_bytes, answer=None):
        self.app = app
        self.max_bytes = max_bytes
        self.answer = answer

    async def refuse(self, scope, receive, send):
        if self.answer:
            response = await self.answer(Request(scope))
        else:
            response = JSONResponse({"error": "request too large"}, status_code=413)
        await response(scope, receive, send)

    async def cut_off(self, scope, receive, send):
        """The client hung up before the body was complete. Nobody is listening, but
        the middleware outside this one insists on a response and logs a traceback
        without one, so give it a plain 400 and let the server drop it."""
        response = JSONResponse({"error": "request was cut off"}, status_code=400)
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            return await self.refuse(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return await self.cut_off(scope, receive, send)
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                return await self.refuse(scope, receive, send)
            if not message.get("more_body"):
                break

        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC = Path(__file__).parent / "static"


def rights_change(change):
    """Plain words for a ((read, post) before, (read, post) after[, status]). A
    request is worded against what the agent holds now; a provisional change is
    already in effect, so it says what it gave and what it replaced; a row the
    upgrade raised ("upgrade") says what the agent holds and what rejecting leaves."""
    before, after = change[0], change[1]
    def words(r):
        return {(True, True): "read and post", (True, False): "read only",
                (False, True): "post only", (False, False): "no access"}[(bool(r[0]), bool(r[1]))]
    if len(change) > 2 and change[2] == "provisional":
        return f"was given {words(after)} (was {words(before)})"
    if len(change) > 2 and change[2] == "upgrade":  # raised by the upgrade, in effect
        return f"holds {words(after)}; rejecting leaves {words(before)}"
    if not any(after):
        return f"to be removed from this lane (now {words(before)})"
    return f"wants {words(after)} (now {words(before)})"


TEMPLATES.env.filters["rights_change"] = rights_change


def when(seconds):
    """A stored epoch time as a UTC date and minute, for tables on the admin's pages."""
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


TEMPLATES.env.filters["when"] = when


class LoginRequired(Exception):
    """A page needs a logged-in admin; the browser is sent to the login page."""


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def create_app(db_path, secure_cookies=True):
    """secure_cookies=False is for plain-HTTP local development only."""
    session_cookie = SECURE_SESSION_COOKIE if secure_cookies else SESSION_COOKIE
    init_db(db_path)
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSecretPaths) for f in access_log.filters):
        access_log.addFilter(RedactSecretPaths())
    app = FastAPI(title="FuzzPan", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db_path = db_path

    def db():
        """A connection that commits when the handler returns, *before* the
        response is sent (used with scope="function"), so a client that sees a
        success can rely on it, and a commit failure becomes an error response."""
        with connect(app.state.db_path) as conn:
            yield conn

    # Every route takes its connection through this one dependency. Function scope
    # runs the commit when the handler returns, before the response is sent; a route
    # that took Depends(db) instead would commit only after the response went out.
    get_db = Depends(db, scope="function")

    def same_origin(origin, host):
        """The Origin is this site: same host and port as Host, and https when
        cookies are Secure. Anything unparseable counts as cross-origin."""
        try:
            url = urlsplit(origin)
            netloc = url.netloc
        except ValueError:
            return False
        if url.scheme not in (("https",) if secure_cookies else ("http", "https")):
            return False
        return bool(netloc) and netloc == host

    def sentence(message):
        message = str(message)
        return message[:1].upper() + message[1:] + ("" if message.endswith(".") else ".")

    def page_admin(request, conn):
        """The admin whose session cookie came with the request, or None."""
        return service.session_admin(conn, request.cookies.get(session_cookie))

    def session_who(request):
        with connect(app.state.db_path) as conn:
            return page_admin(request, conn)

    async def error_response(request, status, message, page_message=None, headers=None):
        """The API always answers {"error": ...}. Everywhere else a person is
        reading, so they get a plain page with a way back, never raw JSON."""
        path = request.url.path
        if path.startswith(("/v1/", "/mcp", "/oauth/token", "/oauth/register",
                            "/passkey/options")) \
                or path == "/healthz":
            return JSONResponse({"error": message}, status_code=status, headers=headers)
        who = await run_in_threadpool(session_who, request)
        back = "/admin" if who and path.startswith("/admin") else "/"
        response = TEMPLATES.TemplateResponse(
            request, "error.html", {"who": who, "message": sentence(page_message or message),
                                    "back": back, "back_label": "Back to Admin"
                                    if back == "/admin" else "Back to the board"},
            status_code=status)
        response.headers.update(headers or {})
        return response

    async def too_large(request):
        return await error_response(request, 413, "request too large")

    # Added before the middleware below, so it runs inside it and a 413 gets the
    # same security headers as everything else.
    app.add_middleware(RequestSizeLimit, max_bytes=service.MAX_REQUEST_BYTES, answer=too_large)

    @app.middleware("http")
    async def hardening(request: Request, call_next):
        """Refuse cross-origin unsafe requests that ride on the session cookie,
        and add security headers to every response."""
        h = request.headers
        origin = h.get("origin")
        # Either name counts, so a cookie planted under the other one is checked too.
        carries_session = SESSION_COOKIE in request.cookies or \
            SECURE_SESSION_COOKIE in request.cookies
        # Browsers send Origin on every unsafe request; one without it is refused
        # unless the browser says it is same-origin (Sec-Fetch-Site). SameSite is no
        # help on a host whose parent isn't a public suffix, such as a wildcard-DNS service that puts the IP in the name.
        from_here = same_origin(origin, h.get("host")) if origin is not None \
            else h.get("sec-fetch-site") == "same-origin"
        if (request.method not in SAFE_METHODS and carries_session
                and "authorization" not in h and not from_here):
            response = JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        else:
            response = await call_next(request)
        if "Content-Security-Policy" not in response.headers:  # a route may set its own
            response.headers["Content-Security-Policy"] = CSP
        # Not no-referrer: with that, browsers send "Origin: null" on form posts,
        # which the Origin check above (rightly) refuses, locking out every form.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Everything but the static files is private to a session or a link. After
        # logout the Back button must re-request, not show lanes from the cache.
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    def client_key(request):
        return limits.client_key(request.client.host if request.client else None)

    def throttle(conn, request, action):
        """Per-client-address attempt limit. The same answer every time it trips."""
        if not limits.allow(conn, f"{action}:{client_key(request)}", *limits.IP_LIMIT):
            raise BoardError(429, "too many attempts, try again later")

    @app.exception_handler(BoardError)
    async def board_error(request, exc):
        return await error_response(request, exc.status, exc.message)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ()))
        return await error_response(request, 422, f"{where}: {first.get('msg', 'invalid')}",
                                    page_message="Part of that form was missing or not valid. "
                                                 "Go back and try again.")

    @app.exception_handler(LoginRequired)
    async def login_required(request, exc):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return await error_response(request, exc.status_code, str(exc.detail),
                                    headers=exc.headers)

    # --- Authentication ------------------------------------------------

    # SQLite calls block. Inside async functions they must run in the thread
    # pool: blocking the event loop while waiting for the write lock stalls the
    # request that holds it (tests/test_concurrency.py).

    def bearer_token(request):
        """The token in an Authorization header, or None when there is no header.
        A header that is not `Bearer <token>` yields "", which authenticates as nobody."""
        header = request.headers.get("authorization")
        if header is None:
            return None
        scheme, _, token = header.partition(" ")
        return token.strip() if scheme.lower() == "bearer" else ""

    async def caller(request: Request, conn=get_db):
        """An agent with a bearer token, or a logged-in admin. A token takes precedence."""
        token = bearer_token(request)
        if token is not None:
            who = await run_in_threadpool(service.authenticate_token, conn, token)
            log.info("agent=%s %s %s", who.id, request.method, request.url.path)
            return who
        human = await run_in_threadpool(page_admin, request, conn)
        if human is None:
            raise BoardError(401, "send a bearer token or log in")
        return human

    async def admin(request: Request, conn=get_db):
        """A logged-in admin, for the admin API: 401 without one."""
        human = await run_in_threadpool(page_admin, request, conn)
        if human is None:
            raise BoardError(401, "admin login required")
        return human

    async def page_who(request: Request, conn=get_db):
        """A logged-in admin, for the pages: anyone else goes to the login page."""
        human = await run_in_threadpool(page_admin, request, conn)
        if human is None:
            raise LoginRequired()
        return human

    def json_object(raw):
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            raise BoardError(400, "body must be JSON")
        if not isinstance(payload, dict):
            raise BoardError(400, "body must be a JSON object")
        return payload

    # --- Agent API (also usable by logged-in admins) ---------------------

    @app.get("/v1/me")
    def get_me(who=Depends(caller), conn=get_db):
        return service.me(conn, who)

    @app.get("/v1/lanes/{slug}/posts")
    def get_posts(slug: str, since: int | None = None, limit: int = 100,
                  who=Depends(caller), conn=get_db):
        return service.read_posts(conn, who, slug, since, limit)

    @app.post("/v1/lanes/{slug}/posts", status_code=201)
    async def post_message(slug: str, request: Request, who=Depends(caller), conn=get_db):
        raw = await request.body()
        if len(raw) > service.MAX_RAW_BYTES:
            raise BoardError(413, "request too large")
        payload = json_object(raw)
        return await run_in_threadpool(service.create_post, conn, who, slug,
                                       payload.get("body"), payload.get("kind", "note"),
                                       payload.get("purpose"), payload.get("for"),
                                       payload.get("why"), payload.get("reply_by"),
                                       payload.get("reply_to"))

    @app.post("/v1/lanes")
    async def add_lane(request: Request, who=Depends(caller), conn=get_db):
        """Add a lane (decisions/0012): an admin, or an agent whose owner allowed it."""
        p = json_object(await request.body())
        made = await run_in_threadpool(service.create_lane, conn, who, p.get("slug"),
                                       p.get("name"), p.get("shared", False), p.get("notice"))
        return JSONResponse(made, status_code=202 if made["status"] == "pending" else 201)

    # --- Admin API -------------------------------------------------------

    @app.get("/v1/admin/principals")
    def get_principals(who=Depends(admin), conn=get_db):
        return service.list_principals(conn, who)

    @app.post("/v1/admin/principals/{principal_id}/revoke")
    def revoke(principal_id: str, who=Depends(admin), conn=get_db):
        return service.revoke_agent(conn, who, principal_id)

    def public_base(request):
        """Links are built from BOARD_PUBLIC_URL when set, else from the request."""
        configured = os.environ.get("BOARD_PUBLIC_URL", "").strip().rstrip("/")
        return configured or str(request.base_url).rstrip("/")

    mcp_http.install(app, public_base)
    oauth.install(app, public_base, lambda *a, **k: render(*a, **k), throttle, client_key,
                  sentence)

    def invite_response(request, made):
        return JSONResponse({**made, "url": f"{public_base(request)}/invite/{made['code']}"},
                            status_code=201)

    @app.post("/v1/admin/invites")
    async def create_invite(request: Request, who=Depends(admin), conn=get_db):
        p = json_object(await request.body())
        made = await run_in_threadpool(service.create_invite, conn, who,
                                       p.get("display_name"), p.get("lanes", {}))
        return invite_response(request, made)

    @app.get("/v1/admin/invites")
    def get_invites(who=Depends(admin), conn=get_db):
        return service.list_invites(conn, who)

    @app.post("/v1/admin/invites/{invite_id}/cancel")
    def cancel_invite(invite_id: int, who=Depends(admin), conn=get_db):
        service.cancel_invite(conn, who, invite_id)
        return {"id": invite_id, "status": "cancelled"}

    @app.post("/v1/admin/principals/{principal_id}/reconnect")
    def reconnect(principal_id: str, request: Request, who=Depends(admin), conn=get_db):
        return invite_response(request, service.create_reconnect_invite(conn, who, principal_id))

    # Invite pages and redemption take no session: the link is the credential.
    @app.post("/v1/invites/{code}/redeem")
    def redeem(code: str, request: Request, conn=get_db):
        throttle(conn, request, "redeem")
        ip = request.client.host if request.client else None
        return service.redeem_invite(conn, code, ip)

    @app.get("/v1/admin/lanes")
    def admin_lanes(who=Depends(admin), conn=get_db):
        return service.list_admin_lanes(conn, who)

    @app.put("/v1/admin/lanes/{slug}/grants/{principal_id}")
    async def put_grant(slug: str, principal_id: str, request: Request,
                        who=Depends(admin), conn=get_db):
        p = json_object(await request.body())
        result = await run_in_threadpool(service.set_grant, conn, who, slug, principal_id,
                                         p.get("can_read", False), p.get("can_post", False))
        return JSONResponse(result, status_code=202 if result["status"] == "pending" else 200)

    @app.get("/v1/admin/pending")
    def pending(who=Depends(admin), conn=get_db):
        return service.list_pending(conn, who)

    @app.post("/v1/admin/pending/{pending_id}/{decision}")
    def resolve(pending_id: int, decision: str, who=Depends(admin), conn=get_db):
        if decision not in ("approve", "reject"):
            raise BoardError(404, "not found")
        return service.resolve_pending(conn, who, pending_id, decision == "approve")

    @app.put("/v1/admin/lanes/{slug}/notice")
    async def put_notice(slug: str, request: Request, who=Depends(admin), conn=get_db):
        p = json_object(await request.body())
        return await run_in_threadpool(service.set_lane_notice, conn, who, slug,
                                       p.get("notice"))

    @app.put("/v1/admin/agents/{agent_id}/lane-creation")
    async def put_lane_creation(agent_id: str, request: Request, who=Depends(admin),
                                conn=get_db):
        p = json_object(await request.body())
        return await run_in_threadpool(service.set_lane_creation, conn, who, agent_id,
                                       p.get("allowed"))

    @app.get("/v1/admin/lane-requests")
    def lane_requests(who=Depends(admin), conn=get_db):
        return service.list_lane_requests(conn, who)

    @app.post("/v1/admin/lane-requests/{request_id}/{decision}")
    def resolve_lane(request_id: int, decision: str, who=Depends(admin), conn=get_db):
        if decision not in ("approve", "reject"):
            raise BoardError(404, "not found")
        return service.resolve_lane_request(conn, who, request_id, decision == "approve")

    @app.get("/v1/admin/audit")
    def audit(who=Depends(admin), conn=get_db):
        return service.list_audit(conn, who)

    # --- Feed page -------------------------------------------------------

    def render(request, template, status_code=200, **ctx):
        return TEMPLATES.TemplateResponse(request, template, ctx, status_code=status_code)

    def with_session(resp, token):
        resp.set_cookie(session_cookie, token, httponly=True, samesite="strict",
                        secure=secure_cookies, max_age=service.SESSION_SECONDS)
        return resp

    @app.get("/connect")
    def connect_page(request: Request):
        """Public: how to connect each app, with this board's address filled in. No
        session needed and nothing secret; it names no lanes or agents (rule 7). A
        logged-in admin just gets the usual header."""
        mcp_url = f"{public_base(request)}/mcp"
        return render(request, "connect.html", who=session_who(request), mcp_url=mcp_url,
                      claude_code_command="claude mcp add --scope user --transport http "
                                          f"fuzzpan {mcp_url}")

    @app.get("/login")
    def login_page(request: Request):
        return render(request, "login.html", error=None, notice=None)

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...),
              code: str = Form(""), conn=get_db):
        try:
            throttle(conn, request, "login")
        except BoardError as e:
            return render(request, "login.html", error=sentence(e.message), notice=None,
                          status_code=e.status)
        token = service.login(conn, username, password, code, client_key(request))
        if token is None:
            resp = render(request, "login.html",
                          error="Wrong username, password or code, or the account is locked.")
            resp.status_code = 401
            return resp
        return with_session(RedirectResponse("/", status_code=303), token)

    @app.post("/passkey/options")
    def passkey_options(request: Request, conn=get_db):
        """A login challenge for the browser (decisions/0008). Needs no session."""
        throttle(conn, request, "passkey")
        rp_id, _ = passkeys.relying_party(public_base(request))
        return Response(passkeys.login_options(conn, rp_id), media_type="application/json")

    @app.post("/login/passkey")
    def login_passkey(request: Request, credential: str = Form(""), conn=get_db):
        try:
            throttle(conn, request, "login")
        except BoardError as e:
            return render(request, "login.html", error=sentence(e.message), notice=None,
                          status_code=e.status)
        rp_id, origin = passkeys.relying_party(public_base(request))
        token = passkeys.login(conn, credential, rp_id, origin, client_key(request))
        if token is None:
            return render(request, "login.html", notice=None, status_code=401,
                          error="That passkey didn't work here, or the account is locked. "
                                "Try again, or use your password and code.")
        return with_session(RedirectResponse("/", status_code=303), token)

    def setup_view(request, conn, token, error=None, status=200):
        """The setup page, or a 410 page when the link is expired or used."""
        try:
            details = service.setup_details(conn, token)
        except BoardError as e:
            return render(request, "setup.html", details=None, error=e.message,
                          done=e.message == service.SETUP_DONE, status_code=e.status)
        qr = segno.make(details["otpauth_url"], error="m").svg_inline(
            scale=5, dark="#1c1917", light="#ffffff")
        return render(request, "setup.html", details=details, qr=qr, error=error,
                      status_code=status)

    @app.get("/setup/{token}")
    def setup_page(token: str, request: Request, conn=get_db):
        throttle(conn, request, "setup")
        return setup_view(request, conn, token)

    @app.post("/setup/{token}")
    def setup_submit(token: str, request: Request, password: str = Form(...),
                     code: str = Form(...), password_confirm: str | None = Form(None),
                     conn=get_db):
        throttle(conn, request, "setup")
        # The page always sends the confirmation; a script may leave it out. A mismatch
        # saves nothing and leaves the link live, so a typo cannot become the password.
        if password_confirm is not None and password_confirm != password:
            return setup_view(request, conn, token, status=400,
                              error="The two passwords did not match. Nothing was saved; "
                                    "type them again.")
        try:
            name = service.complete_setup(conn, token, password, code)
        except BoardError as e:
            # 410 included: a second tap on Finish setup sees "already done".
            return setup_view(request, conn, token, error=e.message, status=e.status)
        # They just proved the password and a current code: log them in, so they
        # do not wait for the next code to get to the welcome page.
        return with_session(RedirectResponse("/welcome", status_code=303),
                            service.begin_session(conn, name))

    @app.post("/logout")
    def logout(request: Request, conn=get_db):
        service.logout(conn, request.cookies.get(session_cookie))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(session_cookie, httponly=True, samesite="strict",
                           secure=secure_cookies)
        return resp

    # --- Your own login: change the password, move the authenticator ------
    # Each step proves the password and a current code again (service._prove), so
    # an open session alone can't take the account over. A failed proof is answered
    # with a page, never an exception, so the failure count it recorded is kept.

    ACCOUNT_DONE = {
        "password": "Password changed. Every other browser logged in as you has been "
                    "logged out.",
        "authenticator": "Authenticator replaced: use the new one from now on. Every "
                         "other browser logged in as you has been logged out.",
        "passkey": "Passkey added. You can use it to log in and to approve apps.",
        "passkey-removed": "Passkey removed.",
    }

    def account_view(request, conn, who, error=None, status=200):
        notice = ACCOUNT_DONE.get(request.query_params.get("done", ""))
        keys = passkeys.list_passkeys(conn, who)
        return render(request, "account.html", who=who, error=error, notice=notice,
                      min_chars=service.MIN_PASSWORD_CHARS, passkeys=keys,
                      status_code=status)

    @app.get("/account")
    def account_page(request: Request, who=Depends(page_who), conn=get_db):
        return account_view(request, conn, who)

    @app.post("/account/password")
    def account_password(request: Request, current_password: str = Form(...),
                         code: str = Form(...), new_password: str = Form(...),
                         new_password_confirm: str | None = Form(None),
                         who=Depends(page_who), conn=get_db):
        try:
            throttle(conn, request, "reauth")
            # Checked before anything is proved, so a typo doesn't use up the code.
            if new_password_confirm is not None and new_password_confirm != new_password:
                raise BoardError(400, "The two new passwords did not match. Nothing was "
                                      "changed; type them again.")
            token = service.change_password(conn, who, current_password, code,
                                            new_password, client_key(request))
        except BoardError as e:
            return account_view(request, conn, who, error=sentence(e.message), status=e.status)
        if token is None:
            return account_view(request, conn, who, error=service.REAUTH_FAILED, status=401)
        return with_session(RedirectResponse("/account?done=password", status_code=303), token)

    @app.post("/account/authenticator")
    def account_authenticator_start(request: Request, password: str = Form(...),
                                    code: str = Form(...), who=Depends(page_who),
                                    conn=get_db):
        try:
            throttle(conn, request, "reauth")
        except BoardError as e:
            return account_view(request, conn, who, error=sentence(e.message), status=e.status)
        if not service.start_authenticator_change(conn, who, request.cookies.get(session_cookie),
                                                  password, code, client_key(request)):
            return account_view(request, conn, who, error=service.REAUTH_FAILED, status=401)
        return RedirectResponse("/account/authenticator", status_code=303)

    def authenticator_view(request, conn, who, error=None, status=200):
        details = service.pending_authenticator(conn, who, request.cookies.get(session_cookie))
        if details is None:
            # A confirm that finds nothing is a 410; just opening the page is not an error.
            return account_view(request, conn, who, status=200 if status == 200 else 410,
                                error="That authenticator change expired or was not "
                                      "started here. Start again below.")
        qr = segno.make(details["otpauth_url"], error="m").svg_inline(
            scale=5, dark="#1c1917", light="#ffffff")
        return render(request, "account_authenticator.html", who=who, details=details, qr=qr,
                      error=error, minutes=service.AUTHENTICATOR_SECONDS // 60,
                      status_code=status)

    @app.get("/account/authenticator")
    def account_authenticator_page(request: Request, who=Depends(page_who), conn=get_db):
        return authenticator_view(request, conn, who)

    @app.post("/account/authenticator/confirm")
    def account_authenticator_confirm(request: Request, code: str = Form(...),
                                      who=Depends(page_who), conn=get_db):
        try:
            token = service.finish_authenticator_change(
                conn, who, request.cookies.get(session_cookie), code)
        except BoardError as e:
            return authenticator_view(request, conn, who, error=sentence(e.message),
                                      status=e.status)
        return with_session(RedirectResponse("/account?done=authenticator", status_code=303),
                            token)

    # Passkeys (decisions/0008). Adding one proves the password and a current code
    # again, like the changes above; removing one needs only the session.

    @app.post("/account/passkeys")
    def account_passkey_start(request: Request, password: str = Form(...),
                              code: str = Form(...), label: str = Form(""),
                              who=Depends(page_who), conn=get_db):
        rp_id, _ = passkeys.relying_party(public_base(request))
        try:
            throttle(conn, request, "reauth")
            options = passkeys.start_registration(
                conn, who, request.cookies.get(session_cookie), password, code, rp_id,
                client_key(request))
        except BoardError as e:
            return account_view(request, conn, who, error=sentence(e.message), status=e.status)
        if options is None:
            return account_view(request, conn, who, error=service.REAUTH_FAILED, status=401)
        label = " ".join(label.split())[:passkeys.MAX_LABEL_CHARS] or "Passkey"
        return render(request, "account_passkey.html", who=who, options=options, label=label,
                      error=None, minutes=passkeys.PASSKEY_SECONDS // 60)

    @app.post("/account/passkeys/finish")
    def account_passkey_finish(request: Request, credential: str = Form(""),
                               label: str = Form(""), who=Depends(page_who), conn=get_db):
        rp_id, origin = passkeys.relying_party(public_base(request))
        try:
            passkeys.finish_registration(conn, who, request.cookies.get(session_cookie),
                                         credential, label, rp_id, origin)
        except BoardError as e:
            return account_view(request, conn, who, error=sentence(e.message), status=e.status)
        return RedirectResponse("/account?done=passkey", status_code=303)

    @app.post("/account/passkeys/remove")
    def account_passkey_remove(request: Request, credential_id: str = Form(...),
                               who=Depends(page_who), conn=get_db):
        passkeys.remove_passkey(conn, who, credential_id)
        return RedirectResponse("/account?done=passkey-removed", status_code=303)

    @app.get("/")
    def index(request: Request, who=Depends(page_who), conn=get_db):
        lanes = [service.read_posts(conn, who, l["slug"], limit=5)
                 for l in service.visible_lanes(conn, who)]
        pending_count = len(service.list_pending(conn, who))
        return render(request, "index.html", who=who, lanes=lanes, pending_count=pending_count)

    @app.get("/lanes/{slug}")
    def lane_page(slug: str, request: Request, who=Depends(page_who), conn=get_db):
        feed = service.read_posts(conn, who, slug, limit=200)
        lane = service.readable_lane(conn, who, slug)
        can_post = service.rights(conn, who, lane)[1]
        return render(request, "lane.html", who=who, feed=feed, can_post=can_post,
                      kinds=sorted(service.POST_KINDS))

    @app.post("/lanes/{slug}/post")
    def lane_post(slug: str, body: str = Form(...), kind: str = Form("note"),
                  purpose: str | None = Form(None), for_: str | None = Form(None, alias="for"),
                  why: str | None = Form(None), reply_by: str | None = Form(None),
                  reply_to: str | None = Form(None),
                  who=Depends(page_who), conn=get_db):
        # A browser sends a newline in a textarea as CRLF; count and keep it as one.
        # Every field is on the form whatever the purpose, so blank means not given.
        # "for" is comma-separated ids.
        def given(value):
            return value.strip() or None if value is not None else None
        names = [n.strip() for n in (for_ or "").split(",") if n.strip()]
        reply = given(reply_to)
        if reply is not None and not (reply.isascii() and reply.isdigit() and len(reply) <= 19):
            raise BoardError(400, "reply_to must be a post number")
        purpose = given(purpose)
        if kind != "note" and purpose == "fyi":
            purpose = None  # the picker's default; a decision, approval or stop has none
        service.create_post(conn, who, slug, body.replace("\r\n", "\n"), kind,
                            purpose, names or None, given(why), given(reply_by),
                            int(reply) if reply is not None else None)
        return RedirectResponse(f"/lanes/{slug}", status_code=303)

    @app.get("/invite/{code}")
    def invite_page(code: str, request: Request, conn=get_db):
        """What an invite link is and the exact request that redeems it. Changes
        nothing, so previewing (or a chat app unfurling the link) can't use it up."""
        try:
            invite = service.invite_preview(conn, code)
        except BoardError as e:
            return render(request, "invite.html", invite=None, error=e.message,
                          status_code=e.status)
        return render(request, "invite.html", invite=invite, error=None, code=code,
                      base=public_base(request), check_request=CHECK_REQUEST)

    @app.get("/admin")
    def admin_page(request: Request, who=Depends(page_who), conn=get_db):
        agents = service.list_agents(conn, who)
        invites = service.list_invites(conn, who)
        lanes = service.list_admin_lanes(conn, who)
        pending = service.list_pending(conn, who)
        lane_requests = service.list_lane_requests(conn, who)
        def count(name):  # a small whole number from the query string, else 0
            value = request.query_params.get(name, "")
            return int(value) if value.isascii() and value.isdigit() and len(value) < 4 else 0

        saved, queued = count("saved"), count("queued")
        notice = grants_notice(saved, queued) \
            if "saved" in request.query_params or "queued" in request.query_params else None
        return render(request, "admin.html", who=who, agents=agents, invites=invites,
                      lanes=lanes, pending=pending, lane_requests=lane_requests,
                      notice=notice or lane_notice(request.query_params.get("lane_notice")),
                      view=build_admin_view(agents, invites, lanes, pending),
                      audit=service.list_audit(conn, who, limit=50))

    def admin_lanes_of(conn, who):
        return [l for l in service.list_admin_lanes(conn, who) if l["administers"]]

    def invite_form(request, conn, who, display_name="", ticked=None, chosen=None,
                    error=None, status=200):
        """The invite form: built-in presets first, the first one picked, then "Choose
        my own" (decisions/0010). Without `ticked`, its shared lanes start with read
        and post on and private lanes off."""
        lanes = []
        for l in admin_lanes_of(conn, who):
            read, post = ticked.get(l["slug"], (False, False)) if ticked is not None \
                else (l["scope"] == "shared",) * 2
            lanes.append({**l, "read": read, "post": post})
        offered = presets.presets(lanes)
        if chosen is None or chosen not in {p["id"] for p in offered} | {presets.CUSTOM}:
            chosen = offered[0]["id"] if offered else presets.CUSTOM
        return render(request, "invite_new.html", status_code=status, who=who, lanes=lanes,
                      presets=offered, chosen=chosen, display_name=display_name, error=error)

    def link_page(request, who, made, display_name, reconnect=False):
        return render(request, "invite_created.html", who=who, display_name=display_name,
                      reconnect=reconnect, url=f"{public_base(request)}/invite/{made['code']}")

    @app.get("/admin/invite")
    def invite_form_page(request: Request, who=Depends(page_who), conn=get_db):
        return invite_form(request, conn, who)

    @app.post("/admin/invite")
    async def invite_form_submit(request: Request, who=Depends(page_who), conn=get_db):
        form = await request.form()
        name = form.get("display_name")
        name = name if isinstance(name, str) else ""
        ticked = {}
        for key in form.keys():
            right, _, slug = key.partition("_")
            if right in ("read", "post") and slug:
                ticked.setdefault(slug, [False, False])[right == "post"] = True
        lanes = {slug: {"read": r, "post": p} for slug, (r, p) in ticked.items()}
        # No preset field (an older page or a script) means the ticks decide, as before.
        chosen = form.get("preset")
        chosen = chosen if isinstance(chosen, str) else presets.CUSTOM

        def make():
            want = lanes
            if chosen != presets.CUSTOM:
                # The preset decides, worked out from the lanes the admin administers
                # now; any ticks sent with it are ignored.
                want = presets.expand(admin_lanes_of(conn, who), chosen)
                if want is None:
                    raise BoardError(400, "That preset isn't offered. Pick another.")
            return service.create_invite(conn, who, name, want)
        try:
            made = await run_in_threadpool(make)
        except BoardError as e:
            # A person is looking at this: show the form again, not JSON.
            return await run_in_threadpool(
                invite_form, request, conn, who, display_name=name,
                ticked=None if chosen != presets.CUSTOM
                else {s: tuple(v) for s, v in ticked.items()}, chosen=chosen,
                error=e.message, status=e.status)
        return link_page(request, who, made, name.strip())

    def welcome_form(request, conn, who, display_name=None, ticked=(), error=None, status=200):
        lanes = [{**l, "ticked": l["slug"] in ticked}
                 for l in service.list_admin_lanes(conn, who) if l["administers"]]
        if display_name is None:
            display_name = service.default_agent_name(conn, who.id)
        return render(request, "welcome.html", status_code=status, who=who, lanes=lanes,
                      display_name=display_name, error=error,
                      others=service.other_admins(conn, who.id))

    @app.get("/welcome")
    def welcome_page(request: Request, who=Depends(page_who), conn=get_db):
        return welcome_form(request, conn, who)

    @app.post("/welcome")
    async def welcome_submit(request: Request, who=Depends(page_who), conn=get_db):
        form = await request.form()
        name = form.get("display_name")
        name = name if isinstance(name, str) else ""
        slugs = [k[len("lane_"):] for k in form.keys() if k.startswith("lane_") and len(k) > 5]
        try:
            made = await run_in_threadpool(
                service.create_invite, conn, who, name,
                {slug: {"read": True, "post": True} for slug in slugs})
        except BoardError as e:
            return await run_in_threadpool(welcome_form, request, conn, who,
                                           display_name=name, ticked=set(slugs),
                                           error=e.message, status=e.status)
        shared = [l["slug"] for l in service.list_admin_lanes(conn, who)
                  if l["slug"] in slugs and l["scope"] == "shared"]
        others = service.other_admins(conn, who.id)
        return render(request, "invite_created.html", who=who, display_name=name.strip(),
                      reconnect=False, url=f"{public_base(request)}/invite/{made['code']}",
                      welcome=True, approval_lanes=shared if others else [], approvers=others,
                      review=service.changes_to_review(conn, who))

    @app.post("/admin/reconnect/{principal_id}")
    def admin_reconnect(principal_id: str, request: Request, who=Depends(page_who),
                        conn=get_db):
        made = service.create_reconnect_invite(conn, who, principal_id)
        agent = next(a for a in service.list_agents(conn, who) if a["id"] == principal_id)
        return link_page(request, who, made, agent["display_name"], reconnect=True)

    @app.post("/admin/invites/{invite_id}/cancel")
    def admin_cancel_invite(invite_id: int, who=Depends(page_who), conn=get_db):
        service.cancel_invite(conn, who, invite_id)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/revoke/{principal_id}")
    def admin_revoke(principal_id: str, who=Depends(page_who), conn=get_db):
        service.revoke_agent(conn, who, principal_id)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/grant")
    def admin_grant(lane: str = Form(...), principal: str = Form(...),
                    can_read: bool = Form(False), can_post: bool = Form(False),
                    who=Depends(page_who), conn=get_db):
        service.set_grant(conn, who, lane, principal, can_read, can_post)
        return RedirectResponse("/admin", status_code=303)

    def cell_of(text):
        """(lane, agent) from a grid value like "household|coder", else None."""
        lane, sep, agent = text.partition("|")
        return (lane, agent) if sep and lane and agent else None

    @app.post("/admin/grants")
    def admin_grants(read: list[str] = Form([]), post: list[str] = Form([]),
                     was: list[str] = Form([]), who=Depends(page_who), conn=get_db):
        """Save the lane grid. `was` records each cell's rights as the page showed
        them ("household|coder|rp"), so only cells the admin changed are applied:
        a cell somebody else changed since the page loaded is left alone."""
        before = {}
        for value in was:
            key, _, held = value.rpartition("|")
            cell = cell_of(key)
            if cell and held in ("r", "p", "rp"):
                before[cell] = ("r" in held, "p" in held)
        wants_read = {c for c in map(cell_of, read) if c}
        wants_post = {c for c in map(cell_of, post) if c}
        changes = []
        for cell in sorted(set(before) | wants_read | wants_post):
            after = (cell in wants_read, cell in wants_post)
            if after != before.get(cell, (False, False)):
                changes.append((*cell, *after))
        done = service.set_grants(conn, who, changes)
        return RedirectResponse(f"/admin?saved={done['applied'] + done['provisional']}"
                                f"&queued={done['pending']}#access", status_code=303)

    def lane_notice(code):
        return {"created": "Lane added.", "provisional": "Lane added. Another admin can ratify "
                "or reject it once they log in.", "pending": "Lane requested. It is made once "
                "another admin approves it."}.get(code)

    @app.post("/admin/lanes")
    def admin_add_lane(slug: str = Form(""), name: str = Form(""), shared: bool = Form(False),
                       notice: str = Form(""), who=Depends(page_who), conn=get_db):
        made = service.create_lane(conn, who, slug.strip(), name, shared, notice.strip() or None)
        return RedirectResponse(f"/admin?lane_notice={made['status']}#lanes-add", status_code=303)

    @app.post("/admin/lanes/{slug}/notice")
    def admin_set_notice(slug: str, notice: str = Form(""), who=Depends(page_who),
                         conn=get_db):
        service.set_lane_notice(conn, who, slug, notice)
        return RedirectResponse("/admin#lane-notices", status_code=303)

    @app.post("/admin/agents/{agent_id}/lane-creation")
    def admin_lane_creation(agent_id: str, allowed: str = Form(...), who=Depends(page_who),
                            conn=get_db):
        service.set_lane_creation(conn, who, agent_id, allowed == "on")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/lane-requests/{request_id}/{decision}")
    def admin_resolve_lane(request_id: int, decision: str, who=Depends(page_who), conn=get_db):
        if decision not in ("approve", "reject"):
            raise BoardError(404, "not found")
        service.resolve_lane_request(conn, who, request_id, decision == "approve")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/pending/{pending_id}/{decision}")
    def admin_resolve(pending_id: int, decision: str, who=Depends(page_who), conn=get_db):
        if decision not in ("approve", "reject"):
            raise BoardError(404, "not found")
        service.resolve_pending(conn, who, pending_id, decision == "approve")
        return RedirectResponse("/admin", status_code=303)

    return app


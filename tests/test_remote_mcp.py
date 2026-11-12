"""Remote MCP endpoint and OAuth sign-in (decisions/0004)."""

import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest

from board import service
from board.oauth import default_agent, redirect_allowed

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def rpc(client, token, method, params=None, id_=1, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers=headers, **kw)


def call(client, token, tool, **arguments):
    r = rpc(client, token, "tools/call", {"name": tool, "arguments": arguments})
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.fixture
def agent(board):
    a = board.register("alice", "helper-bot")
    assert board.grant("alice", "engineering", "helper-bot", read=True, post=True).status_code == 200
    return a


def test_requires_a_token_and_points_at_oauth(board):
    c = board.anon()
    for token in (None, "nope"):
        r = rpc(c, token, "tools/list")
        assert r.status_code == 401
        assert "oauth-protected-resource" in r.headers["www-authenticate"]


def test_handshake_and_tools(board, agent):
    c = agent.http
    init = rpc(c, agent.token, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"]
    note = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                  headers={"Authorization": f"Bearer {agent.token}"})
    assert note.status_code == 202
    names = {t["name"] for t in rpc(c, agent.token, "tools/list").json()["result"]["tools"]}
    assert names == {"board_whoami", "board_read_lane", "board_post"}
    assert "engineering" in call(c, agent.token, "board_whoami")["content"][0]["text"]


def test_post_and_read_use_the_agents_own_rights(board, agent):
    c = agent.http
    ok = call(c, agent.token, "board_post", lane="engineering", body="hello from mcp")
    assert "posted #" in ok["content"][0]["text"]
    assert "hello from mcp" in call(c, agent.token, "board_read_lane",
                                    lane="engineering")["content"][0]["text"]
    # No right on a lane it was not granted: refused by the board, as a tool error.
    denied = call(c, agent.token, "board_post", lane="household", body="x")
    assert denied["isError"] and "refused" in denied["content"][0]["text"]
    assert agent.read("household").status_code in (403, 404)


def test_revocation_is_instant(board, agent):
    assert not call(agent.http, agent.token, "board_whoami").get("isError")
    r = board.admin("alice").post("/v1/admin/principals/helper-bot/revoke")
    assert r.status_code == 200
    assert rpc(agent.http, agent.token, "tools/list").status_code == 401


def test_bad_calls(board, agent):
    c = agent.http
    assert rpc(c, agent.token, "nope").json()["error"]["code"] == -32601
    assert rpc(c, agent.token, "tools/call",
               {"name": "board_delete_everything"}).json()["error"]["code"] == -32602
    bad = call(c, agent.token, "board_read_lane", lane="engineering", limit="lots")
    assert bad["isError"]
    assert c.post("/mcp", content=b"{", headers={"Authorization": f"Bearer {agent.token}"}
                  ).status_code == 400
    assert c.get("/mcp").status_code == 405


# --- OAuth ---------------------------------------------------------------

def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()
                                         ).rstrip(b"=").decode()
    return verifier, challenge


def authorize_fields(challenge, redirect=CALLBACK, **extra):
    return {"response_type": "code", "client_id": "fp_x", "redirect_uri": redirect,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "st8",
            **extra}


def approve(board, admin, challenge, agent="claude-chat", redirect=CALLBACK, lanes=None,
            **override):
    """Approve like a browser. A login spends its authenticator step, so the clock moves on
    afterwards; the next login of the same admin gets a fresh code. If the board asks for
    lanes (a new agent), tick `lanes` ({slug: (read, post)}, default none) and approve."""
    r = sign_in_step(board, admin, challenge, agent, redirect, **override)
    ticket = ticket_of(r)
    if ticket is None:
        return r
    return choose_lanes(board, ticket, lanes or {})


def sign_in_step(board, admin, challenge, agent="claude-chat", redirect=CALLBACK, **override):
    """Only the first step: the credentials form."""
    c = board.anon()
    board._enroll(admin)
    data = {**authorize_fields(challenge, redirect), "agent": agent, "username": admin,
            "password": board.passwords[admin], "code": board.code(admin), **override}
    r = c.post("/oauth/authorize", data=data, follow_redirects=False)
    board.clock.advance(60)
    return r


def ticket_of(r):
    m = re.search(r'name="ticket" value="([^"]+)"', r.text) if r.status_code == 200 else None
    return m.group(1) if m else None


def choose_lanes(board, ticket, lanes, **extra):
    data = {"ticket": ticket, **extra}
    for slug, (read, post) in lanes.items():
        if read:
            data[f"read_{slug}"] = "on"
        if post:
            data[f"post_{slug}"] = "on"
    return board.anon().post("/oauth/authorize/lanes", data=data,
                                                      follow_redirects=False)


def spend(board, code, verifier, redirect=CALLBACK):
    c = board.anon()
    return c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                        "code_verifier": verifier, "redirect_uri": redirect})


def test_discovery_documents(board):
    c = board.anon()
    res = c.get("/.well-known/oauth-protected-resource").json()
    assert res["resource"].endswith("/mcp") and res["authorization_servers"]
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint"].endswith("/oauth/token")


def test_register_accepts_only_allowed_redirects(board):
    c = board.anon()
    ok = c.post("/oauth/register", json={"redirect_uris": [CALLBACK], "client_name": "Claude"})
    assert ok.status_code == 201 and ok.json()["client_id"].startswith("fp_")
    for bad in ("https://evil.example/cb", "http://evil.example:80/cb", "javascript:alert(1)"):
        assert c.post("/oauth/register", json={"redirect_uris": [bad]}).status_code == 400
    assert redirect_allowed("http://localhost:8123/callback")
    assert not redirect_allowed("http://localhost/callback")


def test_full_flow_ends_in_a_working_revocable_token(board):
    verifier, challenge = pkce()
    page = board.anon().get(
        "/oauth/authorize", params=authorize_fields(challenge))
    assert page.status_code == 200 and "Connect to FuzzPan" in page.text
    assert f"form-action 'self' https://claude.ai" in page.headers["content-security-policy"]

    r = approve(board, "alice", challenge)
    assert r.status_code == 303
    loc = urlsplit(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CALLBACK
    q = parse_qs(loc.query)
    assert q["state"] == ["st8"]

    # Nothing exists until the code is spent; then the agent exists, with no rights.
    with service.connect(board.app.state.db_path) as conn:
        assert conn.execute("SELECT 1 FROM principals WHERE id='claude-chat'").fetchone() is None
    t = spend(board, q["code"][0], verifier)
    assert t.status_code == 200 and t.json()["token_type"] == "Bearer"
    token = t.json()["access_token"]
    c = board.anon()
    assert call(c, token, "board_whoami")["content"][0]["text"].startswith("Agent claude-chat")
    assert call(c, token, "board_read_lane", lane="engineering")["isError"]

    # The admin grants lane rights the usual way, and revokes instantly.
    assert board.grant("alice", "engineering", "claude-chat").status_code == 200
    assert not call(c, token, "board_read_lane", lane="engineering").get("isError")
    board.admin("alice").post("/v1/admin/principals/claude-chat/revoke")
    assert rpc(c, token, "tools/list").status_code == 401


def test_code_is_single_use_bound_to_pkce_and_redirect(board):
    verifier, challenge = pkce()
    code = parse_qs(urlsplit(approve(board, "alice", challenge).headers["location"]).query
                    )["code"][0]
    assert spend(board, code, "wrong-verifier").status_code == 400
    # A failed attempt burns the code: it cannot be guessed at repeatedly.
    assert spend(board, code, verifier).status_code == 400

    code = parse_qs(urlsplit(approve(board, "alice", challenge, agent="two").headers["location"]
                             ).query)["code"][0]
    assert spend(board, code, verifier, redirect="https://claude.com/api/mcp/auth_callback"
                 ).status_code == 400
    code = parse_qs(urlsplit(approve(board, "alice", challenge, agent="three").headers["location"]
                             ).query)["code"][0]
    assert spend(board, code, verifier).status_code == 200
    assert spend(board, code, verifier).status_code == 400


def test_approval_needs_real_admin_credentials(board):
    _, challenge = pkce()
    r = approve(board, "alice", challenge, password="wrong")
    assert r.status_code == 401 and "location" not in r.headers
    r = approve(board, "alice", challenge, redirect="https://evil.example/cb")
    assert r.status_code == 400 and "location" not in r.headers


def test_failed_approvals_count_toward_the_login_lockout(board):
    _, challenge = pkce()
    for _ in range(service.LOCK_AFTER):
        assert approve(board, "alice", challenge, password="wrong").status_code == 401
    # Locked now: even the right password and a fresh code are refused.
    r = approve(board, "alice", challenge)
    assert r.status_code == 401 and "location" not in r.headers


def test_cannot_take_over_someone_elses_agent(board):
    board.register("bob", "bobs-bot")
    _, challenge = pkce()
    r = approve(board, "alice", challenge, agent="bobs-bot")
    assert r.status_code == 409


def sign_in(board, agent="claude-chat"):
    """A whole OAuth sign-in: approve, spend the code, return the access token."""
    verifier, challenge = pkce()
    board.clock.advance(60)  # an earlier admin login may have spent the current code
    r = approve(board, "alice", challenge, agent=agent)
    assert r.status_code == 303, r.text
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    t = spend(board, code, verifier)
    assert t.status_code == 200, t.text
    return t.json()["access_token"]


def works(board, token):
    return rpc(board.anon(), token, "tools/list").status_code == 200


def test_signing_in_again_adds_a_connection_and_disconnects_nothing(board):
    """decisions/0005: a new session signing in must not knock out Claude chat."""
    chat = sign_in(board)
    session = sign_in(board)
    assert chat != session
    assert works(board, chat) and works(board, session)
    # Both are the same agent with the same rights.
    assert board.grant("alice", "engineering", "claude-chat").status_code == 200
    c = board.anon()
    for token in (chat, session):
        assert not call(c, token, "board_read_lane", lane="engineering").get("isError")


def test_signing_in_as_an_invited_agent_keeps_its_invite_token(board):
    cli = board.register("alice", "coder")
    oauth = sign_in(board, agent="coder")
    assert works(board, cli.token) and works(board, oauth)


def test_connections_are_capped_dropping_the_least_recently_used(board):
    board.register("alice", "helper-bot")
    with service.connect(board.app.state.db_path) as conn:
        first = service.add_agent_connection(conn, "helper-bot")
        tokens = []
        for _ in range(service.MAX_CONNECTIONS - 1):
            board.clock.advance(1)
            tokens.append(service.add_agent_connection(conn, "helper-bot"))
    board.clock.advance(1)
    assert works(board, first)  # used now, so it is no longer the stalest
    with service.connect(board.app.state.db_path) as conn:
        board.clock.advance(1)
        newest = service.add_agent_connection(conn, "helper-bot")
        assert conn.execute("SELECT COUNT(*) FROM agent_connections WHERE agent_id='helper-bot'"
                            ).fetchone()[0] == service.MAX_CONNECTIONS
    assert works(board, first) and works(board, newest)
    assert not works(board, tokens[0])  # the one used least recently went
    assert all(works(board, t) for t in tokens[1:])


def test_revoking_ends_every_connection_at_once(board):
    cli = board.register("alice", "claude-chat")
    a, b = sign_in(board), sign_in(board)
    board.admin("alice").post("/v1/admin/principals/claude-chat/revoke")
    assert not any(works(board, t) for t in (cli.token, a, b))
    with service.connect(board.app.state.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_connections").fetchone()[0] == 0
    # Signing in again can't bring a revoked agent back.
    verifier, challenge = pkce()
    board.clock.advance(60)
    assert approve(board, "alice", challenge).status_code == 409


def test_a_reconnect_invite_leaves_oauth_connections_alone(board):
    cli = board.register("alice", "coder")
    oauth = sign_in(board, agent="coder")
    r = board.admin("alice").post("/v1/admin/principals/coder/reconnect")
    code = r.json()["code"]
    new = board.anon().post(f"/v1/invites/{code}/redeem").json()
    assert not works(board, cli.token)
    assert works(board, new["token"]) and works(board, oauth)


def test_resetting_an_admin_voids_their_unspent_sign_in_codes(board):
    verifier, challenge = pkce()
    code = parse_qs(urlsplit(approve(board, "alice", challenge).headers["location"]
                             ).query)["code"][0]
    with service.connect(board.app.state.db_path) as conn:
        service.reset_admin(conn, "alice")
    assert spend(board, code, verifier).status_code == 400


# --- Lanes on the sign-in page, ChatGPT (decisions/0006) ------------------

def code_from(r):
    assert r.status_code == 303, r.text
    return parse_qs(urlsplit(r.headers["location"]).query)["code"][0]


def lanes_of(board, token):
    c = board.anon()
    return call(c, token, "board_whoami")["content"][0]["text"]


def test_a_new_agent_picks_lanes_and_can_use_them_at_once(board):
    verifier, challenge = pkce()
    step = sign_in_step(board, "alice", challenge)
    assert step.status_code == 200 and ticket_of(step)
    assert "form-action 'self' https://claude.ai" in step.headers["content-security-policy"]
    # Only lanes alice administers are offered (rule 7): never bob's private lanes.
    assert "engineering" in step.text and "household" in step.text
    assert "bob-money" not in step.text
    # Shared lanes start ticked, private ones don't.
    assert 'name="read_household" aria-label="Read Household & family schedule" checked' \
        in step.text.replace("&amp;", "&")
    assert 'name="read_engineering" aria-label="Read Engineering">' in step.text

    r = choose_lanes(board, ticket_of(step), {"engineering": (True, True)})
    token = spend(board, code_from(r), verifier).json()["access_token"]
    c = board.anon()
    assert call(c, token, "board_post", lane="engineering", body="hi")["content"][0][
        "text"].startswith("posted #")
    assert call(c, token, "board_read_lane", lane="household")["isError"]


def test_lanes_apply_only_when_the_code_is_spent(board):
    _, challenge = pkce()
    approve(board, "alice", challenge, lanes={"engineering": (True, False)})
    with service.connect(board.app.state.db_path) as conn:
        assert conn.execute("SELECT 1 FROM principals WHERE id='claude-chat'").fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM grants").fetchone()[0] == 0


def test_shared_lanes_still_wait_for_the_other_admin(board):
    board.admin("bob")  # bob has logged in, so shared-lane changes need his OK
    verifier, challenge = pkce()
    r = approve(board, "alice", challenge, lanes={"household": (True, True),
                                                  "engineering": (True, False)})
    token = spend(board, code_from(r), verifier).json()["access_token"]
    c = board.anon()
    assert not call(c, token, "board_read_lane", lane="engineering").get("isError")
    assert call(c, token, "board_read_lane", lane="household")["isError"]
    pending = board.admin("bob").get("/v1/admin/pending").json()
    assert [(p["lane"], p["principal"]) for p in pending] == [("household", "claude-chat")]


def test_lanes_the_admin_does_not_administer_are_ignored(board):
    verifier, challenge = pkce()
    r = approve(board, "alice", challenge, lanes={"bob-money": (True, True),
                                                  "nope": (True, True)})
    spend(board, code_from(r), verifier)
    with service.connect(board.app.state.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM grants").fetchone()[0] == 0


def test_signing_in_again_skips_the_lane_step_and_keeps_lanes(board):
    verifier, challenge = pkce()
    r = approve(board, "alice", challenge, lanes={"engineering": (True, True)})
    spend(board, code_from(r), verifier)
    verifier, challenge = pkce()
    again = sign_in_step(board, "alice", challenge)
    token = spend(board, code_from(again), verifier).json()["access_token"]
    assert "engineering" in lanes_of(board, token)


def test_the_lane_ticket_is_single_use_and_fixes_the_request(board):
    verifier, challenge = pkce()
    ticket = ticket_of(sign_in_step(board, "alice", challenge))
    # Fields sent with the ticket can't move the code somewhere else.
    r = choose_lanes(board, ticket, {}, redirect_uri="http://localhost:9999/cb")
    assert urlsplit(r.headers["location"]).netloc == "claude.ai"
    assert choose_lanes(board, ticket, {}).status_code == 410
    assert spend(board, ticket, verifier).status_code == 400  # a ticket is not a code
    assert choose_lanes(board, "oa_made-up", {}).status_code == 410


def test_the_lane_ticket_expires(board):
    _, challenge = pkce()
    ticket = ticket_of(sign_in_step(board, "alice", challenge))
    board.clock.advance(301)
    assert choose_lanes(board, ticket, {}).status_code == 410


def test_cancel_on_the_lane_step_tells_the_client_and_creates_nothing(board):
    _, challenge = pkce()
    ticket = ticket_of(sign_in_step(board, "alice", challenge))
    r = choose_lanes(board, ticket, {"engineering": (True, True)}, deny="1")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["st8"] and "code" not in q
    with service.connect(board.app.state.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM oauth_codes").fetchone()[0] == 0


def test_resetting_an_admin_voids_their_open_lane_tickets(board):
    _, challenge = pkce()
    ticket = ticket_of(sign_in_step(board, "alice", challenge))
    with service.connect(board.app.state.db_path) as conn:
        service.reset_admin(conn, "alice")
    assert choose_lanes(board, ticket, {}).status_code == 410


CHATGPT = "https://chatgpt.com/connector_platform_oauth_redirect"


def test_chatgpt_callbacks_are_allowed_and_nothing_near_them(board):
    assert redirect_allowed(CHATGPT)
    assert redirect_allowed("https://chatgpt.com/connector/oauth/abc_DEF-123")
    for bad in ("https://chatgpt.com/connector/oauth/a/b", "https://chatgpt.com/other",
                "https://chatgpt.com/connector/oauth/x?next=https://evil.example",
                "https://chatgpt.com.evil.example/connector_platform_oauth_redirect",
                "http://chatgpt.com/connector_platform_oauth_redirect"):
        assert not redirect_allowed(bad), bad
    c = board.anon()
    assert c.post("/oauth/register", json={"redirect_uris": [CHATGPT]}).status_code == 201


def test_the_suggested_name_follows_the_app(board):
    assert default_agent(CALLBACK) == "claude-chat"
    assert default_agent(CHATGPT) == "chatgpt"
    assert default_agent("http://127.0.0.1:5555/callback") == "claude-code"
    _, challenge = pkce()
    page = board.anon().get(
        "/oauth/authorize", params=authorize_fields(challenge, CHATGPT))
    assert 'name="agent" value="chatgpt"' in page.text
    assert "form-action 'self' https://chatgpt.com" in page.headers["content-security-policy"]


def test_chatgpt_end_to_end(board):
    verifier, challenge = pkce()
    r = approve(board, "alice", challenge, agent="chatgpt", redirect=CHATGPT,
                lanes={"engineering": (True, False)})
    loc = urlsplit(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CHATGPT
    token = spend(board, code_from(r), verifier, redirect=CHATGPT).json()["access_token"]
    assert lanes_of(board, token).startswith("Agent chatgpt")


def test_a_suggested_name_taken_by_another_admin_becomes_your_own(board):
    board.register("bob", "chatgpt")
    verifier, challenge = pkce()
    r = approve(board, "alice", challenge, agent="chatgpt", redirect=CHATGPT)
    token = spend(board, code_from(r), verifier, redirect=CHATGPT).json()["access_token"]
    assert lanes_of(board, token).startswith("Agent alice-chatgpt")
    # A name the admin typed themselves is still refused, not renamed.
    _, challenge = pkce()
    assert approve(board, "alice", challenge, agent="bobs-thing").status_code in (200, 303)
    board.register("bob", "bobs-bot")
    assert approve(board, "alice", challenge, agent="bobs-bot").status_code == 409


def test_the_callback_names_the_issuer(board):
    c = board.anon()
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["authorization_response_iss_parameter_supported"] is True
    _, challenge = pkce()
    q = parse_qs(urlsplit(approve(board, "alice", challenge).headers["location"]).query)
    assert q["iss"] == [meta["issuer"]]


def test_loopback_redirect_must_be_a_bare_host_and_port():
    """The redirect's origin is written into the sign-in page's CSP header and shown
    to the admin, so a loopback address may carry nothing but the host and port."""
    for good in ("http://localhost:8123/callback", "http://127.0.0.1:8123/cb?x=1",
                 "http://[::1]:8123/cb", "http://LOCALHOST:8123/cb"):
        assert redirect_allowed(good), good
    for bad in ("http://:x; script-src 'unsafe-inline' *@localhost:80/cb",
                "http://:claude.ai@localhost:80/cb",
                "http://@localhost:80/cb",
                "http://user@localhost:80/cb",
                "http://[::1]junk; script-src *:80/cb",
                "http://localhost:080/cb",
                "http://localhost :80/cb"):
        assert not redirect_allowed(bad), bad

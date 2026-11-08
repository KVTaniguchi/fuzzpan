"""Remote MCP endpoint and OAuth sign-in (decisions/0004)."""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from board import service
from board.oauth import redirect_allowed
from tests.conftest import HTTPS

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
    a = board.register("alice", "muse")
    assert board.grant("alice", "engineering", "muse", read=True, post=True).status_code == 200
    return a


def test_requires_a_token_and_points_at_oauth(board):
    c = TestClient(board.app, base_url=HTTPS)
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
    r = board.admin("alice").post("/v1/admin/principals/muse/revoke")
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


def approve(board, admin, challenge, agent="claude-chat", redirect=CALLBACK, **override):
    """Approve like a browser. A login spends its authenticator step, so the clock moves on
    afterwards; the next login of the same admin gets a fresh code."""
    c = TestClient(board.app, base_url=HTTPS)
    board._enroll(admin)
    data = {**authorize_fields(challenge, redirect), "agent": agent, "username": admin,
            "password": board.passwords[admin], "code": board.code(admin), **override}
    r = c.post("/oauth/authorize", data=data, follow_redirects=False)
    board.clock.advance(60)
    return r


def spend(board, code, verifier, redirect=CALLBACK):
    c = TestClient(board.app, base_url=HTTPS)
    return c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                        "code_verifier": verifier, "redirect_uri": redirect})


def test_discovery_documents(board):
    c = TestClient(board.app, base_url=HTTPS)
    res = c.get("/.well-known/oauth-protected-resource").json()
    assert res["resource"].endswith("/mcp") and res["authorization_servers"]
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint"].endswith("/oauth/token")


def test_register_accepts_only_allowed_redirects(board):
    c = TestClient(board.app, base_url=HTTPS)
    ok = c.post("/oauth/register", json={"redirect_uris": [CALLBACK], "client_name": "Claude"})
    assert ok.status_code == 201 and ok.json()["client_id"].startswith("fp_")
    for bad in ("https://evil.example/cb", "http://evil.example:80/cb", "javascript:alert(1)"):
        assert c.post("/oauth/register", json={"redirect_uris": [bad]}).status_code == 400
    assert redirect_allowed("http://localhost:8123/callback")
    assert not redirect_allowed("http://localhost/callback")


def test_full_flow_ends_in_a_working_revocable_token(board):
    verifier, challenge = pkce()
    page = TestClient(board.app, base_url=HTTPS).get(
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
    c = TestClient(board.app, base_url=HTTPS)
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
    return rpc(TestClient(board.app, base_url=HTTPS), token, "tools/list").status_code == 200


def test_signing_in_again_adds_a_connection_and_disconnects_nothing(board):
    """decisions/0005: a new session signing in must not knock out Claude chat."""
    chat = sign_in(board)
    session = sign_in(board)
    assert chat != session
    assert works(board, chat) and works(board, session)
    # Both are the same agent with the same rights.
    assert board.grant("alice", "engineering", "claude-chat").status_code == 200
    c = TestClient(board.app, base_url=HTTPS)
    for token in (chat, session):
        assert not call(c, token, "board_read_lane", lane="engineering").get("isError")


def test_signing_in_as_an_invited_agent_keeps_its_invite_token(board):
    cli = board.register("alice", "coder")
    oauth = sign_in(board, agent="coder")
    assert works(board, cli.token) and works(board, oauth)


def test_connections_are_capped_dropping_the_least_recently_used(board):
    board.register("alice", "muse")
    with service.connect(board.app.state.db_path) as conn:
        first = service.add_agent_connection(conn, "muse")
        tokens = []
        for _ in range(service.MAX_CONNECTIONS - 1):
            board.clock.advance(1)
            tokens.append(service.add_agent_connection(conn, "muse"))
    board.clock.advance(1)
    assert works(board, first)  # used now, so it is no longer the stalest
    with service.connect(board.app.state.db_path) as conn:
        board.clock.advance(1)
        newest = service.add_agent_connection(conn, "muse")
        assert conn.execute("SELECT COUNT(*) FROM agent_connections WHERE agent_id='muse'"
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
    new = TestClient(board.app, base_url=HTTPS).post(f"/v1/invites/{code}/redeem").json()
    assert not works(board, cli.token)
    assert works(board, new["token"]) and works(board, oauth)


def test_resetting_an_admin_voids_their_unspent_sign_in_codes(board):
    verifier, challenge = pkce()
    code = parse_qs(urlsplit(approve(board, "alice", challenge).headers["location"]
                             ).query)["code"][0]
    with service.connect(board.app.state.db_path) as conn:
        service.reset_admin(conn, "alice")
    assert spend(board, code, verifier).status_code == 400

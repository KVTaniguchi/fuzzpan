"""Agent bearer tokens: issued once, stored hashed, checked on every request."""

import os
import stat

import pytest
from fastapi.testclient import TestClient

from board import service
from board.app import create_app
from board.migrations import migrate_db
from client.board_client import write_token
from tests.conftest import HTTPS, Agent
from tests.test_migrations import make_v0


def test_token_is_hashed_at_rest(board):
    agent = board.register("alice", "coder")
    with service.connect(board.app.state.db_path) as c:
        row = c.execute("SELECT token_hash FROM principals WHERE id='coder'").fetchone()
    assert agent.token not in row["token_hash"]
    assert row["token_hash"] == service._hash_token(agent.token)


def test_token_format(board):
    agent = board.register("alice", "coder")
    assert agent.token.startswith("fb_") and len(agent.token) > 40


def test_redemption_response_is_not_cached(board):
    code = board.invite("alice", "coder", {})
    r = TestClient(board.app).post(f"/v1/invites/{code}/redeem")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert set(r.json()) == {"agent_id", "token", "lanes", "pending", "reconnect",
                             "next_steps"}


def test_the_token_is_not_shown_again(board):
    agent = board.register("alice", "coder")
    listing = board.admin("alice").get("/v1/admin/principals")
    assert agent.token not in listing.text and "token" not in listing.text


def test_tokens_are_unique_per_agent(board):
    a, b = board.register("alice", "one"), board.register("bob", "two")
    assert a.token != b.token
    assert a.request("GET", "/v1/me").json()["id"] == "one"
    assert b.request("GET", "/v1/me").json()["id"] == "two"


def test_revocation_applies_on_the_next_request(board):
    agent = board.register("alice", "coder")
    assert agent.request("GET", "/v1/me").status_code == 200
    board.admin("alice").post("/v1/admin/principals/coder/revoke")
    assert agent.request("GET", "/v1/me").status_code == 401


def test_missing_and_wrong_tokens_refused(board):
    agent = board.register("alice", "coder")
    http = board.anon()
    assert http.get("/v1/me").status_code == 401
    for header in ("Bearer", "Bearer ", "Bearer fb_wrong", f"Basic {agent.token}",
                   agent.token, f"Bearer {agent.token}x", f"Bearer {agent.token[:-1]}"):
        r = http.get("/v1/me", headers={"Authorization": header})
        assert r.status_code == 401, header
    assert agent.request("GET", "/v1/me").status_code == 200


def test_one_agents_token_cannot_act_as_another(board):
    a = board.register("alice", "one")
    board.register("bob", "two")
    board.grant("alice", "engineering", "one")
    b = Agent(board.app, "two", board.register("bob", "three").token)
    assert b.read("engineering").status_code == 404
    assert a.read("engineering").status_code == 200


def test_agent_cannot_use_a_session_only_route(board):
    agent = board.register("alice", "coder")
    for method, path in (("GET", "/v1/admin/principals"), ("GET", "/v1/admin/pending"),
                         ("GET", "/v1/admin/audit"), ("GET", "/v1/admin/lanes"),
                         ("POST", "/v1/admin/principals/coder/revoke")):
        assert agent.request(method, path).status_code == 401, path
    r = agent.request("POST", "/v1/admin/invites", {"display_name": "minted", "lanes": {}})
    assert r.status_code == 401
    r = agent.request("PUT", "/v1/admin/lanes/engineering/grants/coder",
                      {"can_read": True, "can_post": True})
    assert r.status_code == 401


def test_an_agent_token_does_not_log_into_the_feed_page(board):
    agent = board.register("alice", "coder")
    r = agent.http.get("/", headers={"Authorization": f"Bearer {agent.token}"},
                       follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_authenticated_requests_are_logged_with_agent_id(board, caplog):
    agent = board.register("alice", "planner")
    with caplog.at_level("INFO"):
        agent.request("GET", "/v1/me")
    assert "agent=planner GET /v1/me" in caplog.text


def test_last_seen_updates_at_most_once_a_minute(board, frozen_time):
    agent = board.register("alice", "coder")

    def last_seen():
        with service.connect(board.app.state.db_path) as c:
            return c.execute("SELECT last_seen_at FROM principals WHERE id='coder'").fetchone()[0]

    assert last_seen() is None
    agent.request("GET", "/v1/me")
    first = last_seen()
    assert first == pytest.approx(frozen_time.time())
    frozen_time.advance(30)
    agent.request("GET", "/v1/me")
    assert last_seen() == first
    frozen_time.advance(31)
    agent.request("GET", "/v1/me")
    assert last_seen() == pytest.approx(frozen_time.time())


def test_migrated_agent_has_no_access_until_reconnected(tmp_path):
    # A v0 agent keeps its id and grants, loses its key, has no token.
    db = make_v0(tmp_path)
    migrate_db(db)
    with service.connect(db) as c:
        c.execute("UPDATE principals SET status = 'active' WHERE id = 'old-agent'")
    app = create_app(db)
    with service.connect(db) as c:
        row = c.execute("SELECT id, status, token_hash FROM principals "
                        "WHERE id='old-agent'").fetchone()
        assert (row["id"], row["status"], row["token_hash"]) == ("old-agent", "active", None)
        assert c.execute("SELECT can_read FROM grants").fetchone()[0] == 1  # grants kept

    # Nothing that could be presented authenticates, not even an empty token.
    for header in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer None"},
                   {"Authorization": "Bearer fb_" + "A" * 43}):
        r = TestClient(app, base_url=HTTPS).get("/v1/me", headers=header)
        assert r.status_code == 401, header

    # Once a token is issued (a reconnect invite does this), the old id
    # and grant work again.
    with service.connect(db) as c:
        token = service.issue_agent_token(c, "old-agent")
    agent = Agent(app, "old-agent", token)
    assert agent.request("GET", "/v1/me").json()["lanes"] == {
        "household": {"read": True, "post": False}}
    assert agent.read("household").json()["posts"][0]["body"] == "hello"


def test_old_posts_survive_dropping_signature_columns(tmp_path):
    db = make_v0(tmp_path)
    migrate_db(db)
    app = create_app(db)
    with service.connect(db) as c:
        c.execute("UPDATE principals SET status = 'active' WHERE id = 'old-agent'")
        c.execute("UPDATE grants SET can_post = 1")
        token = service.issue_agent_token(c, "old-agent")
    agent = Agent(app, "old-agent", token)
    posts = agent.read("household").json()["posts"]
    assert [(p["id"], p["author"], p["body"], p["received_at"]) for p in posts] == \
        [(1, "old-agent", "hello", "now")]
    assert "signed" not in posts[0]
    assert agent.post("household", "after the upgrade").json()["id"] == 2


def test_token_file_is_owner_only(tmp_path):
    write_token(p := tmp_path / "x.token", "fb_test")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert p.read_text().strip() == "fb_test"


def test_token_file_in_a_new_directory_is_private(tmp_path):
    write_token(tmp_path / "tokens" / "x.token", "fb_test")
    assert stat.S_IMODE(os.stat(tmp_path / "tokens").st_mode) == 0o700


def test_token_file_is_never_overwritten_unless_told_to(tmp_path):
    p = tmp_path / "x.token"
    write_token(p, "fb_first")
    with pytest.raises(FileExistsError):
        write_token(p, "fb_second")
    assert p.read_text().strip() == "fb_first"
    write_token(p, "fb_second", overwrite=True)
    assert p.read_text().strip() == "fb_second"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_admin_page_never_shows_a_token(board):
    agent = board.register("alice", "webbot")
    assert agent.token not in board.admin("alice").get("/admin").text


# --- Locking, commit ordering, revocation at rest ----------------------------

def test_a_throttled_authentication_takes_no_write_lock(board):
    agent = board.register("alice", "coder")
    with service.connect(board.app.state.db_path) as c:
        service.authenticate_token(c, agent.token)
        assert c.in_transaction  # the first sighting writes last_seen_at
        c.commit()
        assert service.authenticate_token(c, agent.token).id == "coder"
        assert not c.in_transaction  # within a minute: no UPDATE, no lock


def test_revoking_clears_the_token_hash(board):
    agent = board.register("alice", "coder")
    board.admin("alice").post("/v1/admin/principals/coder/revoke")
    with service.connect(board.app.state.db_path) as c:
        row = c.execute("SELECT status, token_hash FROM principals WHERE id='coder'").fetchone()
    assert (row["status"], row["token_hash"]) == ("revoked", None)
    assert agent.request("GET", "/v1/me").status_code == 401


def _asgi(app, method, path, cookies=None, json_body=None, form=None, on_start=None,
          messages=None):
    """Drive the ASGI app directly. on_start() runs the moment the response
    starts, which is what a client could first observe; TestClient hides it."""
    import asyncio
    import json as jsonlib
    from urllib.parse import urlencode
    body = b""
    headers = [(b"host", b"testserver")]
    if json_body is not None:
        body = jsonlib.dumps(json_body).encode()
        headers.append((b"content-type", b"application/json"))
    if form is not None:
        body = urlencode(form).encode()
        headers.append((b"content-type", b"application/x-www-form-urlencoded"))
    if cookies:
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
             "query_string": b"", "headers": headers, "client": ("127.0.0.1", 1),
             "server": ("testserver", 443)}
    sent = messages if messages is not None else []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.start" and on_start:
            on_start()

    asyncio.run(app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _row(board, principal_id):
    with service.connect(board.app.state.db_path) as c:  # a separate connection
        return c.execute("SELECT status, token_hash FROM principals WHERE id = ?",
                         (principal_id,)).fetchone()


def test_revoke_is_committed_before_the_response_starts(board):
    board.register("alice", "coder")
    cookies = dict(board.admin("alice").cookies)
    seen = []
    status = _asgi(board.app, "POST", "/v1/admin/principals/coder/revoke", cookies,
                   on_start=lambda: seen.append(tuple(_row(board, "coder"))))
    assert status == 200 and seen == [("revoked", None)]


def test_redemption_is_committed_before_the_response_starts(board):
    code = board.invite("alice", "fresh", {})
    seen = []
    status = _asgi(board.app, "POST", f"/v1/invites/{code}/redeem",
                   on_start=lambda: seen.append(_row(board, "fresh")))
    assert status == 200 and seen[0] is not None and seen[0]["token_hash"]


def test_form_and_post_routes_commit_before_the_response_starts(board):
    agent = board.register("alice", "coder")
    board.grant("alice", "engineering", "coder")
    cookies = dict(board.admin("alice").cookies)

    def post_count():
        with service.connect(board.app.state.db_path) as c:
            return c.execute("SELECT count(*) FROM posts").fetchone()[0]

    seen = []
    assert _asgi(board.app, "POST", "/lanes/engineering/post", cookies,
                 form={"body": "hello"}, on_start=lambda: seen.append(post_count())) == 303
    assert seen == [1]
    seen.clear()
    assert _asgi(board.app, "POST", "/admin/revoke/coder", cookies,
                 on_start=lambda: seen.append(_row(board, "coder")["status"])) == 303
    assert seen == ["revoked"]
    assert agent.request("GET", "/v1/me").status_code == 401


def test_a_failed_commit_is_an_error_response_not_a_success(board, monkeypatch):
    import functools
    import sqlite3
    from board import db

    class CommitFails(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("database is locked")

    board.register("alice", "coder")
    cookies = dict(board.admin("alice").cookies)
    # The real connect() runs; only the connection's commit() is made to fail.
    monkeypatch.setattr("board.app.connect", functools.partial(db.connect, factory=CommitFails))
    messages = []
    try:
        _asgi(board.app, "POST", "/v1/admin/principals/coder/revoke", cookies,
              messages=messages)
    except sqlite3.OperationalError:
        pass  # the server error propagates after the 500 has been sent
    starts = [m["status"] for m in messages if m["type"] == "http.response.start"]
    assert starts and starts[0] == 500, starts  # never a 200 followed by a failure
    monkeypatch.undo()
    assert _row(board, "coder")["status"] == "active" and _row(board, "coder")["token_hash"]


# --- Client messages when no token is configured -----------------------------

def test_client_without_a_token_says_so():
    from client.board_client import BoardClient, TokenMissing
    for token in ("", "  ", None):
        with pytest.raises(TokenMissing, match="no token"):
            BoardClient("http://x", token)


def test_from_env_names_the_missing_setting(monkeypatch):
    from client.board_client import BoardClient, TokenMissing
    monkeypatch.setenv("BOARD_URL", "http://x")
    monkeypatch.delenv("BOARD_TOKEN", raising=False)
    with pytest.raises(TokenMissing, match="BOARD_TOKEN is not set"):
        BoardClient.from_env()
    monkeypatch.setenv("BOARD_TOKEN", "   ")
    with pytest.raises(TokenMissing, match="BOARD_TOKEN is not set"):
        BoardClient.from_env()


def test_mcp_server_start_up_messages(tmp_path, monkeypatch):
    from client import mcp_server
    from client.board_client import TokenMissing
    monkeypatch.setenv("BOARD_TOKEN_DIR", str(tmp_path))
    monkeypatch.delenv("BOARD_TOKEN", raising=False)
    monkeypatch.delenv("BOARD_AGENT", raising=False)
    with pytest.raises(TokenMissing, match="BOARD_TOKEN, or BOARD_AGENT"):
        mcp_server.client_from_env()
    monkeypatch.setenv("BOARD_AGENT", "ghost")
    with pytest.raises(TokenMissing, match="no token file at"):
        mcp_server.client_from_env()
    (tmp_path / "ghost.token").write_text("\n")
    with pytest.raises(TokenMissing, match="is empty"):
        mcp_server.client_from_env()
    with pytest.raises(SystemExit, match="family-board: token file"):
        mcp_server.main()
    (tmp_path / "ghost.token").write_text("fb_x\n")
    assert mcp_server.client_from_env().token == "fb_x"

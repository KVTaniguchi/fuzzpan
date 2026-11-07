"""Invite links: single-use, expiring, hashed at rest, all-or-nothing on redemption."""

import hashlib
import io
import logging
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from board import service
from board.app import create_app
from board.provision import provision_family
from client import cli
from client.board_client import BoardClientError, InviteLinkError, redeem
from tests.conftest import Board

RW = {"read": True, "post": True}
READ_ONLY = {"read": True, "post": False}
HTTPS = "https://testserver"


def redeem_code(board, code, http=None):
    return (http or TestClient(board.app, base_url=HTTPS)).post(f"/v1/invites/{code}/redeem")


def me(board, token):
    r = TestClient(board.app, base_url=HTTPS).get(
        "/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()


def db_rows(board, sql, args=()):
    conn = sqlite3.connect(board.app.state.db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def db_exec(board, sql, args=()):
    conn = sqlite3.connect(board.app.state.db_path)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


# --- Using a link -----------------------------------------------------------

def test_previewing_a_link_does_not_use_it_up(board):
    # A GET only previews; only the POST spends the link.
    code = board.invite("alice", "Coder", {"engineering": RW})
    for _ in range(3):
        assert TestClient(board.app).get(f"/invite/{code}").status_code == 200
    assert redeem_code(board, code).status_code == 200


def test_invite_is_single_use(board):
    code = board.invite("alice", "Coder", {"engineering": RW})
    assert redeem_code(board, code).status_code == 200
    assert redeem_code(board, code).status_code == 410


def test_expired_invite_refused(board, frozen_time):
    code = board.invite("alice", "Coder", {"engineering": RW})
    frozen_time.advance(service.INVITE_SECONDS + 1)
    assert redeem_code(board, code).status_code == 410
    assert TestClient(board.app).get(f"/invite/{code}").status_code == 410
    assert db_rows(board, "SELECT id FROM principals WHERE id = 'coder'") == []


def test_cancelled_invite_refused(board):
    alice = board.admin("alice")
    code = board.invite("alice", "Coder", {})
    invite_id = alice.get("/v1/admin/invites").json()[0]["id"]
    assert alice.post(f"/v1/admin/invites/{invite_id}/cancel").status_code == 200
    assert redeem_code(board, code).status_code == 410
    assert alice.get("/v1/admin/invites").json() == []


def test_unknown_code_is_404(board):
    assert redeem_code(board, "inv_nope").status_code == 404
    assert TestClient(board.app).get("/invite/inv_nope").status_code == 404


def test_code_is_stored_hashed(board):
    code = board.invite("alice", "Coder", {})
    assert db_rows(board, "SELECT code_hash FROM invites") == \
        [(hashlib.sha256(code.encode()).hexdigest(),)]
    dump = repr(db_rows(board, "SELECT * FROM invites")) + repr(
        db_rows(board, "SELECT detail FROM audit_log"))
    assert code not in dump
    assert code not in board.admin("alice").get("/v1/admin/invites").text


def test_redeemed_token_works_and_is_returned_only_once(board):
    code = board.invite("alice", "Coder", {"engineering": RW})
    first = redeem_code(board, code)
    token = first.json()["token"]
    assert first.json()["agent_id"] == "coder"
    assert me(board, token)["id"] == "coder"
    assert db_rows(board, "SELECT token_hash FROM principals WHERE id = 'coder'") == \
        [(hashlib.sha256(token.encode()).hexdigest(),)]
    again = redeem_code(board, code)
    assert again.status_code == 410 and token not in again.text
    listing = board.admin("alice").get("/v1/admin/invites")
    assert token not in listing.text


def test_redemption_and_page_are_not_cached(board):
    code = board.invite("alice", "Coder", {})
    assert TestClient(board.app).get(f"/invite/{code}").headers["cache-control"] == "no-store"
    assert redeem_code(board, code).headers["cache-control"] == "no-store"
    created = board.admin("alice").post("/v1/admin/invites",
                                        json={"display_name": "Other", "lanes": {}})
    assert created.headers["cache-control"] == "no-store"


def test_private_lane_grant_applies_at_once(board):
    token = redeem_code(board, board.invite("alice", "Coder", {"engineering": RW})).json()["token"]
    assert me(board, token)["lanes"]["engineering"] == {"read": True, "post": True}


def test_shared_lane_stays_pending_after_redemption(board):
    # Connected now, shared lane waits for the other admin
    board.admin("bob")
    result = redeem_code(board, board.invite("alice", "Coder", {"household": RW})).json()
    assert "household" not in me(board, result["token"])["lanes"]
    assert any(p["lane"] == "household" for p in result["pending"])


def test_cannot_invite_onto_another_admins_private_lane(board):
    r = board.admin("alice").post("/v1/admin/invites", json={
        "display_name": "Spy", "lanes": {"bob-money": RW}})
    assert r.status_code == 404
    assert db_rows(board, "SELECT id FROM invites") == []


def test_unknown_lane_refused_at_creation(board):
    r = board.admin("alice").post("/v1/admin/invites", json={
        "display_name": "Lost", "lanes": {"no-such-lane": RW}})
    assert r.status_code == 404


def test_read_only_invite_cannot_post(board):
    token = redeem_code(board, board.invite("alice", "Reader", {"engineering": READ_ONLY})
                        ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    http = TestClient(board.app, base_url=HTTPS)
    assert http.get("/v1/lanes/engineering/posts", headers=headers).status_code == 200
    assert http.post("/v1/lanes/engineering/posts", json={"body": "hi"},
                     headers=headers).status_code == 403


def test_invite_gives_no_rights_on_unlisted_lanes(board):
    token = redeem_code(board, board.invite("alice", "Coder", {"engineering": RW})).json()["token"]
    assert set(me(board, token)["lanes"]) == {"engineering"}
    headers = {"Authorization": f"Bearer {token}"}
    assert TestClient(board.app, base_url=HTTPS).get(
        "/v1/lanes/alice-money/posts", headers=headers).status_code == 404


def test_duplicate_agent_name_refused_at_creation(board):
    board.register("alice", "coder")
    r = board.admin("bob").post("/v1/admin/invites", json={"display_name": "Coder", "lanes": {}})
    assert r.status_code == 409
    # A name that matches an admin's id is taken too.
    r = board.admin("bob").post("/v1/admin/invites", json={"display_name": "alice", "lanes": {}})
    assert r.status_code == 409


def test_second_open_invite_for_the_same_name_refused(board):
    board.invite("alice", "Coder", {})
    r = board.admin("bob").post("/v1/admin/invites", json={"display_name": "coder!", "lanes": {}})
    assert r.status_code == 409


def test_name_with_no_letters_or_digits_refused(board):
    for name in ("!!!", "   ", "---", ""):
        r = board.admin("alice").post("/v1/admin/invites",
                                      json={"display_name": name, "lanes": {}})
        assert r.status_code == 400, name


def test_agent_id_is_a_slug_of_the_name(board):
    r = board.admin("alice").post("/v1/admin/invites",
                                  json={"display_name": "  Ada's Coder #2 ", "lanes": {}})
    assert r.status_code == 201 and r.json()["agent_id"] == "ada-s-coder-2"


def test_name_taken_between_creation_and_redemption_leaves_nothing_behind(board):
    code = board.invite("alice", "Coder", {"engineering": RW})
    # Someone gets the id first (an internal registration, as redemption does).
    with service.connect(board.app.state.db_path) as conn:
        service.register_agent(conn, service.Caller("bob", "human", "bob"), "coder")
    r = redeem_code(board, code)
    assert r.status_code == 409
    assert db_rows(board, "SELECT owner FROM principals WHERE id = 'coder'") == [("bob",)]
    assert db_rows(board, "SELECT COUNT(*) FROM grants WHERE principal_id = 'coder'") == [(0,)]


# --- Reconnecting -----------------------------------------------------------

def reconnect_code(board, admin, agent_id):
    r = board.admin(admin).post(f"/v1/admin/principals/{agent_id}/reconnect")
    assert r.status_code == 201, r.text
    return r.json()["code"]


def test_reconnect_replaces_the_token_and_keeps_lanes(board):
    old = redeem_code(board, board.invite("alice", "Coder", {"engineering": RW})).json()["token"]
    code = reconnect_code(board, "alice", "coder")
    result = redeem_code(board, code).json()
    assert result["agent_id"] == "coder" and result["token"] != old
    assert me(board, result["token"])["lanes"] == {"engineering": RW}
    r = TestClient(board.app, base_url=HTTPS).get("/v1/me",
                                                  headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 401


def test_cannot_reconnect_a_revoked_agent(board):
    board.register("alice", "coder")
    board.admin("alice").post("/v1/admin/principals/coder/revoke")
    r = board.admin("alice").post("/v1/admin/principals/coder/reconnect")
    assert r.status_code == 404


def test_agent_revoked_while_a_reconnect_invite_is_open_stays_revoked(board):
    board.register("alice", "coder")
    code = reconnect_code(board, "alice", "coder")
    board.admin("alice").post("/v1/admin/principals/coder/revoke")
    assert redeem_code(board, code).status_code == 410
    assert db_rows(board, "SELECT status, token_hash FROM principals WHERE id = 'coder'") == \
        [("revoked", None)]


def test_cannot_reconnect_another_admins_agent(board):
    board.register("bob", "bob-agent")
    r = board.admin("alice").post("/v1/admin/principals/bob-agent/reconnect")
    assert r.status_code == 404


def test_reconnect_invite_for_an_agent_that_changed_owner_is_refused(board):
    board.register("alice", "coder")
    code = reconnect_code(board, "alice", "coder")
    db_exec(board, "UPDATE principals SET owner = 'bob' WHERE id = 'coder'")
    assert redeem_code(board, code).status_code == 410


# --- Redemption is all or nothing -------------------------------------------

def test_failed_grant_at_redemption_leaves_no_agent_and_the_invite_unclaimed(board):
    code = board.invite("alice", "Coder", {"engineering": RW, "household": RW})
    # Alice stops administering household before the link is used.
    db_exec(board, "UPDATE lanes SET scope = 'bob' WHERE slug = 'household'")
    r = redeem_code(board, code)
    assert r.status_code in (403, 404)
    assert db_rows(board, "SELECT id FROM principals WHERE id = 'coder'") == []
    assert db_rows(board, "SELECT COUNT(*) FROM grants WHERE principal_id = 'coder'") == [(0,)]
    assert db_rows(board, "SELECT status, redeemed_at FROM invites") == [("open", None)]


def test_removed_inviting_admin_cannot_have_their_invite_redeemed(board):
    code = board.invite("alice", "Coder", {})
    db_exec(board, "DELETE FROM sessions WHERE admin = 'alice'")
    db_exec(board, "DELETE FROM admins WHERE name = 'alice'")
    assert redeem_code(board, code).status_code == 410
    assert db_rows(board, "SELECT id FROM principals WHERE id = 'coder'") == []


def test_concurrent_redemptions_give_exactly_one_token(board):
    # Several redemptions race for one link, each on its own connection.
    code = board.invite("alice", "Coder", {"engineering": RW})
    racers = 6
    barrier = threading.Barrier(racers)
    results = []

    def race(n):
        barrier.wait()
        try:
            with service.connect(board.app.state.db_path) as conn:
                results.append(("ok", service.redeem_invite(conn, code, f"10.0.0.{n}")))
        except service.BoardError as e:
            results.append(("error", e.status))

    threads = [threading.Thread(target=race, args=(n,)) for n in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r[0] for r in results) == ["error"] * (racers - 1) + ["ok"]
    assert {r[1] for r in results if r[0] == "error"} == {410}
    assert db_rows(board, "SELECT COUNT(*) FROM principals WHERE id = 'coder'") == [(1,)]
    assert db_rows(board, "SELECT COUNT(*) FROM audit_log WHERE action = 'invite-redeem'") == [(1,)]
    assert db_rows(board, "SELECT status FROM invites") == [("redeemed",)]


def test_redemption_is_rate_limited(board):
    http = TestClient(board.app, base_url=HTTPS)
    statuses = [redeem_code(board, "inv_guess", http).status_code for _ in range(12)]
    assert statuses[:10] == [404] * 10 and statuses[10:] == [429, 429]


# --- Listing and the page ---------------------------------------------------

def test_list_shows_only_my_open_invites(board):
    board.invite("alice", "Coder", {"engineering": RW})
    board.invite("bob", "Planner", {})
    mine = board.admin("alice").get("/v1/admin/invites").json()
    assert [i["agent_id"] for i in mine] == ["coder"]
    assert mine[0]["lanes"] == {"engineering": RW} and "code" not in mine[0]
    redeem_code(board, board.invite("alice", "Used", {}))
    assert [i["agent_id"] for i in board.admin("alice").get("/v1/admin/invites").json()] == ["coder"]


def test_cannot_cancel_another_admins_invite(board):
    board.invite("bob", "Planner", {})
    invite_id = board.admin("bob").get("/v1/admin/invites").json()[0]["id"]
    assert board.admin("alice").post(f"/v1/admin/invites/{invite_id}/cancel").status_code == 404


def test_invite_page_tells_the_agent_how_to_connect(board):
    html = TestClient(board.app).get(f"/invite/{board.invite('alice', 'Coder', {})}").text
    assert "POST" in html and "/redeem" in html


def test_invite_page_escapes_the_display_name(board):
    name = '<script>alert(1)</script> "x"'
    html = TestClient(board.app).get(f"/invite/{board.invite('alice', name, {})}").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_link_uses_the_public_url_when_set(board, monkeypatch):
    monkeypatch.setenv("BOARD_PUBLIC_URL", "https://board.example.com/")
    r = board.admin("alice").post("/v1/admin/invites", json={"display_name": "A", "lanes": {}})
    assert r.json()["url"] == f"https://board.example.com/invite/{r.json()['code']}"
    monkeypatch.delenv("BOARD_PUBLIC_URL")
    r = board.admin("alice").post("/v1/admin/invites", json={"display_name": "B", "lanes": {}})
    assert r.json()["url"] == f"{HTTPS}/invite/{r.json()['code']}"
    assert "https://board.example.com" not in TestClient(board.app).get(
        r.json()["url"].replace(HTTPS, "")).text


def test_invite_routes_need_no_session_but_creation_does(board):
    assert TestClient(board.app).post("/v1/admin/invites",
                                      json={"display_name": "x", "lanes": {}}).status_code == 401
    assert TestClient(board.app).get("/v1/admin/invites").status_code == 401


def test_the_json_registration_route_is_gone(board):
    r = board.admin("alice").post("/v1/admin/principals", json={"id": "coder"})
    assert r.status_code in (404, 405)


# --- The client and `board connect` -------------------------------------------

def link(board, name="Coder", lanes=None):
    r = board.admin("alice").post("/v1/admin/invites",
                                  json={"display_name": name, "lanes": lanes or {"engineering": RW}})
    return r.json()["url"]


def test_redeem_returns_the_token_and_base_url(board):
    result = redeem(link(board), http=TestClient(board.app))
    assert result["base_url"] == HTTPS and result["agent_id"] == "coder"
    assert result["token"].startswith("fb_")


@pytest.mark.parametrize("url", [
    "http://board.example.com/invite/inv_x",
    "http://localhost.evil.com/invite/inv_x",
    "http://localhost@evil.com/invite/inv_x",
    "ftp://localhost/invite/inv_x",
    "https://board.example.com/somewhere/inv_x",
    "https://board.example.com/invite/",
    "not a url",
])
def test_redeem_refuses_unsafe_or_malformed_links_before_any_request(url):
    class Boom:
        def post(self, *a, **k):
            raise AssertionError("must not contact the server")
    with pytest.raises(InviteLinkError):
        redeem(url, http=Boom())


@pytest.mark.parametrize("url", ["http://localhost:8799/invite/inv_x",
                                 "http://127.0.0.1:8799/invite/inv_x"])
def test_redeem_allows_plain_http_to_localhost_only(url):
    class Seen:
        called = None
        def post(self, target, **k):
            Seen.called = target
            raise AssertionError("stop here")
    with pytest.raises(AssertionError):
        redeem(url, http=Seen())
    assert Seen.called == url.replace("/invite/", "/v1/invites/") + "/redeem"


def test_redeem_reports_a_used_link(board):
    url = link(board)
    redeem(url, http=TestClient(board.app))
    with pytest.raises(BoardClientError) as e:
        redeem(url, http=TestClient(board.app))
    assert e.value.status == 410 and "invite" in str(e.value)


def test_connect_saves_the_token_and_never_prints_it(board, tmp_path, monkeypatch):
    monkeypatch.setenv("BOARD_TOKEN_DIR", str(tmp_path / "tokens"))
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["connect", link(board)], http=TestClient(board.app), out=out, err=err)
    assert code == 0, err.getvalue()
    saved = (tmp_path / "tokens" / "coder.token").read_text().strip()
    assert saved.startswith("fb_")
    assert oct((tmp_path / "tokens" / "coder.token").stat().st_mode & 0o777) == "0o600"
    shown = out.getvalue() + err.getvalue()
    assert saved not in shown and "fb_" not in shown
    assert "coder" in shown and "engineering" in shown
    assert me(board, saved)["id"] == "coder"


def test_connect_tells_the_agent_to_use_uv_run_not_a_bare_board(board, tmp_path, monkeypatch):
    # A bare `board` is not on PATH unless the project's virtualenv is active.
    monkeypatch.setenv("BOARD_TOKEN_DIR", str(tmp_path / "tokens"))
    out, err = io.StringIO(), io.StringIO()
    assert cli.main(["connect", link(board)], http=TestClient(board.app), out=out, err=err) == 0
    hint = next(line for line in out.getvalue().splitlines() if line.startswith("use it with:"))
    assert "uv run board --as coder me" in hint
    assert " board --as" not in hint.replace("uv run board --as", "")


def test_connect_refuses_plain_http_to_a_remote_host(tmp_path, monkeypatch):
    monkeypatch.setenv("BOARD_TOKEN_DIR", str(tmp_path / "tokens"))
    out, err = io.StringIO(), io.StringIO()
    assert cli.main(["connect", "http://board.example.com/invite/inv_x"], out=out, err=err) == 1
    assert "https" in err.getvalue()
    assert not (tmp_path / "tokens").exists()


# --- Invite page and form details ---------------------------------------------

def test_page_names_shared_lanes_but_only_counts_private_ones(board):
    code = board.invite("alice", "Coder", {"household": RW, "engineering": RW,
                                           "alice-money": READ_ONLY})
    html = TestClient(board.app).get(f"/invite/{code}").text
    assert "household" in html
    assert "engineering" not in html and "alice-money" not in html
    assert "2 private lanes" in html
    one = TestClient(board.app).get(f"/invite/{board.invite('alice', 'Solo', {'engineering': RW})}")
    assert "1 private lane" in one.text and "engineering" not in one.text


def test_redeem_reply_still_names_the_private_lanes(board):
    result = redeem_code(board, board.invite("alice", "Coder", {"engineering": RW})).json()
    assert "engineering" in result["lanes"]


def test_access_log_never_holds_a_live_code(board, caplog):
    code = board.invite("alice", "Coder", {})
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access = logging.getLogger("uvicorn.access")
        fmt = '%s - "%s %s HTTP/%s" %d'  # uvicorn's own access-log format
        access.info(fmt, "1.2.3.4:5", "GET", f"/invite/{code}", "1.1", 200)
        access.info(fmt, "1.2.3.4:5", "POST", f"/v1/invites/{code}/redeem", "1.1", 200)
        access.info(fmt, "1.2.3.4:5", "GET", "/setup/sekrit-setup-token?x=1", "1.1", 200)
        access.info("literal /invite/%s" % code)
    assert caplog.records and code not in caplog.text and "sekrit-setup-token" not in caplog.text
    assert "/invite/[redacted]" in caplog.text and "/v1/invites/[redacted]/redeem" in caplog.text
    assert "GET /healthz" not in caplog.text  # other paths are left alone


def test_access_log_filter_is_installed_once(board):
    create_app(board.app.state.db_path)
    filters = [f for f in logging.getLogger("uvicorn.access").filters
               if type(f).__name__ == "RedactSecretPaths"]
    assert len(filters) == 1


class FakeHttp:
    """Answers a redemption with a canned reply."""

    def __init__(self, status=200, body=None, text=None):
        self.status, self.body, self.text = status, body, text

    def post(self, url, **kwargs):
        import httpx
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.body)


GOOD = {"agent_id": "coder", "token": "fb_x", "lanes": {}, "pending": [], "reconnect": False}


@pytest.mark.parametrize("agent_id", ["coder/../x", "../../etc/passwd", "/abs/path",
                                      "UPPER", "", "a" * 65, "name\n", 7, None])
def test_redeem_refuses_a_hostile_agent_id(agent_id):
    with pytest.raises(BoardClientError):
        redeem("https://board.example.com/invite/inv_x",
               http=FakeHttp(body={**GOOD, "agent_id": agent_id}))


@pytest.mark.parametrize("reply", [
    FakeHttp(body={**GOOD, "token": 5}), FakeHttp(body={**GOOD, "token": ""}),
    FakeHttp(body={"agent_id": "coder"}), FakeHttp(text="<html>not json</html>"),
    FakeHttp(body=["a list"]), FakeHttp(status=302, body=GOOD)])
def test_redeem_refuses_malformed_replies(reply):
    with pytest.raises(BoardClientError):
        redeem("https://board.example.com/invite/inv_x", http=reply)


def test_connect_never_overwrites_a_token_file_for_a_new_agent(board, tmp_path, monkeypatch):
    tokens = tmp_path / "tokens"
    monkeypatch.setenv("BOARD_TOKEN_DIR", str(tokens))
    tokens.mkdir()
    (tokens / "coder.token").write_text("fb_precious\n")
    out, err = io.StringIO(), io.StringIO()
    assert cli.main(["connect", link(board)], http=TestClient(board.app), out=out, err=err) == 1
    assert (tokens / "coder.token").read_text() == "fb_precious\n"
    assert "already exists" in err.getvalue() and "fb_" not in out.getvalue() + err.getvalue()
    # A new invite for this name would be refused: the agent exists now.
    assert "reconnect invite" in err.getvalue() and "new invite" not in err.getvalue()


def test_connect_replaces_the_token_file_on_a_reconnect(board, tmp_path, monkeypatch):
    monkeypatch.setenv("BOARD_TOKEN_DIR", str(tmp_path / "tokens"))
    http = TestClient(board.app)
    assert cli.main(["connect", link(board)], http=http, out=io.StringIO(), err=io.StringIO()) == 0
    old = (tmp_path / "tokens" / "coder.token").read_text()
    r = board.admin("alice").post("/v1/admin/principals/coder/reconnect")
    assert cli.main(["connect", r.json()["url"]], http=http, out=io.StringIO(),
                    err=io.StringIO()) == 0
    new = (tmp_path / "tokens" / "coder.token").read_text()
    assert new != old and me(board, new.strip())["id"] == "coder"


def test_redeem_reply_says_whether_it_was_a_reconnect(board):
    first = redeem_code(board, board.invite("alice", "Coder", {})).json()
    again = redeem_code(board, reconnect_code(board, "alice", "coder")).json()
    assert first["reconnect"] is False and again["reconnect"] is True


def test_resetting_an_admin_cancels_their_open_invites(board):
    code = board.invite("alice", "Coder", {})
    other = board.invite("bob", "Planner", {})
    with service.connect(board.app.state.db_path) as conn:
        service.reset_admin(conn, "alice")
    assert redeem_code(board, code).status_code == 410
    assert redeem_code(board, other).status_code == 200


def test_a_new_reconnect_invite_supersedes_the_older_one(board):
    board.register("alice", "coder")
    older = reconnect_code(board, "alice", "coder")
    newer = reconnect_code(board, "alice", "coder")
    assert redeem_code(board, older).status_code == 410
    assert redeem_code(board, newer).status_code == 200


def test_redeeming_a_reconnect_invite_cancels_the_others_for_that_agent(board):
    board.register("alice", "coder")
    first = reconnect_code(board, "alice", "coder")
    second = reconnect_code(board, "alice", "coder")
    db_exec(board, "UPDATE invites SET status = 'open' WHERE code_hash = ?",
            (hashlib.sha256(first.encode()).hexdigest(),))  # both open, as after a race
    assert redeem_code(board, second).status_code == 200
    assert redeem_code(board, first).status_code == 410


def test_provisional_shared_rights_are_active_and_reported_as_provisional(board):
    # bob has never logged in, so alice's shared-lane change applies at once, to be ratified.
    result = redeem_code(board, board.invite("alice", "Coder", {"household": RW})).json()
    assert result["lanes"]["household"] == RW
    assert [(p["lane"], p["status"]) for p in result["pending"]] == [("household", "provisional")]
    assert me(board, result["token"])["lanes"]["household"] == RW


def test_sole_admin_shared_rights_apply_with_nothing_pending(tmp_path, frozen_time):
    db = tmp_path / "solo.db"
    tokens = provision_family(db, [("alice", "Alice")])
    solo = Board(create_app(db), tokens, frozen_time)
    result = redeem_code(solo, solo.invite("alice", "Coder", {"household": RW})).json()
    assert result["pending"] == [] and result["lanes"]["household"] == RW


@pytest.mark.parametrize("rights", [{"read": "yes"}, {"read": 1}, {"post": "false"},
                                    {"read": True, "post": None}, {"read": []}])
def test_lane_rights_must_be_real_booleans(board, rights):
    r = board.admin("alice").post("/v1/admin/invites", json={
        "display_name": "Coder", "lanes": {"engineering": rights}})
    assert r.status_code == 400
    assert db_rows(board, "SELECT id FROM invites") == []


def test_the_board_command_is_installed_as_an_entry_point():
    import tomllib
    with open("pyproject.toml", "rb") as f:
        assert tomllib.load(f)["project"]["scripts"]["board"] == "client.cli:main"

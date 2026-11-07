"""Hardening for a public URL: cookies, session storage, Origin check, headers,
per-IP rate limits and the health check."""

import hashlib
import sqlite3

from fastapi.testclient import TestClient

from board import limits, service
from board.app import create_app

HTTPS = "https://testserver"


def _client(board, ip="203.0.113.7"):
    return TestClient(board.app, base_url=HTTPS, client=(ip, 50000))


def _bad_login(http, name="alice"):
    return http.post("/login", data={"username": name, "password": "x" * 12, "code": "000000"},
                     follow_redirects=False)


def test_session_cookie_is_secure_httponly_strict(board):
    cookie = board.login_response("alice").headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("secure", "httponly", "samesite=strict"))


def test_cookies_can_be_made_insecure_only_by_asking(board):
    app = create_app(board.app.state.db_path, secure_cookies=False)
    http = TestClient(app)
    r = board.login("alice", board.code("alice"), http=http)
    assert "secure" not in r.headers["set-cookie"].lower()


def test_session_token_is_hashed_at_rest(board):
    token = board.admin("alice").cookies["board_session"]
    with service.connect(board.app.state.db_path) as c:
        assert not c.execute("SELECT 1 FROM sessions WHERE token = ?", (token,)).fetchone()
        assert c.execute("SELECT 1 FROM sessions WHERE token = ?",
                         (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()


def test_hashed_session_still_authenticates_and_logs_out(board):
    http = board.admin("alice")
    assert http.get("/admin", follow_redirects=False).status_code == 200
    http.post("/logout", follow_redirects=False)
    assert http.get("/admin", follow_redirects=False).status_code == 303


def test_migration_4_drops_sessions_stored_in_the_clear(tmp_path):
    from board.migrations import migrate_db
    db = tmp_path / "old.db"
    from board import migrations
    # Build a version-3 database, then add a clear-text session and upgrade.
    saved = migrations.MIGRATIONS
    migrations.MIGRATIONS = saved[:3]
    try:
        migrate_db(db)
    finally:
        migrations.MIGRATIONS = saved
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO principals (id, kind, owner, display_name, created_at) "
                  "VALUES ('a', 'human', 'a', 'A', 'x')")
        c.execute("INSERT INTO admins (name) VALUES ('a')")
        c.execute("INSERT INTO sessions VALUES ('clear-token', 'a', 9999999999)")
    assert migrate_db(db) == len(migrations.MIGRATIONS)
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        c.execute("INSERT INTO rate_limits VALUES ('k', 1.0, 1)")


def test_cross_origin_post_with_session_refused(board):
    r = board.admin("alice").post("/logout", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    # The refused request did nothing: the session still works.
    assert board.admin("alice").get("/admin", follow_redirects=False).status_code == 200


def test_null_origin_refused(board):
    r = board.admin("alice").post("/logout", headers={"origin": "null"})
    assert r.status_code == 403


def test_malformed_origin_is_refused_not_a_crash(board):
    http = TestClient(board.app, base_url=HTTPS, cookies={"board_session": "junk"},
                      raise_server_exceptions=False)
    for bad in ("http://[", "http://[::1", "https://[bad]:1"):
        r = http.post("/logout", headers={"origin": bad})
        assert r.status_code == 403, bad
        assert r.headers["x-frame-options"] == "DENY"


def test_origin_must_match_scheme_and_port(board):
    http = board.admin("alice")
    for bad in ("http://testserver", "https://testserver:8443", "https://testserver.evil.example",
                "ftp://testserver", "testserver"):
        assert http.post("/logout", headers={"origin": bad}).status_code == 403, bad
    assert http.post("/logout", headers={"origin": HTTPS},
                     follow_redirects=False).status_code == 303


def test_http_origin_accepted_only_when_cookies_are_not_secure(board):
    app = create_app(board.app.state.db_path, secure_cookies=False)
    http = TestClient(app)
    board.login("alice", board.code("alice"), http=http)
    assert http.post("/logout", headers={"origin": "http://testserver"},
                     follow_redirects=False).status_code == 303


def test_logout_clears_the_cookie_with_the_same_attributes(board):
    r = board.admin("alice").post("/logout", follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("secure", "httponly", "samesite=strict"))
    assert "max-age=0" in cookie


def test_same_origin_post_allowed(board):
    r = board.admin("alice").post("/logout", headers={"origin": HTTPS},
                                  follow_redirects=False)
    assert r.status_code == 303


def test_cross_origin_get_with_session_is_not_blocked(board):
    r = board.admin("alice").get("/admin", headers={"origin": "https://evil.example"},
                                 follow_redirects=False)
    assert r.status_code == 200


def test_cross_origin_post_without_cookie_is_not_this_checks_business(board):
    r = _client(board).post("/login", headers={"origin": "https://evil.example"},
                            data={"username": "alice", "password": "x" * 12, "code": "000000"})
    assert r.status_code == 401


def test_origin_check_skipped_when_authorization_header_present(board):
    r = board.admin("alice").post("/logout", follow_redirects=False,
                                  headers={"origin": "https://evil.example",
                                           "authorization": "Bearer whatever"})
    assert r.status_code != 403


def test_agent_posts_with_a_token_are_not_affected_by_the_origin_check(board):
    bot = board.register("alice", "bot")
    board.grant("alice", "engineering", "bot")
    r = bot.request("POST", "/v1/lanes/engineering/posts", {"body": "hi", "kind": "note"},
                    origin="https://evil.example")
    assert r.status_code == 201


def test_logged_in_post_with_origin_null_is_still_refused(board):
    r = board.admin("alice").post("/logout", follow_redirects=False,
                                  headers={"origin": "null"})
    assert r.status_code == 403
    r = board.admin("alice").post("/admin/invite", data={"display_name": "X"},
                                  headers={"origin": "null"})
    assert r.status_code == 403


def test_security_headers_present(board):
    h = TestClient(board.app, base_url=HTTPS).get("/login").headers
    assert h["referrer-policy"] == "same-origin"
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"
    csp = h["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert csp == ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                   "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; "
                   "base-uri 'none'")


def test_security_headers_also_on_errors_and_api(board):
    h = TestClient(board.app, base_url=HTTPS).get("/v1/me").headers
    assert h["x-frame-options"] == "DENY"


def test_healthz_exposes_nothing(board):
    r = TestClient(board.app).get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# --- Rate limits --------------------------------------------------------

def test_login_rate_limited_per_ip(board):
    http = _client(board)
    codes = [_bad_login(http).status_code for _ in range(limits.IP_LIMIT[0])]
    assert set(codes) == {401}
    blocked = _bad_login(http)
    assert blocked.status_code == 429
    # A second client address is unaffected.
    assert _bad_login(_client(board, "198.51.100.9")).status_code == 401


def test_a_correct_login_is_refused_too_once_the_ip_is_limited(board):
    http = _client(board)
    for _ in range(limits.IP_LIMIT[0]):
        _bad_login(http)
    r = board.login("alice", board.code("alice"), http=http)
    assert r.status_code == 429 and "set-cookie" not in r.headers


def test_denial_body_is_the_same_every_time(board):
    http = _client(board)
    for _ in range(limits.IP_LIMIT[0]):
        _bad_login(http)
    first, second = _bad_login(http, "alice"), _bad_login(http, "nobody-at-all")
    assert first.status_code == second.status_code == 429
    assert first.text == second.text


def test_denied_attempts_do_not_reach_the_account_lockout_or_password_work(board, monkeypatch):
    http = _client(board)
    for _ in range(limits.IP_LIMIT[0]):
        _bad_login(http)
    monkeypatch.setattr(service, "login", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert _bad_login(http).status_code == 429


def test_setup_is_limited_per_ip_and_separately_from_login(board):
    http = _client(board)
    token = board.tokens["alice"]
    for _ in range(limits.IP_LIMIT[0]):
        assert http.get(f"/setup/{token}x").status_code == 410
    assert http.get(f"/setup/{token}x").status_code == 429
    assert http.get(f"/setup/{token}").status_code == 429
    assert http.post(f"/setup/{token}", data={"password": "p" * 12, "code": "1"}).status_code == 429
    # Login has its own budget, so the setup flood did not spend it.
    assert _bad_login(http).status_code == 401


def test_failing_setup_posts_reach_429(board):
    http = _client(board)
    for _ in range(limits.IP_LIMIT[0]):
        assert http.post("/setup/nope", data={"password": "p" * 12, "code": "1"}).status_code == 410
    assert http.post("/setup/nope", data={"password": "p" * 12, "code": "1"}).status_code == 429


def test_limit_resets_after_the_window(board, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(limits, "_now", lambda: now[0])
    http = _client(board)
    for _ in range(limits.IP_LIMIT[0]):
        _bad_login(http)
    assert _bad_login(http).status_code == 429
    now[0] += limits.IP_LIMIT[1] + 1
    assert _bad_login(http).status_code == 401


def test_allow_counts_per_key_and_window(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(limits, "_now", lambda: now[0])
    with service.connect(_db(tmp_path)) as c:
        assert [limits.allow(c, "k", 3, 60) for _ in range(5)] == [True, True, True, False, False]
        assert limits.allow(c, "other", 3, 60) is True
        now[0] = 161.0
        assert limits.allow(c, "k", 3, 60) is True


def test_allow_deletes_expired_windows(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(limits, "_now", lambda: now[0])
    with service.connect(_db(tmp_path)) as c:
        for i in range(50):
            limits.allow(c, f"login:10.0.0.{i}", 10, 300)
        assert c.execute("SELECT count(*) FROM rate_limits").fetchone()[0] == 50
        now[0] += 301
        limits.allow(c, "login:fresh", 10, 300)
        assert [r[0] for r in c.execute("SELECT key FROM rate_limits")] == ["login:fresh"]


def test_a_denial_survives_the_request_that_raised_it(board):
    http = _client(board)
    for _ in range(limits.IP_LIMIT[0] + 1):
        _bad_login(http)
    with service.connect(board.app.state.db_path) as c:
        row = c.execute("SELECT count FROM rate_limits WHERE key = 'login:203.0.113.7'").fetchone()
    assert row[0] == limits.IP_LIMIT[0] + 1


def _db(tmp_path):
    from board.db import init_db
    path = tmp_path / "limits.db"
    init_db(path)
    return path


# --- Nothing a session rendered stays in the browser's cache ----------------

def test_every_session_page_is_no_store(board):
    http = board.admin("alice")
    for path in ("/", "/lanes/engineering", "/admin", "/admin/invite", "/v1/me",
                 "/v1/lanes/engineering/posts", "/v1/admin/lanes", "/v1/admin/audit"):
        r = http.get(path, follow_redirects=False)
        assert r.status_code == 200, path
        assert r.headers["cache-control"] == "no-store", path


def test_pages_that_answer_a_form_are_no_store_too(board):
    http = board.admin("alice")
    r = board.create_invite_via_form("alice", "fresh-agent")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert http.get("/lanes/bob-money").headers["cache-control"] == "no-store"


def test_after_logout_the_back_button_has_nothing_to_show(board):
    http = board.admin("alice")
    page = http.get("/admin", follow_redirects=False)
    assert page.headers["cache-control"] == "no-store"  # never kept, so Back must re-request
    http.post("/logout", follow_redirects=False)
    again = http.get("/admin", follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == "/login"
    assert again.headers["cache-control"] == "no-store"


def test_static_files_may_still_be_cached(board):
    r = TestClient(board.app, base_url=HTTPS).get("/static/copy.js")
    assert r.status_code == 200 and "no-store" not in r.headers.get("cache-control", "")


# --- Client addresses ------------------------------------------------------

def test_client_key_truncates_ipv6_to_its_64():
    key = limits.client_key
    assert key("2001:db8:1:2:aaaa::1") == key("2001:db8:1:2:bbbb::2") == "2001:db8:1:2::/64"
    assert key("2001:db8:1:3::1") != key("2001:db8:1:2::1")
    assert key("2001:DB8:1:2::1") == "2001:db8:1:2::/64"


def test_client_key_keeps_ipv4_and_folds_mapped_addresses():
    key = limits.client_key
    assert key("203.0.113.7") == "203.0.113.7"
    assert key("::ffff:203.0.113.7") == "203.0.113.7"


def test_client_key_passes_through_what_is_not_an_address():
    assert limits.client_key("testclient") == "testclient"
    assert limits.client_key(None) == "unknown"


def test_every_address_in_a_64_shares_one_rate_limit(board):
    for i in range(limits.IP_LIMIT[0]):
        assert _bad_login(_client(board, f"2001:db8:1:2::{i + 1}")).status_code == 401
    assert _bad_login(_client(board, "2001:db8:1:2:9::9")).status_code == 429
    assert _bad_login(_client(board, "2001:db8:1:3::1")).status_code == 401


def test_redemption_is_limited_by_the_same_key(board):
    for i in range(limits.IP_LIMIT[0]):
        http = _client(board, f"2001:db8:1:2::{i + 1}")
        assert http.post("/v1/invites/nope/redeem").status_code == 404
    assert _client(board, "2001:db8:1:2::ff").post("/v1/invites/nope/redeem").status_code == 429

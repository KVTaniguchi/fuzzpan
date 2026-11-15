"""Human accounts: setup links, password plus TOTP login, lockout, reset."""

import hashlib
import sqlite3
import threading

import pyotp
from fastapi.testclient import TestClient

from board import service
from board.app import create_app
from board.migrations import migrate_db
from board.service import LOCK_AFTER, LOCK_SECONDS, MIN_PASSWORD_CHARS, PAIR_FORGET_SECONDS
from tests.test_migrations import make_v0


def admin_row(board, name="alice"):
    with service.connect(board.app.state.db_path) as c:
        return dict(c.execute("SELECT * FROM admins WHERE name = ?", (name,)).fetchone())


def wrong_code(board, name="alice"):
    """A six-digit code that is not valid at any step the board accepts."""
    totp = pyotp.TOTP(board.totp_secret(name))
    now = int(board.clock.time())
    valid = {totp.at(now + offset) for offset in (-60, -30, 0, 30, 60)}
    return next(c for c in ("000000", "111111", "222222") if c not in valid)


# --- Setup links -----------------------------------------------------------

def test_setup_page_works_without_a_camera(board):
    # Same-phone enrollment: an otpauth link, not only a QR code.
    html = board.setup_page("alice").text
    assert 'href="otpauth://totp/' in html
    assert board.totp_secret("alice") in html
    assert "<svg" in html  # QR for a second device


def test_viewing_the_setup_page_does_not_use_it_up(board):
    first = board.setup_page("alice")
    second = board.setup_page("alice")
    assert first.status_code == second.status_code == 200
    assert board.totp_secret("alice") in second.text
    assert board.finish_setup().status_code == 303


def test_setup_link_is_single_use(board):
    assert board.finish_setup().status_code == 303
    assert board.finish_setup().status_code == 410
    assert board.setup_page("alice").status_code == 410


def test_expired_setup_link_refused(board, frozen_time):
    frozen_time.advance(24 * 3600 + 1)
    assert board.setup_page("alice").status_code == 410
    assert board.finish_setup().status_code == 410
    assert admin_row(board)["pass_hash"] is None


def test_short_password_refused(board):
    r = board.finish_setup(password="x" * (MIN_PASSWORD_CHARS - 1))
    assert r.status_code == 400
    assert admin_row(board)["pass_hash"] is None
    assert board.finish_setup(password="x" * MIN_PASSWORD_CHARS).status_code == 303  # link still live


def test_wrong_code_during_setup_saves_nothing(board):
    r = board.finish_setup(code=wrong_code(board))
    assert r.status_code == 400
    row = admin_row(board)
    assert row["pass_hash"] is None and row["setup_hash"] and row["totp_last_step"] == 0
    assert board.finish_setup().status_code == 303  # same link, right code


def test_setup_token_is_stored_hashed(board):
    token = board.tokens["alice"]
    row = admin_row(board)
    assert row["setup_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in [v for v in row.values() if isinstance(v, str)]


def test_a_setup_link_is_not_issued_over_a_working_account(board):
    board.admin("alice")
    with service.connect(board.app.state.db_path) as c:
        try:
            service.issue_setup_link(c, "alice")
        except service.BoardError as e:
            assert e.status == 409
        else:
            raise AssertionError("expected a refusal")


# --- Login -----------------------------------------------------------------

def test_login_needs_password_and_code(board):
    assert board.login("alice", board.code("alice"), password="not-the-password").status_code == 401
    assert board.login("alice", wrong_code(board)).status_code == 401
    assert board.login("alice", "").status_code == 401
    ok = board.login("alice", board.code("alice"))
    assert ok.status_code == 303 and "board_session" in ok.cookies


def test_login_fields_are_username_password_code(board):
    r = TestClient(board.app).post("/login", data={"admin": "alice", "passphrase": "x"},
                                   follow_redirects=False)
    assert r.status_code == 422


def test_failed_attempt_does_not_use_up_the_code(board):
    code = board.code("alice")
    assert board.login("alice", code, password="wrong-password").status_code == 401
    assert admin_row(board)["totp_last_step"] < int(board.clock.time() // 30)  # unchanged
    assert board.login("alice", code).status_code == 303


def test_migrated_admin_without_totp_cannot_log_in(tmp_path, frozen_time):
    db = make_v0(tmp_path)
    old_password = "old-password-from-before"
    with sqlite3.connect(db) as c:
        c.execute("UPDATE admins SET pass_hash = ? WHERE name = 'legacy-a'",
                  (service.hash_password(old_password),))
        c.execute("INSERT INTO sessions VALUES ('old-session', 'legacy-a', ?)",
                  (frozen_time.time() + 3600,))
    migrate_db(db)
    http = TestClient(create_app(db))
    r = http.post("/login", data={"username": "legacy-a", "password": old_password,
                                  "code": "123456"}, follow_redirects=False)
    assert r.status_code == 401
    http.cookies.set("board_session", "old-session")
    assert http.get("/", follow_redirects=False).status_code == 303
    with service.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        token = service.issue_setup_link(conn, "legacy-a")  # the way back in
    assert TestClient(create_app(db)).get(f"/setup/{token}").status_code == 200


def test_clock_drift_of_one_step_accepted_and_replay_refused(board):
    # One step of clock drift is accepted; replaying the code is refused.
    code = board.code_at("alice", offset_seconds=-30)
    assert board.login("alice", code).status_code == 303
    assert board.login("alice", code).status_code == 401


def test_code_two_steps_out_is_refused(board):
    assert board.login("alice", board.code_at("alice", 90)).status_code == 401


def test_lockout_is_temporary_and_looks_like_a_wrong_password(board, frozen_time):
    # Locking is temporary and indistinguishable from a wrong password.
    for _ in range(LOCK_AFTER):
        wrong = board.login("alice", "000000", password="wrong")
    locked = board.login("alice", board.code("alice"))
    assert locked.status_code == wrong.status_code == 401 and locked.text == wrong.text
    frozen_time.advance(LOCK_SECONDS + 1)
    assert board.login("alice", board.code("alice")).status_code == 303


def test_a_locked_attempt_does_not_use_up_the_code(board, frozen_time):
    for _ in range(LOCK_AFTER):
        board.login("alice", "000000", password="wrong")
    before = admin_row(board)["totp_last_step"]
    assert board.login("alice", board.code("alice")).status_code == 401  # locked
    assert admin_row(board)["totp_last_step"] == before
    frozen_time.advance(LOCK_SECONDS + 1)
    assert board.login("alice", board.code("alice")).status_code == 303


def test_a_good_login_resets_the_failure_count(board):
    for _ in range(LOCK_AFTER - 1):
        board.login("alice", "000000", password="wrong")
    assert board.login("alice", board.code("alice")).status_code == 303
    for _ in range(LOCK_AFTER - 1):
        board.login("alice", "000000", password="wrong")
    board.clock.advance(30)
    assert board.login("alice", board.code("alice")).status_code == 303


def pair(board, client="testclient", name="alice"):
    with service.connect(board.app.state.db_path) as c:
        row = c.execute("SELECT * FROM login_pairs WHERE username = ? AND client = ?",
                        (name, client)).fetchone()
    return dict(row) if row else None


def count(board, table):
    with service.connect(board.app.state.db_path) as c:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def fail_many(board, monkeypatch, n, name="alice", start=0):
    """n real failures, each from its own address, without paying for n scrypt hashes."""
    board.code(name)  # enroll
    with monkeypatch.context() as m:
        m.setattr(service, "_password_ok", lambda *a: False)
        with service.connect(board.app.state.db_path) as c:
            for i in range(start, start + n):
                assert service.login(c, name, "wrong", "000000", client=f"198.51.100.{i}") is None


def test_an_attacker_elsewhere_cannot_lock_the_admin_out(board):
    attacker = board.anon("203.0.113.66")
    admin = board.anon("198.51.100.5")
    for _ in range(LOCK_AFTER):
        assert board.login("alice", "000000", password="wrong", http=attacker).status_code == 401
    assert pair(board, "203.0.113.66")["locked_until"] is not None
    # The attacker's address is locked, even with the right password and code...
    assert board.login("alice", board.code("alice"), http=attacker).status_code == 401
    # ...and the admin, somewhere else, still gets in.
    assert board.login("alice", board.code("alice"), http=admin).status_code == 303


def test_guesses_from_many_addresses_never_lock_the_username(board, monkeypatch):
    # There is no global per-username lock. Each address has its own count, so however
    # many addresses guess, the admin still gets in from another one.
    fail_many(board, monkeypatch, 150)
    assert count(board, "login_pairs") == 150
    with service.connect(board.app.state.db_path) as c:
        assert c.execute("SELECT count(*) FROM login_pairs WHERE locked_until IS NOT NULL").fetchone()[0] == 0
    fresh = board.anon("192.0.2.77")
    assert board.login("alice", board.code("alice"), http=fresh).status_code == 303


def test_there_is_no_username_wide_lock_left_in_the_schema(board):
    with service.connect(board.app.state.db_path) as c:
        columns = {r["name"] for r in c.execute("PRAGMA table_info(admins)")}
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "locked_until" not in columns and "failed_logins" not in columns
    assert "login_failures" not in tables and "login_pairs" in tables


def test_the_lock_on_an_address_is_per_username(board):
    http = board.anon("203.0.113.66")
    for _ in range(LOCK_AFTER):
        board.login("alice", "000000", password="wrong", http=http)
    assert board.login("bob", board.code("bob"), http=http).status_code == 303


def test_a_locked_address_gets_the_same_answer_as_a_wrong_password(board):
    http = board.anon("203.0.113.66")
    for _ in range(LOCK_AFTER):
        wrong = board.login("alice", "000000", password="wrong", http=http)
    locked = board.login("alice", board.code("alice"), http=http)
    assert locked.status_code == wrong.status_code == 401 and locked.text == wrong.text


def test_attempts_from_a_locked_address_are_not_counted(board):
    http = board.anon("203.0.113.66")
    for _ in range(LOCK_AFTER):
        board.login("alice", "000000", password="wrong", http=http)
    locked = pair(board, "203.0.113.66")
    board.login("alice", "000000", password="wrong", http=http)
    assert pair(board, "203.0.113.66") == locked












def test_reset_clears_the_address_locks(board, monkeypatch):
    fail_many(board, monkeypatch, 30)
    assert count(board, "login_pairs") == 30
    with service.connect(board.app.state.db_path) as c:
        token = service.reset_admin(c, "alice")
    assert count(board, "login_pairs") == 0
    board.tokens["alice"], board.passwords["alice"] = token, "a-brand-new-password"
    board._enrolled.discard("alice")
    assert board.login("alice", board.code("alice")).status_code == 303


def test_reset_leaves_another_admins_failures_alone(board, monkeypatch):
    fail_many(board, monkeypatch, 3, name="bob")
    with service.connect(board.app.state.db_path) as c:
        service.reset_admin(c, "alice")
    assert count(board, "login_pairs") == 3


def test_old_pairs_get_pruned_but_a_live_lock_does_not(board):
    locked_ip, idle_ip = board.anon("203.0.113.1"), board.anon("203.0.113.2")
    board.login("alice", "000000", password="wrong", http=idle_ip)
    assert pair(board, "203.0.113.2")
    board.clock.advance(PAIR_FORGET_SECONDS + 1)
    for _ in range(LOCK_AFTER):
        board.login("alice", "000000", password="wrong", http=locked_ip)
    assert pair(board, "203.0.113.2") is None  # idle for over an hour: forgotten
    with service.connect(board.app.state.db_path) as c:
        c.execute("UPDATE login_pairs SET last_failure_at = 0 WHERE client = '203.0.113.1'")
    board.login("alice", "000000", password="wrong", http=idle_ip)
    assert pair(board, "203.0.113.1")["locked_until"] is not None  # still locked: kept
    board.clock.advance(LOCK_SECONDS + 1)
    board.login("alice", "000000", password="wrong", http=idle_ip)
    assert pair(board, "203.0.113.1") is None  # lock over and long idle: gone


def test_unknown_usernames_leave_nothing_behind(board):
    http = board.anon("203.0.113.66")
    for i in range(3):
        http.post("/login", data={"username": f"nobody-{i}", "password": "x", "code": "1"})
    assert count(board, "login_pairs") == 0


def test_a_good_login_forgives_its_address(board):
    http = board.anon("203.0.113.66")
    board.login("alice", "000000", password="wrong", http=http)
    assert board.login("alice", board.code("alice"), http=http).status_code == 303
    assert pair(board, "203.0.113.66") is None


def test_all_addresses_in_one_ipv6_64_share_a_lock(board):
    for i in range(LOCK_AFTER):
        board.login("alice", "000000", password="wrong",
                    http=board.anon(f"2001:db8:1:2::{i + 1}"))
    same_64 = board.anon("2001:db8:1:2:ffff::9")
    other_64 = board.anon("2001:db8:1:3::1")
    assert board.login("alice", board.code("alice"), http=same_64).status_code == 401
    assert board.login("alice", board.code("alice"), http=other_64).status_code == 303


def test_unknown_user_gets_the_same_answer(board):
    wrong = board.login("alice", "000000", password="wrong")
    unknown = TestClient(board.app).post(
        "/login", data={"username": "nobody", "password": "wrong", "code": "000000"},
        follow_redirects=False)
    assert unknown.status_code == wrong.status_code == 401 and unknown.text == wrong.text


def test_unknown_user_still_costs_a_password_hash(board, monkeypatch):
    calls = []
    real = service.hash_password
    monkeypatch.setattr(service, "hash_password", lambda *a, **k: calls.append(1) or real(*a, **k))
    TestClient(board.app).post("/login", data={"username": "nobody", "password": "wrong",
                                               "code": "000000"}, follow_redirects=False)
    assert calls  # same work as a real account, so timing doesn't name users


# --- Reset -----------------------------------------------------------------

def test_reset_clears_access_and_is_visible_to_other_admins(board):
    alice_http = board.admin("alice")
    with service.connect(board.app.state.db_path) as c:
        service.reset_admin(c, "alice")
    assert alice_http.get("/admin", follow_redirects=False).status_code == 303  # session gone
    actions = [e["action"] for e in board.admin("bob").get("/v1/admin/audit").json()]
    assert "admin-reset" in actions


def test_reset_kills_old_credentials_and_issues_a_working_link(board):
    board.admin("alice")
    old_secret = board.totp_secret("alice")
    with service.connect(board.app.state.db_path) as c:
        token = service.reset_admin(c, "alice")
    board.clock.advance(60)
    old_code = pyotp.TOTP(old_secret).at(int(board.clock.time()))
    assert board.login("alice", old_code).status_code == 401
    assert board.setup_page("alice").status_code == 410  # the link from before the reset
    board.tokens["alice"] = token
    board._enrolled.discard("alice")
    board.passwords["alice"] = "a-brand-new-password"
    assert board.login("alice", board.code("alice")).status_code == 303


def test_no_web_route_resets_another_admin(board):
    paths = {r.path for r in board.app.routes}
    assert not any("reset" in p for p in paths)


def test_a_setup_link_recovers_a_locked_account(board, frozen_time):
    for _ in range(LOCK_AFTER):
        board.login("alice", "000000", password="wrong")
    with service.connect(board.app.state.db_path) as c:
        token = service.reset_admin(c, "alice")
    board.tokens["alice"], board.passwords["alice"] = token, "a-brand-new-password"
    board._enrolled.discard("alice")
    # login() enrolls through the new link (which uses up the current step), then
    # the clock moves on to a fresh one.
    assert board.login("alice", board.code("alice")).status_code == 303


# --- Races, odd input, and the page itself --------------------------------

def test_parallel_wrong_attempts_still_lock_the_account(board):
    board.code("alice")  # enroll
    n = LOCK_AFTER + 3
    barrier = threading.Barrier(n)
    real = service._password_ok

    def synced(password, stored):
        result = real(password, stored)
        barrier.wait(timeout=10)  # every attempt has read the row before any writes
        return result

    service._password_ok = synced
    try:
        def attempt():
            with service.connect(board.app.state.db_path) as c:
                assert service.login(c, "alice", "wrong", "000000", client="testclient") is None
        threads = [threading.Thread(target=attempt) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        service._password_ok = real
    assert pair(board)["locked_until"] is not None
    assert board.login("alice", board.code("alice")).status_code == 401




def test_a_lock_set_while_a_good_login_is_in_flight_stops_it(board, monkeypatch):
    code = board.code("alice")
    sessions_before = count(board, "sessions")  # enrolling logged alice in once already
    real = service._password_ok

    def locked_midway(password, stored):
        result = real(password, stored)
        with service.connect(board.app.state.db_path) as c:
            c.execute("INSERT INTO login_pairs (username, client, failures, locked_until, "
                      "last_failure_at) VALUES ('alice', 'testclient', 0, ?, ?)",
                      (board.clock.time() + LOCK_SECONDS, board.clock.time()))
        return result

    monkeypatch.setattr(service, "_password_ok", locked_midway)
    assert board.login("alice", code).status_code == 401
    assert count(board, "sessions") == sessions_before  # the refused login made none


def test_a_reset_during_login_cannot_be_outrun(board, monkeypatch):
    code = board.code("alice")
    real = service._password_ok

    def reset_midway(password, stored):
        result = real(password, stored)  # the password was right...
        with service.connect(board.app.state.db_path) as c:
            service.reset_admin(c, "alice")  # ...then the admin is reset before the claim
        return result

    monkeypatch.setattr(service, "_password_ok", reset_midway)
    assert board.login("alice", code).status_code == 401
    with service.connect(board.app.state.db_path) as c:
        assert c.execute("SELECT count(*) FROM sessions WHERE admin = 'alice'").fetchone()[0] == 0


def test_non_ascii_code_is_an_ordinary_failure(board):
    assert board.finish_setup(code="\u0663\u0664\u0665\u0666\u0667\u0668").status_code == 400
    r = board.login("alice", "12\u00e9456")  # enrolls, then fails like any wrong code
    assert r.status_code == 401
    assert pair(board)["failures"] == 1


def test_setup_page_is_not_cached(board):
    assert board.setup_page("alice").headers["cache-control"] == "no-store"
    board.finish_setup()
    assert board.setup_page("alice").headers["cache-control"] == "no-store"  # the 410 page too


def test_after_setup_the_old_link_says_to_wait_for_a_new_code(board):
    # Setup logs you in, but a code is used up: someone who comes back to the link
    # to log in again is told to wait for the next one.
    r = board.finish_setup()
    assert r.status_code == 303 and r.headers["location"] == "/welcome"
    page = TestClient(board.app).get(f"/setup/{board.tokens['alice']}").text
    assert "already" in page and "wait for the next" in page and 'href="/login"' in page

"""Changing your own login from inside the board: a new password, or a new
authenticator, each only after proving the password and a current code again."""

import pyotp
from fastapi.testclient import TestClient

from board import service
from board.service import AUTHENTICATOR_SECONDS, LOCK_AFTER, MIN_PASSWORD_CHARS
from tests.conftest import HTTPS
from tests.test_accounts import admin_row, wrong_code

NEW_PASSWORD = "a brand new passphrase"


def change_password(board, http, name="alice", current=None, code=None,
                    new=NEW_PASSWORD, confirm=None):
    return http.post("/account/password", follow_redirects=False, data={
        "current_password": board.passwords[name] if current is None else current,
        "code": board.code(name) if code is None else code,
        "new_password": new,
        "new_password_confirm": new if confirm is None else confirm})


def start_authenticator(board, http, name="alice", password=None, code=None):
    return http.post("/account/authenticator", follow_redirects=False, data={
        "password": board.passwords[name] if password is None else password,
        "code": board.code(name) if code is None else code})


def admin(board, name="alice"):
    """A logged-in admin, with the clock moved past the code the login spent."""
    http = board.admin(name)
    board.clock.advance(60)
    return http


def second_browser(board, name="alice"):
    """Another logged-in session of the same admin, on a later code."""
    board.clock.advance(60)
    http = TestClient(board.app, base_url=HTTPS)
    assert board.login(name, board.code(name), http=http).status_code == 303
    board.clock.advance(60)
    return http


def logged_in(http):
    return http.get("/admin", follow_redirects=False).status_code == 200


def audit_actions(board, name):
    return [e["action"] for e in board.admin(name).get("/v1/admin/audit").json()]


def test_account_page_needs_a_login(board):
    anon = TestClient(board.app, base_url=HTTPS)
    assert anon.get("/account", follow_redirects=False).headers["location"] == "/login"
    for path in ("/account/password", "/account/authenticator",
                 "/account/authenticator/confirm"):
        r = anon.post(path, data={"current_password": "x", "password": "x", "code": "1",
                                  "new_password": "x"}, follow_redirects=False)
        assert r.headers["location"] == "/login"


def test_an_agent_token_cannot_reach_the_account_pages(board):
    agent = board.register("alice", "coder")
    r = agent.http.get("/account", headers={"Authorization": f"Bearer {agent.token}"},
                       follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_the_admin_header_links_to_the_account_page(board):
    html = board.admin("alice").get("/").text
    assert 'href="/account"' in html
    page = board.admin("alice").get("/account").text
    assert "Change your password" in page and "Replace your authenticator" in page


# --- Password ------------------------------------------------------------------

def test_change_password_with_password_and_code(board):
    http = admin(board)
    other = second_browser(board)
    r = change_password(board, http)
    assert r.status_code == 303 and r.headers["location"] == "/account?done=password"
    # This browser stays logged in on a fresh session; the other one is out.
    assert logged_in(http) and not logged_in(other)
    assert "Password changed" in http.get("/account?done=password").text
    board.clock.advance(60)
    assert board.login("alice", board.code("alice")).status_code == 401  # old password
    board.clock.advance(60)
    assert board.login("alice", board.code("alice"), password=NEW_PASSWORD).status_code == 303


def test_the_change_is_in_both_admins_audit_logs(board):
    http = admin(board)
    assert change_password(board, http).status_code == 303
    assert "password-changed" in audit_actions(board, "alice")
    assert "password-changed" in audit_actions(board, "bob")


def test_wrong_current_password_or_code_changes_nothing(board):
    http = admin(board)
    before = admin_row(board)
    for kwargs in ({"current": "not the password at all"}, {"code": wrong_code(board)}):
        r = change_password(board, http, **kwargs)
        assert r.status_code == 401 and "Wrong password or code" in r.text
        assert admin_row(board)["pass_hash"] == before["pass_hash"]
        assert logged_in(http)


def test_a_session_alone_is_not_enough(board):
    # Someone at an unlocked computer knows neither the password nor the code.
    r = board.admin("alice").post("/account/password", follow_redirects=False,
                                  data={"current_password": "a good guess", "code": "123456",
                                        "new_password": NEW_PASSWORD})
    assert r.status_code == 401
    r = board.admin("alice").post("/account/authenticator", follow_redirects=False,
                                  data={"password": "a good guess", "code": "123456"})
    assert r.status_code == 401


def test_mismatched_or_short_new_password_saves_nothing_and_keeps_the_code(board):
    http = admin(board)
    before = admin_row(board)
    code = board.code("alice")
    r = change_password(board, http, code=code, confirm="something else entirely")
    assert r.status_code == 400 and "did not match" in r.text
    short = "x" * (MIN_PASSWORD_CHARS - 1)
    r = change_password(board, http, code=code, new=short, confirm=short)
    assert r.status_code == 400
    after = admin_row(board)
    assert after["pass_hash"] == before["pass_hash"]
    assert after["totp_last_step"] == before["totp_last_step"]  # code not used up
    assert change_password(board, http, code=code).status_code == 303


def test_a_code_used_to_change_the_password_does_not_work_again(board):
    http = admin(board)
    code = board.code("alice")
    assert change_password(board, http, code=code).status_code == 303
    assert board.login("alice", code, password=NEW_PASSWORD).status_code == 401


def test_failed_proofs_count_towards_the_login_lock(board):
    http = admin(board)
    for _ in range(LOCK_AFTER):
        assert change_password(board, http, current="wrong wrong wrong").status_code == 401
    # Locked for this address now: the right password and code are refused too,
    # here and at the login form.
    assert change_password(board, http).status_code == 401
    assert board.login("alice", board.code("alice")).status_code == 401
    assert logged_in(http)  # the lock does not end the session


# --- Authenticator -------------------------------------------------------------

def test_replace_the_authenticator(board):
    http = admin(board)
    other = second_browser(board)
    old_secret = board.totp_secret("alice")
    r = start_authenticator(board, http)
    assert r.status_code == 303 and r.headers["location"] == "/account/authenticator"
    page = http.get("/account/authenticator")
    assert page.status_code == 200 and 'href="otpauth://totp/' in page.text and "<svg" in page.text
    with service.connect(board.app.state.db_path) as c:
        new_secret = c.execute("SELECT totp_pending FROM sessions WHERE totp_pending "
                               "IS NOT NULL").fetchone()[0]
    assert new_secret != old_secret and new_secret in page.text
    # Nothing has changed yet: the old authenticator still works.
    assert board.totp_secret("alice") == old_secret and logged_in(other)

    new_code = pyotp.TOTP(new_secret).at(int(board.clock.time()))
    r = http.post("/account/authenticator/confirm", data={"code": new_code},
                  follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account?done=authenticator"
    assert board.totp_secret("alice") == new_secret
    assert logged_in(http) and not logged_in(other)
    assert "authenticator-replaced" in audit_actions(board, "bob")

    board.clock.advance(60)
    old_code = pyotp.TOTP(old_secret).at(int(board.clock.time()))
    assert board.login("alice", old_code).status_code == 401
    assert board.login("alice", new_code).status_code == 401  # spent by the switch
    board.clock.advance(60)
    assert board.login("alice", pyotp.TOTP(new_secret).at(int(board.clock.time()))) \
        .status_code == 303


def test_starting_needs_the_password_and_a_current_code(board):
    http = admin(board)
    for kwargs in ({"password": "not the password at all"}, {"code": wrong_code(board)}):
        assert start_authenticator(board, http, **kwargs).status_code == 401
    r = http.get("/account/authenticator")
    assert "otpauth://" not in r.text and "expired or was not started" in r.text


def test_a_wrong_code_from_the_new_app_changes_nothing(board):
    http = admin(board)
    old_secret = board.totp_secret("alice")
    assert start_authenticator(board, http).status_code == 303
    r = http.post("/account/authenticator/confirm", data={"code": "000000"})
    assert r.status_code == 400 and "did not match" in r.text and "otpauth://" in r.text
    # A code from the old authenticator does not confirm the new one either.
    old_code = pyotp.TOTP(old_secret).at(int(board.clock.time()))
    assert http.post("/account/authenticator/confirm", data={"code": old_code}).status_code == 400
    assert board.totp_secret("alice") == old_secret


def test_the_new_secret_belongs_to_the_browser_that_asked(board):
    http = admin(board)
    other = second_browser(board)
    assert start_authenticator(board, http).status_code == 303
    assert "otpauth://" not in other.get("/account/authenticator").text
    with service.connect(board.app.state.db_path) as c:
        secret = c.execute("SELECT totp_pending FROM sessions WHERE totp_pending "
                           "IS NOT NULL").fetchone()[0]
    code = pyotp.TOTP(secret).at(int(board.clock.time()))
    assert other.post("/account/authenticator/confirm", data={"code": code}).status_code == 410
    assert board.totp_secret("alice") != secret
    assert "otpauth://" not in board.admin("bob").get("/account/authenticator").text


def test_an_unconfirmed_change_expires(board):
    http = admin(board)
    assert start_authenticator(board, http).status_code == 303
    with service.connect(board.app.state.db_path) as c:
        secret = c.execute("SELECT totp_pending FROM sessions WHERE totp_pending "
                           "IS NOT NULL").fetchone()[0]
    board.clock.advance(AUTHENTICATOR_SECONDS + 1)
    assert "otpauth://" not in http.get("/account/authenticator").text
    code = pyotp.TOTP(secret).at(int(board.clock.time()))
    assert http.post("/account/authenticator/confirm", data={"code": code}).status_code == 410
    assert board.totp_secret("alice") != secret


def test_a_host_reset_cancels_a_change_in_progress(board):
    http = admin(board)
    assert start_authenticator(board, http).status_code == 303
    with service.connect(board.app.state.db_path) as c:
        service.reset_admin(c, "alice")
    assert not logged_in(http)
    with service.connect(board.app.state.db_path) as c:
        assert c.execute("SELECT count(*) FROM sessions WHERE totp_pending IS NOT NULL"
                         ).fetchone()[0] == 0


def test_reauthenticating_is_not_a_login(board):
    # last_login_at marks onboarding and lets provisional changes be ratified (§6);
    # proving yourself inside the board must not move it.
    http = admin(board)
    before = admin_row(board)["last_login_at"]
    board.clock.advance(60)
    assert start_authenticator(board, http).status_code == 303
    assert admin_row(board)["last_login_at"] == before

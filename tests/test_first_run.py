"""The first-run path: finishing setup logs the admin in and lands on one welcome
page that asks which lanes their first agent should use, then hands back one link."""

import json
import re
import sqlite3

import pyotp
from fastapi.testclient import TestClient

from board import service
from tests.conftest import HTTPS


def finish(board, name):
    """Finish setup the way a browser does. Returns (client holding any cookie, response)."""
    http = TestClient(board.app, base_url=HTTPS)
    code = pyotp.TOTP(board.totp_secret(name)).at(int(board.clock.time()))
    r = http.post(f"/setup/{board.tokens[name]}", follow_redirects=False,
                  data={"password": board.passwords[name], "code": code})
    return http, r


def invite_rows(board):
    conn = sqlite3.connect(board.app.state.db_path)
    try:
        return conn.execute("SELECT agent_id, display_name, owner, lanes_json, status "
                            "FROM invites").fetchall()
    finally:
        conn.close()


def test_finishing_setup_logs_you_in_and_lands_on_the_welcome_page(board):
    http, r = finish(board, "bob")
    assert r.status_code == 303 and r.headers["location"] == "/welcome"
    assert "httponly" in r.headers["set-cookie"].lower() and "secure" in r.headers["set-cookie"].lower()
    page = http.get("/welcome")
    assert page.status_code == 200 and "Which lanes" in page.text
    assert http.get("/admin").status_code == 200  # a real session, not just this page


def test_finishing_setup_marks_the_admin_onboarded(board):
    with service.connect(board.app.state.db_path) as c:
        assert not service.is_onboarded(c, "bob")
    finish(board, "bob")
    with service.connect(board.app.state.db_path) as c:
        assert service.is_onboarded(c, "bob")


def test_a_failed_setup_sets_no_cookie_and_logs_nobody_in(board):
    http = TestClient(board.app, base_url=HTTPS)
    r = http.post(f"/setup/{board.tokens['bob']}", follow_redirects=False,
                  data={"password": "short", "code": "1"})
    assert r.status_code == 400 and "set-cookie" not in r.headers
    assert http.get("/welcome", follow_redirects=False).status_code == 303


def test_setup_page_names_the_username_and_asks_for_the_password_twice(board):
    page = TestClient(board.app, base_url=HTTPS).get(f"/setup/{board.tokens['bob']}").text
    # So a password manager saves the new password under the right username.
    assert re.search(r'<input name="username" value="bob" autocomplete="username" readonly>',
                     page)
    assert 'name="password_confirm"' in page


def test_mismatched_passwords_save_nothing_and_the_link_still_works(board):
    http = TestClient(board.app, base_url=HTTPS)
    code = pyotp.TOTP(board.totp_secret("bob")).at(int(board.clock.time()))
    r = http.post(f"/setup/{board.tokens['bob']}", follow_redirects=False,
                  data={"password": board.passwords["bob"], "code": code,
                        "password_confirm": board.passwords["bob"] + "x"})
    assert r.status_code == 400 and "did not match" in r.text and "set-cookie" not in r.headers
    with service.connect(board.app.state.db_path) as c:
        assert not service.is_onboarded(c, "bob")
    r = http.post(f"/setup/{board.tokens['bob']}", follow_redirects=False,
                  data={"password": board.passwords["bob"], "code": code,
                        "password_confirm": board.passwords["bob"]})
    assert r.status_code == 303 and r.headers["location"] == "/welcome"


def test_a_second_tap_on_finish_setup_sets_no_cookie(board):
    finish(board, "bob")
    http, r = finish(board, "bob")
    assert r.status_code == 410 and "set-cookie" not in r.headers


def test_the_welcome_page_needs_a_login(board):
    http = TestClient(board.app, base_url=HTTPS)
    r = http.get("/welcome", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = http.post("/welcome", data={"display_name": "x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert invite_rows(board) == []


def test_the_setup_page_tells_you_your_username(board):
    html = board.setup_page("bob").text
    assert "username" in html.lower() and "<strong>bob</strong>" in html


def test_the_welcome_page_asks_one_question_with_nothing_ticked(board):
    http, _ = finish(board, "bob")
    html = http.get("/welcome").text
    assert "Which lanes" in html
    assert 'value="alice-agent"' not in html and 'value="bob-agent"' in html
    # Only lanes bob can hand out: shared ones and his own private one.
    for slug in ("household", "trips", "household-money", "bob-money"):
        assert f'name="lane_{slug}"' in html
    for slug in ("alice-money", "engineering", "health"):
        assert f'name="lane_{slug}"' not in html
    assert "checked" not in re.sub(r"<script.*?</script>", "", html, flags=re.S)


def test_the_default_name_skips_one_already_taken(board):
    board.register("alice", "bob-agent")
    http, _ = finish(board, "bob")
    assert 'value="bob-agent-2"' in http.get("/welcome").text


def test_ticking_lanes_makes_one_link_for_read_and_post_on_each(board):
    http, _ = finish(board, "bob")
    r = http.post("/welcome", data={"display_name": "bob-agent", "lane_household": "on",
                                    "lane_bob-money": "on"})
    assert r.status_code == 200
    (agent_id, display, owner, lanes_json, status), = invite_rows(board)
    assert (agent_id, owner, status) == ("bob-agent", "bob", "open")
    assert json.loads(lanes_json) == {"household": {"read": True, "post": True},
                                      "bob-money": {"read": True, "post": True}}
    link = re.search(r'value="(https://[^"]+/invite/[^"]+)"', r.text)
    assert link and "Copy link" in r.text


def test_no_lane_is_a_fine_answer(board):
    http, _ = finish(board, "bob")
    r = http.post("/welcome", data={"display_name": "bob-agent"})
    assert r.status_code == 200 and "/invite/" in r.text
    assert json.loads(invite_rows(board)[0][3]) == {}


def test_shared_lanes_are_flagged_as_needing_the_other_admin(board):
    http, _ = finish(board, "bob")
    r = http.post("/welcome", data={"display_name": "bob-agent", "lane_household": "on"})
    assert "alice" in r.text and "household" in r.text and "approve" in r.text.lower()
    r2 = http.post("/welcome", data={"display_name": "bob-private", "lane_bob-money": "on"})
    assert "approve" not in r2.text.lower()  # a private lane applies at once


def test_changes_made_before_you_joined_are_pointed_out_without_blocking(board):
    alice = board.admin("alice")
    agent = board.register("alice", "a-bot")
    assert board.grant("alice", "household", agent.id).json()["status"] == "provisional"
    http, _ = finish(board, "bob")
    welcome = http.get("/welcome").text
    assert "Create my link" in welcome  # the link comes first
    r = http.post("/welcome", data={"display_name": "bob-agent"})
    assert "1 change" in r.text and 'href="/admin"' in r.text


def test_no_banner_when_nothing_awaits_you(board):
    http, _ = finish(board, "bob")
    r = http.post("/welcome", data={"display_name": "bob-agent"})
    assert "to review" not in r.text.lower()


def test_a_taken_name_shows_the_form_again_with_the_error_and_your_ticks(board):
    board.register("alice", "taken")
    http, _ = finish(board, "bob")
    r = http.post("/welcome", data={"display_name": "taken", "lane_trips": "on"})
    assert r.status_code == 409 and "taken" in r.text and "<form" in r.text
    assert re.search(r'name="lane_trips"[^>]*checked', r.text)
    assert [row for row in invite_rows(board) if row[4] == "open"] == []  # nothing new was made


def test_lane_names_are_escaped_on_the_welcome_page(board):
    with service.connect(board.app.state.db_path) as c:
        c.execute("UPDATE lanes SET name = '<script>alert(1)</script>' WHERE slug = 'trips'")
    http, _ = finish(board, "bob")
    html = http.get("/welcome").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_an_unknown_or_unadministered_lane_cannot_be_smuggled_in(board):
    http, _ = finish(board, "bob")
    r = http.post("/welcome", data={"display_name": "bob-agent", "lane_alice-money": "on"})
    assert r.status_code in (403, 404)
    assert invite_rows(board) == []

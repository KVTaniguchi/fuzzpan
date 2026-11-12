"""Bad input gets a 4xx, never a 500. The API always answers {"error": ...}, and
people filling in forms always get a plain page, never raw JSON."""

import pytest

from board import limits, service
from board.service import MAX_REQUEST_BYTES


@pytest.fixture
def bot(board):
    return board.agent("alice", "bot", "engineering")


def raw_post(agent, path, content):
    return agent.http.post(path, content=content, headers={
        "Authorization": f"Bearer {agent.token}", "Content-Type": "application/json"})


def is_html(r):
    return r.headers["content-type"].startswith("text/html") and "<html" in r.text


# --- Reachable 500s ---------------------------------------------------------

@pytest.mark.parametrize("since", [2**64, -(2**64), 2**63, 10**30])
def test_since_out_of_range_is_a_400(bot, since):
    r = bot.read("engineering", since=since)
    assert r.status_code == 400 and "since" in r.json()["error"]


def test_since_at_the_edge_of_64_bits_works(bot):
    assert bot.read("engineering", since=2**63 - 1).status_code == 200
    assert bot.read("engineering", since=-(2**63)).status_code == 200


def test_a_huge_limit_is_only_clamped(bot):
    assert bot.read("engineering", limit=2**70).status_code == 200


@pytest.mark.parametrize("kind", [["note"], {"note": 1}, 7, None, True, "\ud800"])
def test_kind_of_the_wrong_type_is_a_400(bot, kind):
    r = bot.post("engineering", "hello", kind=kind)
    assert r.status_code == 400 and "kind" in r.json()["error"]


@pytest.mark.parametrize("text", ['"\\ud800"', '"ok \\udfff ok"'])
def test_a_lone_surrogate_in_a_body_is_a_400(bot, text):
    r = raw_post(bot, "/v1/lanes/engineering/posts", f'{{"body": {text}}}'.encode())
    assert r.status_code == 400 and "error" in r.json()
    assert bot.read("engineering").json()["posts"] == []


def test_a_real_pair_of_surrogates_is_just_an_emoji(bot):
    r = raw_post(bot, "/v1/lanes/engineering/posts", b'{"body": "\\ud83d\\ude00"}')
    assert r.status_code == 201
    assert bot.read("engineering").json()["posts"][0]["body"] == "\U0001f600"


def test_a_lone_surrogate_in_an_invite_is_refused(board):
    alice = board.admin("alice")
    r = alice.post("/v1/admin/invites", content=b'{"display_name": "x\\ud800", "lanes": {}}')
    assert r.status_code == 400
    r = alice.post("/v1/admin/invites", content=b'{"display_name": "x", "lanes": {"\\ud800": {}}}')
    assert r.status_code == 404


@pytest.mark.parametrize("content", [b"not json", b"[1, 2]", b'"text"', b"7", b"null", b"\xff\xfe", b""])
def test_put_grant_with_bad_json_or_a_non_object_is_a_400(board, content):
    board.register("alice", "planner")
    r = board.admin("alice").put("/v1/admin/lanes/engineering/grants/planner", content=content)
    assert r.status_code == 400 and "JSON" in r.json()["error"]


@pytest.mark.parametrize("route", ["post", "invite", "grant"])
def test_deeply_nested_json_is_a_400(board, bot, route):
    deep = b"[" * 16000
    if route == "post":
        r = raw_post(bot, "/v1/lanes/engineering/posts", deep)
    elif route == "invite":
        r = board.admin("alice").post("/v1/admin/invites", content=deep)
    else:
        r = board.admin("alice").put("/v1/admin/lanes/engineering/grants/bot", content=deep)
    assert r.status_code == 400


@pytest.mark.parametrize("path", ["/v1/admin/invites/{}/cancel", "/v1/admin/pending/{}/approve",
                                  "/admin/invites/{}/cancel", "/admin/pending/{}/reject"])
def test_an_id_too_big_for_the_database_is_a_404(board, path):
    assert board.admin("alice").post(path.format(2**64)).status_code == 404


def test_invalid_utf8_in_a_multipart_form_is_a_4xx(board):
    alice = board.admin("alice")
    boundary = "xxBOUNDARYxx"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"display_name\"\r\n\r\n".encode()
            + b"\xff\xfe\r\n" + f"--{boundary}--\r\n".encode())
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    for path in ("/admin/invite", "/admin/grant", "/lanes/household/post", "/login"):
        assert 400 <= alice.post(path, content=body, headers=headers).status_code < 500, path


# --- The API's error shape --------------------------------------------------

def test_a_validation_error_uses_the_error_key(board, bot):
    r = bot.request("GET", "/v1/lanes/engineering/posts?since=abc")
    assert r.status_code == 422 and set(r.json()) == {"error"} and "since" in r.json()["error"]


def test_an_unknown_route_and_wrong_method_use_the_error_key(bot):
    for r, status in ((bot.request("GET", "/v1/nope"), 404),
                      (bot.request("DELETE", "/v1/me"), 405)):
        assert r.status_code == status and set(r.json()) == {"error"}
    assert "allow" in bot.request("DELETE", "/v1/me").headers


def test_api_errors_stay_json_for_a_logged_in_admin_too(board):
    r = board.admin("alice").get("/v1/lanes/bob-money/posts")
    assert r.status_code == 404 and r.json() == {"error": "not found"}


# --- Real booleans ----------------------------------------------------------

@pytest.mark.parametrize("field", ["can_read", "can_post"])
@pytest.mark.parametrize("value", ["false", "true", 1, 0, None, [], {}])
def test_grant_rights_must_be_real_booleans(board, field, value):
    planner = board.register("alice", "planner")
    payload = {"can_read": False, "can_post": False, field: value}
    r = board.admin("alice").put("/v1/admin/lanes/engineering/grants/planner", json=payload)
    assert r.status_code == 400 and "true or false" in r.json()["error"]
    assert planner.request("GET", "/v1/me").json()["lanes"] == {}


def test_the_grant_form_still_works(board):
    board.register("alice", "planner")
    alice = board.admin("alice")
    r = alice.post("/admin/grant", data={"lane": "engineering", "principal": "planner",
                                         "can_read": "true"}, follow_redirects=False)
    assert r.status_code == 303
    assert alice.get("/v1/admin/lanes").json()[-2]["grants"] == [
        {"principal": "planner", "read": True, "post": False}]


# --- Forms answer people in plain pages -------------------------------------

def test_login_429_is_a_page_with_the_form_still_there(board):
    http = board.anon("203.0.113.7")
    for _ in range(limits.IP_LIMIT[0]):
        http.post("/login", data={"username": "a", "password": "b", "code": "1"})
    r = http.post("/login", data={"username": "a", "password": "b", "code": "1"})
    assert r.status_code == 429 and is_html(r)
    assert "Too many attempts" in r.text and 'action="/login"' in r.text


def test_setup_429_is_a_page(board):
    http = board.anon("203.0.113.7")
    for _ in range(limits.IP_LIMIT[0]):
        http.get("/setup/nope")
    r = http.get("/setup/nope")
    assert r.status_code == 429 and is_html(r) and "Too many attempts" in r.text


@pytest.mark.parametrize("path,status", [
    ("/admin/revoke/nobody", 404), ("/admin/reconnect/nobody", 404),
    ("/admin/invites/999/cancel", 404), ("/admin/pending/999/approve", 404),
    ("/admin/pending/999/maybe", 404),
])
def test_failed_admin_buttons_answer_with_a_page(board, path, status):
    r = board.admin("alice").post(path)
    assert r.status_code == status and is_html(r)
    assert 'href="/admin"' in r.text and "Log out" in r.text and '{"error"' not in r.text


def test_a_stale_approval_button_explains_itself(board):
    board.register("alice", "planner")
    pid = board.grant("alice", "household", "planner").json()["pending_id"]
    bob = board.admin("bob")
    assert bob.post(f"/admin/pending/{pid}/reject", follow_redirects=False).status_code == 303
    r = bob.post(f"/admin/pending/{pid}/reject")
    assert r.status_code == 404 and is_html(r) and "already" in r.text


def test_approving_your_own_change_on_the_page_explains_itself(board):
    board.admin("bob")
    board.register("alice", "planner")
    pid = board.grant("alice", "household", "planner").json()["pending_id"]
    r = board.admin("alice").post(f"/admin/pending/{pid}/approve")
    assert r.status_code == 403 and is_html(r) and "Another admin" in r.text


def test_grant_form_errors_are_pages(board):
    alice = board.admin("alice")
    board.register("alice", "planner")
    for data, status in (({"lane": "bob-money", "principal": "planner"}, 404),
                         ({"lane": "engineering", "principal": "nobody"}, 404),
                         ({"lane": "engineering"}, 422)):
        r = alice.post("/admin/grant", data=data)
        assert r.status_code == status and is_html(r), data


def test_the_lane_post_form_errors_are_pages(board):
    alice = board.admin("alice")
    assert is_html(alice.post("/lanes/household/post", data={"body": "x" * 9000}))
    r = alice.post("/lanes/household/post", data={"body": "x", "kind": "nonsense"})
    assert r.status_code == 400 and is_html(r)
    r = alice.post("/lanes/household/post", data={})
    assert r.status_code == 422 and is_html(r) and "field" not in r.text.lower().split("<main>")[1]


def test_a_broken_form_body_is_a_page(board):
    r = board.anon().post("/login", content=b"x", headers={"Content-Type": "multipart/form-data"})
    assert 400 <= r.status_code < 500 and is_html(r)


def test_an_unknown_page_is_a_page_and_escapes_nothing_into_it(board):
    r = board.anon().get("/no/<script>alert(1)</script>")
    assert r.status_code == 404 and is_html(r) and "<script>alert" not in r.text
    assert 'href="/"' in r.text and "Log out" not in r.text


def test_a_too_large_form_is_a_page(board):
    r = board.anon().post("/login", content=b"x" * (MAX_REQUEST_BYTES + 1),
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413 and is_html(r) and "too large" in r.text.lower()
    assert r.headers["x-frame-options"] == "DENY"


def test_lane_page_refusals_are_pages(board):
    r = board.admin("alice").get("/lanes/bob-money")
    assert r.status_code == 404 and is_html(r) and "bob-money" not in r.text.replace("/lanes/bob-money", "")


def test_setting_rights_with_no_active_agents_offers_no_form(board):
    page = board.admin("alice").get("/admin").text
    assert 'action="/admin/grants"' not in page and "No active agents" in page
    board.register("alice", "bot")
    bot_page = board.admin("alice").get("/admin").text
    assert 'action="/admin/grants"' in bot_page and "No active agents" not in bot_page
    board.admin("alice").post("/v1/admin/principals/bot/revoke")
    assert 'action="/admin/grants"' not in board.admin("alice").get("/admin").text


# --- A second tap on Finish setup -------------------------------------------

def test_second_submit_of_a_finished_setup_says_it_is_done_and_links_to_login(board):
    assert board.finish_setup().status_code == 303
    r = board.finish_setup()
    assert r.status_code == 410 and is_html(r)
    assert "already" in r.text and 'href="/login"' in r.text and "expired" not in r.text
    assert "Ask the person" not in r.text


def test_viewing_a_finished_setup_link_says_the_same(board):
    board.finish_setup()
    r = board.anon().get(f"/setup/{board.tokens['alice']}")
    assert r.status_code == 410 and "already" in r.text and 'href="/login"' in r.text
    assert board.totp_secret("alice") not in r.text


def test_the_second_submit_changes_nothing(board):
    board.finish_setup()
    with service.connect(board.app.state.db_path) as c:
        before = dict(c.execute("SELECT * FROM admins WHERE name = 'alice'").fetchone())
    board.passwords["alice"] = "a-different-password"
    assert board.finish_setup().status_code == 410
    with service.connect(board.app.state.db_path) as c:
        assert dict(c.execute("SELECT * FROM admins WHERE name = 'alice'").fetchone()) == before


def test_a_link_from_before_a_reset_is_expired_not_done(board):
    board.finish_setup()
    old = board.tokens["alice"]
    with service.connect(board.app.state.db_path) as c:
        service.reset_admin(c, "alice")
    r = board.anon().get(f"/setup/{old}")
    assert r.status_code == 410 and "expired" in r.text and "already done" not in r.text


def test_a_never_used_expired_link_still_says_expired(board, frozen_time):
    frozen_time.advance(24 * 3600 + 1)
    r = board.anon().get(f"/setup/{board.tokens['alice']}")
    assert r.status_code == 410 and "expired" in r.text


def test_setup_form_errors_still_show_the_form(board):
    r = board.anon().post(f"/setup/{board.tokens['alice']}", data={"password": "short", "code": "1"})
    assert r.status_code == 400 and is_html(r) and "<form" in r.text


# --- The link page has the header -------------------------------------------

def test_the_new_invite_link_page_has_the_header(board):
    r = board.create_invite_via_form("alice", "fresh-agent")
    assert r.status_code == 200 and "Log out" in r.text and 'href="/admin"' in r.text
    assert "alice" in r.text.split("</header>")[0]


def test_the_reconnect_link_page_has_the_header(board):
    board.register("alice", "bot")
    r = board.admin("alice").post("/admin/reconnect/bot")
    assert r.status_code == 200 and "Log out" in r.text

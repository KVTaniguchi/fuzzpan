"""The admin page: inviting an agent in three clicks, and the agent list."""

import re
import tomllib

from tests.conftest import ROOT, checked


def link_from(html):
    return re.search(r'https://testserver/invite/inv_[A-Za-z0-9_-]+', html).group(0)


def redeem_link(board, link):
    code = link.rsplit("/", 1)[1]
    return board.anon().post(f"/v1/invites/{code}/redeem")


def test_invite_form_preselects_shared_lanes_only(board):
    html = board.admin("bob").get("/admin/invite").text
    assert checked(html, "household", "post") and checked(html, "household", "read")
    assert not checked(html, "bob-money", "read") and not checked(html, "bob-money", "post")


def test_invite_form_hides_other_admins_private_lanes(board):
    assert "alice-money" not in board.admin("bob").get("/admin/invite").text


def test_creating_an_invite_shows_the_full_link_with_copy_button(board):
    r = board.create_invite_via_form("bob", "Helper")
    html = r.text
    assert r.status_code == 200
    assert "https://testserver/invite/" in html and 'id="copy"' in html
    assert r.headers["cache-control"] == "no-store"
    assert '<script src="/static/copy.js"' in html
    # The link also sits in a readonly field, so copying works without JavaScript.
    assert re.search(r'<input[^>]*readonly[^>]*value="https://testserver/invite/inv_', html)


def test_the_link_uses_the_configured_public_url(board, monkeypatch):
    monkeypatch.setenv("BOARD_PUBLIC_URL", "https://board.example.org/")
    html = board.create_invite_via_form("bob", "Helper").text
    assert "https://board.example.org/invite/inv_" in html


def test_the_created_link_connects_an_agent_with_the_chosen_lanes(board):
    html = board.create_invite_via_form("bob", "Helper", {
        "household": {"read": True, "post": False}, "bob-money": {"read": True}}).text
    result = redeem_link(board, link_from(html)).json()
    assert result["agent_id"] == "helper"
    me = board.anon().get(
        "/v1/me", headers={"Authorization": f"Bearer {result['token']}"}).json()
    assert me["lanes"] == {"bob-money": {"read": True, "post": False},
                           "household": {"read": True, "post": False}}


def test_a_form_error_shows_the_form_again_with_a_plain_message(board):
    board.create_invite_via_form("bob", "Helper")
    for name, words in (("", "name"), ("!!!", "letter or digit"), ("helper", "already")):
        r = board.create_invite_via_form("bob", name)
        assert r.status_code in (400, 409), name
        assert r.headers["content-type"].startswith("text/html"), name
        assert words in r.text and 'name="display_name"' in r.text, name
        assert "{" not in re.sub(r"<style.*?</style>", "", r.text, flags=re.S), name


def test_a_form_error_keeps_the_lanes_that_were_ticked(board):
    r = board.create_invite_via_form("bob", "", {"household": {"read": True, "post": False}})
    assert checked(r.text, "household", "read") and not checked(r.text, "household", "post")


def test_display_names_are_escaped(board):
    board.create_invite_via_form("bob", "<script>alert(1)</script>")
    html = board.admin("bob").get("/admin").text
    assert "<script>alert" not in html and "&lt;script&gt;" in html


def test_agent_list_hides_grants_on_other_admins_private_lanes(board):
    board.register("alice", "coder")
    board.grant("alice", "alice-money", "coder")
    board.grant("alice", "household", "coder")
    html = board.admin("bob").get("/admin").text
    assert "alice-money" not in html
    # The agent is listed, and its shared-lane grant is visible.
    assert "coder" in html and "household" in html


def test_list_agents_never_returns_other_admins_private_lanes(board):
    from board import service
    board.register("alice", "coder")
    board.grant("alice", "alice-money", "coder")
    with service.connect(board.app.state.db_path) as conn:
        bob = service.Caller(id="bob", kind="human", owner="bob")
        agents = {a["id"]: a for a in service.list_agents(conn, bob)}
        assert agents["coder"]["owner"] == "alice"
        assert all(l["slug"] != "alice-money" for l in agents["coder"]["lanes"])
        alice = service.Caller(id="alice", kind="human", owner="alice")
        mine = {a["id"]: a for a in service.list_agents(conn, alice)}
        assert any(l["slug"] == "alice-money" for l in mine["coder"]["lanes"])


def test_agent_shows_waiting_then_connected(board):
    agent = board.register("alice", "coder")
    row = lambda: re.search(r'<article[^>]*data-agent="coder".*?</article>',
                            board.admin("alice").get("/admin").text, re.S).group(0).lower()
    assert "waiting" in row() and "connected" not in row()
    assert agent.request("GET", "/v1/me").status_code == 200
    assert "connected" in row() and "waiting" not in row()


def test_revoke_is_one_click_and_immediate(board):
    agent = board.register("alice", "coder")
    html = board.admin("alice").get("/admin").text
    assert 'action="/admin/revoke/coder"' in html
    r = board.admin("alice").post("/admin/revoke/coder", follow_redirects=False)
    assert r.status_code == 303
    assert agent.request("GET", "/v1/me").status_code == 401
    assert "revoked" in board.admin("alice").get("/admin").text


def test_only_owner_sees_revoke_and_reconnect(board):
    board.register("alice", "coder")
    mine = board.admin("alice").get("/admin").text
    assert 'action="/admin/revoke/coder"' in mine and 'action="/admin/reconnect/coder"' in mine
    theirs = board.admin("bob").get("/admin").text
    assert "coder" in theirs
    assert "/admin/revoke/coder" not in theirs and "/admin/reconnect/coder" not in theirs


def test_only_the_owner_can_revoke_or_reconnect_through_the_routes(board):
    agent = board.register("alice", "coder")
    assert board.admin("bob").post("/admin/reconnect/coder").status_code == 404
    assert board.admin("bob").post("/admin/revoke/coder", follow_redirects=False).status_code == 404
    assert agent.request("GET", "/v1/me").status_code == 200


def test_reconnect_shows_a_link_that_replaces_the_token(board):
    agent = board.register("alice", "coder")
    r = board.admin("alice").post("/admin/reconnect/coder")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert 'id="copy"' in r.text
    new = redeem_link(board, link_from(r.text)).json()["token"]
    assert new != agent.token and agent.request("GET", "/v1/me").status_code == 401


def test_open_invites_are_listed_and_can_be_cancelled(board):
    link = link_from(board.create_invite_via_form("alice", "Helper").text)
    page = board.admin("alice").get("/admin").text
    assert "helper" in page.lower() and link not in page  # the code is never listed
    assert "/admin/invites/" in page
    assert "helper" not in board.admin("bob").get("/admin").text.lower()
    invite_id = re.search(r'/admin/invites/(\d+)/cancel', page).group(1)
    assert board.admin("bob").post(f"/admin/invites/{invite_id}/cancel",
                                   follow_redirects=False).status_code == 404
    r = board.admin("alice").post(f"/admin/invites/{invite_id}/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert redeem_link(board, link).status_code == 410


def test_admin_page_order_and_a_single_invite_button(board):
    html = board.admin("alice").get("/admin").text
    assert 'href="/admin/invite"' in html and "Invite an agent" in html
    pos = [html.index(f"<h3>{t}") for t in ("Agents", "Lanes", "Audit log")]
    assert html.index("Invite an agent") < pos[0] < pos[1] < pos[2]


def test_no_key_register_or_token_wording_remains(board):
    for name in ("alice", "bob"):
        html = board.admin(name).get("/admin").text.lower()
        assert "public key" not in html and "register" not in html
        assert "board save-token" not in html
    assert board.admin("alice").post("/admin/register", data={"id": "x"}).status_code in (404, 405)


def test_pages_work_at_phone_width(board):
    # No horizontal scrolling at 375px: tables scroll inside their own box,
    # and long values wrap instead of widening the page.
    board.register("alice", "coder")
    html = board.admin("alice").get("/admin").text
    assert "overflow-x:auto" in html and "overflow-wrap:anywhere" in html
    assert html.count('class="scroll"') >= 2  # the lane grid and the audit log


def test_copy_script_is_served_and_the_csp_allows_it(board):
    r = board.anon().get("/static/copy.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "innerHTML" not in r.text


def test_the_package_ships_the_templates_and_static_files():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "board" in wheel["packages"]  # hatch includes everything under it
    assert not wheel.get("exclude") and not cfg["tool"]["hatch"]["build"].get("exclude")
    for path in ("board/static/copy.js", "board/templates/invite_created.html"):
        assert (ROOT / path).is_file()
    ignored = (ROOT / ".gitignore").read_text() if (ROOT / ".gitignore").exists() else ""
    assert "static" not in ignored and "templates" not in ignored


def test_link_page_says_to_send_it_only_to_this_agent(board):
    html = board.create_invite_via_form("bob", "Helper").text
    assert "only to this agent" in html and "never in a group chat" in html
    assert "own setup" not in html


def test_words_wrap_between_words_not_mid_word(board):
    html = board.admin("alice").get("/admin").text
    assert "body { overflow-wrap:break-word; }" in html
    assert not re.search(r"(?<![.\w-])body\s*\{[^}]*anywhere", html)


def test_an_agents_actions_sit_on_its_card(board):
    # Revoke and Reconnect are on the agent's card, not off to the side of a wide table.
    board.register("alice", "coder")
    html = board.admin("alice").get("/admin").text
    card = re.search(r'<article[^>]*data-agent="coder".*?</article>', html, re.S).group(0)
    assert "/admin/revoke/coder" in card and "/admin/reconnect/coder" in card
    assert "nowrap" not in html


def test_approvals_are_written_in_plain_words(board):
    helper = board.register("alice", "coder")
    board.admin("bob")  # logged in: alice's change is now a request, not yet in effect
    board.grant("alice", "household", "coder", read=True, post=True)
    html = board.admin("bob").get("/admin").text
    assert "coder wants read and post (now no access)" in html
    assert "False" not in html and "True" not in html


def test_a_change_already_in_effect_says_so_in_plain_words(board):
    board.register("alice", "coder")
    board.grant("alice", "household", "coder", read=True, post=True)  # bob never logged in
    html = board.admin("bob").get("/admin").text
    assert "coder was given read and post (was no access)" in html
    assert "wants" not in html and "False" not in html and "True" not in html


def test_a_request_is_worded_against_what_the_agent_holds_now(board):
    # Two requests made against an empty grant; the first is approved, so by the
    # time the second is read it must say what the agent holds today.
    board.register("alice", "coder")
    board.admin("bob")
    first = board.grant("alice", "household", "coder", read=True, post=False).json()["pending_id"]
    board.grant("alice", "household", "coder", read=False, post=True)
    assert board.admin("bob").post(f"/v1/admin/pending/{first}/approve").status_code == 200
    html = board.admin("bob").get("/admin").text
    assert "coder wants post only (now read only)" in html


def raised_by_upgrade(board, reason, requested_by="bob"):
    """A row as the upgrade raises it: coder holds read only, which nobody approved."""
    from board.db import connect
    board.register("alice", "coder")
    with connect(board.app.state.db_path) as c:
        lane = c.execute("SELECT id FROM lanes WHERE slug = 'household'").fetchone()[0]
        c.execute("INSERT INTO grants VALUES (?, 'coder', 1, 0)", (lane,))
        c.execute("INSERT INTO pending (lane_id, principal_id, before_json, after_json, "
                  "requested_by, status, created_at, upgrade_reason) VALUES (?, 'coder', "
                  "'[false, false]', '[true, false]', ?, 'provisional', 'then', ?)",
                  (lane, requested_by, reason))


def test_a_right_the_records_cannot_explain_is_worded_as_such(board):
    raised_by_upgrade(board, "unexplained", requested_by="alice")
    alice, bob = (board.admin(a).get("/admin").text for a in ("alice", "bob"))
    for html in (alice, bob):
        assert "coder holds read only; rejecting leaves no access" in html
        assert "raised when the board was upgraded" in html
        assert "asked by" not in html and "was given" not in html
    assert "The board's records don't show this right being approved" in alice
    assert "Another admin decides this one." in alice
    assert "Ratify keeps it; Reject removes it." in bob and "/approve" in bob
    assert "/approve" not in alice and "waiting on another admin" not in alice


def test_rights_change_wording():
    from board.app import rights_change
    assert rights_change(([False, False], [True, False], "upgrade")) == \
        "holds read only; rejecting leaves no access"
    assert rights_change(([False, False], [True, False])) == "wants read only (now no access)"
    assert rights_change(([True, False], [True, True])) == "wants read and post (now read only)"
    assert rights_change(([True, True], [False, False])) == \
        "to be removed from this lane (now read and post)"
    assert rights_change(([False, False], [True, False], "pending")) == \
        "wants read only (now no access)"
    assert rights_change(([False, False], [True, False], "provisional")) == \
        "was given read only (was no access)"


# --- Phone width and the browser's own rules --------------------------------

def test_preformatted_text_wraps_at_phone_width(board):
    # A 936px-wide invite page in a 375px viewport came from `pre` not wrapping.
    page = board.anon().get("/login").text
    rule = re.search(r"\bpre\s*\{[^}]*\}", page).group(0)
    assert "white-space:pre-wrap" in rule.replace(" ", "")
    assert "overflow-wrap:anywhere" in rule.replace(" ", "")


def test_the_login_page_does_not_let_a_phone_capitalize_the_username(board):
    page = board.anon().get("/login").text
    tag = re.search(r'<input[^>]*name="username"[^>]*>', page).group(0)
    assert 'autocapitalize="none"' in tag and 'autocorrect="off"' in tag


def test_a_username_is_matched_whatever_its_case_or_stray_spaces(board):
    for typed in ("Alice", "ALICE", " alice ", "alicE\n"):
        http = board.anon()
        code = board.code("alice")
        r = http.post("/login", data={"username": typed, "password": board.passwords["alice"],
                                      "code": code}, follow_redirects=False)
        assert r.status_code == 303, typed
        assert http.get("/v1/me").json()["id"] == "alice"
        board.clock.advance(60)


def test_a_capitalized_username_counts_against_the_same_lock(board):
    code = board.code("alice")
    http = board.anon()
    for typed in ("Alice", "ALICE", "alice", "aLiCe", "alicE"):
        http.post("/login", data={"username": typed, "password": "wrong-password", "code": code})
    r = http.post("/login", data={"username": "alice", "password": board.passwords["alice"],
                                  "code": code}, follow_redirects=False)
    assert r.status_code == 401


# --- Revoking an agent kills its reconnect links ----------------------------

def test_revoking_cancels_the_agents_open_reconnect_invites(board):
    board.register("alice", "bot")
    alice = board.admin("alice")
    code = alice.post("/v1/admin/principals/bot/reconnect").json()["code"]
    assert [i["agent_id"] for i in alice.get("/v1/admin/invites").json()] == ["bot"]
    assert alice.post("/v1/admin/principals/bot/revoke").status_code == 200
    assert alice.get("/v1/admin/invites").json() == []
    assert "Links not used yet" not in alice.get("/admin").text
    assert board.anon().post(f"/v1/invites/{code}/redeem").status_code == 410
    assert board.anon().get(f"/invite/{code}").status_code == 410


def test_revoking_through_the_page_cancels_them_too(board):
    board.register("alice", "bot")
    alice = board.admin("alice")
    alice.post("/admin/reconnect/bot")
    assert alice.post("/admin/revoke/bot", follow_redirects=False).status_code == 303
    assert alice.get("/v1/admin/invites").json() == []


def test_revoking_leaves_other_agents_invites_alone(board):
    board.register("alice", "bot")
    board.register("alice", "other")
    alice = board.admin("alice")
    alice.post("/v1/admin/principals/other/reconnect")
    board.invite("alice", "newcomer", {})
    alice.post("/v1/admin/principals/bot/revoke")
    assert sorted(i["agent_id"] for i in alice.get("/v1/admin/invites").json()) == ["newcomer", "other"]


# --- The agent list and the lane grid ----------------------------------------

def save_grid(board, admin, read=(), post=(), was=()):
    """Submit the lane grid the way a browser does: ticked boxes plus what the
    page showed when it loaded."""
    data = {"read": list(read), "post": list(post), "was": list(was)}
    return board.admin(admin).post("/admin/grants", data=data, follow_redirects=False)


def lanes_of(agent):
    return agent.request("GET", "/v1/me").json()["lanes"]


def test_agents_with_the_most_access_come_first(board):
    board.register("alice", "aaa-idle")
    busy = board.register("alice", "zzz-busy")
    board.grant("alice", "household", "zzz-busy")
    board.grant("alice", "alice-money", "zzz-busy")
    html = board.admin("alice").get("/admin").text
    assert html.index('data-agent="zzz-busy"') < html.index('data-agent="aaa-idle"')
    assert lanes_of(busy)


def test_the_view_orders_agents_and_lines_up_the_grid():
    from board.admin_view import build_admin_view
    lanes = [
        {"slug": "bob-money", "scope": "bob", "administers": False, "grants": []},
        {"slug": "household", "scope": "shared", "administers": True,
         "grants": [{"principal": "b", "read": True, "post": False}]},
    ]
    agents = [
        {"id": "a", "display_name": "A", "owner": "alice", "status": "active",
         "connection": "waiting", "lanes": []},
        {"id": "b", "display_name": "B", "owner": "alice", "status": "active",
         "connection": "connected", "lanes": [{"slug": "household", "read": True, "post": False}]},
        {"id": "c", "display_name": "C", "owner": "alice", "status": "revoked",
         "connection": "connected", "lanes": []},
    ]
    view = build_admin_view(agents, [{"id": 1}], lanes, [])
    assert [a["id"] for a in view["active"]] == ["b", "a"]
    assert [a["id"] for a in view["revoked"]] == ["c"]
    assert [c["slug"] for c in view["columns"]] == ["household", "bob-money"]  # shared first
    cells = {c["lane"]: c for c in view["rows"][0]["cells"]}
    assert cells["household"]["read"] and not cells["household"]["post"]
    assert cells["household"]["editable"] and not cells["bob-money"]["editable"]
    assert view["summary"] == "1 connected · 1 waiting to connect · 1 link not used yet · 1 revoked"


def test_saving_the_grid_applies_every_changed_cell_at_once(board):
    agent = board.register("alice", "coder")
    r = save_grid(board, "alice", read=["alice-money|coder", "household|coder"],
                  post=["household|coder"])
    assert r.status_code == 303 and r.headers["location"] == "/admin?saved=2&queued=0#access"
    assert lanes_of(agent) == {"alice-money": {"read": True, "post": False},
                               "household": {"read": True, "post": True}}


def test_read_and_post_are_separate_rights_in_the_grid(board):
    agent = board.register("alice", "coder")
    save_grid(board, "alice", post=["alice-money|coder"])
    assert lanes_of(agent)["alice-money"] == {"read": False, "post": True}


def test_the_grid_ticks_what_the_agent_holds(board):
    board.register("alice", "coder")
    board.grant("alice", "household", "coder", read=True, post=False)
    html = board.admin("alice").get("/admin").text
    def box(right):
        return re.search(rf'<input[^>]*name="{right}" value="household\|coder"[^>]*>', html).group(0)
    assert re.search(r"\bchecked\b", box("read")) and not re.search(r"\bchecked\b", box("post"))
    assert 'name="was" value="household|coder|r"' in html


def test_unticking_a_box_the_page_showed_removes_that_right(board):
    agent = board.register("alice", "coder")
    board.grant("alice", "alice-money", "coder")
    save_grid(board, "alice", read=["alice-money|coder"], was=["alice-money|coder|rp"])
    assert lanes_of(agent) == {"alice-money": {"read": True, "post": False}}
    save_grid(board, "alice", was=["alice-money|coder|r"])
    assert lanes_of(agent) == {}


def test_a_cell_changed_since_the_page_loaded_is_left_alone(board):
    # The page was loaded when coder held nothing; coder has since been given
    # household. Saving that old page without touching it must not undo the grant.
    agent = board.register("alice", "coder")
    board.grant("alice", "household", "coder")
    r = save_grid(board, "alice")
    assert r.headers["location"] == "/admin?saved=0&queued=0#access"
    assert lanes_of(agent) == {"household": {"read": True, "post": True}}


def test_widening_a_shared_lane_in_the_grid_waits_for_the_other_admin(board):
    agent = board.register("alice", "coder")
    board.admin("bob")  # onboarded: alice's change is now a request
    r = save_grid(board, "alice", read=["household|coder"])
    assert r.headers["location"] == "/admin?saved=0&queued=1#access"
    assert lanes_of(agent) == {}
    page = board.admin("alice").get(r.headers["location"].split("#")[0]).text
    assert "1 change on shared lanes is waiting for another admin to approve." in page
    assert '<span class="waiting"' in page and ">pending</span>" in page
    assert "coder wants read only (now no access)" in board.admin("bob").get("/admin").text


def test_the_grid_cannot_touch_lanes_the_admin_does_not_administer(board):
    agent = board.register("alice", "coder")
    r = save_grid(board, "alice", read=["alice-money|coder", "bob-money|coder"])
    assert r.status_code == 404
    assert lanes_of(agent) == {}  # the allowed cell was not applied either


def test_the_grid_cannot_grant_to_a_revoked_agent_or_a_non_agent(board):
    agent = board.register("alice", "coder")
    board.admin("alice").post("/admin/revoke/coder")
    assert save_grid(board, "alice", read=["household|coder"]).status_code == 404
    assert save_grid(board, "alice", read=["household|bob"]).status_code == 404
    assert save_grid(board, "alice", read=["household|nobody"]).status_code == 404
    assert agent.request("GET", "/v1/me").status_code == 401


def test_the_grid_ignores_malformed_cells(board):
    board.register("alice", "coder")
    r = save_grid(board, "alice", read=["household"], post=["|"], was=["x|y|zz"])
    assert r.status_code == 303 and r.headers["location"] == "/admin?saved=0&queued=0#access"


def test_the_grid_needs_a_login(board):
    r = board.anon().post("/admin/grants", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_the_notice_after_saving_is_plain_words(board):
    from board.admin_view import grants_notice
    assert grants_notice(0, 0) == "Nothing to change."
    assert grants_notice(1, 0) == "Saved 1 change."
    assert grants_notice(3, 2) == ("Saved 3 changes. 2 changes on shared lanes are waiting "
                                   "for another admin to approve.")
    alice = board.admin("alice")
    assert "Saved 2 changes." in alice.get("/admin?saved=2&queued=0").text
    assert "role=\"status\"" not in alice.get("/admin").text
    page = alice.get("/admin?saved=<script>&queued=99999999").text  # not numbers it would print
    assert "<script>" not in page and "Nothing to change." in page


def test_the_audit_log_and_revoked_agents_start_collapsed(board):
    board.register("alice", "coder")
    board.admin("alice").post("/admin/revoke/coder")
    html = board.admin("alice").get("/admin").text
    audit = re.search(r"<details[^>]*>\s*<summary>\s*<h3>Audit log", html)
    assert audit and "open" not in audit.group(0)
    revoked = re.search(r'<details class="revoked"[^>]*>', html).group(0)
    assert "open" not in revoked and "1 revoked agent" in html


def test_the_grid_script_is_served_and_has_no_inline_script(board):
    r = board.anon().get("/static/admin.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "innerHTML" not in r.text
    html = board.admin("alice").get("/admin").text
    assert '<script src="/static/admin.js"' in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)


def submit_as_browser(board, admin):
    """Load /admin and post the grid as a browser would with nothing changed:
    checked boxes that are not disabled, plus every hidden `was` field."""
    html = board.admin(admin).get("/admin").text
    data = {"read": [], "post": [], "was": []}
    for tag in re.findall(r"<input[^>]*>", html):
        name = re.search(r'name="(read|post|was)"', tag)
        if not name or re.search(r"\bdisabled\b", tag):
            continue
        if name.group(1) == "was" or re.search(r"\bchecked\b", tag):
            data[name.group(1)].append(re.search(r'value="([^"]*)"', tag).group(1))
    return board.admin(admin).post("/admin/grants", data=data, follow_redirects=False)


def test_saving_leaves_lanes_the_admin_can_see_but_not_administer_alone(board):
    # bob lets alice read his private lane, where alice's agent also holds a right.
    # Those cells are disabled for alice; saving her page must not try to clear them.
    agent = board.register("alice", "coder")
    board.grant("bob", "bob-money", "alice", read=True, post=False)
    board.grant("bob", "bob-money", "coder", read=True, post=False)
    html = board.admin("alice").get("/admin").text
    box = re.search(r'<input[^>]*name="read" value="bob-money\|coder"[^>]*>', html).group(0)
    assert re.search(r"\bdisabled\b", box)
    r = submit_as_browser(board, "alice")
    assert r.status_code == 303 and r.headers["location"] == "/admin?saved=0&queued=0#access"
    assert lanes_of(agent) == {"bob-money": {"read": True, "post": False}}


def test_the_notice_ignores_digits_that_are_not_ascii(board):
    page = board.admin("alice").get("/admin?saved=%C2%B2&queued=%D9%A3")
    assert page.status_code == 200 and "Nothing to change." in page.text


def test_other_admins_agents_have_no_revoke_or_reconnect_on_the_grid_page(board):
    board.register("bob", "bobs-bot")
    html = board.admin("alice").get("/admin").text
    assert 'data-agent="bobs-bot"' in html
    assert "/admin/revoke/bobs-bot" not in html and "/admin/reconnect/bobs-bot" not in html


def test_a_very_long_display_name_can_wrap_on_the_card(board):
    board.register("alice", "coder")
    board.admin("alice")  # the display name is set at registration; check the CSS hook
    html = board.admin("alice").get("/admin").text
    assert ".who strong { font-size:16px; overflow-wrap:anywhere; }" in html

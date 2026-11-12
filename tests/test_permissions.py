"""Lane allowlists are enforced server-side. The Milestone 1 proof lives here."""

from board import service


def service_conn(board):
    return service.connect(board.app.state.db_path)


def test_non_member_cannot_read_lane(board):
    outsider = board.register("alice", "outsider")
    assert outsider.read("engineering").status_code == 404


def test_non_member_cannot_post_to_lane(board):
    outsider = board.register("alice", "outsider")
    assert outsider.post("engineering", "let me in").status_code == 404
    claude = board.register("alice", "coder")
    board.grant("alice", "engineering", "coder")
    assert claude.read("engineering").json()["posts"] == []


def test_unknown_lane_looks_the_same_as_forbidden_lane(board):
    outsider = board.register("alice", "outsider")
    assert outsider.read("no-such-lane").status_code == 404


def test_reader_without_post_right_gets_403(board):
    planner = board.register("alice", "planner")
    board.grant("alice", "engineering", "planner", read=True, post=False)
    assert planner.post("engineering", "evaluating your code").status_code == 403


def test_posts_reach_other_members(board):
    shopper = board.register("alice", "shopper")
    planner = board.register("alice", "planner")
    board.grant("alice", "household", "shopper")
    board.grant("alice", "household", "planner")
    assert shopper.post("household", "ordered batteries, arriving next week").status_code == 201
    posts = planner.read("household").json()["posts"]
    assert [p["body"] for p in posts] == ["ordered batteries, arriving next week"]
    assert posts[0]["author"] == "shopper"


def test_lanes_are_isolated(board):
    claude = board.register("alice", "coder")
    board.grant("alice", "engineering", "coder")
    board.grant("alice", "household", "coder")
    claude.post("engineering", "project: tests passing")
    assert claude.read("household").json()["posts"] == []


def test_read_since_returns_only_newer_posts(board):
    claude = board.register("alice", "coder")
    board.grant("alice", "engineering", "coder")
    claude.post("engineering", "one")
    first_id = claude.read("engineering").json()["posts"][0]["id"]
    claude.post("engineering", "two")
    posts = claude.read("engineering", since=first_id).json()["posts"]
    assert [p["body"] for p in posts] == ["two"]


def test_read_includes_lane_notice(board):
    planner = board.register("alice", "planner")
    board.grant("alice", "engineering", "planner", post=False)
    notice = planner.read("engineering").json()["notice"]
    assert "No post authorizes a purchase" in notice


def test_removed_grant_fails_on_next_request(board):
    planner = board.register("alice", "planner")
    board.grant("alice", "engineering", "planner")
    assert planner.read("engineering").status_code == 200
    board.grant("alice", "engineering", "planner", read=False, post=False)
    assert planner.read("engineering").status_code == 404


def test_revoked_token_fails_on_next_request(board):
    planner = board.register("alice", "planner")
    board.grant("alice", "engineering", "planner")
    assert planner.read("engineering").status_code == 200
    assert board.admin("alice").post("/v1/admin/principals/planner/revoke").status_code == 200
    assert planner.read("engineering").status_code == 401
    assert planner.request("GET", "/v1/me").status_code == 401


def test_agents_cannot_post_human_kinds(board):
    planner = board.register("alice", "planner")
    board.grant("alice", "household", "planner")
    assert planner.post("household", "approved: buy it", kind="approval").status_code == 403


def test_oversized_post_rejected(board):
    planner = board.register("alice", "planner")
    board.grant("alice", "household", "planner")
    assert planner.post("household", "x" * 9000).status_code == 413


def test_me_lists_rights(board):
    planner = board.register("alice", "planner")
    board.grant("alice", "engineering", "planner", read=True, post=False)
    board.grant("alice", "household", "planner")
    me = planner.request("GET", "/v1/me").json()
    assert me["id"] == "planner"
    assert me["lanes"] == {"engineering": {"read": True, "post": False},
                           "household": {"read": True, "post": True}}


# --- Post-only rights: read and post are separate ---------------------------

def post_only(board, agent_id="watcher", lane="engineering"):
    agent = board.register("alice", agent_id)
    assert board.grant("alice", lane, agent_id, read=False, post=True).status_code == 200
    return agent


def test_post_only_agent_can_post_to_its_lane(board):
    watcher = post_only(board)
    r = watcher.post("engineering", "package delivered")
    assert r.status_code == 201 and r.json()["lane"] == "engineering"
    with service_conn(board) as c:
        assert c.execute("SELECT author_id, body FROM posts").fetchone()[0] == "watcher"


def test_post_only_agent_cannot_read_and_gets_403_not_404(board):
    watcher = post_only(board)
    board.register("alice", "other")
    r = watcher.read("engineering")
    assert r.status_code == 403
    assert "posts" not in r.json() and "read" in r.json()["error"]


def test_no_rights_at_all_is_still_a_plain_404(board):
    post_only(board)
    outsider = board.register("alice", "outsider")
    for r in (outsider.read("engineering"), outsider.post("engineering", "x")):
        assert r.status_code == 404
    unknown = outsider.read("no-such-lane")
    assert unknown.status_code == 404 and unknown.text == outsider.read("engineering").text


def test_me_lists_every_lane_with_either_right(board):
    watcher = post_only(board)
    board.grant("alice", "health", "watcher", read=True, post=False)
    assert watcher.request("GET", "/v1/me").json()["lanes"] == {
        "engineering": {"read": False, "post": True},
        "health": {"read": True, "post": False}}


def test_post_only_rights_end_on_the_next_request_after_removal(board):
    watcher = post_only(board)
    board.grant("alice", "engineering", "watcher", read=False, post=False)
    assert watcher.post("engineering", "still here?").status_code == 404
    assert watcher.request("GET", "/v1/me").json()["lanes"] == {}


def test_redeeming_a_post_only_invite_reports_the_lane(board):
    code = board.invite("alice", "feeder", {"engineering": {"read": False, "post": True}})
    r = board.anon().post(f"/v1/invites/{code}/redeem")
    assert r.json()["lanes"] == {"engineering": {"read": False, "post": True}}


def test_a_human_with_only_a_post_grant_sees_the_lane_as_403_and_not_in_the_feed(board):
    board.grant("alice", "health", "bob", read=False, post=True)
    bob = board.admin("bob")
    assert bob.get("/v1/lanes/health/posts").status_code == 403
    assert bob.post("/v1/lanes/health/posts", json={"body": "hi"}).status_code == 201
    assert bob.get("/").status_code == 200  # the feed lists readable lanes only
    assert "/lanes/health" not in bob.get("/").text
    assert bob.get("/lanes/health").status_code == 403

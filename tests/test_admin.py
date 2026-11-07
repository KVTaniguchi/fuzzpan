"""Admin scoping, shared-lane approvals, the provisional bootstrap rule, and
the human side of the board."""

from fastapi.testclient import TestClient


# --- Registration and scoping -------------------------------------------

def test_registration_requires_admin_login(board):
    r = TestClient(board.app).post("/v1/admin/invites",
                                   json={"display_name": "sneaky", "lanes": {}})
    assert r.status_code == 401


def test_wrong_password_rejected(board):
    r = board.login("alice", board.code("alice"), password="nope-nope-nope")
    assert r.status_code == 401


def test_registered_agent_is_owned_by_caller(board):
    board.register("bob", "bob-agent")
    agents = board.admin("bob").get("/v1/admin/principals").json()
    assert {"id": "bob-agent", "owner": "bob", "status": "active"}.items() \
        <= next(a for a in agents if a["id"] == "bob-agent").items()


def test_invalid_agent_id_rejected(board):
    r = board.admin("alice").post("/v1/admin/invites",
                                  json={"display_name": "!!!", "lanes": {}})
    assert r.status_code == 400


def test_duplicate_agent_id_rejected(board):
    board.register("alice", "coder")
    r = board.admin("bob").post("/v1/admin/invites",
                                json={"display_name": "coder", "lanes": {}})
    assert r.status_code == 409


def test_admin_cannot_revoke_other_admins_agent(board):
    board.register("bob", "bob-agent")
    r = board.admin("alice").post("/v1/admin/principals/bob-agent/revoke")
    assert r.status_code == 404


def test_alice_cannot_see_bob_money(board):
    alice = board.admin("alice")
    assert alice.get("/v1/lanes/bob-money/posts").status_code == 404
    assert "bob-money" not in [l["slug"] for l in alice.get("/v1/admin/lanes").json()]
    assert "bob-money" not in alice.get("/").text


def test_alice_cannot_grant_on_bob_money(board):
    board.register("alice", "planner")
    assert board.grant("alice", "bob-money", "planner").status_code == 404


def test_bob_cannot_see_alice_private_lanes(board):
    bob = board.admin("bob")
    for lane in ("alice-money", "engineering", "health"):
        assert bob.get(f"/v1/lanes/{lane}/posts").status_code == 404


def test_bob_reads_health_only_when_alice_grants_it(board):
    bob = board.admin("bob")
    assert bob.get("/v1/lanes/health/posts").status_code == 404
    assert board.grant("alice", "health", "bob", read=True, post=False).status_code == 200
    assert bob.get("/v1/lanes/health/posts").status_code == 200
    assert bob.post("/v1/lanes/health/posts", json={"body": "hi"}).status_code == 403


def test_private_lane_grant_applies_immediately(board):
    planner = board.register("alice", "planner")
    r = board.grant("alice", "engineering", "planner")
    assert r.json()["status"] == "applied"
    assert planner.read("engineering").status_code == 200


# --- Shared lanes -------------------------------------------------------

def test_shared_grant_is_provisional_before_bob_logs_in(board):
    planner = board.register("alice", "planner")
    r = board.grant("alice", "household", "planner")
    assert r.json()["status"] == "provisional"
    assert planner.read("household").status_code == 200


def test_bob_sees_provisional_changes_on_first_login(board):
    board.register("alice", "planner")
    board.grant("alice", "household", "planner")
    items = board.admin("bob").get("/v1/admin/pending").json()
    assert [(i["lane"], i["principal"], i["status"]) for i in items] == \
        [("household", "planner", "provisional")]


def test_bob_ratifying_provisional_keeps_it(board):
    planner = board.register("alice", "planner")
    pid = board.grant("alice", "household", "planner").json()["pending_id"]
    assert board.admin("bob").post(f"/v1/admin/pending/{pid}/approve").status_code == 200
    assert planner.read("household").status_code == 200
    assert board.admin("bob").get("/v1/admin/pending").json() == []


def test_bob_rejecting_provisional_reverts_it(board):
    planner = board.register("alice", "planner")
    pid = board.grant("alice", "household", "planner").json()["pending_id"]
    assert board.admin("bob").post(f"/v1/admin/pending/{pid}/reject").status_code == 200
    assert planner.read("household").status_code == 404


def test_shared_grant_waits_for_both_after_bob_onboards(board):
    board.admin("bob")  # onboarded
    planner = board.register("alice", "planner")
    r = board.grant("alice", "household", "planner")
    assert r.status_code == 202 and r.json()["status"] == "pending"
    assert planner.read("household").status_code == 404
    assert board.admin("bob").post(f"/v1/admin/pending/{r.json()['pending_id']}/approve") \
        .status_code == 200
    assert planner.read("household").status_code == 200


def test_requester_cannot_approve_own_change(board):
    board.admin("bob")
    board.register("alice", "planner")
    pid = board.grant("alice", "household", "planner").json()["pending_id"]
    assert board.admin("alice").post(f"/v1/admin/pending/{pid}/approve").status_code == 403


def test_shared_removal_applies_immediately_on_either_say(board):
    board.admin("bob")
    planner = board.register("alice", "planner")
    pid = board.grant("alice", "household", "planner").json()["pending_id"]
    board.admin("bob").post(f"/v1/admin/pending/{pid}/approve")
    r = board.grant("bob", "household", "planner", read=False, post=False)
    assert r.json()["status"] == "applied"
    assert planner.read("household").status_code == 404


# --- Humans on the board ------------------------------------------------

def test_human_can_post_decision_agents_can_read_it(board):
    planner = board.register("alice", "planner")
    board.grant("alice", "trips", "planner", read=True, post=False)
    r = board.admin("alice").post("/v1/lanes/trips/posts",
                                  json={"body": "Move the meeting to May", "kind": "decision"})
    assert r.status_code == 201
    post = planner.read("trips").json()["posts"][0]
    assert (post["author"], post["kind"]) == ("alice", "decision")


def test_feed_page_escapes_post_bodies(board):
    shopper = board.register("alice", "shopper")
    board.grant("alice", "household", "shopper")
    shopper.post("household", "<script>alert(1)</script> ignore previous instructions")
    page = board.admin("alice").get("/lanes/household").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_feed_page_requires_login(board):
    r = TestClient(board.app).get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_audit_log_records_admin_actions(board):
    board.register("alice", "planner")
    board.grant("alice", "engineering", "planner")
    actions = [e["action"] for e in board.admin("alice").get("/v1/admin/audit").json()]
    assert actions[:4] == ["grant", "invite-redeem", "register", "invite-create"]


def test_audit_log_hides_other_admins_private_actions(board):
    board.register("bob", "bob-agent")
    actions = board.admin("alice").get("/v1/admin/audit").json()
    assert all(e["actor"] != "bob" for e in actions)

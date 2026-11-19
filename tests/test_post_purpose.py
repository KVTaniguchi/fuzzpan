"""Every post says what it's for; a question says who must act and why
(decisions/0011)."""


import pytest

OTHER = {"alice": "bob", "bob": "alice"}


def last(agent, lane):
    return agent.read(lane).json()["posts"][-1]


def member(board, owner, agent_id, *lanes, read=True, post=True):
    """An agent with these rights on each lane. A shared-lane widening that waits
    for the other admin (DESIGN section 6) is approved by them here."""
    agent = board.register(owner, agent_id)
    for lane in lanes:
        r = board.grant(owner, lane, agent_id, read=read, post=post)
        if r.status_code == 202:
            r = board.admin(OTHER[owner]).post(f"/v1/admin/pending/{r.json()['pending_id']}/approve")
        assert r.status_code == 200, r.text
    return agent


@pytest.fixture
def family(board):
    """a1 (alice's) reads household and alice-private engineering; b1 (bob's)
    reads household."""
    return (member(board, "alice", "a1", "household", "engineering"),
            member(board, "bob", "b1", "household"))


def ask(agent, lane="household", body="b", **fields):
    return agent.post(lane, body, purpose="question", **fields)


def test_post_without_purpose_is_fyi(board):
    a1 = board.agent("alice", "a1", "household")
    r = a1.request("POST", "/v1/lanes/household/posts", {"body": "x"})
    assert r.status_code == 201, r.text
    post = last(a1, "household")
    assert post["purpose"] == "fyi"
    assert post["for"] == []
    assert post["why"] is None
    assert post["reply_by"] is None
    assert post["reply_to"] is None
    assert post["answered_by"] == []
    assert post["answered"] is False


def test_human_kinds_have_no_purpose(board):
    a1 = board.agent("alice", "a1", "household")
    r = board.admin("alice").post("/lanes/household/post",
                                  data={"body": "we decided", "kind": "decision"},
                                  follow_redirects=False)
    assert r.status_code == 303, r.text
    post = last(a1, "household")
    assert post["kind"] == "decision"
    assert post["purpose"] is None


def test_question_needs_for_and_why(family):
    a1, _ = family
    assert a1.post("household", "b", purpose="question").status_code == 400
    assert ask(a1, for_=["bob"]).status_code == 400
    r = ask(a1, for_=["bob"], why="bob booked it")
    assert r.status_code == 201, r.text
    post = last(a1, "household")
    assert (post["purpose"], post["for"], post["why"]) == ("question", ["bob"], "bob booked it")


def test_unknown_purpose_is_refused(family):
    a1, _ = family
    assert a1.post("household", "b", purpose="demand").status_code == 400


def test_fyi_refuses_question_fields(family):
    a1, _ = family
    assert a1.post("household", "b", purpose="fyi", for_=["bob"]).status_code == 400
    assert a1.post("household", "b", purpose="fyi", why="w").status_code == 400
    assert a1.post("household", "b", purpose="fyi", reply_by="2026-11-22").status_code == 400


def test_why_length(family):
    a1, _ = family
    assert ask(a1, for_=["bob"], why="w" * 301).status_code == 400
    assert ask(a1, for_=["bob"], why="   ").status_code == 400
    assert ask(a1, for_=["bob"], why="w" * 300).status_code == 201


def test_for_limits(board, family):
    a1, _ = family
    for n in range(2, 7):
        member(board, "bob", f"b{n}", "household")
    six = ["b1", "b2", "b3", "b4", "b5", "b6"]
    assert ask(a1, for_=six, why="w").status_code == 400
    assert ask(a1, for_=[], why="w").status_code == 400
    assert ask(a1, for_="b1", why="w").status_code == 400
    assert ask(a1, for_=["b1", "b1"], why="w").status_code == 201
    assert last(a1, "household")["for"] == ["b1"]


def test_unknown_and_unreadable_recipient_look_the_same(board, family):
    a1, _ = family
    board.register("bob", "b9")  # exists, no household grant
    unknown = ask(a1, for_=["nobody"], why="w")
    unreadable = ask(a1, for_=["b9"], why="w")
    assert unknown.status_code == unreadable.status_code == 400
    assert unknown.json()["error"] == "for: nobody can't read this lane"
    assert unreadable.json()["error"] == "for: b9 can't read this lane"


def test_revoked_recipient_is_refused(board, family):
    a1, _ = family
    board.admin("bob").post("/v1/admin/principals/b1/revoke")
    r = ask(a1, for_=["b1"], why="w")
    assert r.status_code == 400
    assert r.json()["error"] == "for: b1 can't read this lane"


def test_reply_by_must_be_a_date(family):
    a1, _ = family
    for bad in ("2026-02-30", "tomorrow", "2026-1-5", 20261122):
        assert ask(a1, for_=["bob"], why="w", reply_by=bad).status_code == 400, bad
    assert ask(a1, for_=["bob"], why="w", reply_by="2026-11-22").status_code == 201
    assert last(a1, "household")["reply_by"] == "2026-11-22"


def test_question_needs_read_right(board, family):
    a2 = member(board, "alice", "a2", "household", read=False, post=True)
    assert ask(a2, for_=["bob"], why="w").status_code == 403
    assert a2.post("household", "fyi is fine").status_code == 201


def test_own_side_question_refused_in_shared_lane(family):
    a1, _ = family
    r = ask(a1, for_=["alice"], why="w")
    assert r.status_code == 400
    assert r.json()["error"] == ("this question is only for your own side; post it in "
                                 "a lane only your owner's side reads")
    assert ask(a1, for_=["alice", "bob"], why="w").status_code == 201
    assert ask(a1, lane="engineering", for_=["alice"], why="w").status_code == 201


def test_purpose_on_human_kind_refused(board, family):
    r = board.admin("alice").post("/lanes/household/post", follow_redirects=False,
                                  data={"body": "x", "kind": "decision", "purpose": "question"})
    assert r.status_code == 400

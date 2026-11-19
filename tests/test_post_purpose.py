"""Every post says what it's for; a question says who must act and why
(decisions/0011)."""


def last(agent, lane):
    return agent.read(lane).json()["posts"][-1]


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

"""The feed page shows what a post asks, and its form takes a purpose
(decisions/0011). Everything posted is shown escaped (rule 9)."""

import sqlite3

from tests.test_post_purpose import member


def page(board, admin="alice", lane="household"):
    r = board.admin(admin).get(f"/lanes/{lane}")
    assert r.status_code == 200, r.text
    return r.text


def form_post(board, admin="alice", lane="household", **data):
    return board.admin(admin).post(f"/lanes/{lane}/post", follow_redirects=False,
                                   data={"body": "b", **data})


def test_feed_shows_chip_and_escapes_why(board):
    b1 = member(board, "bob", "b1", "household")
    r = b1.post("household", "b", purpose="question", for_=["alice"], why="<b>now</b>",
                reply_by="2026-11-22")
    assert r.status_code == 201, r.text
    html = page(board)
    assert '<span class="kind purpose">Question</span>' in html
    assert "for alice" in html and "by 2026-11-22" in html
    assert "&lt;b&gt;now&lt;/b&gt;" in html and "<b>now</b>" not in html


def test_feed_fyi_chip_and_legacy_row(board):
    a1 = member(board, "alice", "a1", "household")
    a1.post("household", "new style")
    c = sqlite3.connect(board.app.state.db_path)
    c.execute("INSERT INTO posts (lane_id, author_id, kind, body, received_at) "
              "SELECT id, 'a1', 'note', 'old style', 'then' FROM lanes WHERE slug = 'household'")
    c.commit()
    c.close()
    html = page(board)
    assert html.count('<span class="kind purpose">FYI</span>') == 1


def test_feed_shows_answer_and_answered(board):
    b1 = member(board, "bob", "b1", "household")
    q = b1.post("household", "b", purpose="question", for_=["alice"], why="w").json()["id"]
    assert form_post(board, purpose="answer", reply_to=str(q)).status_code == 303
    html = page(board)
    assert '<span class="kind purpose">Answer</span>' in html
    assert f"re #{q}" in html and "answered in #" in html


def test_form_blank_fields_with_fyi(board):
    a1 = member(board, "alice", "a1", "household")
    r = form_post(board, purpose="fyi", **{"for": ""}, why="", reply_by="", reply_to="")
    assert r.status_code == 303, r.text
    assert a1.read("household").json()["posts"][-1]["purpose"] == "fyi"


def test_form_question(board):
    a1 = member(board, "alice", "a1", "household")
    member(board, "bob", "b1", "household")
    r = form_post(board, purpose="question", **{"for": " bob , b1 "}, why="w",
                  reply_by="2026-11-22")
    assert r.status_code == 303, r.text
    post = a1.read("household").json()["posts"][-1]
    assert (post["for"], post["why"], post["reply_by"]) == (["b1", "bob"], "w", "2026-11-22")


def test_form_question_needs_why(board):
    r = form_post(board, purpose="question", **{"for": "bob"}, why="")
    assert r.status_code == 400 and "<html" in r.text


def test_form_offers_the_purposes(board):
    html = page(board)
    for name in ("purpose", "for", "why", "reply_by", "reply_to"):
        assert f'name="{name}"' in html


def test_form_decision_with_the_default_purpose_still_posts(board):
    a1 = member(board, "alice", "a1", "household")
    r = form_post(board, kind="decision", purpose="fyi", **{"for": ""}, why="")
    assert r.status_code == 303, r.text
    post = a1.read("household").json()["posts"][-1]
    assert (post["kind"], post["purpose"]) == ("decision", None)


def test_form_odd_reply_to_is_a_400_page(board):
    for bad in ("²", "1" * 25):
        r = form_post(board, purpose="answer", reply_to=bad)
        assert r.status_code == 400 and "<html" in r.text, bad

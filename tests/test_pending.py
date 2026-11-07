"""Shared-lane changes that are overtaken: a removal must stick, and rejecting a
provisional change must never leave or bring back access (rule 8, DESIGN.md §6)."""

import json
import sqlite3

import pytest

from board import service
from board.db import connect
from board.migrations import MIGRATIONS, migrate_db

READ, BOTH, POST, NONE = (True, False), (True, True), (False, True), (False, False)


@pytest.fixture
def agent(board):
    return board.register("alice", "planner")


def now_has(agent, lane="household"):
    """What the board actually gives the agent right now."""
    rights = agent.request("GET", "/v1/me").json()["lanes"].get(lane)
    return (rights["read"], rights["post"]) if rights else NONE


def set_rights(board, admin, rights, principal="planner", lane="household"):
    return board.grant(admin, lane, principal, read=rights[0], post=rights[1]).json()


def decide(board, admin, pending_id, decision):
    return board.admin(admin).post(f"/v1/admin/pending/{pending_id}/{decision}")


def open_rows(board):
    return board.admin("bob").get("/v1/admin/pending").json()


def statuses(board):
    with connect(board.app.state.db_path) as c:
        return [r[0] for r in c.execute("SELECT status FROM pending ORDER BY id")]


# --- Two provisional changes, rejected --------------------------------------

def two_provisional(board):
    first = set_rights(board, "alice", READ)["pending_id"]
    second = set_rights(board, "alice", BOTH)["pending_id"]
    board.admin("bob")
    return first, second


def test_rejecting_both_in_listed_order_leaves_no_access(board, agent):
    two_provisional(board)
    assert now_has(agent) == BOTH
    for row in open_rows(board):
        assert decide(board, "bob", row["id"], "reject").status_code == 200
    assert now_has(agent) == NONE
    assert open_rows(board) == [] and statuses(board) == ["rejected", "rejected"]


def test_rejecting_both_newest_first_leaves_no_access(board, agent):
    first, second = two_provisional(board)
    assert decide(board, "bob", second, "reject").status_code == 200
    assert now_has(agent) == READ
    assert decide(board, "bob", first, "reject").status_code == 200
    assert now_has(agent) == NONE


def test_rejecting_the_earlier_change_takes_its_rights_away_at_once(board, agent):
    first, second = two_provisional(board)
    assert decide(board, "bob", first, "reject").status_code == 200
    assert now_has(agent) == NONE  # no wider than the admin who rejected it expects
    row = next(r for r in open_rows(board) if r["id"] == second)
    # What is left is an ordinary request: not in effect, and it says so.
    assert (row["status"], row["before"], row["after"]) == ("pending", list(NONE), list(BOTH))


def test_the_later_change_can_still_be_approved_on_its_own_merits(board, agent):
    first, second = two_provisional(board)
    decide(board, "bob", first, "reject")
    assert decide(board, "bob", second, "approve").json()["status"] == "approved"
    assert now_has(agent) == BOTH


def test_the_requester_still_cannot_approve_the_reopened_change(board, agent):
    first, second = two_provisional(board)
    decide(board, "bob", first, "reject")
    assert decide(board, "alice", second, "approve").status_code == 403
    assert now_has(agent) == NONE


def test_ratify_the_first_then_reject_the_second_keeps_only_the_first(board, agent):
    first, second = two_provisional(board)
    decide(board, "bob", first, "approve")
    decide(board, "bob", second, "reject")
    assert now_has(agent) == READ


def test_rejecting_the_first_after_ratifying_the_second_drops_only_what_the_first_added(board, agent):
    first, second = two_provisional(board)
    decide(board, "bob", second, "approve")
    decide(board, "bob", first, "reject")
    assert now_has(agent) == POST  # read came from the rejected change; nothing is restored


def test_a_rejection_undoes_a_change_that_swapped_rights(board, agent):
    board.admin("bob")
    pid = set_rights(board, "alice", READ)["pending_id"]
    decide(board, "bob", pid, "approve")
    with connect(board.app.state.db_path) as c:
        c.execute("UPDATE admins SET last_login_at = NULL WHERE name = 'bob'")
    swap = set_rights(board, "alice", POST)
    assert swap["status"] == "provisional" and now_has(agent) == POST
    decide(board, "bob", swap["pending_id"], "reject")
    assert now_has(agent) == READ


def test_a_later_change_left_asking_for_nothing_new_is_closed_not_shown(board, agent):
    board.admin("bob")
    decide(board, "bob", set_rights(board, "alice", READ)["pending_id"], "approve")
    with connect(board.app.state.db_path) as c:
        c.execute("UPDATE admins SET last_login_at = NULL WHERE name = 'bob'")
    swap = set_rights(board, "alice", POST)["pending_id"]
    back = set_rights(board, "alice", READ)["pending_id"]  # built on the swap
    assert decide(board, "bob", swap, "reject").status_code == 200
    # Demoted, the later change would want read only with read only held: nothing new.
    assert now_has(agent) == READ and open_rows(board) == []
    assert statuses(board)[-2:] == ["rejected", "superseded"]
    assert decide(board, "bob", back, "approve").status_code == 404
    with connect(board.app.state.db_path) as c:
        audit = [e for e in service.list_audit(c, service.Caller("bob", "human", "bob"))
                 if e["action"] == "superseded"]
    assert [e["detail"]["pending_ids"] for e in audit] == [[back]]


@pytest.mark.parametrize("requested_by, raised_by_upgrade", [("nobody", None),
                                                             ("alice", "unexplained")])
def test_a_later_change_no_admin_asked_for_is_closed_not_turned_into_a_request(
        board, agent, requested_by, raised_by_upgrade):
    # A later open change that no admin actually asked for (its requester is not an
    # admin, or the upgrade raised it) must not become a request one admin could
    # approve alone when an earlier change is rejected: it goes with the rejection.
    first = set_rights(board, "alice", READ)["pending_id"]  # provisional, bob never logged in
    with connect(board.app.state.db_path) as c:
        later = c.execute(
            "INSERT INTO pending (lane_id, principal_id, before_json, after_json, requested_by, "
            "status, created_at) SELECT id, 'planner', '[true, false]', '[true, true]', ?, "
            "'provisional', 'then' FROM lanes WHERE slug = 'household'", (requested_by,)).lastrowid
        if raised_by_upgrade:
            c.execute("UPDATE pending SET upgrade_reason = ? WHERE id = ?", (raised_by_upgrade, later))
        c.execute("UPDATE grants SET can_post = 1 WHERE principal_id = 'planner'")
    assert now_has(agent) == BOTH
    assert decide(board, "bob", first, "reject").status_code == 200
    assert now_has(agent) == NONE and open_rows(board) == []
    assert statuses(board) == ["rejected", "superseded"]
    for admin in ("alice", "bob"):
        assert decide(board, admin, later, "approve").status_code == 404
    assert now_has(agent) == NONE


# --- Add, widen, remove, then reject ----------------------------------------

def test_removal_closes_every_open_change_and_reject_cannot_bring_read_back(board, agent):
    first, second = two_provisional(board)
    assert set_rights(board, "alice", NONE)["status"] == "applied"
    assert now_has(agent) == NONE
    assert open_rows(board) == [] and statuses(board) == ["superseded", "superseded"]
    for pid in (second, first):
        assert decide(board, "bob", pid, "reject").status_code == 404
        assert decide(board, "bob", pid, "approve").status_code == 404
    assert now_has(agent) == NONE


def test_either_admin_can_remove_and_it_sticks(board, agent):
    two_provisional(board)
    assert set_rights(board, "bob", NONE)["status"] == "applied"
    assert now_has(agent) == NONE and open_rows(board) == []


# --- A pending add after a removal ------------------------------------------

def test_a_pending_add_is_not_approvable_after_a_removal(board, agent):
    board.admin("bob")
    pid = set_rights(board, "alice", BOTH)["pending_id"]
    assert now_has(agent) == NONE
    assert set_rights(board, "bob", NONE)["status"] == "applied"  # nothing to remove yet: a no-op
    assert open_rows(board) == []
    assert decide(board, "bob", pid, "approve").status_code == 404
    assert now_has(agent) == NONE


def test_after_a_removal_a_new_request_works_normally(board, agent):
    board.admin("bob")
    set_rights(board, "alice", BOTH)
    set_rights(board, "bob", NONE)
    pid = set_rights(board, "alice", READ)["pending_id"]
    assert decide(board, "bob", pid, "approve").status_code == 200
    assert now_has(agent) == READ


# --- Narrowing --------------------------------------------------------------

def test_narrowing_closes_a_pending_widening_that_goes_beyond_it(board, agent):
    board.admin("bob")
    decide(board, "bob", set_rights(board, "alice", BOTH)["pending_id"], "approve")
    assert set_rights(board, "bob", READ)["status"] == "applied"
    pid = set_rights(board, "alice", BOTH)["pending_id"]
    assert set_rights(board, "bob", NONE)["status"] == "applied"
    assert decide(board, "bob", pid, "approve").status_code == 404
    assert now_has(agent) == NONE


def test_narrowing_a_provisional_change_keeps_what_is_left_up_for_ratification(board, agent):
    set_rights(board, "alice", BOTH)
    assert set_rights(board, "alice", READ)["status"] == "applied"
    assert now_has(agent) == READ
    rows = open_rows(board)
    assert [(r["status"], r["before"], r["after"], r["requested_by"]) for r in rows] == \
        [("provisional", list(NONE), list(READ), "alice")]
    assert decide(board, "bob", rows[0]["id"], "reject").status_code == 200
    assert now_has(agent) == NONE


def test_narrowing_to_an_earlier_step_leaves_that_step_open(board, agent):
    first, second = two_provisional(board)
    assert set_rights(board, "bob", READ)["status"] == "applied"
    assert [r["id"] for r in open_rows(board)] == [first]
    assert statuses(board) == ["provisional", "superseded"]
    assert decide(board, "bob", first, "reject").status_code == 200
    assert now_has(agent) == NONE


def test_re_setting_the_same_rights_closes_only_what_goes_beyond_them(board, agent):
    set_rights(board, "alice", READ)
    assert set_rights(board, "bob", READ)["status"] == "applied"
    assert [r["after"] for r in open_rows(board)] == [list(READ)]  # it asked for no more than this


def test_other_agents_and_lanes_are_left_alone(board, agent):
    other = board.register("alice", "other")
    set_rights(board, "alice", READ)
    set_rights(board, "alice", READ, principal="other")
    set_rights(board, "alice", READ, lane="trips")
    set_rights(board, "alice", NONE)
    assert sorted((r["lane"], r["principal"]) for r in open_rows(board)) == \
        [("household", "other"), ("trips", "planner")]
    assert now_has(other) == READ and now_has(agent, "trips") == READ


def test_the_audit_log_shows_a_change_was_superseded(board, agent):
    first, second = two_provisional(board)
    set_rights(board, "bob", NONE)
    entry = next(e for e in board.admin("alice").get("/v1/admin/audit").json()
                 if e["action"] == "superseded")
    assert entry["actor"] == "bob"
    assert entry["detail"]["pending_ids"] == [first, second]
    with connect(board.app.state.db_path) as c:
        row = c.execute("SELECT resolved_by, resolved_at FROM pending WHERE id = ?", (first,)).fetchone()
    assert row["resolved_by"] == "bob" and row["resolved_at"]


def test_the_admin_page_lists_no_superseded_change(board, agent):
    two_provisional(board)
    set_rights(board, "alice", NONE)
    assert "Waiting on approval" not in board.admin("bob").get("/admin").text


# --- No right comes back or stays without a reason ---------------------------

def seed_grant(board, rights, principal="planner", lane="household"):
    """A right held from before any change, as a legacy grant can be."""
    with connect(board.app.state.db_path) as c:
        c.execute("INSERT INTO grants (lane_id, principal_id, can_read, can_post) "
                  "SELECT id, ?, ?, ? FROM lanes WHERE slug = ?",
                  (principal, int(rights[0]), int(rights[1]), lane))


def reject_everything(board, admin="bob"):
    for row in open_rows(board):
        assert decide(board, admin, row["id"], "reject").status_code == 200


def test_a_requester_cannot_take_a_right_off_the_list_and_keep_it(board, agent):
    # r-, -p, -p, rp, r-: three rows once closed, the grant stayed read, and the
    # other admin's list was empty.
    for rights in (READ, POST, POST, BOTH, READ):
        set_rights(board, "alice", rights)
    assert now_has(agent) == READ
    assert open_rows(board), "read is held and nothing is left for bob to act on"
    reject_everything(board)
    assert now_has(agent) == NONE


def test_double_tap_then_a_reject_does_not_bring_read_back(board, agent):
    set_rights(board, "alice", READ)
    swap = set_rights(board, "alice", POST)["pending_id"]
    set_rights(board, "alice", POST)  # the second tap
    assert now_has(agent) == POST
    assert decide(board, "bob", swap, "reject").status_code == 200
    assert now_has(agent) == NONE


def test_a_rejected_swap_cannot_bring_back_a_right_the_other_admin_removed(board, agent):
    seed_grant(board, READ)
    change = set_rights(board, "alice", BOTH)["pending_id"]
    assert now_has(agent) == BOTH
    assert set_rights(board, "bob", POST)["status"] == "applied"  # bob logs in; read is removed
    assert now_has(agent) == POST
    assert [(r["before"], r["after"]) for r in open_rows(board)] == [(list(NONE), list(POST))]
    assert decide(board, "bob", change, "reject").status_code == 200
    assert now_has(agent) == NONE  # read stays removed


def test_a_swap_that_is_rejected_gives_back_what_was_held_before(board, agent):
    seed_grant(board, READ)
    swap = set_rights(board, "alice", POST)["pending_id"]
    assert now_has(agent) == POST
    decide(board, "bob", swap, "reject")
    assert now_has(agent) == READ  # nobody removed it: the swap is simply undone


def test_an_approval_that_drops_a_right_cannot_be_undone_by_rejecting_an_older_swap(board, agent):
    # Found by the state explorer: alice sets -p, alice sets r-, bob sets -p,
    # bob approves #2, alice approves #3, bob rejects #1 (read was held from the start).
    seed_grant(board, READ)
    older = set_rights(board, "alice", POST)["pending_id"]       # provisional swap
    newer = set_rights(board, "alice", READ)["pending_id"]       # provisional swap back
    bobs = set_rights(board, "bob", POST)                        # bob logs in: a request
    assert bobs["status"] == "pending"
    assert decide(board, "bob", newer, "approve").status_code == 200
    assert decide(board, "alice", bobs["pending_id"], "approve").status_code == 200
    assert now_has(agent) == POST   # alice approved bob's "post only"
    assert decide(board, "bob", older, "reject").status_code == 200
    assert now_has(agent)[0] is False  # read was dropped by an approval and stays dropped


def test_pending_requests_show_what_the_agent_holds_now(board, agent):
    board.admin("bob")
    first = set_rights(board, "alice", READ)["pending_id"]
    second = set_rights(board, "alice", POST)["pending_id"]
    assert [(r["before"], r["after"]) for r in open_rows(board)] == \
        [(list(NONE), list(READ)), (list(NONE), list(POST))]
    decide(board, "bob", first, "approve")
    row = next(r for r in open_rows(board) if r["id"] == second)
    assert (row["before"], row["after"]) == (list(READ), list(POST))


def test_approving_a_request_gives_exactly_what_it_says_it_wants(board, agent):
    board.admin("bob")
    first = set_rights(board, "alice", READ)["pending_id"]
    second = set_rights(board, "alice", POST)["pending_id"]
    decide(board, "bob", first, "approve")
    assert now_has(agent) == READ
    assert "agent wants post only (now read only)".replace("agent", "planner") in \
        board.admin("bob").get("/admin").text
    decide(board, "bob", second, "approve")
    assert now_has(agent) == POST  # as the page said


def test_narrowing_cuts_an_open_change_back_in_place(board, agent):
    both = set_rights(board, "alice", BOTH)["pending_id"]
    set_rights(board, "alice", READ)
    (row,) = open_rows(board)
    assert (row["id"], row["before"], row["after"], row["status"]) == \
        (both, list(NONE), list(READ), "provisional")
    entry = next(e for e in board.admin("bob").get("/v1/admin/audit").json()
                 if e["action"] == "narrowed")
    assert entry["detail"]["pending_ids"] == [both]


def test_the_requester_cannot_resolve_any_of_these_changes(board, agent):
    for rights in (READ, POST, BOTH):
        set_rights(board, "alice", rights)
    for row in open_rows(board):
        for decision in ("approve", "reject"):
            assert decide(board, "alice", row["id"], decision).status_code == 403


# --- Legacy rows ------------------------------------------------------------

def v6_database(tmp_path, monkeypatch):
    """A board at migration 6, as the previous version left it."""
    db = tmp_path / "v6.db"
    with monkeypatch.context() as m:
        m.setattr("board.migrations.MIGRATIONS", MIGRATIONS[:6])
        assert migrate_db(db) == 6
    conn = sqlite3.connect(db)
    for name in ("alice", "bob"):
        conn.execute("INSERT INTO principals (id, kind, owner, display_name, created_at) "
                     "VALUES (?, 'human', ?, ?, 'then')", (name, name, name))
        conn.execute("INSERT INTO admins (name, last_login_at) VALUES (?, 'then')", (name,))
    for agent_id in ("a1", "a2", "a3", "a4", "a5", "a6"):
        conn.execute("INSERT INTO principals (id, kind, owner, display_name, created_at) "
                     "VALUES (?, 'agent', 'alice', ?, 'then')", (agent_id, agent_id))
    conn.execute("INSERT INTO lanes (id, slug, name, scope, notice, created_at) "
                 "VALUES (1, 'household', 'H', 'shared', 'n', 'then')")
    return db, conn


def legacy_change(conn, agent_id, before, after, status="provisional", logged=True):
    cur = conn.execute(
        "INSERT INTO pending (lane_id, principal_id, before_json, after_json, requested_by, status, "
        "created_at) VALUES (1, ?, ?, ?, 'alice', ?, 'then')",
        (agent_id, json.dumps(before), json.dumps(after), status))
    if logged:
        conn.execute("INSERT INTO audit_log (ts, actor, action, scope, detail) VALUES "
                     "('then', 'alice', ?, 'shared', ?)",
                     (f"grant-{status}", json.dumps({"pending_id": cur.lastrowid, "lane": "household",
                                                    "principal": agent_id, "before": before,
                                                    "after": after})))
    return cur.lastrowid


def legacy_direct(conn, actor, agent_id, before, after):
    conn.execute("INSERT INTO audit_log (ts, actor, action, scope, detail) VALUES "
                 "('then', ?, 'grant', 'shared', ?)",
                 (actor, json.dumps({"lane": "household", "principal": agent_id,
                                     "before": before, "after": after})))
    conn.execute("DELETE FROM grants WHERE lane_id = 1 AND principal_id = ?", (agent_id,))
    if any(after):
        conn.execute("INSERT INTO grants VALUES (1, ?, ?, ?)", (agent_id, int(after[0]), int(after[1])))


def legacy_status(db):
    with connect(db) as c:
        return {r["id"]: r["status"] for r in c.execute("SELECT id, status FROM pending")}


def test_upgrade_closes_legacy_changes_a_removal_already_overruled(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    # add, widen, remove, all before this version
    add = legacy_change(conn, "a1", NONE, READ)
    widen = legacy_change(conn, "a1", READ, BOTH)
    legacy_direct(conn, "alice", "a1", BOTH, NONE)
    # a pending add, then a no-op removal
    waiting = legacy_change(conn, "a2", NONE, BOTH, status="pending")
    legacy_direct(conn, "bob", "a2", NONE, NONE)
    # a removal first and a fresh request after it: the request stands
    legacy_direct(conn, "bob", "a3", READ, NONE)
    fresh = legacy_change(conn, "a3", NONE, READ, status="pending")
    # untouched: an ordinary open change still in effect
    conn.execute("INSERT INTO grants VALUES (1, 'a4', 1, 0)")
    in_effect = legacy_change(conn, "a4", NONE, READ)
    conn.commit()
    conn.close()

    assert migrate_db(db) == len(MIGRATIONS)
    assert legacy_status(db) == {add: "superseded", widen: "superseded", waiting: "superseded",
                                 fresh: "pending", in_effect: "provisional"}
    bob = service.Caller("bob", "human", "bob")
    with connect(db) as c:
        for pid in (add, widen, waiting):
            with pytest.raises(service.BoardError) as e:
                service.resolve_pending(c, bob, pid, False)
            assert e.value.status == 404
        assert service._grant(c, 1, "a1") == NONE and service._grant(c, 1, "a2") == NONE
        service.resolve_pending(c, bob, in_effect, False)
        assert service._grant(c, 1, "a4") == NONE
        audit = [e for e in service.list_audit(c, bob) if e["action"] == "superseded"]
    assert sorted(i for e in audit for i in e["detail"]["pending_ids"]) == sorted([add, widen, waiting])


def test_upgrade_keeps_what_a_legacy_narrowing_left_up_for_ratification(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    pid = legacy_change(conn, "a1", NONE, BOTH)
    legacy_direct(conn, "alice", "a1", BOTH, READ)
    conn.commit()
    conn.close()
    migrate_db(db)
    bob = service.Caller("bob", "human", "bob")
    with connect(db) as c:
        rows = service.list_pending(c, bob)
        assert [(r["before"], r["after"], r["status"], r["requested_by"]) for r in rows] == \
            [(NONE, READ, "provisional", "alice")]
        assert rows[0]["id"] == pid  # cut back in place, not replaced
        service.resolve_pending(c, bob, rows[0]["id"], False)
        assert service._grant(c, 1, "a1") == NONE


def test_upgrade_leaves_a_row_it_cannot_judge_open_and_harmless(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    conn.execute("INSERT INTO pending (lane_id, principal_id, before_json, after_json, requested_by, "
                 "status, created_at) VALUES (1, 'a1', '{}', '{}', 'alice', 'provisional', 'then')")
    unlogged = legacy_change(conn, "a2", NONE, READ, status="pending", logged=False)
    conn.commit()
    conn.close()
    migrate_db(db)
    bob = service.Caller("bob", "human", "bob")
    with connect(db) as c:
        rows = service.list_pending(c, bob)
        assert len(rows) == 2 and rows[0]["before"] == rows[0]["after"] == NONE
        assert service.resolve_pending(c, bob, rows[0]["id"], False)["status"] == "rejected"
        assert service.resolve_pending(c, bob, unlogged, True)["status"] == "approved"
        assert service._grant(c, 1, "a1") == NONE and service._grant(c, 1, "a2") == READ


def test_legacy_rows_keep_their_data_and_ids_keep_counting_up(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    conn.execute("INSERT INTO pending (id, lane_id, principal_id, before_json, after_json, requested_by, "
                 "status, created_at, resolved_by, resolved_at) VALUES (41, 1, 'a1', '[false, false]', "
                 "'[true, false]', 'alice', 'ratified', 'then', 'bob', 'later')")
    conn.commit()
    conn.close()
    migrate_db(db)
    with connect(db) as c:
        old = c.execute("SELECT * FROM pending").fetchone()
        assert tuple(old)[0:2] == (41, 1) and old["status"] == "ratified" and old["resolved_by"] == "bob"
        cur = c.execute("INSERT INTO pending (lane_id, principal_id, before_json, after_json, requested_by, "
                        "status, created_at) VALUES (1, 'a1', '[]', '[]', 'alice', 'superseded', 'now')")
        assert cur.lastrowid == 42
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE pending SET status = 'nonsense'")


def test_upgrade_is_not_stopped_by_an_audit_entry_it_cannot_read(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    pid = legacy_change(conn, "a1", NONE, READ, status="pending")
    conn.execute("INSERT INTO audit_log (ts, actor, action, scope, detail) VALUES "
                 "('then', 'alice', 'grant', 'shared', 'not json')")
    conn.execute("INSERT INTO audit_log (ts, actor, action, scope, detail) VALUES "
                 "('then', 'alice', 'grant', 'shared', '[1, 2]')")
    conn.commit()
    conn.close()
    assert migrate_db(db) == len(MIGRATIONS)
    assert legacy_status(db) == {pid: "pending"}


# --- Legacy rows obey the same rule after the upgrade ------------------------

def legacy_grant(conn, agent_id, rights):
    conn.execute("DELETE FROM grants WHERE lane_id = 1 AND principal_id = ?", (agent_id,))
    if any(rights):
        conn.execute("INSERT INTO grants VALUES (1, ?, ?, ?)", (agent_id, int(rights[0]), int(rights[1])))


def legacy_approval(conn, pending_id):
    """The old version approved a pending change by marking it so and logging it."""
    conn.execute("UPDATE pending SET status = 'approved', resolved_by = 'bob', resolved_at = 'then' "
                 "WHERE id = ?", (pending_id,))
    conn.execute("INSERT INTO audit_log (ts, actor, action, scope, detail) VALUES "
                 "('then', 'bob', 'approved', 'shared', ?)", (json.dumps({"pending_id": pending_id}),))


def upgraded_rows(db, agent_id):
    bob = service.Caller("bob", "human", "bob")
    with connect(db) as c:
        return [(r["before"], r["after"], r["status"]) for r in service.list_pending(c, bob)
                if r["principal"] == agent_id], service._grant(c, 1, agent_id)


def explore_upgraded(db, agent_id):
    """Run the state explorer from the upgraded database. What it holds and no open
    provisional change claims counts as held from before; the rest is on a row."""
    from test_pending_exploration import explore_from, report
    src = sqlite3.connect(db)
    blob = src.serialize()
    src.close()
    rows, held = upgraded_rows(db, agent_id)
    claimed = [False, False]
    for before, after, status in rows:
        if status == "provisional":
            claimed = [c or (a and not b) for c, a, b in zip(claimed, after, before)]
    auth = tuple(h and not c for h, c in zip(held, claimed))
    found, states, tried = explore_from(blob, auth, principal=agent_id, depth=6)
    assert not found, report(found, states, tried)


def test_upgrade_does_not_let_a_rejection_bring_back_a_right_a_logged_removal_took(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    legacy_grant(conn, "a1", READ)                                   # held before
    legacy_change(conn, "a1", READ, BOTH)                            # alice, provisional
    legacy_direct(conn, "bob", "a1", BOTH, POST)                     # bob removes read
    conn.commit()
    conn.close()
    migrate_db(db)
    assert upgraded_rows(db, "a1") == ([(NONE, POST, "provisional")], POST)
    bob = service.Caller("bob", "human", "bob")
    with connect(db) as c:
        (row,) = service.list_pending(c, bob)
        service.resolve_pending(c, bob, row["id"], False)
        assert service._grant(c, 1, "a1") == NONE
    explore_upgraded(db, "a1")


def test_upgrade_closes_the_earlier_step_of_a_double_tap(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    first = legacy_change(conn, "a1", NONE, READ)
    second = legacy_change(conn, "a1", READ, POST)
    legacy_grant(conn, "a1", POST)
    legacy_direct(conn, "alice", "a1", POST, POST)                   # the second tap
    conn.commit()
    conn.close()
    migrate_db(db)
    assert legacy_status(db) == {first: "superseded", second: "provisional"}
    assert upgraded_rows(db, "a1") == ([(NONE, POST, "provisional")], POST)
    explore_upgraded(db, "a1")


def test_upgrade_strikes_a_right_a_logged_approval_dropped(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    legacy_grant(conn, "a1", READ)
    swap = legacy_change(conn, "a1", READ, POST)                     # provisional swap
    legacy_grant(conn, "a1", POST)
    approved = legacy_change(conn, "a1", POST, POST, status="pending")  # bob's request for post only
    legacy_approval(conn, approved)
    conn.commit()
    conn.close()
    migrate_db(db)
    assert upgraded_rows(db, "a1") == ([(NONE, POST, "provisional")], POST)
    explore_upgraded(db, "a1")


def test_upgrade_gives_a_row_with_no_history_nothing_to_give_back(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    legacy_grant(conn, "a1", POST)
    legacy_change(conn, "a1", READ, POST, logged=False)              # a swap nobody logged
    conn.commit()
    conn.close()
    migrate_db(db)
    assert upgraded_rows(db, "a1") == ([(NONE, POST, "provisional")], POST)
    explore_upgraded(db, "a1")


def test_upgraded_untouched_changes_stay_exactly_as_they_were(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    legacy_grant(conn, "a1", READ)
    legacy_change(conn, "a1", READ, BOTH)
    legacy_change(conn, "a2", NONE, READ, status="pending")
    conn.commit()
    conn.close()
    migrate_db(db)
    assert upgraded_rows(db, "a1")[0] == [(READ, BOTH, "provisional")]
    assert upgraded_rows(db, "a2")[0] == [(NONE, READ, "pending")]
    explore_upgraded(db, "a1")
    explore_upgraded(db, "a2")

"""Upgrading a board whose shared-lane history includes rejections made by the
previous version (DESIGN.md section 6, "Upgrading").

The previous version rejected a provisional change by putting back exactly what
the agent held when it was asked, whatever had happened since. So an old
rejection could give back a right that a removal took, or one that another
rejection had just undone. The upgrade has to work out from the audit log which
rights an admin actually blessed, and leave every other held right on an open
change that another admin can still decide.

`Legacy` below drives a database at migration 6 the way the previous version
did, writing the same rows and audit entries, so these tests need no old code.
"""

import json
import sqlite3

import pytest

from board import service
from board.db import connect
from board.migrations import MIGRATIONS, migrate_db
from test_pending import BOTH, NONE, POST, READ, explore_upgraded, v6_database

BOB = service.Caller("bob", "human", "bob")
ALICE = service.Caller("alice", "human", "alice")


class Legacy:
    """The previous version's set_grant and resolve_pending, on raw rows."""

    def __init__(self, conn, agent="a1", seed=NONE, logged_in=("alice",)):
        self.conn, self.agent = conn, agent
        self.logged_in = set(logged_in)
        if any(seed):
            conn.execute("INSERT INTO grants VALUES (1, ?, ?, ?)", (agent, int(seed[0]), int(seed[1])))

    def grant(self):
        row = self.conn.execute("SELECT can_read, can_post FROM grants WHERE lane_id = 1 AND "
                                "principal_id = ?", (self.agent,)).fetchone()
        return (bool(row[0]), bool(row[1])) if row else NONE

    def _apply(self, rights):
        self.conn.execute("DELETE FROM grants WHERE lane_id = 1 AND principal_id = ?", (self.agent,))
        if any(rights):
            self.conn.execute("INSERT INTO grants VALUES (1, ?, ?, ?)",
                              (self.agent, int(rights[0]), int(rights[1])))

    def _audit(self, actor, action, **detail):
        self.conn.execute("INSERT INTO audit_log (ts, actor, action, scope, detail) "
                          "VALUES ('then', ?, ?, 'shared', ?)", (actor, action, json.dumps(detail)))

    def set(self, admin, rights):
        self.logged_in.add(admin)
        before = self.grant()
        detail = {"lane": "household", "principal": self.agent, "before": before, "after": rights}
        if not any(a and not b for a, b in zip(rights, before)):
            self._apply(rights)
            self._audit(admin, "grant", **detail)
            return None
        status = "pending" if self.logged_in - {admin} else "provisional"
        cur = self.conn.execute(
            "INSERT INTO pending (lane_id, principal_id, before_json, after_json, requested_by, "
            "status, created_at) VALUES (1, ?, ?, ?, ?, ?, 'then')",
            (self.agent, json.dumps(before), json.dumps(rights), admin, status))
        if status == "provisional":
            self._apply(rights)
        self._audit(admin, f"grant-{status}", pending_id=cur.lastrowid, **detail)
        return cur.lastrowid

    def decide(self, admin, pending_id, approve):
        self.logged_in.add(admin)
        status, before, after = self.conn.execute(
            "SELECT status, before_json, after_json FROM pending WHERE id = ?", (pending_id,)).fetchone()
        if status == "pending":
            new = "approved" if approve else "rejected"
            if approve:
                self._apply(tuple(json.loads(after)))
        else:
            new = "ratified" if approve else "rejected"
            if not approve:
                self._apply(tuple(json.loads(before)))  # what it held when asked, always
        self.conn.execute("UPDATE pending SET status = ?, resolved_by = ?, resolved_at = 'then' "
                          "WHERE id = ?", (new, admin, pending_id))
        self._audit(admin, new, pending_id=pending_id)


OPEN = ("pending", "provisional")


def upgrade(db, conn):
    """Upgrade, then check what every upgrade must leave true."""
    conn.commit()
    old = {r[0]: r[1:] for r in conn.execute(
        "SELECT id, requested_by, status, before_json, after_json FROM pending")}
    conn.close()
    assert migrate_db(db) == len(MIGRATIONS)
    check_upgraded(db, old)


def check_upgraded(db, old):
    """No row is deleted, no requester changes, a closed row stays as it was, every
    open row has a real admin as its requester and is on every admin's approval
    list, and no admin can approve, ratify or reject a row they requested."""
    with connect(db) as c:
        admins = service.list_admins(c)
        rows = {r["id"]: r for r in c.execute("SELECT * FROM pending")}
        assert set(old) <= set(rows), "the upgrade deleted a row"
        for pid, (requested_by, status, before, after) in old.items():
            assert rows[pid]["requested_by"] == requested_by, f"row {pid} changed requester"
            if status not in OPEN:
                assert (rows[pid]["status"], rows[pid]["before_json"], rows[pid]["after_json"]) \
                    == (status, before, after), f"closed row {pid} changed"
        open_ids = sorted(pid for pid, r in rows.items() if r["status"] in OPEN)
        for pid in open_ids:
            assert rows[pid]["requested_by"] in admins, f"row {pid} has no admin requester"
        for name in admins:
            caller = service.Caller(name, "human", name)
            assert [p["id"] for p in service.list_pending(c, caller)] == open_ids
    for pid in open_ids:
        requester = service.Caller(rows[pid]["requested_by"], "human", rows[pid]["requested_by"])
        for approve in (True, False):
            with pytest.raises(service.BoardError) as e, connect(db) as c:
                service.resolve_pending(c, requester, pid, approve)
            assert e.value.status == 403


def open_rows(db):
    with connect(db) as c:
        return [(r["id"], r["before"], r["after"], r["status"], r["requested_by"])
                for r in service.list_pending(c, BOB)]


def held(db, agent="a1"):
    with connect(db) as c:
        return service._grant(c, 1, agent)


def decide(db, who, pending_id, approve):
    with connect(db) as c:
        return service.resolve_pending(c, who, pending_id, approve)


# --- The two shortest cases ---------------------------------------------------

def removal_logged(db):
    with connect(db) as c:
        return [e["detail"] for e in service.list_audit(c, BOB)
                if e["action"] == "removed" and e["actor"] == "upgrade"]


def test_a_right_an_old_rejection_gave_back_after_it_was_removed_is_removed_at_upgrade(
        tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn, seed=READ)
    old.set("alice", POST)                          # 1, provisional
    second = old.set("alice", READ)                 # 2, provisional
    old.set("alice", NONE)                          # a removal
    old.decide("bob", 1, False)                     # the old rejection put read back
    assert old.grant() == READ
    upgrade(db, conn)
    # Alice took read away and nobody approved it since: bob's old rejection put it
    # back by mistake. The upgrade removes it, closes alice's request for it, and
    # logs what it did.
    assert held(db) == NONE
    assert open_rows(db) == []
    assert statuses(db)[second] == "superseded"
    assert [(d["lane"], d["principal"], tuple(d["before"]), tuple(d["after"]))
            for d in removal_logged(db)] == [("household", "a1", READ, NONE)]
    explore_upgraded_copy(tmp_path, db)


def test_rejecting_an_upgraded_change_cannot_give_back_a_right_already_rejected(
        tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn, seed=READ)
    old.set("alice", POST)                          # 1, provisional
    second = old.set("alice", READ)                 # 2, provisional, built on 1
    old.decide("bob", 1, False)                     # post rejected
    upgrade(db, conn)
    (row,) = open_rows(db)
    assert row[0] == second and not row[1][1]       # it no longer starts from post
    explore_upgraded_copy(tmp_path, db)
    decide(db, BOB, second, False)
    assert not held(db)[1]


# --- More histories with old rejections ---------------------------------------

def test_a_right_an_old_rejection_brought_back_with_nothing_open_is_removed(
        tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn, seed=READ)
    old.set("alice", POST)                          # 1, provisional
    old.set("alice", NONE)                          # read and post removed
    old.decide("bob", 1, False)                     # read came back, with no row open
    assert old.grant() == READ
    upgrade(db, conn)
    assert held(db) == NONE                         # alice removed it; nobody approved it since
    assert open_rows(db) == []
    assert len(removal_logged(db)) == 1


def test_a_right_bob_rejected_that_an_old_rejection_brought_back_is_removed(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn)
    old.set("alice", READ)                          # 1, provisional
    old.set("alice", POST)                          # 2, provisional
    old.decide("bob", 1, False)                     # read rejected: nothing held
    old.decide("bob", 2, False)                     # the old code put read back
    assert old.grant() == READ
    upgrade(db, conn)
    assert held(db) == NONE                         # bob rejected read; it came back by mistake
    assert open_rows(db) == []


def test_a_later_change_built_on_a_rejected_one_cannot_give_back_its_rights(
        tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn)
    old.set("alice", READ)                          # 1, provisional
    old.set("alice", BOTH)                          # 2, provisional
    old.set("alice", POST)                          # 3, provisional
    old.decide("bob", 1, False)                     # back to nothing
    upgrade(db, conn)
    for pid, *_ in open_rows(db):
        decide(db, BOB, pid, False)
    assert held(db) == NONE
    assert open_rows(db) == []


def test_rejections_that_left_nothing_unblessed_change_nothing(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    one = Legacy(conn, "a1")
    one.set("alice", READ)
    one.decide("bob", 1, True)                      # ratified
    one.decide("alice", one.set("bob", BOTH), False)  # a pending request, rejected
    two = Legacy(conn, "a2", seed=READ)
    two.decide("bob", two.set("alice", BOTH), False)  # back to what was held before
    conn.commit()
    before = dump(db)
    upgrade(db, conn)
    assert held(db, "a1") == READ and held(db, "a2") == READ
    assert open_rows(db) == []
    with connect(db) as c:
        assert not c.execute("SELECT 1 FROM audit_log WHERE actor = 'upgrade'").fetchone()
        rows = c.execute("SELECT id, before_json, after_json, status FROM pending ORDER BY id")
        assert [tuple(r) for r in rows] == before


def test_upgrading_twice_changes_nothing_the_second_time(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn, seed=READ)
    old.set("alice", POST)
    old.set("alice", READ)
    old.set("alice", NONE)
    old.decide("bob", 1, False)
    upgrade(db, conn)
    first = full_dump(db)
    assert migrate_db(db) == len(MIGRATIONS)
    assert full_dump(db) == first
    with connect(db) as c:  # and migration 8 ran on top of it
        assert c.execute("SELECT name FROM sqlite_master WHERE name = 'login_pairs'").fetchone()


# --- Who decides a right the upgrade puts on a new row ------------------------
#
# A right the upgrade leaves on a new row was given back by an old rejection, not
# asked for by anyone. The row names a real admin as its requester, never one the
# admin who last took the right away: so that admin decides, and nobody can keep
# or restore it on their own say. If an earlier change is rejected, the new row
# goes with it instead of turning into a request one admin could approve alone.

def test_a_right_taken_away_and_given_back_by_an_old_rejection_does_not_come_back(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn, seed=READ)                   # bob has never logged in
    first = old.set("alice", POST)                  # 1, provisional
    old.set("alice", NONE)                          # alice removes read
    second = old.set("alice", POST)                 # 2, provisional
    old.decide("bob", first, False)                 # the old rejection put read back
    assert old.grant() == READ
    upgrade(db, conn)
    assert held(db) == NONE                         # read is removed; nobody approved it back
    assert [r[0] for r in open_rows(db)] <= [second]
    assert one_admin_alone_can_reach(tmp_path, db) <= {NONE, POST}
    for pid, *_ in open_rows(db):                   # rejecting what is left leaves nothing
        decide(db, BOB, pid, False)
    assert held(db) == NONE


def test_a_right_one_admin_took_and_his_old_rejection_gave_back_is_removed(
        tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn, seed=READ)
    first = old.set("alice", BOTH)                  # 1, provisional
    old.set("bob", NONE)                            # bob removes read and post
    old.decide("bob", first, False)                 # and his old rejection put read back
    assert old.grant() == READ
    upgrade(db, conn)
    assert held(db) == NONE                         # bob took it; no one approved it since
    assert open_rows(db) == []


def test_a_right_rejected_on_a_damaged_history_cannot_come_back_on_one_admins_say(
        tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn, seed=READ)
    first = old.set("alice", BOTH)                  # 1, provisional
    # marked approved by hand, with nothing logged: the history doesn't add up
    conn.execute("UPDATE pending SET status = 'approved', resolved_by = 'bob' WHERE id = ?", (first,))
    upgrade(db, conn)
    assert held(db) == BOTH
    rows = open_rows(db)
    assert rows and all(r[3] == "provisional" for r in rows)
    decide(db, BOB, rows[0][0], False)              # bob rejects the first open row
    assert not held(db)[1]
    assert all(not rights[1] for rights in one_admin_alone_can_reach(tmp_path, db))


def test_rights_asked_for_by_different_admins_on_a_damaged_history_go_on_rows_in_turn(
        tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn)
    first = old.set("alice", READ)                  # 1, provisional
    old.decide("bob", first, True)                  # ratified
    second = old.set("bob", BOTH)                   # 2, pending
    old.decide("alice", second, True)               # approved
    conn.execute("DELETE FROM audit_log WHERE action = 'grant-provisional'")  # 1's entry lost
    upgrade(db, conn)
    assert held(db) == BOTH
    rows = open_rows(db)
    # read was asked for by alice and post by bob, so each is decided by the other
    assert [r[1:] for r in rows] == [(NONE, READ, "provisional", "alice"),
                                     (READ, BOTH, "provisional", "bob")]
    assert one_admin_alone_can_reach(tmp_path, db) <= {BOTH, READ, NONE}
    copy = copy_of(tmp_path, db)
    decide(copy, BOB, rows[0][0], False)            # takes read, and post with it
    assert held(copy) == NONE and open_rows(copy) == []
    decide(db, ALICE, rows[1][0], False)            # takes post only
    assert held(db) == READ and [r[0] for r in open_rows(db)] == [rows[0][0]]


# --- A damaged log fails closed -----------------------------------------------

def a_right_given_back_by_an_old_rejection(tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn, seed=READ)
    old.set("alice", POST)                          # 1, provisional
    old.set("alice", NONE)                          # read and post removed
    old.decide("bob", 1, False)                     # read came back
    return db, conn


def test_rows_with_no_logged_history_at_all_bless_nothing(tmp_path, monkeypatch):
    db, conn = a_right_given_back_by_an_old_rejection(tmp_path, monkeypatch)
    conn.execute("DELETE FROM audit_log")
    upgrade(db, conn)
    assert held(db) == READ and claimed(db) == READ


def test_an_unreadable_shared_entry_blesses_nothing(tmp_path, monkeypatch):
    db, conn = a_right_given_back_by_an_old_rejection(tmp_path, monkeypatch)
    conn.execute("UPDATE audit_log SET detail = 'not json' WHERE action = 'grant'")
    upgrade(db, conn)
    assert held(db) == READ and claimed(db) == READ


def test_open_rows_no_admin_asked_for_are_closed_and_their_rights_put_on_a_new_row(
        tmp_path, monkeypatch):
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn)
    pid = old.set("alice", READ)                    # 1, provisional
    conn.execute("UPDATE pending SET requested_by = 'nobody' WHERE id = ?", (pid,))
    upgrade(db, conn)
    assert statuses(db)[pid] == "superseded"
    ((new, before, after, status, requested_by),) = open_rows(db)
    assert (before, after, status, requested_by) == (NONE, READ, "provisional", "alice")


# --- helpers ------------------------------------------------------------------

def dump(db):
    c = sqlite3.connect(db)
    try:
        return [tuple(r) for r in c.execute(
            "SELECT id, before_json, after_json, status FROM pending ORDER BY id")]
    finally:
        c.close()


def full_dump(db):
    c = sqlite3.connect(db)
    try:
        return list(c.iterdump()), c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def statuses(db):
    with connect(db) as c:
        return dict(c.execute("SELECT id, status FROM pending").fetchall())


def claimed(db):
    """The rights open provisional rows claim (added and still undecided)."""
    out = NONE
    for _, before, after, status, _ in open_rows(db):
        if status == "provisional":
            out = tuple(o or (a and not b) for o, a, b in zip(out, after, before))
    return out


def copy_of(tmp_path, db, name="copy.db"):
    copy = tmp_path / name
    copy.unlink(missing_ok=True)
    src, dst = sqlite3.connect(db), sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()
    return copy


def one_admin_alone_can_reach(tmp_path, db):
    """Every grant one admin's single approval or ratification of an open row gives."""
    with connect(db) as c:
        ids = [r["id"] for r in service.list_pending(c, BOB)]
        admins = service.list_admins(c)
    reached = set()
    for pid in ids:
        for name in admins:
            copy = copy_of(tmp_path, db, "try.db")
            try:
                decide(copy, service.Caller(name, "human", name), pid, True)
            except service.BoardError:
                continue
            reached.add(held(copy))
    return reached


def explore_upgraded_copy(tmp_path, db):
    """Run the live state explorer from a copy, so the test's own database stays put."""
    copy = tmp_path / "copy.db"
    src, dst = sqlite3.connect(db), sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()
    explore_upgraded(copy, "a1")


def test_one_admin_cannot_get_back_a_right_they_removed_after_the_upgrade(tmp_path, monkeypatch):
    # The six-move replay: alice removes read, bob's old rejection puts it back, and
    # the upgrade used to list it under a requester who is no admin, so alice could
    # approve it alone. Now it is removed at upgrade, and nothing is left to approve.
    db, conn = v6_database(tmp_path, monkeypatch)
    old = Legacy(conn, seed=READ)
    first = old.set("alice", POST)                  # 1, provisional
    old.set("alice", NONE)
    second = old.set("alice", POST)                 # 2, provisional
    old.decide("bob", first, False)                 # read comes back
    upgrade(db, conn)
    assert held(db) == NONE
    assert one_admin_alone_can_reach(tmp_path, db) <= {NONE, POST}
    decide(db, BOB, second, False)
    assert held(db) == NONE and open_rows(db) == []

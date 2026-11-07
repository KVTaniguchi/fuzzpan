"""Every right an agent holds on a shared lane has a reason, whatever order two
admins act in (rule 8, DESIGN.md section 6).

This explores the real service code breadth first: two admins, one agent, one
shared lane, every sequence of the moves below up to DEPTH moves, starting once
with no grant and once with a read grant already held (as legacy grants can be).
After each move it checks the invariant against a small model that knows nothing
about how the board stores its changes:

  * Every right the agent holds was held at the start, or was approved or
    ratified by an admin other than the one who asked for it, or sits on an open
    provisional change that the other admin can still ratify or reject.
  * Taking a right away (directly, or by approving a change that drops it) ends
    its authority: it comes back only through a new change.
  * Nobody resolves their own change, and a closed change cannot be resolved.
  * A pending request changes nothing until it is approved.

Moves: either admin sets any of the four rights; bob logs in for the first time
(until then alice's changes are provisional); either admin approves or rejects
each open change, and the newest closed one (which must be refused).

Bounds, so it runs in a few seconds (about 4):
  * DEPTH = 8 moves. Every break this found in the old code showed within 8:
    the shortest took 3 moves, the five-set sequence r-, -p, -p, rp, r- (a
    double tap, widen, narrow) takes 6, and the longest it found against a
    deliberately broken variant of the fix took 8. With repeated states
    merged, 8 moves reach about 3,600 states from an empty grant and 4,300
    from a held read.
  * At most MAX_OPEN = 3 open changes at once (a state with more is not
    expanded). Every break found needed three or fewer.
Under those bounds the state space closes by 14 moves (4,558 and 5,013 states,
about 6 seconds), and it was run to that depth clean; with MAX_OPEN = 4 it was
run clean to 8. EXPLORE_DEPTH and EXPLORE_OPEN in the environment look further.
"""

import json
import os
import sqlite3

import pytest

from board import service
from board.provision import provision_family
from board.service import BoardError, Caller

DEPTH = int(os.environ.get("EXPLORE_DEPTH", "8"))
MAX_OPEN = int(os.environ.get("EXPLORE_OPEN", "3"))
ALICE, BOB = Caller("alice", "human", "alice"), Caller("bob", "human", "bob")
WHO = {"alice": ALICE, "bob": BOB}
RIGHTS = [(False, False), (True, False), (False, True), (True, True)]
WORDS = {(False, False): "--", (True, False): "r-", (False, True): "-p", (True, True): "rp"}
OPEN = ("pending", "provisional")
NONE = (False, False)


def _beyond(a, b):
    """The rights in a that b lacks."""
    return tuple(x and not y for x, y in zip(a, b))


def _union(a, b):
    return tuple(x or y for x, y in zip(a, b))


def _rights(text):
    value = json.loads(text)
    return (bool(value[0]), bool(value[1]))


def _start(tmp_path, seed):
    path = tmp_path / "explore.db"
    provision_family(path, [("alice", "Alice"), ("bob", "Bob")],
                     [{"slug": "household", "name": "H", "scope": "shared"}])
    src = sqlite3.connect(path)
    src.execute("INSERT INTO principals (id, kind, owner, display_name, created_at) "
                "VALUES ('p', 'agent', 'alice', 'P', 'now')")
    src.execute("UPDATE admins SET last_login_at = 'now' WHERE name = 'alice'")
    if seed != NONE:
        src.execute("INSERT INTO grants (lane_id, principal_id, can_read, can_post) "
                    "VALUES (1, 'p', ?, ?)", (int(seed[0]), int(seed[1])))
    src.commit()
    blob = src.serialize()
    src.close()
    return blob


class World:
    """One in-memory database. A move is tried inside a savepoint and rolled back,
    so a state is only copied out when it is new."""

    def __init__(self, blob, principal="p"):
        self.principal = principal
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.deserialize(blob)
        self.conn.row_factory = sqlite3.Row

    def held(self):
        return service._grant(self.conn, 1, self.principal)

    def rows(self):
        return self.conn.execute("SELECT * FROM pending ORDER BY id").fetchall()

    def bob_in(self):
        return self.conn.execute("SELECT last_login_at FROM admins WHERE name = 'bob'"
                                 ).fetchone()[0] is not None

    def login_bob(self):
        self.conn.execute("UPDATE admins SET last_login_at = 'now' WHERE name = 'bob'")

    def key(self, auth, rows):
        shape = tuple((r["status"], r["before_json"], r["after_json"], r["requested_by"])
                      for r in rows if r["status"] in OPEN)
        return (self.held(), shape, auth, self.bob_in(),
                any(r["status"] not in OPEN for r in rows))

    def moves(self, rows):
        out = [("set", who, r) for who in WHO for r in RIGHTS]
        if not self.bob_in():
            out.append(("login", "bob"))
        targets = [r["id"] for r in rows if r["status"] in OPEN]
        targets += [r["id"] for r in rows if r["status"] not in OPEN][-1:]
        for pid in targets:
            for who in WHO:
                out += [("resolve", who, pid, True), ("resolve", who, pid, False)]
        return out


def describe(path):
    words = []
    for m in path:
        if m[0] == "set":
            words.append(f"{m[1]} sets {WORDS[m[2]]}")
        elif m[0] == "login":
            words.append("bob logs in")
        else:
            words.append(f"{m[1]} {'approves' if m[3] else 'rejects'} #{m[2]}")
    return "; ".join(words)


def try_move(world, parent, rows, auth, move, is_new):
    """Apply one move, check it, and put the database back as it was (`parent` is
    its copy). Returns None when the board refused the move; otherwise (problems,
    new auth, key, blob), where blob is a copy of the resulting database if
    is_new(key), else None."""
    conn = world.conn
    held0 = world.held()
    problems = []
    row = result = None
    conn.execute("BEGIN")
    try:
        try:
            if move[0] == "login":
                world.login_bob()
            elif move[0] == "set":
                if move[1] == "bob":
                    world.login_bob()
                result = service.set_grant(conn, WHO[move[1]], "household", world.principal,
                                           *move[2])
            else:
                who, pid, approve = move[1], move[2], move[3]
                if who == "bob":
                    world.login_bob()
                row = next(r for r in rows if r["id"] == pid)
                service.resolve_pending(conn, WHO[who], pid, approve)
                if row["status"] not in OPEN:
                    problems.append("a closed change was resolved")
                if row["requested_by"] == who:
                    problems.append("a requester resolved their own change")
        except BoardError:
            return None  # refused; the app rolls the request back

        held1 = world.held()
        gone = _beyond(held0, held1)  # what this move took away
        # A provisional swap or a rejection takes rights away tentatively; only a
        # direct change or an approval ends their authority.
        committing = (move[0] == "set" and result["status"] == "applied") or \
            (move[0] == "resolve" and move[3])
        auth1 = _beyond(auth, gone) if committing else auth
        if move[0] == "resolve" and move[3]:
            wanted, was = _rights(row["after_json"]), _rights(row["before_json"])
            # An approval vouches for everything the pending request wants (the
            # grant becomes exactly that); a ratification vouches for what it added.
            auth1 = _union(auth1, wanted if row["status"] == "pending" else _beyond(wanted, was))
        if move[0] == "set" and result["status"] == "pending" and held1 != held0:
            problems.append("a pending request changed the grant")

        rows1 = world.rows()
        claimed = NONE
        for r in rows1:
            if r["status"] == "provisional":
                claimed = _union(claimed, _beyond(_rights(r["after_json"]),
                                                  _rights(r["before_json"])))
        unexplained = _beyond(held1, _union(auth1, claimed))
        for i, name in enumerate(("read", "post")):
            if unexplained[i]:
                problems.append(f"{name} is held with no approval and no open change "
                                "to ratify it")
        key = world.key(auth1, rows1)
        blob = None
        if is_new(key):
            conn.execute("COMMIT")  # a copy taken mid-transaction would miss the change
            blob = conn.serialize()
            conn.deserialize(parent)
        return problems, auth1, key, blob
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")


def explore(tmp_path, seed, depth=DEPTH, max_open=MAX_OPEN):
    """Start from a fresh board whose agent holds `seed`."""
    return explore_from(_start(tmp_path, seed), seed, depth=depth, max_open=max_open)


def explore_from(start, auth, principal="p", depth=DEPTH, max_open=MAX_OPEN):
    """Breadth first from the database `start` (a serialized copy), in which the
    rights in `auth` are the ones already authorized. Breadth first, so the first
    problems found have the shortest paths, and exploration stops at the end of
    the level that finds any. Returns ({problem: [paths]}, states seen, moves tried)."""
    first = World(start, principal)
    level = [(start, auth, ())]
    seen = {first.key(auth, first.rows())}
    found = {}
    tried = 0

    def is_new(key):
        return key not in seen and len(key[1]) <= max_open

    for _ in range(depth):
        following = []
        for blob, auth_now, path in level:
            world = World(blob, principal)
            rows = world.rows()
            for move in world.moves(rows):
                result = try_move(world, blob, rows, auth_now, move, is_new)
                if result is None:
                    continue
                problems, auth1, key, blob1 = result
                tried += 1
                for problem in problems:
                    found.setdefault(problem, []).append(path + (move,))
                if not problems and blob1 is not None:
                    seen.add(key)
                    following.append((blob1, auth1, path + (move,)))
            world.conn.close()
        if found or not following:
            break
        level = following
    return found, len(seen), tried


def report(found, states, tried):
    lines = [f"{problem} ({len(paths)} paths); shortest: {describe(min(paths, key=len))}"
             for problem, paths in found.items()]
    return f"{states} states, {tried} moves explored\n" + "\n".join(lines)


@pytest.mark.parametrize("seed", [NONE, (True, False)],
                         ids=["starts-with-nothing", "starts-with-read"])
def test_no_right_is_ever_held_without_a_reason(tmp_path, seed):
    found, states, tried = explore(tmp_path, seed)
    if found:
        pytest.fail(report(found, states, tried), pytrace=False)

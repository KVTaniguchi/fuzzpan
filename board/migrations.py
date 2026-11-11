"""Versioned schema migrations. The version lives in PRAGMA user_version.

Append new migrations to MIGRATIONS; never edit one that has shipped.
"""

import json
import os
import sqlite3
import stat

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS principals (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('agent', 'human')),
    owner        TEXT NOT NULL,
    display_name TEXT NOT NULL,
    public_key   BLOB,
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admins (
    name          TEXT PRIMARY KEY REFERENCES principals(id),
    pass_hash     TEXT,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    admin      TEXT NOT NULL REFERENCES admins(name),
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lanes (
    id         INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    scope      TEXT NOT NULL,
    notice     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    lane_id      INTEGER NOT NULL REFERENCES lanes(id),
    principal_id TEXT NOT NULL REFERENCES principals(id),
    can_read     INTEGER NOT NULL,
    can_post     INTEGER NOT NULL,
    PRIMARY KEY (lane_id, principal_id)
);
CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lane_id     INTEGER NOT NULL REFERENCES lanes(id),
    author_id   TEXT NOT NULL REFERENCES principals(id),
    kind        TEXT NOT NULL,
    body        TEXT NOT NULL,
    received_at TEXT NOT NULL,
    client_ts   INTEGER,
    nonce       TEXT,
    signature   TEXT,
    raw         TEXT
);
CREATE TABLE IF NOT EXISTS pending (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lane_id      INTEGER NOT NULL REFERENCES lanes(id),
    principal_id TEXT NOT NULL REFERENCES principals(id),
    before_json  TEXT NOT NULL,
    after_json   TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status       TEXT NOT NULL
        CHECK (status IN ('pending', 'provisional', 'approved', 'ratified', 'rejected')),
    created_at   TEXT NOT NULL,
    resolved_by  TEXT,
    resolved_at  TEXT
);
CREATE TABLE IF NOT EXISTS seen_nonces (
    principal_id TEXT NOT NULL,
    nonce        TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    PRIMARY KEY (principal_id, nonce)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    scope  TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def _migration_1(conn):
    """The base schema. IF NOT EXISTS lets fresh and old databases share a path;
    an old database keeps whatever CHECKs it had, and migration 2 rebuilds them away."""
    for statement in SCHEMA_V1.split(";"):
        if statement.strip():
            conn.execute(statement)


def _migration_2(conn):
    """Rebuild principals and lanes without any CHECK on owner or scope, whatever
    CHECKs an older database gave them.

    SQLite's documented rebuild: create the new table, copy, drop the old, rename.
    """
    conn.execute("""
        CREATE TABLE principals_new (
            id           TEXT PRIMARY KEY,
            kind         TEXT NOT NULL CHECK (kind IN ('agent', 'human')),
            owner        TEXT NOT NULL,
            display_name TEXT NOT NULL,
            public_key   BLOB,
            status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
            created_at   TEXT NOT NULL
        )""")
    conn.execute("INSERT INTO principals_new SELECT id, kind, owner, display_name, "
                 "public_key, status, created_at FROM principals")
    conn.execute("DROP TABLE principals")
    conn.execute("ALTER TABLE principals_new RENAME TO principals")
    conn.execute("""
        CREATE TABLE lanes_new (
            id         INTEGER PRIMARY KEY,
            slug       TEXT NOT NULL UNIQUE,
            name       TEXT NOT NULL,
            scope      TEXT NOT NULL,
            notice     TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    conn.execute("INSERT INTO lanes_new SELECT id, slug, name, scope, notice, created_at FROM lanes")
    conn.execute("DROP TABLE lanes")
    conn.execute("ALTER TABLE lanes_new RENAME TO lanes")


def _migration_3(conn):
    """Accounts with a password and TOTP. Old passphrases stop working: pass_hash
    is cleared, and every session is deleted, so each admin re-enrolls through a
    setup link (python -m board.admin_cli reset-admin NAME)."""
    for column in ("totp_secret TEXT", "totp_last_step INTEGER NOT NULL DEFAULT 0",
                   "failed_logins INTEGER NOT NULL DEFAULT 0", "locked_until REAL",
                   "setup_hash TEXT", "setup_expires_at REAL"):
        conn.execute(f"ALTER TABLE admins ADD COLUMN {column}")
    conn.execute("UPDATE admins SET pass_hash = NULL")
    conn.execute("DELETE FROM sessions")


def _migration_4(conn):
    """Public-internet hardening: per-key attempt counters, and session tokens
    stored hashed. Sessions held in the clear are deleted; everyone logs in again."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            key          TEXT PRIMARY KEY,
            window_start REAL NOT NULL,
            count        INTEGER NOT NULL
        )""")
    conn.execute("DELETE FROM sessions")


def _migration_5(conn):
    """Agent bearer tokens replace request signing. Tokens are stored as SHA-256
    hashes. Public keys, the nonce table and the per-post signature columns go.

    Existing agents keep their id, owner and grants but have no token (token_hash
    is NULL, which never matches), so each must be reconnected with a new token.
    Posts keep their id, author, kind, body and time."""
    conn.execute("ALTER TABLE principals ADD COLUMN token_hash TEXT")
    conn.execute("ALTER TABLE principals ADD COLUMN last_seen_at REAL")
    conn.execute("CREATE UNIQUE INDEX principals_token_hash ON principals(token_hash)")
    conn.execute("ALTER TABLE principals DROP COLUMN public_key")
    conn.execute("DROP TABLE seen_nonces")
    for column in ("client_ts", "nonce", "signature", "raw"):
        conn.execute(f"ALTER TABLE posts DROP COLUMN {column}")


def _migration_6(conn):
    """Single-use invite links. Only the SHA-256 of a link's code is stored.
    owner is the inviting admin; replaces_token marks a reconnect invite, whose
    redemption issues a new token for an existing agent and changes no lanes."""
    conn.execute("""
        CREATE TABLE invites (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            code_hash      TEXT NOT NULL UNIQUE,
            agent_id       TEXT NOT NULL,
            display_name   TEXT NOT NULL,
            owner          TEXT NOT NULL,
            lanes_json     TEXT NOT NULL,
            replaces_token INTEGER NOT NULL DEFAULT 0,
            status         TEXT NOT NULL DEFAULT 'open'
                           CHECK (status IN ('open', 'redeemed', 'cancelled')),
            created_at     TEXT NOT NULL,
            expires_at     REAL NOT NULL,
            redeemed_at    TEXT,
            redeemed_ip    TEXT
        )""")


def _migration_7(conn):
    """Open shared-lane changes can be superseded, and a decision that takes a
    right away is struck from every open change. pending's status CHECK has to
    change, which SQLite only allows by rebuilding the table; upgrade_reason marks
    a row the upgrade raised itself. Then rows left open by the previous version
    are brought in line with DESIGN.md section 6 from the audit log (see
    _settle_legacy_changes)."""
    conn.execute("""
        CREATE TABLE pending_new (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            lane_id      INTEGER NOT NULL REFERENCES lanes(id),
            principal_id TEXT NOT NULL REFERENCES principals(id),
            before_json  TEXT NOT NULL,
            after_json   TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            status       TEXT NOT NULL CHECK (status IN ('pending', 'provisional', 'approved',
                                                         'ratified', 'rejected', 'superseded')),
            created_at   TEXT NOT NULL,
            resolved_by  TEXT,
            resolved_at  TEXT,
            upgrade_reason TEXT CHECK (upgrade_reason IN ('unexplained'))
        )""")
    conn.execute("INSERT INTO pending_new (id, lane_id, principal_id, before_json, after_json, "
                 "requested_by, status, created_at, resolved_by, resolved_at) SELECT id, "
                 "lane_id, principal_id, before_json, after_json, requested_by, status, "
                 "created_at, resolved_by, resolved_at FROM pending")
    conn.execute("DROP TABLE pending")
    conn.execute("ALTER TABLE pending_new RENAME TO pending")
    _settle_legacy_changes(conn)


NO_RIGHTS = (False, False)


def _legacy_rights(value):
    """(read, post) from a decoded JSON value, or None if it isn't one."""
    return tuple(bool(v) for v in value) if isinstance(value, list) and len(value) == 2 else None


def _legacy_json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _within(wanted, allowed):
    return tuple(w and a for w, a in zip(wanted, allowed))


def _either(one, other):
    return tuple(a or b for a, b in zip(one, other))


def _minus(rights, other):
    return tuple(r and not o for r, o in zip(rights, other))


def _beyond(wanted, allowed):
    return any(w and not a for w, a in zip(wanted, allowed))


def _blessed(history, held, rows):
    """Replay one agent's history on one shared lane the way the previous version
    ran it. Returns (blessed, taker, giver):

    - blessed: the rights an admin blessed: held before anyone acted, or approved
      or ratified by an admin other than the requester, and not taken away since.
      A rejection blesses nothing (the previous version's rejection put back what
      the agent held when the change was asked, which may have been taken away
      since). Anything the log can't account for is not blessed: a history that
      doesn't add up blesses nothing, and a right that changed with nothing logged
      loses its blessing.
    - taker[i]: the admin whose logged action last took right i away or rejected
      a change asking for it, and giver[i]: (admin, action) of the logged action
      that last gave it since, or None where the log doesn't say (never, or a
      history that doesn't add up)."""
    unknown = (NO_RIGHTS, [None, None], [None, None])
    made = {pid for _, action, pid, *_ in history if action in ("grant-pending", "grant-provisional")}
    if any(pid not in made for pid in rows):
        return unknown  # a row, open or closed, with no logged history of its own
    if not history:
        return held, [None, None], [None, None]  # nothing logged ever changed it

    grant = blessed = None
    taker, giver = [None, None], [None, None]
    sim = {}

    def moved(old, new, actor, action):
        for i in (0, 1):
            if old[i] and not new[i]:
                taker[i], giver[i] = actor, None
            elif new[i] and not old[i]:
                giver[i] = (actor, action)

    for _, action, pid, before, after, actor in history:
        if action in ("grant", "grant-pending", "grant-provisional"):
            if grant is None:
                grant = blessed = before
            elif before != grant:  # changed with nothing logged
                for i in (0, 1):
                    if before[i] != grant[i]:
                        taker[i] = giver[i] = None
                blessed = tuple(b and g == x for b, g, x in zip(blessed, grant, before))
                grant = before
            if action == "grant":  # direct: a narrowing, or a sole admin's change
                moved(grant, after, actor, action)
                blessed, grant = _either(_within(blessed, after), _minus(after, before)), after
            else:
                sim[pid] = [before, after, action[len("grant-"):]]
                if action == "grant-provisional":
                    moved(grant, after, actor, action)
                    grant = after
            continue
        row = sim.get(pid)
        wanted = {"approved": "pending", "ratified": "provisional"}.get(action)
        if grant is None or row is None or row[2] not in ("pending", "provisional") or \
                (wanted and row[2] != wanted):
            return unknown
        if action == "approved":
            moved(grant, row[1], actor, action)
            blessed = _either(_minus(blessed, _minus(grant, row[1])), row[1])
            grant = row[1]
        elif action == "ratified":
            blessed = _either(blessed, row[1])
        else:  # a rejection says no to what the change asked for, in effect or not
            for i in (0, 1):
                if row[1][i] and not row[0][i]:
                    taker[i], giver[i] = actor, None
            if row[2] == "provisional":
                moved(grant, row[0], actor, action)
                grant = row[0]
        row[2] = action
    for pid, row in rows.items():
        if (sim[pid][2] in ("pending", "provisional")) != (row["status"] in ("pending", "provisional")):
            return unknown
    for i in (0, 1):
        if grant[i] != held[i]:  # changed after the last entry, with nothing logged
            taker[i] = giver[i] = None
    return tuple(b and g == h for b, g, h in zip(blessed, grant, held)), taker, giver


def _requester_for(i, taker, giver, asked, owner, admins):
    """Who a new row claiming right i is listed under: a real admin, and never the
    admin who last took the right away (taker), so that admin decides. Preferred:
    the admin whose action gave it back (in practice an old rejection), then the
    last admin who asked for it, then the agent's owner, then any other admin."""
    for name in (giver[i][0] if giver[i] else None, asked, owner, *admins):
        if name in admins and name != taker[i]:
            return name
    # A sole admin who took it away: nobody else can decide, so nobody can keep it.
    return taker[i] if taker[i] in admins else (admins[0] if admins else owner)


def _settle_legacy_changes(conn):
    """Bring the open changes the previous version left in line with DESIGN.md
    section 6, from the audit log. It never widens a grant or a change, and never
    reopens or rewrites a closed one. For each agent on each shared lane:

    1. An open change whose requester is not an admin is closed: no admin asked
       for it, so nobody may decide it. Each other open change is cut back by
       every direct narrowing logged after it was made, and cannot restore a
       right a logged approval dropped. One with no audit entry of its own keeps
       no right to restore; one whose rights can't be read is left as it is (it
       holds nothing to give back).
    2. A provisional change may only give back, if rejected, a right an admin
       blessed (_blessed) or one an earlier open provisional change still claims.
       So rejecting it never restores a right the log shows was rejected or taken.
    3. A change left asking for nothing new is closed (superseded).
    4. A held right that is neither blessed nor claimed by an open provisional
       change is never taken away here, and never left unexplained. The admin who
       last took it away decides it: the latest provisional change that asked
       for it is kept open claiming it, unless that admin asked for it; otherwise
       a new provisional change claims it, listed under another admin
       (_requester_for) and marked with upgrade_reason. New changes are chained
       like the live code's: each starts from what the one before it gives, and
       the last gives what is held.
    Every closed, cut back or new change is logged."""
    shared = {slug: lane_id for lane_id, slug in
              conn.execute("SELECT id, slug FROM lanes WHERE scope = 'shared'")}
    slugs = {lane_id: slug for slug, lane_id in shared.items()}
    admins = sorted(r[0] for r in conn.execute("SELECT name FROM admins"))
    owners = dict(conn.execute("SELECT id, owner FROM principals"))
    rows = {}  # (slug, principal) -> {pid: row}
    for pid, lane_id, principal, before, after, requested_by, status in conn.execute(
            "SELECT id, lane_id, principal_id, before_json, after_json, requested_by, status "
            "FROM pending ORDER BY id"):
        if lane_id in slugs:
            rows.setdefault((slugs[lane_id], principal), {})[pid] = {
                "before": _legacy_rights(_legacy_json(before)),
                "after": _legacy_rights(_legacy_json(after)),
                "requested_by": requested_by, "status": status}
    pair_of = {pid: pair for pair, group in rows.items() for pid in group}

    # An entry that could concern a shared lane but can't be read, or names a row
    # or lane that isn't there, could apply to any agent: then nothing is blessed.
    history, created, broken, broken_all = {}, {}, set(), False
    for audit_id, actor, action, scope, detail in conn.execute(
            "SELECT id, actor, action, scope, detail FROM audit_log WHERE action IN ('grant', "
            "'grant-pending', 'grant-provisional', 'approved', 'ratified', 'rejected') ORDER BY id"):
        relevant = scope == "shared" or action in ("approved", "ratified", "rejected")
        detail = _legacy_json(detail)
        if not isinstance(detail, dict):
            broken_all = broken_all or relevant
            continue
        pid = detail.get("pending_id")
        if action.startswith("grant"):
            lane, principal = detail.get("lane"), detail.get("principal")
            if not (isinstance(lane, str) and isinstance(principal, str)):
                broken_all = broken_all or relevant
                continue
            pair = (lane, principal)
            if lane not in shared:
                continue
            before, after = _legacy_rights(detail.get("before")), _legacy_rights(detail.get("after"))
            if before is None or after is None or \
                    (action != "grant" and (not isinstance(pid, int) or pair_of.get(pid) != pair)):
                broken.add(pair)
                continue
            if action != "grant":
                created[pid] = audit_id
        elif isinstance(pid, int) and pid in pair_of:
            pair, before, after = pair_of[pid], None, None
        else:
            broken_all = True
            continue
        history.setdefault(pair, []).append((audit_id, action, pid, before, after, actor))

    held = {}
    for lane_id, principal, can_read, can_post in conn.execute(
            "SELECT lane_id, principal_id, can_read, can_post FROM grants"):
        if lane_id in slugs:
            held[(slugs[lane_id], principal)] = (bool(can_read), bool(can_post))

    now = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')").fetchone()[0]
    for pair in sorted(set(rows) | set(held)):
        group, events = rows.get(pair, {}), history.get(pair, [])
        grant = held.get(pair, NO_RIGHTS)
        if broken_all or pair in broken:
            blessed, taker, giver = NO_RIGHTS, [None, None], [None, None]
        else:
            blessed, taker, giver = _blessed(events, grant, group)

        # 1. Close what no admin asked for; cut back by what was logged after the rest.
        settled, closing = {}, set()
        for pid, row in group.items():
            before, after = row["before"], row["after"]
            if row["status"] not in ("pending", "provisional"):
                continue
            if row["requested_by"] not in admins:
                closing.add(pid)
                continue
            if before is None or after is None:
                continue
            if pid not in created:
                before = _within(before, after)  # no history: nothing to give back
            for audit_id, action, other, b, a, _ in events:
                if pid not in created or audit_id < created[pid]:
                    continue
                if action == "grant" and not _beyond(a, b):
                    before, after = _within(before, a), _within(after, a)
                elif action == "approved" and group[other]["status"] == "approved" and \
                        group[other]["after"] is not None:
                    before = _within(before, group[other]["after"])
            settled[pid] = [before, after]

        # 2 and 3. Give back only what is blessed or claimed by an earlier change.
        claimed = NO_RIGHTS
        for pid in sorted(settled):
            before, after = settled[pid]
            if group[pid]["status"] == "provisional":
                before = settled[pid][0] = _within(before, _either(blessed, claimed))
                if _beyond(after, before):
                    claimed = _either(claimed, _minus(after, before))
            if not _beyond(after, before):
                closing.add(pid)

        # 4. No held right is left unexplained, and the admin who took it decides.
        loose = _minus(_minus(grant, blessed), claimed)
        new_rows = {}  # requester -> rights
        removed = NO_RIGHTS
        for i in (0, 1):
            if not loose[i]:
                continue
            right = tuple(j == i for j in (0, 1))
            if giver[i] and giver[i][1] == "rejected":
                # An old rejection put this right back after it was taken away; no
                # admin approved it since. It is removed, not left to be decided.
                removed = _either(removed, right)
                continue
            keep = [pid for pid in settled if group[pid]["status"] == "provisional"
                    and group[pid]["after"][i] and group[pid]["requested_by"] != taker[i]]
            if keep:
                pid = max(keep)
                settled[pid] = [_minus(settled[pid][0], right), _either(settled[pid][1], right)]
                closing.discard(pid)
                continue
            asked = [pid for pid, row in group.items() if row["before"] is not None and
                     row["after"] is not None and row["after"][i] and not row["before"][i]]
            requester = _requester_for(i, taker, giver, group[max(asked)]["requested_by"]
                                       if asked else None, owners.get(pair[1]), admins)
            new_rows[requester] = _either(new_rows.get(requester, NO_RIGHTS), right)

        if any(removed):
            for pid in settled:
                settled[pid] = [_minus(settled[pid][0], removed), _minus(settled[pid][1], removed)]
                if not _beyond(settled[pid][1], settled[pid][0]):
                    closing.add(pid)
            grant_after = _minus(grant, removed)
            conn.execute("DELETE FROM grants WHERE lane_id = ? AND principal_id = ?",
                         (shared[pair[0]], pair[1]))
            if any(grant_after):
                conn.execute("INSERT INTO grants (lane_id, principal_id, can_read, can_post) "
                             "VALUES (?, ?, ?, ?)", (shared[pair[0]], pair[1], int(grant_after[0]),
                                                     int(grant_after[1])))
            _upgrade_audit(conn, now, "removed", lane=pair[0], principal=pair[1],
                           before=grant, after=grant_after, reason="rejected")
            grant = grant_after

        closed = sorted(closing)
        cut = sorted(pid for pid in settled if pid not in closing and
                     tuple(settled[pid]) != (group[pid]["before"], group[pid]["after"]))
        slug, principal = pair
        if closed:
            conn.executemany("UPDATE pending SET status = 'superseded', resolved_by = 'upgrade', "
                             "resolved_at = ? WHERE id = ?", [(now, pid) for pid in closed])
        for pid in cut:
            conn.execute("UPDATE pending SET before_json = ?, after_json = ? WHERE id = ?",
                         (json.dumps(settled[pid][0]), json.dumps(settled[pid][1]), pid))
        for ids, action in ((closed, "superseded"), (cut, "narrowed")):
            if ids:
                _upgrade_audit(conn, now, action, lane=slug, principal=principal, pending_ids=ids)
        start = grant
        for rights in new_rows.values():
            start = _minus(start, rights)
        for requester, rights in sorted(new_rows.items()):
            before, after = start, _either(start, rights)
            cur = conn.execute(
                "INSERT INTO pending (lane_id, principal_id, before_json, after_json, requested_by, "
                "status, created_at, upgrade_reason) VALUES (?, ?, ?, ?, ?, 'provisional', ?, ?)",
                (shared[slug], principal, json.dumps(before), json.dumps(after), requester, now,
                 "unexplained"))
            _upgrade_audit(conn, now, "grant-provisional", pending_id=cur.lastrowid, lane=slug,
                           principal=principal, before=before, after=after, requested_by=requester,
                           reason="unexplained")
            start = after


def _upgrade_audit(conn, now, action, **detail):
    conn.execute("INSERT INTO audit_log (ts, actor, action, scope, detail) "
                 "VALUES (?, 'upgrade', ?, 'shared', ?)", (now, action, json.dumps(detail)))


def _migration_8(conn):
    """Login failures are counted per (username, client address), so an attacker
    elsewhere can't lock an admin out. There is no lock on the username itself:
    admins.failed_logins and admins.locked_until, which were one, are dropped."""
    conn.execute("""
        CREATE TABLE login_pairs (
            username        TEXT NOT NULL,
            client          TEXT NOT NULL,
            failures        INTEGER NOT NULL,
            locked_until    REAL,
            last_failure_at REAL NOT NULL,
            PRIMARY KEY (username, client)
        )""")
    conn.execute("ALTER TABLE admins DROP COLUMN failed_logins")
    conn.execute("ALTER TABLE admins DROP COLUMN locked_until")


def _migration_9(conn):
    """Sensitive lanes: money and health take short summaries and refuse
    account-like numbers (service._check_sensitive). Existing lanes named
    *-money, and health, are flagged; an admin can change any lane with
    admin_cli set-sensitive. Safe to run on a table that already has the column."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(lanes)")}
    if "sensitive" not in columns:
        conn.execute("ALTER TABLE lanes ADD COLUMN sensitive INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE lanes SET sensitive = 1 WHERE slug LIKE '%-money' OR slug = 'health'")


def _migration_10(conn):
    """Retention: lanes.retention_days is how long a lane keeps posts (NULL: forever).
    `admin_cli prune` deletes older ones. Existing *-money lanes get 396 days
    (13 months); health and the rest keep forever until an admin sets otherwise.
    Safe to run on a table that already has the column."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(lanes)")}
    if "retention_days" not in columns:
        conn.execute("ALTER TABLE lanes ADD COLUMN retention_days INTEGER")
    conn.execute("UPDATE lanes SET retention_days = 396 WHERE slug LIKE '%-money' "
                 "AND retention_days IS NULL")


def _migration_11(conn):
    """OAuth sign-in for remote MCP clients (Claude chat). A code is made when an admin
    approves a connection and spent at the token endpoint, which is where the agent's
    bearer token is made, so no token is ever stored. Only the code's hash is kept."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_codes (
            code_hash    TEXT PRIMARY KEY,
            owner        TEXT NOT NULL,
            agent_id     TEXT NOT NULL,
            display_name TEXT NOT NULL,
            challenge    TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            expires_at   REAL NOT NULL
        )""")


def _migration_12(conn):
    """One token per OAuth connection (decisions/0005). A sign-in adds a row here
    instead of replacing the agent's token, so connections don't knock each other off.
    Only the token's hash is kept."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_connections (
            token_hash   TEXT PRIMARY KEY,
            agent_id     TEXT NOT NULL REFERENCES principals(id),
            created_at   REAL NOT NULL,
            last_seen_at REAL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS agent_connections_agent "
                 "ON agent_connections (agent_id)")


def _migration_13(conn):
    """Lanes chosen on the sign-in page (decisions/0006). After the admin proves who
    they are, an `oauth_approvals` row holds the validated request for five minutes
    while they tick lanes; only the ticket's hash is kept. The code then carries the
    chosen lanes, which apply when the code is spent, as an invite's lanes do."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(oauth_codes)")}
    if "lanes_json" not in columns:
        conn.execute("ALTER TABLE oauth_codes ADD COLUMN lanes_json TEXT NOT NULL "
                     "DEFAULT '{}'")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_approvals (
            ticket_hash  TEXT PRIMARY KEY,
            owner        TEXT NOT NULL,
            agent_id     TEXT NOT NULL,
            params_json  TEXT NOT NULL,
            expires_at   REAL NOT NULL
        )""")


def _migration_14(conn):
    """An admin can move their authenticator to a new device from inside the board
    (§5.3). The new secret waits on the session that asked for it, for ten minutes,
    until a code from it confirms the move."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "totp_pending" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN totp_pending TEXT")
    if "totp_pending_expires_at" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN totp_pending_expires_at REAL")


MIGRATIONS = [_migration_1, _migration_2, _migration_3, _migration_4, _migration_5,
              _migration_6, _migration_7, _migration_8, _migration_9, _migration_10,
              _migration_11, _migration_12, _migration_13, _migration_14]


class DatabaseTooNew(Exception):
    """The database was written by a newer version of the board than this code."""


def too_new_message(db_path, version):
    return (f"{db_path} is at schema version {version}, but this code only knows "
            f"{len(MIGRATIONS)}: it was written by a newer version of the board. "
            "Upgrade the board, or restore a backup made by this version. Nothing was changed.")


def _make_private(db_path):
    """The file holds TOTP secrets and token hashes, so only its owner may read it,
    from the moment it exists. SQLite gives its journal the same mode."""
    try:
        os.close(os.open(db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        try:  # one from before this rule; on a file we don't own, leave it be
            if stat.S_IMODE(os.stat(db_path).st_mode) & 0o077:
                os.chmod(db_path, 0o600)
        except OSError:
            pass


def migrate_db(db_path):
    """Bring the database at db_path to the latest version; return that version.
    Refuses (DatabaseTooNew) one newer than this code, changing nothing.

    Each pending migration and its version bump commit together, so a failure
    leaves the database at the last good version.
    """
    _make_private(db_path)
    # We issue BEGIN/COMMIT ourselves. The long timeout lets a second process
    # starting at the same time wait for the first to finish migrating.
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=60)
    try:
        # Off while tables are dropped and renamed; checked before each commit.
        conn.execute("PRAGMA foreign_keys = OFF")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise DatabaseTooNew(too_new_message(db_path, version))
        for number, migration in enumerate(MIGRATIONS, start=1):
            if number <= version:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Another process may have migrated while we waited for the lock.
                if conn.execute("PRAGMA user_version").fetchone()[0] >= number:
                    conn.execute("ROLLBACK")
                    version = number
                    continue
                migration(conn)
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise sqlite3.IntegrityError(
                        f"migration {number} left {len(violations)} foreign key violations")
                conn.execute(f"PRAGMA user_version = {number}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            version = number
        return version
    finally:
        conn.close()

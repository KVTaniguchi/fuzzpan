"""Versioned schema migrations for an existing board database."""

import sqlite3
import threading

import pytest

from board.db import connect
from board.migrations import MIGRATIONS, migrate_db

V0_SCHEMA = """
CREATE TABLE IF NOT EXISTS principals (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('agent', 'human')),
    owner        TEXT NOT NULL CHECK (owner IN ('legacy-a', 'legacy-b')),
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
    scope      TEXT NOT NULL CHECK (scope IN ('legacy-a', 'legacy-b', 'shared')),
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


def make_v0(tmp_path):
    """A database as the pre-migration code left it (owner and scope CHECKed to
    two fixed names), with data in it."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(V0_SCHEMA)
    for name in ("legacy-a", "legacy-b"):
        conn.execute("INSERT INTO principals (id, kind, owner, display_name, created_at) "
                     "VALUES (?, 'human', ?, ?, 'now')", (name, name, name.title()))
        conn.execute("INSERT INTO admins (name) VALUES (?)", (name,))
    conn.execute("INSERT INTO principals (id, kind, owner, display_name, status, created_at) "
                 "VALUES ('old-agent', 'agent', 'legacy-a', 'Old', 'revoked', 'now')")
    conn.execute("UPDATE principals SET public_key = ? WHERE id = 'old-agent'", (b"\x01" * 32,))
    conn.execute("INSERT INTO seen_nonces VALUES ('old-agent', 'n1', 1)")
    conn.execute("INSERT INTO lanes (id, slug, name, scope, notice, created_at) "
                 "VALUES (1, 'household', 'Household', 'shared', 'n', 'now')")
    conn.execute("INSERT INTO grants VALUES (1, 'old-agent', 1, 0)")
    conn.execute("INSERT INTO posts (lane_id, author_id, kind, body, received_at, client_ts, "
                 "nonce, signature, raw) VALUES (1, 'old-agent', 'note', 'hello', 'now', 1, "
                 "'n1', 'sig', '{}')")
    conn.execute("INSERT INTO pending (lane_id, principal_id, before_json, after_json, "
                 "requested_by, status, created_at) "
                 "VALUES (1, 'old-agent', '{}', '{}', 'legacy-a', 'provisional', 'now')")
    conn.commit()
    conn.close()
    return path


def user_version(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def table_missing(db, name):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT count(*) FROM sqlite_master WHERE name=?",
                            (name,)).fetchone()[0] == 0
    finally:
        conn.close()


def broken_migration(conn):
    conn.execute("CREATE TABLE created_by_broken_migration (x)")
    conn.execute("SELECT * FROM no_such_table")


def test_fresh_database_reaches_latest_version(tmp_path):
    assert migrate_db(tmp_path / "b.db") == len(MIGRATIONS)


def test_v0_database_keeps_its_data(tmp_path):
    db = make_v0(tmp_path)
    migrate_db(db)
    with connect(db) as c:
        assert c.execute("SELECT status FROM principals WHERE id='old-agent'").fetchone()[0] == "revoked"
        assert c.execute("SELECT count(*) FROM principals WHERE kind='human'").fetchone()[0] == 2
        post = c.execute("SELECT author_id, body, kind, received_at FROM posts").fetchall()
        assert [tuple(r) for r in post] == [("old-agent", "hello", "note", "now")]
        assert c.execute("SELECT status FROM pending").fetchone()[0] == "provisional"
        assert c.execute("SELECT can_read FROM grants").fetchone()[0] == 1
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_signing_columns_and_nonce_table_are_gone_after_upgrade(tmp_path):
    db = make_v0(tmp_path)
    migrate_db(db)
    with connect(db) as c:
        post_cols = {r["name"] for r in c.execute("PRAGMA table_info(posts)")}
        principal_cols = {r["name"] for r in c.execute("PRAGMA table_info(principals)")}
    assert post_cols.isdisjoint({"client_ts", "nonce", "signature", "raw"})
    assert "public_key" not in principal_cols
    assert {"token_hash", "last_seen_at"} <= principal_cols
    assert table_missing(db, "seen_nonces")


def test_owner_and_scope_accept_any_admin_name(tmp_path):
    db = tmp_path / "b.db"
    migrate_db(db)
    with connect(db) as c:
        c.execute("INSERT INTO principals (id, kind, owner, display_name, created_at) "
                  "VALUES ('alice', 'human', 'alice', 'Alice', 'now')")
        c.execute("INSERT INTO lanes (slug, name, scope, notice, created_at) "
                  "VALUES ('alice-money', 'Alice money', 'alice', 'n', 'now')")


def test_legacy_checks_are_gone_after_upgrade(tmp_path):
    db = make_v0(tmp_path)
    migrate_db(db)
    with connect(db) as c:
        c.execute("INSERT INTO principals (id, kind, owner, display_name, created_at) "
                  "VALUES ('alice', 'human', 'alice', 'Alice', 'now')")
        c.execute("INSERT INTO lanes (slug, name, scope, notice, created_at) "
                  "VALUES ('alice-money', 'Alice money', 'alice', 'n', 'now')")


def test_kind_and_status_checks_survive_the_rebuild(tmp_path):
    db = make_v0(tmp_path)
    migrate_db(db)
    with pytest.raises(sqlite3.IntegrityError):
        with connect(db) as c:
            c.execute("INSERT INTO principals (id, kind, owner, display_name, created_at) "
                      "VALUES ('x', 'robot', 'legacy-a', 'X', 'now')")


def test_failed_upgrade_leaves_the_original_database_unchanged(tmp_path, monkeypatch):
    db = make_v0(tmp_path)

    def rebuild_then_fail(conn):
        MIGRATIONS[1](conn)  # the real rebuild, then die before it can commit
        conn.execute("SELECT * FROM no_such_table")

    monkeypatch.setattr("board.migrations.MIGRATIONS", [MIGRATIONS[0], rebuild_then_fail])
    with pytest.raises(sqlite3.OperationalError):
        migrate_db(db)
    assert user_version(db) == 1  # stopped at the last completed migration
    assert table_missing(db, "principals_new") and table_missing(db, "lanes_new")
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM principals").fetchone()[0] == 3
        assert c.execute("SELECT count(*) FROM posts").fetchone()[0] == 1
        assert c.execute("SELECT status FROM pending").fetchone()[0] == "provisional"
        # the original CHECK on owner is still in force
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO principals (id, kind, owner, display_name, created_at) "
                      "VALUES ('alice', 'human', 'alice', 'Alice', 'now')")


def test_failed_migration_changes_nothing(tmp_path, monkeypatch):
    db = make_v0(tmp_path)
    monkeypatch.setattr("board.migrations.MIGRATIONS", [*MIGRATIONS, broken_migration])
    with pytest.raises(sqlite3.OperationalError):
        migrate_db(db)
    assert user_version(db) == len(MIGRATIONS)
    assert table_missing(db, "created_by_broken_migration")


def test_concurrent_migrations_run_each_migration_once(tmp_path, monkeypatch):
    db = make_v0(tmp_path)
    runs = []

    def counting(number, migration):
        def run(conn):
            runs.append(number)
            migration(conn)
        return run

    monkeypatch.setattr("board.migrations.MIGRATIONS",
                        [counting(n, m) for n, m in enumerate(MIGRATIONS, start=1)])
    barrier = threading.Barrier(2)
    results = []

    def go():
        barrier.wait()
        results.append(migrate_db(db))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [len(MIGRATIONS)] * 2
    assert sorted(runs) == list(range(1, len(MIGRATIONS) + 1))


def test_migrating_twice_is_a_no_op(tmp_path):
    db = tmp_path / "b.db"
    assert migrate_db(db) == migrate_db(db)


def test_login_pair_table_arrives_and_the_old_username_lock_goes(tmp_path, monkeypatch):
    db = tmp_path / "v7.db"
    with monkeypatch.context() as m:
        m.setattr("board.migrations.MIGRATIONS", MIGRATIONS[:7])
        assert migrate_db(db) == 7
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO principals (id, kind, owner, display_name, created_at) "
                  "VALUES ('alice', 'human', 'alice', 'A', 'then')")
        c.execute("INSERT INTO admins (name, failed_logins, locked_until) VALUES ('alice', 3, 99.5)")
    migrate_db(db)
    with connect(db) as c:
        admin = c.execute("SELECT * FROM admins").fetchone()
        # A lock on the username itself is gone with its counter, so an admin
        # who was locked out when the board was upgraded can log in.
        assert "failed_logins" not in admin.keys() and "locked_until" not in admin.keys()
        assert c.execute("SELECT count(*) FROM login_pairs").fetchone()[0] == 0
        assert c.execute("SELECT name FROM sqlite_master WHERE name = 'login_failures'").fetchone() is None

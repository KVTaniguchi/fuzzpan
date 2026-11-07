"""admin_cli backup / restore: a consistent copy even mid-write, mode 0600, and a
restore that never silently overwrites a board that has content."""

import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from board import admin_cli
from board.db import connect, init_db
from board.provision import provision_family

ROOT = Path(__file__).resolve().parent.parent


def make_board(path, post_body="hello"):
    provision_family(str(path), [("alice", "Alice"), ("bob", "Bob")])
    with connect(str(path)) as conn:
        conn.execute("INSERT INTO principals (id, kind, owner, display_name, created_at) "
                     "VALUES ('bot', 'agent', 'alice', 'Bot', 'now')")
        lane = conn.execute("SELECT id FROM lanes ORDER BY id LIMIT 1").fetchone()["id"]
        conn.execute("INSERT INTO grants VALUES (?, 'bot', 1, 1)", (lane,))
        conn.execute("INSERT INTO posts (lane_id, author_id, kind, body, received_at) "
                     "VALUES (?, 'bot', 'note', ?, 'now')", (lane, post_body))
    return lane


def run_cli(db, *args):
    env = {**os.environ, "BOARD_DB": str(db), "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-m", "board.admin_cli", *args],
                          env=env, capture_output=True, text=True, cwd=ROOT)


def test_backup_restores_posts_and_grants(tmp_path):
    live = tmp_path / "live.db"
    lane = make_board(live, "remember the milk")
    backup = tmp_path / "copy.db"
    r = run_cli(live, "backup", str(backup))
    assert r.returncode == 0, r.stderr
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    fresh = tmp_path / "fresh.db"
    init_db(str(fresh))  # an empty, migrated board: no admins, no posts
    r = run_cli(fresh, "restore", str(backup))
    assert r.returncode == 0, r.stderr
    with connect(str(fresh)) as conn:
        assert conn.execute("SELECT body FROM posts").fetchone()["body"] == "remember the milk"
        assert conn.execute("SELECT can_read, can_post FROM grants WHERE principal_id='bot' "
                            "AND lane_id=?", (lane,)).fetchone()["can_post"] == 1
        assert {r["name"] for r in conn.execute("SELECT name FROM admins")} == {"alice", "bob"}


def test_backup_works_during_an_open_write_transaction(tmp_path):
    # A plain file copy could capture a half-written database
    live = tmp_path / "live.db"
    lane = make_board(live, "committed")
    writer = sqlite3.connect(str(live), isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO posts (lane_id, author_id, kind, body, received_at) "
                   "VALUES (?, 'bot', 'note', 'half-written', 'now')", (lane,))
    backup = tmp_path / "copy.db"
    try:
        r = run_cli(live, "backup", str(backup))  # readers are not blocked in WAL mode
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert r.returncode == 0, r.stderr
    check = sqlite3.connect(str(backup))
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        bodies = [row[0] for row in check.execute("SELECT body FROM posts")]
    finally:
        check.close()
    assert bodies == ["committed"]  # the uncommitted row is not in the copy


def test_backup_refuses_to_overwrite_an_existing_file(tmp_path):
    live = tmp_path / "live.db"
    make_board(live)
    target = tmp_path / "copy.db"
    target.write_text("precious")
    r = run_cli(live, "backup", str(target))
    assert r.returncode != 0
    assert target.read_text() == "precious"


def test_restore_refuses_to_overwrite_without_force(tmp_path):
    source = tmp_path / "source.db"
    make_board(source, "from backup")
    backup = tmp_path / "copy.db"
    assert run_cli(source, "backup", str(backup)).returncode == 0

    live = tmp_path / "live.db"
    make_board(live, "newer content")
    r = run_cli(live, "restore", str(backup))
    assert r.returncode != 0
    assert "--force" in r.stderr
    with connect(str(live)) as conn:
        assert conn.execute("SELECT body FROM posts").fetchone()["body"] == "newer content"


def test_restore_refuses_when_target_has_only_an_admin(tmp_path):
    source = tmp_path / "source.db"
    make_board(source)
    backup = tmp_path / "copy.db"
    assert run_cli(source, "backup", str(backup)).returncode == 0
    live = tmp_path / "live.db"
    provision_family(str(live), [("carol", "Carol")])
    assert run_cli(live, "restore", str(backup)).returncode != 0


def test_restore_force_overwrites_and_keeps_a_safety_copy(tmp_path):
    source = tmp_path / "source.db"
    make_board(source, "from backup")
    backup = tmp_path / "copy.db"
    assert run_cli(source, "backup", str(backup)).returncode == 0
    live = tmp_path / "live.db"
    make_board(live, "about to be replaced")

    r = run_cli(live, "restore", str(backup), "--force")
    assert r.returncode == 0, r.stderr
    with connect(str(live)) as conn:
        assert [p["body"] for p in conn.execute("SELECT body FROM posts")] == ["from backup"]
    safety = [p for p in tmp_path.iterdir() if p.name.startswith("live.db.before-restore-")]
    assert len(safety) == 1
    assert stat.S_IMODE(safety[0].stat().st_mode) == 0o600
    old = sqlite3.connect(str(safety[0]))
    try:
        assert old.execute("SELECT body FROM posts").fetchone()[0] == "about to be replaced"
    finally:
        old.close()


def test_restore_rejects_a_file_that_is_not_a_board_backup(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_text("not a database at all")
    live = tmp_path / "live.db"
    init_db(str(live))
    r = run_cli(live, "restore", str(junk))
    assert r.returncode != 0
    assert "Traceback" not in r.stderr


def test_backup_requires_board_db(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "BOARD_DB"}
    env["PYTHONPATH"] = str(ROOT)
    r = subprocess.run([sys.executable, "-m", "board.admin_cli", "backup", str(tmp_path / "x")],
                       env=env, capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode != 0 and "BOARD_DB" in r.stderr


def test_restore_does_not_fail_open_when_the_target_is_locked(tmp_path):
    source = tmp_path / "source.db"
    make_board(source, "from backup")
    backup = tmp_path / "copy.db"
    assert run_cli(source, "backup", str(backup)).returncode == 0
    live = tmp_path / "live.db"
    make_board(live, "must survive")
    holder = sqlite3.connect(str(live), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        r = run_cli(live, "restore", str(backup))
        r_force = run_cli(live, "restore", str(backup), "--force")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert r.returncode != 0 and r_force.returncode != 0
    assert "Traceback" not in r.stderr + r_force.stderr
    assert not [p for p in tmp_path.iterdir() if ".before-restore-" in p.name]
    with connect(str(live)) as conn:
        assert [p["body"] for p in conn.execute("SELECT body FROM posts")] == ["must survive"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_backup_is_reported_as_a_permission_error(tmp_path):
    source = tmp_path / "source.db"
    make_board(source)
    backup = tmp_path / "copy.db"
    assert run_cli(source, "backup", str(backup)).returncode == 0
    backup.chmod(0)
    live = tmp_path / "live.db"
    init_db(str(live))
    r = run_cli(live, "restore", str(backup))
    assert r.returncode != 0
    assert "permission denied" in r.stderr
    assert "not a SQLite" not in r.stderr


def test_paths_with_hash_and_question_mark_work(tmp_path):
    source = tmp_path / "source.db"
    make_board(source, "odd names")
    backup = tmp_path / "a#b?c.db"
    assert run_cli(source, "backup", str(backup)).returncode == 0
    fresh = tmp_path / "fresh.db"
    init_db(str(fresh))
    r = run_cli(fresh, "restore", str(backup))
    assert r.returncode == 0, r.stderr
    with connect(str(fresh)) as conn:
        assert conn.execute("SELECT body FROM posts").fetchone()["body"] == "odd names"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a#b?c.db", "fresh.db", "source.db"]


def test_holds_content_raises_instead_of_answering_empty_when_locked(tmp_path):
    live = tmp_path / "live.db"
    make_board(live)
    holder = sqlite3.connect(str(live), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.Error):
            admin_cli._holds_content(str(live))
    finally:
        holder.execute("ROLLBACK")
        holder.close()

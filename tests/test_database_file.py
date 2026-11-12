"""The database file itself: private from the moment it exists, refused if it is
newer than this code, and migrated before any host command relies on its schema."""

import os
import sqlite3
import stat
import subprocess
import sys

import pytest

from board import admin_cli
from board.app import create_app
from board.db import init_db
from board.migrations import MIGRATIONS, DatabaseTooNew, migrate_db
from board.provision import provision_family
from tests.conftest import ROOT
from tests.test_migrations import make_v0, user_version

NEWER = len(MIGRATIONS) + 1


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def make_newer(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE from_the_future (x)")
    conn.execute(f"PRAGMA user_version = {NEWER}")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def open_umask():
    old = os.umask(0)  # the worst case: nothing masked
    yield
    os.umask(old)


# --- A newer database is refused --------------------------------------------

def test_a_newer_database_is_refused_with_a_clear_message(tmp_path):
    db = make_newer(tmp_path / "b.db")
    with pytest.raises(DatabaseTooNew) as e:
        migrate_db(db)
    message = str(e.value)
    assert str(db) in message and str(NEWER) in message and str(len(MIGRATIONS)) in message
    assert "newer" in message and "upgrade" in message.lower()


def test_a_refused_database_is_left_exactly_as_it_was(tmp_path):
    db = make_newer(tmp_path / "b.db")
    before = db.read_bytes()
    with pytest.raises(DatabaseTooNew):
        init_db(db)
    assert db.read_bytes() == before and user_version(db) == NEWER


def test_the_app_does_not_start_on_a_newer_database(tmp_path):
    with pytest.raises(DatabaseTooNew):
        create_app(make_newer(tmp_path / "b.db"))


def test_board_main_exits_with_the_message_and_no_traceback(tmp_path):
    db = make_newer(tmp_path / "b.db")
    env = {**os.environ, "BOARD_DB": str(db), "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-c", "import board.main"], cwd=tmp_path, env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "newer" in r.stderr and "Traceback" not in r.stderr


@pytest.mark.parametrize("argv", [["reset-admin", "alice"],
                                  ["provision", "--admin", "alice:Alice"]])
def test_admin_cli_refuses_a_newer_database_cleanly(tmp_path, monkeypatch, argv):
    db = make_newer(tmp_path / "b.db")
    monkeypatch.setenv("BOARD_DB", str(db))
    with pytest.raises(SystemExit) as e:
        admin_cli.main(argv)
    assert "newer" in str(e.value)
    assert user_version(db) == NEWER


@pytest.mark.parametrize("force", [False, True])
def test_restore_refuses_a_newer_backup_before_touching_anything(tmp_path, force):
    live = tmp_path / "live.db"
    provision_family(live, [("alice", "Alice")])
    before = live.read_bytes()
    backup = tmp_path / "from-the-future.db"
    conn = sqlite3.connect(backup)
    for table in ("admins", "posts", "lanes", "grants"):
        conn.execute(f"CREATE TABLE {table} (x)")
    conn.execute(f"PRAGMA user_version = {NEWER}")
    conn.commit()
    conn.close()
    with pytest.raises(admin_cli.BackupError) as e:
        admin_cli.restore_database(str(live), str(backup), force=force)
    assert "newer" in str(e.value)
    assert live.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir() if "before-restore" in p.name] == []


def test_restore_into_a_new_database_refuses_a_newer_backup_too(tmp_path):
    target = tmp_path / "new.db"
    backup = tmp_path / "from-the-future.db"
    conn = sqlite3.connect(backup)
    for table in ("admins", "posts", "lanes", "grants"):
        conn.execute(f"CREATE TABLE {table} (x)")
    conn.execute(f"PRAGMA user_version = {NEWER}")
    conn.commit()
    conn.close()
    with pytest.raises(admin_cli.BackupError):
        admin_cli.restore_database(str(target), str(backup))
    assert not target.exists()


# --- Private from the start -------------------------------------------------

def test_a_new_database_is_0600_whatever_the_umask(tmp_path, open_umask):
    db = tmp_path / "b.db"
    migrate_db(db)
    assert mode(db) == 0o600


def test_every_way_of_creating_it_is_0600(tmp_path, open_umask, monkeypatch):
    create_app(tmp_path / "app.db")
    assert mode(tmp_path / "app.db") == 0o600
    provision_family(tmp_path / "prov.db", [("alice", "Alice")])
    assert mode(tmp_path / "prov.db") == 0o600
    monkeypatch.setenv("BOARD_DB", str(tmp_path / "cli.db"))
    admin_cli.main(["provision", "--admin", "alice:Alice"])
    assert mode(tmp_path / "cli.db") == 0o600


def test_an_existing_database_with_loose_rights_is_tightened(tmp_path):
    db = tmp_path / "b.db"
    migrate_db(db)
    os.chmod(db, 0o644)
    migrate_db(db)
    assert mode(db) == 0o600


def test_its_journal_is_no_looser(tmp_path, open_umask):
    db = tmp_path / "b.db"
    migrate_db(db)
    conn = sqlite3.connect(db)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.execute("PRAGMA cache_spill = 1")
    conn.execute("PRAGMA cache_size = 1")
    conn.execute("INSERT INTO t SELECT zeroblob(100000)")
    assert mode(f"{db}-journal") == 0o600
    conn.rollback()
    conn.close()


# --- Host commands migrate first --------------------------------------------

def test_reset_admin_on_an_unmigrated_database_works(tmp_path, monkeypatch, capsys):
    db = make_v0(tmp_path)
    monkeypatch.setenv("BOARD_DB", str(db))
    admin_cli.main(["reset-admin", "legacy-a"])
    assert "legacy-a: /setup/" in capsys.readouterr().out
    assert user_version(db) == len(MIGRATIONS)


def test_reset_admin_on_an_empty_file_is_a_message(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    db.touch()
    monkeypatch.setenv("BOARD_DB", str(db))
    with pytest.raises(SystemExit) as e:
        admin_cli.main(["reset-admin", "alice"])
    assert "unknown admin" in str(e.value)


@pytest.mark.parametrize("argv", [["reset-admin", "alice"],
                                  ["provision", "--admin", "alice:Alice"]])
def test_a_file_that_is_not_a_database_is_a_message(tmp_path, monkeypatch, argv):
    db = tmp_path / "junk.db"
    db.write_bytes(b"this is not a database, not at all" * 100)
    monkeypatch.setenv("BOARD_DB", str(db))
    with pytest.raises(SystemExit) as e:
        admin_cli.main(argv)
    assert isinstance(e.value.code, str) and "database" in e.value.code


def test_backup_copies_an_old_database_as_it_is(tmp_path):
    # The upgrade steps say to back up first, so backup must not migrate anything.
    db = make_v0(tmp_path)
    copy = tmp_path / "copy.db"
    admin_cli.backup_database(str(db), str(copy))
    assert user_version(db) == 0 and user_version(copy) == 0

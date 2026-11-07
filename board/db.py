"""SQLite schema and connection handling."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from board.migrations import migrate_db


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path, factory=sqlite3.Connection):
    """One connection per unit of work; commits on success, rolls back on error."""
    # A request's dependencies and handler may run on different threads, but
    # never concurrently, and each request gets its own connection.
    conn = sqlite3.connect(db_path, check_same_thread=False, factory=factory)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path):
    """Migrate the schema. Admins and lanes come from board.provision."""
    migrate_db(db_path)

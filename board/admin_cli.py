"""Host-side administration, run on the machine that holds the database.

    BOARD_DB=/path/to/board.db python -m board.admin_cli provision \\
        --admin alice:Alice --admin bob:Bob [--lanes lanes.toml]

    BOARD_DB=/path/to/board.db BOARD_PUBLIC_URL=https://board.example.com \\
        python -m board.admin_cli reset-admin NAME

Setup links are built from BOARD_PUBLIC_URL.

    BOARD_DB=... python -m board.admin_cli remove-lane SLUG

remove-lane deletes a lane that holds no posts, with its grants and requests, and
writes a shared audit entry so every admin sees it. It refuses a lane with posts.

    BOARD_DB=... python -m board.admin_cli backup PATH
    BOARD_DB=... python -m board.admin_cli restore PATH [--force]

backup uses SQLite's backup API, so it is consistent while the board writes. It
copies the database as it is and never migrates it, so a backup taken before an
upgrade really is the old version. reset-admin and provision migrate first.
restore overwrites the database in place: stop the board first (see
docs/DESIGN.md §10b). It refuses if the database already holds any admin or post,
and with --force it first saves what it replaces next to the database.

Imports nothing from board.main: that module builds an app on import.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from board import service
from board.db import init_db
from board.migrations import MIGRATIONS, DatabaseTooNew, too_new_message
from board.provision import FamilyExists, load_lanes_file, provision_family


def _admin(value):
    name, sep, display = value.partition(":")
    if not sep or not name or not display:
        raise argparse.ArgumentTypeError("use name:Display Name, e.g. alice:Alice")
    return name, display


def _print_links(tokens):
    base = os.environ.get("BOARD_PUBLIC_URL", "").rstrip("/")
    if not base:
        print("BOARD_PUBLIC_URL is not set; put your board's address in front of these paths.",
              file=sys.stderr)
    print("One-time setup links (valid 24 hours; each shows the authenticator QR code):")
    for name, token in tokens.items():
        print(f"  {name}: {base}/setup/{token}")


class BackupError(Exception):
    pass


def _ro_uri(path):
    """A read-only SQLite URI with the path percent-encoded, so '#' and '?' in a
    file name are part of the name."""
    return Path(path).resolve().as_uri() + "?mode=ro"


def _copy_database(src_path, dst_path, readonly_src=False):
    """Copy with SQLite's backup API: a consistent snapshot even while another
    process writes. The result is a single file, mode 0600 (it holds TOTP
    secrets and token hashes)."""
    old_umask = os.umask(0o077)
    try:
        src = sqlite3.connect(_ro_uri(src_path) if readonly_src else src_path,
                              uri=readonly_src)
        try:
            dst = sqlite3.connect(dst_path)
            try:
                dst.execute("PRAGMA journal_mode=DELETE")
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    finally:
        os.umask(old_umask)
    os.chmod(dst_path, 0o600)


def backup_database(db_path, dest):
    if not os.path.exists(db_path):
        raise BackupError(f"{db_path} does not exist.")
    try:  # exclusive create: never overwrite, and the file is 0600 from the start
        os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        raise BackupError(f"{dest} already exists; choose a new file name.")
    except OSError as e:
        raise BackupError(f"cannot create {dest}: {e}")
    try:
        _copy_database(db_path, dest)
    except sqlite3.Error as e:
        os.unlink(dest)
        raise BackupError(f"backup failed: {e}")


def _holds_content(path):
    """True if the database has any admin or post. A missing or empty file, or
    one without those tables, holds nothing. Any other failure (locked, I/O,
    permissions) raises sqlite3.Error: it must never be read as "empty"."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    conn = sqlite3.connect(path, timeout=2)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("admins", "posts"):
            if table in tables and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return True
        return False
    finally:
        conn.close()


def _check_backup_file(path):
    if not os.path.isfile(path):
        raise BackupError(f"{path} is not a file.")
    if not os.access(path, os.R_OK):
        raise BackupError(f"permission denied: cannot read {path} (check its owner and mode; "
                          "inside Docker the board runs as uid 10001).")
    try:
        conn = sqlite3.connect(_ro_uri(path), uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError(f"{path} failed SQLite's integrity check.")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise BackupError(too_new_message(path, version))
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.Error:
        raise BackupError(f"{path} is not a SQLite database.")
    if not {"admins", "posts", "lanes", "grants"} <= tables:
        raise BackupError(f"{path} does not look like a board backup.")


def restore_database(db_path, source, force=False):
    _check_backup_file(source)
    try:
        holds = _holds_content(db_path)
    except sqlite3.Error as e:
        raise BackupError(f"cannot tell whether {db_path} holds data ({e}); "
                          "stop the board and try again. Nothing was changed.")
    if holds:
        if not force:
            raise BackupError(
                f"{db_path} already holds admins or posts; restoring would replace them. "
                "Stop the board, then re-run with --force to overwrite "
                "(a safety copy of the current database is kept).")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        safety = f"{db_path}.before-restore-{stamp}"
        backup_database(db_path, safety)
        print(f"Saved the current database to {safety}")
    os.makedirs(os.path.dirname(db_path) or ".", mode=0o700, exist_ok=True)
    try:
        _copy_database(source, db_path, readonly_src=True)
    except sqlite3.Error as e:
        raise BackupError(f"restore failed: {e}")
    init_db(db_path)  # an older backup is migrated to the current schema


def _on_board(db_path, label, action):
    """Run `action(conn)` on an existing board database, migrated first (it may
    predate the columns the action writes), and return its result. Any failure
    exits with a message that starts with `label`."""
    if not os.path.exists(db_path):
        sys.exit(f"{db_path} does not exist.")
    try:
        init_db(db_path)
        with service.connect(db_path) as conn:
            return action(conn)
    except DatabaseTooNew as e:
        sys.exit(str(e))
    except service.BoardError as e:
        sys.exit(f"{label} failed: {e.message}")
    except sqlite3.Error as e:
        sys.exit(f"{label} failed: cannot use the database at {db_path}: {e}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="board.admin_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    prov = sub.add_parser("provision", help="create the admins and lanes in a new database")
    prov.add_argument("--admin", action="append", type=_admin, required=True,
                      metavar="NAME:DISPLAY", help="repeat for each admin")
    prov.add_argument("--lanes", metavar="FILE",
                      help="TOML file of [[lane]] tables replacing the default lanes")
    reset = sub.add_parser("reset-admin", help="clear an admin's login and print a new setup link")
    reset.add_argument("name")
    rml = sub.add_parser("remove-lane", help="delete a lane that has no posts")
    rml.add_argument("slug")
    sens = sub.add_parser("set-sensitive", help="turn a lane's totals-only rules on or off")
    sens.add_argument("slug")
    sens.add_argument("state", choices=["on", "off"])
    ret = sub.add_parser("set-retention", help="set how many days a lane keeps posts, or 'off'")
    ret.add_argument("slug")
    ret.add_argument("days", help="a number of days (30 or more), or 'off' to keep forever")
    sub.add_parser("prune", help="delete posts older than their lane's retention")
    bak = sub.add_parser("backup", help="write a consistent copy of the database to a new file")
    bak.add_argument("path")
    res = sub.add_parser("restore", help="replace the database with a backup (stop the board first)")
    res.add_argument("path")
    res.add_argument("--force", action="store_true",
                     help="overwrite a database that already holds admins or posts")
    args = parser.parse_args(argv)

    db_path = os.environ.get("BOARD_DB")
    if not db_path:
        sys.exit("BOARD_DB is not set. Set it to the path of the board's SQLite database file.")
    if args.command in ("backup", "restore"):
        try:
            if args.command == "backup":
                backup_database(db_path, args.path)
                print(f"Backed up {db_path} to {args.path} (mode 0600).")
            else:
                restore_database(db_path, args.path, args.force)
                print(f"Restored {args.path} into {db_path}.")
        except BackupError as e:
            sys.exit(str(e))
        return
    if args.command == "reset-admin":
        token = _on_board(db_path, "reset", lambda conn: service.reset_admin(conn, args.name))
        _print_links({args.name: token})
        return
    if args.command == "remove-lane":
        result = _on_board(db_path, "remove-lane",
                           lambda conn: service.remove_lane(conn, args.slug))
        print(f"Removed lane {result['lane']} ({result['grants']} grant(s), "
              f"{result['requests']} request(s)).")
        return
    if args.command == "set-sensitive":
        result = _on_board(db_path, "set-sensitive",
                           lambda conn: service.set_sensitive(conn, args.slug, args.state == "on"))
        print(f"Lane {result['lane']} is now "
              f"{'sensitive' if result['sensitive'] else 'not sensitive'}.")
        return
    if args.command == "prune":
        result = _on_board(db_path, "prune", service.prune_posts)
        if result:
            print("Pruned " + ", ".join(f"{slug}: {n}" for slug, n in result.items()) + ".")
        else:
            print("Nothing to prune.")
        return
    if args.command == "set-retention":
        if not os.path.exists(db_path):
            sys.exit(f"{db_path} does not exist.")
        days = None
        if args.days != "off":
            try:
                days = int(args.days)
            except ValueError:
                sys.exit("set-retention failed: days must be a whole number, or 'off'")
        result = _on_board(db_path, "set-retention",
                           lambda conn: service.set_retention(conn, args.slug, days))
        kept = "forever" if result["days"] is None else f"{result['days']} days"
        print(f"Lane {result['lane']} now keeps posts {kept}.")
        return
    os.makedirs(os.path.dirname(db_path) or ".", mode=0o700, exist_ok=True)
    try:
        lanes = load_lanes_file(args.lanes) if args.lanes else None
        tokens = provision_family(db_path, args.admin, lanes)
    except FamilyExists:
        sys.exit(f"{db_path} already has admins; refusing to provision it again.")
    except DatabaseTooNew as e:
        sys.exit(str(e))
    except sqlite3.Error as e:
        sys.exit(f"provision failed: cannot use the database at {db_path}: {e}")
    except (ValueError, OSError) as e:
        sys.exit(f"provision failed: {e}")
    print(f"Provisioned {', '.join(n for n, _ in args.admin)} in {db_path}.")
    _print_links(tokens)


if __name__ == "__main__":
    main()

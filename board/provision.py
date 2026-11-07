"""Set up a new family's board: its admins and lanes. Done once per database."""

import re
import tomllib

from board import service
from board.db import connect, init_db, now_iso
from board.lanes import BASE_NOTICE, default_lanes

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class FamilyExists(Exception):
    """The database already has admins."""


def load_lanes_file(path):
    """Read `[[lane]]` tables (slug, name, scope, optional notice) from a TOML file."""
    with open(path, "rb") as f:
        return tomllib.load(f).get("lane", [])


def _check_admins(admins):
    if not admins:
        raise ValueError("at least one admin is required")
    names = [name for name, _ in admins]
    for name, display in admins:
        if not NAME_RE.match(name) or name == "shared":
            raise ValueError(f"admin name {name!r} must be lowercase letters, digits and "
                             "hyphens, start with a letter, and not be 'shared'")
        if not display.strip():
            raise ValueError(f"admin {name!r} needs a display name")
    if len(set(names)) != len(names):
        raise ValueError("admin names must be unique")


def _check_lanes(lanes, names):
    seen = set()
    out = []
    for lane in lanes:
        slug, scope = lane.get("slug", ""), lane.get("scope", "")
        if not SLUG_RE.match(slug) or slug in seen:
            raise ValueError(f"lane slug {slug!r} is invalid or repeated")
        if scope != "shared" and scope not in names:
            raise ValueError(f"lane {slug!r}: scope must be 'shared' or an admin name")
        if not lane.get("name"):
            raise ValueError(f"lane {slug!r} needs a name")
        notice = lane.get("notice") or ""
        # The no-purchase notice stays on every lane, whatever the file says.
        if BASE_NOTICE not in notice:
            notice = f"{BASE_NOTICE} {notice}".strip()
        days = lane.get("retention_days")
        if days is not None and (isinstance(days, bool) or not isinstance(days, int)
                                 or days < service.MIN_RETENTION_DAYS):
            raise ValueError(f"lane {slug!r}: retention_days must be a whole number of "
                             f"{service.MIN_RETENTION_DAYS} or more")
        seen.add(slug)
        out.append({"slug": slug, "name": lane["name"], "scope": scope, "notice": notice,
                    "sensitive": bool(lane.get("sensitive", False)),
                    "retention_days": lane.get("retention_days")})
    return out


def provision_family(db_path, admins, lanes=None):
    """Create the admins (and their human principals) and the lanes, in one
    transaction. `admins` is [(username, display name)]; `lanes` replaces the
    default template. Returns {admin: setup token}, one per admin, for the setup
    links. Raises FamilyExists if the database already has an admin."""
    _check_admins(admins)
    names = [name for name, _ in admins]
    if lanes is None:
        lanes = default_lanes(names, dict(admins))
    lanes = _check_lanes(lanes, set(names))
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")  # two provisioners can't both pass the check
        if conn.execute("SELECT 1 FROM admins LIMIT 1").fetchone():
            raise FamilyExists("this database already has admins")
        tokens = {}
        for name, display in admins:
            conn.execute(
                "INSERT INTO principals (id, kind, owner, display_name, created_at) "
                "VALUES (?, 'human', ?, ?, ?)", (name, name, display, now_iso()))
            conn.execute("INSERT INTO admins (name) VALUES (?)", (name,))
            tokens[name] = service.issue_setup_link(conn, name)
        for lane in lanes:
            conn.execute(
                "INSERT INTO lanes (slug, name, scope, notice, sensitive, retention_days, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lane["slug"], lane["name"], lane["scope"], lane["notice"],
                 int(bool(lane.get("sensitive"))), lane.get("retention_days"), now_iso()))
    return tokens

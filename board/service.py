"""Board rules. Every permission decision is made here, from the database, on
every call: nothing is cached, which is what makes revocation instant."""

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pyotp

from board.db import connect, now_iso  # noqa: F401  (connect re-exported for callers)

MAX_BODY_CHARS = 8192
# Sensitive lanes (money, health) take short summaries, not records.
SENSITIVE_MAX_CHARS = 2048
SENSITIVE_DIGIT_RUN = 9  # one run of this many digits looks like an account number
# Digits, with single spaces or dashes between them (card numbers, SSNs). \d also
# matches non-ASCII digits, so full-width numbers are caught too.
_DIGIT_RUN = re.compile(r"\d(?:[ -]?\d)*")
# The most a post's JSON request can legitimately take: every character written as
# a \uXXXX escape (12 bytes for one outside the basic plane, as the client does),
# plus the envelope around it. Any body within MAX_BODY_CHARS fits.
MAX_RAW_BYTES = 12 * MAX_BODY_CHARS + 1024
# The cap on any request, checked before anything parses it. The largest legitimate
# one is a full post from a browser form, where a character can take 12 bytes once
# percent-encoded, plus the other fields. Keep the Caddyfile's max_size above this.
MAX_REQUEST_BYTES = 12 * MAX_BODY_CHARS + 4096
SESSION_SECONDS = 12 * 3600
SETUP_SECONDS = 24 * 3600
MIN_PASSWORD_CHARS = 12
LOCK_AFTER = 5  # failures per (username, client address)
LOCK_SECONDS = 900
PAIR_FORGET_SECONDS = 3600  # a pair with no failure for this long, and no live lock, is deleted
TOKEN_PREFIX = "fb_"
INVITE_PREFIX = "inv_"
INVITE_SECONDS = 3600
MAX_NAME_CHARS = 80
LAST_SEEN_INTERVAL = 60
MAX_CONNECTIONS = 10  # OAuth connections kept per agent (decisions/0005)
HUMAN_KINDS = {"decision", "approval", "stop"}
POST_KINDS = {"note"} | HUMAN_KINDS


def _now():
    """Wall-clock time for human login; tests replace it to freeze the clock."""
    return time.time()


class BoardError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


INT64 = range(-2**63, 2**63)  # what SQLite can store; a larger int raises OverflowError


def _is_text(value):
    """A string SQLite can store. JSON can carry a lone surrogate, which it can't."""
    if not isinstance(value, str):
        return False
    try:
        value.encode()
    except UnicodeEncodeError:
        return False
    return True


def not_found():
    # Same answer for "doesn't exist" and "no rights on it", so lanes can't be probed.
    return BoardError(404, "not found")


@dataclass(frozen=True)
class Caller:
    id: str
    kind: str   # 'agent' or 'human'
    owner: str  # the admin who owns the principal


def list_admins(conn):
    return [r["name"] for r in conn.execute("SELECT name FROM admins ORDER BY name")]


def other_admins(conn, name):
    return [a for a in list_admins(conn) if a != name]


# --- Authentication -----------------------------------------------------

def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_agent_token(conn, principal_id):
    """Make a new bearer token for an agent, replacing any it had. Only the
    SHA-256 is stored; the caller must show the token once and never keep it."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    conn.execute("UPDATE principals SET token_hash = ?, last_seen_at = NULL WHERE id = ?",
                 (_hash_token(token), principal_id))
    return token


def add_agent_connection(conn, principal_id):
    """Make a token for one more OAuth connection of an agent (decisions/0005). It
    replaces nothing, so other connections keep working. Past MAX_CONNECTIONS the
    connection used least recently is dropped. Only the SHA-256 is stored."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    conn.execute("INSERT INTO agent_connections (token_hash, agent_id, created_at) "
                 "VALUES (?, ?, ?)", (_hash_token(token), principal_id, _now()))
    conn.execute(
        "DELETE FROM agent_connections WHERE agent_id = ? AND token_hash NOT IN ("
        "SELECT token_hash FROM agent_connections WHERE agent_id = ? "
        "ORDER BY COALESCE(last_seen_at, created_at) DESC, rowid DESC LIMIT ?)",
        (principal_id, principal_id, MAX_CONNECTIONS))
    return token


def authenticate_token(conn, token):
    """The agent holding this bearer token: its invite token or one of its OAuth
    connections. Any failure is the same plain 401."""
    denied = BoardError(401, "invalid token")
    if not token:
        raise denied
    token_hash = _hash_token(token)
    row = conn.execute("SELECT * FROM principals WHERE token_hash = ?",
                       (token_hash,)).fetchone()
    connection = None
    if row is None:
        connection = conn.execute("SELECT agent_id, last_seen_at FROM agent_connections "
                                  "WHERE token_hash = ?", (token_hash,)).fetchone()
        if connection is not None:
            row = conn.execute("SELECT * FROM principals WHERE id = ?",
                               (connection["agent_id"],)).fetchone()
    if row is None or row["kind"] != "agent" or row["status"] != "active":
        raise denied
    now = _now()
    # Decide in Python: even an UPDATE that matches no rows takes SQLite's write
    # lock, which would serialize every agent request and block an admin's revoke.
    if row["last_seen_at"] is None or row["last_seen_at"] <= now - LAST_SEEN_INTERVAL:
        conn.execute("UPDATE principals SET last_seen_at = ? WHERE id = ?", (now, row["id"]))
    if connection is not None and (connection["last_seen_at"] is None or
                                   connection["last_seen_at"] <= now - LAST_SEEN_INTERVAL):
        conn.execute("UPDATE agent_connections SET last_seen_at = ? WHERE token_hash = ?",
                     (now, token_hash))
    return Caller(row["id"], "agent", row["owner"])


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


# Hashed once at import: unknown and unenrolled users get the same scrypt work.
_DUMMY_HASH = hash_password("no-such-account")


def _password_ok(password, stored):
    _, salt_hex, _ = (stored or _DUMMY_HASH).split("$")
    candidate = hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(candidate, stored or _DUMMY_HASH) and stored is not None


def _matching_step(secret, code, after_step):
    """The TOTP step `code` is valid for (one step either side of now), if it is
    later than `after_step`; else None. Time comes from _now so tests can freeze it."""
    code = (code or "").replace(" ", "")
    if not code.isascii():
        return None  # compare_digest raises on non-ASCII text
    totp = pyotp.TOTP(secret or pyotp.random_base32())
    now = int(_now())
    found = None
    for offset in (-1, 0, 1):
        step = now // totp.interval + offset
        if hmac.compare_digest(totp.at(step * totp.interval), code) and step > after_step:
            found = step
    return found if secret else None


def issue_setup_link(conn, admin):
    """Make a one-time, 24-hour setup token (stored hashed) and a fresh TOTP secret."""
    row = conn.execute("SELECT pass_hash FROM admins WHERE name = ?", (admin,)).fetchone()
    if row is None:
        raise BoardError(400, "unknown admin")
    if row["pass_hash"]:
        raise BoardError(409, "this admin already has a login; reset it first")
    token = secrets.token_urlsafe(32)
    conn.execute("UPDATE admins SET totp_secret = ?, totp_last_step = 0, setup_hash = ?, "
                 "setup_expires_at = ? WHERE name = ?",
                 (pyotp.random_base32(), _hash_token(token), _now() + SETUP_SECONDS, admin))
    return token


SETUP_DONE = "setup is already done for this link, so you can log in"
SETUP_GONE = "this setup link has expired or was replaced"


def _setup_row(conn, token):
    row = conn.execute("SELECT * FROM admins WHERE setup_hash = ?",
                       (_hash_token(token or ""),)).fetchone()
    if row is not None and row["setup_expires_at"] is None:
        raise BoardError(410, SETUP_DONE)
    if row is None or row["setup_expires_at"] <= _now():
        raise BoardError(410, SETUP_GONE)
    return row


def setup_details(conn, token):
    """What the setup page shows. Viewing it uses nothing up."""
    row = _setup_row(conn, token)
    url = pyotp.TOTP(row["totp_secret"]).provisioning_uri(name=row["name"],
                                                         issuer_name="FuzzPan")
    return {"admin": row["name"], "secret": row["totp_secret"], "otpauth_url": url}


def complete_setup(conn, token, password, code):
    """Set the password and confirm the authenticator in one step; nothing is
    saved unless both are good. The token works once. Its hash stays, with no
    expiry, so a second tap on the button can say setup is already done."""
    row = _setup_row(conn, token)
    if len(password) < MIN_PASSWORD_CHARS:
        raise BoardError(400, f"Use a password of at least {MIN_PASSWORD_CHARS} characters.")
    step = _matching_step(row["totp_secret"], code, 0)
    if step is None:
        raise BoardError(400, "That code did not match. Check the authenticator and try again.")
    used = conn.execute(
        "UPDATE admins SET pass_hash = ?, totp_last_step = ?, setup_expires_at = NULL "
        "WHERE name = ? AND setup_hash = ? AND setup_expires_at IS NOT NULL",
        (hash_password(password), step, row["name"], row["setup_hash"])).rowcount
    if not used:
        raise BoardError(410, SETUP_DONE)
    return row["name"]


def begin_session(conn, name):
    """Log an admin in right after setup finished: they have just proved the
    password and a current authenticator code, so a second login would only make
    them wait for the next code. Counts as their first login (it onboards them),
    and returns a session token like `login` does."""
    now = _now()
    conn.execute("UPDATE admins SET last_login_at = ? WHERE name = ?", (now_iso(), name))
    return _new_session(conn, name, now)


def _new_session(conn, name, now):
    token = secrets.token_urlsafe(32)
    # Only the hash is stored, so a copy of the database holds no usable session.
    conn.execute("INSERT INTO sessions (token, admin, expires_at) VALUES (?, ?, ?)",
                 (_hash_token(token), name, now + SESSION_SECONDS))
    return token


def _record_login_failure(conn, username, client, now):
    """Count one failure for this (username, client) pair and forget pairs gone
    stale. A single statement, so parallel attempts can't undercount, and one that
    lost a race can't overwrite a lock another just set. There is no lock on the
    username itself: a lock that anyone's guesses could set would let an attacker
    keep the admin out."""
    conn.execute("DELETE FROM login_pairs WHERE last_failure_at <= ? "
                 "AND (locked_until IS NULL OR locked_until <= ?)",
                 (now - PAIR_FORGET_SECONDS, now))
    conn.execute(
        "INSERT INTO login_pairs (username, client, failures, locked_until, last_failure_at) "
        "VALUES (?, ?, 1, NULL, ?) ON CONFLICT (username, client) DO UPDATE SET "
        "locked_until = CASE WHEN failures + 1 >= ? THEN ? ELSE locked_until END, "
        "failures = CASE WHEN failures + 1 >= ? THEN 0 ELSE failures + 1 END, "
        "last_failure_at = excluded.last_failure_at "
        "WHERE locked_until IS NULL OR locked_until <= ?",
        (username, client, now, LOCK_AFTER, now + LOCK_SECONDS, LOCK_AFTER, now))


def login(conn, username, password, code, client="unknown"):
    """Return a session token, or None. Every failure looks the same, including
    unknown users, unenrolled accounts and a locked account or address. `client`
    is the caller's address (limits.client_key): failures lock that pair, so
    someone elsewhere can't lock an admin out. A first login onboards the admin."""
    username = username.strip().lower()  # admin names are lowercase; phones capitalize
    row = conn.execute("SELECT * FROM admins WHERE name = ?", (username,)).fetchone()
    enrolled = row is not None and row["pass_hash"] and row["totp_secret"]
    now = _now()
    # Always do the password work and the code check, whatever is wrong.
    password_ok = _password_ok(password, row["pass_hash"] if enrolled else None)
    step = _matching_step(row["totp_secret"] if enrolled else None, code,
                          row["totp_last_step"] if enrolled else 0)
    if not enrolled:
        return None
    pair_locked = conn.execute("SELECT 1 FROM login_pairs WHERE username = ? AND client = ? "
                               "AND locked_until > ?", (username, client, now)).fetchone()
    if pair_locked:
        return None  # locked: no counting, and the code is not consumed
    if not (password_ok and step is not None):
        _record_login_failure(conn, username, client, now)
        return None
    # Claim the step atomically so two concurrent logins can't share a code. The
    # claim also requires the credentials we checked to still be the stored ones
    # (a reset in between clears them) and no lock to have been set meanwhile.
    claimed = conn.execute(
        "UPDATE admins SET totp_last_step = ?, last_login_at = ? "
        "WHERE name = ? AND totp_last_step < ? AND pass_hash = ? AND totp_secret = ? "
        "AND NOT EXISTS (SELECT 1 FROM login_pairs WHERE username = ? AND client = ? "
        "AND locked_until > ?)",
        (step, now_iso(), username, step, row["pass_hash"], row["totp_secret"],
         username, client, now)).rowcount
    if not claimed:
        return None
    conn.execute("DELETE FROM login_pairs WHERE username = ? AND client = ?", (username, client))
    return _new_session(conn, username, now)


def reset_admin(conn, admin):
    """Host-side only. Clears the password, authenticator and sessions, tells
    every admin, and returns a new setup token."""
    if conn.execute("SELECT 1 FROM admins WHERE name = ?", (admin,)).fetchone() is None:
        raise BoardError(400, "unknown admin")
    conn.execute("UPDATE admins SET pass_hash = NULL, totp_secret = NULL, totp_last_step = 0, "
                 "setup_hash = NULL, setup_expires_at = NULL WHERE name = ?", (admin,))
    conn.execute("DELETE FROM sessions WHERE admin = ?", (admin,))
    conn.execute("DELETE FROM login_pairs WHERE username = ?", (admin,))
    # Links made before the reset are void: whoever now holds the account is not
    # who made them.
    conn.execute("UPDATE invites SET status = 'cancelled' WHERE owner = ? AND status = 'open'",
                 (admin,))
    conn.execute("DELETE FROM oauth_codes WHERE owner = ?", (admin,))
    _audit(conn, "host", "admin-reset", "shared", admin=admin)
    return issue_setup_link(conn, admin)


def remove_lane(conn, slug):
    """Host-side only. Removes a lane that has no posts, with its grants and its
    requests. Refuses a lane that holds posts (nothing is ever discarded silently)
    or that an open invite still names. Tells every admin, whatever the lane's scope,
    so removing a private lane is never invisible to its owner."""
    lane = _lane(conn, slug)
    if lane is None:
        raise BoardError(400, "unknown lane")
    posts = conn.execute("SELECT count(*) FROM posts WHERE lane_id = ?", (lane["id"],)).fetchone()[0]
    if posts:
        raise BoardError(409, f"{slug} has {posts} post(s); only an empty lane can be removed")
    for (lanes_json,) in conn.execute("SELECT lanes_json FROM invites WHERE status = 'open'"):
        try:
            named = json.loads(lanes_json)
        except ValueError:
            named = {}
        if isinstance(named, dict) and slug in named:
            raise BoardError(409, f"an open invite still names {slug}; cancel it first")
    grants = conn.execute("SELECT count(*) FROM grants WHERE lane_id = ?", (lane["id"],)).fetchone()[0]
    requests = conn.execute("SELECT count(*) FROM pending WHERE lane_id = ?", (lane["id"],)).fetchone()[0]
    conn.execute("DELETE FROM pending WHERE lane_id = ?", (lane["id"],))
    conn.execute("DELETE FROM grants WHERE lane_id = ?", (lane["id"],))
    conn.execute("DELETE FROM lanes WHERE id = ?", (lane["id"],))
    _audit(conn, "host", "lane-removed", "shared", lane=slug, lane_scope=lane["scope"],
           grants=grants, requests=requests)
    return {"lane": slug, "grants": grants, "requests": requests}


def set_sensitive(conn, slug, on):
    """Host-side only. Turns the sensitive-lane rules on or off for a lane. Audited,
    visible to every admin; turning them off is as visible as turning them on."""
    lane = _lane(conn, slug)
    if lane is None:
        raise BoardError(400, "unknown lane")
    conn.execute("UPDATE lanes SET sensitive = ? WHERE id = ?", (int(bool(on)), lane["id"]))
    _audit(conn, "host", "lane-sensitive-on" if on else "lane-sensitive-off",
           "shared", lane=slug, lane_scope=lane["scope"])
    return {"lane": slug, "sensitive": bool(on)}


MIN_RETENTION_DAYS = 30


def set_retention(conn, slug, days):
    """Host-side only. How many days a lane keeps posts; None keeps them forever.
    Audited with scope 'shared', so both admins see it."""
    lane = _lane(conn, slug)
    if lane is None:
        raise BoardError(400, "unknown lane")
    if days is not None and (isinstance(days, bool) or not isinstance(days, int)
                             or days < MIN_RETENTION_DAYS):
        raise BoardError(400, f"retention must be at least {MIN_RETENTION_DAYS} days, or off")
    conn.execute("UPDATE lanes SET retention_days = ? WHERE id = ?", (days, lane["id"]))
    _audit(conn, "host", "lane-retention-set", "shared", lane=slug, lane_scope=lane["scope"],
           days=days)
    return {"lane": slug, "days": days}


def prune_posts(conn, now=None):
    """Host-side only. Deletes posts older than their lane's retention. Returns
    {slug: count} for lanes that lost posts. Writes one `posts-pruned` entry per such
    lane with scope 'shared' (the lane, its scope and the count; never any text), so
    each admin sees that posts in a private lane were pruned. Lanes without retention
    are untouched. This is the one way a post is ever deleted."""
    now = now or datetime.now(timezone.utc)
    pruned = {}
    for lane in conn.execute("SELECT * FROM lanes WHERE retention_days IS NOT NULL").fetchall():
        cutoff = (now - timedelta(days=lane["retention_days"])).isoformat(timespec="seconds")
        count = conn.execute("DELETE FROM posts WHERE lane_id = ? AND received_at < ?",
                             (lane["id"], cutoff)).rowcount
        if count:
            pruned[lane["slug"]] = count
            _audit(conn, "host", "posts-pruned", "shared", lane=lane["slug"],
                   lane_scope=lane["scope"], posts=count, older_than=cutoff,
                   retention_days=lane["retention_days"])
    return pruned


def logout(conn, token):
    conn.execute("DELETE FROM sessions WHERE token = ?", (_hash_token(token or ""),))


def session_admin(conn, token):
    if not token:
        return None
    row = conn.execute("SELECT admin FROM sessions WHERE token = ? AND expires_at > ?",
                       (_hash_token(token), _now())).fetchone()
    return Caller(row["admin"], "human", row["admin"]) if row else None


def is_onboarded(conn, admin):
    row = conn.execute("SELECT last_login_at FROM admins WHERE name = ?", (admin,)).fetchone()
    return bool(row and row["last_login_at"])


# --- Lanes and rights ---------------------------------------------------

def _lane(conn, slug):
    if not _is_text(slug):
        return None
    return conn.execute("SELECT * FROM lanes WHERE slug = ?", (slug,)).fetchone()


def _grant(conn, lane_id, principal_id):
    row = conn.execute("SELECT can_read, can_post FROM grants WHERE lane_id = ? "
                       "AND principal_id = ?", (lane_id, principal_id)).fetchone()
    return (bool(row["can_read"]), bool(row["can_post"])) if row else (False, False)


def administers(caller, lane):
    return caller.kind == "human" and lane["scope"] in (caller.id, "shared")


def rights(conn, caller, lane):
    """(can_read, can_post) for this caller on this lane."""
    if administers(caller, lane):
        return True, True
    return _grant(conn, lane["id"], caller.id)


def known_lane(conn, caller, slug):
    """The lane, if the caller holds either right on it. With neither: 404."""
    lane = _lane(conn, slug)
    if lane is None or not any(rights(conn, caller, lane)):
        raise not_found()
    return lane


def readable_lane(conn, caller, slug):
    """404 with neither right; 403 for a post-only caller."""
    lane = known_lane(conn, caller, slug)
    if not rights(conn, caller, lane)[0]:
        raise BoardError(403, "no read right on this lane")
    return lane


def visible_lanes(conn, caller):
    """The lanes the caller can read: the feed page and the admin views."""
    lanes = conn.execute("SELECT * FROM lanes ORDER BY id").fetchall()
    return [l for l in lanes if rights(conn, caller, l)[0]]


def me(conn, caller):
    """Every lane the caller holds either right on, post-only ones included."""
    lanes = {}
    for lane in conn.execute("SELECT * FROM lanes ORDER BY id").fetchall():
        can_read, can_post = rights(conn, caller, lane)
        if can_read or can_post:
            lanes[lane["slug"]] = {"read": can_read, "post": can_post}
    return {"id": caller.id, "kind": caller.kind, "owner": caller.owner,
            "lanes": dict(sorted(lanes.items()))}


# --- Posts --------------------------------------------------------------

def create_post(conn, caller, slug, body, kind="note"):
    lane = known_lane(conn, caller, slug)
    if not rights(conn, caller, lane)[1]:
        raise BoardError(403, "no publish right on this lane")
    if not isinstance(kind, str) or kind not in POST_KINDS:
        raise BoardError(400, f"kind must be one of {sorted(POST_KINDS)}")
    if kind in HUMAN_KINDS and caller.kind != "human":
        raise BoardError(403, "only humans post decisions, approvals, and stops")
    if not _is_text(body) or not body.strip():
        raise BoardError(400, "body must be a non-empty string of valid text")
    if len(body) > MAX_BODY_CHARS:
        raise BoardError(413, "body too long")
    if lane["sensitive"]:
        _check_sensitive(conn, caller, lane, body)
    cur = conn.execute(
        "INSERT INTO posts (lane_id, author_id, kind, body, received_at) VALUES (?, ?, ?, ?, ?)",
        (lane["id"], caller.id, kind, body, now_iso()))
    return {"id": cur.lastrowid, "lane": slug}


def _refuse_sensitive(conn, caller, lane, status, reason):
    """Record a refusal (never the body) and keep it: the caller's error would
    otherwise roll the audit row back with the rest of the request."""
    _audit(conn, caller.id, "post-rejected", lane["scope"], lane=lane["slug"], reason=reason)
    conn.commit()
    raise BoardError(status, {
        "too-long": f"posts to this lane are limited to {SENSITIVE_MAX_CHARS} characters",
        "digits": "this lane takes totals and averages only: no account, card or ID "
                  f"numbers (a run of {SENSITIVE_DIGIT_RUN} or more digits is refused)",
    }[reason])


def _check_sensitive(conn, caller, lane, body):
    if len(body) > SENSITIVE_MAX_CHARS:
        _refuse_sensitive(conn, caller, lane, 413, "too-long")
    for run in _DIGIT_RUN.finditer(body):
        if sum(ch.isdigit() for ch in run.group()) >= SENSITIVE_DIGIT_RUN:
            _refuse_sensitive(conn, caller, lane, 422, "digits")


def _post_dict(row):
    return {"id": row["id"], "author": row["author_id"], "kind": row["kind"],
            "body": row["body"], "received_at": row["received_at"]}


def read_posts(conn, caller, slug, since=None, limit=100):
    lane = readable_lane(conn, caller, slug)
    limit = max(1, min(int(limit), 500))
    if since is not None and int(since) not in INT64:
        raise BoardError(400, "since is out of range")
    if since is not None:
        rows = conn.execute("SELECT * FROM posts WHERE lane_id = ? AND id > ? "
                            "ORDER BY id LIMIT ?", (lane["id"], int(since), limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM posts WHERE lane_id = ? ORDER BY id DESC LIMIT ?",
                            (lane["id"], limit)).fetchall()[::-1]
    return {"lane": slug, "name": lane["name"], "notice": lane["notice"],
            "posts": [_post_dict(r) for r in rows]}


# --- Admin: principals --------------------------------------------------

def _audit(conn, actor, action, scope, **detail):
    conn.execute("INSERT INTO audit_log (ts, actor, action, scope, detail) "
                 "VALUES (?, ?, ?, ?, ?)", (now_iso(), actor, action, scope, json.dumps(detail)))


def _require_admin(caller):
    if caller.kind != "human":
        raise BoardError(403, "admin only")


def register_agent(conn, caller, principal_id, display_name=None, with_token=True):
    """Register an agent owned by the calling admin and return its bearer token,
    which is shown this once. with_token=False makes no invite token (an OAuth
    sign-in gives the agent a connection token instead); "token" is then None."""
    _require_admin(caller)
    if not principal_id or not principal_id.replace("-", "").isalnum() or len(principal_id) > 64:
        raise BoardError(400, "id must be letters, digits, and hyphens")
    if conn.execute("SELECT 1 FROM principals WHERE id = ?", (principal_id,)).fetchone():
        raise BoardError(409, "id already registered")
    conn.execute("INSERT INTO principals (id, kind, owner, display_name, created_at)"
                 " VALUES (?, 'agent', ?, ?, ?)",
                 (principal_id, caller.id, display_name or principal_id, now_iso()))
    token = issue_agent_token(conn, principal_id) if with_token else None
    _audit(conn, caller.id, "register", caller.id, principal=principal_id)
    return {"id": principal_id, "owner": caller.id, "token": token}


def revoke_agent(conn, caller, principal_id):
    _require_admin(caller)
    row = conn.execute("SELECT * FROM principals WHERE id = ? AND kind = 'agent' AND owner = ?",
                       (principal_id, caller.id)).fetchone()
    if row is None:
        raise not_found()
    # The token hashes go too, so revoked tokens are dead at rest as well: the invite
    # token and every OAuth connection.
    conn.execute("UPDATE principals SET status = 'revoked', token_hash = NULL WHERE id = ?",
                 (principal_id,))
    conn.execute("DELETE FROM agent_connections WHERE agent_id = ?", (principal_id,))
    # Its reconnect links are dead now, so they stop being listed as if they weren't.
    conn.execute("UPDATE invites SET status = 'cancelled' WHERE agent_id = ? AND "
                 "replaces_token = 1 AND status = 'open'", (principal_id,))
    _audit(conn, caller.id, "revoke", caller.id, principal=principal_id)
    return {"id": principal_id, "status": "revoked"}


# --- Admin: invite links ------------------------------------------------

def default_agent_name(conn, admin):
    """A free name to prefill for an admin's first agent: `<admin>-agent`, then
    `-2`, `-3` and so on past any id taken or held by an open invite."""
    n = 1
    while True:
        name = f"{admin}-agent" if n == 1 else f"{admin}-agent-{n}"
        taken = conn.execute("SELECT 1 FROM principals WHERE id = ?", (name,)).fetchone() or \
            conn.execute("SELECT 1 FROM invites WHERE agent_id = ? AND replaces_token = 0 "
                         "AND status = 'open' AND expires_at > ?", (name, _now())).fetchone()
        if not taken:
            return name
        n += 1


def changes_to_review(conn, caller):
    """How many open changes someone else asked for that this admin can decide."""
    return sum(1 for r in list_pending(conn, caller) if r["requested_by"] != caller.id)


def slugify(display_name):
    """The agent id for a display name: lowercase letters and digits, hyphens
    between runs of anything else. Empty when the name has none."""
    return re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")[:64].strip("-")


def _new_invite(conn, caller, agent_id, display_name, lanes, replaces_token):
    if replaces_token:
        # One live reconnect link per agent: a newer one supersedes the rest.
        conn.execute("UPDATE invites SET status = 'cancelled' WHERE agent_id = ? AND "
                     "replaces_token = 1 AND status = 'open'", (agent_id,))
    code = INVITE_PREFIX + secrets.token_urlsafe(32)
    expires_at = _now() + INVITE_SECONDS
    conn.execute(
        "INSERT INTO invites (code_hash, agent_id, display_name, owner, lanes_json, "
        "replaces_token, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_hash_token(code), agent_id, display_name, caller.id, json.dumps(lanes),
         int(replaces_token), now_iso(), expires_at))
    _audit(conn, caller.id, "invite-create", caller.id, agent=agent_id,
           kind="reconnect" if replaces_token else "new", lanes=lanes)
    return {"code": code, "agent_id": agent_id, "expires_at": expires_at}


def create_invite(conn, caller, display_name, lanes):
    """A single-use link that creates the agent `slug(display_name)` with the
    given lane rights when redeemed. `lanes` is {slug: {"read": bool, "post": bool}}.
    The caller must administer every lane listed. Rights apply only at
    redemption, through set_grant, so shared lanes still need approval."""
    _require_admin(caller)
    if not _is_text(display_name) or not display_name.strip() \
            or len(display_name.strip()) > MAX_NAME_CHARS:
        raise BoardError(400, f"give the agent a name of 1 to {MAX_NAME_CHARS} characters")
    display_name = display_name.strip()
    agent_id = slugify(display_name)
    if not agent_id:
        raise BoardError(400, "the name needs at least one letter or digit")
    if conn.execute("SELECT 1 FROM principals WHERE id = ?", (agent_id,)).fetchone() or \
            conn.execute("SELECT 1 FROM invites WHERE agent_id = ? AND replaces_token = 0 "
                         "AND status = 'open' AND expires_at > ?", (agent_id, _now())).fetchone():
        raise BoardError(409, f"the agent id '{agent_id}' is already taken or has an open invite")
    if not isinstance(lanes, dict):
        raise BoardError(400, "lanes must be an object")
    wanted = {}
    for slug, rights_ in lanes.items():
        lane = known_lane(conn, caller, slug)  # 404 for lanes the caller can't see
        if not administers(caller, lane):
            raise BoardError(403, "you do not administer this lane")
        if not isinstance(rights_, dict):
            raise BoardError(400, "each lane needs {\"read\": bool, \"post\": bool}")
        entry = {"read": rights_.get("read", False), "post": rights_.get("post", False)}
        if not all(isinstance(v, bool) for v in entry.values()):
            raise BoardError(400, "read and post must be true or false")
        if entry["read"] or entry["post"]:
            wanted[slug] = entry
    return _new_invite(conn, caller, agent_id, display_name, wanted, replaces_token=False)


def create_reconnect_invite(conn, caller, principal_id):
    """A link that gives one of the caller's active agents a new token and
    changes nothing else. Revoked agents cannot be reconnected."""
    _require_admin(caller)
    row = conn.execute("SELECT * FROM principals WHERE id = ? AND kind = 'agent' AND owner = ? "
                       "AND status = 'active'", (principal_id, caller.id)).fetchone()
    if row is None:
        raise not_found()
    return _new_invite(conn, caller, principal_id, row["display_name"], {}, replaces_token=True)


def cancel_invite(conn, caller, invite_id):
    _require_admin(caller)
    if invite_id not in INT64:
        raise not_found()
    done = conn.execute("UPDATE invites SET status = 'cancelled' WHERE id = ? AND owner = ? "
                        "AND status = 'open'", (invite_id, caller.id)).rowcount
    if not done:
        raise not_found()
    _audit(conn, caller.id, "invite-cancel", caller.id, invite_id=invite_id)


def list_invites(conn, caller):
    """The caller's own open invites. Codes are never listed: only hashes exist."""
    _require_admin(caller)
    rows = conn.execute("SELECT * FROM invites WHERE owner = ? AND status = 'open' "
                        "AND expires_at > ? ORDER BY id", (caller.id, _now())).fetchall()
    return [{"id": r["id"], "agent_id": r["agent_id"], "display_name": r["display_name"],
             "kind": "reconnect" if r["replaces_token"] else "new",
             "lanes": json.loads(r["lanes_json"]), "created_at": r["created_at"],
             "expires_at": r["expires_at"]} for r in rows]


GONE = "this invite link has expired, was already used, or was cancelled"


def invite_preview(conn, code):
    """What the invite page shows. Changes nothing, so previews never use a link up."""
    row = conn.execute("SELECT * FROM invites WHERE code_hash = ?",
                       (_hash_token(code or ""),)).fetchone()
    if row is None:
        raise not_found()
    if row["status"] != "open" or row["expires_at"] <= _now():
        raise BoardError(410, GONE)
    # Rule 7: the page is open to anyone holding the link, so it names shared
    # lanes only and counts the private ones; the agent learns them on redemption.
    shared, private = {}, 0
    for slug, want in json.loads(row["lanes_json"]).items():
        lane = _lane(conn, slug)
        if lane is not None and lane["scope"] == "shared":
            shared[slug] = want
        else:
            private += 1
    return {"agent_id": row["agent_id"], "display_name": row["display_name"],
            "owner": row["owner"], "reconnect": bool(row["replaces_token"]),
            "shared_lanes": shared, "private_lane_count": private,
            "expires_at": row["expires_at"]}


def redeem_invite(conn, code, ip=None):
    """Spend an invite: claim it, create the agent (or replace its token), apply
    the lanes, and return the token once. All or nothing: the claim, the agent
    and the grants commit together, so any failure leaves the invite open and
    nothing behind. The claim is one atomic UPDATE, so concurrent redemptions
    produce one winner and the rest get 410."""
    row = conn.execute("SELECT * FROM invites WHERE code_hash = ?",
                       (_hash_token(code or ""),)).fetchone()
    if row is None:
        raise not_found()
    try:
        claimed = conn.execute(
            "UPDATE invites SET status = 'redeemed', redeemed_at = ?, redeemed_ip = ? "
            "WHERE id = ? AND status = 'open' AND expires_at > ?",
            (now_iso(), ip, row["id"], _now())).rowcount
        if not claimed:
            raise BoardError(410, GONE)
        owner = row["owner"]
        if conn.execute("SELECT 1 FROM admins WHERE name = ?", (owner,)).fetchone() is None:
            raise BoardError(410, "the admin who made this invite is gone")
        admin = Caller(owner, "human", owner)
        if row["replaces_token"]:
            # Checked again now: the agent may have been revoked since the link was made.
            if conn.execute("SELECT 1 FROM principals WHERE id = ? AND kind = 'agent' AND "
                            "owner = ? AND status = 'active'",
                            (row["agent_id"], owner)).fetchone() is None:
                raise BoardError(410, "this agent is no longer active; ask for a new invite")
            token = issue_agent_token(conn, row["agent_id"])
            conn.execute("UPDATE invites SET status = 'cancelled' WHERE agent_id = ? AND "
                         "replaces_token = 1 AND status = 'open' AND id != ?",
                         (row["agent_id"], row["id"]))
        else:
            token = register_agent(conn, admin, row["agent_id"], row["display_name"])["token"]
        pending = []
        for slug, want in json.loads(row["lanes_json"]).items():
            result = set_grant(conn, admin, slug, row["agent_id"], want["read"], want["post"])
            if result["status"] != "applied":
                pending.append({"lane": slug, "status": result["status"],
                                "pending_id": result["pending_id"]})
        _audit(conn, row["agent_id"], "invite-redeem", owner, invite_id=row["id"], ip=ip,
               kind="reconnect" if row["replaces_token"] else "new")
        lanes = me(conn, Caller(row["agent_id"], "agent", owner))["lanes"]
    except BaseException:
        conn.rollback()
        raise
    return {"agent_id": row["agent_id"], "token": token, "lanes": lanes, "pending": pending,
            "reconnect": bool(row["replaces_token"])}


def list_principals(conn, caller):
    """All principals, so admins can grant on shared lanes. Never lane membership."""
    _require_admin(caller)
    rows = conn.execute("SELECT id, kind, owner, display_name, status, created_at "
                        "FROM principals ORDER BY owner, kind, id").fetchall()
    return [dict(r) for r in rows]


def list_agents(conn, caller):
    """Every agent, for the admin page. `lanes` holds only the lanes the caller
    can see (their own private lanes and the shared ones), so another admin's
    private lanes and the grants on them never appear. Another admin's agent is
    listed by id and owner, as list_principals does."""
    _require_admin(caller)
    visible = {l["id"]: l["slug"] for l in visible_lanes(conn, caller)}
    grants = {}
    for g in conn.execute("SELECT lane_id, principal_id, can_read, can_post FROM grants"):
        if g["lane_id"] in visible:
            grants.setdefault(g["principal_id"], []).append(
                {"slug": visible[g["lane_id"]], "read": bool(g["can_read"]),
                 "post": bool(g["can_post"])})
    rows = conn.execute("SELECT id, owner, display_name, status, last_seen_at FROM principals "
                        "WHERE kind = 'agent' ORDER BY owner, id").fetchall()
    return [{"id": r["id"], "display_name": r["display_name"], "owner": r["owner"],
             "status": r["status"],
             "connection": "connected" if r["last_seen_at"] is not None else "waiting",
             "lanes": sorted(grants.get(r["id"], []), key=lambda l: l["slug"])} for r in rows]


def list_admin_lanes(conn, caller):
    _require_admin(caller)
    out = []
    for lane in visible_lanes(conn, caller):
        grants = conn.execute("SELECT principal_id, can_read, can_post FROM grants "
                              "WHERE lane_id = ? ORDER BY principal_id", (lane["id"],)).fetchall()
        out.append({"slug": lane["slug"], "name": lane["name"], "scope": lane["scope"],
                    "notice": lane["notice"], "administers": administers(caller, lane),
                    "grants": [{"principal": g["principal_id"], "read": bool(g["can_read"]),
                                "post": bool(g["can_post"])} for g in grants]})
    return out


# --- Admin: grants and shared-lane approvals ----------------------------

def _apply_grant(conn, lane_id, principal_id, can_read, can_post):
    if can_read or can_post:
        conn.execute("INSERT INTO grants (lane_id, principal_id, can_read, can_post) "
                     "VALUES (?, ?, ?, ?) ON CONFLICT (lane_id, principal_id) DO UPDATE SET "
                     "can_read = excluded.can_read, can_post = excluded.can_post",
                     (lane_id, principal_id, int(can_read), int(can_post)))
    else:
        conn.execute("DELETE FROM grants WHERE lane_id = ? AND principal_id = ?",
                     (lane_id, principal_id))


def _stored_rights(text):
    """(read, post) from a pending row. Anything unreadable counts as no rights."""
    try:
        value = json.loads(text)
    except ValueError:
        return False, False
    return (bool(value[0]), bool(value[1])) if isinstance(value, list) and len(value) == 2 \
        else (False, False)


def _within(rights, allowed):
    """The rights in `rights` that `allowed` also has."""
    return tuple(r and a for r, a in zip(rights, allowed))


def _minus(rights, other):
    """The rights in `rights` that `other` lacks."""
    return tuple(r and not o for r, o in zip(rights, other))


def _beyond(wanted, allowed):
    """Whether `wanted` holds a right that `allowed` does not."""
    return any(w and not a for w, a in zip(wanted, allowed))


def _open_changes(conn, lane_id, principal_id, after_id=0):
    return conn.execute("SELECT * FROM pending WHERE lane_id = ? AND principal_id = ? AND id > ? "
                        "AND status IN ('pending', 'provisional') ORDER BY id",
                        (lane_id, principal_id, after_id)).fetchall()


def _open_change(conn, actor, requested_by, lane, principal_id, before, after, status):
    cur = conn.execute(
        "INSERT INTO pending (lane_id, principal_id, before_json, after_json, requested_by, "
        "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (lane["id"], principal_id, json.dumps(before), json.dumps(after), requested_by, status,
         now_iso()))
    _audit(conn, actor, f"grant-{status}", "shared", pending_id=cur.lastrowid, lane=lane["slug"],
           principal=principal_id, before=before, after=after)
    return cur.lastrowid


def _close_changes(conn, caller, lane, principal_id, ids):
    """Close these open changes as `superseded`, and log it."""
    if ids:
        conn.executemany("UPDATE pending SET status = 'superseded', resolved_by = ?, "
                         "resolved_at = ? WHERE id = ?", [(caller.id, now_iso(), i) for i in ids])
        _audit(conn, caller.id, "superseded", "shared", lane=lane["slug"],
               principal=principal_id, pending_ids=ids)


def _cut_back(conn, caller, lane, principal_id, rows, rewrite):
    """Rewrite the (before, after) of these open changes with `rewrite`. A change
    left asking for nothing new is closed as `superseded` (it can't be approved or
    rejected any more); the others are logged as `narrowed`."""
    closed, cut = [], []
    for row in rows:
        old = (_stored_rights(row["before_json"]), _stored_rights(row["after_json"]))
        new = rewrite(row, *old)
        if new == old:
            continue
        if not _beyond(new[1], new[0]):
            closed.append(row["id"])
        else:
            conn.execute("UPDATE pending SET before_json = ?, after_json = ? WHERE id = ?",
                         (json.dumps(new[0]), json.dumps(new[1]), row["id"]))
            cut.append(row["id"])
    _close_changes(conn, caller, lane, principal_id, closed)
    if cut:
        _audit(conn, caller.id, "narrowed", "shared", lane=lane["slug"],
               principal=principal_id, pending_ids=cut)


def _narrow_open_changes(conn, caller, lane, principal_id, allowed):
    """An admin just set this grant directly to `allowed`, no wider than it was.
    That settles what the agent may hold, so every open change for this agent and
    lane is cut back to it: it can no longer ask for, or hand back, a right outside
    `allowed`. A change left asking for nothing new is closed as `superseded`, so
    a removal sticks and can't be approved or rejected back into place."""
    _cut_back(conn, caller, lane, principal_id, _open_changes(conn, lane["id"], principal_id),
              lambda row, before, after: (_within(before, allowed), _within(after, allowed)))


def _forget_dropped(conn, caller, lane, principal_id, dropped, except_id):
    """An approval just took `dropped` rights away. No other open change may hand
    them back: they are struck from what those changes started from, and a
    provisional change stops claiming them."""
    keep = tuple(not d for d in dropped)
    rows = [r for r in _open_changes(conn, lane["id"], principal_id) if r["id"] != except_id]
    _cut_back(conn, caller, lane, principal_id, rows,
              lambda row, before, after: (_within(before, keep),
                                          _within(after, keep) if row["status"] == "provisional"
                                          else after))


def set_grant(conn, caller, slug, principal_id, can_read, can_post):
    """Returns {"status": "applied" | "pending" | "provisional", ...}. See DESIGN.md §6."""
    _require_admin(caller)
    lane = known_lane(conn, caller, slug)
    if not administers(caller, lane):
        raise BoardError(403, "you do not administer this lane")
    if not conn.execute("SELECT 1 FROM principals WHERE id = ?", (principal_id,)).fetchone():
        raise not_found()
    if not (isinstance(can_read, bool) and isinstance(can_post, bool)):
        raise BoardError(400, "can_read and can_post must be true or false")
    before = _grant(conn, lane["id"], principal_id)
    after = (can_read, can_post)
    detail = {"lane": slug, "principal": principal_id, "before": before, "after": after}

    widens = _beyond(after, before)
    others = other_admins(conn, caller.id)
    if lane["scope"] != "shared" or not widens or not others:
        # Private lanes are the owner's call; narrowing a shared lane is any admin's;
        # a sole admin has nobody to ask.
        _apply_grant(conn, lane["id"], principal_id, *after)
        _audit(conn, caller.id, "grant", lane["scope"], **detail)
        if lane["scope"] == "shared" and not widens:
            _narrow_open_changes(conn, caller, lane, principal_id, after)
        return {"status": "applied"}

    provisional = not any(is_onboarded(conn, a) for a in others)
    status = "provisional" if provisional else "pending"
    pending_id = _open_change(conn, caller.id, caller.id, lane, principal_id, before, after, status)
    if provisional:
        _apply_grant(conn, lane["id"], principal_id, *after)
    return {"status": status, "pending_id": pending_id}


def list_pending(conn, caller):
    _require_admin(caller)
    rows = conn.execute(
        "SELECT p.*, l.slug FROM pending p JOIN lanes l ON l.id = p.lane_id "
        "WHERE p.status IN ('pending', 'provisional') ORDER BY p.id").fetchall()
    # A request that is not in effect is shown against what the agent holds now,
    # not what it held when asked; a change already in effect, against what it replaced.
    return [{"id": r["id"], "lane": r["slug"], "principal": r["principal_id"],
             "before": _grant(conn, r["lane_id"], r["principal_id"]) if r["status"] == "pending"
             else _stored_rights(r["before_json"]),
             "after": _stored_rights(r["after_json"]),
             "requested_by": r["requested_by"], "status": r["status"],
             "upgrade_reason": r["upgrade_reason"], "created_at": r["created_at"]}
            for r in rows]


def _reject_provisional(conn, caller, row):
    """Undo a provisional change, which is in effect. Never leaves rights wider
    than they should be, and never restores any unless the grant is still what
    this change made it. Later open changes were built on this one, so none of
    their rights stand until decided: they become ordinary pending requests. One
    left asking for nothing new is closed as `superseded`, and so is one no admin
    asked for (raised by the upgrade, or whose requester isn't an admin): its
    rights go with this rejection rather than wait on one admin's approval."""
    before = _stored_rights(row["before_json"])
    after = _stored_rights(row["after_json"])
    current = _grant(conn, row["lane_id"], row["principal_id"])
    later = _open_changes(conn, row["lane_id"], row["principal_id"], after_id=row["id"])
    if later:
        target = tuple(c and b for c, b in zip(current, before))
        admins = set(list_admins(conn))
        keep = [r for r in later if _beyond(_stored_rights(r["after_json"]), target)
                and r["upgrade_reason"] is None and r["requested_by"] in admins]
        conn.executemany("UPDATE pending SET status = 'pending', before_json = ? WHERE id = ?",
                         [(json.dumps(target), r["id"]) for r in keep])
        lane = conn.execute("SELECT * FROM lanes WHERE id = ?", (row["lane_id"],)).fetchone()
        kept = {r["id"] for r in keep}
        _close_changes(conn, caller, lane, row["principal_id"],
                       [r["id"] for r in later if r["id"] not in kept])
    elif current == after:
        target = before
    else:  # a later decision changed it: drop only what this change added
        target = tuple(c and (b or not a) for c, b, a in zip(current, before, after))
    _apply_grant(conn, row["lane_id"], row["principal_id"], *target)
    return target


def resolve_pending(conn, caller, pending_id, approve):
    _require_admin(caller)
    if pending_id not in INT64:
        raise not_found()
    row = conn.execute("SELECT * FROM pending WHERE id = ?", (pending_id,)).fetchone()
    if row is None:
        raise not_found()
    if row["status"] not in ("pending", "provisional"):
        raise BoardError(404, "this change was already decided or replaced")
    if row["requested_by"] == caller.id:
        raise BoardError(403, "another admin must approve or reject this change")
    if row["status"] == "pending":
        new_status = "approved" if approve else "rejected"
        if approve:
            wanted = _stored_rights(row["after_json"])
            dropped = _minus(_grant(conn, row["lane_id"], row["principal_id"]), wanted)
            _apply_grant(conn, row["lane_id"], row["principal_id"], *wanted)
            if any(dropped):
                lane = conn.execute("SELECT * FROM lanes WHERE id = ?", (row["lane_id"],)).fetchone()
                _forget_dropped(conn, caller, lane, row["principal_id"], dropped, row["id"])
    else:
        new_status = "ratified" if approve else "rejected"
        if not approve:
            _reject_provisional(conn, caller, row)
    conn.execute("UPDATE pending SET status = ?, resolved_by = ?, resolved_at = ? WHERE id = ?",
                 (new_status, caller.id, now_iso(), pending_id))
    _audit(conn, caller.id, new_status, "shared", pending_id=pending_id)
    return {"id": pending_id, "status": new_status}


def list_audit(conn, caller, limit=200):
    _require_admin(caller)
    rows = conn.execute("SELECT * FROM audit_log WHERE scope IN (?, 'shared') "
                        "ORDER BY id DESC LIMIT ?", (caller.id, limit)).fetchall()
    return [{"ts": r["ts"], "actor": r["actor"], "action": r["action"],
             "detail": json.loads(r["detail"])} for r in rows]

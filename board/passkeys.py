"""Passkeys (decisions/0008): a second way for an admin to prove who they are,
alongside password and authenticator code, which keep working everywhere.

A passkey alone is enough, because user verification is required: the device checks
Face ID, Touch ID or its PIN before it signs. The relying party is the board's own
host, so a lookalike site can't get an answer that works here.

Adding a passkey needs the full login again (password and a current code, through
service._prove), so an open session alone can't plant a way back in. A failed passkey
check counts towards the same (admin, address) lock as a failed password. Challenges
are single use, hashed, and last five minutes. Only public keys are stored.
"""

import json
from urllib.parse import urlsplit

from webauthn import (generate_authentication_options, generate_registration_options,
                      options_to_json, verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (AttestationConveyancePreference,
                                      AuthenticatorSelectionCriteria,
                                      PublicKeyCredentialDescriptor, ResidentKeyRequirement,
                                      UserVerificationRequirement)

from board import service
from board.db import now_iso
from board.service import BoardError

PASSKEY_SECONDS = 300
MAX_PASSKEYS = 10
MAX_LABEL_CHARS = 60
MAX_CREDENTIAL_CHARS = 16384  # a browser's credential JSON is a few KB at most
RP_NAME = "FuzzPan"


def relying_party(public_base):
    """(rp_id, origin) for the board at `public_base` (BOARD_PUBLIC_URL, else the
    request's address). A passkey made under one address won't work under another."""
    url = urlsplit(public_base)
    return url.hostname, f"{url.scheme}://{url.netloc}"


def _hash(challenge):
    return service._hash_token(bytes_to_base64url(challenge))


def _new_challenge(conn, purpose, challenge, admin=None, session=None):
    now = service._now()
    conn.execute("DELETE FROM passkey_challenges WHERE expires_at <= ?", (now,))
    conn.execute("INSERT INTO passkey_challenges (challenge_hash, purpose, admin, "
                 "session_hash, expires_at) VALUES (?, ?, ?, ?, ?)",
                 (_hash(challenge), purpose, admin,
                  service._hash_token(session) if session else None,
                  now + PASSKEY_SECONDS))


def _parse(credential_json):
    """The credential as a dict and the challenge its client data names, or
    (None, None) when it is not something a browser would send."""
    if not isinstance(credential_json, str) or len(credential_json) > MAX_CREDENTIAL_CHARS:
        return None, None
    try:
        credential = json.loads(credential_json)
        client_data = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
        challenge = base64url_to_bytes(client_data["challenge"])
        if not isinstance(credential.get("id"), str):
            return None, None
        return credential, challenge
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, None


def _claim(conn, challenge, purpose, admin=None, session=None):
    """Spend the challenge: true once, for the purpose (and, when registering, the
    admin and browser session) it was made for, while it is live."""
    if purpose == "register":
        return conn.execute(
            "DELETE FROM passkey_challenges WHERE challenge_hash = ? AND purpose = ? "
            "AND admin = ? AND session_hash = ? AND expires_at > ?",
            (_hash(challenge), purpose, admin, service._hash_token(session or ""),
             service._now())).rowcount == 1
    return conn.execute(
        "DELETE FROM passkey_challenges WHERE challenge_hash = ? AND purpose = ? "
        "AND expires_at > ?", (_hash(challenge), purpose, service._now())).rowcount == 1


# --- Adding and removing, from the Account page ------------------------------

def list_passkeys(conn, caller):
    service._require_admin(caller)
    rows = conn.execute("SELECT credential_id, label, created_at, last_used_at FROM passkeys "
                        "WHERE admin = ? ORDER BY created_at", (caller.id,)).fetchall()
    return [dict(r) for r in rows]


def start_registration(conn, caller, session, password, code, rp_id, client="unknown"):
    """After the full login again (password and a current code), make a registration
    challenge for this admin and this browser session. Returns the options for the
    browser as JSON, or None if the proof failed (counted like a failed login)."""
    service._require_admin(caller)
    count = conn.execute("SELECT COUNT(*) FROM passkeys WHERE admin = ?",
                         (caller.id,)).fetchone()[0]
    if count >= MAX_PASSKEYS:
        raise BoardError(400, f"You already have {MAX_PASSKEYS} passkeys. Remove one first.")
    if not service._prove(conn, caller.id, password, code, client, logging_in=False):
        return None
    existing = conn.execute("SELECT credential_id FROM passkeys WHERE admin = ?",
                            (caller.id,)).fetchall()
    options = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_name=caller.id, user_id=caller.id.encode(),
        user_display_name=caller.id, timeout=PASSKEY_SECONDS * 1000,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(r[0]))
                             for r in existing])
    _new_challenge(conn, "register", options.challenge, caller.id, session)
    return options_to_json(options)


def finish_registration(conn, caller, session, credential_json, label, rp_id, origin):
    """Check the browser's new credential against the challenge this session was
    given, and store its public key. Raises BoardError with a reason a person can
    act on; nothing is stored unless every check passes."""
    service._require_admin(caller)
    label = " ".join(str(label or "").split())[:MAX_LABEL_CHARS] or "Passkey"
    credential, challenge = _parse(credential_json)
    if credential is None or not _claim(conn, challenge, "register", caller.id, session):
        raise BoardError(410, "That passkey request expired or was not started here. "
                              "Start again.")
    try:
        verified = verify_registration_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
            expected_origin=origin, require_user_verification=True)
    except WebAuthnException:
        raise BoardError(400, "The passkey could not be checked. Nothing was saved; "
                              "try again.") from None
    credential_id = bytes_to_base64url(verified.credential_id)
    if conn.execute("SELECT 1 FROM passkeys WHERE credential_id = ?",
                    (credential_id,)).fetchone():
        raise BoardError(409, "That passkey is already saved.")
    if conn.execute("SELECT COUNT(*) FROM passkeys WHERE admin = ?",
                    (caller.id,)).fetchone()[0] >= MAX_PASSKEYS:
        raise BoardError(400, f"You already have {MAX_PASSKEYS} passkeys. Remove one first.")
    conn.execute("INSERT INTO passkeys (credential_id, admin, public_key, sign_count, label, "
                 "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (credential_id, caller.id, verified.credential_public_key,
                  verified.sign_count, label, service._now()))
    service._audit(conn, caller.id, "passkey-added", "shared", admin=caller.id, label=label)
    return label


def remove_passkey(conn, caller, credential_id):
    """Remove one of the caller's own passkeys. Needs only the session: it can only
    take access away, and password and code still work."""
    service._require_admin(caller)
    row = conn.execute("SELECT label FROM passkeys WHERE credential_id = ? AND admin = ?",
                       (credential_id, caller.id)).fetchone()
    if row is None:
        raise BoardError(404, "no such passkey")
    conn.execute("DELETE FROM passkeys WHERE credential_id = ?", (credential_id,))
    service._audit(conn, caller.id, "passkey-removed", "shared", admin=caller.id,
                   label=row["label"])


# --- Signing in ---------------------------------------------------------------

def login_options(conn, rp_id):
    """A login challenge. No username: the passkey on the device names the admin."""
    options = generate_authentication_options(
        rp_id=rp_id, timeout=PASSKEY_SECONDS * 1000,
        user_verification=UserVerificationRequirement.REQUIRED)
    _new_challenge(conn, "login", options.challenge)
    return options_to_json(options)


def prove(conn, credential_json, rp_id, origin, client, logging_in):
    """The admin a passkey assertion proves, or None. Every failure looks the same.
    The challenge is spent whatever happens. A failure for a known passkey counts
    towards that (admin, address) lock, as a wrong password does; a locked pair is
    refused without counting. Success clears the pair and, when logging in, sets
    last_login_at (which onboards a new admin, as a password login does)."""
    credential, challenge = _parse(credential_json)
    if credential is None or not _claim(conn, challenge, "login"):
        return None
    row = conn.execute("SELECT * FROM passkeys WHERE credential_id = ?",
                       (credential["id"],)).fetchone()
    if row is None:
        return None
    admin, now = row["admin"], service._now()
    if conn.execute("SELECT 1 FROM login_pairs WHERE username = ? AND client = ? "
                    "AND locked_until > ?", (admin, client, now)).fetchone():
        return None
    try:
        verified = verify_authentication_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
            expected_origin=origin, credential_public_key=row["public_key"],
            credential_current_sign_count=row["sign_count"], require_user_verification=True)
        handle = credential["response"].get("userHandle")
        if handle and base64url_to_bytes(handle) != admin.encode():
            raise WebAuthnException("user handle does not match")
    except (WebAuthnException, ValueError, TypeError):
        service._record_login_failure(conn, admin, client, now)
        return None
    # Conditional on the count we checked, so a replayed assertion racing this one loses.
    if not conn.execute("UPDATE passkeys SET sign_count = ?, last_used_at = ? "
                        "WHERE credential_id = ? AND sign_count = ?",
                        (verified.new_sign_count, now, row["credential_id"],
                         row["sign_count"])).rowcount:
        return None
    if logging_in:
        conn.execute("UPDATE admins SET last_login_at = ? WHERE name = ?", (now_iso(), admin))
    conn.execute("DELETE FROM login_pairs WHERE username = ? AND client = ?", (admin, client))
    return admin


def login(conn, credential_json, rp_id, origin, client="unknown"):
    """A session token for the admin the passkey proves, or None."""
    admin = prove(conn, credential_json, rp_id, origin, client, logging_in=True)
    return service._new_session(conn, admin, service._now()) if admin else None

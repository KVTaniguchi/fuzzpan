"""Passkeys alongside password and code (decisions/0008, DESIGN §5.3).

A software authenticator stands in for a phone: a P-256 key that builds the same
attestation and assertion a browser would hand the page."""

import base64
import hashlib
import html
import json
import re
import secrets

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from board import service
from test_remote_mcp import authorize_fields, call, choose_lanes, code_from, pkce, spend, \
    ticket_of

ORIGIN = "https://testserver"  # the test client's address, so the relying party id
RP_ID = "testserver"           # and origin the board expects


@pytest.fixture(autouse=True)
def no_public_url(monkeypatch):
    monkeypatch.delenv("BOARD_PUBLIC_URL", raising=False)


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SoftKey:
    """A passkey on a pretend device. `count` is its signature counter."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = secrets.token_bytes(16)
        self.count = 0
        self.user_handle = None

    def _auth_data(self, rp_id, uv, attested=b""):
        flags = 0x01 | (0x04 if uv else 0) | (0x40 if attested else 0)
        return (hashlib.sha256(rp_id.encode()).digest() + bytes([flags])
                + self.count.to_bytes(4, "big") + attested)

    def _client_data(self, kind, challenge, origin):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin,
                           "crossOrigin": False}).encode()

    def create(self, options_json, origin=ORIGIN, rp_id=None, uv=True):
        o = json.loads(options_json)
        self.user_handle = o["user"]["id"]
        n = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"),
                            -3: n.y.to_bytes(32, "big")})
        attested = bytes(16) + len(self.id).to_bytes(2, "big") + self.id + cose
        auth = self._auth_data(rp_id or o["rp"]["id"], uv, attested)
        return json.dumps({
            "id": b64(self.id), "rawId": b64(self.id), "type": "public-key",
            "response": {
                "clientDataJSON": b64(self._client_data("webauthn.create", o["challenge"],
                                                        origin)),
                "attestationObject": b64(cbor2.dumps({"fmt": "none", "attStmt": {},
                                                      "authData": auth}))},
            "clientExtensionResults": {}})

    def get(self, options_json, origin=ORIGIN, rp_id=None, uv=True, step=1):
        o = json.loads(options_json)
        self.count += step
        auth = self._auth_data(rp_id or o["rpId"], uv)
        client_data = self._client_data("webauthn.get", o["challenge"], origin)
        signature = self.key.sign(auth + hashlib.sha256(client_data).digest(),
                                  ec.ECDSA(hashes.SHA256()))
        return json.dumps({
            "id": b64(self.id), "rawId": b64(self.id), "type": "public-key",
            "response": {"clientDataJSON": b64(client_data), "authenticatorData": b64(auth),
                         "signature": b64(signature), "userHandle": self.user_handle},
            "clientExtensionResults": {}})


def start_adding(board, name="alice", http=None, label="Phone", code=None, password=None):
    """Submit Account's add-a-passkey form (password and a fresh code)."""
    http = http or board.admin(name)
    board.clock.advance(60)
    return http.post("/account/passkeys", data={
        "label": label, "password": password or board.passwords[name],
        "code": code or board.code(name)})


def options_on(page):
    m = re.search(r'data-options="([^"]+)"', page.text)
    assert m, page.text
    return html.unescape(m.group(1))


def add_passkey(board, name="alice", key=None, label="Phone"):
    key = key or SoftKey()
    http = board.admin(name)
    page = start_adding(board, name, http, label)
    assert page.status_code == 200, page.text
    r = http.post("/account/passkeys/finish", follow_redirects=False,
                  data={"credential": key.create(options_on(page)), "label": label})
    assert r.status_code == 303 and r.headers["location"] == "/account?done=passkey", r.text
    return key


def passkey_login(board, key, ip=None, **how):
    c = board.anon(ip=ip)
    options = c.post("/passkey/options")
    assert options.status_code == 200, options.text
    r = c.post("/login/passkey", data={"credential": key.get(options.text, **how)},
               follow_redirects=False)
    return c, r


# --- Adding ------------------------------------------------------------------

def test_adding_needs_the_password_and_a_current_code(board):
    r = start_adding(board, code="000000")
    assert r.status_code == 401 and "data-options" not in r.text
    assert board.q("SELECT COUNT(*) FROM passkey_challenges")[0][0] == 0
    # Counted like a failed login, for the same lock.
    assert board.q("SELECT failures FROM login_pairs WHERE username = 'alice'")[0][0] == 1
    r = start_adding(board, password="wrong-password-123")
    assert r.status_code == 401


def test_add_then_log_in_with_only_the_passkey(board):
    key = add_passkey(board, label="Kitchen iPad")
    rows = board.q("SELECT admin, label, public_key FROM passkeys")
    assert rows[0][0] == "alice" and rows[0][1] == "Kitchen iPad" and rows[0][2]
    assert board.q("SELECT scope FROM audit_log WHERE action = 'passkey-added'") == [("shared",)]

    c, r = passkey_login(board, key)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/account").status_code == 200
    assert "Kitchen iPad" in c.get("/account").text
    assert board.q("SELECT last_used_at FROM passkeys")[0][0] is not None


def test_password_and_code_keep_working_alongside(board):
    add_passkey(board)
    board.clock.advance(60)
    assert board.login_response("alice").status_code == 303


def test_the_registration_challenge_belongs_to_the_session_that_asked(board):
    page = start_adding(board)
    other = board.anon()
    board.clock.advance(60)
    assert board.login("alice", board.code("alice"), http=other).status_code == 303
    r = other.post("/account/passkeys/finish",
                   data={"credential": SoftKey().create(options_on(page)), "label": "x"})
    assert r.status_code == 410
    assert board.q("SELECT COUNT(*) FROM passkeys")[0][0] == 0


def test_registration_needs_user_verification_and_the_right_origin(board):
    for how in ({"uv": False}, {"origin": "https://evil.example"},
                {"rp_id": "evil.example"}):
        page = start_adding(board)
        r = board.admin("alice").post("/account/passkeys/finish",
                                      data={"credential": SoftKey().create(options_on(page),
                                                                           **how),
                                            "label": "x"})
        assert r.status_code == 400, how
    assert board.q("SELECT COUNT(*) FROM passkeys")[0][0] == 0


def test_the_label_is_shown_escaped(board):
    add_passkey(board, label="<b>Phone</b>")
    page = board.admin("alice").get("/account").text
    assert "<b>Phone</b>" not in page and "&lt;b&gt;Phone&lt;/b&gt;" in page


def test_removing_a_passkey_stops_it_at_once(board):
    key = add_passkey(board)
    cred = board.q("SELECT credential_id FROM passkeys")[0][0]
    assert board.admin("bob").post("/account/passkeys/remove",
                                   data={"credential_id": cred}).status_code == 404
    r = board.admin("alice").post("/account/passkeys/remove", data={"credential_id": cred},
                                  follow_redirects=False)
    assert r.status_code == 303
    assert passkey_login(board, key)[1].status_code == 401
    assert board.q("SELECT scope FROM audit_log WHERE action = 'passkey-removed'") == \
        [("shared",)]


def test_reset_clears_passkeys(board):
    key = add_passkey(board)
    with service.connect(board.app.state.db_path) as conn:
        service.reset_admin(conn, "alice")
    assert board.q("SELECT COUNT(*) FROM passkeys")[0][0] == 0
    assert passkey_login(board, key)[1].status_code == 401


# --- Signing in --------------------------------------------------------------

def test_login_options_ask_for_user_verification_and_name_this_board(board):
    o = board.anon().post("/passkey/options").json()
    assert o["rpId"] == RP_ID and o["userVerification"] == "required"
    assert not o.get("allowCredentials")


def test_the_relying_party_follows_the_public_url(board, monkeypatch):
    monkeypatch.setenv("BOARD_PUBLIC_URL", "https://board.example.org/")
    assert board.anon().post("/passkey/options").json()["rpId"] == "board.example.org"


@pytest.mark.parametrize("how", [{"uv": False}, {"origin": "https://evil.example"},
                                 {"rp_id": "evil.example"}])
def test_a_bad_assertion_is_refused(board, how):
    key = add_passkey(board)
    _, r = passkey_login(board, key, **how)
    assert r.status_code == 401


def test_a_signature_counter_that_goes_backwards_is_refused(board):
    key = add_passkey(board)
    assert passkey_login(board, key)[1].status_code == 303  # counter now 1
    assert passkey_login(board, key, step=0)[1].status_code == 401  # a cloned key


def test_a_device_that_never_counts_still_works(board):
    key = add_passkey(board)  # many phone passkeys always report 0
    for _ in range(2):
        assert passkey_login(board, key, step=0)[1].status_code == 303


def test_a_challenge_works_once(board):
    key = add_passkey(board)
    c = board.anon()
    credential = key.get(c.post("/passkey/options").text)
    assert c.post("/login/passkey", data={"credential": credential},
                  follow_redirects=False).status_code == 303
    assert board.anon().post("/login/passkey", data={"credential": credential},
                             follow_redirects=False).status_code == 401


def test_an_expired_challenge_is_refused(board):
    key = add_passkey(board)
    c = board.anon()
    options = c.post("/passkey/options").text
    board.clock.advance(301)
    assert c.post("/login/passkey", data={"credential": key.get(options)}).status_code == 401


def test_an_unknown_passkey_and_junk_get_the_same_answer(board):
    add_passkey(board)
    stranger = SoftKey()
    stranger.user_handle = b64(b"alice")
    assert passkey_login(board, stranger)[1].status_code == 401
    for junk in ("", "{}", "not json", "x" * 20000):
        assert board.anon().post("/login/passkey", data={"credential": junk}).status_code == 401


def test_failures_share_the_password_lockout(board):
    key = add_passkey(board)
    for _ in range(service.LOCK_AFTER):
        assert passkey_login(board, key, ip="203.0.113.9", uv=False)[1].status_code == 401
    # Locked here: a good passkey and a good password are both refused from this address...
    assert passkey_login(board, key, ip="203.0.113.9")[1].status_code == 401
    board.clock.advance(60)
    r = board.login("alice", board.code("alice"), http=board.anon(ip="203.0.113.9"))
    assert r.status_code == 401
    # ...but not from somewhere else.
    assert passkey_login(board, key, ip="198.51.100.4")[1].status_code == 303


def test_a_passkey_login_onboards_a_new_admin(board):
    key = add_passkey(board)
    # alice logged in to add it; clear that to see the passkey login set it.
    with service.connect(board.app.state.db_path) as conn:
        conn.execute("UPDATE admins SET last_login_at = NULL WHERE name = 'alice'")
    passkey_login(board, key)
    assert board.q("SELECT last_login_at FROM admins WHERE name = 'alice'")[0][0]


def test_the_login_page_offers_a_passkey(board):
    page = board.anon().get("/login").text
    assert 'data-passkey="login"' in page and "/static/passkey.js" in page


# --- Approving an app --------------------------------------------------------

def approve_with_passkey(board, key, challenge, agent="claude-chat"):
    c = board.anon()
    credential = key.get(c.post("/passkey/options").text)
    return c.post("/oauth/authorize", follow_redirects=False,
                  data={**authorize_fields(challenge), "agent": agent, "passkey": credential})


def test_an_app_can_be_approved_with_a_passkey(board):
    key = add_passkey(board)
    sessions = board.q("SELECT COUNT(*) FROM sessions")[0][0]
    verifier, challenge = pkce()
    page = board.anon().get("/oauth/authorize", params=authorize_fields(challenge))
    assert 'data-passkey="login"' in page.text and "just started yourself" in page.text

    step = approve_with_passkey(board, key, challenge)
    assert step.status_code == 200 and ticket_of(step)  # a new agent: the lane step
    r = choose_lanes(board, ticket_of(step), {"engineering": (True, True)})
    token = spend(board, code_from(r), verifier).json()["access_token"]
    assert board.q("SELECT owner FROM principals WHERE id = 'claude-chat'") == [("alice",)]
    posted = call(board.anon(), token, "board_post", lane="engineering", body="hi")
    assert posted["content"][0]["text"].startswith("posted #")
    # No browser session is left behind by the approval.
    assert board.q("SELECT COUNT(*) FROM sessions")[0][0] == sessions


def test_a_bad_passkey_does_not_approve(board):
    key = add_passkey(board)
    _, challenge = pkce()
    c = board.anon()
    credential = key.get(c.post("/passkey/options").text, uv=False)
    r = c.post("/oauth/authorize", follow_redirects=False,
               data={**authorize_fields(challenge), "agent": "claude-chat",
                     "passkey": credential})
    assert r.status_code == 401 and "location" not in r.headers
    assert board.q("SELECT COUNT(*) FROM oauth_approvals")[0][0] == 0
    assert board.q("SELECT COUNT(*) FROM oauth_codes")[0][0] == 0

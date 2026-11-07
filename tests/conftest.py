"""Test harness: a fresh board per test, admins logged in over HTTP, and
agents holding the bearer token the board issued them at registration."""

import os
import tempfile

# Before any board import: board.main refuses to start without BOARD_DB, and
# tests must never touch a real database.
os.environ["BOARD_DB"] = os.path.join(tempfile.mkdtemp(prefix="board-test-"), "unused.db")

import json
import re
import secrets
import time

import pyotp
import pytest
from fastapi.testclient import TestClient

from board.app import create_app
from board import service
from board.provision import provision_family

HTTPS = "https://testserver"  # the session cookie is Secure


class FrozenTime:
    """The board's clock for human login: stands still until a test advances it."""

    def __init__(self):
        self.now = time.time()

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def frozen_time(monkeypatch):
    clock = FrozenTime()
    monkeypatch.setattr(service, "_now", clock.time)
    return clock


class Agent:
    def __init__(self, app, principal_id, token):
        self.http = TestClient(app, base_url=HTTPS)  # own client: no admin cookies leak in
        self.id = principal_id
        self.token = token

    def request(self, method, path, payload=None, **header_overrides):
        body = b"" if payload is None else json.dumps(payload).encode()
        headers = {"Authorization": f"Bearer {self.token}"}
        headers.update(header_overrides)
        if body:
            headers["Content-Type"] = "application/json"
        return self.http.request(method, path, content=body, headers=headers)

    def read(self, lane, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return self.request("GET", f"/v1/lanes/{lane}/posts" + (f"?{query}" if query else ""))

    def post(self, lane, body, kind="note"):
        return self.request("POST", f"/v1/lanes/{lane}/posts", {"body": body, "kind": kind})


class Board:
    """A board whose admins enroll (setup link, password, TOTP) the first time a
    test needs them. Each login consumes its TOTP step, so enrolling moves the
    frozen clock a minute on: later codes are always fresh."""

    def __init__(self, app, tokens, clock):
        self.app = app
        self.tokens = tokens  # admin -> setup token, from provision_family
        self.clock = clock
        self.passwords = {name: secrets.token_urlsafe(16) for name in tokens}
        self._enrolled = set()
        self._admins = {}

    def setup_page(self, name):
        return TestClient(self.app, base_url=HTTPS).get(f"/setup/{self.tokens[name]}")

    def totp_secret(self, name):
        with service.connect(self.app.state.db_path) as conn:
            return conn.execute("SELECT totp_secret FROM admins WHERE name = ?",
                                (name,)).fetchone()[0]

    def _enroll(self, name):
        if name in self._enrolled:
            return
        code = pyotp.TOTP(self.totp_secret(name)).at(int(self.clock.time()))
        r = TestClient(self.app, base_url=HTTPS).post(
            f"/setup/{self.tokens[name]}",
            data={"password": self.passwords[name], "code": code}, follow_redirects=False)
        assert r.status_code == 303, r.text
        self._enrolled.add(name)
        self.clock.advance(60)

    def code_at(self, name, offset_seconds):
        self._enroll(name)
        return pyotp.TOTP(self.totp_secret(name)).at(int(self.clock.time() + offset_seconds))

    def code(self, name):
        return self.code_at(name, 0)

    def login(self, name, code, password=None, http=None):
        self._enroll(name)
        http = http or TestClient(self.app, base_url=HTTPS)
        return http.post("/login", data={"username": name, "code": code,
                                         "password": password or self.passwords[name]},
                         follow_redirects=False)

    def login_response(self, name):
        return self.login(name, self.code(name))

    def admin(self, name):
        """A logged-in admin client. Logging in marks the admin as onboarded."""
        if name not in self._admins:
            http = TestClient(self.app, base_url=HTTPS)
            r = self.login(name, self.code(name), http=http)
            assert r.status_code == 303, r.text
            self._admins[name] = http
        return self._admins[name]

    def invite(self, admin, display_name, lanes):
        """An invite code made by this admin through the admin API."""
        r = self.admin(admin).post("/v1/admin/invites",
                                   json={"display_name": display_name, "lanes": lanes})
        assert r.status_code == 201, r.text
        return r.json()["code"]

    def register(self, owner, principal_id):
        """An agent with no lanes, connected the way real agents are: an invite
        link made by the owner, redeemed for a token. The id is the slug of the name."""
        code = self.invite(owner, principal_id, {})
        r = TestClient(self.app, base_url=HTTPS).post(f"/v1/invites/{code}/redeem")
        assert r.status_code == 200, r.text
        assert r.json()["agent_id"] == principal_id
        return Agent(self.app, principal_id, r.json()["token"])

    def create_invite_via_form(self, admin, display_name, lanes=None):
        """Submit the admin page's invite form the way a browser does. `lanes`
        is {slug: {"read": bool, "post": bool}}; by default the boxes the form
        comes with preselected."""
        http = self.admin(admin)
        if lanes is None:
            form = http.get("/admin/invite").text
            lanes = {}
            for right, slug in re.findall(
                    r'<input[^>]*name="(read|post)_([a-z0-9-]+)"[^>]*\bchecked\b', form):
                lanes.setdefault(slug, {})[right] = True
        data = {"display_name": display_name}
        for slug, rights in lanes.items():
            for right, on in rights.items():
                if on:
                    data[f"{right}_{slug}"] = "on"
        return http.post("/admin/invite", data=data)

    def grant(self, admin, lane, principal_id, read=True, post=True):
        return self.admin(admin).put(f"/v1/admin/lanes/{lane}/grants/{principal_id}",
                                     json={"can_read": read, "can_post": post})


@pytest.fixture
def board(tmp_path, frozen_time):
    db_path = tmp_path / "board.db"
    tokens = provision_family(db_path, [("alice", "Alice"), ("bob", "Bob")])
    return Board(create_app(db_path), tokens, frozen_time)


def checked(html, lane, right):
    """Whether the form's `right` ("read" or "post") box for `lane` is ticked."""
    tag = re.search(rf'<input[^>]*name="{right}_{re.escape(lane)}"[^>]*>', html)
    assert tag, f"no {right} box for {lane}"
    return bool(re.search(r"\bchecked\b", tag.group(0)))

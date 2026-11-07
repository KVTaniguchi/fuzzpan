"""One anonymous request must not be able to exhaust the machine's memory: the
body is capped before anything parses it, whatever the route."""

import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from board import limits, service
from board.app import RequestSizeLimit, create_app
from board.service import MAX_BODY_CHARS, MAX_RAW_BYTES, MAX_REQUEST_BYTES

HTTPS = "https://testserver"
ROOT = Path(__file__).parent.parent
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def chunks(total, size=65536):
    """A body sent chunked: httpx gives a generator no Content-Length."""
    def gen():
        sent = 0
        while sent < total:
            piece = min(size, total - sent)
            sent += piece
            yield b"a" * piece
    return gen()


def no_parsing_allowed(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the request reached the board's logic")
    monkeypatch.setattr(service, "login", boom)
    monkeypatch.setattr(limits, "allow", boom)


def test_oversized_login_is_refused_before_parsing_by_content_length(board, monkeypatch):
    no_parsing_allowed(monkeypatch)
    http = TestClient(board.app, base_url=HTTPS)
    r = http.post("/login", content=b"a=" + b"x" * (MAX_REQUEST_BYTES + 1), headers=FORM)
    assert r.status_code == 413


def test_oversized_chunked_login_is_refused_before_parsing(board, monkeypatch):
    no_parsing_allowed(monkeypatch)
    http = TestClient(board.app, base_url=HTTPS)
    r = http.post("/login", content=chunks(MAX_REQUEST_BYTES * 3), headers=FORM)
    assert r.status_code == 413
    assert "content-length" not in r.request.headers  # it really was chunked


@pytest.mark.parametrize("method,path", [
    ("POST", "/login"), ("POST", "/setup/x"), ("POST", "/logout"), ("POST", "/admin/grant"),
    ("POST", "/admin/invite"), ("POST", "/lanes/household/post"),
    ("POST", "/v1/invites/x/redeem"), ("POST", "/v1/lanes/household/posts"),
    ("POST", "/v1/admin/invites"), ("PUT", "/v1/admin/lanes/household/grants/x"),
    ("POST", "/no/such/route"), ("GET", "/healthz"),
])
def test_the_cap_covers_every_route(board, method, path):
    http = TestClient(board.app, base_url=HTTPS)
    big = b"x" * (MAX_REQUEST_BYTES + 1)
    assert http.request(method, path, content=big, headers=FORM).status_code == 413
    assert http.request(method, path, content=chunks(len(big)), headers=FORM).status_code == 413


def test_a_413_still_carries_the_security_headers(board):
    r = TestClient(board.app, base_url=HTTPS).post(
        "/v1/invites/x/redeem", content=b"x" * (MAX_REQUEST_BYTES + 1))
    assert r.status_code == 413 and r.json() == {"error": "request too large"}
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


def test_a_body_exactly_at_the_cap_gets_through_and_one_byte_over_does_not(board):
    http = TestClient(board.app, base_url=HTTPS)
    at_cap = b"x" * MAX_REQUEST_BYTES
    assert http.post("/v1/invites/nope/redeem", content=at_cap).status_code == 404
    assert http.post("/v1/invites/nope/redeem", content=chunks(MAX_REQUEST_BYTES)).status_code == 404
    assert http.post("/v1/invites/nope/redeem", content=at_cap + b"x").status_code == 413


def test_the_largest_legitimate_requests_fit(board):
    # A browser form percent-encodes: one emoji is 12 bytes on the wire, so a
    # maximum post by form is far larger than the JSON limit for agents.
    alice = board.admin("alice")
    body = "\U0001f600" * MAX_BODY_CHARS
    r = alice.post("/lanes/household/post", data={"body": body, "kind": "note"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert alice.get("/v1/lanes/household/posts").json()["posts"][-1]["body"] == body

    bot = board.register("alice", "bot")
    board.grant("alice", "engineering", "bot")
    payload = {"body": "x" * MAX_BODY_CHARS}
    payload["pad"] = "y" * (MAX_RAW_BYTES - len(json.dumps(payload)) - len(', "pad": ""'))
    assert len(json.dumps(payload)) == MAX_RAW_BYTES
    assert bot.request("POST", "/v1/lanes/engineering/posts", payload).status_code == 201


# --- Any post within MAX_BODY_CHARS gets through, however it is written ------

WRITTEN_DIFFERENTLY = {
    "newlines": "\n" * MAX_BODY_CHARS + "",
    "cjk": "\u6f22" * MAX_BODY_CHARS,
    "emoji": "\U0001f600" * MAX_BODY_CHARS,
    "quotes": '"\\' * (MAX_BODY_CHARS // 2),
    "controls": "\x01" * MAX_BODY_CHARS,
    "ascii": "x" * MAX_BODY_CHARS,
}


@pytest.fixture
def poster(board):
    bot = board.register("alice", "bot")
    board.grant("alice", "engineering", "bot")
    return bot


def stored(board, lane="engineering"):
    return board.admin("alice").get(f"/v1/lanes/{lane}/posts").json()["posts"][-1]["body"]


@pytest.mark.parametrize("kind", WRITTEN_DIFFERENTLY)
def test_a_maximum_post_gets_through_as_json_as_python_writes_it(board, poster, kind):
    text = WRITTEN_DIFFERENTLY[kind] if kind != "newlines" else "a" + "\n" * (MAX_BODY_CHARS - 1)
    r = poster.request("POST", "/v1/lanes/engineering/posts", {"body": text})  # \uXXXX escapes
    assert r.status_code == 201, (kind, len(json.dumps({"body": text})))
    assert stored(board) == text


@pytest.mark.parametrize("kind", WRITTEN_DIFFERENTLY)
def test_a_maximum_post_gets_through_as_raw_utf8_json(board, poster, kind):
    text = WRITTEN_DIFFERENTLY[kind] if kind != "newlines" else "a" + "\n" * (MAX_BODY_CHARS - 1)
    raw = json.dumps({"body": text}, ensure_ascii=False).encode()
    r = poster.http.post("/v1/lanes/engineering/posts", content=raw, headers={
        "Authorization": f"Bearer {poster.token}", "Content-Type": "application/json"})
    assert r.status_code == 201, (kind, len(raw))
    assert stored(board) == text


@pytest.mark.parametrize("kind", ["emoji", "newlines", "cjk"])
def test_a_maximum_post_gets_through_the_board_client(board, poster, kind):
    from client.board_client import BoardClient
    text = WRITTEN_DIFFERENTLY[kind] if kind != "newlines" else "a" + "\n" * (MAX_BODY_CHARS - 1)
    client = BoardClient("https://testserver", poster.token, http=poster.http)
    client.post("engineering", text)
    assert stored(board) == text


def test_one_character_more_than_the_maximum_is_still_refused(board, poster):
    r = poster.request("POST", "/v1/lanes/engineering/posts", {"body": "x" * (MAX_BODY_CHARS + 1)})
    assert r.status_code == 413
    assert poster.request("POST", "/v1/lanes/engineering/posts",
                          {"body": "\U0001f600" * (MAX_BODY_CHARS + 1)}).status_code == 413


def test_the_clients_message_for_a_413_says_the_real_limit():
    from client.board_client import REFUSALS
    assert str(MAX_BODY_CHARS) in REFUSALS[413] and "8 KB" not in REFUSALS[413]


def test_the_json_limit_for_a_post_always_fits_under_the_app_cap():
    # Twelve bytes a character, as escapes, plus the rest of the request.
    assert 12 * MAX_BODY_CHARS < MAX_RAW_BYTES <= MAX_REQUEST_BYTES


def test_a_browsers_crlf_newlines_count_once(board):
    # A textarea at maxlength 8192 whose newlines are CRLF on the wire is 8192 long.
    alice = board.admin("alice")
    text = "line\n" * (MAX_BODY_CHARS // 5)  # 8190 characters
    on_the_wire = text.replace("\n", "\r\n")
    assert len(on_the_wire) > MAX_BODY_CHARS
    r = alice.post("/lanes/household/post", data={"body": on_the_wire, "kind": "note"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert alice.get("/v1/lanes/household/posts").json()["posts"][-1]["body"] == text


def test_a_form_of_only_newlines_at_the_maximum_gets_through(board):
    alice = board.admin("alice")
    text = "a" + "\n" * (MAX_BODY_CHARS - 1)
    r = alice.post("/lanes/household/post", data={"body": text.replace("\n", "\r\n"), "kind": "note"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert alice.get("/v1/lanes/household/posts").json()["posts"][-1]["body"] == text


def test_a_form_one_character_over_is_still_refused_after_the_newlines_are_counted(board):
    alice = board.admin("alice")
    text = "a" + "\n" * MAX_BODY_CHARS  # 8193 characters
    r = alice.post("/lanes/household/post", data={"body": text.replace("\n", "\r\n"), "kind": "note"},
                   follow_redirects=False)
    assert r.status_code == 413


def test_the_per_route_json_limit_still_holds_under_the_cap(board):
    bot = board.register("alice", "bot")
    board.grant("alice", "engineering", "bot")
    r = bot.request("POST", "/v1/lanes/engineering/posts", {"body": "x", "pad": "y" * MAX_RAW_BYTES})
    assert r.status_code == 413


def test_a_chunked_request_stops_being_read_as_soon_as_it_passes_the_cap():
    reached, sent, calls = [], [], [0]

    async def inner(scope, receive, send):
        reached.append(True)

    async def receive():
        calls[0] += 1
        return {"type": "http.request", "body": b"x" * 40000, "more_body": True}

    async def send(message):
        sent.append(message)

    limited = RequestSizeLimit(inner, max_bytes=100000)
    scope = {"type": "http", "method": "POST", "path": "/login", "headers": [],
             "query_string": b"", "scheme": "https", "server": ("t", 443), "root_path": ""}
    asyncio.run(limited(scope, receive, send))
    assert not reached and calls[0] == 3
    assert sent[0]["status"] == 413


def cut_off_request(path="/login", headers=()):
    """An ASGI exchange in which the client sends part of its body and hangs up.
    Returns (messages sent back, exception raised or None)."""
    messages = [{"type": "http.request", "body": b"username=al", "more_body": True},
                {"type": "http.disconnect"}]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    return sent, receive, send, {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "scheme": "https", "server": ("testserver", 443), "client": ("203.0.113.9", 50000),
        "headers": [(b"host", b"testserver"), (b"content-type", FORM["Content-Type"].encode()),
                    (b"content-length", b"500"), *headers]}


def test_a_client_that_goes_away_mid_body_reaches_nothing():
    reached = []

    async def inner(scope, receive, send):
        reached.append(True)

    sent, receive, send, scope = cut_off_request()
    asyncio.run(RequestSizeLimit(inner, max_bytes=100000)(scope, receive, send))
    assert not reached
    assert sent[0]["status"] == 400  # something for the middleware outside to pass on


@pytest.mark.parametrize("path", ["/login", "/v1/lanes/engineering/posts", "/no/such/route"])
def test_a_client_that_hangs_up_mid_body_gets_no_traceback_through_the_whole_app(
        board, caplog, path):
    # Through the real app: its HTTP middleware raises "No response returned" when
    # the layer inside it returns without answering.
    sent, receive, send, scope = cut_off_request(path)
    with caplog.at_level("DEBUG"):
        asyncio.run(board.app(scope, receive, send))  # must not raise
    assert sent and sent[0]["status"] == 400
    assert "No response returned" not in caplog.text
    assert not [r for r in caplog.records if r.exc_info]
    headers = dict(sent[0]["headers"])
    assert headers[b"x-content-type-options"] == b"nosniff"  # still through the hardening layer


def test_hanging_up_mid_body_does_not_stop_the_next_request(board):
    sent, receive, send, scope = cut_off_request()
    asyncio.run(board.app(scope, receive, send))
    assert TestClient(board.app, base_url=HTTPS).get("/healthz").json() == {"status": "ok"}


def test_a_small_chunked_body_reaches_the_route_whole(board):
    bot = board.register("alice", "bot")
    board.grant("alice", "engineering", "bot")
    body = json.dumps({"body": "in pieces"}).encode()
    pieces = (body[i:i + 3] for i in range(0, len(body), 3))
    r = bot.http.post("/v1/lanes/engineering/posts", content=pieces,
                      headers={"Authorization": f"Bearer {bot.token}"})
    assert r.status_code == 201
    assert bot.read("engineering").json()["posts"][0]["body"] == "in pieces"


def test_caddy_caps_bodies_no_lower_than_the_app_needs():
    caddyfile = (ROOT / "Caddyfile").read_text()
    match = re.search(r"request_body\s*\{\s*max_size\s+(\d+)KB\s*\}", caddyfile)
    assert match, "the Caddyfile must limit request bodies"
    assert int(match.group(1)) * 1000 >= MAX_REQUEST_BYTES


def test_the_smoke_script_checks_the_body_limit():
    assert "413" in (ROOT / "scripts/smoke_compose.sh").read_text()


def run_code_of(curl_body):
    """The smoke script's code_of, run under set -e with a stand-in curl."""
    script = (ROOT / "scripts/smoke_compose.sh").read_text()
    function = re.search(r"^code_of\(\) \{.*?^\}", script, re.S | re.M).group(0)
    bash = f"set -euo pipefail\ncurl() {{ {curl_body}; }}\n{function}\nc=\"$(code_of -X POST x)\"\necho \"got:$c\"\n"
    return subprocess.run(["bash", "-c", bash], capture_output=True, text=True)


def test_the_smoke_script_reports_a_413_even_if_curl_exits_nonzero():
    # Caddy closing mid-upload: the answer was 413 but curl exits 55.
    done = run_code_of("printf 413; return 55")
    assert done.returncode == 0 and "got:413" in done.stdout


def test_the_smoke_script_reports_no_answer_instead_of_stopping():
    done = run_code_of("printf 000; return 52")
    assert done.returncode == 0 and "got:000 (curl exit 52)" in done.stdout


def test_the_smoke_script_passes_an_ordinary_status_through():
    done = run_code_of("printf 303; return 0")
    assert done.returncode == 0 and "got:303" in done.stdout

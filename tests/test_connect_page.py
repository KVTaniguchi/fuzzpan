"""The public /connect page (onboarding roadmap item 1)."""

import re

from board.app import CSP
from tests.conftest import HTTPS


def test_anyone_can_open_it_and_it_uses_the_public_url(board, monkeypatch):
    monkeypatch.setenv("BOARD_PUBLIC_URL", "https://board.example.org/")
    r = board.anon().get("/connect", follow_redirects=False)
    assert r.status_code == 200
    html = r.text
    # Claude chat: the connector URL. Claude Code: the full command.
    assert 'value="https://board.example.org/mcp"' in html
    assert ("claude mcp add --scope user --transport http fuzzpan "
            "https://board.example.org/mcp") in html
    assert "/mcp" in html and "authenticate" in html
    # ChatGPT is marked untested; other agents are pointed at an invite link.
    assert "ChatGPT" in html and "not been tested" in html
    assert "invite link" in html


def test_without_a_public_url_it_uses_the_address_it_was_reached_at(board, monkeypatch):
    monkeypatch.delenv("BOARD_PUBLIC_URL", raising=False)
    assert f'value="{HTTPS}/mcp"' in board.anon().get("/connect").text


def test_it_names_no_lanes_and_no_agents(board):
    board.agent("alice", "quiet-helper", "engineering")
    lane_words = ["household", "trips", "money", "engineering", "health",
                  "Household & family schedule", "quiet-helper", "alice", "bob"]
    for http in (board.anon(), board.admin("alice")):
        html = http.get("/connect").text.replace("&amp;", "&")
        body = html.split("<main", 1)[1]  # the header names the logged-in admin
        for word in lane_words:
            assert word.lower() not in body.lower(), word


def test_the_csp_is_the_usual_one_and_copy_buttons_use_the_static_script(board):
    r = board.anon().get("/connect")
    assert r.headers["content-security-policy"] == CSP
    assert '<script src="/static/copy.js"' in r.text
    assert "<script>" not in r.text
    for target in re.findall(r'data-copy="([^"]+)"', r.text):
        assert f'id="{target}"' in r.text
    assert len(re.findall(r'data-copy="', r.text)) >= 2


def test_the_login_page_and_the_admin_header_link_to_it(board):
    assert 'href="/connect"' in board.anon().get("/login").text
    assert 'href="/connect"' in board.admin("alice").get("/admin").text

"""Built-in lane presets on the OAuth lane step and the invite form (decisions/0010)."""

import re

from board import service
from board.presets import presets
from tests.test_remote_mcp import (choose_lanes, code_from, lanes_of, pkce, sign_in_step,
                                   spend, ticket_of)


def alice_lanes(board, name="alice"):
    with service.connect(board.app.state.db_path) as conn:
        who = service.Caller(name, "human", name)
        return [l for l in service.list_admin_lanes(conn, who) if l["administers"]]


def by_id(board, name="alice"):
    return {p["id"]: p for p in presets(alice_lanes(board, name))}


def test_presets_leave_out_sensitive_lanes_and_lanes_you_do_not_administer(board):
    found = by_id(board)
    assert list(found) == ["shared", "everyday", "read-only"]
    assert found["shared"]["lanes"] == {"household": {"read": True, "post": True},
                                        "trips": {"read": True, "post": True}}
    assert found["everyday"]["lanes"] == {**found["shared"]["lanes"],
                                          "engineering": {"read": True, "post": True}}
    assert found["read-only"]["lanes"] == {s: {"read": True, "post": False}
                                           for s in ("household", "trips", "engineering")}
    for preset in found.values():
        # Money and health never come from a preset; bob's lanes never show (rule 7).
        assert not set(preset["lanes"]) & {"alice-money", "household-money", "health",
                                           "bob-money"}


def test_a_preset_that_adds_nothing_is_not_offered(board):
    # bob's only private lane is his money lane, so "everyday" would equal "shared".
    assert list(by_id(board, "bob")) == ["shared", "read-only"]
    assert "engineering" not in str(by_id(board, "bob"))


def test_the_lane_step_offers_presets_first_then_your_own(board):
    _, challenge = pkce()
    page = sign_in_step(board, "alice", challenge).text
    assert re.search(r'<input[^>]*name="preset" value="shared"[^>]*checked', page)
    for value in ("everyday", "read-only", "custom"):
        assert f'name="preset" value="{value}"' in page
    assert page.index('value="shared"') < page.index('value="custom"')
    # The tick table is still there for "choose my own", with today's defaults.
    assert 'name="read_household"' in page and 'name="read_engineering"' in page
    # The page says what each preset contains.
    assert "Engineering" in page.split('value="everyday"')[1].split('value="read-only"')[0]


def test_picking_a_preset_on_the_lane_step_gives_exactly_its_lanes(board):
    verifier, challenge = pkce()
    step = sign_in_step(board, "alice", challenge)
    # Ticks sent alongside a preset are ignored: the preset decides.
    r = choose_lanes(board, ticket_of(step), {"health": (True, True)}, preset="everyday")
    token = spend(board, code_from(r), verifier).json()["access_token"]
    have = lanes_of(board, token)
    for slug in ("household", "trips", "engineering"):
        assert slug in have
    assert "health" not in have and "money" not in have


def test_a_preset_grants_nothing_until_the_code_is_spent(board):
    _, challenge = pkce()
    step = sign_in_step(board, "alice", challenge)
    code_from(choose_lanes(board, ticket_of(step), {}, preset="everyday"))
    assert board.q("SELECT COUNT(*) FROM grants")[0][0] == 0


def test_shared_lanes_from_a_preset_still_wait_for_the_other_admin(board):
    board.admin("bob")
    verifier, challenge = pkce()
    step = sign_in_step(board, "alice", challenge)
    r = choose_lanes(board, ticket_of(step), {}, preset="everyday")
    spend(board, code_from(r), verifier)
    pending = board.admin("bob").get("/v1/admin/pending").json()
    assert sorted(p["lane"] for p in pending) == ["household", "trips"]


def test_choose_my_own_uses_the_ticks(board):
    verifier, challenge = pkce()
    step = sign_in_step(board, "alice", challenge)
    r = choose_lanes(board, ticket_of(step), {"health": (True, False)}, preset="custom")
    token = spend(board, code_from(r), verifier).json()["access_token"]
    have = lanes_of(board, token)
    assert "health" in have and "household" not in have


def test_an_unknown_preset_is_refused_and_keeps_the_ticket(board):
    _, challenge = pkce()
    ticket = ticket_of(sign_in_step(board, "alice", challenge))
    r = choose_lanes(board, ticket, {}, preset="everything")
    assert r.status_code == 400
    assert code_from(choose_lanes(board, ticket, {}, preset="shared"))


def invite_code(r):
    assert r.status_code == 200, r.text
    return re.search(r"/invite/([A-Za-z0-9_-]+)", r.text).group(1)


def test_the_invite_form_offers_presets_and_applies_the_chosen_one(board):
    form = board.admin("alice").get("/admin/invite").text
    assert re.search(r'<input[^>]*name="preset" value="shared"[^>]*checked', form)
    assert 'name="preset" value="custom"' in form
    r = board.admin("alice").post("/admin/invite", data={
        "display_name": "helper", "preset": "read-only", "post_household": "on"})
    token = board.anon().post(f"/v1/invites/{invite_code(r)}/redeem").json()["token"]
    me = board.anon().get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["lanes"] == {s: {"read": True, "post": False}
                           for s in ("engineering", "household", "trips")}


def test_a_refused_invite_keeps_the_chosen_preset(board):
    r = board.admin("alice").post("/admin/invite", data={"display_name": "",
                                                         "preset": "read-only"})
    assert r.status_code == 400
    assert re.search(r'<input[^>]*name="preset" value="read-only"[^>]*checked', r.text)

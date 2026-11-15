import sqlite3

import pytest

from board import admin_cli, service
from board.app import create_app
from board.lanes import BASE_NOTICE, default_lanes
from board.provision import FamilyExists, provision_family
from tests.conftest import Board, q, slugs


def scope(db, slug):
    return q(db, f"SELECT scope FROM lanes WHERE slug = '{slug}'")[0][0]


def admins(db):
    return [r[0] for r in q(db, "SELECT name FROM admins ORDER BY name")]


def test_two_admins_get_the_seven_default_lanes(tmp_path):
    provision_family(db := tmp_path / "b.db", [("alice", "Alice"), ("bob", "Bob")])
    assert slugs(db) == {"household", "trips", "alice-money", "bob-money",
                         "household-money", "engineering", "health"}
    assert scope(db, "bob-money") == "bob" and scope(db, "household") == "shared"
    assert scope(db, "health") == "alice"
    assert q(db, "SELECT name FROM lanes WHERE slug = 'bob-money'") == [("Bob money",)]


def test_every_lane_carries_the_no_purchase_notice(tmp_path):
    provision_family(db := tmp_path / "b.db", [("alice", "Alice"), ("bob", "Bob")])
    notices = [r[0] for r in q(db, "SELECT notice FROM lanes")]
    assert len(notices) == 7 and all(n.startswith(BASE_NOTICE) for n in notices)
    assert "Planning agents propose; humans decide." in \
        q(db, "SELECT notice FROM lanes WHERE slug = 'trips'")[0][0]


def test_one_admin_gets_six_lanes(tmp_path):
    provision_family(db := tmp_path / "b.db", [("alice", "Alice")])
    assert slugs(db) == {"household", "trips", "alice-money", "household-money",
                         "engineering", "health"}


def test_custom_lanes_replace_the_template(tmp_path):
    lanes = [{"slug": "garden", "name": "Garden", "scope": "shared"},
             {"slug": "bob-notes", "name": "Bob notes", "scope": "bob", "notice": "Mine."}]
    provision_family(db := tmp_path / "b.db", [("alice", "Alice"), ("bob", "Bob")], lanes)
    assert slugs(db) == {"garden", "bob-notes"}
    # The no-purchase notice is kept even when the file leaves it out.
    assert q(db, "SELECT notice FROM lanes WHERE slug = 'bob-notes'") == \
        [(f"{BASE_NOTICE} Mine.",)]
    assert q(db, "SELECT notice FROM lanes WHERE slug = 'garden'") == [(BASE_NOTICE,)]


def test_custom_lane_scope_must_be_an_admin_or_shared(tmp_path):
    with pytest.raises(ValueError):
        provision_family(tmp_path / "b.db", [("alice", "Alice")],
                         [{"slug": "x", "name": "X", "scope": "carol"}])


def test_second_family_is_independent(tmp_path):
    provision_family(tmp_path / "a.db", [("alice", "Alice")])
    provision_family(tmp_path / "b.db", [("carol", "Carol")])
    assert admins(tmp_path / "b.db") == ["carol"]
    assert "carol-money" in slugs(tmp_path / "b.db")
    assert "alice-money" not in slugs(tmp_path / "b.db")


def test_provisioning_twice_refused(tmp_path):
    provision_family(db := tmp_path / "b.db", [("alice", "Alice")])
    with pytest.raises(FamilyExists):
        provision_family(db, [("mallory", "M")])
    assert admins(db) == ["alice"]


def test_a_failed_provision_leaves_nothing_behind(tmp_path, monkeypatch):
    db = tmp_path / "b.db"
    # Skip the up-front checks so the failure comes from the UNIQUE constraint,
    # after the admin rows have been inserted.
    monkeypatch.setattr("board.provision._check_lanes", lambda lanes, names: lanes)
    dup = {"slug": "x", "name": "X", "scope": "shared", "notice": BASE_NOTICE}
    with pytest.raises(sqlite3.IntegrityError):
        provision_family(db, [("alice", "Alice")], [dup, dup])
    assert admins(db) == [] and slugs(db) == set()
    assert q(db, "SELECT COUNT(*) FROM principals") == [(0,)]
    monkeypatch.undo()
    provision_family(db, [("alice", "Alice")])  # not blocked by the failed attempt


def test_bad_admin_names_refused(tmp_path):
    for bad in ("Alice", "shared", "", "a b", "-x"):
        with pytest.raises(ValueError):
            provision_family(tmp_path / "b.db", [(bad, "X")])


def _app(tmp_path, names, clock):
    db = tmp_path / "b.db"
    tokens = provision_family(db, [(n, n.title()) for n in names])
    return db, Board(create_app(db), tokens, clock)


def test_single_admin_shared_change_applies_directly(tmp_path, frozen_time):
    db, board = _app(tmp_path, ["alice"], frozen_time)
    alice = board.admin("alice")
    board.register("alice", "bot")
    assert board.grant("alice", "household", "bot").json() == {"status": "applied"}
    assert q(db, "SELECT COUNT(*) FROM pending") == [(0,)]
    assert q(db, "SELECT can_read, can_post FROM grants") == [(1, 1)]


def test_three_admins_any_other_admin_can_approve(tmp_path, frozen_time):
    db, board = _app(tmp_path, ["alice", "bob", "carol"], frozen_time)
    alice, carol = board.admin("alice"), board.admin("carol")  # carol onboarded; bob never logs in
    board.register("alice", "bot")
    result = board.grant("alice", "household", "bot").json()
    assert result["status"] == "pending"
    assert q(db, "SELECT COUNT(*) FROM grants") == [(0,)]
    # The requester cannot approve their own change.
    assert alice.post(f"/v1/admin/pending/{result['pending_id']}/approve").status_code == 403
    assert carol.post(f"/v1/admin/pending/{result['pending_id']}/approve").status_code == 200
    assert q(db, "SELECT can_read, can_post FROM grants") == [(1, 1)]


def test_provisional_until_any_other_admin_has_logged_in(tmp_path, frozen_time):
    db, board = _app(tmp_path, ["alice", "bob", "carol"], frozen_time)
    alice = board.admin("alice")
    board.register("alice", "bot")
    first = board.grant("alice", "household", "bot").json()
    assert first["status"] == "provisional"
    assert q(db, "SELECT can_read FROM grants") == [(1,)]  # takes effect at once
    board.admin("bob")  # one other admin is enough to end the bootstrap
    board.register("alice", "bot2")
    assert board.grant("alice", "household", "bot2").json()["status"] == "pending"


def test_three_admins_private_lanes_stay_isolated(tmp_path, frozen_time):
    db, board = _app(tmp_path, ["alice", "bob", "carol"], frozen_time)
    alice, bob, carol = (board.admin(n) for n in ("alice", "bob", "carol"))
    board.register("alice", "bot")
    for lane in ("alice-money", "bob-money"):
        assert carol.get(f"/v1/lanes/{lane}/posts").status_code == 404
        assert carol.put(f"/v1/admin/lanes/{lane}/grants/bot",
                         json={"can_read": True, "can_post": True}).status_code == 404
    listed = [l["slug"] for l in carol.get("/v1/admin/lanes").json()]
    assert "alice-money" not in listed and "bob-money" not in listed
    assert "carol-money" in listed
    assert bob.put("/v1/admin/lanes/alice-money/grants/bot",
                   json={"can_read": True, "can_post": True}).status_code == 404
    assert q(db, "SELECT COUNT(*) FROM grants") == [(0,)]


def test_three_admins_another_admin_can_reject_a_pending_change(tmp_path, frozen_time):
    db, board = _app(tmp_path, ["alice", "bob", "carol"], frozen_time)
    alice, bob = board.admin("alice"), board.admin("bob")
    board.register("alice", "bot")
    pid = board.grant("alice", "household", "bot").json()["pending_id"]
    assert bob.post(f"/v1/admin/pending/{pid}/reject").status_code == 200
    assert q(db, "SELECT COUNT(*) FROM grants") == [(0,)]
    assert q(db, "SELECT status FROM pending") == [("rejected",)]


def test_three_admins_non_requester_resolves_a_provisional_change(tmp_path, frozen_time):
    db, board = _app(tmp_path, ["alice", "bob", "carol"], frozen_time)
    alice = board.admin("alice")
    board.register("alice", "bot")
    board.register("alice", "bot2")
    p1 = board.grant("alice", "household", "bot").json()
    p2 = board.grant("alice", "household", "bot2").json()
    assert p1["status"] == p2["status"] == "provisional"
    carol = board.admin("carol")
    assert carol.post(f"/v1/admin/pending/{p1['pending_id']}/approve").status_code == 200
    assert carol.post(f"/v1/admin/pending/{p2['pending_id']}/reject").status_code == 200
    assert q(db, "SELECT principal_id FROM grants") == [("bot",)]  # rejection undid bot2
    assert [r[0] for r in q(db, "SELECT status FROM pending ORDER BY id")] == \
        ["ratified", "rejected"]


def test_other_admins(tmp_path, frozen_time):
    db, _ = _app(tmp_path, ["alice", "bob", "carol"], frozen_time)
    with service.connect(db) as conn:
        assert service.list_admins(conn) == ["alice", "bob", "carol"]
        assert service.other_admins(conn, "bob") == ["alice", "carol"]


def test_default_lanes_template_is_pure():
    lanes = default_lanes(["x", "y"], {"x": "X", "y": "Y"})
    assert [l["slug"] for l in lanes] == ["household", "trips", "x-money", "y-money",
                                          "household-money", "engineering", "health"]


def test_cli_provisions_and_refuses_a_second_run(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.db"
    monkeypatch.setenv("BOARD_DB", str(db))
    admin_cli.main(["provision", "--admin", "alice:Alice", "--admin", "bob:Bob"])
    assert admins(db) == ["alice", "bob"]
    with pytest.raises(SystemExit) as e:
        admin_cli.main(["provision", "--admin", "carol:Carol"])
    assert "already has admins" in str(e.value)


def test_cli_reads_a_lanes_file(tmp_path, monkeypatch):
    db, lanes = tmp_path / "cli.db", tmp_path / "lanes.toml"
    lanes.write_text('[[lane]]\nslug = "garden"\nname = "Garden"\nscope = "shared"\n')
    monkeypatch.setenv("BOARD_DB", str(db))
    admin_cli.main(["provision", "--admin", "alice:Alice", "--lanes", str(lanes)])
    assert slugs(db) == {"garden"}


def test_cli_requires_board_db(monkeypatch):
    monkeypatch.delenv("BOARD_DB", raising=False)
    with pytest.raises(SystemExit) as e:
        admin_cli.main(["provision", "--admin", "alice:Alice"])
    assert "BOARD_DB is not set" in str(e.value)


def test_provision_returns_one_setup_token_per_admin(tmp_path):
    tokens = provision_family(db := tmp_path / "b.db", [("alice", "Alice"), ("bob", "Bob")])
    assert set(tokens) == {"alice", "bob"} and tokens["alice"] != tokens["bob"]
    assert q(db, "SELECT COUNT(*) FROM admins WHERE setup_hash IS NOT NULL") == [(2,)]


def test_cli_prints_a_setup_url_per_admin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOARD_DB", str(tmp_path / "cli.db"))
    monkeypatch.setenv("BOARD_PUBLIC_URL", "https://board.example.test/")
    admin_cli.main(["provision", "--admin", "alice:Alice", "--admin", "bob:Bob"])
    out = capsys.readouterr().out
    urls = [l.split()[-1] for l in out.splitlines() if "/setup/" in l]
    assert len(urls) == 2 and all(u.startswith("https://board.example.test/setup/") for u in urls)


def test_cli_reset_admin_prints_a_new_link(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.db"
    monkeypatch.setenv("BOARD_DB", str(db))
    monkeypatch.setenv("BOARD_PUBLIC_URL", "https://board.example.test")
    admin_cli.main(["provision", "--admin", "alice:Alice"])
    capsys.readouterr()
    admin_cli.main(["reset-admin", "alice"])
    assert "https://board.example.test/setup/" in capsys.readouterr().out
    assert q(db, "SELECT action FROM audit_log") == [("admin-reset",)]
    with pytest.raises(SystemExit):
        admin_cli.main(["reset-admin", "nobody"])

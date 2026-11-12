"""admin_cli remove-lane: an empty lane goes, with its grants and requests, and every
admin is told. A lane with posts, or one an open invite names, is refused untouched."""

import json
import sqlite3

import pytest

from board import admin_cli, service
from tests.conftest import q, slugs


def test_removes_an_empty_lane_and_everything_hanging_off_it(db, capsys):
    lane_id = q(db, "SELECT id FROM lanes WHERE slug = 'health'")[0][0]
    other = q(db, "SELECT count(*) FROM grants WHERE lane_id != ?", lane_id)[0][0]
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO pending (lane_id, principal_id, before_json, after_json, "
                 "requested_by, status, created_at) VALUES (?, 'bob', '[false,false]', "
                 "'[true,false]', 'alice', 'provisional', 'now')", (lane_id,))
    conn.commit()
    conn.close()
    admin_cli.main(["remove-lane", "health"])
    assert "Removed lane health" in capsys.readouterr().out
    assert "health" not in slugs(db) and "household" in slugs(db)
    assert q(db, "SELECT count(*) FROM grants WHERE lane_id = ?", lane_id)[0][0] == 0
    assert q(db, "SELECT count(*) FROM pending WHERE lane_id = ?", lane_id)[0][0] == 0
    assert q(db, "SELECT count(*) FROM grants")[0][0] == other  # other lanes keep theirs


def test_every_admin_is_told_even_for_a_private_lane(db):
    admin_cli.main(["remove-lane", "bob-money"])
    (scope, action, detail), = q(db, "SELECT scope, action, detail FROM audit_log "
                                 "WHERE action = 'lane-removed'")
    assert scope == "shared" and action == "lane-removed"
    d = json.loads(detail)
    assert d["lane"] == "bob-money" and d["lane_scope"] != "shared"


def test_a_lane_with_posts_is_refused_and_nothing_changes(db):
    lane_id = q(db, "SELECT id FROM lanes WHERE slug = 'trips'")[0][0]
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO posts (lane_id, author_id, kind, body, received_at) "
                 "VALUES (?, 'alice', 'text', 'hello', 'now')", (lane_id,))
    conn.commit()
    conn.close()
    before = (slugs(db), q(db, "SELECT count(*) FROM grants")[0][0], q(db, "SELECT count(*) FROM audit_log"))
    with pytest.raises(SystemExit) as e:
        admin_cli.main(["remove-lane", "trips"])
    assert "1 post" in str(e.value)
    assert (slugs(db), q(db, "SELECT count(*) FROM grants")[0][0], q(db, "SELECT count(*) FROM audit_log")) == before


def test_an_unknown_lane_is_refused(db):
    with pytest.raises(SystemExit) as e:
        admin_cli.main(["remove-lane", "nope"])
    assert "unknown lane" in str(e.value)


def test_an_open_invite_naming_the_lane_blocks_removal(db):
    with service.connect(db) as conn:
        service.create_invite(conn, service.Caller("alice", "human", "alice"), "Bot",
                              {"engineering": {"read": True, "post": False}})
    with pytest.raises(SystemExit) as e:
        admin_cli.main(["remove-lane", "engineering"])
    assert "open invite" in str(e.value)
    assert "engineering" in slugs(db)


def test_a_missing_database_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("BOARD_DB", str(tmp_path / "none.db"))
    with pytest.raises(SystemExit) as e:
        admin_cli.main(["remove-lane", "health"])
    assert "does not exist" in str(e.value)

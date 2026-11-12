"""Retention: money lanes keep 13 months of posts; `admin_cli prune` deletes older
ones, audits the count (never the text), and leaves every other lane alone."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from board import admin_cli
from board.migrations import migrate_db
from board.provision import provision_family
from tests.conftest import q

DAYS = 396  # 13 months


def add_post(db, lane, days_old, body="Average in 12,480 out 10,912"):
    when = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat(timespec="seconds")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO posts (lane_id, author_id, kind, body, received_at) "
                 "SELECT id, 'alice', 'note', ?, ? FROM lanes WHERE slug = ?", (body, when, lane))
    conn.commit()
    conn.close()


def count(db, lane):
    return q(db, "SELECT count(*) FROM posts p JOIN lanes l ON l.id = p.lane_id "
                 "WHERE l.slug = ?", lane)[0][0]


def test_money_lanes_default_to_13_months_and_others_to_forever(db):
    got = dict(q(db, "SELECT slug, retention_days FROM lanes"))
    assert got["alice-money"] == got["bob-money"] == got["household-money"] == DAYS
    assert got["health"] is None and got["household"] is None and got["trips"] is None


def test_prune_deletes_only_posts_older_than_the_lane_keeps(db, capsys):
    add_post(db, "alice-money", DAYS + 1)
    add_post(db, "alice-money", DAYS - 1)
    add_post(db, "bob-money", 30)
    admin_cli.main(["prune"])
    assert count(db, "alice-money") == 1 and count(db, "bob-money") == 1
    assert "alice-money: 1" in capsys.readouterr().out


def test_lanes_without_retention_are_never_pruned(db):
    add_post(db, "health", 5000)
    add_post(db, "household", 5000)
    admin_cli.main(["prune"])
    assert count(db, "health") == 1 and count(db, "household") == 1


def test_prune_is_audited_for_every_admin_without_the_text(db):
    add_post(db, "alice-money", DAYS + 5, body="secret-looking text xyzzy")
    admin_cli.main(["prune"])
    rows = q(db, "SELECT actor, scope, detail FROM audit_log WHERE action = 'posts-pruned'")
    assert len(rows) == 1
    actor, scope, detail = rows[0]
    assert actor == "host" and scope == "shared"
    assert '"alice-money"' in detail and '"posts": 1' in detail
    # Letters only: the detail also holds a cutoff timestamp, so a number could match by chance.
    assert "secret" not in detail and "xyzzy" not in detail


def test_nothing_to_prune_writes_no_audit_row(db, capsys):
    add_post(db, "alice-money", 10)
    admin_cli.main(["prune"])
    assert q(db, "SELECT count(*) FROM audit_log WHERE action = 'posts-pruned'") == [(0,)]
    assert "Nothing to prune" in capsys.readouterr().out


def test_set_retention_changes_and_clears_a_lane(db):
    admin_cli.main(["set-retention", "health", "730"])
    assert q(db, "SELECT retention_days FROM lanes WHERE slug = 'health'") == [(730,)]
    admin_cli.main(["set-retention", "health", "off"])
    assert q(db, "SELECT retention_days FROM lanes WHERE slug = 'health'") == [(None,)]
    actions = [r[0] for r in q(db, "SELECT action FROM audit_log")]
    assert actions.count("lane-retention-set") == 2


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "29"])
def test_set_retention_refuses_nonsense(db, bad):
    with pytest.raises(SystemExit):
        admin_cli.main(["set-retention", "health", bad])
    assert q(db, "SELECT retention_days FROM lanes WHERE slug = 'health'") == [(None,)]


def test_set_retention_unknown_lane(db):
    with pytest.raises(SystemExit):
        admin_cli.main(["set-retention", "nope", "400"])


def test_migration_gives_existing_money_lanes_13_months(tmp_path):
    path = tmp_path / "old.db"
    provision_family(path, [("alice", "Alice")])
    conn = sqlite3.connect(path)
    conn.execute("UPDATE lanes SET retention_days = NULL")
    conn.execute("INSERT INTO lanes (slug, name, scope, notice, created_at, sensitive) "
                 "VALUES ('extra-money', 'x', 'shared', 'n', 'now', 1)")
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()
    migrate_db(path)
    got = dict(q(path, "SELECT slug, retention_days FROM lanes"))
    assert got["extra-money"] == DAYS and got["alice-money"] == DAYS
    assert got["health"] is None


@pytest.mark.parametrize("bad", ["400", 5, True, -1])
def test_provision_refuses_bad_retention(tmp_path, bad):
    lane = {"slug": "x", "name": "X", "scope": "shared", "retention_days": bad}
    with pytest.raises(ValueError):
        provision_family(tmp_path / "b.db", [("alice", "Alice")], [lane])

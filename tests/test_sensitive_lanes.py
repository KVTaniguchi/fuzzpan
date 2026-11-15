"""Sensitive lanes (money and health): the board refuses account-like numbers and
long posts, and says nothing about the digits it refused."""

import sqlite3

import pytest

from board import admin_cli
from board.migrations import migrate_db
from board.provision import provision_family

LANE = "alice-money"  # sensitive in the default template


@pytest.fixture
def bot(board):
    return board.agent("alice", "bot", LANE, "household")


def test_default_template_marks_money_and_health_lanes(board):
    flagged = {r[0] for r in board.q("SELECT slug FROM lanes WHERE sensitive = 1")}
    assert flagged == {"alice-money", "bob-money", "household-money", "health"}


@pytest.mark.parametrize("body", [
    "card 4111 1111 1111 1111",          # Luhn-valid, spaces
    "card 4111-1111-1111-1111",          # Luhn-valid, dashes
    "card 4111111111111111",             # Luhn-valid, plain
    "acct 1234567890123456",             # not Luhn-valid, still 16 digits
    "routing 021000021",                 # 9 digits
    "ssn 123-45-6789",                   # 9 digits across dashes
    "acct 1234 5678 9012",               # 12 digits across spaces
])
def test_account_like_numbers_are_refused(board, bot, body):
    r = bot.post(LANE, body)
    assert r.status_code == 422, r.text
    assert r.json()["error"]
    for digits in ("4111", "1234", "0210", "6789"):
        assert digits not in r.text  # never echo what was refused
    assert board.q("SELECT count(*) FROM posts")[0][0] == 0


@pytest.mark.parametrize("body", [
    "Average monthly in: $12,480.55. Average monthly out: $10,912.10.",
    "Jul-Oct 2026: in 49,922 / out 43,648 (4 months).",
    "Net +1,568 per month; 2026-11-05.",
    "12345678 is 8 digits and passes",
])
def test_totals_and_averages_pass(board, bot, body):
    assert bot.post(LANE, body).status_code == 201


def test_digit_rule_counts_only_the_digits_in_a_run(board, bot):
    # Separate numbers in a sentence are fine; only one run of 9+ is not.
    assert bot.post(LANE, "In 12,480 out 10,912 net 1,568 over 4 months").status_code == 201


def test_sensitive_posts_are_capped_at_2_kb(board, bot):
    assert bot.post(LANE, "x" * 2048).status_code == 201
    r = bot.post(LANE, "x" * 2049)
    assert r.status_code == 413


def test_other_lanes_keep_the_old_rules(board, bot):
    assert bot.post("household", "order 1234567890123456 arrived").status_code == 201
    assert bot.post("household", "x" * 8192).status_code == 201


def test_refusals_are_audited_without_the_body(board, bot):
    bot.post(LANE, "card 4111 1111 1111 1111")
    rows = board.q("SELECT actor, action, scope, detail FROM audit_log "
                    "WHERE action = 'post-rejected'")
    assert len(rows) == 1
    actor, action, scope, detail = rows[0]
    assert actor == "bot" and '"alice-money"' in detail
    assert "4111" not in detail and "1111" not in detail


def test_the_feed_page_form_is_guarded_too(board):
    http = board.admin("alice")
    r = http.post(f"/lanes/{LANE}/post", data={"body": "routing 021000021"},
                  follow_redirects=False)
    assert r.status_code == 422, r.status_code
    assert board.q("SELECT count(*) FROM posts")[0][0] == 0


def test_migration_flags_existing_money_and_health_lanes(tmp_path):
    path = tmp_path / "old.db"
    provision_family(path, [("alice", "Alice"), ("bob", "Bob")])
    conn = sqlite3.connect(path)
    conn.execute("UPDATE lanes SET sensitive = 0")
    conn.execute("INSERT INTO lanes (slug, name, scope, notice, created_at, sensitive) "
                 "VALUES ('extra-money', 'x', 'shared', 'n', 'now', 0)")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    conn.close()
    # Running migration 9 again on an already-widened table must not break, and
    # must flag every -money lane and health, including one the template lacks.
    migrate_db(path)
    conn = sqlite3.connect(path)
    flagged = {r[0] for r in conn.execute("SELECT slug FROM lanes WHERE sensitive = 1")}
    conn.close()
    assert {"extra-money", "alice-money", "health"} <= flagged
    assert "household" not in flagged and "trips" not in flagged


def test_admin_cli_can_flag_and_unflag_a_lane(board, monkeypatch, capsys):
    monkeypatch.setenv("BOARD_DB", str(board.app.state.db_path))
    admin_cli.main(["set-sensitive", "trips", "on"])
    assert board.q("SELECT sensitive FROM lanes WHERE slug = 'trips'") == [(1,)]
    admin_cli.main(["set-sensitive", "trips", "off"])
    assert board.q("SELECT sensitive FROM lanes WHERE slug = 'trips'") == [(0,)]
    with pytest.raises(SystemExit):
        admin_cli.main(["set-sensitive", "no-such-lane", "on"])

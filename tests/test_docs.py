"""The public docs must not contradict what the board does."""

import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from board.migrations import migrate_db
from tests.conftest import HTTPS

ROOT = Path(__file__).parent.parent
DESIGN = (ROOT / "docs/DESIGN.md").read_text()
README = (ROOT / "README.md").read_text()
AGENTS = (ROOT / "docs/agents.md").read_text()


def section(number):
    return re.search(rf"^## {number}\. .*?(?=^## )", DESIGN, re.S | re.M).group(0)


def test_design_sections_are_in_order():
    numbers = [int(n) for n in re.findall(r"^## (\d+)[a-z]?\.", DESIGN, re.M)]
    assert numbers == sorted(numbers)


def test_design_says_narrowing_applies_at_once_and_where_the_other_admin_sees_it():
    assert "Any grant change on a `shared` lane" not in DESIGN
    assert "Any change on a shared lane creates" not in DESIGN
    assert "notified on the feed page" not in DESIGN
    six = section(6)
    assert "apply at once" in six and "audit log" in six


def test_design_section_six_states_the_rule_and_does_not_contradict_itself():
    six = section(6)
    assert "Nothing goes on the approval list" not in six
    assert "held before the change" in six and "other than the one who asked" in six
    assert "tests/test_pending_exploration.py" in six
    assert (Path(__file__).parent / "test_pending_exploration.py").exists()
    # every status the code can write is described
    for status in ("pending", "provisional", "approved", "ratified", "rejected", "superseded"):
        assert f"`{status}`" in six or f"**{status}**" in six


def test_design_says_there_is_no_lock_on_a_username_as_a_whole():
    three = section(5)
    assert "no lock on the username as a whole" in three
    assert "GLOBAL_LOCK_AFTER" not in DESIGN and "login_failures" not in DESIGN
    assert "ceiling" not in DESIGN.lower()


def test_design_says_when_the_401_body_differs():
    assert "missing or malformed, no agent has that token" not in DESIGN
    assert "A request with no\nheader and no login session" in DESIGN


def test_design_data_model_lists_every_table(tmp_path):
    db = tmp_path / "b.db"
    migrate_db(db)
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    four = section(4)
    for table in tables - {"sqlite_sequence"}:
        assert re.search(rf"^{table}\s", four, re.M), f"§4 omits {table}"
    assert "scope" in re.search(r"^audit_log.*$", four, re.M).group(0)


def test_design_repo_layout_lists_what_is_there():
    ten = section(10)
    for name in ("limits.py", "static/", "connect"):
        assert name in ten


def test_docs_say_how_to_get_the_board_command():
    for doc in (README, AGENTS):
        assert "uv run board connect" in doc and "uv tool install" in doc
    assert AGENTS.index("**Hosted agents**") < AGENTS.index("**CLI agents**")


def test_readme_says_to_remove_the_safety_copy_after_a_restore():
    assert "before-restore-" in README and "holds TOTP secrets" in README
    assert "/data" in README[README.index("Clean up afterwards"):]


def test_design_lists_the_manual_browser_check():
    seven = section(7)
    assert "Manual browser check" in seven
    assert "invite link" in seven and "press Back" in seven


def test_gitignore_covers_macos_folder_files():
    assert ".DS_Store" in (ROOT / ".gitignore").read_text().splitlines()


def test_the_invite_page_leads_with_the_http_request(board):
    code = board.invite("alice", "coder", {})
    page = TestClient(board.app, base_url=HTTPS).get(f"/invite/{code}").text
    assert page.index("curl -X POST") < page.index("uv run board connect")
    assert "needs nothing installed" in page
    assert f"uv run board connect https://testserver/invite/{code}" in page


def test_the_link_page_tells_the_admin_nothing_needs_installing(board):
    page = board.create_invite_via_form("alice", "coder").text
    assert "nothing needs installing" in page and "uv run board connect" in page


def test_install_docs_name_options_the_script_really_has():
    script = (ROOT / "scripts/install.sh").read_text()
    agent_doc = (ROOT / "docs/setup-with-an-agent.md").read_text()
    for flag in ("--yes", "--domain", "--admin", "--ref", "--skip-dns-check"):
        assert f"{flag})" in script, f"install.sh has no {flag}"
        assert flag in agent_doc, f"the agent guide omits {flag}"
    assert "scripts/install.sh" in README and "setup-with-an-agent.md" in README
    assert "doctor.py" in README


def test_the_agent_guide_keeps_the_money_and_secret_rules():
    guide = (ROOT / "docs/setup-with-an-agent.md").read_text()
    assert "never spend money" in guide
    assert "never see, ask for, store or print a secret" in guide
    assert "Do not open a setup link yourself" in guide


def test_design_describes_the_install_section_and_its_decision_record():
    ten_c = re.search(r"^## 10c\. .*?(?=^## )", DESIGN, re.S | re.M).group(0)
    assert "install.sh" in ten_c and "doctor.py" in ten_c and "Not yet run on a real server" in ten_c
    assert (ROOT / "docs/decisions/0003-guided-server-install.md").exists()


def test_the_installer_never_publishes_the_boards_port_or_prints_a_token():
    script = (ROOT / "scripts/install.sh").read_text()
    code = "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))
    assert "8787" not in code
    assert "board connect" not in code and "redeem" not in code


def test_readme_leads_the_server_install_with_the_agent_route():
    install = README[README.index("## Install on a server"):README.index("## Lanes")]
    assert install.index("hand it to your agent") < install.index("install.sh")
    assert "raw.githubusercontent.com/kvtaniguchi/fuzzpan/RELEASE/docs/setup-with-an-agent.md" in install


def test_readme_quickstart_leads_with_the_local_agent_route():
    quick = README[README.index("## Quickstart"):README.index("### Without Docker")]
    assert quick.index("hand it to your agent") < quick.index("docker compose up")
    assert "raw.githubusercontent.com/kvtaniguchi/fuzzpan/RELEASE/docs/run-locally-with-an-agent.md" in quick


def test_the_local_guide_keeps_the_rules_and_names_real_settings():
    guide = (ROOT / "docs/run-locally-with-an-agent.md").read_text()
    assert "never spend money" in guide
    assert "never see, ask for, store or print a secret" in guide
    assert "Do not open a setup link yourself" in guide
    assert "Bind to `127.0.0.1` only" in guide
    # every setting it tells the agent to use exists in the code
    main_py = (ROOT / "board/main.py").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "BOARD_SECURE_COOKIES" in main_py and "BOARD_DB" in main_py
    for name in ("BOARD_PUBLIC_URL", "BOARD_HTTP_PORT", "BOARD_HTTPS_PORT"):
        assert name in compose and name in guide
    assert "root.crt" in guide and "root.crt" in README
    assert "Docker route has not been run" in guide


def test_design_notes_the_local_guide_and_what_was_and_was_not_run():
    assert "run-locally-with-an-agent.md" in DESIGN and "not yet run" in DESIGN

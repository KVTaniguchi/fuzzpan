"""board.main must refuse to run without an explicit database path."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_main_exits_with_a_clear_message_when_board_db_is_unset(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "BOARD_DB"}
    env["PYTHONPATH"] = str(ROOT)
    env["HOME"] = str(tmp_path)  # nothing real reachable via ~ either
    result = subprocess.run([sys.executable, "-c", "import board.main"],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "BOARD_DB" in result.stderr
    assert "Traceback" not in result.stderr
    assert list(tmp_path.iterdir()) == []  # created no database or directory


def _cookie_flag(tmp_path, value):
    env = {k: v for k, v in os.environ.items() if k != "BOARD_SECURE_COOKIES"}
    env.update(PYTHONPATH=str(ROOT), BOARD_DB=str(tmp_path / "b.db"))
    if value is not None:
        env["BOARD_SECURE_COOKIES"] = value
    # Reaching into the closure is brittle; the Origin check is the visible effect:
    # an http:// Origin is only same-origin when secure cookies are off.
    code = ("from board.main import app\\n"
            "from fastapi.testclient import TestClient\\n"
            "c = TestClient(app, base_url='http://h')\\n"
            "c.cookies.set('board_session', 'x')\\n"
            "r = c.post('/login', headers={'Origin': 'http://h'}, data={})\\n"
            "print(r.status_code)\\n")
    out = subprocess.run([sys.executable, "-c", code.replace("\\n", "\n")], cwd=tmp_path,
                         env=env, capture_output=True, text=True)
    return out.stdout.strip(), out.stderr


def test_secure_cookies_default_on_and_only_zero_turns_them_off(tmp_path):
    assert _cookie_flag(tmp_path, None)[0] == "403"
    assert _cookie_flag(tmp_path, "false")[0] == "403"
    assert _cookie_flag(tmp_path, "0")[0] != "403"

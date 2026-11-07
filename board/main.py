"""Entry point: `uv run uvicorn board.main:app --port 8787` (binds 127.0.0.1).

BOARD_DB must name the database file; there is no default, so a forgotten
setting can never fall through to someone else's database.
"""

import os
import sys

from board.app import create_app
from board.migrations import DatabaseTooNew

DB_PATH = os.environ.get("BOARD_DB")
if not DB_PATH:
    sys.exit("BOARD_DB is not set. Set it to the path of the board's SQLite database file.")
os.makedirs(os.path.dirname(DB_PATH) or ".", mode=0o700, exist_ok=True)
# Cookies are Secure unless BOARD_SECURE_COOKIES=0 (any other value keeps them
# on). The 0 setting is for plain-HTTP local runs only; never use it on a
# board that is reachable by anyone else.
try:
    app = create_app(DB_PATH, secure_cookies=os.environ.get("BOARD_SECURE_COOKIES") != "0")
except DatabaseTooNew as e:
    sys.exit(str(e))

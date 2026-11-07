"""The MCP server is launched from whatever folder a Claude Code session is in
(`uv run --project <this repo> python -m client.mcp_server`), so the board's
modules must import from anywhere, not just from the repo root."""

import subprocess
import sys


def test_mcp_server_imports_from_outside_the_repo(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", "import client.mcp_server, board.app"],
        cwd=tmp_path, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr

"""scripts/backup_pull.sh with stand-in `ssh` and `docker` (no server needed): the
"server" is this machine, the board container is the real admin_cli. A copy lands in
the backup folder, private, whole, and failures are loud and leave nothing half-made."""

import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from board.provision import provision_family

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backup_pull.sh"


def stub(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # ssh [-o X] -- HOST CMD : run CMD here, in place of the server
    stub(bin_dir / "ssh", 'while [ "$1" != "--" ]; do shift; done; shift; host="$1"; cmd="$2"\n'
                          '[ -n "$SSH_FAIL" ] && exit 255\n'
                          'echo "$host" >> "$SSH_LOG"\n'
                          'case "$cmd" in *"$DOWNLOAD_FAIL"*) [ -n "$DOWNLOAD_FAIL" ] && exit 1;; esac\n'
                          'sh -c "$cmd"\n')
    # docker compose exec -T board CMD... : run CMD here ("python" is the real interpreter)
    stub(bin_dir / "docker", 'shift 4; if [ "$1" = python ]; then shift; exec "$PY" "$@"; fi\n'
                             'if [ -n "$GARBAGE" ] && [ "$1" = cat ]; then echo not a database; exit 0; fi\n'
                             'exec "$@"\n')
    stub(bin_dir / "age", 'out=""; while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; '
                          '-r) shift;; *) in="$1";; esac; shift; done; '
                          '(echo ENCRYPTED; cat "$in") > "$out"\n')
    db = tmp_path / "board.db"
    provision_family(str(db), [("alice", "Alice")])
    dest = tmp_path / "backups"
    # Keep sqlite3 and the system tools, but not any real age.
    e = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "BOARD_DB": str(db),
         "PYTHONPATH": str(ROOT), "PY": sys.executable, "SSH_LOG": str(tmp_path / "ssh.log"),
         "BOARD_SSH": "deploy@board.example.com", "BOARD_REMOTE_DIR": str(ROOT),
         "BACKUP_DIR": str(dest)}
    return e, dest, tmp_path


def run(e, **extra):
    return subprocess.run(["sh", str(SCRIPT)], env={**e, **extra}, capture_output=True,
                          text=True, cwd="/")


def copies(dest):
    return sorted(p for p in dest.iterdir())


def test_success_saves_one_whole_private_copy_and_leaves_nothing_else(env):
    e, dest, _ = env
    r = run(e)
    assert r.returncode == 0, r.stderr
    (got,) = copies(dest)
    assert got.name.startswith("board-") and got.name.endswith(".db")
    assert stat.S_IMODE(got.stat().st_mode) == 0o600
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    c = sqlite3.connect(got)
    assert c.execute("SELECT count(*) FROM principals").fetchone()[0] >= 1
    c.close()
    # It connected to the host named in BOARD_SSH.
    assert "deploy@board.example.com" in Path(e["SSH_LOG"]).read_text()


def test_the_copy_on_the_server_is_removed_afterwards(env):
    e, dest, tmp = env
    before = set(Path("/tmp").glob("board-backup-*.db"))
    assert run(e).returncode == 0
    assert set(Path("/tmp").glob("board-backup-*.db")) == before


def test_age_recipient_encrypts_and_leaves_no_plaintext(env):
    e, dest, _ = env
    r = run(e, AGE_RECIPIENT="age1testrecipient")
    assert r.returncode == 0, r.stderr
    (got,) = copies(dest)
    assert got.name.endswith(".db.age")
    assert got.read_bytes().startswith(b"ENCRYPTED")


def test_a_download_that_is_not_a_database_is_refused_and_nothing_is_kept(env):
    e, dest, _ = env
    r = run(e, GARBAGE="1")
    assert r.returncode != 0 and "not a SQLite database" in r.stderr
    assert copies(dest) == []


def test_unreachable_server_fails_loudly_and_keeps_nothing(env):
    e, dest, _ = env
    r = run(e, SSH_FAIL="1")
    assert r.returncode != 0 and "could not make a backup" in r.stderr
    assert copies(dest) == []


def test_failed_download_fails_loudly_and_keeps_nothing(env):
    e, dest, _ = env
    r = run(e, DOWNLOAD_FAIL="cat /tmp/board-backup")
    assert r.returncode != 0 and "could not download" in r.stderr
    assert copies(dest) == []


def test_keeps_only_the_newest_copies(env):
    e, dest, _ = env
    dest.mkdir()
    old = [f"board-2020010{i}T000000Z.db" for i in range(1, 6)]
    for n in old:
        (dest / n).write_bytes(b"x")
    (dest / "notes.txt").write_text("not a backup, never deleted")
    r = run(e, BACKUP_KEEP="3")
    assert r.returncode == 0, r.stderr
    names = [p.name for p in copies(dest)]
    assert "notes.txt" in names
    kept = [n for n in names if n.startswith("board-")]
    assert len(kept) == 3
    assert old[0] not in kept and old[1] not in kept and old[2] not in kept
    assert old[3] in kept and old[4] in kept


@pytest.mark.parametrize("name,value", [
    ("BOARD_SSH", ""), ("BOARD_REMOTE_DIR", ""),
    ("BOARD_SSH", "-oProxyCommand=evil"), ("BOARD_SSH", "a@b; rm -rf /"),
    ("BOARD_REMOTE_DIR", "/srv/x; rm -rf ~"), ("BOARD_REMOTE_DIR", "/srv/$(id)"),
    ("BACKUP_KEEP", "0"), ("BACKUP_KEEP", "-1"), ("BACKUP_KEEP", "many"),
])
def test_bad_settings_are_refused_before_anything_runs(env, name, value):
    e, dest, _ = env
    r = run(e, **{name: value})
    assert r.returncode != 0
    assert not Path(e["SSH_LOG"]).exists()


@pytest.mark.skipif(shutil.which("age", path="/usr/local/bin:/opt/homebrew/bin") is not None,
                    reason="the script adds these folders to PATH, so a real age is found")
def test_age_recipient_without_age_installed_is_refused(env):
    e, dest, tmp = env
    (tmp / "bin" / "age").unlink()
    r = run(e, AGE_RECIPIENT="age1x")
    assert r.returncode != 0 and "age is not installed" in r.stderr

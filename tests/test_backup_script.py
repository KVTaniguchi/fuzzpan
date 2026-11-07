"""scripts/backup_offsite.sh with stand-in `age` and `rclone` (the real tools are
not needed): no plaintext is left behind, even on failure, and failures are loud."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from board.provision import provision_family

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backup_offsite.sh"


def stub(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    remote = tmp_path / "remote"
    remote.mkdir()
    # age -r RECIP -o OUT IN : "encrypt" by prefixing a marker line
    stub(bin_dir / "age", 'out=""; while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; '
                          '-r) shift;; *) in="$1";; esac; shift; done; '
                          '[ -n "$AGE_FAIL" ] && exit 1; (echo ENCRYPTED; cat "$in") > "$out"\n')
    # rclone copyto SRC DEST
    stub(bin_dir / "rclone", '[ -n "$RCLONE_FAIL" ] && exit 1; cp "$2" "$REMOTE_DIR/$(basename "$2")"\n')
    db = tmp_path / "board.db"
    provision_family(str(db), [("alice", "Alice")])
    work = tmp_path / "work"
    work.mkdir()
    e = {"PATH": f"{bin_dir}:/usr/bin:/bin", "BOARD_DB": str(db), "PYTHONPATH": str(ROOT),
         "AGE_RECIPIENT": "age1testrecipient", "RCLONE_REMOTE": "fake:bucket",
         "REMOTE_DIR": str(remote), "BACKUP_TMPDIR": str(work),
         "BOARD_ADMIN_CMD": f"{sys.executable} -m board.admin_cli"}
    return e, work, remote


def run(e, **extra):
    return subprocess.run(["sh", str(SCRIPT)], env={**e, **extra}, capture_output=True,
                          text=True, cwd=ROOT)


def test_success_uploads_one_encrypted_file_and_leaves_nothing_local(env):
    e, work, remote = env
    r = run(e)
    assert r.returncode == 0, r.stderr
    uploaded = list(remote.iterdir())
    assert len(uploaded) == 1 and uploaded[0].name.endswith(".db.age")
    assert uploaded[0].read_bytes().startswith(b"ENCRYPTED")
    assert list(work.iterdir()) == []


@pytest.mark.parametrize("failing", ["AGE_FAIL", "RCLONE_FAIL"])
def test_failure_is_nonzero_and_still_removes_the_plaintext(env, failing):
    e, work, remote = env
    r = run(e, **{failing: "1"})
    assert r.returncode != 0
    assert list(work.iterdir()) == []
    assert list(remote.iterdir()) == []


def test_failed_backup_step_is_nonzero(env):
    e, work, remote = env
    r = run(e, BOARD_DB=str(work / "does-not-exist.db"))
    assert r.returncode != 0
    assert list(work.iterdir()) == []


@pytest.mark.parametrize("missing", ["AGE_RECIPIENT", "RCLONE_REMOTE"])
def test_refuses_to_start_without_recipient_or_remote(env, missing):
    e, work, remote = env
    e = {k: v for k, v in e.items() if k != missing}
    r = run(e)
    assert r.returncode != 0
    assert missing in r.stderr
    assert list(work.iterdir()) == [] and list(remote.iterdir()) == []


def test_temporary_files_are_private(env):
    e, work, remote = env
    # age stand-in that records the mode of the plaintext it was given
    stub(Path(e["PATH"].split(":")[0]) / "age",
         'for a; do :; done; stat -f %Lp "$a" > "$REMOTE_DIR/mode" 2>/dev/null '
         '|| stat -c %a "$a" > "$REMOTE_DIR/mode"; echo x > "$4"\n')
    r = run(e)
    assert r.returncode == 0, r.stderr
    assert (remote / "mode").read_text().strip() == "600"


def test_reads_settings_from_an_env_file_and_runs_from_any_folder(env, tmp_path):
    e, work, remote = env
    e = {k: v for k, v in e.items() if k not in ("AGE_RECIPIENT", "RCLONE_REMOTE")}
    envfile = tmp_path / "board.env"
    envfile.write_text('AGE_RECIPIENT=age1fromfile\nRCLONE_REMOTE="fake:bucket"\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = subprocess.run(["sh", str(SCRIPT)], env={**e, "BOARD_ENV_FILE": str(envfile)},
                       capture_output=True, text=True, cwd=elsewhere)
    assert r.returncode == 0, r.stderr
    assert len(list(remote.iterdir())) == 1 and list(work.iterdir()) == []


def test_environment_beats_the_env_file(env, tmp_path):
    e, work, remote = env
    envfile = tmp_path / "board.env"
    envfile.write_text("RCLONE_REMOTE=\n")  # empty: the real value must stay
    r = run(e, BOARD_ENV_FILE=str(envfile))
    assert r.returncode == 0, r.stderr

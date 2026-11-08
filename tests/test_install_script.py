"""scripts/install.sh with stand-in system commands (docker, ufw, apt-get, curl, ...)
and a local tarball made from this very tree. Nothing real is installed: every
path it writes is under tmp_path. What is checked is the script's own logic: what
it refuses, the order it does things in, what it writes, and that setup links
reach the terminal but not the log.

It cannot show that apt, Docker or Let's Encrypt behave on a real server; that
needs one (see docs/DESIGN.md §10c)."""

import os
import shlex
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install.sh"
LINK = "https://board.example.com/setup/SECRETSETUPTOKEN"


def stub(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def box(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    calls.touch()
    log = f'echo "$(basename "$0") $*" >> {shlex.quote(str(calls))}\n'

    stub(bin_dir / "id", 'echo "${FAKE_UID:-0}"\n')
    stub(bin_dir / "hostname", 'echo "${FAKE_IPS:-203.0.113.10 10.0.0.5}"\n')
    stub(bin_dir / "getent", 'echo "${FAKE_DNS:-203.0.113.10}  STREAM $3"\n')
    stub(bin_dir / "ss", '[ -n "$FAKE_PORT_BUSY" ] && echo "LISTEN 0 4096 0.0.0.0:80 0.0.0.0:*"; exit 0\n')
    stub(bin_dir / "apt-get", log)
    stub(bin_dir / "ufw", log)
    stub(bin_dir / "dpkg", 'echo amd64\n')
    stub(bin_dir / "install", log)
    stub(bin_dir / "sleep", "exit 0\n")
    # curl: the tarball download writes our archive; anything else answers healthy
    stub(bin_dir / "curl",
         log + 'out=""; while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; esac; shift; done\n'
               '[ -n "$out" ] && { cp "$FAKE_TARBALL" "$out"; exit 0; }\n'
               'echo \'{"status":"ok"}\'\n')
    stub(bin_dir / "python3", 'echo "doctor ran"; exit "${FAKE_DOCTOR_RC:-0}"\n')
    # docker: `compose version` works; exec ... provision prints links; ps -q says
    # whether a board container exists
    stub(bin_dir / "docker",
         log + 'case "$*" in\n'
               '  "compose version") echo "Docker Compose version v2.30.0";;\n'
               '  *"ps -q board"*) [ -n "$FAKE_RUNNING" ] && echo abc123;;\n'
               '  *"admin_cli provision"*) if [ -n "$FAKE_PROVISIONED" ]; then '
               'echo "x already has admins; refusing to provision it again." >&2; exit 1; fi; '
               f'echo "One-time setup links:"; echo "  alice: {LINK}";;\n'
               'esac\nexit 0\n')

    tarball = tmp_path / "src.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        for rel in ("docker-compose.yml", "Caddyfile", "Dockerfile", "pyproject.toml", "uv.lock",
                    "board", "client", "scripts/doctor.py", ".env.example"):
            tar.add(ROOT / rel, arcname=f"fuzzpan-main/{rel}",
                    filter=lambda i: None if "__pycache__" in i.name else i)

    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_CODENAME=noble\n')
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FUZZPAN_DIR": str(tmp_path / "opt"),
           "FUZZPAN_LOG": str(tmp_path / "install.log"), "FUZZPAN_ROOTFS": str(tmp_path / "root"),
           "FUZZPAN_OS_RELEASE": str(os_release), "FAKE_TARBALL": str(tarball),
           "FUZZPAN_TARBALL_URL": "https://example.invalid/src.tar.gz",
           "SSH_CONNECTION": "198.51.100.7 50000 203.0.113.10 2222"}
    return env, tmp_path


def run(box, *args, stdin="", **extra):
    env, _ = box
    return subprocess.run(["bash", str(SCRIPT), *args], env={**env, **extra}, input=stdin,
                          capture_output=True, text=True, timeout=60)


def calls(box):
    return (box[1] / "calls").read_text().splitlines()


GOOD = ["--yes", "--domain", "board.example.com", "--admin", "alice:Alice Smith"]


def test_fresh_install_does_everything_in_order(box):
    r = run(box, *GOOD)
    assert r.returncode == 0, r.stdout + r.stderr
    opt = box[1] / "opt"
    env_file = opt / ".env"
    assert env_file.read_text() == ("BOARD_DOMAIN=board.example.com\n"
                                    "BOARD_PUBLIC_URL=https://board.example.com\n")
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    for f in ("docker-compose.yml", "Caddyfile", "board/app.py", "scripts/doctor.py"):
        assert (opt / f).exists(), f
    c = calls(box)
    order = [next(i for i, line in enumerate(c) if needle in line) for needle in
             ("ufw --force enable", "docker compose up -d --build --wait", "admin_cli provision")]
    assert order == sorted(order)
    assert "docker compose exec -T board python -m board.admin_cli provision --admin alice:Alice Smith" in c
    assert "doctor ran" in r.stdout
    # the firewall: SSH on the port this session came in on, plus 22, 80 and 443
    for port in ("22", "2222", "80", "443"):
        assert f"ufw allow {port}/tcp" in c
    assert not any("8787" in line for line in c)
    assert (box[1] / "root/etc/apt/apt.conf.d/20auto-upgrades").exists()


def test_the_setup_link_is_printed_but_never_logged(box):
    r = run(box, *GOOD)
    assert LINK in r.stdout
    assert "SECRETSETUPTOKEN" not in (box[1] / "install.log").read_text()


def test_nothing_secret_or_unexpected_goes_in_env(box):
    run(box, *GOOD)
    keys = {line.split("=")[0] for line in (box[1] / "opt/.env").read_text().splitlines()}
    assert keys == {"BOARD_DOMAIN", "BOARD_PUBLIC_URL"}


def test_refuses_when_not_root_before_touching_anything(box):
    r = run(box, *GOOD, FAKE_UID="1000")
    assert r.returncode == 1 and "root" in r.stderr
    assert calls(box) == [] and not (box[1] / "opt").exists()


def test_refuses_an_unsupported_system(box):
    (box[1] / "os-release").write_text("ID=fedora\n")
    r = run(box, *GOOD)
    assert r.returncode == 1 and "Ubuntu and Debian" in r.stderr
    assert not any(line.startswith("ufw") for line in calls(box))


def test_dns_that_does_not_point_here_stops_before_any_change(box):
    r = run(box, *GOOD, FAKE_DNS="198.51.100.99")
    assert r.returncode == 1 and "points at 198.51.100.99" in r.stderr
    assert not any(line.startswith(("ufw", "docker")) for line in calls(box))
    assert not (box[1] / "opt").exists()


def test_a_domain_that_does_not_resolve_says_so(box):
    stub(box[1] / "bin/getent", "exit 2\n")
    r = run(box, *GOOD)
    assert r.returncode == 1 and "does not resolve" in r.stderr


def test_skip_dns_check_lets_a_nat_server_carry_on(box):
    r = run(box, *GOOD, "--skip-dns-check", FAKE_DNS="198.51.100.99")
    assert r.returncode == 0


def test_auto_domain_is_built_from_the_public_address_only(box):
    r = run(box, "--yes", "--domain", "auto", "--admin", "alice:Alice",
            FAKE_IPS="10.0.0.5 203.0.113.10", FAKE_DNS="203.0.113.10")
    assert r.returncode == 0, r.stderr
    assert "BOARD_DOMAIN=203-0-113-10.sslip.io" in (box[1] / "opt/.env").read_text()


def test_auto_domain_needs_a_public_address(box):
    r = run(box, "--yes", "--domain", "auto", "--admin", "alice:Alice", FAKE_IPS="10.0.0.5")
    assert r.returncode == 1 and "no public IPv4" in r.stderr


@pytest.mark.parametrize("domain", ["localhost", "203.0.113.10", "Bad Name.com", "a..b", "-x.com"])
def test_bad_domains_are_refused(box, domain):
    r = run(box, "--yes", "--domain", domain, "--admin", "alice:Alice")
    assert r.returncode == 1 and "not a valid domain" in r.stderr


@pytest.mark.parametrize("admin", ["Alice:Alice", "alice", "alice:", "shared:Shared", "1abc:X",
                                   "al ice:Al"])
def test_bad_admins_are_refused(box, admin):
    r = run(box, "--yes", "--domain", "board.example.com", "--admin", admin)
    assert r.returncode == 1 and "not a valid admin" in r.stderr


def test_yes_without_the_needed_answers_fails_instead_of_hanging(box):
    r = run(box, "--yes")
    assert r.returncode == 1 and "Not given" in r.stderr


def test_it_asks_when_run_without_options(box):
    env, _ = box
    # a pseudo-terminal is needed for the prompts; feed answers through script(1) if present
    pytest.importorskip("pty")
    import pty
    answers = b"board.example.com\nalice:Alice\n\n"
    master, slave = pty.openpty()
    p = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdin=slave, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    os.write(master, answers)
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0, out + err
    assert "alice:Alice" in "\n".join(calls(box))


def test_a_port_already_in_use_is_refused(box):
    r = run(box, *GOOD, FAKE_PORT_BUSY="1")
    assert r.returncode == 1 and "port 80 is already in use" in r.stderr


def test_a_wrong_download_is_refused_before_it_is_installed(box):
    bad = box[1] / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        tar.add(ROOT / "LICENSE", arcname="x/LICENSE")
    r = run(box, *GOOD, FAKE_TARBALL=str(bad))
    assert r.returncode == 1 and "does not contain" in r.stderr
    assert not (box[1] / "opt/docker-compose.yml").exists()


def test_a_failing_doctor_makes_the_script_fail_but_still_prints_the_link(box):
    r = run(box, *GOOD, FAKE_DOCTOR_RC="1")
    assert r.returncode == 1 and LINK in r.stdout


def test_a_second_run_upgrades_keeps_env_backs_up_first_and_does_not_reprovision(box):
    assert run(box, *GOOD).returncode == 0
    env_file = box[1] / "opt/.env"
    env_file.write_text(env_file.read_text() + "BOARD_HTTPS_PORT=8443\n")
    (box[1] / "calls").write_text("")
    r = run(box, "--yes", FAKE_RUNNING="1")
    assert r.returncode == 0, r.stderr
    assert "BOARD_HTTPS_PORT=8443" in env_file.read_text()
    c = calls(box)
    backup = next(i for i, line in enumerate(c) if "admin_cli backup /data/pre-upgrade-" in line)
    up = next(i for i, line in enumerate(c) if "up -d --build" in line)
    assert backup < up
    assert not any("provision" in line for line in c)
    assert not any(line.startswith("ufw") for line in c)
    assert LINK not in r.stdout
    assert "== Upgraded" in r.stdout and "setup link" not in r.stdout


def test_an_upgrade_stops_if_the_backup_fails(box):
    assert run(box, *GOOD).returncode == 0
    stub(box[1] / "bin/docker",
         'case "$*" in *"ps -q board"*) echo abc;; *"admin_cli backup"*) exit 1;; '
         '*) echo "$*" >> ' + shlex.quote(str(box[1] / "calls")) + ';; esac\n')
    (box[1] / "calls").write_text("")
    r = run(box, "--yes")
    assert r.returncode == 1 and "backup failed" in r.stderr
    assert not any("up -d" in line for line in calls(box))


def test_a_rerun_after_a_crash_between_start_and_provision_says_how_to_get_a_link(box):
    r = run(box, *GOOD, FAKE_PROVISIONED="1")
    assert r.returncode == 0
    assert "reset-admin NAME" in r.stdout and LINK not in r.stdout


def test_help_prints_the_options(box):
    r = run(box, "--help")
    assert r.returncode == 0 and "--domain" in r.stdout and "--admin" in r.stdout

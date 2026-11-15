"""scripts/update.sh (fuzzpan-update), scripts/update_remote.sh and scripts/release.sh
with stand-in commands. Nothing is downloaded, no SSH connection is made and
nothing is pushed: curl, ssh and uv are stubs, and the release tests use a
throwaway git repository with a local bare "origin"."""

import os
import shlex
import subprocess

import pytest

from tests.conftest import ROOT, stub

UPDATE = ROOT / "scripts" / "update.sh"
REMOTE = ROOT / "scripts" / "update_remote.sh"
RELEASE = ROOT / "scripts" / "release.sh"


# ------------------------------------------------------------ fuzzpan-update

@pytest.fixture
def server(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    calls.touch()
    opt = tmp_path / "opt"
    opt.mkdir()
    (opt / "docker-compose.yml").write_text("services: {}\n")
    (opt / ".fuzzpan-release").write_text("REPO=someone/fork\nREF=v0.1.0\n")
    # The installer a release would provide: records how it was run.
    installer = tmp_path / "installer.sh"
    installer.write_text('#!/bin/bash\necho "installer FUZZPAN_REPO=$FUZZPAN_REPO $*" >> '
                         + shlex.quote(str(calls)) + "\n")
    stub(bin_dir / "id", 'echo "${FAKE_UID:-0}"\n')
    stub(bin_dir / "curl",
         f'echo "curl $*" >> {shlex.quote(str(calls))}\n'
         '[ -n "$FAKE_GITHUB_DOWN" ] && exit 22\n'
         'out=""; while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; esac; shift; done\n'
         '[ -n "$out" ] && { cp "$FAKE_INSTALLER" "$out"; exit 0; }\n'
         'printf \'{"url":"x","tag_name":"%s","name":"y"}\' "${FAKE_LATEST:-v0.2.0}"\n')
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FUZZPAN_DIR": str(opt),
           "FAKE_INSTALLER": str(installer)}
    return env, tmp_path


def update(server, *args, **extra):
    env, _ = server
    return subprocess.run(["bash", str(UPDATE), *args], env={**env, **extra},
                          capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)


def server_calls(server):
    return (server[1] / "calls").read_text().splitlines()


def test_update_runs_the_installer_from_the_newest_release(server):
    r = update(server, "--yes")
    assert r.returncode == 0, r.stderr
    c = server_calls(server)
    assert any("api.github.com/repos/someone/fork/releases/latest" in line for line in c)
    assert any("raw.githubusercontent.com/someone/fork/v0.2.0/scripts/install.sh" in line for line in c)
    assert f"installer FUZZPAN_REPO=someone/fork --yes --ref v0.2.0 --dir {server[1] / 'opt'}" in c
    assert "Installed: v0.1.0" in r.stdout and "Newest:    v0.2.0" in r.stdout


def test_up_to_date_downloads_nothing(server):
    r = update(server, "--yes", FAKE_LATEST="v0.1.0")
    assert r.returncode == 0 and "Up to date." in r.stdout
    assert not any(" -o " in line or "installer" in line for line in server_calls(server))


def test_force_reinstalls_the_same_release(server):
    r = update(server, "--yes", "--force", FAKE_LATEST="v0.1.0")
    assert r.returncode == 0
    assert any(line.startswith("installer") for line in server_calls(server))


def test_check_only_reports_and_needs_no_root(server):
    r = update(server, "--check", FAKE_UID="1000")
    assert r.returncode == 0, r.stderr
    assert "An update is available" in r.stdout and "releases/tag/v0.2.0" in r.stdout
    assert not any("installer" in line or " -o " in line for line in server_calls(server))


def test_updating_needs_root(server):
    r = update(server, "--yes", FAKE_UID="1000")
    assert r.returncode == 1 and "sudo fuzzpan-update" in r.stderr
    assert not any("installer" in line for line in server_calls(server))


def test_without_a_terminal_it_asks_for_yes_instead_of_hanging(server):
    r = update(server)
    assert r.returncode == 1 and "--yes" in r.stderr
    assert not any("installer" in line for line in server_calls(server))


def test_a_named_release_skips_asking_github(server):
    r = update(server, "--yes", "--ref", "v0.3.0")
    assert r.returncode == 0, r.stderr
    c = server_calls(server)
    assert not any("api.github.com" in line for line in c)
    assert any("--ref v0.3.0" in line for line in c if line.startswith("installer"))


@pytest.mark.parametrize("ref", ["../etc", "-x", "v1;rm -rf /", "a b", "v0..1"])
def test_odd_release_names_are_refused(server, ref):
    r = update(server, "--yes", "--ref", ref)
    assert r.returncode == 1 and "not a valid release" in r.stderr
    assert server_calls(server) == []


def test_a_release_name_from_github_is_checked_too(server):
    r = update(server, "--yes", FAKE_LATEST="v1;reboot")
    assert r.returncode == 1 and "not a valid release" in r.stderr
    assert not any("installer" in line for line in server_calls(server))


def test_github_unreachable_says_what_to_do(server):
    r = update(server, "--yes", FAKE_GITHUB_DOWN="1")
    assert r.returncode == 1 and "--ref" in r.stderr


def test_a_download_that_is_not_the_installer_is_not_run(server):
    bad = server[1] / "bad.html"
    bad.write_text("<html>not found</html>\n")
    r = update(server, "--yes", FAKE_INSTALLER=str(bad))
    assert r.returncode == 1 and "not the installer" in r.stderr
    assert not any(line.startswith("installer") for line in server_calls(server))


def test_no_install_no_update(server, tmp_path):
    r = update(server, "--yes", FUZZPAN_DIR=str(tmp_path / "nowhere"))
    assert r.returncode == 1 and "no FuzzPan install" in r.stderr


def test_an_install_from_before_release_records_defaults_to_the_main_repository(server):
    (server[1] / "opt/.fuzzpan-release").unlink()
    r = update(server, "--yes")
    assert r.returncode == 0, r.stderr
    assert "unknown" in r.stdout
    assert any("repos/kvtaniguchi/fuzzpan/releases/latest" in line for line in server_calls(server))


# ---------------------------------------------------------- update_remote.sh

@pytest.fixture
def laptop(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    stub(bin_dir / "ssh", f'for a in "$@"; do printf "%s\\n" "$a"; done > {shlex.quote(str(calls))}\n')
    return {"PATH": f"{bin_dir}:/usr/bin:/bin"}, calls


def remote(laptop, *args, **extra):
    env, _ = laptop
    return subprocess.run(["sh", str(REMOTE), *args], env={**env, **extra},
                          capture_output=True, text=True, timeout=30)


def test_remote_runs_fuzzpan_update_over_ssh_with_a_terminal(laptop):
    r = remote(laptop, "--check", BOARD_SSH="root@board.example.com")
    assert r.returncode == 0, r.stderr
    argv = laptop[1].read_text().splitlines()
    assert argv[:2] == ["-t", "root@board.example.com"]
    command = "\n".join(argv[2:])
    assert "fuzzpan-update --dir /opt/fuzzpan --check" in command
    assert "sudo fuzzpan-update" in command  # for a user other than root
    assert "RELEASE/scripts/install.sh" in command  # what to do on an old install


def test_remote_passes_ref_yes_and_dir(laptop):
    r = remote(laptop, "--ref", "v0.3.0", "--yes", BOARD_SSH="deploy@board.example.com",
               BOARD_REMOTE_DIR="/srv/fuzzpan")
    assert r.returncode == 0, r.stderr
    assert "fuzzpan-update --dir /srv/fuzzpan --ref v0.3.0 --yes" in laptop[1].read_text()


def test_remote_needs_board_ssh(laptop):
    r = remote(laptop)
    assert r.returncode == 1 and "BOARD_SSH" in r.stderr


@pytest.mark.parametrize("extra", [
    {"BOARD_SSH": "root@host;reboot"}, {"BOARD_SSH": "-oProxyCommand=x"},
    {"BOARD_SSH": "root@host", "BOARD_REMOTE_DIR": "/opt/x;reboot"},
])
def test_remote_refuses_odd_settings(laptop, extra):
    r = remote(laptop, **extra)
    assert r.returncode == 1 and "not allowed" in r.stderr
    assert not laptop[1].exists()


@pytest.mark.parametrize("args", [["--ref", "v1;reboot"], ["--ref", "-x"], ["--ref", "../x"], ["--wipe"]])
def test_remote_refuses_odd_options(laptop, args):
    r = remote(laptop, *args, BOARD_SSH="root@host")
    assert r.returncode == 1
    assert not laptop[1].exists()


# ---------------------------------------------------------------- release.sh

@pytest.fixture
def clone(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir / "uv", 'echo "uv $* in $PWD BOARD_DB=$BOARD_DB" >> "$FAKE_UV_LOG"; exit "${FAKE_UV_RC:-0}"\n')
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(home),
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
           "FAKE_UV_LOG": str(tmp_path / "uv.log")}

    def git(*args, cwd):
        subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    git("checkout", "-q", "-b", "main", cwd=work)
    (work / "README.md").write_text("hi\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "first", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    return env, work, git, tmp_path


def release(clone, *args, **extra):
    env, work, _, _ = clone
    return subprocess.run(["sh", str(RELEASE), *args], cwd=work, env={**env, **extra},
                          capture_output=True, text=True, timeout=60)


def tags(clone):
    env, work, _, _ = clone
    return subprocess.run(["git", "tag"], cwd=work, env=env, capture_output=True,
                          text=True).stdout.split()


def test_release_tests_origin_main_then_tags_it_locally_and_prints_how_to_publish(clone):
    r = release(clone, "v0.1.0")
    assert r.returncode == 0, r.stderr
    assert tags(clone) == ["v0.1.0"]
    uv = (clone[3] / "uv.log").read_text()
    assert "uv run pytest -q" in uv and "BOARD_DB=" in uv and str(clone[1]) + " " not in uv
    assert "git push origin v0.1.0" in r.stdout
    assert "gh release create v0.1.0 --verify-tag --generate-notes" in r.stdout
    # it pushed nothing
    env, work, _, tmp = clone
    remote_tags = subprocess.run(["git", "ls-remote", "--tags", str(tmp / "origin.git")],
                                 env=env, capture_output=True, text=True).stdout
    assert remote_tags == ""


def test_release_does_not_tag_when_the_tests_fail(clone):
    r = release(clone, "v0.1.0", FAKE_UV_RC="1")
    assert r.returncode == 1 and "tests failed" in r.stderr
    assert tags(clone) == []


def test_release_refuses_an_existing_or_older_version(clone):
    assert release(clone, "v0.2.0").returncode == 0
    r = release(clone, "v0.2.0")
    assert r.returncode == 1 and "already exists" in r.stderr
    r = release(clone, "v0.1.9")
    assert r.returncode == 1 and "not newer" in r.stderr


@pytest.mark.parametrize("version", ["0.1.0", "v1.2", "v01.2.3", "v1.2.3-rc1", "latest"])
def test_release_refuses_odd_version_names(clone, version):
    r = release(clone, version)
    assert r.returncode == 1 and "not a version" in r.stderr

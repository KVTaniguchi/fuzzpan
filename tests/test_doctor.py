"""scripts/doctor.py with stand-in commands and a stand-in HTTPS fetch: no Docker,
network or root needed. What matters is that each way an install can be broken or
unsafe is reported as FAIL or WARN, and that a healthy install passes."""

import importlib.util
import json
import ssl
import urllib.error

import pytest

from tests.conftest import ROOT

spec = importlib.util.spec_from_file_location("doctor", ROOT / "scripts" / "doctor.py")
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)

GOOD_CONFIG = {"services": {"board": {"environment": {"BOARD_DB": "/data/board.db"}},
                            "caddy": {"ports": [{"published": "443"}]}}}
GOOD_PS = [{"Service": "board", "State": "running", "Health": "healthy"},
           {"Service": "caddy", "State": "running", "Health": ""}]
GOOD_SSHD = "port 22\npasswordauthentication no\n"


def make_runner(config=GOOD_CONFIG, ps=GOOD_PS, ufw="Status: active\n", sshd=GOOD_SSHD, dpkg=0):
    def runner(cmd, cwd=None):
        if cmd[:3] == ["docker", "compose", "config"]:
            return (0, json.dumps(config)) if config is not None else (1, "")
        if cmd[:3] == ["docker", "compose", "ps"]:
            return (0, "\n".join(json.dumps(r) for r in ps)) if ps is not None else (1, "")
        if cmd[0] == "ufw":
            return (0, ufw) if ufw is not None else (127, "")
        if cmd[0] == "sshd":
            return (0, sshd) if sshd is not None else (1, "")
        if cmd[0] == "dpkg":
            return dpkg, ""
        raise AssertionError(cmd)
    return runner


def healthy_fetch(url):
    return 200, '{"status":"ok"}'


@pytest.fixture
def install(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / ".env").write_text("BOARD_DOMAIN=board.example.com\n"
                                   "BOARD_PUBLIC_URL=https://board.example.com\n")
    return tmp_path


def results(install, **kw):
    fetch = kw.pop("fetch", healthy_fetch)
    ctx = doctor.Context(install, run_fn=make_runner(**kw), fetch_fn=fetch)
    return {r.name: r for r in doctor.run_checks(ctx)}


def test_a_healthy_install_has_no_failures_or_warnings(install):
    r = results(install)
    assert {x.level for x in r.values()} <= {doctor.OK, doctor.INFO}, r
    assert r["reachable"].level == doctor.OK


def test_backups_are_always_a_reminder_never_a_pass(install):
    assert results(install)["backups"].level == doctor.INFO


def test_no_public_url_fails(install):
    (install / ".env").write_text("")
    assert results(install)["address"].level == doctor.FAIL


def test_http_address_fails(install):
    (install / ".env").write_text("BOARD_PUBLIC_URL=http://board.example.com\n")
    assert results(install)["address"].level == doctor.FAIL


def test_localhost_is_only_a_trial_warning_and_skips_the_network_check(install):
    (install / ".env").write_text("BOARD_PUBLIC_URL=https://localhost\n")

    def no_network(url):
        raise AssertionError("must not fetch a localhost address")
    r = results(install, fetch=no_network)
    assert r["address"].level == doctor.WARN
    assert r["reachable"].level == doctor.INFO


def test_domain_alone_is_enough(install):
    (install / ".env").write_text("BOARD_DOMAIN=board.example.com\n")
    assert results(install)["address"].level == doctor.OK


@pytest.mark.parametrize("error, word", [
    (urllib.error.URLError(ssl.SSLCertVerificationError("bad")), "certificate"),
    (urllib.error.URLError(OSError(-2, "Name or service not known")), "resolve"),
    (urllib.error.URLError(ConnectionRefusedError()), "could not connect"),
])
def test_unreachable_board_fails_with_a_reason(install, error, word):
    def broken(url):
        raise error
    r = results(install, fetch=broken)["reachable"]
    assert r.level == doctor.FAIL and word in r.detail and r.fix


def test_unhealthy_answer_fails(install):
    r = results(install, fetch=lambda url: (502, "bad gateway"))["reachable"]
    assert r.level == doctor.FAIL


def test_a_published_board_port_fails(install):
    config = {"services": {"board": {"ports": [{"published": "8787", "target": 8787}]}}}
    assert results(install, config=config)["port 8787"].level == doctor.FAIL


def test_unreadable_compose_config_is_a_warning_not_a_pass(install):
    assert results(install, config=None)["port 8787"].level == doctor.WARN


def test_insecure_cookies_fail(install):
    config = {"services": {"board": {"environment": {"BOARD_SECURE_COOKIES": "0"}}}}
    assert results(install, config=config)["secure cookies"].level == doctor.FAIL


def test_insecure_cookies_in_env_file_fail_too(install):
    (install / ".env").write_text("BOARD_PUBLIC_URL=https://b.example.com\n"
                                  "BOARD_SECURE_COOKIES=0\n")
    assert results(install)["secure cookies"].level == doctor.FAIL


def test_stopped_or_unhealthy_containers_fail(install):
    ps = [{"Service": "board", "State": "running", "Health": "unhealthy"},
          {"Service": "caddy", "State": "exited", "Health": ""}]
    r = results(install, ps=ps)["containers"]
    assert r.level == doctor.FAIL and "caddy is not running" in r.detail \
        and "unhealthy" in r.detail


def test_compose_ps_array_format_is_understood(install, monkeypatch):
    runner = make_runner()

    def array_runner(cmd, cwd=None):
        if cmd[:3] == ["docker", "compose", "ps"]:
            return 0, json.dumps(GOOD_PS)
        return runner(cmd, cwd)
    ctx = doctor.Context(install, run_fn=array_runner, fetch_fn=healthy_fetch)
    assert doctor.check_containers(ctx).level == doctor.OK


def test_inactive_or_missing_firewall_warns(install):
    assert results(install, ufw="Status: inactive\n")["firewall"].level == doctor.WARN
    assert results(install, ufw=None)["firewall"].level == doctor.WARN


def test_ssh_password_login_warns_and_unreadable_settings_are_only_info(install):
    assert results(install, sshd="passwordauthentication yes\n")["ssh"].level == doctor.WARN
    assert results(install, sshd=None)["ssh"].level == doctor.INFO


def test_missing_unattended_upgrades_warns(install):
    assert results(install, dpkg=1)["security updates"].level == doctor.WARN
    assert results(install, dpkg=127)["security updates"].level == doctor.INFO


def test_nothing_it_prints_contains_env_file_values_beyond_the_address(install):
    (install / ".env").write_text("BOARD_PUBLIC_URL=https://b.example.com\nSECRET_THING=hunter2\n")
    text = doctor.render(list(results(install).values()))
    assert "hunter2" not in text


def test_exit_status_follows_failures_not_warnings(install, monkeypatch):
    monkeypatch.setattr(doctor, "run", make_runner(ufw=None))
    monkeypatch.setattr(doctor, "fetch", healthy_fetch)
    assert doctor.main(["--dir", str(install)]) == 0      # a warning alone passes
    (install / ".env").write_text("")
    assert doctor.main(["--dir", str(install)]) == 1      # a failure does not
    assert doctor.main(["--dir", str(install / "nowhere")]) == 2


def test_json_output_is_a_list_of_results(install, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "run", make_runner())
    monkeypatch.setattr(doctor, "fetch", healthy_fetch)
    doctor.main(["--dir", str(install), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {"level", "name", "detail", "fix"} <= set(data[0])

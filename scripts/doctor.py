#!/usr/bin/env python3
"""Check a FuzzPan install on the server it runs on.

    python3 scripts/doctor.py [--dir /opt/fuzzpan] [--json]

Run it on the VPS, as root if you can (the firewall and SSH checks need root).
It only reads: it changes nothing, sends nothing anywhere except one request to
the board's own public address, and never prints a secret. The standard library
is all it needs, so it runs before anything else is installed.

Each check prints one line: [ok], [info], [warn] or [FAIL], with what to do for
anything that is not ok. The exit status is 1 if any check FAILED (something is
broken or unsafe), otherwise 0. A warning is worth fixing but does not fail.
"""

import argparse
import json
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

OK, INFO, WARN, FAIL = "ok", "info", "warn", "FAIL"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass
class Result:
    level: str
    name: str
    detail: str
    fix: str = ""


def read_env(path):
    """KEY=VALUE lines of a .env file. No expansion; # starts a comment line."""
    values = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def run(cmd, cwd=None):
    """(exit status, stdout) of a command; (127, '') if it is not installed."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return p.returncode, p.stdout


def fetch(url):
    """(status, body) of a GET with the certificate VERIFIED. Raises on failure."""
    request = urllib.request.Request(url, headers={"User-Agent": "fuzzpan-doctor"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.read(4096).decode("utf-8", "replace")


class Context:
    def __init__(self, directory, run_fn=None, fetch_fn=None):
        self.dir = Path(directory)
        self.run = run_fn or run
        self.fetch = fetch_fn or fetch
        self.env = read_env(self.dir / ".env")
        self._config = None

    def compose_config(self):
        """`docker compose config` as a dict, or None if it cannot be read."""
        if self._config is None:
            rc, out = self.run(["docker", "compose", "config", "--format", "json"],
                               cwd=str(self.dir))
            try:
                self._config = json.loads(out) if rc == 0 else False
            except ValueError:
                self._config = False
        return self._config or None

    def public_url(self):
        url = self.env.get("BOARD_PUBLIC_URL")
        if not url and self.env.get("BOARD_DOMAIN"):
            url = f"https://{self.env['BOARD_DOMAIN']}"
        return (url or "").rstrip("/")


def check_address(ctx):
    url = ctx.public_url()
    if not url:
        return Result(FAIL, "address", "BOARD_PUBLIC_URL is not set in .env",
                      f"Set BOARD_DOMAIN and BOARD_PUBLIC_URL in {ctx.dir}/.env, then "
                      "run: docker compose up -d")
    parts = urlparse(url)
    if parts.scheme != "https":
        return Result(FAIL, "address", f"{url} is not https",
                      "A bearer token must only travel over https. Use https://your-domain.")
    if parts.hostname in LOCAL_HOSTS:
        return Result(WARN, "address", f"{url} is the local trial address",
                      "Fine for a trial on your own machine. For a real board, set "
                      "BOARD_DOMAIN to a domain that points at this server.")
    return Result(OK, "address", url)


def check_reachable(ctx):
    url = ctx.public_url()
    host = urlparse(url).hostname if url else None
    if not host or host in LOCAL_HOSTS or urlparse(url).scheme != "https":
        return Result(INFO, "reachable", "skipped: no public https address to test")
    try:
        status, body = ctx.fetch(url + "/healthz")
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return Result(FAIL, "reachable", f"the certificate for {host} did not verify",
                          "Check that the domain's DNS record points at this server and that "
                          "ports 80 and 443 are open, then wait a minute: Caddy fetches the "
                          "certificate once it can be reached. See: docker compose logs caddy")
        if "Name or service not known" in str(reason) or "No address associated" in str(reason) \
                or "Temporary failure in name resolution" in str(reason):
            return Result(FAIL, "reachable", f"{host} does not resolve",
                          "Create a DNS A record for the domain pointing at this server's IP "
                          "address. It can take a few minutes to spread.")
        return Result(FAIL, "reachable", f"could not connect to {url}: {reason}",
                      "Check that ports 80 and 443 are open in the firewall (and in the "
                      "provider's firewall, if it has one) and that the stack is running.")
    except OSError as e:
        return Result(FAIL, "reachable", f"could not connect to {url}: {e}",
                      "Check that ports 80 and 443 are open and the stack is running.")
    if status == 200 and '"status":"ok"' in body.replace(" ", ""):
        return Result(OK, "reachable", f"{url}/healthz answers over https with a valid certificate")
    return Result(FAIL, "reachable", f"{url}/healthz answered {status}, not a healthy board",
                  "See: docker compose logs board")


def check_containers(ctx):
    rc, out = ctx.run(["docker", "compose", "ps", "--format", "json"], cwd=str(ctx.dir))
    if rc == 127:
        return Result(FAIL, "containers", "docker is not installed", "Re-run install.sh.")
    if rc != 0:
        return Result(FAIL, "containers", "docker compose could not list the stack",
                      f"Run it by hand in {ctx.dir}: docker compose ps")
    rows = []
    out = out.strip()
    try:
        parsed = json.loads(out) if out.startswith("[") else \
            [json.loads(line) for line in out.splitlines() if line.strip()]
        rows = parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        return Result(WARN, "containers", "could not read the output of docker compose ps")
    by_service = {r.get("Service"): r for r in rows}
    problems = []
    for service in ("board", "caddy"):
        row = by_service.get(service)
        if not row or row.get("State") != "running":
            problems.append(f"{service} is not running")
    board = by_service.get("board")
    if board and board.get("State") == "running" and board.get("Health") not in ("healthy", "", None):
        problems.append(f"board is {board.get('Health')}")
    if problems:
        return Result(FAIL, "containers", "; ".join(problems),
                      f"In {ctx.dir}: docker compose up -d, then docker compose logs board")
    return Result(OK, "containers", "board and caddy are running, board is healthy")


def check_board_port(ctx):
    config = ctx.compose_config()
    if config is None:
        return Result(WARN, "port 8787", "could not read the compose configuration",
                      f"Run in {ctx.dir}: docker compose config")
    board = (config.get("services") or {}).get("board") or {}
    if board.get("ports"):
        return Result(FAIL, "port 8787", "the board service publishes a host port",
                      "Remove the `ports:` entry from the board service in docker-compose.yml. "
                      "The board trusts forwarded headers from whatever can reach it, so only "
                      "Caddy may.")
    return Result(OK, "port 8787", "the board's own port is not published")


def check_cookies(ctx):
    config = ctx.compose_config() or {}
    board_env = ((config.get("services") or {}).get("board") or {}).get("environment") or {}
    value = board_env.get("BOARD_SECURE_COOKIES", ctx.env.get("BOARD_SECURE_COOKIES"))
    if value == "0":
        return Result(FAIL, "secure cookies", "BOARD_SECURE_COOKIES=0 is set",
                      "Remove it. It is for development on your own machine only.")
    return Result(OK, "secure cookies", "on")


def check_firewall(ctx):
    rc, out = ctx.run(["ufw", "status"])
    if rc == 127:
        return Result(WARN, "firewall", "ufw is not installed",
                      "Allow only SSH, 80 and 443, here or in your provider's firewall. "
                      "install.sh does this for you.")
    if rc != 0:
        return Result(INFO, "firewall", "could not read ufw status (needs root)",
                      "Run the doctor with sudo to check this.")
    if "Status: active" in out:
        return Result(OK, "firewall", "ufw is active")
    return Result(WARN, "firewall", "ufw is installed but not active",
                  "Run: sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable "
                  "(allow SSH first, or you will lock yourself out).")


def check_ssh(ctx):
    rc, out = ctx.run(["sshd", "-T"])
    if rc != 0:
        return Result(INFO, "ssh", "could not read the SSH server settings (needs root)",
                      "Run the doctor with sudo to check this.")
    settings = {}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        settings[key.lower()] = value.strip().lower()
    if settings.get("passwordauthentication") == "yes":
        return Result(WARN, "ssh", "password login over SSH is on",
                      "Use SSH keys only: set PasswordAuthentication no in "
                      "/etc/ssh/sshd_config, then restart ssh. Test a key login in a second "
                      "window before closing this one.")
    return Result(OK, "ssh", "password login is off")


def check_updates(ctx):
    rc, _ = ctx.run(["dpkg", "-s", "unattended-upgrades"])
    if rc == 127:
        return Result(INFO, "security updates", "not a Debian or Ubuntu system, so not checked")
    if rc != 0:
        return Result(WARN, "security updates", "unattended-upgrades is not installed",
                      "Run: sudo apt-get install -y unattended-upgrades")
    return Result(OK, "security updates", "automatic security updates are installed")


def check_disk(ctx):
    try:
        free = shutil.disk_usage(ctx.dir).free
    except OSError:
        return Result(INFO, "disk", "could not read free space")
    gib = free / 2**30
    if gib < 1:
        return Result(WARN, "disk", f"only {gib:.1f} GiB free",
                      "Free some space (docker image prune) or use a bigger disk.")
    return Result(OK, "disk", f"{gib:.1f} GiB free")


def check_backups(ctx):
    return Result(INFO, "backups", "this check cannot see backups kept on another computer",
                  "Copy the board's database to your own computer regularly with "
                  "scripts/backup_pull.sh (see the README, 'Backups and restore'). A server "
                  "that dies takes the only copy with it.")


CHECKS = [check_address, check_reachable, check_containers, check_board_port, check_cookies,
          check_firewall, check_ssh, check_updates, check_disk, check_backups]


def run_checks(ctx):
    return [check(ctx) for check in CHECKS]


def render(results):
    lines = []
    for r in results:
        lines.append(f"[{r.level:>4}] {r.name}: {r.detail}")
        if r.fix and r.level != OK:
            lines.append(f"       -> {r.fix}")
    failed = sum(r.level == FAIL for r in results)
    warned = sum(r.level == WARN for r in results)
    lines.append("")
    lines.append(f"{failed} failed, {warned} to look at." if failed or warned else "All good.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check a FuzzPan install.")
    parser.add_argument("--dir", default="/opt/fuzzpan",
                        help="the install folder holding docker-compose.yml and .env")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args(argv)
    if not (Path(args.dir) / "docker-compose.yml").exists():
        print(f"doctor: no docker-compose.yml in {args.dir}; use --dir.", file=sys.stderr)
        return 2
    results = run_checks(Context(args.dir))
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        print(render(results))
    return 1 if any(r.level == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())

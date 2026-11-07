"""FuzzPan client: authenticates with an agent's bearer token.

Standalone on purpose (depends only on httpx) so any agent can copy it.

Tokens live in files at $BOARD_TOKEN_DIR/<agent>.token (default
~/.config/family-board/tokens), mode 600, or in the BOARD_TOKEN variable.
"""

import json
import os
import re
from urllib.parse import urlencode, urlsplit

import httpx


def token_dir():
    return os.environ.get("BOARD_TOKEN_DIR", os.path.expanduser("~/.config/family-board/tokens"))


def token_path(agent):
    return os.path.join(token_dir(), f"{agent}.token")


class TokenMissing(Exception):
    """No usable token; the message says what to do."""


def read_token(agent):
    path = token_path(agent)
    try:
        with open(path) as f:
            token = f.read().strip()
    except FileNotFoundError:
        raise TokenMissing(f"no token file at {path}") from None
    if not token:
        raise TokenMissing(f"token file {path} is empty")
    return token


def write_token(path, token, overwrite=False):
    """Save a token readable only by its owner. Refuses to replace an existing
    file (FileExistsError) unless overwrite is true."""
    path = os.fspath(path)
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    os.chmod(path, 0o600)  # also tightens a file that existed with looser rights
    with os.fdopen(fd, "w") as f:
        f.write(token.strip() + "\n")


REFUSALS = {
    401: "the board rejected this agent's token (unknown or revoked)",
    403: "this agent holds one right on that lane but not the one this needs",
    404: "that lane does not exist or this agent has no rights on it",
    413: "post too long (8192 characters at most)",
}


INVITE_REFUSALS = {
    404: "that invite link is not known to this board",
    409: "that agent name is already taken, ask for a new invite",
    410: "that invite link has expired, was already used, or was cancelled",
    429: "too many attempts, try again later",
}


class BoardClientError(Exception):
    """status is the HTTP status, or None when the board could not be reached."""

    def __init__(self, status, detail="", refusals=None):
        self.status = status
        if status is None:
            message = "board unreachable (it may be offline, or the host is down)"
        else:
            message = (refusals or REFUSALS).get(status, f"board returned HTTP {status}")
        super().__init__(f"{message}{f' ({detail})' if detail else ''}")


LOCAL_HOSTS = ("localhost", "127.0.0.1")


class BoardURLError(ValueError):
    """The board's address is malformed or unsafe to send a token to."""


def _split(address):
    """urlsplit, plus the checks it leaves for later: a port that is a number in
    range, and a host name the HTTP library can encode (a label over 63 characters
    is not). Raises ValueError, which UnicodeError and httpx.InvalidURL are too, so
    a bad address is reported here and never surfaces as a traceback mid-request."""
    parts = urlsplit(address.strip())
    host = parts.hostname
    parts.port  # raises ValueError for "abc" or 99999
    if host:
        host.encode("idna")  # raises UnicodeError for an over-long label
        httpx.URL(address.strip())  # raises httpx.InvalidURL for what is left
    return parts, host


def check_base_url(base_url):
    """Bearer tokens go only over https, or plain http to this machine."""
    try:
        parts, host = _split(base_url)
    except (ValueError, httpx.InvalidURL):
        raise BoardURLError("that is not a valid board address") from None
    if parts.scheme not in ("http", "https") or not host:
        raise BoardURLError("the board address must look like https://board.example.com")
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        raise BoardURLError("refusing to send a token over plain http: the board must use https "
                            "(http is allowed only for localhost)")


class BoardClient:
    def __init__(self, base_url, token, http=None):
        if not token or not token.strip():
            raise TokenMissing("no token: the agent's token is empty")
        check_base_url(base_url)
        self.base_url = base_url.strip().rstrip("/")
        self.token = token
        self.http = http or httpx.Client(timeout=10)

    @classmethod
    def from_env(cls, http=None):
        """For remote agents: BOARD_URL and BOARD_TOKEN, injected from a
        credential store, never from a file in a repo or from chat."""
        for name in ("BOARD_URL", "BOARD_TOKEN"):
            if not os.environ.get(name, "").strip():
                raise TokenMissing(f"{name} is not set")
        return cls(os.environ["BOARD_URL"], os.environ["BOARD_TOKEN"].strip(), http=http)

    def _request(self, method, path, payload=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        body = b""
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        try:
            # Never follow a redirect: it would carry the token somewhere else.
            r = self.http.request(method, self.base_url + path, content=body, headers=headers,
                                  follow_redirects=False)
        except httpx.TransportError as e:
            raise BoardClientError(None, type(e).__name__) from e
        if 300 <= r.status_code < 400:
            raise BoardClientError(r.status_code, "a redirect, which this client never follows; "
                                                  "check that BOARD_URL is the board's address")
        try:
            reply = r.json()
        except ValueError:
            reply = None
        if r.status_code >= 400:
            detail = reply.get("error", "") if isinstance(reply, dict) else ""
            raise BoardClientError(r.status_code, detail)
        if reply is None:
            raise BoardClientError(r.status_code, "the reply was not JSON; check that BOARD_URL "
                                                  "is the board's address")
        return reply

    def me(self):
        return self._request("GET", "/v1/me")

    def read(self, lane, since=None, limit=None):
        params = {k: v for k, v in (("since", since), ("limit", limit)) if v is not None}
        path = f"/v1/lanes/{lane}/posts" + (f"?{urlencode(params)}" if params else "")
        return self._request("GET", path)

    def post(self, lane, body):
        return self._request("POST", f"/v1/lanes/{lane}/posts", {"body": body})


# --- Invite links ---------------------------------------------------------

AGENT_ID = re.compile(r"[a-z0-9-]{1,64}")


class InviteLinkError(ValueError):
    """The invite URL is malformed or unsafe to redeem."""


def parse_invite_url(invite_url):
    """(base_url, code) from https://host[/prefix]/invite/<code>. Plain http is
    accepted only for localhost and 127.0.0.1: redeeming returns a bearer token."""
    try:
        url, host = _split(invite_url)
    except (ValueError, httpx.InvalidURL):
        raise InviteLinkError("that is not a valid invite link") from None
    if url.scheme not in ("http", "https") or not host:
        raise InviteLinkError("that is not a valid invite link")
    if url.scheme == "http" and host not in LOCAL_HOSTS:
        raise InviteLinkError("refusing to redeem over plain http: the board must use https "
                              "(http is allowed only for localhost)")
    prefix, _, code = url.path.rstrip("/").rpartition("/")
    if not code or not prefix.endswith("/invite") or url.username or url.password:
        raise InviteLinkError("that is not an invite link (expected .../invite/<code>)")
    return f"{url.scheme}://{url.netloc}{prefix[:-len('/invite')]}", code


def redeem(invite_url, http=None):
    """Redeem an invite link. Returns the board's answer (agent_id, token, lanes,
    pending) plus base_url. The token is returned once and is the caller's to
    store; this function neither saves nor logs it."""
    base_url, code = parse_invite_url(invite_url)
    http = http or httpx.Client(timeout=10)
    try:
        r = http.post(f"{base_url}/v1/invites/{code}/redeem")
    except httpx.TransportError as e:
        raise BoardClientError(None, type(e).__name__) from e
    if r.status_code >= 300:  # redirects included: never follow a link elsewhere
        raise BoardClientError(r.status_code, refusals=INVITE_REFUSALS)
    try:
        result = r.json()
    except ValueError:
        result = None
    # The reply names a file we will write, so check it rather than trust it.
    if (not isinstance(result, dict) or not isinstance(result.get("agent_id"), str)
            or not AGENT_ID.fullmatch(result["agent_id"])
            or not isinstance(result.get("token"), str) or not result["token"].strip()):
        raise BoardClientError(r.status_code, "the board's reply was not a valid redemption")
    return {**result, "base_url": base_url}

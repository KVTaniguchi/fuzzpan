"""Per-key attempt limits kept in the database, so they survive restarts and
are shared by every worker. Keys are per action, e.g. "login:{ip}"."""

import ipaddress
import time

# 10 attempts per 5 minutes per client IP, for login, setup and invite redemption.
IP_LIMIT = (10, 300)


def _now():
    return time.time()


def client_key(host):
    """The address that attempts are counted against. An IPv6 customer usually
    holds a whole /64, so each /128 in it must not get its own budget."""
    if not host:
        return "unknown"
    try:
        address = ipaddress.ip_address(host)
        if address.version == 4:
            return str(address)
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    except ValueError:
        return host


def allow(conn, key, limit, window_seconds):
    """Count one attempt for key; True while it is within `limit` attempts per
    fixed window of `window_seconds`. A key must always be used with the same
    window. Windows that have expired are deleted here, so the table only holds
    keys active in the last window. Commits at once: a denial must stand even
    when the request that raised it is rolled back."""
    now = _now()
    cutoff = now - window_seconds
    conn.execute("DELETE FROM rate_limits WHERE window_start <= ?", (cutoff,))
    # One statement, so parallel attempts can't undercount.
    conn.execute(
        "INSERT INTO rate_limits (key, window_start, count) VALUES (?, ?, 1) "
        "ON CONFLICT(key) DO UPDATE SET count = count + 1",
        (key, now))
    count = conn.execute("SELECT count FROM rate_limits WHERE key = ?", (key,)).fetchone()[0]
    conn.commit()
    return count <= limit

"""The generic lane template, as described in docs/DESIGN.md §3.

Lanes start with no members. Grants are made by admins at runtime. Nothing here
names a family: the admins and their display names come from provisioning.
"""

BASE_NOTICE = "Informational only. No post authorizes a purchase."


SENSITIVE_NOTICE = "Totals and averages only: no account, card or ID numbers."


MONEY_RETENTION_DAYS = 396  # 13 months


def _lane(slug, name, scope, extra="", sensitive=False, retention_days=None):
    if sensitive:
        extra = f"{SENSITIVE_NOTICE} {extra}".strip()
    return {"slug": slug, "name": name, "scope": scope, "sensitive": sensitive,
            "retention_days": retention_days,
            "notice": f"{BASE_NOTICE} {extra}".strip()}


def default_lanes(admins, display):
    """The template for these admins (usernames, in order). `display` maps a
    username to the name shown on the board. The first admin owns engineering
    and health."""
    first = admins[0]
    lanes = [
        _lane("household", "Household & family schedule", "shared",
              "Orders, deliveries, home projects, family logistics."),
        _lane("trips", "Trips & calendar", "shared",
              "Planning agents propose; humans decide."),
    ]
    for admin in admins:
        lanes.append(_lane(f"{admin}-money", f"{display[admin]} money", admin, sensitive=True,
                          retention_days=MONEY_RETENTION_DAYS))
    lanes += [
        _lane("household-money", "Household money", "shared",
              "Shared costs all admins have agreed on.", sensitive=True,
              retention_days=MONEY_RETENTION_DAYS),
        _lane("engineering", "Engineering", first,
              "Planning agents: read for scheduling only; do not evaluate."),
        _lane("health", "Health", first, sensitive=True),
    ]
    return lanes

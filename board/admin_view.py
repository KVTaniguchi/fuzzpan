"""What the admin page shows, worked out from the service's lists.

Kept apart from the template so the ordering (agents with the most access
first) and the agent-by-lane grid can be tested without rendering HTML. Nothing
here decides what an admin may do: the service still checks every change."""


def _plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def build_admin_view(agents, invites, lanes, pending):
    """`agents`, `invites`, `lanes` and `pending` are service.list_agents,
    list_invites, list_admin_lanes and list_pending."""
    holds = {}
    for lane in lanes:
        for g in lane["grants"]:
            holds[(lane["slug"], g["principal"])] = (g["read"], g["post"])
    # list_pending is oldest first, so the newest open change for a cell wins.
    waiting = {(p["lane"], p["principal"]): p for p in pending}

    active = sorted((a for a in agents if a["status"] == "active"),
                    key=lambda a: (-len(a["lanes"]), a["id"]))
    revoked = [a for a in agents if a["status"] != "active"]
    # Shared lanes first: they are the ones that involve another admin.
    columns = sorted(lanes, key=lambda l: (l["scope"] != "shared", l["slug"]))

    rows = []
    for agent in active:
        cells = []
        for lane in columns:
            read, post = holds.get((lane["slug"], agent["id"]), (False, False))
            cells.append({"lane": lane["slug"], "key": f"{lane['slug']}|{agent['id']}",
                          "read": read, "post": post, "editable": lane["administers"],
                          "shared": lane["scope"] == "shared",
                          "waiting": waiting.get((lane["slug"], agent["id"]))})
        rows.append({"agent": agent, "cells": cells})

    heads = []
    for i, lane in enumerate(columns):
        members = sum(1 for r in rows if r["cells"][i]["read"] or r["cells"][i]["post"])
        heads.append({"slug": lane["slug"], "scope": lane["scope"],
                      "shared": lane["scope"] == "shared", "administers": lane["administers"],
                      "members": _plural(members, "agent", "agents")})

    connected = sum(1 for a in active if a["connection"] == "connected")
    parts = [f"{connected} connected"]
    if len(active) > connected:
        parts.append(f"{len(active) - connected} waiting to connect")
    if invites:
        parts.append(_plural(len(invites), "link not used yet", "links not used yet"))
    if revoked:
        parts.append(f"{len(revoked)} revoked")
    return {"active": active, "revoked": revoked, "columns": heads, "rows": rows,
            "summary": " · ".join(parts)}


def grants_notice(saved, queued):
    """The banner shown after the lane grid is saved. `saved` changes are in
    effect; `queued` wait for another admin."""
    if not saved and not queued:
        return "Nothing to change."
    parts = []
    if saved:
        parts.append(f"Saved {_plural(saved, 'change', 'changes')}.")
    if queued:
        parts.append(f"{_plural(queued, 'change', 'changes')} on shared lanes "
                     f"{'is' if queued == 1 else 'are'} waiting for another admin to approve.")
    return " ".join(parts)

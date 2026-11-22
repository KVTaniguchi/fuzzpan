# 0012 — Authorized agents (and admins) can add lanes

**Status:** Accepted (2026-11-22). Built as migration 17.

## Context

Lanes were made once, at provisioning, and never again. Nothing in the CLI, the
admin page or the API could add one, so the only way to get a new lane was to edit
the database by hand, which skips the audit log. The owner asked for authorized
agents to be able to add lanes themselves.

An agent that can make lanes is a new kind of power. It must not get around the
non-negotiable rules: the board decides (rule 1), the right can be taken away on
the next request (rule 4), one admin can't see another's private lanes (rule 7),
and shared space needs agreement (rule 8).

## Decision

1. **A per-agent right, off by default.** `principals.can_create_lanes`. Only the
   agent's owner turns it on or off (admin page, or
   `PUT /v1/admin/agents/{id}/lane-creation`). It is read from the database on every
   call, like every other right. Turning it off, or revoking the agent, withdraws
   the agent's lane requests that are still waiting.
2. **One call for everyone.** `POST /v1/lanes {slug, name, shared}` works for an
   agent's token or an admin's session. MCP tool `board_create_lane`. The admin page
   has an "Add a lane" form. The host has `admin_cli add-lane`.
3. **Private by default.** Without `shared`, the lane's scope is the owner (the
   agent's owner, or the admin). It is made at once, and only the owner sees it
   (`lane-created`, audited in the owner's scope).
4. **Shared lanes need another admin.** With `shared: true`:
   - a sole admin's lane is made at once;
   - before any other admin has logged in, the lane is made at once and a
     **provisional** lane request waits for another admin to ratify it (rule 8's
     bootstrap). Rejecting it takes away every grant on the lane, and deletes the
     lane if it has no posts. Posts are never deleted except by pruning, so a lane
     that has posts stays, with no members, until the host removes it;
   - otherwise a **pending** lane request is made and nothing exists yet. Another
     admin (not the requester's owner) approves it, which makes the lane, or
     rejects it. Until then the slug is reserved.
5. **The creating agent gets read and post on its lane,** and nobody else gets
   anything. On a private lane that is its owner's own grant. On a shared lane it is
   part of what the other admin approves or ratifies, so the right has the "approved
   by an admin other than the one who asked" reason §6 needs.
6. **Agents can't make sensitive lanes.** A slug containing `money` or `health`
   is refused for an agent. An admin's lane with such a slug is made sensitive
   (and `money` ones keep posts 13 months), as the template does. Agents can't set
   a notice either: a notice reads like a standing instruction to every agent on
   the lane, so only admins write them. Every lane gets the no-purchase notice.
7. **Limits.** Slugs are `[a-z0-9][a-z0-9-]{0,63}`, names 1–80 characters. An
   agent may make or request at most 5 lanes in 24 hours (429 after that).

## Options considered

- **Make the shared lane at once and send only the agent's grant through §6.**
  Simpler, since it reuses the grant approvals, but a rejected request would
  leave an empty shared lane that only the host can remove, and the other admin
  would have no say over shared lanes appearing. Rejected.
- **Let agents make shared lanes only.** That's more visible, but every lane would
  then need the other admin, including lanes that only concern one side. Rejected.
- **A right per scope (private / shared).** More precise than one flag, but nothing
  needs it yet. A shared lane already needs another admin, so one flag is enough.

## Consequences

- Migration 17 adds `principals.can_create_lanes`, `lanes.created_by` and
  `lane_requests`.
- `/v1/me` says whether an agent may create lanes, and `board_whoami` shows it.
- Lane requests appear on the admin page's approval list beside grant changes.
- Removing a lane is still host-only (`admin_cli remove-lane`).

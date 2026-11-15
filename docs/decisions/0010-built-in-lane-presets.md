# 0010 — Built-in lane presets, then "Choose my own"

**Status:** Accepted (2026-11-15).

## Context

Every new agent meant ticking the same boxes again, on the OAuth lane step and on
the invite form. The onboarding roadmap (item 4) proposed presets that each admin
saves on the admin page. But a new admin has none saved, so the people who most
need fewer clicks would get no help. The goal stays the one in the roadmap: once the
board knows it's you, connecting is *paste one thing, tap once, pick a preset*.

## Decision

1. **Presets are built in and worked out from your lanes.** There's no table and
   nothing to set up. From the lanes the admin administers (rule 7: nothing else):
   - **Shared lanes**: read and post on every shared lane.
   - **Shared and my own lanes**: read and post on those plus the admin's private
     lanes.
   - **Read only**: read on all of those, post on none.
2. **Sensitive lanes are never in a preset.** Money and health lanes can only be
   added by ticking them under "Choose my own". Adding one should always be a
   deliberate choice.
3. **Presets come first, then "Choose my own".** The first preset that applies is
   picked by default. "Choose my own" shows the old tick table, with its old
   defaults (shared ticked, private not). It works without JavaScript: CSS shows the
   table when that option is picked, so the OAuth page's CSP is unchanged.
4. **A preset fills the form. It grants nothing.** On submit, the board works the
   preset out again from the lanes the admin administers at that moment, and
   ignores any ticks sent with it. The lanes then apply exactly as before: on
   the OAuth path when the code is spent, and on the invite path when the link
   is redeemed. Both go through `set_grant`, so shared lanes still follow §6.
5. **A preset that adds nothing is left out.** If it gives no lanes, or the same
   lanes as one shown above it, it isn't offered. (For example, an admin whose only
   private lane is a money lane doesn't see "Shared and my own lanes".)
6. **Old forms keep working.** A submit with no `preset` field uses the ticks, as
   before. An unknown preset is refused with 400 before the lane ticket is used, so
   the admin can try again.

## Options considered

- **Presets saved by each admin (the roadmap's plan).** These are flexible, but
  empty for new admins, and they need a new table and a new admin page section.
  Saving the custom table as a preset could still be added later, on top of this.
- **Change the default ticks only.** This saves no clicks for anyone whose agent
  needs something other than the default.
- **Include sensitive lanes in "everything".** That's one tap to give an agent your
  money lane. Rejected.

## Consequences

- No migration. `list_admin_lanes` now also returns `sensitive`.
- The presets live in `board/presets.py`, and both screens use one partial,
  `_lane_picker.html`.
- The welcome page (`/welcome`, the first agent) keeps its simple lane list for
  now.

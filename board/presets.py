"""Built-in lane presets (decisions/0010).

A preset fills the lane form; it grants nothing. The board works each one out from
the lanes the admin administers at the moment the form is shown or submitted, so a
preset can never name a lane the admin doesn't administer (rule 7). Sensitive lanes
(money, health) are never in a preset: adding one is always a deliberate tick.
"""

CUSTOM = "custom"

_PRESETS = (
    ("shared", "Shared lanes", "Read and post on the lanes every admin shares.",
     lambda l: l["scope"] == "shared", True),
    ("everyday", "Shared and my own lanes", "Read and post on the shared lanes and your "
     "own private ones.", lambda l: True, True),
    ("read-only", "Read only", "Read the shared lanes and your own private ones. Can't post.",
     lambda l: True, False),
)

IDS = frozenset(p[0] for p in _PRESETS)


def presets(lanes):
    """The presets for these lanes (the admin's own, from list_admin_lanes, already
    limited to lanes they administer). A preset that would give no lanes, or exactly
    what an earlier one gives, is left out."""
    out, seen = [], []
    for pid, name, summary, include, post in _PRESETS:
        chosen = {l["slug"]: {"read": True, "post": post}
                  for l in lanes if not l.get("sensitive") and include(l)}
        if not chosen or chosen in seen:
            continue
        seen.append(chosen)
        out.append({"id": pid, "name": name, "summary": summary, "lanes": chosen,
                    "lane_names": [l["name"] for l in lanes if l["slug"] in chosen]})
    return out


def expand(lanes, preset_id):
    """The lanes a preset gives, or None if it isn't offered for these lanes."""
    for p in presets(lanes):
        if p["id"] == preset_id:
            return p["lanes"]
    return None

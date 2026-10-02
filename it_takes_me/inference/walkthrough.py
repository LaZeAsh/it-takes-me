"""Chapter walkthroughs given to the model, adapted from the It Takes Two wiki.

Source: https://ittakestwo.fandom.com/wiki/The_Shed (the wiki only covers the opening section,
"Wake-up Call", so later sections are left to the on-screen markers).
"""

from __future__ import annotations

WALKTHROUGHS: dict[str, list[str]] = {
    "The Shed": [
        "Wake-up Call, the opening area among moving boxes and jars:",
        "- A lever lowers a staircase; both players `interact` with it together. Nearby are a "
        "bell, a wide metal can you can roll, and jars you can smash; they do nothing for "
        "progress.",
        "- Pulling the lever makes three fuses fly away. Two stay put; one runs off and flees "
        "whenever you get close, leading you through platforming.",
        "- The running fuse gets stuck in a jar: jump onto the jar and `ground_pound` to free it.",
        "- The fuse pushes a piece of wood against a wall to climb higher; follow with wall "
        "jumps (jump at the wall, then jump again while touching it to kick off).",
        "- The fuse rides a saw up onto the toolbox and jumps over a green door. Both players "
        "open the door and hold it down to get through. The game teaches `double_jump` here.",
        "- The fuse runs into a cord that you crouch through: hold B while moving (a `raw` "
        'step with buttons ["B"] and the left stick held). The game then teaches sprinting '
        "(`run` with `sprint: true`).",
        "- The wiki does not describe the chapter beyond this point.",
    ],
}


def walkthrough(chapter: str | None) -> list[str]:
    """Walkthrough lines for `chapter`, or an empty list if there is none."""
    if chapter is None:
        return []
    if chapter not in WALKTHROUGHS:
        raise ValueError(f"no walkthrough for chapter {chapter!r}; known: {sorted(WALKTHROUGHS)}")
    return WALKTHROUGHS[chapter]

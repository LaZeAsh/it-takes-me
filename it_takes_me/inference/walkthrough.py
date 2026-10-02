"""Chapter walkthroughs given to the model, adapted from the It Takes Two wiki.

Source: https://ittakestwo.fandom.com/wiki/The_Shed (the wiki only covers the opening section,
"Wake-up Call", so later sections are left to the on-screen markers).
"""

from __future__ import annotations

WALKTHROUGHS: dict[str, list[str]] = {
    "The Shed": [
        "Wake-up Call (start of the chapter, among moving boxes and jars):",
        "1. Find the lever that lowers the staircase and pull it together with your partner: "
        "both of you `interact` with it. Along the way there is a bell, a wide metal can you "
        "can roll, and jars you can smash with `ground_pound`; these are optional.",
        "2. Pulling the lever makes three fuses fly away. One of them runs off. Chase that "
        "running fuse; it flees when you get close, leading you through platforming.",
        "3. The fuse gets stuck in a jar: jump onto the jar and `ground_pound` to free it.",
        "4. The fuse pushes a piece of wood against a wall to climb higher. Follow it with "
        "wall jumps: jump at the wall, then jump again while touching it to kick off.",
        "5. The fuse rides a saw up onto the toolbox, then jumps over a green door. Open the "
        "door together with your partner, holding it down while both of you get through. "
        "The game teaches `double_jump` here.",
        "6. The fuse waits, then runs into a cord. Crouch through it: hold B while moving "
        '(a `raw` step with buttons ["B"] and the left stick held). Then the game teaches '
        "sprinting (`run` with `sprint: true`).",
        "After this, the wiki has no further steps: follow the on-screen markers.",
    ],
}


def walkthrough(chapter: str | None) -> list[str]:
    """Walkthrough lines for `chapter`, or an empty list if there is none."""
    if chapter is None:
        return []
    if chapter not in WALKTHROUGHS:
        raise ValueError(f"no walkthrough for chapter {chapter!r}; known: {sorted(WALKTHROUGHS)}")
    return WALKTHROUGHS[chapter]

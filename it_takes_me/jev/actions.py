"""Jev's action space: Sol's skills, with each skill field asked as a fixed-choice question.

Jev cannot generate numbers or free-form arguments, so every field of an `act` step becomes a
static option set. `build_step` keeps only the fields that apply to the chosen skill, and
`compile_step_safely` runs it through the shared action runner's checks, falling back to the
skill's default timing when Jev's duration is invalid for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from it_takes_me.game.chunks import AIRTIME_MS, Segment, compile_chunk
from it_takes_me.jev.state import Scene, Thing

SKILLS: dict[str, str] = {
    "run": "Move in `dir` for `duration` at `speed`: walk or run somewhere.",
    "jump": "One jump, travelling in `dir` through the air: small steps and gaps.",
    "double_jump": "Jump, then jump again in the air: higher and farther than one jump.",
    "dash": "Quick burst in `dir`; also works in the air to stretch a jump.",
    "jump_dash": "Jump, then dash in the air: the longest horizontal leap.",
    "ground_pound": "Slam straight down while in the air: breaks jars, presses buttons.",
    "interact": "Press Y, held for `duration`: use, pick up, or place an object. Only works "
    "when a yellow_circle or button_prompt is touching or near the character; otherwise it "
    "does nothing.",
    "grapple": "Press RB: grapple to a rope point in range.",
    "ability": "Hold a trigger (`trigger`) for `duration`: the chapter ability.",
    "look": "Turn the camera toward `look` for `duration` without moving. This changes "
    "which way every later direction points.",
    "locate_partner": "Reveal where the partner is on screen.",
    "skip_cutscene": "Hold B to skip a cutscene.",
}

DIRECTIONS: dict[str, str] = {
    "forward": "Away from the camera.",
    "back": "Toward the camera.",
    "left": "Left of the camera view. Sideways moves make the camera swing and curve the path.",
    "right": "Right of the camera view. Sideways moves make the camera swing and curve the path.",
    "forward-left": "Diagonally away and left.",
    "forward-right": "Diagonally away and right.",
    "back-left": "Diagonally toward the camera and left.",
    "back-right": "Diagonally toward the camera and right.",
    "none": "No movement.",
}

DURATIONS: dict[str, tuple[int, str]] = {
    "ms_150": (150, "a tap: a nudge or a tiny camera adjustment"),
    "ms_300": (300, "one or two steps; a small camera turn"),
    "ms_600": (600, "a few steps; one jump arc"),
    "ms_1000": (1000, "about 1 second: a short run; a quarter camera turn"),
    "ms_1500": (1500, "a medium run across a room section"),
    "ms_2500": (2500, "a long run"),
    "ms_4000": (4000, "a very long run on open ground"),
}

SPEEDS: dict[str, tuple[float, str]] = {
    "walk": (0.4, "slow and careful: ledges, narrow surfaces, near edges"),
    "jog": (0.7, "moderate"),
    "run": (1.0, "full speed: open ground"),
}

LOOKS: dict[str, str] = {
    "left": "Rotate the camera left.",
    "right": "Rotate the camera right.",
    "up": "Tilt the camera up.",
    "down": "Tilt the camera down.",
}

TRIGGERS: dict[str, str] = {"LT": "Left trigger.", "RT": "Right trigger."}

# Skills whose step takes a movement direction.
STEERED = {"run", "jump", "double_jump", "dash", "jump_dash", "ground_pound"}
# Skills whose length is Jev's chosen duration (runs are sized by distance; others use defaults).
_TIMED = {"interact", "ability", "look"}
# Run length from Luna's distance to the goal. Jev's duration answers anchored on its own
# history (300 ms every step), so runs are sized from the scene instead.
RUN_MS_BY_DISTANCE = {"touching": 300, "near": 800, "mid": 1800, "far": 3500}
EXPLORE_RUN_MS = 1500
# Cap when Luna reports a drop nearby.
EDGE_RUN_MAX_MS = 800
# Things the interact button can act on.
_PROMPTS = ("yellow_circle", "button_prompt")
LOOK_MAX_MS = 1500
SKIP_CUTSCENE_MIN_MS = 2000


def available_skills(scene: Scene) -> dict[str, str]:
    """Sol's skills minus the ones that cannot do anything in this scene."""
    if scene.scene == "cutscene":
        return {"skip_cutscene": SKILLS["skip_cutscene"]}
    skills = {skill: text for skill, text in SKILLS.items() if skill != "skip_cutscene"}
    in_reach = any(
        thing.kind in _PROMPTS and thing.distance in ("touching", "near") for thing in scene.things
    )
    if not in_reach:
        del skills["interact"]
    return skills


def run_ms(scene: Scene, goal: Thing | None) -> int:
    """How long a run lasts: far enough to reach the goal, short near drops."""
    ms = RUN_MS_BY_DISTANCE[goal.distance] if goal is not None else EXPLORE_RUN_MS
    if scene.character.edges:
        ms = min(ms, EDGE_RUN_MAX_MS)
    return ms


def action_questions(skills: dict[str, str] = SKILLS) -> dict[str, Any]:
    """The questions for one step. Every answer is one of these fixed options."""
    return {
        "skill": {
            "type": "choice",
            "instructions": "Which skill should the character use next?",
            "criteria": skills,
        },
        "dir": {
            "type": "choice",
            "instructions": "Which camera-relative direction should the character move in? "
            "Only used by run, jump, double_jump, dash, jump_dash, and ground_pound.",
            "criteria": DIRECTIONS,
        },
        "duration": {
            "type": "choice",
            "instructions": "How long should the input last? Used by interact, ability, "
            "and look. Runs are sized from the goal's distance; jumps and dashes use their own "
            "airtime unless this is longer.",
            "criteria": {key: f"{ms} ms: {text}" for key, (ms, text) in DURATIONS.items()},
        },
        "speed": {
            "type": "choice",
            "instructions": "How fast should a run move? Only used by run.",
            "criteria": {key: text for key, (_, text) in SPEEDS.items()},
        },
        "look": {
            "type": "choice",
            "instructions": "Which way should the camera turn? Only used by look.",
            "criteria": LOOKS,
        },
        "trigger": {
            "type": "choice",
            "instructions": "Which trigger does the ability use? Only used by ability.",
            "criteria": TRIGGERS,
        },
    }


@dataclass(frozen=True, slots=True)
class ActionChoice:
    skill: str
    dir: str
    duration: str
    speed: str
    look: str
    trigger: str


def build_step(choice: ActionChoice, *, run_ms: int) -> dict[str, Any]:
    """Turn Jev's answers into one `act` step, keeping only the fields the skill uses."""
    skill = choice.skill
    if skill not in SKILLS:
        raise ValueError(f"unknown skill {skill!r}")
    ms = DURATIONS[choice.duration][0]
    step: dict[str, Any] = {"skill": skill}
    if skill in STEERED:
        step["dir"] = choice.dir
    if skill == "run" and choice.dir == "none":
        # Runs must move (there is no standing still); "none" means no preference.
        step["dir"] = "forward"
    if skill == "run":
        step["speed"] = SPEEDS[choice.speed][0]
        step["ms"] = run_ms
    elif skill == "look":
        step["look"] = choice.look
    elif skill == "ability":
        step["button"] = choice.trigger
    if skill in _TIMED:
        step["ms"] = min(ms, LOOK_MAX_MS) if skill == "look" else ms
    elif skill == "skip_cutscene":
        step["ms"] = max(ms, SKIP_CUTSCENE_MIN_MS)
    elif skill in AIRTIME_MS and ms > AIRTIME_MS[skill]:
        # Longer than the airtime: keep steering after landing.
        step["ms"] = ms
    return step


def compile_step_safely(
    step: dict[str, Any], max_ms: int
) -> tuple[dict[str, Any], list[Segment], str | None]:
    """Compile `step`; if its timing is invalid, retry with the skill's default timing."""
    try:
        return step, compile_chunk([step], max_ms), None
    except ValueError as exc:
        if "ms" not in step:
            raise
        fallback = {k: v for k, v in step.items() if k != "ms"}
        return fallback, compile_chunk([fallback], max_ms), f"used default timing: {exc}"

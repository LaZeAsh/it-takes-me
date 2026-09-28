"""Action chunks: the model plans ~0.5-3 s of play in one tool call, we execute it locally.

A chunk is a list of steps. Each step is either a named *skill* (a macro with the game's timing
baked in, e.g. `double_jump`) or a *raw* pad segment. Steps compile to `Segment`s — a pad state
held for some ms — and `play_chunk` replays them against wall-clock deadlines so timing inside a
chunk never depends on model latency.

Directions are camera-relative words ("forward", "back-left", ...), not stick floats.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from typing import Any

from .io import NEUTRAL, Button, GameIO, PadState

# --- It Takes Two bindings (Xbox defaults, EA accessibility page) -----------------------------
# GROUND_POUND isn't listed there; B is my assumption — verify in-game.

JUMP = Button.A
DASH = Button.X
INTERACT = Button.Y
CANCEL = Button.B
GROUND_POUND = Button.B
SPRINT = Button.LS
GRAPPLE = Button.RB

# --- Skill timings (ms) -----------------------------------------------------------------------

TAP_MS = 100  # how long a button is held for a "press"
JUMP_AIR_MS = 400  # airtime after a jump press before the next step
DOUBLE_JUMP_GAP_MS = 250  # delay between the two jump presses
DASH_AFTER_MS = 250
GROUND_POUND_AFTER_MS = 500
GRAPPLE_AFTER_MS = 600

MAX_CHUNK_MS = 3000

_D = math.sqrt(0.5)
DIRECTIONS: dict[str, tuple[float, float]] = {
    "none": (0.0, 0.0),
    "forward": (0.0, 1.0),
    "back": (0.0, -1.0),
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "forward-left": (-_D, _D),
    "forward-right": (_D, _D),
    "back-left": (-_D, -_D),
    "back-right": (_D, -_D),
}
LOOK_DIRECTIONS: dict[str, tuple[float, float]] = {
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "up": (0.0, 1.0),
    "down": (0.0, -1.0),
}

SKILLS = (
    "run",
    "jump",
    "double_jump",
    "dash",
    "jump_dash",
    "ground_pound",
    "interact",
    "grapple",
    "ability",
    "look",
    "wait",
    "raw",
)


@dataclass(frozen=True, slots=True)
class Segment:
    ms: int
    state: PadState


def _dir(step: dict[str, Any], key: str = "dir") -> tuple[float, float]:
    word = step.get(key, "none")
    if word not in DIRECTIONS:
        raise ValueError(f"unknown direction {word!r}; use one of {sorted(DIRECTIONS)}")
    return DIRECTIONS[word]


def _ms(step: dict[str, Any], default: int) -> int:
    return max(0, int(step.get("ms", default)))


def _vec(value: Any) -> tuple[float, float]:
    if value is None:
        return (0.0, 0.0)
    x, y = value
    return (float(x), float(y))


def compile_step(step: dict[str, Any]) -> list[Segment]:
    skill = step.get("skill")
    left = _dir(step)
    moving = PadState(left=left)

    def press(button: Button, after_ms: int, base: PadState = moving) -> list[Segment]:
        pressed = replace(base, buttons=base.buttons | {button})
        return [Segment(TAP_MS, pressed), Segment(after_ms, base)]

    if skill == "run":
        buttons = frozenset({SPRINT}) if step.get("sprint") else frozenset()
        return [Segment(_ms(step, 500), PadState(left=left, buttons=buttons))]
    if skill == "jump":
        return press(JUMP, JUMP_AIR_MS)
    if skill == "double_jump":
        return press(JUMP, DOUBLE_JUMP_GAP_MS) + press(JUMP, JUMP_AIR_MS)
    if skill == "dash":
        return press(DASH, DASH_AFTER_MS)
    if skill == "jump_dash":
        return press(JUMP, DOUBLE_JUMP_GAP_MS) + press(DASH, DASH_AFTER_MS)
    if skill == "ground_pound":
        return press(GROUND_POUND, GROUND_POUND_AFTER_MS)
    if skill == "interact":
        return [Segment(_ms(step, TAP_MS), PadState(left=left, buttons=frozenset({INTERACT})))]
    if skill == "grapple":
        return press(GRAPPLE, GRAPPLE_AFTER_MS)
    if skill == "ability":
        trigger = Button(step.get("button", "RT"))
        if trigger not in (Button.LT, Button.RT):
            raise ValueError("ability button must be LT or RT")
        return [Segment(_ms(step, 300), PadState(left=left, buttons=frozenset({trigger})))]
    if skill == "look":
        word = step.get("look", "right")
        if word not in LOOK_DIRECTIONS:
            raise ValueError(f"unknown look direction {word!r}; use one of {list(LOOK_DIRECTIONS)}")
        return [Segment(_ms(step, 200), PadState(left=left, right=LOOK_DIRECTIONS[word]))]
    if skill == "wait":
        return [Segment(_ms(step, 500), NEUTRAL)]
    if skill == "raw":
        state = PadState(
            left=_vec(step.get("left")),
            right=_vec(step.get("right")),
            buttons=frozenset(Button(b) for b in step.get("buttons", [])),
        )
        return [Segment(_ms(step, 100), state)]
    raise ValueError(f"unknown skill {skill!r}; use one of {list(SKILLS)}")


def compile_chunk(steps: list[dict[str, Any]], max_ms: int = MAX_CHUNK_MS) -> list[Segment]:
    segments = [seg for step in steps for seg in compile_step(step)]
    total = sum(s.ms for s in segments)
    if total > max_ms:
        raise ValueError(f"chunk is {total} ms; keep it under {max_ms} ms and look again")
    return segments


def play_chunk(io: GameIO, segments: list[Segment], keyframes: int = 0) -> list[tuple[int, bytes]]:
    """Execute segments against wall-clock deadlines; return `keyframes` evenly spaced
    mid-chunk frames as (ms since start, png). The pad is neutral when this returns.

    A capture happens while the current pad state is held, so it only skews timing if it runs
    past the end of the segment it falls in.
    """
    total = sum(s.ms for s in segments)
    shot_times = [total * (i + 1) / (keyframes + 1) for i in range(keyframes)] if total else []
    shots: list[tuple[int, bytes]] = []
    start = time.monotonic()

    def elapsed_ms() -> float:
        return (time.monotonic() - start) * 1000

    deadline = 0.0
    try:
        for seg in segments:
            io.set_pad(seg.state)
            deadline += seg.ms
            while shot_times and shot_times[0] < deadline:
                time.sleep(max(0.0, shot_times.pop(0) - elapsed_ms()) / 1000)
                shots.append((round(elapsed_ms()), io.capture()))
            time.sleep(max(0.0, deadline - elapsed_ms()) / 1000)
    finally:
        io.set_pad(NEUTRAL)
    return shots

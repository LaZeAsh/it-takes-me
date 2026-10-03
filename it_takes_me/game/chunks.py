"""
Action chunks: the model chooses input durations and we execute them locally.

This module compiles steps into timed pad segments; `playback` runs them and
`it_takes_me.sol.pad_thread` queues them back to back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from .io import NEUTRAL, Button, PadState
from .tuning import (
    AUTO_WATCH_RUN_MS,
    DASH,
    DASH_AFTER_MS,
    DOUBLE_JUMP_GAP_MS,
    GRAPPLE,
    GRAPPLE_AFTER_MS,
    GROUND_POUND,
    GROUND_POUND_AFTER_MS,
    INTERACT,
    JUMP,
    JUMP_AIR_MS,
    LOCATE_PARTNER,
    LOCATE_PARTNER_AFTER_MS,
    MAX_CHUNK_MS,
    MAX_REPEAT,
    SKIP_CUTSCENE,
    SKIP_CUTSCENE_MS,
    SPRINT,
    TAP_MS,
)

CONDITIONS = ("stuck", "cut")
HALVES = ("left", "right")

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
    "locate_partner",
    "skip_cutscene",
    "press",
    "raw",
)
BUTTON_SKILLS = frozenset(s for s in SKILLS if s not in ("run", "look", "raw"))
REPEAT = "repeat"  # a block of steps played `times` in a row; expanded before compiling


@dataclass(frozen=True, slots=True)
class Segment:
    ms: int  # hold time; the timeout when `until` is set
    state: PadState
    until: frozenset[str] = frozenset()
    step: int = 0  # index of the step this came from


def _dir(step: dict[str, Any]) -> tuple[float, float]:
    """Left-stick vector from `dir` or an exact `heading`, scaled by `speed`."""
    if "heading" in step:
        if "dir" in step:
            raise ValueError("use either dir or heading, not both")
        heading = step["heading"]
        if isinstance(heading, bool) or not isinstance(heading, int | float):
            raise ValueError("heading must be a number of degrees (0 forward, 90 right)")
        if not -180 <= heading <= 180:
            raise ValueError("heading must be between -180 and 180 degrees")
        rad = math.radians(heading)
        x, y = math.sin(rad), math.cos(rad)
    else:
        word = step.get("dir", "none")
        if word not in DIRECTIONS:
            raise ValueError(f"unknown direction {word!r}; use one of {sorted(DIRECTIONS)}")
        x, y = DIRECTIONS[word]
    speed = _fraction(step, "speed")
    return (x * speed, y * speed)


def _fraction(step: dict[str, Any], key: str) -> float:
    value = step.get(key, 1.0)
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0.1 <= value <= 1:
        raise ValueError(f"{key} must be a number from 0.1 to 1")
    return float(value)


def _ms(step: dict[str, Any], default: int) -> int:
    return _timing(step, "ms", default)


def _timing(step: dict[str, Any], key: str, default: int) -> int:
    value = step.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{key} must be a positive integer number of milliseconds")
    return value


def _vec(value: Any) -> tuple[float, float]:
    if value is None:
        return (0.0, 0.0)
    x, y = value
    return (float(x), float(y))


def _until(step: dict[str, Any], allowed: tuple[str, ...]) -> frozenset[str]:
    until = frozenset(step.get("until", []))
    if bad := until - set(allowed):
        raise ValueError(f"{step.get('skill')} can't use until {sorted(bad)}; allowed: {allowed}")
    return until


def compile_step(step: dict[str, Any]) -> list[Segment]:
    skill = step.get("skill")
    left = _dir(step)
    moving = PadState(left=left)
    if "until" in step and skill != "run":
        raise ValueError("`until` only works on run")
    if "hold_ms" in step and skill not in BUTTON_SKILLS:
        raise ValueError("`hold_ms` only works on skills that press a button")
    if "look_speed" in step and skill != "look":
        raise ValueError("`look_speed` only works on look")
    if "gap_ms" in step and skill not in ("double_jump", "jump_dash"):
        raise ValueError("`gap_ms` only works on double_jump and jump_dash")

    def press(button: Button, default_ms: int, *, sustained: bool = False) -> list[Segment]:
        total = _ms(step, default_ms)
        hold = _timing(step, "hold_ms", total if sustained else TAP_MS)
        if hold > total:
            raise ValueError("hold_ms cannot exceed the step's total ms")
        pressed = replace(moving, buttons=frozenset({button}))
        segments = [Segment(hold, pressed)]
        if total > hold:
            segments.append(Segment(total - hold, moving))
        return segments

    def pair(first: Button, second: Button, default_ms: int) -> list[Segment]:
        total = _ms(step, default_ms)
        hold = _timing(step, "hold_ms", TAP_MS)
        gap = _timing(step, "gap_ms", DOUBLE_JUMP_GAP_MS)
        used = 2 * hold + gap
        if used > total:
            raise ValueError("ms must cover both button holds and gap_ms (2 * hold_ms + gap_ms)")
        segments = [
            Segment(hold, replace(moving, buttons=frozenset({first}))),
            Segment(gap, moving),
            Segment(hold, replace(moving, buttons=frozenset({second}))),
        ]
        if total > used:
            segments.append(Segment(total - used, moving))
        return segments

    if skill == "run":
        if left == (0.0, 0.0):
            raise ValueError(
                "run needs a direction (dir or heading): there is no standing still. Start your "
                "next move straight away instead."
            )
        buttons = frozenset({SPRINT}) if step.get("sprint") else frozenset()
        until = _until(step, CONDITIONS)
        ms = _ms(step, 500)
        if ms > AUTO_WATCH_RUN_MS:
            until |= frozenset(CONDITIONS)
        return [Segment(ms, PadState(left=left, buttons=buttons), until)]
    if skill == "jump":
        return press(JUMP, TAP_MS + JUMP_AIR_MS)
    if skill == "double_jump":
        return pair(JUMP, JUMP, 2 * TAP_MS + DOUBLE_JUMP_GAP_MS + JUMP_AIR_MS)
    if skill == "dash":
        return press(DASH, TAP_MS + DASH_AFTER_MS)
    if skill == "jump_dash":
        return pair(JUMP, DASH, 2 * TAP_MS + DOUBLE_JUMP_GAP_MS + DASH_AFTER_MS)
    if skill == "ground_pound":
        return press(GROUND_POUND, TAP_MS + GROUND_POUND_AFTER_MS)
    if skill == "interact":
        return press(INTERACT, TAP_MS, sustained=True)
    if skill == "grapple":
        return press(GRAPPLE, TAP_MS + GRAPPLE_AFTER_MS)
    if skill == "ability":
        trigger = Button(step.get("button", "RT"))
        if trigger not in (Button.LT, Button.RT):
            raise ValueError("ability button must be LT or RT")
        return press(trigger, 300, sustained=True)
    if skill == "look":
        word = step.get("look", "right")
        if word not in LOOK_DIRECTIONS:
            raise ValueError(f"unknown look direction {word!r}; use one of {list(LOOK_DIRECTIONS)}")
        x, y = LOOK_DIRECTIONS[word]
        turn = _fraction(step, "look_speed")
        return [Segment(_ms(step, 200), PadState(left=left, right=(x * turn, y * turn)))]
    if skill == "locate_partner":
        return press(LOCATE_PARTNER, TAP_MS + LOCATE_PARTNER_AFTER_MS)
    if skill == "skip_cutscene":
        if left != (0.0, 0.0):
            raise ValueError("skip_cutscene must use dir=none (no movement)")
        return press(SKIP_CUTSCENE, SKIP_CUTSCENE_MS, sustained=True)
    if skill == "press":
        if "button" not in step:
            raise ValueError("press requires a button")
        return press(Button(step["button"]), TAP_MS)
    if skill == "raw":
        state = PadState(
            left=_vec(step.get("left")),
            right=_vec(step.get("right")),
            buttons=frozenset(Button(b) for b in step.get("buttons", [])),
        )
        if state == NEUTRAL:
            raise ValueError("raw must press or push something: there is no standing still")
        return [Segment(_ms(step, 100), state)]
    raise ValueError(f"unknown skill {skill!r}; use one of {list(SKILLS)}")


# Shortest time a directional jump must keep steering when nothing airborne follows it: the
# skill defaults, which cover the airtime. Releasing the stick earlier drops the character short.
AIRTIME_MS = {
    "jump": TAP_MS + JUMP_AIR_MS,
    "double_jump": 2 * TAP_MS + DOUBLE_JUMP_GAP_MS + JUMP_AIR_MS,
    "jump_dash": 2 * TAP_MS + DOUBLE_JUMP_GAP_MS + DASH_AFTER_MS,
    "dash": TAP_MS + DASH_AFTER_MS,
}
_KEEPS_STEERING = ("run", "jump", "double_jump", "jump_dash", "dash")
# Airborne follow-ups that are meant to end horizontal movement (slam down, rope pull).
_ENDS_JUMP = ("ground_pound", "grapple")


def _check_air_steering(
    steps: list[dict[str, Any]], segments: list[Segment], labels: list[str]
) -> None:
    """Reject a directional jump that lets go of the stick before it can land."""
    moving = {seg.step for seg in segments if seg.state.left != (0.0, 0.0)}
    for i, step in enumerate(steps):
        skill = step.get("skill")
        if skill not in AIRTIME_MS or i not in moving:
            continue
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if nxt is not None and (
            nxt.get("skill") in _ENDS_JUMP
            or (nxt.get("skill") in _KEEPS_STEERING and i + 1 in moving)
        ):
            continue
        if _ms(step, AIRTIME_MS[skill]) < AIRTIME_MS[skill]:
            what = "the chunk ends"
            if nxt is not None:
                what = f"step {labels[i + 1]} ({nxt.get('skill')}) stops"
            raise ValueError(
                f"step {labels[i]} ({skill}) lets go of the stick after {step['ms']} ms because "
                f"{what} steering, so you drop short mid-air. Give it ms >= "
                f"{AIRTIME_MS[skill]}, or follow it with a run in the same direction until you "
                "land. Nothing was pressed."
            )


def expand_repeats(steps: list[dict[str, Any]]) -> list[tuple[int, str, dict[str, Any]]]:
    """Flatten `repeat` blocks into (top-level index, label, step) in play order."""
    flat: list[tuple[int, str, dict[str, Any]]] = []
    for i, step in enumerate(steps):
        if step.get("skill") != REPEAT:
            if "times" in step or "steps" in step:
                raise ValueError(f"step {i}: `times` and `steps` only work on repeat")
            flat.append((i, str(i), step))
            continue
        if extra := set(step) - {"skill", "times", "steps"}:
            raise ValueError(f"step {i}: repeat only takes times and steps, not {sorted(extra)}")
        times = step.get("times")
        if isinstance(times, bool) or not isinstance(times, int) or not 2 <= times <= MAX_REPEAT:
            raise ValueError(f"step {i}: repeat times must be an integer from 2 to {MAX_REPEAT}")
        inner = step.get("steps")
        if not isinstance(inner, list) or not inner:
            raise ValueError(f"step {i}: repeat needs a non-empty list of steps")
        if any(not isinstance(s, dict) or s.get("skill") == REPEAT for s in inner):
            raise ValueError(f"step {i}: repeat steps must be plain skills (no nested repeat)")
        for n in range(times):
            for j, sub in enumerate(inner):
                flat.append((i, f"{i}.{j} (repeat {n + 1}/{times})", sub))
    return flat


def compile_chunk(steps: list[dict[str, Any]], max_ms: int = MAX_CHUNK_MS) -> list[Segment]:
    """Compile a chunk; each segment's `step` is the top-level step it came from."""
    flat = expand_repeats(steps)
    labels = [label for _, label, _ in flat]
    segments: list[Segment] = []
    for k, (_, label, step) in enumerate(flat):
        try:
            segments += [replace(seg, step=k) for seg in compile_step(step)]
        except ValueError as exc:
            raise ValueError(f"step {label}: {exc}") from exc
    _check_air_steering([step for _, _, step in flat], segments, labels)
    total = sum(s.ms for s in segments)
    if total > max_ms:
        raise ValueError(f"chunk is {total} ms; keep it under {max_ms} ms and look again")
    return [replace(seg, step=flat[seg.step][0]) for seg in segments]

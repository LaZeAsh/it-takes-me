"""Action chunks: the model chooses input durations and we execute them locally.

A chunk is a list of steps. Each step is either a named *skill* (with overridable button and
movement timings, e.g. `double_jump`) or a *raw* pad segment. Steps compile to `Segment`s — a
pad state held for some ms — and `play_chunk` replays them against wall-clock deadlines so
timing inside a chunk never depends on model latency.

Directions are camera-relative words ("forward", "back-left", ...), not stick floats.

`run` and `wait` take `until`: local checks polled during the step (a cheap low-res snapshot,
~10 Hz) that end it early. `ms` becomes the timeout. When a check fires, the rest of the chunk
is skipped and the model is told why. Runs longer than three seconds enable these checks
automatically. They detect a frozen view or abrupt change, not arrival or every obstacle.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from PIL import ImageChops, ImageStat

from .io import NEUTRAL, Button, GameIO, PadState

if TYPE_CHECKING:
    from PIL.Image import Image

# --- It Takes Two bindings (Xbox defaults, EA accessibility page) -----------------------------

JUMP = Button.A
DASH = Button.X
INTERACT = Button.Y
CANCEL = Button.B
GROUND_POUND = Button.B
SPRINT = Button.LS
GRAPPLE = Button.RB
LOCATE_PARTNER = Button.RS
SKIP_CUTSCENE = Button.B

# --- Skill timings (ms) -----------------------------------------------------------------------

TAP_MS = 100  # how long a button is held for a "press"
JUMP_AIR_MS = 400  # airtime after a jump press before the next step
DOUBLE_JUMP_GAP_MS = 250  # delay between the two jump presses
DASH_AFTER_MS = 250
GROUND_POUND_AFTER_MS = 500
GRAPPLE_AFTER_MS = 600
LOCATE_PARTNER_AFTER_MS = 200  # allow the partner indicator to appear before observing
SKIP_CUTSCENE_MS = 2000  # default hold; the model can choose a longer duration

MAX_CHUNK_MS = 10_000
AUTO_WATCH_RUN_MS = 3000

# --- `until` checks ---------------------------------------------------------------------------
# Diffs are mean absolute grayscale difference (0-255) between consecutive snapshots of our half
# of the screen. Thresholds are first guesses — tune them from recorded runs.

SNAPSHOT_SIZE = (160, 90)  # full frame; our half is cropped from it
CHECK_EVERY_MS = 100
STUCK_DIFF = 2.0  # below this our view is ~frozen
STUCK_MS = 400  # ...for this long while the stick is pushed = stuck
STUCK_GRACE_MS = 300  # ignore the start of a step (accelerating from standstill)
CUT_DIFF = 40.0  # a jump this big in one tick = cutscene cut, fade, respawn, menu
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
    "wait",
    "raw",
)


@dataclass(frozen=True, slots=True)
class Segment:
    ms: int  # hold time; the timeout when `until` is set
    state: PadState
    until: frozenset[str] = frozenset()
    step: int = 0  # index of the step this came from


@dataclass(slots=True)
class ChunkResult:
    elapsed_ms: int
    shots: list[tuple[int, bytes]] = field(default_factory=list)
    # (step index, condition) when an `until` check ended the chunk early.
    stopped: tuple[int, str] | None = None


def _dir(step: dict[str, Any], key: str = "dir") -> tuple[float, float]:
    word = step.get(key, "none")
    if word not in DIRECTIONS:
        raise ValueError(f"unknown direction {word!r}; use one of {sorted(DIRECTIONS)}")
    return DIRECTIONS[word]


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
    if "until" in step and skill not in ("run", "wait"):
        raise ValueError("`until` only works on run and wait")
    button_skills = {
        "jump",
        "double_jump",
        "dash",
        "jump_dash",
        "ground_pound",
        "interact",
        "grapple",
        "ability",
        "locate_partner",
        "skip_cutscene",
        "press",
    }
    if "hold_ms" in step and skill not in button_skills:
        raise ValueError("`hold_ms` only works on skills that press a button")
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
        buttons = frozenset({SPRINT}) if step.get("sprint") else frozenset()
        until = _until(step, CONDITIONS if left != (0.0, 0.0) else ("cut",))
        ms = _ms(step, 500)
        if ms > AUTO_WATCH_RUN_MS:
            until |= frozenset(CONDITIONS if left != (0.0, 0.0) else ("cut",))
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
        return [Segment(_ms(step, 200), PadState(left=left, right=LOOK_DIRECTIONS[word]))]
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
    if skill == "wait":
        return [Segment(_ms(step, 500), NEUTRAL, _until(step, ("cut",)))]
    if skill == "raw":
        state = PadState(
            left=_vec(step.get("left")),
            right=_vec(step.get("right")),
            buttons=frozenset(Button(b) for b in step.get("buttons", [])),
        )
        return [Segment(_ms(step, 100), state)]
    raise ValueError(f"unknown skill {skill!r}; use one of {list(SKILLS)}")


def compile_chunk(steps: list[dict[str, Any]], max_ms: int = MAX_CHUNK_MS) -> list[Segment]:
    segments = [replace(seg, step=i) for i, step in enumerate(steps) for seg in compile_step(step)]
    total = sum(s.ms for s in segments)
    if total > max_ms:
        raise ValueError(f"chunk is {total} ms; keep it under {max_ms} ms and look again")
    return segments


class _Watcher:
    """Polls snapshots of our half during one `until` segment and says which check fired."""

    def __init__(self, io: GameIO, until: frozenset[str], half: str, started_ms: float) -> None:
        self.io, self.until, self.half, self.started_ms = io, until, half, started_ms
        self.prev = self._look()
        self.still_since: float | None = None
        self.next_ms = started_ms + CHECK_EVERY_MS

    def _look(self) -> Image:
        im = self.io.snapshot(SNAPSHOT_SIZE)
        w, h = im.size
        return im.crop((0, 0, w // 2, h) if self.half == "left" else (w // 2, 0, w, h))

    def check(self, now_ms: float) -> str | None:
        self.next_ms = now_ms + CHECK_EVERY_MS
        cur = self._look()
        diff = ImageStat.Stat(ImageChops.difference(self.prev, cur)).mean[0]
        self.prev = cur
        if "cut" in self.until and diff > CUT_DIFF:
            return "cut"
        if "stuck" in self.until and now_ms - self.started_ms >= STUCK_GRACE_MS:
            if diff >= STUCK_DIFF:
                self.still_since = None
            elif self.still_since is None:
                self.still_since = now_ms
            elif now_ms - self.still_since >= STUCK_MS:
                return "stuck"
        return None


def play_chunk(
    io: GameIO,
    segments: list[Segment],
    keyframes: int = 0,
    half: str = "left",
    *,
    instant: bool = False,
) -> ChunkResult:
    """Execute segments against wall-clock deadlines. Returns `keyframes` evenly spaced
    mid-chunk frames as (ms since start, png), and which `until` check stopped the chunk, if
    any. The pad is neutral when this returns.

    Captures happen while the current pad state is held, so they only skew timing if they run
    past the end of the segment they fall in. Keyframe times are planned from the full-length
    chunk, so a chunk stopped early returns fewer of them.
    """
    if instant:
        try:
            for segment in segments:
                io.set_pad(segment.state)
        finally:
            io.set_pad(NEUTRAL)
        return ChunkResult(elapsed_ms=0)

    total = sum(s.ms for s in segments)
    shot_times = [total * (i + 1) / (keyframes + 1) for i in range(keyframes)] if total else []
    result = ChunkResult(elapsed_ms=0)
    start = time.monotonic()

    def elapsed_ms() -> float:
        return (time.monotonic() - start) * 1000

    deadline = 0.0
    try:
        for seg in segments:
            io.set_pad(seg.state)
            deadline += seg.ms
            watcher = _Watcher(io, seg.until, half, elapsed_ms()) if seg.until else None
            while (now := elapsed_ms()) < deadline:
                wake = min(
                    deadline,
                    shot_times[0] if shot_times else deadline,
                    watcher.next_ms if watcher else deadline,
                )
                time.sleep(max(0.0, wake - now) / 1000)
                if shot_times and shot_times[0] <= elapsed_ms():
                    shot_times.pop(0)
                    result.shots.append((round(elapsed_ms()), io.capture()))
                if watcher and watcher.next_ms <= elapsed_ms():
                    fired = watcher.check(elapsed_ms())
                    if fired:
                        result.stopped = (seg.step, fired)
                        return result
    finally:
        io.set_pad(NEUTRAL)
        result.elapsed_ms = round(elapsed_ms())
    return result

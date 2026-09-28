"""Action chunks: the model plans ~0.5-3 s of play in one tool call, we execute it locally.

A chunk is a list of steps. Each step is either a named *skill* (a macro with the game's timing
baked in, e.g. `double_jump`) or a *raw* pad segment. Steps compile to `Segment`s — a pad state
held for some ms — and `play_chunk` replays them against wall-clock deadlines so timing inside a
chunk never depends on model latency.

Directions are camera-relative words ("forward", "back-left", ...), not stick floats.

`run` and `wait` take `until`: local checks polled during the step (a cheap low-res snapshot,
~10 Hz) that end it early. `ms` becomes the timeout. When a check fires, the rest of the chunk
is skipped and the model is told why, so a long `run ... until stuck` is safe where a long blind
`run` is not.
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

# --- Skill timings (ms) -----------------------------------------------------------------------

TAP_MS = 100  # how long a button is held for a "press"
JUMP_AIR_MS = 400  # airtime after a jump press before the next step
DOUBLE_JUMP_GAP_MS = 250  # delay between the two jump presses
DASH_AFTER_MS = 250
GROUND_POUND_AFTER_MS = 500
GRAPPLE_AFTER_MS = 600

MAX_CHUNK_MS = 3000

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
    return max(0, int(step.get("ms", default)))


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

    def press(button: Button, after_ms: int, base: PadState = moving) -> list[Segment]:
        pressed = replace(base, buttons=base.buttons | {button})
        return [Segment(TAP_MS, pressed), Segment(after_ms, base)]

    if skill == "run":
        buttons = frozenset({SPRINT}) if step.get("sprint") else frozenset()
        until = _until(step, CONDITIONS if left != (0.0, 0.0) else ("cut",))
        return [Segment(_ms(step, 500), PadState(left=left, buttons=buttons), until)]
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
    io: GameIO, segments: list[Segment], keyframes: int = 0, half: str = "left"
) -> ChunkResult:
    """Execute segments against wall-clock deadlines. Returns `keyframes` evenly spaced
    mid-chunk frames as (ms since start, png), and which `until` check stopped the chunk, if
    any. The pad is neutral when this returns.

    Captures happen while the current pad state is held, so they only skew timing if they run
    past the end of the segment they fall in. Keyframe times are planned from the full-length
    chunk, so a chunk stopped early returns fewer of them.
    """
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

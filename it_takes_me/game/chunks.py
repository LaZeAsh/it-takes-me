"""
Action chunks: the model chooses input durations and we execute them locally.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from PIL import ImageChops, ImageStat

from .io import NEUTRAL, Button, GameIO, PadState, grab

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
CARRY_MAX_MS = 6000  # longest a final run keeps going while the model plans the next chunk

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

# There is no `wait`: steps already run back to back, and the model's planning time between
# calls stands the character still anyway. In recorded runs, waits were either a whole chunk
# spent just looking (~3.5 s of planning for ~0.3 s of play) or a pause to land between jumps,
# which giving the jump enough `ms` already covers. `run` must move and `raw` must press
# something for the same reason.
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
REPEAT = "repeat"  # a block of steps played `times` in a row; expanded before compiling
MAX_REPEAT = 20
# A chunk is only handed back early if that point is at least this far through it. Earlier, the
# frame barely differs from the one the model planned from: in a live run, 300 ms frames from
# 1-2 s chunks left Sol blind, and it spent every other call standing still just to look.
MIN_RELEASE_FRACTION = 0.5


@dataclass(frozen=True, slots=True)
class Segment:
    ms: int  # hold time; the timeout when `until` is set
    state: PadState
    until: frozenset[str] = frozenset()
    step: int = 0  # index of the step this came from


@dataclass(slots=True)
class ChunkResult:
    elapsed_ms: int
    shots: list[tuple[int, Image]] = field(default_factory=list)
    # (step index, condition) when an `until` check ended the chunk early.
    stopped: tuple[int, str] | None = None


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


def release_point(total_ms: int, lead_ms: float) -> int | None:
    """When to hand a chunk back early so the next one is planned before this one ends.

    None means at the end: the chunk is shorter than about twice the planning time, so an early
    frame would show too little of it.
    """
    release = round(total_ms - lead_ms)
    return release if release >= total_ms * MIN_RELEASE_FRACTION else None


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
    hold_last: bool = False,
) -> ChunkResult:
    """Execute segments against wall-clock deadlines. Returns `keyframes` evenly spaced
    mid-chunk frames as (ms since start, image), and which `until` check stopped the chunk, if
    any. The pad is neutral when this returns, except with `hold_last` on a chunk that ran to
    completion: then the last segment's state stays held for the caller to take over.

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
    return _play(io, segments, keyframes, half, hold_last=hold_last)


def _play(
    io: GameIO,
    segments: list[Segment],
    keyframes: int,
    half: str,
    *,
    hold_last: bool,
    halt: threading.Event | None = None,
    release_ms: int | None = None,
    on_release: Callable[[int, Image], None] | None = None,
) -> ChunkResult:
    """The deadline loop behind `play_chunk` and `PadThread`.

    At `release_ms` into the chunk it captures a frame and hands it to `on_release` while play
    continues. A set `halt` ends the chunk at the next wake-up, reported as condition "halt".
    """
    total = sum(s.ms for s in segments)
    shot_times = [total * (i + 1) / (keyframes + 1) for i in range(keyframes)] if total else []
    release = release_ms if on_release is not None else None
    result = ChunkResult(elapsed_ms=0)
    start = time.monotonic()

    def elapsed_ms() -> float:
        return (time.monotonic() - start) * 1000

    deadline = 0.0
    completed = False
    try:
        for seg in segments:
            io.set_pad(seg.state)
            deadline += seg.ms
            watcher = _Watcher(io, seg.until, half, elapsed_ms()) if seg.until else None
            while (now := elapsed_ms()) < deadline:
                if release is not None and release <= now:
                    release = None
                    assert on_release is not None
                    on_release(round(now), grab(io))
                    continue
                wake = min(
                    deadline,
                    shot_times[0] if shot_times else deadline,
                    watcher.next_ms if watcher else deadline,
                    release if release is not None else deadline,
                    now + CHECK_EVERY_MS if halt is not None else deadline,
                )
                time.sleep(max(0.0, wake - now) / 1000)
                if halt is not None and halt.is_set():
                    result.stopped = (seg.step, "halt")
                    return result
                if shot_times and shot_times[0] <= elapsed_ms():
                    shot_times.pop(0)
                    result.shots.append((round(elapsed_ms()), grab(io)))
                if watcher and watcher.next_ms <= elapsed_ms():
                    fired = watcher.check(elapsed_ms())
                    if fired:
                        result.stopped = (seg.step, fired)
                        return result
        completed = True
    finally:
        if not (hold_last and completed):
            io.set_pad(NEUTRAL)
        result.elapsed_ms = round(elapsed_ms())
    return result


@dataclass(eq=False)
class Job:
    """One chunk queued on a `PadThread`. Fields after `epoch` are written by the pad thread."""

    segments: list[Segment]
    keyframes: int = 0
    # Chunk time at which to hand back a frame while play continues; None = at the end.
    release_ms: int | None = None
    capture: bool = True
    # After a completed chunk, keep holding its last state until the next job or a check.
    keep_moving: bool = False
    # `PadThread.epoch` the model had seen when it planned this job; a mismatch cancels it.
    epoch: int = 0
    status: str = "queued"  # queued, playing, done, stopped, cancelled, error
    result: ChunkResult | None = None
    frame: Image | None = None
    frame_ms: int | None = None  # chunk time of `frame`; None = taken after the chunk
    frame_epoch: int = 0
    idle_ms: int = 0  # neutral pad time between the previous job and this one
    carrying: bool = False
    carry_ms: int = 0
    carry_reason: str | None = None
    error: BaseException | None = None
    started: float | None = None  # time.monotonic() when play began
    # Set once `frame` (or the final outcome) is available.
    ready: threading.Event = field(default_factory=threading.Event)
    # Set once the job and any carry after it are over.
    over: threading.Event = field(default_factory=threading.Event)

    @property
    def total_ms(self) -> int:
        return sum(s.ms for s in self.segments)

    def remaining_from(self, t_ms: int) -> tuple[int, int]:
        """(top-level step playing at `t_ms`, ms of the chunk left after it)."""
        end = 0
        for seg in self.segments:
            end += seg.ms
            if end > t_ms:
                return seg.step, self.total_ms - t_ms
        return self.segments[-1].step, 0


class PadThread:
    """Owns the pad on a background thread and plays queued jobs back to back.

    A job queued while another plays starts the moment that one ends, with no neutral gap.
    When a job is stopped early by a check (or errors), `epoch` goes up, and every job planned
    before that (a lower `Job.epoch`) is cancelled without pressing anything: it assumed the
    stopped chunk would finish. A completed `keep_moving` job keeps its last state held, with
    the stuck/cut checks of long runs, until the next job, a check, or `carry_max_ms`.
    """

    def __init__(
        self,
        io: GameIO,
        half: str = "left",
        *,
        instant: bool = False,
        carry_max_ms: int = CARRY_MAX_MS,
        autostart: bool = True,
    ) -> None:
        self.io, self.half, self.instant, self.carry_max_ms = io, half, instant, carry_max_ms
        self.epoch = 0
        self._carry_watcher: _Watcher | None = None
        self._queue: queue.Queue[Job | None] = queue.Queue()
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="pad")
        if autostart:
            self.start()

    def start(self) -> None:
        self._thread.start()

    def submit(self, job: Job) -> Job:
        if self._halt.is_set():
            self._cancel(job)
        else:
            self._queue.put(job)
        return job

    def stop(self) -> None:
        """Halt whatever is playing, cancel the queue, and leave the pad neutral."""
        self._halt.set()
        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join()
        elif not self._thread.ident:
            self._drain()

    # -- pad thread ----------------------------------------------------------------------------

    def _run(self) -> None:
        carrying: Job | None = None
        neutral_since = time.monotonic()
        try:
            while True:
                job: Job | None = None
                if carrying is not None:
                    job = self._carry(carrying)
                    if job is None:
                        self.io.set_pad(NEUTRAL)
                        neutral_since = time.monotonic()
                    self._end_carry(carrying)
                    carrying = None
                if job is None:
                    job = self._queue.get()
                    if job is None or self._halt.is_set():
                        self._cancel(job)
                        return
                    job.idle_ms = round((time.monotonic() - neutral_since) * 1000)
                if job.epoch != self.epoch:
                    self._cancel(job)
                    continue
                self._play_job(job)
                neutral_since = time.monotonic()
                if job.carrying:
                    carrying = job
                else:
                    job.over.set()
        finally:
            self.io.set_pad(NEUTRAL)
            if carrying is not None:
                self._end_carry(carrying)
            self._drain()

    def _play_job(self, job: Job) -> None:
        job.status, job.started = "playing", time.monotonic()

        def release(t: int, png: Image) -> None:
            job.frame, job.frame_ms, job.frame_epoch = png, t, self.epoch
            job.ready.set()

        try:
            if self.instant:
                job.result = play_chunk(self.io, job.segments, instant=True)
            else:
                job.result = _play(
                    self.io,
                    job.segments,
                    job.keyframes,
                    self.half,
                    hold_last=True,
                    halt=self._halt,
                    release_ms=job.release_ms if job.capture else None,
                    on_release=release,
                )
            if job.result.stopped is not None:
                self.epoch += 1
                job.status = "stopped"
            else:
                job.status = "done"
                job.carrying = job.keep_moving and not self.instant
                if job.carrying:
                    # First snapshot before the caller hears back, so the carry compares
                    # against the end of the chunk.
                    self._carry_watcher = _Watcher(self.io, frozenset(CONDITIONS), self.half, 0.0)
                elif self._queue.empty():
                    self.io.set_pad(NEUTRAL)
            if not job.ready.is_set():
                if job.capture:
                    job.frame = grab(self.io)
                job.frame_epoch = self.epoch
        except Exception as exc:  # noqa: BLE001 - handed to the caller waiting on the job
            self.io.set_pad(NEUTRAL)
            self.epoch += 1
            job.status, job.error, job.carrying = "error", exc, False
            job.frame_epoch = self.epoch
        finally:
            job.ready.set()

    def _carry(self, job: Job) -> Job | None:
        """Hold `job`'s last state until the next job arrives (returned) or a check ends it."""
        start = time.monotonic()
        watcher, self._carry_watcher = self._carry_watcher, None
        try:
            if watcher is None:
                watcher = _Watcher(self.io, frozenset(CONDITIONS), self.half, 0.0)
            while True:
                try:
                    nxt = self._queue.get(timeout=CHECK_EVERY_MS / 1000)
                except queue.Empty:
                    nxt = False
                now = (time.monotonic() - start) * 1000
                job.carry_ms = round(now)
                if nxt is None or self._halt.is_set():
                    self._queue.put(nxt)  # let `_run` see the shutdown or cancel the job
                    job.carry_reason = "stopped"
                    return None
                if nxt is not False:
                    job.carry_reason = "next chunk"
                    return nxt
                if now >= self.carry_max_ms:
                    job.carry_reason = "limit"
                    return None
                if fired := watcher.check(now):
                    if fired == "cut":
                        self.epoch += 1
                    job.carry_reason = fired
                    return None
        except Exception:  # noqa: BLE001 - a failed snapshot just ends the carry
            job.carry_reason = "error"
            return None

    def _end_carry(self, job: Job) -> None:
        job.carrying = False
        job.carry_reason = job.carry_reason or "stopped"
        job.over.set()

    def _drain(self) -> None:
        while True:
            try:
                self._cancel(self._queue.get_nowait())
            except queue.Empty:
                return

    def _cancel(self, job: Job | None) -> None:
        if job is not None:
            job.status = "cancelled"
            job.frame_epoch = self.epoch
            job.ready.set()
            job.over.set()

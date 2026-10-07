"""
Plays compiled segments on the pad against wall-clock deadlines, watching for `until` checks.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PIL import ImageChops, ImageStat

from .chunks import Segment
from .io import NEUTRAL, GameIO, grab
from .tuning import (
    CHECK_EVERY_MS,
    CUT_DIFF,
    JUMP_BLOCKED_DIFF,
    JUMP_MATCH,
    JUMP_RISE,
    JUMP_SHIFT_MAX,
    MIN_RELEASE_FRACTION,
    SNAPSHOT_SIZE,
    STUCK_DIFF,
    STUCK_GRACE_MS,
    STUCK_MS,
)

if TYPE_CHECKING:
    from PIL.Image import Image


@dataclass(frozen=True, slots=True)
class JumpCheck:
    """How the view changed over one jump step: a guess at where the jump left the character."""

    label: str  # e.g. "2 (jump)"
    change: float  # mean grayscale difference between the views before and after
    # Vertical view shift, fraction of the height; > 0: the view rose (higher). None: no
    # shift lines the two views up, so the height change cannot be told.
    shift: float | None
    directional: bool  # the stick was pushed during the jump

    @property
    def verdict(self) -> str:
        if self.change < JUMP_BLOCKED_DIFF:
            return "blocked"
        if self.shift is None:
            return "moved"
        if self.shift >= JUMP_RISE:
            return "higher"
        if self.shift <= -JUMP_RISE:
            return "lower"
        return "level"

    def describe(self) -> str:
        pct = round(abs(self.shift or 0) * 100)
        return {
            "blocked": f"step {self.label}: the view barely changed, so the jump got you "
            "nowhere (blocked by a wall or ledge, or fell back where it started)",
            "higher": f"step {self.label}: the view rose about {pct}% of its height, so you "
            "probably landed higher",
            "lower": f"step {self.label}: the view dropped about {pct}% of its height, so you "
            "probably landed lower (fell short or dropped down)",
            "level": f"step {self.label}: you landed at about the same height",
            "moved": f"step {self.label}: the view changed too much to tell whether you "
            "landed higher; check the frame",
        }[self.verdict]


@dataclass(slots=True)
class ChunkResult:
    elapsed_ms: int
    shots: list[tuple[int, Image]] = field(default_factory=list)
    # (step index, condition) when an `until` check ended the chunk early.
    stopped: tuple[int, str] | None = None
    jumps: list[JumpCheck] = field(default_factory=list)


def half_snapshot(io: GameIO, half: str) -> Image:
    """A small grayscale snapshot of our half of the split screen."""
    im = io.snapshot(SNAPSHOT_SIZE)
    w, h = im.size
    return im.crop((0, 0, w // 2, h) if half == "left" else (w // 2, 0, w, h))


def _diff(a: Image, b: Image) -> float:
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


def jump_outcome(label: str, before: Image, after: Image, directional: bool) -> JumpCheck:
    """Compare views from before and after a jump. The camera follows the character's height,
    so landing higher moves the scene down in the view: find the vertical shift that best lines
    the two up."""
    w, h = before.size
    unshifted = _diff(before, after)
    best_dy, best = 0, unshifted
    for dy in range(-round(h * JUMP_SHIFT_MAX), round(h * JUMP_SHIFT_MAX) + 1):
        if dy == 0:
            continue
        # after[y] ~ before[y - dy]: with dy > 0 the scene moved down, so the view rose.
        top = before.crop((0, max(0, -dy), w, h - max(0, dy)))
        bottom = after.crop((0, max(0, dy), w, h - max(0, -dy)))
        if (d := _diff(top, bottom)) < best:
            best_dy, best = dy, d
    shift = round(best_dy / h, 3) if best_dy == 0 or best <= JUMP_MATCH * unshifted else None
    return JumpCheck(label, round(unshifted, 2), shift, directional)


def release_point(total_ms: int, lead_ms: float) -> int | None:
    """When to hand a chunk back early so the next one is planned before this one ends.

    None means at the end: the chunk is shorter than about twice the planning time, so an early
    frame would show too little of it.
    """
    release = round(total_ms - lead_ms)
    return release if release >= total_ms * MIN_RELEASE_FRACTION else None


class Watcher:
    """Polls snapshots of our half during one `until` segment and says which check fired."""

    def __init__(self, io: GameIO, until: frozenset[str], half: str, started_ms: float) -> None:
        self.io, self.until, self.half, self.started_ms = io, until, half, started_ms
        self.prev = self._look()
        self.still_since: float | None = None
        self.next_ms = started_ms + CHECK_EVERY_MS

    def _look(self) -> Image:
        return half_snapshot(self.io, self.half)

    def check(self, now_ms: float) -> str | None:
        self.next_ms = now_ms + CHECK_EVERY_MS
        cur = self._look()
        diff = _diff(self.prev, cur)
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
    return play_segments(io, segments, keyframes, half, hold_last=hold_last)


def play_segments(
    io: GameIO,
    segments: list[Segment],
    keyframes: int,
    half: str,
    *,
    hold_last: bool,
    halt: threading.Event | None = None,
    release_ms: int | None = None,
    on_release: Callable[[int, Image], None] | None = None,
    check_jumps: bool = False,
) -> ChunkResult:
    """The deadline loop behind `play_chunk` and `PadThread`.

    At `release_ms` into the chunk it captures a frame and hands it to `on_release` while play
    continues. A set `halt` ends the chunk at the next wake-up, reported as condition "halt".
    With `check_jumps`, each jump step's landing is judged from snapshots taken just before and
    after it (`result.jumps`), and a directional jump that got nowhere ends the chunk as
    "blocked": the steps after it assumed it landed.
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
    before: Image | None = None
    moved = False
    try:
        for k, seg in enumerate(segments):
            starts_jump = check_jumps and seg.jump and (k == 0 or segments[k - 1].jump != seg.jump)
            if starts_jump:
                before, moved = half_snapshot(io, half), False
            moved = moved or seg.state.left != (0.0, 0.0)
            io.set_pad(seg.state)
            deadline += seg.ms
            watcher = Watcher(io, seg.until, half, elapsed_ms()) if seg.until else None
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
            ends_jump = k + 1 == len(segments) or segments[k + 1].jump != seg.jump
            if before is not None and seg.jump and ends_jump:
                check = jump_outcome(seg.jump, before, half_snapshot(io, half), moved)
                before = None
                result.jumps.append(check)
                if check.directional and check.verdict == "blocked" and k + 1 < len(segments):
                    result.stopped = (seg.step, "blocked")
                    return result
        completed = True
    finally:
        if not (hold_last and completed):
            io.set_pad(NEUTRAL)
        result.elapsed_ms = round(elapsed_ms())
    return result

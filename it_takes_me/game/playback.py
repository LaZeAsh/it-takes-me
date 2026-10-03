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
    MIN_RELEASE_FRACTION,
    SNAPSHOT_SIZE,
    STUCK_DIFF,
    STUCK_GRACE_MS,
    STUCK_MS,
)

if TYPE_CHECKING:
    from PIL.Image import Image


@dataclass(slots=True)
class ChunkResult:
    elapsed_ms: int
    shots: list[tuple[int, Image]] = field(default_factory=list)
    # (step index, condition) when an `until` check ended the chunk early.
    stopped: tuple[int, str] | None = None


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
        completed = True
    finally:
        if not (hold_last and completed):
            io.set_pad(NEUTRAL)
        result.elapsed_ms = round(elapsed_ms())
    return result

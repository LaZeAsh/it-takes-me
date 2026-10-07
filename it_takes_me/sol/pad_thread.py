"""
A background thread that owns the pad and plays queued chunks back to back.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from it_takes_me.game.chunks import CONDITIONS, Segment
from it_takes_me.game.io import NEUTRAL, GameIO, grab
from it_takes_me.game.playback import ChunkResult, Watcher, play_chunk, play_segments
from it_takes_me.game.tuning import CARRY_MAX_MS, CHECK_EVERY_MS

if TYPE_CHECKING:
    from PIL.Image import Image


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
        self._carry_watcher: Watcher | None = None
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
                job.result = play_segments(
                    self.io,
                    job.segments,
                    job.keyframes,
                    self.half,
                    hold_last=True,
                    halt=self._halt,
                    release_ms=job.release_ms if job.capture else None,
                    on_release=release,
                    check_jumps=True,
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
                    self._carry_watcher = Watcher(self.io, frozenset(CONDITIONS), self.half, 0.0)
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
                watcher = Watcher(self.io, frozenset(CONDITIONS), self.half, 0.0)
            while True:
                try:
                    nxt = self._queue.get(timeout=CHECK_EVERY_MS / 1000)
                except queue.Empty:
                    nxt = False
                now = (time.monotonic() - start) * 1000
                job.carry_ms = round(now)
                if nxt is None or self._halt.is_set():
                    if nxt is not False:
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

"""Per-session recording: frames as PNG files plus a JSONL event log for replay/debugging."""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image


class RunRecorder:
    def __init__(self, runs_dir: Path) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = Path(runs_dir) / stamp
        self.frames_dir = self.dir / "frames"
        self.observations_dir = self.dir / "observations"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.observations_dir.mkdir(parents=True, exist_ok=True)
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self._frame_count = 0
        self._observation_count = 0
        self._t0 = time.monotonic()
        # Full-resolution PNGs are encoded and written off the critical path: encoding a 4K
        # frame takes ~0.4 s, which the model would otherwise wait through on every call.
        self._writes: queue.Queue[tuple[Path, bytes | Image.Image] | None] = queue.Queue()
        self._writer = threading.Thread(target=self._write_frames, daemon=True, name="frames")
        self._writer.start()

    def event(self, kind: str, **data: Any) -> None:
        record = {"t": round(time.monotonic() - self._t0, 3), "kind": kind, **data}
        self._events.write(json.dumps(record, default=str) + "\n")
        self._events.flush()

    def frame(self, frame: bytes | Image.Image, *, reason: str) -> Path:
        """Record a full-resolution capture (PNG bytes or an image); written in the background."""
        self._frame_count += 1
        path = self.frames_dir / f"{self._frame_count:05d}.png"
        self._writes.put((path, frame))
        self.event("frame", path=str(path), reason=reason)
        return path

    def _write_frames(self) -> None:
        while (item := self._writes.get()) is not None:
            path, frame = item
            if isinstance(frame, Image.Image):
                frame.save(path, format="PNG")
            else:
                path.write_bytes(frame)

    def observation(
        self,
        data: bytes,
        *,
        reason: str,
        media_type: str,
        width: int,
        height: int,
        detail: str,
    ) -> Path:
        """Save the exact cropped/resized image sent to the model."""
        self._observation_count += 1
        suffix = ".jpg" if media_type == "image/jpeg" else ".png"
        path = self.observations_dir / f"{self._observation_count:05d}{suffix}"
        path.write_bytes(data)
        self.event(
            "observation",
            path=str(path),
            reason=reason,
            bytes=len(data),
            media_type=media_type,
            width=width,
            height=height,
            detail=detail,
        )
        return path

    def close(self) -> None:
        self._writes.put(None)
        self._writer.join()
        self._events.close()

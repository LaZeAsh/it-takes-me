"""Per-session recording: frames as PNG files plus a JSONL event log for replay/debugging."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class RunRecorder:
    def __init__(self, runs_dir: Path) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = Path(runs_dir) / stamp
        self.frames_dir = self.dir / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self._frame_count = 0
        self._t0 = time.monotonic()

    def event(self, kind: str, **data: Any) -> None:
        record = {"t": round(time.monotonic() - self._t0, 3), "kind": kind, **data}
        self._events.write(json.dumps(record, default=str) + "\n")
        self._events.flush()

    def frame(self, png: bytes, *, reason: str) -> Path:
        self._frame_count += 1
        path = self.frames_dir / f"{self._frame_count:05d}.png"
        path.write_bytes(png)
        self.event("frame", path=str(path), reason=reason, bytes=len(png))
        return path

    def close(self) -> None:
        self._events.close()

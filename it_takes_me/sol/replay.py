"""Deterministic GameIO backed by frames from a recorded run."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from it_takes_me.game.io import NEUTRAL, PadState


class ReplayGameIO:
    def __init__(self, run_dir: Path) -> None:
        frames_dir = Path(run_dir) / "frames"
        self.paths = sorted(frames_dir.glob("*.png"))
        if not self.paths:
            raise ValueError(f"no replay frames found in {frames_dir}")
        self._next = 0
        self._current = self.paths[0].read_bytes()
        self.pad_states: list[PadState] = []

    def capture(self) -> bytes:
        index = min(self._next, len(self.paths) - 1)
        self._current = self.paths[index].read_bytes()
        self._next += 1
        return self._current

    def snapshot(self, size: tuple[int, int]) -> Image.Image:
        with Image.open(io.BytesIO(self._current)) as image:
            return image.convert("L").resize(size, Image.Resampling.BILINEAR, reducing_gap=2.0)

    def set_pad(self, state: PadState) -> None:
        if state != NEUTRAL:
            self.pad_states.append(state)

    def close(self) -> None:
        return

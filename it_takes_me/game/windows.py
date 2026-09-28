"""Windows backend: screen capture via mss, input via a virtual Xbox 360 pad (vgamepad).

This is the backend that runs on the gaming PC. Capture works anywhere mss does; input is a
stub until the controller layer is built out.
"""

from __future__ import annotations

import io
import time

from .io import Button


class WindowsGameIO:
    def __init__(self, monitor: int = 1) -> None:
        import mss

        self._sct = mss.mss()
        self._monitor = self._sct.monitors[monitor]
        self._pad = None  # vgamepad.VX360Gamepad once implemented

    def capture(self) -> bytes:
        from PIL import Image

        shot = self._sct.grab(self._monitor)
        im = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def _not_implemented(self, what: str) -> None:
        raise NotImplementedError(
            f"WindowsGameIO.{what}: controller input is not wired up yet (next phase: vgamepad)."
        )

    def press(self, button: Button, hold_ms: int = 80) -> None:
        self._not_implemented("press")

    def hold(self, button: Button) -> None:
        self._not_implemented("hold")

    def release(self, button: Button) -> None:
        self._not_implemented("release")

    def move(self, x: float, y: float, ms: int) -> None:
        self._not_implemented("move")

    def camera(self, dx: float, dy: float, ms: int) -> None:
        self._not_implemented("camera")

    def wait(self, ms: int) -> None:
        time.sleep(ms / 1000)

    def close(self) -> None:
        self._sct.close()

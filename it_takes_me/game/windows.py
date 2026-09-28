"""Windows backend: screen capture via mss, input via a virtual Xbox 360 pad (vgamepad).

Runs on the gaming PC. Requires the ViGEmBus driver (vgamepad installs it on first import) and
the game to be in borderless/windowed mode so mss can capture it.

Input is state-based: `set_pad` writes the full pad state (sticks, buttons, triggers) in one
report, and it stays until the next call. The chunk executor owns timing.
"""

from __future__ import annotations

import io
import time
from typing import Any

from PIL import Image

from .io import NEUTRAL, Button, PadState

_XUSB_NAMES: dict[Button, str] = {
    Button.A: "XUSB_GAMEPAD_A",
    Button.B: "XUSB_GAMEPAD_B",
    Button.X: "XUSB_GAMEPAD_X",
    Button.Y: "XUSB_GAMEPAD_Y",
    Button.LB: "XUSB_GAMEPAD_LEFT_SHOULDER",
    Button.RB: "XUSB_GAMEPAD_RIGHT_SHOULDER",
    Button.START: "XUSB_GAMEPAD_START",
    Button.BACK: "XUSB_GAMEPAD_BACK",
    Button.DPAD_UP: "XUSB_GAMEPAD_DPAD_UP",
    Button.DPAD_DOWN: "XUSB_GAMEPAD_DPAD_DOWN",
    Button.DPAD_LEFT: "XUSB_GAMEPAD_DPAD_LEFT",
    Button.DPAD_RIGHT: "XUSB_GAMEPAD_DPAD_RIGHT",
    Button.LS: "XUSB_GAMEPAD_LEFT_THUMB",
    Button.RS: "XUSB_GAMEPAD_RIGHT_THUMB",
}


def _clamp(v: float) -> float:
    return max(-1.0, min(1.0, float(v)))


class WindowsGameIO:
    def __init__(self, monitor: int = 1) -> None:
        import mss  # lazy: only importable/meaningful on the gaming PC
        import vgamepad as vg  # pyright: ignore[reportMissingImports] - Windows-only package

        self._sct = mss.mss()
        self._monitor = self._sct.monitors[monitor]
        self._vg: Any = vg
        self._pad: Any = vg.VX360Gamepad()
        # Give the game a moment to enumerate the new controller.
        self._pad.reset()
        self._pad.update()
        time.sleep(0.5)

    # -- observation -------------------------------------------------------------------------

    def _grab(self) -> Image.Image:
        shot = self._sct.grab(self._monitor)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def capture(self) -> bytes:
        buf = io.BytesIO()
        self._grab().save(buf, format="PNG")
        return buf.getvalue()

    def snapshot(self, size: tuple[int, int]) -> Image.Image:
        return self._grab().convert("L").resize(size, Image.Resampling.BILINEAR, reducing_gap=2.0)

    # -- input ---------------------------------------------------------------------------------

    def set_pad(self, state: PadState) -> None:
        for button, name in _XUSB_NAMES.items():
            xusb = getattr(self._vg.XUSB_BUTTON, name)
            if button in state.buttons:
                self._pad.press_button(xusb)
            else:
                self._pad.release_button(xusb)
        self._pad.left_trigger(255 if Button.LT in state.buttons else 0)
        self._pad.right_trigger(255 if Button.RT in state.buttons else 0)
        lx, ly = state.left
        rx, ry = state.right
        self._pad.left_joystick_float(x_value_float=_clamp(lx), y_value_float=_clamp(ly))
        self._pad.right_joystick_float(x_value_float=_clamp(rx), y_value_float=_clamp(ry))
        self._pad.update()

    def close(self) -> None:
        # Leave the pad neutral when the process exits.
        self.set_pad(NEUTRAL)
        self._pad.reset()
        self._pad.update()
        self._sct.close()

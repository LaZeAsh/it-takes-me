"""Windows backend: screen capture via mss, input via a virtual Xbox 360 pad (vgamepad).

Runs on the gaming PC. Requires the ViGEmBus driver (vgamepad installs it on first import) and
the game to be in borderless/windowed mode so mss can capture it.

Pad state is held on this object: `hold` keeps a button down across tool calls until `release`,
so the model can do hold(RT) -> move(...) -> release(RT).
"""

from __future__ import annotations

import io
import time
from typing import Any

from .io import Button

# Buttons that are real digital buttons on an XInput pad. Triggers are analog and handled apart.
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
        self._held: set[Button] = set()
        # Give the game a moment to enumerate the new controller.
        self._pad.reset()
        self._pad.update()
        time.sleep(0.5)

    # -- observation -------------------------------------------------------------------------

    def capture(self) -> bytes:
        from PIL import Image

        shot = self._sct.grab(self._monitor)
        im = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    # -- pad state helpers -------------------------------------------------------------------

    def _set(self, button: Button, down: bool) -> None:
        if button is Button.LT:
            self._pad.left_trigger(255 if down else 0)
        elif button is Button.RT:
            self._pad.right_trigger(255 if down else 0)
        else:
            xusb = getattr(self._vg.XUSB_BUTTON, _XUSB_NAMES[button])
            if down:
                self._pad.press_button(xusb)
            else:
                self._pad.release_button(xusb)
        self._pad.update()

    def _stick(self, which: str, x: float, y: float, ms: int) -> None:
        setter = getattr(self._pad, f"{which}_joystick_float")
        setter(x_value_float=_clamp(x), y_value_float=_clamp(y))
        self._pad.update()
        time.sleep(max(0, ms) / 1000)
        setter(x_value_float=0.0, y_value_float=0.0)
        self._pad.update()

    # -- input ---------------------------------------------------------------------------------

    def press(self, button: Button, hold_ms: int = 80) -> None:
        self._set(button, True)
        time.sleep(max(0, hold_ms) / 1000)
        # Don't release a button the model is deliberately holding.
        if button not in self._held:
            self._set(button, False)

    def hold(self, button: Button) -> None:
        self._held.add(button)
        self._set(button, True)

    def release(self, button: Button) -> None:
        self._held.discard(button)
        self._set(button, False)

    def move(self, x: float, y: float, ms: int) -> None:
        self._stick("left", x, y, ms)

    def camera(self, dx: float, dy: float, ms: int) -> None:
        self._stick("right", dx, dy, ms)

    def wait(self, ms: int) -> None:
        time.sleep(max(0, ms) / 1000)

    def close(self) -> None:
        # Release everything so the pad is neutral when the process exits.
        for button in list(self._held):
            self.release(button)
        self._pad.reset()
        self._pad.update()
        self._sct.close()

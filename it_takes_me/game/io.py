"""Abstract interface between the model's tools and the actual game.

Everything the model can do to the game goes through this protocol, so the inference layer
is identical whether we are driving a virtual Xbox pad on the Windows
PC, or something else later.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class Button(StrEnum):
    A = "A"
    B = "B"
    X = "X"
    Y = "Y"
    LB = "LB"
    RB = "RB"
    LT = "LT"
    RT = "RT"
    START = "START"
    BACK = "BACK"
    DPAD_UP = "DPAD_UP"
    DPAD_DOWN = "DPAD_DOWN"
    DPAD_LEFT = "DPAD_LEFT"
    DPAD_RIGHT = "DPAD_RIGHT"
    LS = "LS"  # left-stick click
    RS = "RS"  # right-stick click


class GameIO(Protocol):
    """Capture the screen and inject controller input.

    Stick axes are in [-1, 1]; +x is right, +y is forward/up on screen. Durations are ms.
    Implementations should be quick: tool handlers run on the SDK's reader thread and block
    event delivery while they execute.
    """

    def capture(self) -> bytes:
        """Return the current frame as PNG bytes."""
        ...

    def press(self, button: Button, hold_ms: int = 80) -> None: ...

    def hold(self, button: Button) -> None: ...

    def release(self, button: Button) -> None: ...

    def move(self, x: float, y: float, ms: int) -> None:
        """Push the left stick to (x, y) for `ms`, then recentre."""
        ...

    def camera(self, dx: float, dy: float, ms: int) -> None:
        """Push the right stick to (dx, dy) for `ms`, then recentre."""
        ...

    def wait(self, ms: int) -> None: ...

    def close(self) -> None: ...

"""Abstract interface between the model's tools and the actual game.

Everything the model can do to the game goes through this protocol, so the inference layer
is identical whether we are driving a virtual Xbox pad on the Windows
PC, or something else later.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from PIL.Image import Image


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


@dataclass(frozen=True, slots=True)
class PadState:
    """Everything the pad is doing at one instant. Sticks are (x, y) in [-1, 1]; +x is right,
    +y is forward/up on screen. Triggers count as pressed when in `buttons`."""

    left: tuple[float, float] = (0.0, 0.0)
    right: tuple[float, float] = (0.0, 0.0)
    buttons: frozenset[Button] = frozenset()


NEUTRAL = PadState()


class GameIO(Protocol):
    """Capture the screen and drive the controller.

    Input is a single primitive: `set_pad` replaces the whole pad state, which stays until the
    next call. Timing (how long a state is held) is owned by the chunk executor, not here.
    """

    def capture(self) -> bytes:
        """Return the current frame as PNG bytes."""
        ...

    def snapshot(self, size: tuple[int, int]) -> Image:
        """Return the current frame as a small grayscale image, fast enough to poll ~10 Hz.
        Used by the chunk executor's `until` checks, never shown to the model."""
        ...

    def set_pad(self, state: PadState) -> None: ...

    def close(self) -> None: ...


def grab(game: GameIO) -> Image:
    """The current frame as an RGB image.

    Backends that can hand over the screen directly implement `capture_image()`; that skips
    encoding a full-resolution PNG (~0.4 s at 4K) only to decode it again for the model. Others
    fall back to decoding `capture()`.
    """
    capture_image = getattr(game, "capture_image", None)
    if capture_image is not None:
        return capture_image()
    from PIL import Image as PILImage

    with PILImage.open(io.BytesIO(game.capture())) as image:
        return image.convert("RGB")

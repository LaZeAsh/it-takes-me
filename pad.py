"""Play one character yourself through the virtual Xbox pad, no agent.

Opens the on-screen controller: left-click and hold buttons, drag the sticks, right-click a
stick to click it.  uv run python pad.py
"""

from __future__ import annotations

from it_takes_me.game.pad_gui import PadMixer, PadWindow
from it_takes_me.game.windows import WindowsGameIO

MONITOR = 1  # mss monitor index (unused for input, but WindowsGameIO opens it)


def main() -> None:
    pad = PadMixer(WindowsGameIO(monitor=MONITOR))
    try:
        PadWindow(pad, title="virtual pad").run()
    finally:
        pad.close()


if __name__ == "__main__":
    main()

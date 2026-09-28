"""Own the virtual Xbox pad for the whole play session.

Start this once and leave it open: it plugs in one controller, shows it on screen (left-click
and hold buttons, drag the sticks, right-click a stick to click it), and lets `run.py` drive
it over localhost. Restarting the agent then keeps the same controller, so the game keeps it
as player 2.  uv run python pad.py
"""

from __future__ import annotations

import logging
import sys

from it_takes_me.game.pad_gui import PadMixer, PadWindow
from it_takes_me.game.pad_host import PORT, bind_pad_server, serve_pad
from it_takes_me.game.windows import WindowsGameIO

MONITOR = 1  # mss monitor index (unused for input, but WindowsGameIO opens it)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        server = bind_pad_server()
    except OSError:
        sys.exit(f"a pad is already running (port {PORT} is taken); use that one")
    pad = PadMixer(WindowsGameIO(monitor=MONITOR))
    serve_pad(server, pad)
    print(f"pad ready; run.py will connect on port {PORT}. Close the window to unplug it.")
    try:
        PadWindow(pad, title="virtual pad").run()
    finally:
        pad.close()
        server.close()


if __name__ == "__main__":
    main()

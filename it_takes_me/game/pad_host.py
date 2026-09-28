"""Share one virtual pad across agent runs.

A ViGEm controller lives only as long as the process that created it, so every `run.py` start
would plug in a fresh controller and the game would lose track of which one is player 2.
Instead `pad.py` owns the pad for the whole play session and serves it on localhost; `run.py`
connects with `RemotePadIO` and streams its pad states there.

Wire format: one JSON object per line, `{"l": [x, y], "r": [x, y], "b": ["A", ...]}`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import threading
from typing import TYPE_CHECKING

from .io import NEUTRAL, Button, PadState

if TYPE_CHECKING:
    from PIL.Image import Image

    from .pad_gui import PadMixer
    from .windows import ScreenCapture

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 47800


def _encode(state: PadState) -> bytes:
    msg = {"l": list(state.left), "r": list(state.right), "b": sorted(state.buttons)}
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


def _decode(line: bytes) -> PadState:
    msg = json.loads(line)
    return PadState(
        left=(float(msg["l"][0]), float(msg["l"][1])),
        right=(float(msg["r"][0]), float(msg["r"][1])),
        buttons=frozenset(Button(b) for b in msg["b"]),
    )


def bind_pad_server() -> socket.socket:
    """Claim the pad port. Fails with OSError if another pad host already holds it, which is
    what stops a second `pad.py` from plugging in a second controller."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind((HOST, PORT))
    server.listen(1)
    return server


def serve_pad(server: socket.socket, mixer: PadMixer) -> threading.Thread:
    """Feed agent pad states from connected clients into `mixer`, one client at a time.
    A client that disconnects leaves the agent side of the pad neutral."""

    def _handle(conn: socket.socket) -> None:
        with conn, conn.makefile("rb") as lines:
            for line in lines:
                mixer.set_pad(_decode(line))
        mixer.set_pad(NEUTRAL)

    def _run() -> None:
        while True:
            conn, _ = server.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            log.info("agent connected")
            try:
                _handle(conn)
            except (OSError, ValueError) as e:
                log.warning("agent connection dropped: %s", e)
                mixer.set_pad(NEUTRAL)
            log.info("agent disconnected")

    thread = threading.Thread(target=_run, daemon=True, name="pad-server")
    thread.start()
    return thread


def connect_pad() -> socket.socket | None:
    """Connect to a running pad host, or None if `pad.py` is not running."""
    try:
        sock = socket.create_connection((HOST, PORT), timeout=0.5)
    except OSError:
        return None
    sock.settimeout(None)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


class RemotePadIO:
    """A `GameIO` that captures the screen locally and drives the pad owned by `pad.py`."""

    def __init__(self, screen: ScreenCapture, sock: socket.socket) -> None:
        self._screen = screen
        self._sock = sock

    def capture(self) -> bytes:
        return self._screen.capture()

    def snapshot(self, size: tuple[int, int]) -> Image:
        return self._screen.snapshot(size)

    def set_pad(self, state: PadState) -> None:
        self._sock.sendall(_encode(state))

    def close(self) -> None:
        with contextlib.suppress(OSError):  # pad.py may already be gone
            self.set_pad(NEUTRAL)
        self._sock.close()
        self._screen.close()

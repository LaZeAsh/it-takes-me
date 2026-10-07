"""Measure how long the right stick takes to turn the camera a full circle (LOOK_MS_PER_TURN).

With the game running and your character standing still somewhere with a varied view (not
facing a blank wall), run:  uv run python calibrate_look.py
It holds the right stick fully right for a few seconds per trial while it watches your half of
the screen for the moment the view comes back to where it started. Put the printed value in
it_takes_me/game/tuning.py.
"""

from __future__ import annotations

import statistics
import time

from it_takes_me.game.io import NEUTRAL, GameIO, PadState
from it_takes_me.game.pad_host import RemotePadIO, connect_pad
from it_takes_me.game.playback import half_snapshot
from it_takes_me.game.tuning import LOOK_MS_PER_TURN
from it_takes_me.game.windows import ScreenCapture, WindowsGameIO
from PIL import ImageChops, ImageFilter, ImageStat

MONITOR = 1
HALF = "left"  # May's half; "right" for Cody
TRIALS = 3
MAX_TURN_S = 10.0
POLL_S = 0.02


def _view(io: GameIO):
    # Blurred so a near match still scores low between samples a few pixels of turn apart.
    return half_snapshot(io, HALF).filter(ImageFilter.GaussianBlur(3))


def measure(io: GameIO, stick: float) -> tuple[float, float]:
    """Turn until the view matches the start again; (ms for a full turn, best match diff)."""
    io.set_pad(NEUTRAL)
    time.sleep(0.5)
    ref = _view(io)
    samples: list[tuple[float, float]] = []
    start = time.monotonic()
    io.set_pad(PadState(right=(stick, 0.0)))
    try:
        while (t := time.monotonic() - start) < MAX_TURN_S:
            diff = ImageStat.Stat(ImageChops.difference(ref, _view(io))).mean[0]
            samples.append((t * 1000, diff))
            time.sleep(POLL_S)
    finally:
        io.set_pad(NEUTRAL)
    # Once the view has turned away, the first dip back close to the start is the first full
    # turn (later circles dip too); its lowest point is the best match.
    peak = max(d for _, d in samples)
    turned_away, dip = False, []
    for t, d in samples:
        if d >= 0.6 * peak:
            if dip:
                break
            turned_away = True
        elif turned_away and d <= 0.35 * peak:
            dip.append((t, d))
    if not dip:
        raise SystemExit("the view never came back to the start: raise MAX_TURN_S")
    ms, diff = min(dip, key=lambda s: s[1])
    return ms, diff


def main() -> None:
    sock = connect_pad()
    io: GameIO = (
        RemotePadIO(ScreenCapture(monitor=MONITOR), sock) if sock else WindowsGameIO(MONITOR)
    )
    try:
        turns = []
        for i in range(TRIALS):
            ms, diff = measure(io, 1.0)
            print(f"trial {i + 1}: full turn in {ms:.0f} ms (match diff {diff:.1f})")
            turns.append(ms)
        best = round(statistics.median(turns))
        print(f"\nLOOK_MS_PER_TURN = {best}  (currently {LOOK_MS_PER_TURN})")
        print("A match diff above ~10 means the view never lined up: try a busier view.")
    finally:
        io.set_pad(NEUTRAL)
        io.close()


if __name__ == "__main__":
    main()

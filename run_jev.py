"""Run the Luna + Jev player as your It Takes Two co-op partner.

Separate from run.py (the Sol/Codex player). Luna describes each frame on your Codex subscription
(Fast tier); Jev picks the next skill via OpenRouter, so OPENROUTER_API_KEY must be in .env (see
.example.env). Uses your existing Codex login. Edit the hyperparameters below and run:
    uv run python run_jev.py
Type on the terminal to talk to Jev while it plays; `/quit` stops after the current step.
"""

from __future__ import annotations

import logging
from pathlib import Path

from it_takes_me.game.io import GameIO
from it_takes_me.game.pad_gui import PadMixer, start_pad_window
from it_takes_me.game.pad_host import RemotePadIO, connect_pad
from it_takes_me.game.windows import ScreenCapture, WindowsGameIO
from it_takes_me.jev.config import DECIDER_MODEL, PERCEIVER_MODEL, JevSettings
from it_takes_me.jev.loop import play_session
from rich.console import Console
from rich.logging import RichHandler

# ----------------------------------------------------------------------------------------------
# Hyperparameters
# ----------------------------------------------------------------------------------------------

PERCEIVER_MODEL_ID = PERCEIVER_MODEL  # Luna on Codex: describes frames
PERCEIVER_EFFORT = "low"  # lowest effort Codex offers for Luna
PERCEIVER_SERVICE_TIER: str | None = "priority"  # Codex Fast mode; None = standard tier
DECIDER_MODEL_ID = DECIDER_MODEL  # Jev: picks the next skill
CHARACTER = "May"

MONITOR = 1  # mss monitor index the game is on (1 = primary)
SHOW_PAD = True  # on-screen controller when run_jev.py owns the pad (pad.py shows its own)

MAX_STEPS: int | None = None
SKILLS_PER_LOOK = 2  # Jev decisions per Luna description
HISTORY_LEN = 6
FRAME_MAX_PX = 512
FRAME_JPEG_QUALITY = 85

RUNS_DIR = Path("runs/jev")
VERBOSE = False

# ----------------------------------------------------------------------------------------------

console = Console()


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if VERBOSE else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )
    settings = JevSettings(
        character=CHARACTER,
        perceiver_model=PERCEIVER_MODEL_ID,
        perceiver_effort=PERCEIVER_EFFORT,
        perceiver_service_tier=PERCEIVER_SERVICE_TIER,
        decider_model=DECIDER_MODEL_ID,
        runs_dir=RUNS_DIR,
        max_steps=MAX_STEPS,
        skills_per_look=SKILLS_PER_LOOK,
        history_len=HISTORY_LEN,
        frame_max_px=FRAME_MAX_PX,
        frame_jpeg_quality=FRAME_JPEG_QUALITY,
    )

    game: GameIO
    sock = connect_pad()
    if sock is not None:
        # pad.py owns the controller (and its on-screen window); we only stream inputs to it.
        game = RemotePadIO(ScreenCapture(monitor=MONITOR), sock)
        console.print("using the pad from pad.py")
    else:
        console.print(
            "[yellow]pad.py is not running: plugging in a new controller for this run only. "
            "Start pad.py first to keep one controller across runs.[/yellow]"
        )
        mixer = PadMixer(WindowsGameIO(monitor=MONITOR))
        if SHOW_PAD:
            start_pad_window(mixer, title=f"{CHARACTER} pad (Jev)")
        game = mixer
    try:
        play_session(game, settings, console=console)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")
    finally:
        game.close()


if __name__ == "__main__":
    main()

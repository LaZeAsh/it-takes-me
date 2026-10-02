"""Run GPT-6.1 Sol as your It Takes Two co-op partner.

Edit the hyperparameters below and run:  uv run python run.py
Type on the terminal to talk to the model while it plays; `/quit` stops after the current turn.
"""

from __future__ import annotations

import logging
from pathlib import Path

from it_takes_me.app import play_session
from it_takes_me.config import DEFAULT_MODEL, DEFAULT_REASONING_EFFORT, Settings
from it_takes_me.game.chunks import MAX_CHUNK_MS as DEFAULT_MAX_CHUNK_MS
from it_takes_me.game.io import GameIO
from it_takes_me.game.pad_gui import PadMixer, start_pad_window
from it_takes_me.game.pad_host import RemotePadIO, connect_pad
from it_takes_me.game.windows import ScreenCapture, WindowsGameIO
from it_takes_me.vision import FrameDetail
from rich.console import Console
from rich.logging import RichHandler

# ----------------------------------------------------------------------------------------------
# Hyperparameters
# ----------------------------------------------------------------------------------------------

RUNTIME = "codex"  # "codex" uses your subscription; "responses" uses OPENAI_API_KEY
MODEL = DEFAULT_MODEL
REASONING_EFFORT = DEFAULT_REASONING_EFFORT
SERVICE_TIER: str | None = "priority"  # Codex "Fast" tier: ~2x speed, more usage. None = standard
CHARACTER = "May"

MONITOR = 1  # mss monitor index the game is on (1 = primary)
SHOW_PAD = True  # on-screen controller when run.py owns the pad (pad.py shows its own)

MAX_TURNS: int | None = None
MAX_TOOL_CALLS_PER_TURN = 8
FRAME_AFTER_ACTION = True
MODEL_FRAME_DETAIL: FrameDetail = "low"  # high is for small prompts/details
MODEL_FRAME_LOW_MAX_PX = 512
MODEL_FRAME_HIGH_MAX_PX = 1536
MODEL_FRAME_JPEG_QUALITY = 85
MAX_CHUNK_MS = DEFAULT_MAX_CHUNK_MS  # up to 10 s; long runs automatically monitor stuck/cut
KEYFRAMES = 2  # mid-chunk frames returned when the model asks for `observe: keyframes`
COMPACT_AFTER_INPUT_TOKENS = 200_000
MAX_OUTPUT_TOKENS = 4000
API_STORE = True

RUNS_DIR = Path("runs")
EXTRA_INSTRUCTIONS: list[str] = []
VERBOSE = False

# ----------------------------------------------------------------------------------------------

console = Console()


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if VERBOSE else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )
    settings = Settings(
        runtime=RUNTIME,
        model=MODEL,
        reasoning_effort=REASONING_EFFORT,
        service_tier=SERVICE_TIER,
        runs_dir=RUNS_DIR,
        character=CHARACTER,
        max_tool_calls_per_turn=MAX_TOOL_CALLS_PER_TURN,
        max_turns=MAX_TURNS,
        frame_after_action=FRAME_AFTER_ACTION,
        model_frame_detail=MODEL_FRAME_DETAIL,
        model_frame_low_max_px=MODEL_FRAME_LOW_MAX_PX,
        model_frame_high_max_px=MODEL_FRAME_HIGH_MAX_PX,
        model_frame_jpeg_quality=MODEL_FRAME_JPEG_QUALITY,
        max_chunk_ms=MAX_CHUNK_MS,
        keyframes=KEYFRAMES,
        compact_after_input_tokens=COMPACT_AFTER_INPUT_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        api_store=API_STORE,
        extra_instructions=list(EXTRA_INSTRUCTIONS),
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
            start_pad_window(mixer, title=f"{CHARACTER} pad")
        game = mixer
    try:
        play_session(game, settings, console=console)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")
    finally:
        game.close()


if __name__ == "__main__":
    main()

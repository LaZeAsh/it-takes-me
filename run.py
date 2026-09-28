"""Run GPT-6 Astra as your It Takes Two co-op partner.

Edit the hyperparameters below and run:  uv run python run.py
Type on the terminal to talk to the model while it plays; `/quit` stops after the current turn.
"""

from __future__ import annotations

import logging
from pathlib import Path

from it_takes_me.config import Settings
from it_takes_me.game.io import GameIO
from it_takes_me.game.pad_gui import PadMixer, start_pad_window
from it_takes_me.game.pad_host import RemotePadIO, connect_pad
from it_takes_me.game.windows import ScreenCapture, WindowsGameIO
from it_takes_me.inference.player import AstraPlayer
from it_takes_me.inference.runtime import CodexRuntime
from it_takes_me.inference.tools import build_game_tools
from it_takes_me.recording import RunRecorder
from rich.console import Console
from rich.logging import RichHandler

# ----------------------------------------------------------------------------------------------
# Hyperparameters
# ----------------------------------------------------------------------------------------------

MODEL = "gpt-6-astra"
REASONING_EFFORT = "medium"
CHARACTER = "May"

MONITOR = 1  # mss monitor index the game is on (1 = primary)
SHOW_PAD = True  # on-screen controller when run.py owns the pad (pad.py shows its own)

MAX_TURNS: int | None = None
MAX_TOOL_CALLS_PER_TURN = 40
FRAME_AFTER_ACTION = True
MAX_CHUNK_MS = 3000  # longest action chunk the model may plan in one `act` call
KEYFRAMES = 2  # mid-chunk frames returned when the model asks for `observe: keyframes`
COMPACT_AFTER_TOKENS = 600_000

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
        model=MODEL,
        reasoning_effort=REASONING_EFFORT,
        runs_dir=RUNS_DIR,
        character=CHARACTER,
        max_tool_calls_per_turn=MAX_TOOL_CALLS_PER_TURN,
        max_turns=MAX_TURNS,
        frame_after_action=FRAME_AFTER_ACTION,
        max_chunk_ms=MAX_CHUNK_MS,
        keyframes=KEYFRAMES,
        compact_after_tokens=COMPACT_AFTER_TOKENS,
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
    recorder = RunRecorder(settings.runs_dir)
    console.print(f"recording to {recorder.dir}")
    tools = build_game_tools(
        game,
        frame_after_action=settings.frame_after_action,
        keyframes=settings.keyframes,
        max_chunk_ms=settings.max_chunk_ms,
        screen_half=settings.screen_half,
        on_frame=lambda png, reason: recorder.frame(png, reason=reason),
        on_say=lambda text: console.print(
            f"[bold magenta]{settings.character}:[/bold magenta] {text}"
        ),
        max_calls_per_turn=settings.max_tool_calls_per_turn,
    )
    try:
        with CodexRuntime(cwd=settings.cwd, tools=tools) as runtime:
            thread = runtime.start_thread(
                model=settings.model,
                reasoning_effort=settings.reasoning_effort,
                character=settings.character,
                extra_instructions=settings.extra_instructions,
            )
            console.print(
                f"thread {thread.id} on [bold]{settings.model}[/bold] ({settings.reasoning_effort})"
            )
            player = AstraPlayer(
                runtime=runtime,
                thread=thread,
                io=game,
                tools=tools,
                recorder=recorder,
                settings=settings,
                console=console,
            )
            player.start_stdin_listener()
            player.play()
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")
    finally:
        game.close()
        recorder.close()


if __name__ == "__main__":
    main()

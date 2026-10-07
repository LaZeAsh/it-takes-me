"""Replay recorded frames through either inference runtime without launching the game."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from it_takes_me.opus.runtime import OPUS_MODEL
from it_takes_me.sol.app import play_session
from it_takes_me.sol.config import DEFAULT_MODEL, DEFAULT_REASONING_EFFORT, Settings
from it_takes_me.sol.replay import ReplayGameIO
from rich.console import Console
from rich.logging import RichHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path, help="recorded run directory containing frames/")
    parser.add_argument("--runtime", choices=("codex", "responses", "claude"), default="codex")
    parser.add_argument("--model", help="default: Sol for codex/responses, Opus for claude")
    parser.add_argument("--effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--character", choices=("May", "Cody"), default="May")
    parser.add_argument("--turns", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    console = Console()
    logging.basicConfig(format="%(message)s", handlers=[RichHandler(console=console)])
    settings = Settings(
        runtime=args.runtime,
        model=args.model or (OPUS_MODEL if args.runtime == "claude" else DEFAULT_MODEL),
        reasoning_effort=args.effort,
        character=args.character,
        max_turns=args.turns,
    )
    game = ReplayGameIO(args.run)
    try:
        output = play_session(
            game,
            settings,
            console=console,
            interactive=False,
            instant_actions=True,
        )
        console.print(f"replay recorded to {output}")
    finally:
        game.close()


if __name__ == "__main__":
    main()

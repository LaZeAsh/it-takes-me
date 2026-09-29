"""Replay recorded frames through either inference runtime without launching the game."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from it_takes_me.app import play_session
from it_takes_me.config import Settings
from it_takes_me.game.replay import ReplayGameIO
from rich.console import Console
from rich.logging import RichHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path, help="recorded run directory containing frames/")
    parser.add_argument("--runtime", choices=("codex", "responses"), default="codex")
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--character", choices=("May", "Cody"), default="May")
    parser.add_argument("--turns", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    console = Console()
    logging.basicConfig(format="%(message)s", handlers=[RichHandler(console=console)])
    settings = Settings(
        runtime=args.runtime,
        model=args.model,
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

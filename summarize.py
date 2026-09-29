"""Print comparable metrics for one or more recorded runs."""

from __future__ import annotations

import argparse
from pathlib import Path

from it_takes_me.analysis import summarize_run
from rich.console import Console
from rich.table import Table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="*", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = args.runs or sorted(Path("runs").glob("*/events.jsonl"))
    paths = [path.parent if path.name == "events.jsonl" else path for path in paths]
    table = Table("run", "runtime/model", "sec", "turns", "turn s", "tools", "act/look")
    table.add_column("decision s")
    table.add_column("obs")
    table.add_column("tokens")
    table.add_column("tok/act")
    table.add_column("errors")
    for path in paths:
        summary = summarize_run(path)
        table.add_row(
            summary.run,
            f"{summary.runtime}/{summary.model}",
            str(summary.seconds),
            str(summary.turns),
            str(summary.median_turn_seconds),
            str(summary.tool_calls),
            f"{summary.acts}/{summary.looks}",
            str(summary.median_decision_seconds),
            str(summary.observations),
            str(summary.total_tokens),
            str(summary.tokens_per_act),
            str(summary.errors),
        )
    Console(width=160).print(table)


if __name__ == "__main__":
    main()

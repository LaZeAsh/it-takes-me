"""Run-log summaries used to compare harness and model configurations."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RunSummary:
    run: str
    runtime: str
    model: str
    seconds: float
    turns: int
    median_turn_seconds: float
    tool_calls: int
    acts: int
    looks: int
    observations: int
    median_decision_seconds: float
    errors: int
    total_tokens: int
    tokens_per_act: int


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 2) if values else 0.0


def summarize_run(run_dir: Path) -> RunSummary:
    events_path = Path(run_dir) / "events.jsonl"
    events: list[dict[str, Any]] = [
        json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line
    ]
    session = next((event for event in events if event.get("kind") == "session"), {})
    tools = [event for event in events if event.get("kind") == "tool_call"]
    turns = [event for event in events if event.get("kind") == "turn_end"]
    observations = [event for event in events if event.get("kind") == "observation"]
    usage = [event for event in events if event.get("kind") == "usage"]
    total_tokens = 0
    if usage:
        cumulative = usage[-1].get("cumulative") or {}
        total_tokens = int(cumulative.get("total_tokens", usage[-1].get("total_tokens", 0)))

    observation_kind = "observation" if observations else "frame"
    observation_times: list[float] = []
    decision_times: list[float] = []
    for event in events:
        if event.get("kind") == observation_kind:
            observation_times.append(float(event["t"]))
        elif event.get("kind") == "tool_call":
            tool_started = float(event["t"]) - float(event.get("duration_ms", 0)) / 1000
            prior = [timestamp for timestamp in observation_times if timestamp <= tool_started]
            if prior:
                decision_times.append(max(0.0, tool_started - prior[-1]))

    acts = sum(event.get("tool") == "act" for event in tools)
    return RunSummary(
        run=Path(run_dir).name,
        runtime=str(session.get("runtime", "legacy")),
        model=str(session.get("model", "unknown")),
        seconds=round(float(events[-1]["t"]), 2) if events else 0.0,
        turns=len(turns),
        median_turn_seconds=_median([float(event.get("duration_ms", 0)) / 1000 for event in turns]),
        tool_calls=len(tools),
        acts=acts,
        looks=sum(event.get("tool") == "look_at_screen" for event in tools),
        observations=len(observations),
        median_decision_seconds=_median(decision_times),
        errors=sum(event.get("kind") == "error" for event in events),
        total_tokens=total_tokens,
        tokens_per_act=round(total_tokens / acts) if acts else 0,
    )

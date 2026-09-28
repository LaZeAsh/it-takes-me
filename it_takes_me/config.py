"""Runtime settings for a play session."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "gpt-6-astra"


@dataclass(slots=True)
class Settings:
    model: str = DEFAULT_MODEL
    reasoning_effort: str = "medium"
    runs_dir: Path = Path("runs")
    # Which character the model controls. The human plays the other one.
    character: str = "May"
    # Per-turn guardrails.
    max_tool_calls_per_turn: int = 40
    max_turns: int | None = None
    frame_after_action: bool = True
    # Action chunks: longest chunk the model may plan, and mid-chunk frames on `observe: keyframes`.
    max_chunk_ms: int = 3000
    keyframes: int = 2
    # Compact the thread once its context passes this many tokens.
    compact_after_tokens: int = 600_000
    extra_instructions: list[str] = field(default_factory=list)

    @property
    def screen_half(self) -> str:
        """Half of the split screen showing `character`: May is always left, Cody right."""
        return "left" if self.character == "May" else "right"

    @property
    def cwd(self) -> Path:
        return Path.cwd()

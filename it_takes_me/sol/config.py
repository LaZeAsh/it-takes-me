"""Runtime settings for a play session."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from it_takes_me.game.tuning import MAX_CHUNK_MS
from it_takes_me.vision import FrameDetail, ScreenHalf

# DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_REASONING_EFFORT = "low"


@dataclass(slots=True)
class Settings:
    runtime: str = "codex"
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    service_tier: str | None = "priority"
    runs_dir: Path = Path("runs")
    character: Literal["May", "Cody"] = "May"
    max_tool_calls_per_turn: int = 30
    max_turns: int | None = None
    frame_after_action: bool = True

    model_frame_detail: FrameDetail = "low"
    model_frame_low_max_px: int = 512
    model_frame_high_max_px: int = 1536
    model_frame_jpeg_quality: int = 85
    max_chunk_ms: int = MAX_CHUNK_MS
    keyframes: int = 2
    # Return long chunks early and allow keep_moving, so the model plans while it moves. Off:
    # planning from a predicted position compounded errors; standing still costs nothing.
    pipelining: bool = False
    compact_after_input_tokens: int = 244_800
    max_output_tokens: int = 4000
    api_store: bool = True
    extra_instructions: list[str] = field(default_factory=list)

    @property
    def screen_half(self) -> ScreenHalf:
        """Half of the split screen showing `character`: May is always left, Cody right."""
        return "left" if self.character == "May" else "right"

    @property
    def cwd(self) -> Path:
        return Path.cwd()

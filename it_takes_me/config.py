"""Runtime settings for a play session."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from it_takes_me.vision import FrameDetail, ScreenHalf

DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_REASONING_EFFORT = "low"


@dataclass(slots=True)
class Settings:
    runtime: str = "codex"
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    runs_dir: Path = Path("runs")
    # Which character the model controls. The human plays the other one.
    character: str = "May"
    # Per-turn guardrails.
    max_tool_calls_per_turn: int = 8
    max_turns: int | None = None
    frame_after_action: bool = True
    # Model-facing observations are cropped to the controlled half and resized. Full-resolution
    # PNG captures are still retained in the run recording.
    model_frame_detail: FrameDetail = "low"
    model_frame_low_max_px: int = 512
    model_frame_high_max_px: int = 1536
    model_frame_jpeg_quality: int = 85
    # Action chunks: longest chunk the model may plan, and mid-chunk frames on `observe: keyframes`.
    max_chunk_ms: int = 3000
    keyframes: int = 2
    # Both runtimes compact automatically when rendered input context passes this threshold,
    # rather than using cumulative session usage, which only grows over a session.
    compact_after_input_tokens: int = 200_000
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

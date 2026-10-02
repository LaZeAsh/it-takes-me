"""Settings for a Luna + Jev session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from it_takes_me.vision import ScreenHalf

PERCEIVER_MODEL = "gpt-6-luna"  # on the local Codex subscription
DECIDER_MODEL = "~typesafe/jev-latest"


@dataclass(slots=True)
class JevSettings:
    # Which character the model controls. The human plays the other one.
    character: str = "May"
    perceiver_model: str = PERCEIVER_MODEL
    decider_model: str = DECIDER_MODEL
    runs_dir: Path = Path("runs/jev")
    max_steps: int | None = None
    # Jev decisions (one skill each) made from one Luna description before Luna looks again.
    skills_per_look: int = 1
    # Past decisions Jev sees, newest last.
    history_len: int = 6
    # Frames sent to Luna are cropped to the controlled half and resized.
    frame_max_px: int = 512
    frame_jpeg_quality: int = 85
    # Luna's reasoning effort ("low" is the lowest Codex offers) and service tier
    # ("priority" is Codex's Fast mode; None is the standard tier).
    perceiver_effort: str = "low"
    perceiver_service_tier: str | None = "priority"
    # Tests only: apply pad states without real-time waits.
    instant_actions: bool = False

    @property
    def partner(self) -> str:
        return "Cody" if self.character.lower() == "may" else "May"

    @property
    def screen_half(self) -> ScreenHalf:
        """Half of the split screen showing `character`: May is always left, Cody right."""
        return "left" if self.character == "May" else "right"

"""The scene Luna extracts from a frame. Pure description: no actions, tasks, or advice."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Side = Literal["front", "back", "left", "right"]


class Thing(BaseModel):
    kind: Literal[
        "yellow_circle",
        "button_prompt",
        "tutorial_prompt",
        "objective_marker",
        "partner_marker",
        "partner",
        "platform",
        "ledge",
        "wall",
        "gap",
        "object",
        "hazard",
        "other",
    ]
    label: str = Field(description="A few words naming it, e.g. 'fuse with Y prompt'.")
    screen_x: float = Field(description="Horizontal position, 0 = left edge, 1 = right edge.")
    screen_y: float = Field(description="Vertical position, 0 = top, 1 = bottom.")
    direction_deg: int = Field(
        description="Horizontal angle from the camera's forward direction (screen center): "
        "negative is left, positive is right, about +/-45 at the screen edges."
    )
    distance: Literal["touching", "near", "mid", "far"]
    height: Literal["above", "level", "below"] = Field(
        description="Relative to the controlled character's feet."
    )


class Character(BaseModel):
    visible: bool
    airborne: bool
    surface: str = Field(description="What they stand on, e.g. 'floor', 'narrow shelf', 'box top'.")
    edges: list[Side] = Field(
        description="Sides (relative to the camera) with a drop within a few steps."
    )
    holding: str | None = Field(description="Object being carried, or null.")


class Scene(BaseModel):
    scene: Literal["gameplay", "cutscene", "menu", "loading", "other"]
    character: Character
    things: list[Thing]
    screen_text: list[str] = Field(description="Readable on-screen text and button prompts.")

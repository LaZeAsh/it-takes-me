"""Luna: turn one frame into a `Scene`. It never sees actions, tasks, or the action space.

Luna runs on the local Codex subscription (the Codex app-server via the `openai_codex` SDK), on
the Fast service tier. Each frame gets a fresh ephemeral thread so descriptions stay stateless,
with the perceiver instructions replacing Codex's coding-agent base instructions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai.lib._pydantic import to_strict_json_schema
from openai_codex import LocalImageInput, TextInput
from openai_codex._sandbox import Sandbox

from it_takes_me.jev.state import Scene


def perceiver_instructions(character: str, partner: str) -> str:
    partner_dot = "green" if partner == "Cody" else "blue"
    return (
        f"You describe frames from the split-screen game It Takes Two. The image is {character}'s "
        f"half of the screen; {character} is the character the camera follows. Describe only "
        "what is visible, as structured data. Do not suggest actions or goals.\n"
        "- character: whether "
        f"{character} is visible and airborne, the surface under them, which sides have a drop "
        "within a few steps (relative to the camera: front is away from the camera), and "
        "anything they carry.\n"
        "- things: every marker and every nearby object, platform, ledge, wall, gap, or hazard "
        "that matters for moving around, with its position, angle from the camera's forward "
        f"direction, distance from {character}, and height relative to {character}'s feet. "
        "List at most 8 things: markers first, then the nearest things that matter for "
        "moving. Keep labels to a few words. Marker kinds, by color and shape: "
        "yellow_circle (a YELLOW ring around a white dot, sitting on an object; it shows a "
        "controller letter when close), button_prompt (a controller letter with no yellow "
        "ring), tutorial_prompt (a button circle with a word under it near the "
        "bottom-center), objective_marker (a small WHITE diamond/hexagon icon with a symbol "
        f"inside, often far away), partner_marker (a small {partner_dot.upper()} ring or dot "
        f"showing where {partner} is, often at the screen edge; never a yellow_circle). Only "
        "report a marker you can actually see. "
        f"Use kind 'partner' if {partner} is visible.\n"
        "- screen_text: any readable text or button labels.\n"
        "- scene: gameplay, cutscene (cinematic, no control), menu, loading, or other."
    )


@dataclass(frozen=True, slots=True)
class Perception:
    scene: Scene
    model: str
    latency_ms: int
    usage: dict[str, Any] | None


class Perceiver:
    """Describe frames with Luna through a running `openai_codex.Codex` client."""

    def __init__(
        self,
        codex: Any,
        model: str,
        *,
        character: str,
        partner: str,
        effort: str,
        service_tier: str | None,
    ) -> None:
        self._codex = codex
        self._model = model
        self._instructions = perceiver_instructions(character, partner)
        self._effort = effort
        self._service_tier = service_tier
        self._schema = to_strict_json_schema(Scene)

    def describe(self, image_path: Path) -> Perception:
        started = time.monotonic()
        thread = self._codex.thread_start(
            model=self._model,
            base_instructions=self._instructions,
            ephemeral=True,
            sandbox=Sandbox.read_only,
            service_tier=self._service_tier,
        )
        result = thread.run(
            [TextInput("Describe this frame."), LocalImageInput(str(image_path.resolve()))],
            effort=self._effort,
            output_schema=self._schema,
            service_tier=self._service_tier,
        )
        latency_ms = round((time.monotonic() - started) * 1000)
        if result.final_response is None:
            error = result.error.message if result.error else result.status
            raise RuntimeError(f"perceiver returned no scene: {error}")
        usage = result.usage.last.model_dump() if result.usage is not None else None
        return Perception(
            Scene.model_validate_json(result.final_response), self._model, latency_ms, usage
        )

"""Dynamic tools exposed to the model on the Codex thread.

`ToolRegistry` turns Python callables into `dynamicTools` specs for `thread/start` and
dispatches the `item/tool/call` requests the app-server sends back while a turn runs.
`build_game_tools` wires the It Takes Two action set on top of a `GameIO`.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from it_takes_me.game.chunks import (
    DIRECTIONS,
    LOOK_DIRECTIONS,
    MAX_CHUNK_MS,
    SKILLS,
    compile_chunk,
    play_chunk,
)
from it_takes_me.game.io import Button, GameIO

log = logging.getLogger(__name__)

ContentItem = dict[str, Any]
ToolHandler = Callable[[dict[str, Any]], list[ContentItem]]


def text_item(text: str) -> ContentItem:
    return {"type": "inputText", "text": text}


def image_item(png: bytes) -> ContentItem:
    b64 = base64.b64encode(png).decode("ascii")
    return {"type": "inputImage", "imageUrl": f"data:image/png;base64,{b64}"}


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class ToolBudgetExceeded(Exception):
    pass


@dataclass
class ToolRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)
    # Per-turn budget. `None` disables it. Reset with `begin_turn()`.
    max_calls_per_turn: int | None = None
    calls_this_turn: int = 0
    # Observers get (tool, arguments, response_dict, duration_ms) after every dispatch.
    observers: list[Callable[[str, dict[str, Any], dict[str, Any], float], None]] = field(
        default_factory=list
    )

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        if name in self.tools:
            raise ValueError(f"tool already registered: {name}")
        self.tools[name] = ToolSpec(name, description, input_schema, handler)

    def specs(self) -> list[dict[str, Any]]:
        return [spec.to_wire() for spec in self.tools.values()]

    def begin_turn(self) -> None:
        self.calls_this_turn = 0

    def dispatch(self, name: str, arguments: Any) -> dict[str, Any]:
        """Run a tool and shape the result as a `DynamicToolCallResponse`.

        Never raises: failures are reported to the model as `success: False` with a message so
        it can recover on its own.
        """
        args = arguments if isinstance(arguments, dict) else {}
        started = time.monotonic()
        self.calls_this_turn += 1
        try:
            if (
                self.max_calls_per_turn is not None
                and self.calls_this_turn > self.max_calls_per_turn
            ):
                raise ToolBudgetExceeded(
                    f"Tool budget for this turn ({self.max_calls_per_turn} calls) is spent. "
                    "Stop calling tools and end your turn with a one-line status."
                )
            spec = self.tools.get(name)
            if spec is None:
                raise KeyError(f"unknown tool: {name}")
            items = spec.handler(args)
            response = {"success": True, "contentItems": items}
        except Exception as exc:  # noqa: BLE001 - surfaced to the model on purpose
            log.warning("tool %s failed: %s", name, exc)
            response = {
                "success": False,
                "contentItems": [text_item(f"{type(exc).__name__}: {exc}")],
            }
        duration_ms = (time.monotonic() - started) * 1000
        for observer in self.observers:
            observer(name, args, response, duration_ms)
        return response


# --- It Takes Two action set --------------------------------------------------------------

_BUTTON = {"type": "string", "enum": [b.value for b in Button]}
_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skill": {"type": "string", "enum": list(SKILLS)},
        "dir": {"type": "string", "enum": list(DIRECTIONS)},
        "ms": {"type": "integer", "minimum": 0, "maximum": MAX_CHUNK_MS},
        "sprint": {"type": "boolean"},
        "button": {"type": "string", "enum": ["LT", "RT"]},
        "look": {"type": "string", "enum": list(LOOK_DIRECTIONS)},
        "left": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
        "right": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
        "buttons": {"type": "array", "items": _BUTTON},
    },
    "required": ["skill"],
    "additionalProperties": False,
}

_ACT_DESCRIPTION = """\
Play a short chunk of input (up to {max_ms} ms total). Steps run back to back with exact \
timing on the pad; the pad returns to neutral when the chunk ends. Plan the next 0.5-3 s, not \
more: you will see the result and plan again.

`dir` is relative to the camera: forward, back, left, right, forward-left, forward-right, \
back-left, back-right, none. Every skill except wait/raw accepts `dir` (steer while doing it).

Skills:
- run {{dir, ms=500, sprint?}}: move with the left stick.
- jump {{dir}}: one jump, including airtime.
- double_jump {{dir}}: jump, then jump again in the air (longer/higher gaps).
- dash {{dir}}: quick dash (works in the air too).
- jump_dash {{dir}}: jump then dash in the air (long horizontal gaps).
- ground_pound {{dir}}: slam down; use while airborne.
- interact {{dir, ms=100}}: Y. Use a longer ms for hold-to-interact prompts.
- grapple {{dir}}: RB, grapple to a rope point in range.
- ability {{button: LT|RT, dir, ms=300}}: hold a chapter ability trigger.
- look {{look: left|right|up|down, dir, ms=200}}: turn the camera.
- wait {{ms=500}}: stand still.
- raw {{ms, left: [x,y], right: [x,y], buttons: [...]}}: exact pad state for ms, for anything \
the skills cannot express. Sticks in [-1, 1], +y is forward/up.

`observe`: "end" (default) returns the frame after the chunk; "keyframes" also returns \
{{n}} frames from during the chunk, to see why a jump or sequence went wrong."""


def build_game_tools(
    io: GameIO,
    *,
    frame_after_action: bool = True,
    keyframes: int = 2,
    max_chunk_ms: int = MAX_CHUNK_MS,
    on_frame: Callable[[bytes, str], object] | None = None,
    on_say: Callable[[str], None] | None = None,
    max_calls_per_turn: int | None = None,
) -> ToolRegistry:
    reg = ToolRegistry(max_calls_per_turn=max_calls_per_turn)

    def frame(png: bytes, reason: str) -> ContentItem:
        if on_frame is not None:
            on_frame(png, reason)
        return image_item(png)

    def look_at_screen(_: dict[str, Any]) -> list[ContentItem]:
        return [
            text_item(f"frame captured at {time.strftime('%H:%M:%S')}"),
            frame(io.capture(), "look"),
        ]

    def act(a: dict[str, Any]) -> list[ContentItem]:
        steps = a.get("steps") or []
        intent = str(a.get("intent", "")).strip()
        observe = a.get("observe", "end")
        segments = compile_chunk(steps, max_chunk_ms)
        shots = play_chunk(io, segments, keyframes if observe == "keyframes" else 0)
        total = sum(s.ms for s in segments)
        items = [text_item(f"done: {len(steps)} steps, {total} ms ({intent})")]
        for t, png in shots:
            items += [text_item(f"keyframe at {t} ms"), frame(png, f"keyframe {t}ms: {intent}")]
        if frame_after_action:
            items += [text_item("end of chunk"), frame(io.capture(), f"after: {intent}")]
        return items

    def say(a: dict[str, Any]) -> list[ContentItem]:
        text = str(a.get("text", "")).strip()
        if on_say is not None:
            on_say(text)
        return [text_item("said to your co-op partner: " + text)]

    reg.register(
        "look_at_screen",
        "Capture the current game frame. Use this whenever you need to know what is happening "
        "now; the frame you got at the start of the turn goes stale within a second.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        look_at_screen,
    )
    reg.register(
        "act",
        _ACT_DESCRIPTION.format(max_ms=max_chunk_ms, n=keyframes),
        {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "What this chunk is meant to achieve, in a few words.",
                },
                "steps": {"type": "array", "items": _STEP_SCHEMA, "minItems": 1},
                "observe": {"type": "string", "enum": ["end", "keyframes"]},
            },
            "required": ["intent", "steps"],
            "additionalProperties": False,
        },
        act,
    )
    reg.register(
        "say",
        "Say something short to your human co-op partner (shown on their terminal). Use it to "
        "coordinate: what you are about to do, or what you need them to do.",
        {
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": 300}},
            "required": ["text"],
            "additionalProperties": False,
        },
        say,
    )
    return reg

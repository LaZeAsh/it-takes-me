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
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from it_takes_me.game.chunks import (
    CONDITIONS,
    DIRECTIONS,
    LOOK_DIRECTIONS,
    MAX_CHUNK_MS,
    SKILLS,
    compile_chunk,
    play_chunk,
)
from it_takes_me.game.io import Button, GameIO
from it_takes_me.vision import FrameDetail, ModelFrame, ModelFrameEncoder, ScreenHalf

log = logging.getLogger(__name__)

ContentItem = dict[str, Any]
ToolHandler = Callable[[dict[str, Any]], list[ContentItem]]


def text_item(text: str) -> ContentItem:
    return {"type": "inputText", "text": text}


def image_item(frame: ModelFrame) -> ContentItem:
    b64 = base64.b64encode(frame.data).decode("ascii")
    return {"type": "inputImage", "imageUrl": f"data:{frame.media_type};base64,{b64}"}


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

    def responses_specs(self) -> list[dict[str, Any]]:
        """Return the same registry in the Responses API function-tool shape."""
        return [
            {
                "type": "function",
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
                "strict": False,
            }
            for spec in self.tools.values()
        ]

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
        "ms": {
            "type": "integer",
            "minimum": 1,
            "description": "Total step duration, including button holds, gaps, and movement.",
        },
        "hold_ms": {
            "type": "integer",
            "minimum": 1,
            "description": "Button hold within ms; each hold for double_jump/jump_dash.",
        },
        "gap_ms": {
            "type": "integer",
            "minimum": 1,
            "description": "Released time between the two presses in double_jump/jump_dash.",
        },
        "sprint": {"type": "boolean"},
        "button": {**_BUTTON, "description": "Any button for press; only LT or RT for ability."},
        "look": {"type": "string", "enum": list(LOOK_DIRECTIONS)},
        "left": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
        "right": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
        "buttons": {"type": "array", "items": _BUTTON},
        "until": {"type": "array", "items": {"type": "string", "enum": list(CONDITIONS)}},
    },
    "required": ["skill"],
    "additionalProperties": False,
}

_ACT_DESCRIPTION = """\
Play a short chunk of input (up to {max_ms} ms total). Steps run back to back with exact \
timing on the pad; the pad returns to neutral when the chunk ends. Choose durations based on \
distance and uncertainty: 100-500 ms for precise corrections, 1-3 s to explore, and longer \
runs for a clearly visible unobstructed route, within the total limit. Avoid repeated short \
runs on a clear route: each new decision adds model latency. Shorten runs near a destination \
or hazard; the local checks cannot detect arrival or reliably prevent falls.

Every skill accepts `ms`, its TOTAL duration. Button skills also accept `hold_ms`: the button \
is held at the start, then released while movement continues for the remaining ms. Defaults \
are 100 ms holds, except interact/ability/skip_cutscene which hold for the entire step. \
A hold must fit within ms. double_jump/jump_dash also accept `gap_ms` (default 250): time between \
two holds, each of hold_ms. Their total ms must cover 2*hold_ms + gap_ms. All timings are \
positive integers. Omitted fields retain the defaults below.

`dir` is relative to the camera: forward, back, left, right, forward-left, forward-right, \
back-left, back-right, none. Every skill except wait/raw/skip_cutscene accepts `dir` \
(steer while doing it).

Skills:
- run {{dir, ms=500, sprint?, until?}}: move with the left stick.
- jump {{dir, ms=500, hold_ms=100}}: one jump, including movement after release.
- double_jump {{dir, ms=850, hold_ms=100, gap_ms=250}}: jump, then jump again in the air.
- dash {{dir, ms=350, hold_ms=100}}: quick dash (works in the air too).
- jump_dash {{dir, ms=700, hold_ms=100, gap_ms=250}}: jump then dash in the air.
- ground_pound {{dir, ms=600, hold_ms=100}}: slam down; use while airborne.
- interact {{dir, ms=100, hold_ms?}}: Y. Defaults to holding for ms; set hold_ms for a tap.
- grapple {{dir, ms=700, hold_ms=100}}: RB, grapple to a rope point in range.
- ability {{button: LT|RT, dir, ms=300, hold_ms?}}: trigger; defaults to holding for ms.
- look {{look: left|right|up|down, dir, ms=200}}: turn the camera.
- locate_partner {{dir, ms=300, hold_ms=100}}: click RS to reveal your partner's location. \
Use this alone when you lose track of your partner; inspect the returned frame before moving.
- skip_cutscene {{ms=2000, hold_ms?}}: hold B to skip a visible cutscene, with neutral sticks. \
Defaults to holding for the entire ms. Use this as a single step and inspect the returned \
frame before resuming gameplay. Choose a longer ms if the skip prompt needs more hold time.
- press {{button, dir, ms=100, hold_ms=100}}: press any button, optionally move after release.
- wait {{ms=500, until?}}: stand still.
- raw {{ms, left: [x,y], right: [x,y], buttons: [...]}}: exact pad state for ms, for anything \
the skills cannot express. Sticks in [-1, 1], +y is forward/up.

`until` (run and wait only) turns `ms` into a timeout and ends the step early when a check \
fires; the rest of the chunk is then skipped and you are told which check fired:
- stuck: your view stopped changing while you were moving (walked into a wall or ledge).
- cut: the screen changed abruptly (cutscene, fade, respawn, menu).
Runs longer than 3000 ms automatically enable stuck and cut checks (cut only with dir=none), \
even if until is omitted or empty. Shorter runs can opt in using until. For example, \
run forward ms=7000 executes without another model decision until its timeout or a check \
fires, provided it fits the chunk limit. Wait can use until [cut] through a cutscene.

`observe`: "end" (default) returns the frame after the chunk; "keyframes" also returns \
{n} frames from during the chunk, to see why a jump or sequence went wrong."""


def build_game_tools(
    io: GameIO,
    *,
    frame_after_action: bool = True,
    keyframes: int = 2,
    max_chunk_ms: int = MAX_CHUNK_MS,
    screen_half: ScreenHalf = "left",
    frame_encoder: ModelFrameEncoder | None = None,
    on_frame: Callable[[bytes, str], object] | None = None,
    on_observation: Callable[[ModelFrame, str], object] | None = None,
    on_say: Callable[[str], None] | None = None,
    max_calls_per_turn: int | None = None,
    instant_actions: bool = False,
) -> ToolRegistry:
    reg = ToolRegistry(max_calls_per_turn=max_calls_per_turn)
    encoder = frame_encoder or ModelFrameEncoder(half=screen_half)
    step_schema = deepcopy(_STEP_SCHEMA)
    for timing in ("ms", "hold_ms", "gap_ms"):
        step_schema["properties"][timing]["maximum"] = max_chunk_ms

    def frame(png: bytes, reason: str, detail: FrameDetail = "low") -> ContentItem:
        if on_frame is not None:
            on_frame(png, reason)
        model_frame = encoder.encode(png, detail=detail)
        if on_observation is not None:
            on_observation(model_frame, reason)
        return image_item(model_frame)

    def look_at_screen(a: dict[str, Any]) -> list[ContentItem]:
        detail: FrameDetail = "high" if a.get("detail") == "high" else "low"
        return [
            text_item(f"frame captured at {time.strftime('%H:%M:%S')}"),
            frame(io.capture(), f"look ({detail})", detail),
        ]

    def act(a: dict[str, Any]) -> list[ContentItem]:
        steps = a.get("steps") or []
        intent = str(a.get("intent", "")).strip()
        observe = a.get("observe", "end")
        segments = compile_chunk(steps, max_chunk_ms)
        result = play_chunk(
            io,
            segments,
            keyframes if observe == "keyframes" else 0,
            half=screen_half,
            instant=instant_actions,
        )
        if result.stopped is None:
            summary = f"done: {len(steps)} steps, {result.elapsed_ms} ms ({intent})"
        else:
            i, cond = result.stopped
            summary = (
                f"stopped early: step {i} ({steps[i].get('skill')}) ended on `{cond}` after "
                f"{result.elapsed_ms} ms; skipped {len(steps) - i - 1} later steps ({intent})"
            )
        items = [text_item(summary)]
        for t, png in result.shots:
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
        "Capture a current game frame when the world may have changed without your input or the "
        "last result is ambiguous. Use low detail for navigation and high only for small prompts, "
        "text, or objects. Do not call this immediately after receiving a turn-start or act frame.",
        {
            "type": "object",
            "properties": {"detail": {"type": "string", "enum": ["low", "high"]}},
            "additionalProperties": False,
        },
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
                "steps": {"type": "array", "items": step_schema, "minItems": 1},
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

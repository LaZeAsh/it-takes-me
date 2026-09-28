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

_AXIS = {"type": "number", "minimum": -1, "maximum": 1}
_MS = {"type": "integer", "minimum": 0, "maximum": 2000}
_BUTTON = {"type": "string", "enum": [b.value for b in Button]}


def build_game_tools(
    io: GameIO,
    *,
    frame_after_action: bool = True,
    on_frame: Callable[[bytes, str], object] | None = None,
    on_say: Callable[[str], None] | None = None,
    max_calls_per_turn: int | None = None,
) -> ToolRegistry:
    reg = ToolRegistry(max_calls_per_turn=max_calls_per_turn)

    def frame(reason: str) -> ContentItem:
        png = io.capture()
        if on_frame is not None:
            on_frame(png, reason)
        return image_item(png)

    def after(action: str) -> list[ContentItem]:
        items = [text_item(f"done: {action}")]
        if frame_after_action:
            items.append(frame(f"after {action}"))
        return items

    def look_at_screen(_: dict[str, Any]) -> list[ContentItem]:
        return [text_item(f"frame captured at {time.strftime('%H:%M:%S')}"), frame("look")]

    def move(a: dict[str, Any]) -> list[ContentItem]:
        x, y, ms = float(a.get("x", 0)), float(a.get("y", 0)), int(a.get("ms", 300))
        io.move(x, y, ms)
        return after(f"move({x:+.2f}, {y:+.2f}, {ms}ms)")

    def camera(a: dict[str, Any]) -> list[ContentItem]:
        dx, dy, ms = float(a.get("dx", 0)), float(a.get("dy", 0)), int(a.get("ms", 200))
        io.camera(dx, dy, ms)
        return after(f"camera({dx:+.2f}, {dy:+.2f}, {ms}ms)")

    def press(a: dict[str, Any]) -> list[ContentItem]:
        button = Button(a["button"])
        hold_ms = int(a.get("hold_ms", 80))
        io.press(button, hold_ms)
        return after(f"press({button}, {hold_ms}ms)")

    def hold(a: dict[str, Any]) -> list[ContentItem]:
        button = Button(a["button"])
        io.hold(button)
        return after(f"hold({button})")

    def release(a: dict[str, Any]) -> list[ContentItem]:
        button = Button(a["button"])
        io.release(button)
        return after(f"release({button})")

    def wait(a: dict[str, Any]) -> list[ContentItem]:
        ms = min(int(a.get("ms", 500)), 2000)
        io.wait(ms)
        return after(f"wait({ms}ms)")

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
        "move",
        "Push the LEFT stick to (x, y) for `ms` milliseconds, then recentre. x: -1 left … +1 "
        "right. y: -1 back/down … +1 forward/up. Default ms=300.",
        {
            "type": "object",
            "properties": {"x": _AXIS, "y": _AXIS, "ms": _MS},
            "required": ["x", "y"],
            "additionalProperties": False,
        },
        move,
    )
    reg.register(
        "camera",
        "Push the RIGHT stick (camera) to (dx, dy) for `ms` milliseconds, then recentre. "
        "dx: -1 look left … +1 look right. dy: -1 look down … +1 look up. Default ms=200.",
        {
            "type": "object",
            "properties": {"dx": _AXIS, "dy": _AXIS, "ms": _MS},
            "required": ["dx", "dy"],
            "additionalProperties": False,
        },
        camera,
    )
    reg.register(
        "press",
        "Tap a controller button and release it after `hold_ms` (default 80). Xbox layout: "
        "A jump, X dash/interact, B/Y ability (context-specific), RT/LT tools, LB/RB "
        "grab/swap, START pause.",
        {
            "type": "object",
            "properties": {"button": _BUTTON, "hold_ms": _MS},
            "required": ["button"],
            "additionalProperties": False,
        },
        press,
    )
    reg.register(
        "hold",
        "Hold a button down until you call `release`. Use for grabbing, charging, sustained "
        "abilities.",
        {
            "type": "object",
            "properties": {"button": _BUTTON},
            "required": ["button"],
            "additionalProperties": False,
        },
        hold,
    )
    reg.register(
        "release",
        "Release a button you are holding.",
        {
            "type": "object",
            "properties": {"button": _BUTTON},
            "required": ["button"],
            "additionalProperties": False,
        },
        release,
    )
    reg.register(
        "wait",
        "Do nothing for up to 2000 ms (e.g. wait for your partner, a cutscene, or an animation).",
        {"type": "object", "properties": {"ms": _MS}, "additionalProperties": False},
        wait,
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

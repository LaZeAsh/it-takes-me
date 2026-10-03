"""Dynamic tools exposed to the model on the Codex thread.

`ToolRegistry` turns Python callables into `dynamicTools` specs for `thread/start` and
dispatches the `item/tool/call` requests the app-server sends back while a turn runs.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from it_takes_me.vision import ModelFrame

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
    # The one task the model has committed to via `act`; it must close it before switching.
    current_task: str | None = None
    # Called when play ends: stop anything running in the background. Each returns an optional
    # note for the model, e.g. how far a `keep_moving` run carried on.
    settle_hooks: list[Callable[[], str | None]] = field(default_factory=list)
    # Called at turn start, right before the turn's frame is captured. They must not wait for
    # a playing chunk; each returns an optional note, e.g. how much of it is left.
    progress_hooks: list[Callable[[], str | None]] = field(default_factory=list)
    # True while the newest frame the model has is a high-detail look with no action since.
    fresh_high_frame: bool = False
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
        self.fresh_high_frame = False

    def settle(self) -> str | None:
        """Stop anything running in the background; return notes for the model, if any."""
        notes = [note for hook in self.settle_hooks if (note := hook())]
        return " ".join(notes) or None

    def progress(self) -> str | None:
        """News about background play for the turn-start message, without stopping it."""
        notes = [note for hook in self.progress_hooks if (note := hook())]
        return " ".join(notes) or None

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

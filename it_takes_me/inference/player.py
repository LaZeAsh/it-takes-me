"""The play loop: feed frames, let the model act through tools, keep the human in the loop."""

from __future__ import annotations

import logging
import queue
import sys
import threading
from typing import Any

from openai_codex import LocalImageInput, TextInput
from openai_codex.api import Thread, TurnHandle
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    ErrorNotification,
    ItemCompletedNotification,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
)
from rich.console import Console

from it_takes_me.config import Settings
from it_takes_me.game.io import GameIO
from it_takes_me.inference.runtime import CodexRuntime
from it_takes_me.inference.tools import ToolRegistry
from it_takes_me.recording import RunRecorder

log = logging.getLogger(__name__)


def _strip_data_urls(value: Any) -> Any:
    """Replace inline base64 images with a short placeholder so events.jsonl stays readable.

    Frames are already saved as PNGs by the recorder; the log only needs to reference them.
    """
    if isinstance(value, dict):
        return {k: _strip_data_urls(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_data_urls(v) for v in value]
    if isinstance(value, str) and value.startswith("data:image/"):
        return f"<data url, {len(value)} chars>"
    return value


class AstraPlayer:
    def __init__(
        self,
        *,
        runtime: CodexRuntime,
        thread: Thread,
        io: GameIO,
        tools: ToolRegistry,
        recorder: RunRecorder,
        settings: Settings,
        console: Console | None = None,
    ) -> None:
        self.runtime = runtime
        self.thread = thread
        self.io = io
        self.tools = tools
        self.recorder = recorder
        self.settings = settings
        self.console = console or Console()
        self._hints: queue.Queue[str] = queue.Queue()
        self._active: TurnHandle | None = None
        self._total_tokens = 0
        self._stop = threading.Event()
        self.tools.observers.append(self._on_tool)

    # -- human input --------------------------------------------------------------------------

    def start_stdin_listener(self) -> None:
        """Lines typed on the terminal go to the model: steered into the active turn if there is
        one, otherwise prepended to the next turn. `/quit` stops after the current turn."""

        def _reader() -> None:
            for line in sys.stdin:
                text = line.strip()
                if not text:
                    continue
                if text in ("/quit", "/q"):
                    self._stop.set()
                    return
                self._deliver_hint(text)

        threading.Thread(target=_reader, daemon=True, name="stdin-hints").start()

    def _deliver_hint(self, text: str) -> None:
        handle = self._active
        if handle is not None:
            try:
                handle.steer(f"Partner says: {text}")
                self.recorder.event("steer", text=text)
                return
            except Exception as exc:  # noqa: BLE001 - turn may have just ended
                log.debug("steer failed (%s); queueing for next turn", exc)
        self._hints.put(text)

    def _drain_hints(self) -> list[str]:
        hints: list[str] = []
        while True:
            try:
                hints.append(self._hints.get_nowait())
            except queue.Empty:
                return hints

    # -- loop -----------------------------------------------------------------------------------

    def play(self) -> None:
        turn_no = 0
        while not self._stop.is_set():
            if self.settings.max_turns is not None and turn_no >= self.settings.max_turns:
                break
            turn_no += 1
            self._run_turn(turn_no)

    def _turn_text(self, turn_no: int, hints: list[str]) -> str:
        parts = [
            f"Turn {turn_no}. Here is the current frame. Continue playing as "
            f"{self.settings.character}."
        ]
        if hints:
            parts.append("Your partner says: " + " | ".join(hints))
        return " ".join(parts)

    def _run_turn(self, turn_no: int) -> None:
        png = self.io.capture()
        frame_path = self.recorder.frame(png, reason=f"turn {turn_no} start")
        hints = self._drain_hints()
        self.tools.begin_turn()
        self.recorder.event("turn_start", turn=turn_no, hints=hints)

        handle = self.thread.turn(
            [TextInput(self._turn_text(turn_no, hints)), LocalImageInput(str(frame_path.resolve()))]
        )
        self._active = handle
        self.console.rule(f"turn {turn_no}")
        try:
            for event in handle.stream():
                self._on_event(event.method, event.payload)
        finally:
            self._active = None

    # -- events ---------------------------------------------------------------------------------

    def _on_event(self, method: str, payload: Any) -> None:
        if isinstance(payload, AgentMessageDeltaNotification):
            self.console.print(payload.delta, end="", highlight=False)
        elif isinstance(payload, ItemCompletedNotification):
            item = payload.item.root if hasattr(payload.item, "root") else payload.item
            kind = type(item).__name__
            if kind == "AgentMessageThreadItem":
                self.console.print()
            elif kind == "ContextCompactionThreadItem":
                self.console.print("[dim]thread context compacted[/dim]")
            self.recorder.event(
                "item", type=kind, item=_strip_data_urls(item.model_dump(mode="json"))
            )
        elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
            total = getattr(payload.token_usage, "total", None)
            self._total_tokens = int(getattr(total, "total_tokens", self._total_tokens))
            self.recorder.event("usage", total_tokens=self._total_tokens)
        elif isinstance(payload, ErrorNotification):
            self.console.print(f"[red]error:[/red] {payload.error.message}")
            self.recorder.event(
                "error", message=payload.error.message, will_retry=payload.will_retry
            )
        elif isinstance(payload, TurnCompletedNotification):
            turn = payload.turn
            self.console.print(f"[dim]turn {turn.status.value} in {turn.duration_ms} ms[/dim]")
            self.recorder.event(
                "turn_end",
                status=turn.status.value,
                duration_ms=turn.duration_ms,
                error=turn.error.message if turn.error else None,
            )
        else:
            self.recorder.event("notification", method=method)

    def _on_tool(
        self, name: str, args: dict[str, Any], response: dict[str, Any], duration_ms: float
    ) -> None:
        ok = response.get("success", False)
        colour = "green" if ok else "red"
        self.console.print(f"  [{colour}]⚙ {name}[/{colour}] {args} ({duration_ms:.0f} ms)")
        self.recorder.event(
            "tool_call",
            tool=name,
            args=args,
            success=ok,
            duration_ms=round(duration_ms, 1),
            text=[
                c.get("text")
                for c in response.get("contentItems", [])
                if c.get("type") == "inputText"
            ],
        )

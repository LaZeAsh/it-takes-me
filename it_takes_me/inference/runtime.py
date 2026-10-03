"""Owns the Codex app-server process and the thread the model plays on.

Why the low-level client: the public `openai_codex.Codex` wrapper does not expose
`dynamicTools`, but `CodexClient.thread_start` accepts the raw wire dict and its
`approval_handler` receives *every* server-initiated JSON-RPC request, including the
`item/tool/call` requests the app-server sends when the model invokes one of our tools.
Threads started here are wrapped in the SDK's own `Thread`/`TurnHandle` so streaming, steering
and interrupting reuse the SDK code paths.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from openai_codex import LocalImageInput, TextInput
from openai_codex.api import Thread, TurnHandle
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    ErrorNotification,
    ItemCompletedNotification,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
)

from it_takes_me.inference.prompts import developer_instructions
from it_takes_me.inference.registry import ToolRegistry
from it_takes_me.inference.session import (
    ActiveTurn,
    InferenceError,
    InferenceEvent,
    ItemCompleted,
    Notification,
    TextDelta,
    TurnCompleted,
    UsageBreakdown,
    UsageUpdated,
)

log = logging.getLogger(__name__)

_DECLINE = {"decision": "decline"}


class CodexTurn(ActiveTurn):
    def __init__(self, handle: TurnHandle) -> None:
        self._handle = handle

    def steer(self, text: str) -> None:
        self._handle.steer(text)

    def stream(self) -> Iterator[InferenceEvent]:
        for event in self._handle.stream():
            payload = event.payload
            if isinstance(payload, AgentMessageDeltaNotification):
                yield TextDelta(payload.delta)
            elif isinstance(payload, ItemCompletedNotification):
                item = payload.item.root if hasattr(payload.item, "root") else payload.item
                yield ItemCompleted(type(item).__name__, item.model_dump(mode="json"))
            elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
                yield UsageUpdated(
                    last=UsageBreakdown.from_object(payload.token_usage.last),
                    cumulative=UsageBreakdown.from_object(payload.token_usage.total),
                    model_context_window=payload.token_usage.model_context_window,
                )
            elif isinstance(payload, ErrorNotification):
                yield InferenceError(payload.error.message, payload.will_retry)
            elif isinstance(payload, TurnCompletedNotification):
                turn = payload.turn
                yield TurnCompleted(
                    status=turn.status.value,
                    duration_ms=turn.duration_ms,
                    error=turn.error.message if turn.error else None,
                )
            else:
                yield Notification(event.method)


class CodexSession:
    def __init__(self, thread: Thread, model: str) -> None:
        self._thread = thread
        self.id = thread.id
        self.model = model

    def turn(self, text: str, image_path: Path) -> ActiveTurn:
        handle = self._thread.turn([TextInput(text), LocalImageInput(str(image_path.resolve()))])
        return CodexTurn(handle)


class CodexRuntime:
    def __init__(
        self,
        *,
        cwd: Path | None = None,
        tools: ToolRegistry | None = None,
        compact_threshold: int = 200_000,
        service_tier: str | None = None,
    ) -> None:
        self.tools = tools  # noqa: BLE001
        self._compact_threshold = compact_threshold
        self._service_tier = service_tier
        self._client = CodexClient(
            config=CodexConfig(cwd=str(cwd) if cwd else None),
            approval_handler=self._on_server_request,
        )
        self._started = False

    # -- lifecycle -------------------------------------------------------------------------

    def __enter__(self) -> CodexRuntime:
        self._client.start()
        self._client.initialize()
        self._started = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._started:
            self._client.close()
            self._started = False

    @property
    def client(self) -> CodexClient:
        return self._client

    # -- server -> client requests -----------------------------------------------------------

    def _on_server_request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """Answer requests the app-server makes of us.

        Runs on the SDK's stdout reader thread: while a tool handler executes, no notifications
        are delivered. Keep handlers short.
        """
        params = params or {}
        if method == "item/tool/call":
            if self.tools is None:
                return {
                    "success": False,
                    "contentItems": [{"type": "inputText", "text": "no tools"}],
                }
            return self.tools.dispatch(str(params.get("tool", "")), params.get("arguments"))
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            # The player has no business running commands or editing files.
            return _DECLINE
        if method == "item/permissions/requestApproval":
            return _DECLINE
        log.debug("unhandled server request %s", method)
        return {}

    # -- threads -----------------------------------------------------------------------------

    def _thread_payload(
        self,
        *,
        model: str,
        reasoning_effort: str,
        character: str,
        extra_instructions: list[str] | None,
        ephemeral: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            # Compact inline; a manual compaction starts a background turn that can race play.
            "config": {
                "model_reasoning_effort": reasoning_effort,
                "model_auto_compact_token_limit": self._compact_threshold,
            },
            "developerInstructions": developer_instructions(character, extra_instructions),
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "ephemeral": ephemeral,
        }
        if self._service_tier is not None:
            payload["serviceTier"] = self._service_tier
        if self.tools is not None:
            payload["dynamicTools"] = self.tools.specs()
        return payload

    def start_thread(
        self,
        *,
        model: str,
        reasoning_effort: str,
        character: str,
        extra_instructions: list[str] | None = None,
        ephemeral: bool = False,
    ) -> Thread:
        payload = self._thread_payload(
            model=model,
            reasoning_effort=reasoning_effort,
            character=character,
            extra_instructions=extra_instructions,
            ephemeral=ephemeral,
        )
        started = self._client.thread_start(payload)
        log.info("thread %s started on model %s", started.thread.id, started.model)
        return Thread(self._client, started.thread.id)

    def start_session(
        self,
        *,
        model: str,
        reasoning_effort: str,
        character: str,
        extra_instructions: list[str] | None = None,
        ephemeral: bool = False,
    ) -> CodexSession:
        payload = self._thread_payload(
            model=model,
            reasoning_effort=reasoning_effort,
            character=character,
            extra_instructions=extra_instructions,
            ephemeral=ephemeral,
        )
        started = self._client.thread_start(payload)
        log.info("thread %s started on model %s", started.thread.id, started.model)
        return CodexSession(
            Thread(self._client, started.thread.id),
            started.model or model,
        )

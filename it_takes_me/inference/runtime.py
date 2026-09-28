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
from pathlib import Path
from typing import Any

from openai_codex.api import Thread
from openai_codex.client import CodexClient, CodexConfig

from it_takes_me.inference.prompts import developer_instructions
from it_takes_me.inference.tools import ToolRegistry

log = logging.getLogger(__name__)

_DECLINE = {"decision": "decline"}


class CodexRuntime:
    def __init__(self, *, cwd: Path | None = None, tools: ToolRegistry | None = None) -> None:
        self.tools = tools
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

    def start_thread(
        self,
        *,
        model: str,
        reasoning_effort: str,
        character: str,
        extra_instructions: list[str] | None = None,
        ephemeral: bool = False,
    ) -> Thread:
        payload: dict[str, Any] = {
            "model": model,
            "config": {"model_reasoning_effort": reasoning_effort},
            "developerInstructions": developer_instructions(character, extra_instructions),
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "ephemeral": ephemeral,
        }
        if self.tools is not None:
            payload["dynamicTools"] = self.tools.specs()
        started = self._client.thread_start(payload)
        log.info("thread %s started on model %s", started.thread.id, started.model)
        return Thread(self._client, started.thread.id)

    def compact(self, thread_id: str) -> None:
        self._client.thread_compact(thread_id)

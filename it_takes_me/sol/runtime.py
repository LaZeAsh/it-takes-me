"""Owns the Codex app-server process and the thread the model plays on.

Why the low-level client: the public `openai_codex.Codex` wrapper does not expose
`dynamicTools`, but `CodexClient.thread_start` accepts the raw wire dict and its
`approval_handler` receives *every* server-initiated JSON-RPC request, including the
`item/tool/call` requests the app-server sends when the model invokes one of our tools.
Threads started here are wrapped in the SDK's own `Thread`/`TurnHandle` so streaming, steering
and interrupting reuse the SDK code paths.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import tomllib
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

from it_takes_me.sol.prompts import developer_instructions
from it_takes_me.sol.registry import ToolRegistry
from it_takes_me.sol.session import (
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

# Replaces Codex's coding-agent system prompt; the game rules are the developer instructions.
BASE_INSTRUCTIONS = (
    "You play a video game through the tools you are given. Follow the developer instructions."
)
# Codex features a game player never uses. Each one adds tools or instructions to every request,
# which the model reads (time to first token) on every call.
_UNUSED_FEATURES = (
    "apps",
    "plugins",
    "multi_agent",
    "shell_tool",
    "unified_exec",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "goals",
    "skill_search",
    "tool_suggest",
    "view_image",
    "sleep_tool",
    "in_app_browser",
    "realtime_conversation",
    "hooks",
    "memories",
    "workspace_dependencies",
)
_UNUSED_CONTEXT = ("permissions", "apps", "collaboration_mode")


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def direct_tool_catalog(home: Path, out: Path) -> Path | None:
    """Write Codex's model catalog with every model calling tools directly; None if absent.

    Codex marks current models `code_mode_only`: our tools then sit behind a JavaScript `exec`
    tool, and every call costs ~70 tokens of script to unwrap the result. The first call also
    printed its frame as base64 text (~27k tokens kept for the whole session).
    """
    try:
        cache = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("no Codex model catalog found; tools stay behind code mode")
        return None
    models = []
    for model in cache.get("models", []):
        model = {**model, "tool_mode": "direct"}
        model.pop("multi_agent_version", None)  # drops the sub-agent role instructions
        models.append(model)
    out.write_text(json.dumps({"models": models}), encoding="utf-8")
    return out


def _mcp_servers(home: Path) -> list[str]:
    try:
        with open(home / "config.toml", "rb") as f:
            return list(tomllib.load(f).get("mcp_servers", {}))
    except (OSError, tomllib.TOMLDecodeError):
        return []


def lean_overrides(home: Path, catalog: Path | None) -> tuple[str, ...]:
    """`-c` overrides that strip a play session down to our own tools and instructions."""
    overrides = [f"features.{name}=false" for name in _UNUSED_FEATURES]
    overrides += [f"include_{name}_instructions=false" for name in _UNUSED_CONTEXT]
    overrides.append("include_environment_context=false")
    # The user's own MCP servers (e.g. a browser REPL) would add their tools too.
    overrides += [f"mcp_servers.{name}.enabled=false" for name in _mcp_servers(home)]
    if catalog is not None:
        overrides.append(f"model_catalog_json={json.dumps(catalog.as_posix())}")
    return tuple(overrides)


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
        home = _codex_home()
        catalog = direct_tool_catalog(home, Path(tempfile.gettempdir()) / "it-takes-me-models.json")
        self._client = CodexClient(
            config=CodexConfig(
                cwd=str(cwd) if cwd else None,
                config_overrides=lean_overrides(home, catalog),
            ),
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
        pipelining: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            # Compact inline; a manual compaction starts a background turn that can race play.
            "config": {
                "model_reasoning_effort": reasoning_effort,
                "model_auto_compact_token_limit": self._compact_threshold,
            },
            "baseInstructions": BASE_INSTRUCTIONS,
            "developerInstructions": developer_instructions(
                character, extra_instructions, pipelining=pipelining
            ),
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
        pipelining: bool = False,
    ) -> Thread:
        payload = self._thread_payload(
            model=model,
            reasoning_effort=reasoning_effort,
            character=character,
            extra_instructions=extra_instructions,
            ephemeral=ephemeral,
            pipelining=pipelining,
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
        pipelining: bool = False,
    ) -> CodexSession:
        payload = self._thread_payload(
            model=model,
            reasoning_effort=reasoning_effort,
            character=character,
            extra_instructions=extra_instructions,
            ephemeral=ephemeral,
            pipelining=pipelining,
        )
        started = self._client.thread_start(payload)
        log.info("thread %s started on model %s", started.thread.id, started.model)
        return CodexSession(
            Thread(self._client, started.thread.id),
            started.model or model,
        )

"""Claude runtime: drives Claude Code through the Claude Agent SDK.

Claude Code signs in with your Claude subscription (`claude login`), so play is billed to the
subscription, not an API key. The game tools are served from an in-process MCP server named
`game`; Claude Code's own tools, settings, CLAUDE.md files, plugins and MCP servers are all
switched off, and our instructions replace its system prompt.

The SDK is async while the player loop is not: a background thread runs the event loop, and
each turn's messages are handed to the player through a queue. Tool calls run the (blocking)
registry on a worker thread so the SDK keeps reading while a chunk plays.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import queue
import threading
import time
from collections.abc import AsyncIterator, Coroutine, Iterator
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, TypeVar

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SdkMcpTool,
    SystemMessage,
    TextBlock,
    create_sdk_mcp_server,
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

OPUS_MODEL = "claude-opus-5-5"
SERVER = "game"
_EFFORTS = ("low", "medium", "high", "xhigh", "max")

BASE_INSTRUCTIONS = (
    "You play a video game through the tools you are given. Your tools are named "
    f"`mcp__{SERVER}__<name>`; the instructions below call them by <name> alone (`act` is "
    f"`mcp__{SERVER}__act`)."
)

T = TypeVar("T")


def _usage(value: dict[str, Any] | None) -> UsageBreakdown:
    """One API call's usage. `input_tokens` counts the whole prompt, cached parts included."""
    value = value or {}
    fresh = int(value.get("input_tokens") or 0)
    read = int(value.get("cache_read_input_tokens") or 0)
    write = int(value.get("cache_creation_input_tokens") or 0)
    output = int(value.get("output_tokens") or 0)
    return UsageBreakdown(
        input_tokens=fresh + read + write,
        cached_input_tokens=read,
        cache_write_input_tokens=write,
        output_tokens=output,
        total_tokens=fresh + read + write + output,
    )


def _mcp_content(item: dict[str, Any]) -> dict[str, Any] | None:
    """A registry content item as an MCP content block."""
    if item.get("type") == "inputText":
        return {"type": "text", "text": str(item.get("text", ""))}
    if item.get("type") == "inputImage":
        header, _, data = str(item.get("imageUrl", "")).partition(",")
        media_type = header.removeprefix("data:").split(";")[0] or "image/jpeg"
        return {"type": "image", "data": data, "mimeType": media_type}
    return None


def _image_block(path: Path) -> dict[str, Any]:
    media_type = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def _as_dict(value: Any) -> dict[str, Any]:
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else {}


class ClaudeTurn(ActiveTurn):
    def __init__(self, session: ClaudeSession, text: str, image_path: Path) -> None:
        self._session = session
        self._events: queue.Queue[InferenceEvent] = queue.Queue()
        session.runtime.submit(session.run_turn(text, image_path, self._events.put))

    def steer(self, text: str) -> None:
        self._session.runtime.queue_steer(text)

    def stream(self) -> Iterator[InferenceEvent]:
        while True:
            event = self._events.get()
            yield event
            if isinstance(event, TurnCompleted):
                return


class ClaudeSession:
    def __init__(self, runtime: ClaudeRuntime, client: ClaudeSDKClient, model: str) -> None:
        self.runtime = runtime
        self.client = client
        self.model = model
        self.id = "claude:new"
        self.cumulative_usage = UsageBreakdown()
        self._usage_seen: set[str] = set()

    def turn(self, text: str, image_path: Path) -> ActiveTurn:
        return ClaudeTurn(self, text, image_path)

    async def run_turn(self, text: str, image_path: Path, emit: Any) -> None:
        started = time.monotonic()
        try:
            await self._run(text, image_path, emit)
        except Exception as exc:  # noqa: BLE001 - surface provider errors in the common path
            emit(InferenceError(str(exc), will_retry=False))
            emit(
                TurnCompleted(
                    status="failed",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    error=str(exc),
                )
            )

    async def _run(self, text: str, image_path: Path, emit: Any) -> None:
        # Partner lines that arrived after the last tool call of the previous turn.
        if late := self.runtime.take_steers():
            text = " ".join([text, *late])
        message = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}, _image_block(image_path)],
            },
            "parent_tool_use_id": None,
        }

        async def prompt() -> AsyncIterator[dict[str, Any]]:
            yield message

        await self.client.query(prompt())
        async for msg in self.client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        emit(TextDelta(block.text.rstrip() + "\n"))
                    emit(ItemCompleted(type(block).__name__, _as_dict(block)))
                self._on_usage(msg, emit)
                if msg.error:
                    emit(InferenceError(str(msg.error), will_retry=False))
            elif isinstance(msg, SystemMessage):
                if msg.subtype == "init":
                    self.id = str(msg.data.get("session_id") or self.id)
                elif msg.subtype == "compact_boundary":
                    emit(ItemCompleted("ContextCompactionThreadItem", msg.data))
                    continue
                emit(Notification(f"system/{msg.subtype}"))
            elif isinstance(msg, ResultMessage):
                self.id = msg.session_id or self.id
                error = None
                if msg.is_error:
                    error = "; ".join(msg.errors or []) or msg.result or msg.subtype
                emit(
                    TurnCompleted(
                        status="failed" if msg.is_error else "completed",
                        duration_ms=msg.duration_ms,
                        error=error,
                    )
                )
                return

    def _on_usage(self, msg: AssistantMessage, emit: Any) -> None:
        # One API response arrives as several messages (a block each) sharing its usage.
        key = msg.message_id or msg.uuid or ""
        if msg.usage is None or key in self._usage_seen:
            return
        self._usage_seen.add(key)
        last = _usage(msg.usage)
        self.cumulative_usage = self.cumulative_usage + last
        emit(UsageUpdated(last=last, cumulative=self.cumulative_usage))


class ClaudeRuntime:
    def __init__(self, *, tools: ToolRegistry, cwd: Path | None = None) -> None:
        self.tools = tools
        self._cwd = cwd
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="claude-sdk"
        )
        self._client: ClaudeSDKClient | None = None
        self._steers: list[str] = []
        self._steer_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------------------------

    def __enter__(self) -> ClaudeRuntime:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        if self._client is not None:
            try:
                self.call(self._client.disconnect(), timeout=10)
            except Exception as exc:  # noqa: BLE001 - best effort on the way out
                log.debug("disconnect failed: %s", exc)
            self._client = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def submit(self, coro: Coroutine[Any, Any, Any]) -> None:
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    # -- partner lines mid-turn ----------------------------------------------------------------

    def queue_steer(self, text: str) -> None:
        with self._steer_lock:
            self._steers.append(text)

    def take_steers(self) -> list[str]:
        with self._steer_lock:
            values, self._steers = self._steers, []
        return values

    # -- tools ---------------------------------------------------------------------------------

    def _mcp_tools(self) -> list[SdkMcpTool[Any]]:
        def handler(name: str) -> Any:
            async def handle(args: dict[str, Any]) -> dict[str, Any]:
                response = await asyncio.to_thread(self.tools.dispatch, name, args)
                content = [
                    block
                    for item in response.get("contentItems", [])
                    if (block := _mcp_content(item)) is not None
                ]
                # Lines typed by the partner reach the model with the next tool result.
                content += [{"type": "text", "text": s} for s in self.take_steers()]
                if not content:
                    content = [{"type": "text", "text": "done"}]
                return {"content": content, "is_error": not response.get("success", False)}

            return handle

        return [
            SdkMcpTool(spec.name, spec.description, spec.input_schema, handler(spec.name))
            for spec in self.tools.tools.values()
        ]

    def options(
        self, *, model: str, reasoning_effort: str, instructions: str
    ) -> ClaudeAgentOptions:
        names = [f"mcp__{SERVER}__{name}" for name in self.tools.tools]
        return ClaudeAgentOptions(
            model=model,
            effort=reasoning_effort if reasoning_effort in _EFFORTS else None,  # type: ignore[arg-type]
            system_prompt=f"{BASE_INSTRUCTIONS}\n\n{instructions}",
            mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=self._mcp_tools())},
            strict_mcp_config=True,
            tools=[],  # none of Claude Code's own tools
            allowed_tools=names,
            permission_mode="dontAsk",
            setting_sources=[],  # no settings, hooks, plugins or CLAUDE.md files
            cwd=self._cwd,
            env={
                # An API key would take precedence over the subscription login.
                "ANTHROPIC_API_KEY": "",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "CLAUDE_AGENT_SDK_CLIENT_APP": "it-takes-me/0.1",
            },
            stderr=lambda line: log.debug("claude: %s", line),
        )

    # -- sessions ------------------------------------------------------------------------------

    def start_session(
        self,
        *,
        model: str,
        reasoning_effort: str,
        character: str,
        extra_instructions: list[str] | None = None,
        ephemeral: bool = False,
        pipelining: bool = False,
    ) -> ClaudeSession:
        del ephemeral
        instructions = developer_instructions(character, extra_instructions, pipelining=pipelining)
        options = self.options(
            model=model, reasoning_effort=reasoning_effort, instructions=instructions
        )
        client = ClaudeSDKClient(options=options)
        self.call(client.connect(), timeout=120)
        self._client = client
        return ClaudeSession(self, client, model)

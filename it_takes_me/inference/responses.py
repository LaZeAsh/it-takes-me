"""OpenAI Responses API adapter for the provider-neutral gameplay session."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from openai import OpenAI

from it_takes_me.inference.prompts import developer_instructions
from it_takes_me.inference.session import (
    ActiveTurn,
    InferenceError,
    InferenceEvent,
    ItemCompleted,
    TextDelta,
    TurnCompleted,
    UsageBreakdown,
    UsageUpdated,
)
from it_takes_me.inference.tools import ToolRegistry


def _data_url(path: Path) -> str:
    media_type = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return f"data:{media_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _responses_usage(value: Any) -> UsageBreakdown:
    if value is None:
        return UsageBreakdown()
    input_details = getattr(value, "input_tokens_details", None)
    output_details = getattr(value, "output_tokens_details", None)
    return UsageBreakdown(
        input_tokens=int(getattr(value, "input_tokens", 0) or 0),
        cached_input_tokens=int(getattr(input_details, "cached_tokens", 0) or 0),
        cache_write_input_tokens=int(getattr(input_details, "cache_write_tokens", 0) or 0),
        output_tokens=int(getattr(value, "output_tokens", 0) or 0),
        reasoning_output_tokens=int(getattr(output_details, "reasoning_tokens", 0) or 0),
        total_tokens=int(getattr(value, "total_tokens", 0) or 0),
    )


def _tool_output(response: dict[str, Any]) -> str | list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in response.get("contentItems", []):
        if item.get("type") == "inputText":
            items.append({"type": "input_text", "text": str(item.get("text", ""))})
        elif item.get("type") == "inputImage":
            items.append(
                {
                    "type": "input_image",
                    "image_url": str(item.get("imageUrl", "")),
                    "detail": "original",
                }
            )
    if not items:
        return "success" if response.get("success") else "tool failed"
    return items


class ResponsesTurn(ActiveTurn):
    def __init__(self, session: ResponsesSession, text: str, image_path: Path) -> None:
        self._session = session
        self._text = text
        self._image_path = image_path

    def steer(self, text: str) -> None:
        self._session.queue_steer(text)

    def stream(self) -> Iterator[InferenceEvent]:
        started = time.monotonic()
        try:
            yield from self._run()
        except Exception as exc:  # noqa: BLE001 - surface provider errors in the common event path
            yield InferenceError(str(exc), will_retry=False)
            yield TurnCompleted(
                status="failed",
                duration_ms=round((time.monotonic() - started) * 1000),
                error=str(exc),
            )
            return
        yield TurnCompleted(
            status="completed",
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    def _run(self) -> Iterator[InferenceEvent]:
        inputs: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": self._text},
                    {
                        "type": "input_image",
                        "image_url": _data_url(self._image_path),
                        "detail": "original",
                    },
                ],
            }
        ]
        while True:
            inputs.extend(self._session.take_steers())
            response = self._session.create(inputs)
            usage = _responses_usage(response.usage)
            self._session.cumulative_usage = self._session.cumulative_usage + usage
            yield UsageUpdated(last=usage, cumulative=self._session.cumulative_usage)

            calls: list[Any] = []
            for item in response.output:
                data = item.model_dump(mode="json")
                yield ItemCompleted(type(item).__name__, data)
                if item.type == "function_call":
                    calls.append(item)

            if not calls:
                if response.output_text:
                    yield TextDelta(response.output_text + "\n")
                return

            inputs = []
            for call in calls:
                try:
                    arguments = json.loads(call.arguments)
                except json.JSONDecodeError:
                    arguments = {}
                result = self._session.tools.dispatch(call.name, arguments)
                inputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": _tool_output(result),
                    }
                )


class ResponsesSession:
    def __init__(
        self,
        *,
        client: Any,
        tools: ToolRegistry,
        model: str,
        reasoning_effort: str,
        character: str,
        extra_instructions: list[str] | None,
        chapter: str | None,
        compact_threshold: int,
        max_output_tokens: int,
        store: bool,
    ) -> None:
        self.client = client
        self.tools = tools
        self.model = model
        self.id = "responses:new"
        self.reasoning_effort = reasoning_effort
        self.instructions = developer_instructions(character, extra_instructions, chapter=chapter)
        self.compact_threshold = compact_threshold
        self.max_output_tokens = max_output_tokens
        self.store = store
        self.previous_response_id: str | None = None
        self.cumulative_usage = UsageBreakdown()
        self._steers: list[str] = []
        self._steer_lock = threading.Lock()

    def turn(self, text: str, image_path: Path) -> ActiveTurn:
        return ResponsesTurn(self, text, image_path)

    def queue_steer(self, text: str) -> None:
        with self._steer_lock:
            self._steers.append(text)

    def take_steers(self) -> list[dict[str, Any]]:
        with self._steer_lock:
            values, self._steers = self._steers, []
        return [{"role": "user", "content": value} for value in values]

    def create(self, inputs: list[dict[str, Any]]) -> Any:
        response = self.client.responses.create(
            model=self.model,
            input=inputs,
            instructions=self.instructions,
            tools=self.tools.responses_specs(),
            previous_response_id=self.previous_response_id,
            reasoning={"effort": self.reasoning_effort},
            parallel_tool_calls=False,
            max_output_tokens=self.max_output_tokens,
            context_management=[
                {"type": "compaction", "compact_threshold": self.compact_threshold}
            ],
            store=self.store,
        )
        self.previous_response_id = response.id
        self.id = response.id
        return response


class ResponsesRuntime:
    def __init__(
        self,
        *,
        tools: ToolRegistry,
        compact_threshold: int,
        max_output_tokens: int = 4000,
        store: bool = True,
        client: Any | None = None,
    ) -> None:
        if client is None and not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required when RUNTIME is 'responses'")
        self.tools = tools
        self.compact_threshold = compact_threshold
        self.max_output_tokens = max_output_tokens
        self.store = store
        self.client = client or OpenAI()

    def __enter__(self) -> ResponsesRuntime:
        return self

    def __exit__(self, *exc: object) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            close()

    def start_session(
        self,
        *,
        model: str,
        reasoning_effort: str,
        character: str,
        extra_instructions: list[str] | None = None,
        chapter: str | None = None,
        ephemeral: bool = False,
    ) -> ResponsesSession:
        del ephemeral
        return ResponsesSession(
            client=self.client,
            tools=self.tools,
            model=model,
            reasoning_effort=reasoning_effort,
            character=character,
            extra_instructions=extra_instructions,
            chapter=chapter,
            compact_threshold=self.compact_threshold,
            max_output_tokens=self.max_output_tokens,
            store=self.store,
        )

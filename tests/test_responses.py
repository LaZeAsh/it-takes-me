from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from it_takes_me.sol.registry import ToolRegistry, text_item
from it_takes_me.sol.responses import ResponsesRuntime, _tool_output
from it_takes_me.sol.session import TextDelta, TurnCompleted


class FakeItem:
    def __init__(self, item_type: str, **values: Any) -> None:
        self.type = item_type
        self.__dict__.update(values)

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        del mode
        return dict(self.__dict__)


def usage(input_tokens: int, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=3, cache_write_tokens=4),
        output_tokens=output_tokens,
        output_tokens_details=SimpleNamespace(reasoning_tokens=2),
        total_tokens=input_tokens + output_tokens,
    )


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results = [
            SimpleNamespace(
                id="resp_1",
                usage=usage(100, 10),
                output=[
                    FakeItem(
                        "function_call",
                        name="ping",
                        arguments='{"value": 7}',
                        call_id="call_1",
                    )
                ],
                output_text="",
            ),
            SimpleNamespace(
                id="resp_2",
                usage=usage(125, 5),
                output=[FakeItem("message", role="assistant")],
                output_text="done",
            ),
        ]

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.results.pop(0)


class FakeClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()


class ResponsesRuntimeTests(unittest.TestCase):
    def test_tool_result_images_map_to_supported_content(self) -> None:
        output = _tool_output(
            {
                "success": True,
                "contentItems": [
                    {"type": "inputText", "text": "ready"},
                    {"type": "inputImage", "imageUrl": "data:image/jpeg;base64,abc"},
                ],
            }
        )

        self.assertEqual(output[0], {"type": "input_text", "text": "ready"})
        self.assertEqual(output[1]["type"], "input_image")
        self.assertEqual(output[1]["detail"], "original")

    def test_multi_call_turn_reuses_response_id_and_dispatches_tools(self) -> None:
        client = FakeClient()
        tools = ToolRegistry()
        tools.register(
            "ping",
            "Ping a test value.",
            {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
            lambda args: [text_item(f"pong {args['value']}")],
        )
        runtime = ResponsesRuntime(
            tools=tools,
            compact_threshold=200_000,
            client=client,
        )
        session = runtime.start_session(
            model="gpt-6-astra",
            reasoning_effort="low",
            character="May",
        )
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "frame.jpg"
            image.write_bytes(b"test image")
            events = list(session.turn("play", image).stream())

        self.assertTrue(any(isinstance(event, TextDelta) for event in events))
        self.assertEqual(events[-1], TurnCompleted("completed", events[-1].duration_ms))
        self.assertEqual(client.responses.calls[1]["previous_response_id"], "resp_1")
        tool_result = client.responses.calls[1]["input"][0]
        self.assertEqual(tool_result["call_id"], "call_1")
        self.assertEqual(tool_result["output"][0]["text"], "pong 7")
        self.assertEqual(
            client.responses.calls[0]["context_management"],
            [{"type": "compaction", "compact_threshold": 200_000}],
        )


if __name__ == "__main__":
    unittest.main()

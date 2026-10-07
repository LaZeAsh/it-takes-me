from __future__ import annotations

import asyncio
import unittest

from it_takes_me.opus.runtime import ClaudeRuntime, _mcp_content, _usage
from it_takes_me.sol.registry import ToolRegistry, text_item


class ClaudeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = ToolRegistry()
        self.tools.register(
            "ping", "Ping.", {"type": "object", "properties": {}}, lambda a: [text_item("pong")]
        )
        self.runtime = ClaudeRuntime(tools=self.tools)

    def test_only_the_game_tools_are_exposed_and_settings_are_ignored(self) -> None:
        options = self.runtime.options(model="m", reasoning_effort="low", instructions="play")
        self.assertEqual(options.tools, [])
        self.assertEqual(options.allowed_tools, ["mcp__game__ping"])
        self.assertEqual(options.setting_sources, [])
        self.assertTrue(options.strict_mcp_config)
        self.assertEqual(options.env["ANTHROPIC_API_KEY"], "")  # bill the subscription
        self.assertEqual(options.effort, "low")
        self.assertIsNone(
            self.runtime.options(model="m", reasoning_effort="none", instructions="").effort
        )

    def test_a_partner_line_rides_along_with_the_next_tool_result(self) -> None:
        (ping,) = self.runtime._mcp_tools()
        self.runtime.queue_steer("Partner says: go left")
        result = asyncio.run(ping.handler({}))
        self.assertEqual(
            result,
            {
                "content": [
                    {"type": "text", "text": "pong"},
                    {"type": "text", "text": "Partner says: go left"},
                ],
                "is_error": False,
            },
        )
        self.assertEqual(self.runtime.take_steers(), [])

    def test_content_and_usage_conversion(self) -> None:
        self.assertEqual(
            _mcp_content({"type": "inputImage", "imageUrl": "data:image/jpeg;base64,QUJD"}),
            {"type": "image", "data": "QUJD", "mimeType": "image/jpeg"},
        )
        usage = _usage(
            {
                "input_tokens": 5,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "output_tokens": 7,
            }
        )
        self.assertEqual((usage.input_tokens, usage.cached_input_tokens), (125, 100))
        self.assertEqual((usage.cache_write_input_tokens, usage.total_tokens), (20, 132))


if __name__ == "__main__":
    unittest.main()

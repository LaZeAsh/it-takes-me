from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from it_takes_me.sol.config import Settings
from it_takes_me.sol.player import GamePlayer
from it_takes_me.sol.registry import ToolRegistry
from it_takes_me.sol.runtime import CodexRuntime
from it_takes_me.sol.session import UsageBreakdown, UsageUpdated
from it_takes_me.vision import ModelFrameEncoder
from rich.console import Console


class AutomaticCompactionTests(unittest.TestCase):
    def test_both_codex_entry_points_configure_inline_compaction(self) -> None:
        client = Mock(spec=["thread_start"])
        client.thread_start.return_value = SimpleNamespace(
            thread=SimpleNamespace(id="test-thread"), model="gpt-6.1-sol"
        )
        with patch("it_takes_me.sol.runtime.CodexClient", return_value=client):
            runtime = CodexRuntime(compact_threshold=123_000)
        for start in (runtime.start_thread, runtime.start_session):
            with self.subTest(entry_point=start.__name__):
                start(model="gpt-6.1-sol", reasoning_effort="low", character="May")
                payload = client.thread_start.call_args.args[0]
                self.assertEqual(payload["config"]["model_auto_compact_token_limit"], 123_000)
                self.assertEqual(payload["config"]["model_reasoning_effort"], "low")

    def test_player_continues_after_threshold_without_manual_compaction(self) -> None:
        settings = Settings(max_turns=2, compact_after_input_tokens=100)
        player = GamePlayer(
            session=Mock(spec=["turn", "id", "model"]),
            io=Mock(),
            tools=ToolRegistry(),
            recorder=Mock(),
            settings=settings,
            frame_encoder=ModelFrameEncoder("left"),
            console=Console(file=io.StringIO()),
        )
        usage = UsageBreakdown(input_tokens=200, total_tokens=200)
        with patch.object(
            player, "_run_turn", side_effect=lambda _: player._on_event(UsageUpdated(usage, usage))
        ) as run_turn:
            player.play()
        self.assertEqual(run_turn.call_count, 2)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from it_takes_me.analysis import summarize_run
from it_takes_me.game.replay import ReplayGameIO
from PIL import Image


class ReplayAndAnalysisTests(unittest.TestCase):
    def test_replay_advances_then_holds_last_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            frames = run / "frames"
            frames.mkdir()
            Image.new("RGB", (4, 2), "red").save(frames / "00001.png")
            Image.new("RGB", (4, 2), "blue").save(frames / "00002.png")
            game = ReplayGameIO(run)

            self.assertNotEqual(game.capture(), game.capture())
            self.assertEqual(game.capture(), game.capture())

    def test_summary_calculates_decision_and_token_metrics(self) -> None:
        events = [
            {"t": 0.0, "kind": "session", "runtime": "responses", "model": "test"},
            {"t": 1.0, "kind": "observation"},
            {"t": 3.5, "kind": "tool_call", "tool": "act"},
            {"t": 4.0, "kind": "turn_end", "duration_ms": 3000},
            {
                "t": 4.1,
                "kind": "usage",
                "cumulative": {"total_tokens": 1200},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "events.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
            )
            summary = summarize_run(run)

        self.assertEqual(summary.median_decision_seconds, 2.5)
        self.assertEqual(summary.tokens_per_act, 1200)
        self.assertEqual(summary.runtime, "responses")

    def test_moving_share_counts_chunks_and_carries(self) -> None:
        events = [
            {"t": 0.0, "kind": "session", "runtime": "codex", "model": "test"},
            {"t": 2.0, "kind": "tool_call", "tool": "act", "duration_ms": 1000},
            {"t": 5.0, "kind": "chunk", "elapsed_ms": 4000, "carry_ms": 500},
            {"t": 11.0, "kind": "chunk", "elapsed_ms": 3500, "carry_ms": 0},
        ]
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "events.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
            )
            summary = summarize_run(run)

        self.assertEqual(summary.moving_share, 0.8)


if __name__ == "__main__":
    unittest.main()

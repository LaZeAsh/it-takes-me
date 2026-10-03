from __future__ import annotations

import io
import threading
import unittest
from unittest.mock import patch

from it_takes_me.game.chunks import (
    Job,
    PadThread,
    compile_chunk,
    expand_repeats,
    release_point,
)
from it_takes_me.game.io import NEUTRAL, PadState
from it_takes_me.inference.tools import LEAD_MAX_MS, LEAD_MIN_MS, LeadTimer, build_game_tools
from PIL import Image
from tests.test_chunks import Clock, FakeGame

FORWARD = PadState(left=(0.0, 1.0))


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 36)).save(buf, "PNG")
    return buf.getvalue()


def texts(response: dict) -> list[str]:
    return [item["text"] for item in response["contentItems"] if item["type"] == "inputText"]


def moving_view(size: tuple[int, int], n: int) -> Image.Image:
    # A smooth triangle wave: always changing, never jumping (a jump would read as a scene cut).
    return Image.new("L", size, abs(n * 3 % 200 - 100))


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.io = FakeGame(self.clock)
        frame = png()
        self.io.capture = lambda: frame
        self.io.snapshot_fn = moving_view
        self.enterContext(patch("it_takes_me.game.chunks.time.monotonic", self.clock.monotonic))
        self.enterContext(patch("it_takes_me.game.chunks.time.sleep", self.clock.sleep))

    def act(self, tools, steps: list[dict], **fields: object) -> dict:
        return tools.dispatch("act", {"task": "t", "intent": "go", "steps": steps, **fields})

    def test_release_point_only_for_chunks_at_least_twice_the_lead(self) -> None:
        self.assertEqual(release_point(9000, 3500), 5500)
        self.assertEqual(release_point(7000, 3500), 3500)
        self.assertEqual(release_point(5000, 1000), 4000)
        self.assertIsNone(release_point(6900, 3500))
        self.assertIsNone(release_point(1500, 3500))
        self.assertIsNone(release_point(200, 1000))

    def test_early_act_returns_a_mid_chunk_frame_and_says_what_is_left(self) -> None:
        tools = build_game_tools(self.io)
        run = [{"skill": "run", "dir": "forward", "ms": 8500}, {"skill": "jump", "dir": "forward"}]
        response = self.act(tools, run)
        self.assertTrue(response["success"])
        summary = texts(response)[0]
        self.assertIn("playing: this frame is 5500 ms into the 9000 ms chunk", summary)
        self.assertIn("step 0 (run) onward is still playing, about 3500 ms more", summary)
        self.assertIn("frame at 5500 ms", texts(response))

    def test_short_chunk_returns_its_end_frame(self) -> None:
        tools = build_game_tools(self.io)
        response = self.act(tools, [{"skill": "run", "dir": "forward", "ms": 2500}])
        self.assertIn("done: 1 steps, 2500 ms (go)", texts(response))
        self.assertIn("end of chunk", texts(response))

    def test_next_chunk_reports_how_the_previous_one_ended(self) -> None:
        tools = build_game_tools(self.io)
        self.act(tools, [{"skill": "run", "dir": "forward", "ms": 9000}])
        response = self.act(tools, [{"skill": "run", "dir": "forward", "ms": 300}])
        self.assertIn("Your previous chunk finished all its steps (go).", texts(response)[0])
        self.assertIn("done: 1 steps, 300 ms (go)", texts(response))
        self.assertIn("end of chunk", texts(response))
        self.assertEqual(self.io.inputs[-1][1], NEUTRAL)

    def test_chunk_planned_before_an_early_stop_is_not_played(self) -> None:
        # The view freezes about 7 s in: after the frame at 5500 ms went back to the model.
        self.io.snapshot_fn = lambda size, n: Image.new("L", size, min(n, 70) * 3)
        tools = build_game_tools(self.io)
        first = self.act(tools, [{"skill": "run", "dir": "forward", "ms": 9000}])
        self.assertIn("playing:", texts(first)[0])
        threading.Event().wait(0.2)  # the pad thread finishes the chunk on the fake clock
        pressed = len(self.io.inputs)

        second = self.act(tools, [{"skill": "jump", "dir": "forward"}])
        notes = texts(second)
        self.assertTrue(second["success"])
        self.assertIn("Your previous chunk stopped early: step 0 (run) ended on `stuck`", notes[0])
        self.assertIn("NOT PLAYED", notes[1])
        self.assertIn("current frame", notes)
        self.assertEqual(len(self.io.inputs), pressed)

        third = self.act(tools, [{"skill": "run", "dir": "forward", "ms": 100}])
        self.assertIn("done: 1 steps, 100 ms (go)", texts(third))

    def test_pad_thread_cancels_queued_jobs_planned_before_a_stop(self) -> None:
        self.io.snapshot_fn = lambda size, n: Image.new("L", size, 0)
        pad = PadThread(self.io, autostart=False)
        stuck = pad.submit(Job(compile_chunk([{"skill": "run", "dir": "forward", "ms": 5000}])))
        planned = pad.submit(Job(compile_chunk([{"skill": "jump", "dir": "forward"}])))
        pad.start()
        self.assertTrue(planned.over.wait(2))
        self.assertEqual(stuck.status, "stopped")
        self.assertEqual(planned.status, "cancelled")
        self.assertEqual(pad.epoch, 1)
        self.assertEqual(self.io.inputs[-1][1], NEUTRAL)
        replanned = pad.submit(
            Job(compile_chunk([{"skill": "look", "look": "left", "ms": 100}]), epoch=1)
        )
        self.assertTrue(replanned.over.wait(2))
        self.assertEqual(replanned.status, "done")
        pad.stop()

    def test_queued_job_starts_the_moment_the_previous_ends(self) -> None:
        pad = PadThread(self.io, autostart=False)
        run = compile_chunk([{"skill": "run", "dir": "forward", "ms": 300}])
        first = pad.submit(Job(run, capture=False))
        second = pad.submit(Job(run, capture=False))
        pad.start()
        self.assertTrue(second.over.wait(2))
        self.assertEqual(first.status, "done")
        self.assertEqual(second.idle_ms, 0)
        self.assertEqual(self.io.inputs, [(0, FORWARD), (300, FORWARD), (600, NEUTRAL)])
        pad.stop()

    def test_turn_start_reports_a_playing_chunk_without_waiting(self) -> None:
        gate = threading.Event()

        def gated(size: tuple[int, int], n: int) -> Image.Image:
            if self.clock.monotonic() > 5.6:
                gate.wait(5)
            return moving_view(size, n)

        self.io.snapshot_fn = gated
        tools = build_game_tools(self.io)
        self.act(tools, [{"skill": "run", "dir": "forward", "ms": 9000}])
        note = tools.progress()
        self.assertIsNotNone(note)
        self.assertIn("Your last chunk is still playing (go): about", note)
        self.assertNotIn(NEUTRAL, [state for _, state in self.io.inputs])
        gate.set()
        tools.settle()
        self.assertEqual(self.io.inputs[-1][1], NEUTRAL)

    def test_settle_halts_a_playing_chunk(self) -> None:
        gate = threading.Event()

        def gated(size: tuple[int, int], n: int) -> Image.Image:
            if n > 20:
                gate.set()
            return moving_view(size, n)

        self.io.snapshot_fn = gated
        tools = build_game_tools(self.io, frame_after_action=False)
        done = threading.Thread(
            target=self.act, args=(tools, [{"skill": "run", "dir": "forward", "ms": 9000}])
        )
        done.start()
        self.assertTrue(gate.wait(2))
        tools.settle()
        done.join(2)
        self.assertEqual(self.io.inputs[-1][1], NEUTRAL)
        self.assertLess(self.io.inputs[-1][0], 9000)

    def test_observe_end_is_refused_before_anything_is_pressed(self) -> None:
        tools = build_game_tools(self.io)
        schema = tools.tools["act"].input_schema["properties"]["observe"]
        self.assertEqual(schema["enum"], ["keyframes"])
        response = self.act(tools, [{"skill": "run", "dir": "forward", "ms": 300}], observe="end")
        self.assertFalse(response["success"])
        self.assertIn("end frame", texts(response)[0])
        self.assertEqual(self.io.inputs, [])
        run = [{"skill": "run", "dir": "forward", "ms": 9000}]
        keyframes = self.act(tools, run, observe="keyframes")
        self.assertIn("done: 1 steps, 9000 ms (go)", texts(keyframes))
        self.assertEqual(texts(keyframes).count("end of chunk"), 1)

    def test_lead_time_follows_the_measured_gap_within_bounds(self) -> None:
        lead = LeadTimer()
        lead.returned()
        self.clock.sleep(1.0)
        lead.arrived()
        self.assertAlmostEqual(lead.ms, 0.7 * 3500 + 0.3 * 1000)
        for gap in (0.0, 0.0, 0.0, 0.0, 0.0, 0.0):
            lead.returned()
            self.clock.sleep(gap)
            lead.arrived()
        self.assertEqual(lead.ms, LEAD_MIN_MS)
        for _ in range(20):
            lead.returned()
            self.clock.sleep(30)
            lead.arrived()
        self.assertEqual(lead.ms, LEAD_MAX_MS)
        lead.returned()
        lead.interrupt()
        self.clock.sleep(1)
        lead.arrived()
        self.assertEqual(lead.ms, LEAD_MAX_MS)

    def test_repeat_expands_in_order_and_maps_segments_to_its_step(self) -> None:
        hop = [
            {"skill": "jump", "dir": "left", "ms": 450},
            {"skill": "jump", "dir": "right", "ms": 450},
        ]
        steps = [{"skill": "repeat", "times": 3, "steps": hop}, {"skill": "run", "dir": "right"}]
        flat = expand_repeats(steps)
        self.assertEqual(len(flat), 7)
        self.assertEqual(flat[2][1], "0.0 (repeat 2/3)")
        segments = compile_chunk(steps)
        self.assertEqual(sum(s.ms for s in segments), 6 * 450 + 500)
        self.assertEqual({s.step for s in segments}, {0, 1})

    def test_invalid_repeat_is_rejected(self) -> None:
        jump = {"skill": "jump"}
        for step in (
            {"skill": "repeat", "times": 1, "steps": [jump]},
            {"skill": "repeat", "times": 21, "steps": [jump]},
            {"skill": "repeat", "times": True, "steps": [jump]},
            {"skill": "repeat", "times": 2, "steps": []},
            {"skill": "repeat", "times": 2, "steps": [{"skill": "repeat"}]},
            {"skill": "repeat", "times": 2, "steps": [jump], "ms": 100},
            {"skill": "jump", "times": 2},
        ):
            with self.subTest(step=step), self.assertRaises(ValueError):
                compile_chunk([step])
        with self.assertRaisesRegex(ValueError, "10000 ms"):
            compile_chunk(
                [
                    {
                        "skill": "repeat",
                        "times": 20,
                        "steps": [{"skill": "look", "look": "left", "ms": 500}] * 2,
                    }
                ]
            )
        with self.assertRaisesRegex(ValueError, r"step 0\.0 \(repeat 1/2\).*mid-air"):
            short_hop = [
                {"skill": "jump", "dir": "left", "ms": 200},
                {"skill": "look", "look": "left", "ms": 200},
            ]
            compile_chunk([{"skill": "repeat", "times": 2, "steps": short_hop}])

    def test_act_plays_a_repeat_and_the_schema_offers_it(self) -> None:
        tools = build_game_tools(self.io, frame_after_action=False)
        schema = tools.tools["act"].input_schema["properties"]["steps"]["items"]
        self.assertIn("repeat", schema["properties"]["skill"]["enum"])
        inner = schema["properties"]["steps"]["items"]
        self.assertNotIn("repeat", inner["properties"]["skill"]["enum"])
        self.assertEqual(inner["properties"]["ms"]["maximum"], 10_000)
        hop = [{"skill": "jump", "dir": "left"}, {"skill": "jump", "dir": "right"}]
        response = self.act(tools, [{"skill": "repeat", "times": 5, "steps": hop}])
        self.assertIn("done: 1 steps, 5000 ms (go)", texts(response))
        self.assertEqual(len(self.io.inputs), 21)


if __name__ == "__main__":
    unittest.main()

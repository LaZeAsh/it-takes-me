from __future__ import annotations

import io
import threading
import unittest
from collections.abc import Callable
from unittest.mock import patch

from it_takes_me.game.chunks import compile_chunk, compile_step, play_chunk
from it_takes_me.game.io import NEUTRAL, Button, PadState
from it_takes_me.inference.tools import build_game_tools
from PIL import Image


class Clock:
    def __init__(self) -> None:
        self.ns = 0

    def monotonic(self) -> float:
        return self.ns / 1_000_000_000

    def sleep(self, seconds: float) -> None:
        self.ns += max(1, round(seconds * 1_000_000_000))


class FakeGame:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.inputs: list[tuple[int, PadState]] = []
        self.snapshots = 0
        self.snapshot_fn: Callable[[tuple[int, int], int], Image.Image] = lambda size, n: Image.new(
            "L", size, 0
        )

    def set_pad(self, state: PadState) -> None:
        self.inputs.append((round(self.clock.monotonic() * 1000), state))

    def snapshot(self, size: tuple[int, int]) -> Image.Image:
        self.snapshots += 1
        return self.snapshot_fn(size, self.snapshots)

    def capture(self) -> bytes:
        raise AssertionError("these tests do not request model frames")

    def close(self) -> None:
        pass


class ActionTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.io = FakeGame(self.clock)
        self.enterContext(patch("it_takes_me.game.chunks.time.monotonic", self.clock.monotonic))
        self.enterContext(patch("it_takes_me.game.chunks.time.sleep", self.clock.sleep))

    def test_jump_releases_button_while_movement_continues(self) -> None:
        result = play_chunk(
            self.io,
            compile_chunk([{"skill": "jump", "dir": "forward", "ms": 800, "hold_ms": 100}]),
        )

        self.assertEqual(result.elapsed_ms, 800)
        self.assertEqual(
            self.io.inputs,
            [
                (0, PadState(left=(0, 1), buttons=frozenset({Button.A}))),
                (100, PadState(left=(0, 1))),
                (800, NEUTRAL),
            ],
        )

    def test_pair_uses_chosen_gap_without_extra_inference(self) -> None:
        result = play_chunk(
            self.io,
            compile_chunk(
                [
                    {
                        "skill": "jump_dash",
                        "dir": "forward",
                        "ms": 1200,
                        "hold_ms": 80,
                        "gap_ms": 300,
                    },
                ]
            ),
        )

        self.assertEqual(result.elapsed_ms, 1200)
        self.assertEqual([t for t, _ in self.io.inputs], [0, 80, 380, 460, 1200])
        self.assertEqual(
            [state.buttons for _, state in self.io.inputs],
            [frozenset({Button.A}), frozenset(), frozenset({Button.X}), frozenset(), frozenset()],
        )

    def test_defaults_preserve_original_durations(self) -> None:
        totals = {
            "run": 500,
            "jump": 500,
            "double_jump": 850,
            "dash": 350,
            "jump_dash": 700,
            "ground_pound": 600,
            "interact": 100,
            "grapple": 700,
            "ability": 300,
            "look": 200,
            "locate_partner": 300,
            "wait": 500,
            "raw": 100,
        }
        for skill, total in totals.items():
            with self.subTest(skill=skill):
                self.assertEqual(sum(s.ms for s in compile_step({"skill": skill})), total)

    def test_all_skills_honor_total_duration(self) -> None:
        skills = [
            "run",
            "jump",
            "double_jump",
            "dash",
            "jump_dash",
            "ground_pound",
            "interact",
            "grapple",
            "ability",
            "look",
            "locate_partner",
            "wait",
            "raw",
        ]
        for skill in skills:
            with self.subTest(skill=skill):
                self.assertEqual(
                    sum(s.ms for s in compile_step({"skill": skill, "ms": 1500})), 1500
                )
        interact = compile_step({"skill": "interact", "ms": 1500})
        self.assertEqual(len(interact), 1)
        self.assertEqual(interact[0].state.buttons, frozenset({Button.Y}))
        ability = compile_step({"skill": "ability", "ms": 1500, "hold_ms": 200})
        self.assertEqual([s.ms for s in ability], [200, 1300])
        self.assertEqual(ability[1].state.buttons, frozenset())

    def test_generic_press_supports_every_button(self) -> None:
        for button in Button:
            with self.subTest(button=button):
                segments = compile_step(
                    {"skill": "press", "button": button.value, "ms": 500, "hold_ms": 75}
                )
                self.assertEqual([s.ms for s in segments], [75, 425])
                self.assertEqual(segments[0].state.buttons, frozenset({button}))
                self.assertEqual(segments[1].state, NEUTRAL)

    def test_skip_cutscene_holds_b_then_releases_with_neutral_sticks(self) -> None:
        tools = build_game_tools(self.io, frame_after_action=False)
        for duration in (None, 3500):
            with self.subTest(duration=duration):
                self.io.inputs.clear()
                self.clock.ns = 0
                step = {"skill": "skip_cutscene"}
                if duration is not None:
                    step["ms"] = duration
                response = tools.dispatch(
                    "act", {"task": "skip", "intent": "skip the cutscene", "steps": [step]}
                )
                self.assertTrue(response["success"])
                self.assertEqual(
                    self.io.inputs,
                    [(0, PadState(buttons=frozenset({Button.B}))), (duration or 2000, NEUTRAL)],
                )
                # Camera cuts during a cutscene must not interrupt the required hold.
                self.assertEqual(self.io.snapshots, 0)

    def test_task_switch_requires_closing_the_current_task(self) -> None:
        tools = build_game_tools(self.io, frame_after_action=False)
        wait = [{"skill": "wait", "ms": 100}]

        def act(**fields: object) -> dict:
            return tools.dispatch("act", {"intent": "step", "steps": wait, **fields})

        self.assertFalse(act()["success"])
        self.assertTrue(act(task="pull the lever")["success"])
        self.assertTrue(act(task="Pull the  lever")["success"])
        self.io.inputs.clear()
        refused = act(task="roll the can")
        self.assertFalse(refused["success"])
        self.assertIn("pull the lever", refused["contentItems"][0]["text"])
        self.assertEqual(self.io.inputs, [])
        self.assertEqual(tools.current_task, "pull the lever")
        self.assertTrue(act(task="chase the fuse", previous_task="done")["success"])
        self.assertEqual(tools.current_task, "chase the fuse")

    def test_scene_cut_clears_the_current_task(self) -> None:
        tools = build_game_tools(self.io, frame_after_action=False)
        wait = [{"skill": "wait", "ms": 100}]
        tools.dispatch("act", {"task": "pull the lever", "intent": "step", "steps": wait})
        self.io.snapshot_fn = lambda size, n: Image.new("L", size, 0 if n == 1 else 255)
        run = [{"skill": "run", "dir": "forward", "ms": 7000}]
        response = tools.dispatch("act", {"task": "pull the lever", "intent": "go", "steps": run})
        self.assertIn("task cleared", response["contentItems"][1]["text"])
        self.assertIsNone(tools.current_task)
        self.assertTrue(
            tools.dispatch("act", {"task": "chase the fuse", "intent": "go", "steps": wait})[
                "success"
            ]
        )

    def test_keep_moving_holds_the_run_until_the_next_call(self) -> None:
        tools = build_game_tools(self.io, frame_after_action=False)
        run = [{"skill": "run", "dir": "forward", "sprint": True, "ms": 500}]
        response = tools.dispatch(
            "act", {"task": "t", "intent": "go", "steps": run, "keep_moving": True}
        )
        self.assertTrue(response["success"])
        held = PadState(left=(0.0, 1.0), buttons=frozenset({Button.LS}))
        self.assertEqual(self.io.inputs[-1][1], held)
        self.assertNotIn(NEUTRAL, [state for _, state in self.io.inputs])

        wait = [{"skill": "wait", "ms": 100}]
        response = tools.dispatch("act", {"task": "t", "intent": "stop", "steps": wait})
        texts = [item["text"] for item in response["contentItems"] if item["type"] == "inputText"]
        self.assertIn("You kept moving", texts[0])
        self.assertEqual(self.io.inputs[-1][1], NEUTRAL)
        self.assertIsNone(tools.settle())

    def test_keep_moving_requires_a_final_directional_run(self) -> None:
        tools = build_game_tools(self.io, frame_after_action=False)
        for steps in ([{"skill": "jump", "dir": "forward"}], [{"skill": "run", "dir": "none"}]):
            with self.subTest(steps=steps):
                response = tools.dispatch(
                    "act", {"task": "t", "intent": "go", "steps": steps, "keep_moving": True}
                )
                self.assertFalse(response["success"])
        self.assertEqual(self.io.inputs, [])

    def test_carry_stops_on_scene_cut_and_clears_task(self) -> None:
        tools = build_game_tools(self.io, frame_after_action=False)
        run = [{"skill": "run", "dir": "forward", "ms": 500}]
        tools.dispatch("act", {"task": "t", "intent": "go", "steps": run, "keep_moving": True})
        self.io.snapshot_fn = lambda size, n: Image.new("L", size, 255)
        threading.Event().wait(0.5)  # real time: the carry thread polls every 100 ms
        self.assertEqual(self.io.inputs[-1][1], NEUTRAL)
        note = tools.settle()
        self.assertIn("ended: cut", note)
        self.assertIsNone(tools.current_task)

    def test_look_at_screen_is_high_detail_once_between_actions(self) -> None:
        png = io.BytesIO()
        Image.new("RGB", (64, 36)).save(png, "PNG")
        self.io.capture = lambda: png.getvalue()
        tools = build_game_tools(self.io, frame_after_action=False)
        self.assertTrue(tools.dispatch("look_at_screen", {})["success"])
        self.assertFalse(tools.dispatch("look_at_screen", {})["success"])
        wait = [{"skill": "wait", "ms": 100}]
        tools.dispatch("act", {"task": "t", "intent": "wait", "steps": wait})
        self.assertTrue(tools.dispatch("look_at_screen", {})["success"])

    def test_speed_and_heading_set_the_stick_vector(self) -> None:
        (seg,) = compile_step({"skill": "run", "dir": "forward", "speed": 0.4, "ms": 500})
        self.assertEqual(seg.state.left, (0.0, 0.4))
        (seg,) = compile_step({"skill": "run", "heading": 90, "speed": 0.5, "ms": 500})
        self.assertAlmostEqual(seg.state.left[0], 0.5)
        self.assertAlmostEqual(seg.state.left[1], 0.0)
        (seg,) = compile_step({"skill": "run", "heading": -30, "ms": 500})
        self.assertAlmostEqual(seg.state.left[0], -0.5)
        self.assertAlmostEqual(seg.state.left[1], 0.8660254)
        jump = compile_step({"skill": "jump", "heading": 180, "speed": 0.3})
        self.assertAlmostEqual(jump[0].state.left[1], -0.3)
        (look,) = compile_step({"skill": "look", "look": "left", "look_speed": 0.25})
        self.assertEqual(look.state.right, (-0.25, 0.0))

    def test_invalid_speed_or_heading_is_rejected(self) -> None:
        for step in (
            {"skill": "run", "dir": "forward", "speed": 0},
            {"skill": "run", "dir": "forward", "speed": 1.5},
            {"skill": "run", "dir": "forward", "speed": True},
            {"skill": "run", "heading": 200},
            {"skill": "run", "heading": "left"},
            {"skill": "run", "dir": "forward", "heading": 10},
            {"skill": "run", "dir": "forward", "look_speed": 0.5},
        ):
            with self.subTest(step=step), self.assertRaises(ValueError):
                compile_step(step)

    def test_directional_jump_must_steer_until_it_lands(self) -> None:
        rejected = [
            [{"skill": "double_jump", "dir": "forward", "ms": 650}],
            [{"skill": "jump", "dir": "forward", "ms": 250}, {"skill": "wait", "ms": 400}],
            [{"skill": "dash", "dir": "left", "ms": 150}, {"skill": "wait", "ms": 400}],
            [{"skill": "jump", "dir": "forward", "ms": 250}, {"skill": "run", "dir": "none"}],
        ]
        for steps in rejected:
            with self.subTest(steps=steps), self.assertRaisesRegex(ValueError, "mid-air"):
                compile_chunk(steps)
        allowed = [
            [{"skill": "double_jump", "dir": "forward"}, {"skill": "wait", "ms": 400}],
            [{"skill": "jump", "dir": "forward", "ms": 250}] * 4
            + [{"skill": "run", "dir": "forward", "ms": 300}],
            [{"skill": "jump", "ms": 200}, {"skill": "wait", "ms": 300}],
            [{"skill": "jump", "dir": "forward", "ms": 300}, {"skill": "ground_pound"}],
        ]
        for steps in allowed:
            with self.subTest(steps=steps):
                compile_chunk(steps)

    def test_invalid_timing_is_rejected_before_any_input(self) -> None:
        invalid = [{"skill": "run", "ms": value} for value in (0, -1, 100.5, True, "500")] + [
            {"skill": "jump", "ms": 100, "hold_ms": 101},
            {"skill": "double_jump", "ms": 400},
            {"skill": "jump_dash", "ms": 1000, "gap_ms": 0},
            {"skill": "interact", "hold_ms": False},
            {"skill": "run", "hold_ms": 100},
            {"skill": "jump", "gap_ms": 100},
            {"skill": "press"},
            {"skill": "ability", "button": "Y"},
            {"skill": "skip_cutscene", "dir": "forward"},
        ]
        for step in invalid:
            with self.subTest(step=step), self.assertRaises(ValueError):
                play_chunk(self.io, compile_chunk([{"skill": "run", "ms": 100}, step]))
        self.assertEqual(self.io.inputs, [])

    def test_long_run_stops_locally_and_skips_later_steps(self) -> None:
        # Partner animation must not hide a frozen controlled character's view.
        def partner_moves(size: tuple[int, int], n: int) -> Image.Image:
            image = Image.new("L", size, 0)
            image.paste(n * 10 % 256, (size[0] // 2, 0, size[0], size[1]))
            return image

        self.io.snapshot_fn = partner_moves
        result = play_chunk(
            self.io,
            compile_chunk(
                [
                    {"skill": "run", "dir": "forward", "ms": 7000, "until": []},
                    {"skill": "jump"},
                ]
            ),
        )
        self.assertEqual(result.stopped, (0, "stuck"))
        self.assertLess(result.elapsed_ms, 1000)
        self.assertEqual(len(self.io.inputs), 2)
        self.assertEqual(self.io.inputs[-1][1], NEUTRAL)

    def test_long_run_executes_full_duration_when_view_keeps_moving(self) -> None:
        self.io.snapshot_fn = lambda size, n: Image.new("L", size, n * 3)
        result = play_chunk(
            self.io, compile_chunk([{"skill": "run", "dir": "forward", "ms": 7000}])
        )
        self.assertEqual(result.elapsed_ms, 7000)
        self.assertIsNone(result.stopped)
        self.assertGreater(self.io.snapshots, 60)
        self.assertEqual(self.io.inputs[-1], (7000, NEUTRAL))

    def test_long_run_stops_on_scene_change(self) -> None:
        self.io.snapshot_fn = lambda size, n: Image.new("L", size, 0 if n == 1 else 255)
        result = play_chunk(
            self.io, compile_chunk([{"skill": "run", "dir": "forward", "ms": 7000}])
        )
        self.assertEqual(result.stopped, (0, "cut"))
        self.assertEqual(result.elapsed_ms, 100)
        self.assertEqual(self.io.inputs[-1][1], NEUTRAL)

    def test_capture_failure_releases_pad(self) -> None:
        def fail(size: tuple[int, int], n: int) -> Image.Image:
            raise RuntimeError("capture failed")

        self.io.snapshot_fn = fail
        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            play_chunk(self.io, compile_chunk([{"skill": "run", "dir": "forward", "ms": 7000}]))
        self.assertEqual(self.io.inputs[-1][1], NEUTRAL)

    def test_tool_schema_and_execution_share_configured_limit(self) -> None:
        tools = build_game_tools(self.io, max_chunk_ms=6000, frame_after_action=False)
        other = build_game_tools(self.io, max_chunk_ms=9000, frame_after_action=False)
        for spec in (tools.specs()[1], tools.responses_specs()[1]):
            schema = spec.get("inputSchema", spec.get("parameters"))
            properties = schema["properties"]["steps"]["items"]["properties"]
            for timing in ("ms", "hold_ms", "gap_ms"):
                self.assertEqual(properties[timing]["minimum"], 1)
                self.assertEqual(properties[timing]["maximum"], 6000)
            self.assertEqual(properties["button"]["enum"], [b.value for b in Button])
            self.assertIn("skip_cutscene", properties["skill"]["enum"])
            self.assertIn("2 frames", spec["description"])
        self.assertEqual(
            other.tools["act"].input_schema["properties"]["steps"]["items"]["properties"]["ms"][
                "maximum"
            ],
            9000,
        )
        response = tools.dispatch(
            "act",
            {
                "intent": "too long",
                "steps": [
                    {"skill": "run", "ms": 5000},
                    {"skill": "wait", "ms": 1001},
                ],
            },
        )
        self.assertFalse(response["success"])
        self.assertEqual(self.io.inputs, [])

        response = tools.dispatch(
            "act",
            {
                "task": "find Cody",
                "intent": "tap then move",
                "steps": [
                    {"skill": "press", "button": "RS", "dir": "forward", "ms": 600, "hold_ms": 100},
                ],
            },
        )
        self.assertTrue(response["success"])
        self.assertIn("600 ms", response["contentItems"][0]["text"])
        self.assertEqual(self.io.inputs[-1], (600, NEUTRAL))


if __name__ == "__main__":
    unittest.main()

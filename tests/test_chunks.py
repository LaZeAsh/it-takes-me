from __future__ import annotations

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
                response = tools.dispatch("act", {"intent": "skip the cutscene", "steps": [step]})
                self.assertTrue(response["success"])
                self.assertEqual(
                    self.io.inputs,
                    [(0, PadState(buttons=frozenset({Button.B}))), (duration or 2000, NEUTRAL)],
                )
                # Camera cuts during a cutscene must not interrupt the required hold.
                self.assertEqual(self.io.snapshots, 0)

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

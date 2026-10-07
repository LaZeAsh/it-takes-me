from __future__ import annotations

import unittest
from unittest.mock import patch

from it_takes_me.game.chunks import compile_chunk, compile_step
from it_takes_me.game.playback import jump_outcome, play_segments
from it_takes_me.game.shorthand import parse_steps
from it_takes_me.game.tuning import LOOK_MS_PER_TURN
from it_takes_me.sol.tools import build_game_tools
from PIL import Image, ImageFilter
from tests.test_chunks import Clock, FakeGame

# A textured world taller than the view, so the view can be moved up and down over it.
WORLD = Image.effect_noise((160, 200), 80).convert("L").filter(ImageFilter.GaussianBlur(1.5))
OTHER = Image.effect_noise((160, 200), 80).convert("L").filter(ImageFilter.GaussianBlur(1.5))


def view(rise: int = 0, world: Image.Image = WORLD) -> Image.Image:
    """The 160x90 snapshot with the camera `rise` pixels higher (the scene moves down)."""
    top = 55 - rise
    return world.crop((0, top, 160, top + 90))


def half(im: Image.Image) -> Image.Image:
    return im.crop((0, 0, 80, 90))


class JumpOutcomeTests(unittest.TestCase):
    def test_verdicts(self) -> None:
        cases = {
            "higher": view(9),
            "lower": view(-9),
            "blocked": view(0),
            "moved": view(0, OTHER),
            "level": view(1),
        }
        for verdict, after in cases.items():
            with self.subTest(verdict=verdict):
                check = jump_outcome("0 (jump)", half(view()), half(after), True)
                self.assertEqual(check.verdict, verdict)
                self.assertIn("step 0 (jump)", check.describe())
        self.assertAlmostEqual(jump_outcome("0", half(view()), half(view(9)), True).shift, 0.1)


class LandingPlaybackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.io = FakeGame(self.clock)
        self.enterContext(patch("it_takes_me.game.playback.time.monotonic", self.clock.monotonic))
        self.enterContext(patch("it_takes_me.game.playback.time.sleep", self.clock.sleep))
        self.enterContext(patch("it_takes_me.sol.pad_thread.time.monotonic", self.clock.monotonic))
        self.enterContext(patch("it_takes_me.sol.pad_thread.time.sleep", self.clock.sleep))

    def world_after(self, ms: int, after: Image.Image) -> None:
        """Our view is `view()` until `ms` into play, then `after`."""
        self.io.snapshot_fn = lambda size, n: (
            view() if self.clock.monotonic() * 1000 < ms else after
        )

    def play(self, text: str):
        segments = compile_chunk(parse_steps(text))
        return play_segments(self.io, segments, 0, "left", hold_last=False, check_jumps=True)

    def test_a_jump_that_lands_higher_lets_the_chunk_continue(self) -> None:
        self.world_after(300, view(9))
        result = self.play("jump f; run f 400")
        self.assertIsNone(result.stopped)
        self.assertEqual([j.verdict for j in result.jumps], ["higher"])
        self.assertEqual(result.jumps[0].label, "0 (jump)")

    def test_a_blocked_directional_jump_skips_the_steps_planned_after_it(self) -> None:
        self.world_after(10_000, view())  # nothing ever changes
        result = self.play("run f 300; jump f; run f 400; jump f")
        self.assertEqual(result.stopped, (1, "blocked"))
        self.assertEqual(result.elapsed_ms, 300 + 650)

    def test_a_jump_in_place_or_at_the_end_is_reported_without_stopping(self) -> None:
        self.world_after(10_000, view())
        self.assertIsNone(self.play("jump none; run f 400").stopped)
        result = self.play("run f 300; jump f")
        self.assertIsNone(result.stopped)
        self.assertEqual([j.verdict for j in result.jumps], ["blocked"])

    def test_jumps_in_a_repeat_are_checked_one_by_one(self) -> None:
        # Every snapshot alternates worlds, so each jump sees a new view.
        self.io.snapshot_fn = lambda size, n: view(0, WORLD if n % 2 else OTHER)
        result = self.play("2x(jump l 500; jump r 500)")
        self.assertEqual(
            [j.label for j in result.jumps],
            [
                "0.0 (jump, repeat 1/2)",
                "0.1 (jump, repeat 1/2)",
                "0.0 (jump, repeat 2/2)",
                "0.1 (jump, repeat 2/2)",
            ],
        )

    def test_act_reports_landings(self) -> None:
        self.world_after(300, view(9))
        tools = build_game_tools(self.io, frame_after_action=False)
        response = tools.dispatch("act", {"task": "t", "intent": "go", "steps": "jump f"})
        texts = [i["text"] for i in response["contentItems"] if i["type"] == "inputText"]
        self.assertTrue(any(t.startswith("landings: step 0 (jump): the view rose") for t in texts))


class LookDegreesTests(unittest.TestCase):
    def test_degrees_turn_for_a_share_of_a_full_circle(self) -> None:
        (seg,) = compile_step(parse_steps("look r 90deg")[0])
        self.assertEqual(seg.ms, round(LOOK_MS_PER_TURN / 4))
        self.assertEqual(seg.state.right, (1.0, 0.0))
        (seg,) = compile_step(parse_steps("look l 360deg")[0])
        self.assertEqual((seg.ms, seg.state.right), (LOOK_MS_PER_TURN, (-1.0, 0.0)))

    def test_degree_mistakes_are_rejected(self) -> None:
        for text, message in (
            ("look u 30deg", "only work on look left or right"),
            ("look r 30deg 400", "not both"),
            ("look r 30deg s0.5", "not both"),
            ("look r 400deg", "from 1 to 360"),
            ("run f 30deg", "only works on look"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, message):
                compile_step(parse_steps(text)[0])


if __name__ == "__main__":
    unittest.main()

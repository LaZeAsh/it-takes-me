from __future__ import annotations

import unittest

from it_takes_me.game.chunks import compile_chunk
from it_takes_me.game.shorthand import parse_steps


class ShorthandTests(unittest.TestCase):
    def test_tokens_map_to_step_fields(self) -> None:
        self.assertEqual(
            parse_steps("run f 1200 sprint stuck; jump @-22 600 h250; djump fl 900 h250 g300"),
            [
                {"skill": "run", "dir": "forward", "ms": 1200, "sprint": True, "until": ["stuck"]},
                {"skill": "jump", "heading": -22.0, "ms": 600, "hold_ms": 250},
                {
                    "skill": "double_jump",
                    "dir": "forward-left",
                    "ms": 900,
                    "hold_ms": 250,
                    "gap_ms": 300,
                },
            ],
        )

    def test_look_speed_and_look_directions(self) -> None:
        self.assertEqual(
            parse_steps("look l 300 s0.3; run b 400 s0.5"),
            [
                {"skill": "look", "look": "left", "ms": 300, "look_speed": 0.3},
                {"skill": "run", "dir": "back", "ms": 400, "speed": 0.5},
            ],
        )

    def test_capital_letters_are_buttons_and_lowercase_are_directions(self) -> None:
        self.assertEqual(
            parse_steps("press B b 200; raw 250 L0,1 R-0.5,0 LS B"),
            [
                {"skill": "press", "button": "B", "dir": "back", "ms": 200},
                {
                    "skill": "raw",
                    "ms": 250,
                    "left": [0.0, 1.0],
                    "right": [-0.5, 0.0],
                    "buttons": ["LS", "B"],
                },
            ],
        )

    def test_repeat_block(self) -> None:
        steps = parse_steps("run f 300; 3x(jump l 500 h250; jump r 500 h250)")
        self.assertEqual(steps[1]["skill"], "repeat")
        self.assertEqual(steps[1]["times"], 3)
        self.assertEqual(len(steps[1]["steps"]), 2)
        self.assertEqual(sum(s.ms for s in compile_chunk(steps)), 3300)

    def test_aliases(self) -> None:
        skills = [s["skill"] for s in parse_steps("jdash f; pound; locate; skip 2500")]
        self.assertEqual(skills, ["jump_dash", "ground_pound", "locate_partner", "skip_cutscene"])

    def test_errors_name_the_step(self) -> None:
        for text, message in (
            ("hop f 300", "unknown skill 'hop'"),
            ("run f 300 fast", "in step 'run f 300 fast': unknown token 'fast'"),
            ("2x(run f; 2x(jump f))", "repeats cannot nest"),
            ("2x(run f", "unmatched"),
            (" ; ", "empty"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, message):
                parse_steps(text)


if __name__ == "__main__":
    unittest.main()

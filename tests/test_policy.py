from __future__ import annotations

import unittest

from it_takes_me.config import Settings
from it_takes_me.inference.prompts import developer_instructions


class GameplayPolicyTests(unittest.TestCase):
    def test_default_tool_budget_is_short(self) -> None:
        self.assertEqual(Settings().max_tool_calls_per_turn, 8)

    def test_prompt_tells_model_to_act_from_fresh_frame(self) -> None:
        prompt = developer_instructions("May")

        self.assertIn("act from it immediately", prompt)
        self.assertIn("only gives a high-detail frame", prompt)
        self.assertIn("keep_moving", prompt)

    def test_prompt_prioritizes_on_screen_markers(self) -> None:
        prompt = developer_instructions("May")

        self.assertIn("## One task at a time", prompt)
        self.assertIn("Choosing the next task from on-screen markers", prompt)
        self.assertIn("Yellow circle on an object", prompt)
        self.assertLess(prompt.index("Yellow circle"), prompt.index("Objective hexagon"))
        self.assertIn("Cody's, green on your screen", prompt)

    def test_prompt_includes_chapter_walkthrough(self) -> None:
        prompt = developer_instructions("May", chapter="The Shed")

        self.assertIn("## Chapter background: The Shed", prompt)
        self.assertIn("lever", prompt)
        self.assertIn("It is not a checklist", prompt)
        self.assertIn("resume from any checkpoint", prompt)
        self.assertNotIn("Chapter background", developer_instructions("May"))


if __name__ == "__main__":
    unittest.main()

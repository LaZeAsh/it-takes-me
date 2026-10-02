from __future__ import annotations

import unittest

from it_takes_me.config import Settings
from it_takes_me.inference.prompts import developer_instructions


class GameplayPolicyTests(unittest.TestCase):
    def test_default_tool_budget_is_short(self) -> None:
        self.assertEqual(Settings().max_tool_calls_per_turn, 8)

    def test_prompt_tells_model_to_act_from_fresh_frame(self) -> None:
        prompt = developer_instructions("May")

        self.assertIn("normally act from it immediately", prompt)
        self.assertIn("Request high detail only", prompt)

    def test_prompt_prioritizes_on_screen_markers(self) -> None:
        prompt = developer_instructions("May")

        self.assertIn("Follow the on-screen markers first", prompt)
        self.assertIn("white diamond", prompt)
        self.assertIn("Cody's, green on your screen", prompt)

    def test_prompt_includes_chapter_walkthrough(self) -> None:
        prompt = developer_instructions("May", chapter="The Shed")

        self.assertIn("## Chapter walkthrough: The Shed", prompt)
        self.assertIn("lever", prompt)
        self.assertNotIn("Chapter walkthrough", developer_instructions("May"))


if __name__ == "__main__":
    unittest.main()

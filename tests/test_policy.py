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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from types import SimpleNamespace

from it_takes_me.sol.session import UsageBreakdown


class TokenBreakdownTests(unittest.TestCase):
    def test_normalizes_all_usage_fields(self) -> None:
        value = SimpleNamespace(
            input_tokens=120,
            cached_input_tokens=80,
            cache_write_input_tokens=None,
            output_tokens=10,
            reasoning_output_tokens=7,
            total_tokens=137,
        )

        self.assertEqual(
            UsageBreakdown.from_object(value).to_dict(),
            {
                "input_tokens": 120,
                "cached_input_tokens": 80,
                "cache_write_input_tokens": 0,
                "output_tokens": 10,
                "reasoning_output_tokens": 7,
                "total_tokens": 137,
            },
        )


if __name__ == "__main__":
    unittest.main()

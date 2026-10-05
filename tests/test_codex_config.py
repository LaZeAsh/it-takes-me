from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from it_takes_me.sol.runtime import direct_tool_catalog, lean_overrides


class CodexConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_catalog_switches_every_model_to_direct_tools(self) -> None:
        models = [
            {"slug": "a", "tool_mode": "code_mode_only", "multi_agent_version": "v2"},
            {"slug": "b"},
        ]
        (self.home / "models_cache.json").write_text(json.dumps({"models": models}))
        out = direct_tool_catalog(self.home, self.home / "catalog.json")
        assert out is not None
        written = json.loads(out.read_text())["models"]
        self.assertEqual([m["tool_mode"] for m in written], ["direct", "direct"])
        self.assertNotIn("multi_agent_version", written[0])

    def test_missing_catalog_leaves_codex_defaults(self) -> None:
        self.assertIsNone(direct_tool_catalog(self.home, self.home / "catalog.json"))
        self.assertFalse(any("model_catalog_json" in o for o in lean_overrides(self.home, None)))

    def test_overrides_disable_user_mcp_servers_and_point_at_catalog(self) -> None:
        (self.home / "config.toml").write_text('[mcp_servers.node_repl]\ncommand = "x"\n')
        overrides = lean_overrides(self.home, Path("C:/tmp/catalog.json"))
        self.assertIn("mcp_servers.node_repl.enabled=false", overrides)
        self.assertIn("features.shell_tool=false", overrides)
        self.assertIn('model_catalog_json="C:/tmp/catalog.json"', overrides)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

import config


class ConfigNormalizationTests(unittest.TestCase):
    def test_normalize_daily_limit_from_string(self):
        cfg = config.normalize_config({"filter": {"daily_limit": "30"}})
        self.assertEqual(cfg["filter"]["daily_limit"], 30)

    def test_normalize_daily_limit_from_invalid_type_uses_default(self):
        cfg = config.normalize_config({"filter": {"daily_limit": {"bad": True}}})
        self.assertEqual(cfg["filter"]["daily_limit"], 20)

    def test_load_config_rejects_non_mapping_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text("- just\n- a\n- list\n", encoding="utf-8")
            self.assertEqual(config.load_config(path), {})


if __name__ == "__main__":
    unittest.main()

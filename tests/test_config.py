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

    def test_normalize_display_tags_max_ip_count_from_string(self):
        cfg = config.normalize_config({"filter": {"display_tags": {"max_ip_count": "3"}}})
        self.assertEqual(cfg["filter"]["display_tags"]["max_ip_count"], 3)

    def test_normalize_display_tags_max_ip_count_from_invalid_type_uses_default(self):
        cfg = config.normalize_config({"filter": {"display_tags": "bad"}})
        self.assertEqual(cfg["filter"]["display_tags"]["max_ip_count"], 2)

    def test_normalize_tag_classifier_defaults_and_ints(self):
        cfg = config.normalize_config({"tag_classifier": {"ttl_days": "14", "batch_size": "bad"}})
        self.assertFalse(cfg["tag_classifier"]["enabled"])
        self.assertEqual(cfg["tag_classifier"]["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(cfg["tag_classifier"]["model"], "deepseek-chat")
        self.assertEqual(cfg["tag_classifier"]["ttl_days"], 14)
        self.assertEqual(cfg["tag_classifier"]["batch_size"], 50)
        self.assertEqual(cfg["tag_classifier"]["concurrency"], 5)

    def test_normalize_tag_classifier_invalid_section_uses_defaults(self):
        cfg = config.normalize_config({"tag_classifier": "bad"})
        self.assertFalse(cfg["tag_classifier"]["enabled"])
        self.assertEqual(cfg["tag_classifier"]["ttl_days"], 30)

    def test_load_config_rejects_non_mapping_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text("- just\n- a\n- list\n", encoding="utf-8")
            self.assertEqual(config.load_config(path), {})

    def test_global_key_inheritance(self):
        """profiler.ai.api_key → scorer + tag_classifier 自动继承"""
        cfg = config.normalize_config({
            "profiler": {"ai": {"api_key": "sk-shared"}},
            "ai": {"scorer": {"enabled": True}},
            "tag_classifier": {"enabled": True},
        })
        self.assertEqual(cfg["ai"]["scorer"]["api_key"], "sk-shared")
        self.assertEqual(cfg["tag_classifier"]["api_key"], "sk-shared")

    def test_global_key_no_override_existing(self):
        """已有自己的 key 时不被覆盖"""
        cfg = config.normalize_config({
            "profiler": {"ai": {"api_key": "sk-shared"}},
            "tag_classifier": {"api_key": "sk-own", "enabled": True},
        })
        self.assertEqual(cfg["tag_classifier"]["api_key"], "sk-own")


if __name__ == "__main__":
    unittest.main()

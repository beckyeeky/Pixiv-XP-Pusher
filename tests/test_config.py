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
        self.assertEqual(cfg["providers"], {})
        self.assertEqual(cfg["models"], {})
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
        """profiler.ai.api_key → scorer 自动继承；分类器改由 Provider 持有凭据"""
        cfg = config.normalize_config({
            "profiler": {"ai": {"api_key": "sk-shared"}},
            "ai": {"scorer": {"enabled": True}},
            "tag_classifier": {"enabled": True},
        })
        self.assertEqual(cfg["ai"]["scorer"]["api_key"], "sk-shared")
        self.assertNotIn("api_key", cfg["tag_classifier"])

    def test_global_key_no_override_existing(self):
        """已有自己的 key 时不被覆盖"""
        cfg = config.normalize_config({
            "profiler": {"ai": {"api_key": "sk-shared"}},
            "tag_classifier": {"api_key": "sk-own", "enabled": True},
        })
        self.assertNotIn("api_key", cfg["tag_classifier"])

    def test_normalize_providers_models_and_resolves_judge_model_references(self):
        cfg = config.normalize_config({
            "providers": {
                "gateway": {
                    "type": "openai_compatible",
                    "base_url": "https://a.example/v1",
                    "api_key": "provider-key",
                },
            },
            "models": {
                "fast": {"provider": "gateway", "model": "model-a"},
            },
            "tag_classifier": {
                "maintenance": {"max_tags_per_run": "3"},
                "judges": ["fast", "missing"],
            },
        })

        self.assertEqual(cfg["tag_classifier"]["maintenance"]["max_tags_per_run"], 3)
        self.assertEqual(cfg["tag_classifier"]["judges"], ["fast"])
        self.assertEqual(cfg["models"]["fast"]["provider"], "gateway")
        self.assertEqual(cfg["providers"]["gateway"]["api_key"], "provider-key")

    def test_normalize_migrates_current_single_model_classifier_to_provider_and_model(self):
        cfg = config.normalize_config({
            "tag_classifier": {
                "api_key": "existing-key",
                "base_url": "https://judge.example/v1",
                "model": "existing-model",
            },
        })

        self.assertEqual(cfg["tag_classifier"]["judges"], ["tag_classifier_default"])
        self.assertEqual(cfg["models"]["tag_classifier_default"]["provider"], "tag_classifier_provider")
        self.assertEqual(cfg["models"]["tag_classifier_default"]["model"], "existing-model")
        self.assertEqual(cfg["providers"]["tag_classifier_provider"]["api_key"], "existing-key")

    def test_normalize_ip_diversity_defaults_and_invalid_values(self):
        cfg = config.normalize_config({"filter": {"ip_diversity": {"enabled": 1, "decay_factor": "bad", "floor": 2}}})
        self.assertTrue(cfg["filter"]["ip_diversity"]["enabled"])
        self.assertEqual(cfg["filter"]["ip_diversity"]["decay_factor"], 0.6)
        self.assertEqual(cfg["filter"]["ip_diversity"]["floor"], 1.0)

    def test_normalize_author_diversity_uses_tuned_default(self):
        cfg = config.normalize_config({"filter": {"author_diversity": "bad"}})
        self.assertFalse(cfg["filter"]["author_diversity"]["enabled"])
        self.assertEqual(cfg["filter"]["author_diversity"]["decay_factor"], 0.5)
        self.assertEqual(cfg["filter"]["author_diversity"]["floor"], 0.1)

    def test_normalize_proxy_url_string_none_to_null(self):
        cfg = config.normalize_config({"notifier": {"telegram": {"proxy_url": "None"}}})
        self.assertIsNone(cfg["notifier"]["telegram"]["proxy_url"])

    def test_normalize_proxy_url_adds_http_scheme(self):
        cfg = config.normalize_config({"notifier": {"telegram": {"proxy_url": "127.0.0.1:7890"}}})
        self.assertEqual(cfg["notifier"]["telegram"]["proxy_url"], "http://127.0.0.1:7890")


if __name__ == "__main__":
    unittest.main()

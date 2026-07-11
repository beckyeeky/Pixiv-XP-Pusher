import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "refresh_config.py"
SPEC = importlib.util.spec_from_file_location("refresh_config", SCRIPT_PATH)
refresh_config = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(refresh_config)


class MergeTemplateTests(unittest.TestCase):
    def test_merges_known_mapping_values_and_keeps_new_template_defaults(self):
        result = refresh_config.merge_template(
            {
                "pixiv": {"refresh_token": "", "sync_token": ""},
                "filter": {"daily_limit": 20, "daily_slate": {"enabled": True}},
            },
            {
                "pixiv": {"refresh_token": "saved-token"},
                "filter": {"daily_limit": 50, "obsolete_option": True},
                "obsolete_section": {"enabled": True},
            },
        )

        self.assertEqual(result["pixiv"]["refresh_token"], "saved-token")
        self.assertEqual(result["pixiv"]["sync_token"], "")
        self.assertEqual(result["filter"]["daily_limit"], 50)
        self.assertTrue(result["filter"]["daily_slate"]["enabled"])
        self.assertNotIn("obsolete_option", result["filter"])
        self.assertNotIn("obsolete_section", result)

    def test_keep_unknown_retains_source_only_fields(self):
        result = refresh_config.merge_template(
            {"filter": {"daily_limit": 20}},
            {"filter": {"daily_limit": 30, "legacy": "value"}, "plugin": {"x": 1}},
            keep_unknown=True,
        )

        self.assertEqual(result["filter"], {"daily_limit": 30, "legacy": "value"})
        self.assertEqual(result["plugin"], {"x": 1})

    def test_empty_template_mapping_accepts_user_defined_entries(self):
        result = refresh_config.merge_template(
            {"notifier": {"telegram": {"topic_rules": {}}}},
            {"notifier": {"telegram": {"topic_rules": {"r18": 12345}}}},
        )

        self.assertEqual(result["notifier"]["telegram"]["topic_rules"], {"r18": 12345})
        self.assertEqual(
            refresh_config.find_unknown_fields(
                {"notifier": {"telegram": {"topic_rules": {}}}},
                {"notifier": {"telegram": {"topic_rules": {"r18": 12345}}}},
            ),
            {},
        )

    def test_finds_unknown_fields_with_original_structure_and_values(self):
        unknown = refresh_config.find_unknown_fields(
            {"filter": {"daily_limit": 20}, "pixiv": {"refresh_token": ""}},
            {
                "filter": {"daily_limit": 30, "legacy": "value"},
                "pixiv": {"refresh_token": "saved-token"},
                "plugin": {"enabled": True},
            },
        )

        self.assertEqual(
            unknown,
            {"filter": {"legacy": "value"}, "plugin": {"enabled": True}},
        )

    def test_migrates_legacy_tag_classifier_judge_settings(self):
        migrated, messages = refresh_config.migrate_legacy_judge_settings(
            {
                "providers": {"deepseek": {"type": "openai_compatible", "api_key": ""}},
                "models": {"deepseek_flash": {"provider": "deepseek", "model": "deepseek-v4-flash"}},
                "tag_classifier": {"judges": ["deepseek_flash"]},
            },
            {
                "tag_classifier": {
                    "enabled": True,
                    "provider": "openai_compatible",
                    "api_key": "old-key",
                    "base_url": "https://api.example/v1",
                    "model": "old-model",
                }
            },
        )

        self.assertNotIn("api_key", migrated["tag_classifier"])
        self.assertEqual(migrated["providers"]["deepseek"]["api_key"], "old-key")
        self.assertEqual(migrated["providers"]["deepseek"]["base_url"], "https://api.example/v1")
        self.assertEqual(migrated["models"]["deepseek_flash"]["model"], "old-model")
        self.assertIn("tag_classifier.api_key -> providers.deepseek.api_key", messages)

    def test_round_trip_output_keeps_template_comments(self):
        yaml = refresh_config.create_yaml()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "example.yaml"
            source_path = Path(tmpdir) / "source.yaml"
            template_path.write_text(
                "# Template heading\npixiv:\n  # Keep this explanation\n  refresh_token: ''\n",
                encoding="utf-8",
            )
            source_path.write_text("pixiv:\n  refresh_token: saved-token\n", encoding="utf-8")
            template = refresh_config.load_mapping(template_path, yaml)
            source = refresh_config.load_mapping(source_path, yaml)
            rendered = refresh_config.dump_yaml(yaml, refresh_config.merge_template(template, source))

        self.assertIn("# Template heading", rendered)
        self.assertIn("# Keep this explanation", rendered)
        self.assertIn("refresh_token: 'saved-token'", rendered)

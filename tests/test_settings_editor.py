import unittest

from web.settings_editor import (
    apply_settings_payload,
    build_settings_snapshot,
    merge_config_replace_lists,
    redact_sensitive_config,
)


class SettingsEditorTests(unittest.TestCase):
    def test_snapshot_fills_missing_settings_tree(self):
        snapshot = build_settings_snapshot({"web": {"password": "hash"}})

        self.assertEqual(snapshot["pixiv"]["refresh_token"], "")
        self.assertEqual(snapshot["notifier"]["telegram"]["rich_message"]["image_mode"], "photo")
        self.assertEqual(snapshot["fetcher"]["bookmark_threshold"]["related"], 0)
        self.assertEqual(snapshot["network"]["random_delay"], [1.0, 3.0])
        self.assertEqual(snapshot["profiler"]["danbooru_login"], "")
        self.assertEqual(snapshot["profiler"]["danbooru_api_key"], "")

    def test_snapshot_repairs_malformed_random_delay(self):
        snapshot = build_settings_snapshot({"network": {"random_delay": "bad"}})

        self.assertEqual(snapshot["network"]["random_delay"], [1.0, 3.0])

    def test_apply_settings_removes_legacy_push_cron(self):
        merged = apply_settings_payload(
            {
                "scheduler": {"cron": "0 12 * * *", "coalesce": True},
                "web": {"require_login_password": False, "password": ""},
            },
            {
                "scheduler": {"cron": "0 20 * * *"},
                "web": {"require_login_password": False},
            },
            str,
        )

        self.assertNotIn("cron", merged["scheduler"])
        self.assertTrue(merged["scheduler"]["coalesce"])

    def test_merge_replaces_lists_and_preserves_unspecified_fields(self):
        merged = merge_config_replace_lists(
            {"strategies": ["xp_search", "related"], "web": {"port": 8000, "enabled": True}},
            {"strategies": ["ranking"], "web": {"port": 9000}},
        )

        self.assertEqual(merged["strategies"], ["ranking"])
        self.assertEqual(merged["web"]["port"], 9000)
        self.assertTrue(merged["web"]["enabled"])

    def test_apply_settings_keeps_existing_password_when_left_blank(self):
        merged = apply_settings_payload(
            {"web": {"require_login_password": True, "password": "old-hash"}},
            {"web": {"require_login_password": True}, "web_password": "", "web_password_confirm": ""},
            lambda value: f"hash:{value}",
        )

        self.assertEqual(merged["web"]["password"], "old-hash")
        self.assertNotIn("web_password", merged)

    def test_apply_settings_hashes_new_password(self):
        merged = apply_settings_payload(
            {"web": {"require_login_password": True, "password": "old-hash"}},
            {"web": {"require_login_password": True}, "web_password": "newpass", "web_password_confirm": "newpass"},
            lambda value: f"hash:{value}",
        )

        self.assertEqual(merged["web"]["password"], "hash:newpass")

    def test_apply_settings_rejects_password_mismatch(self):
        with self.assertRaisesRegex(ValueError, "两次输入不一致"):
            apply_settings_payload(
                {"web": {"require_login_password": True, "password": "old-hash"}},
                {"web": {"require_login_password": True}, "web_password": "newpass", "web_password_confirm": "other"},
                lambda value: f"hash:{value}",
            )

    def test_apply_settings_can_disable_password_auth(self):
        merged = apply_settings_payload(
            {"web": {"require_login_password": True, "password": "old-hash"}},
            {"web": {"require_login_password": False}, "web_password": "", "web_password_confirm": ""},
            lambda value: f"hash:{value}",
        )

        self.assertFalse(merged["web"]["require_login_password"])
        self.assertEqual(merged["web"]["password"], "")

    def test_redact_sensitive_values_preserves_regular_config(self):
        redacted = redact_sensitive_config({
            "pixiv": {"refresh_token": "secret"},
            "notifier": {"telegram": {"bot_token": "token"}},
            "profiler": {"boost_tags": {"cat": 1.8}},
        })

        self.assertEqual(redacted["pixiv"]["refresh_token"], "••••")
        self.assertEqual(redacted["notifier"]["telegram"]["bot_token"], "••••")
        self.assertEqual(redacted["profiler"]["boost_tags"]["cat"], 1.8)

    def test_provider_credentials_are_masked_replaced_or_explicitly_deleted(self):
        current = {
            "web": {"require_login_password": False, "password": ""},
            "providers": {"gateway": {"type": "openai_compatible", "base_url": "https://gateway.example/v1", "api_key": "sk-0123456789"}},
        }
        retained = apply_settings_payload(current, {"providers": {"gateway": {"type": "openai_compatible", "api_key": ""}}}, str)
        replaced = apply_settings_payload(current, {"providers": {"gateway": {"type": "openai_compatible", "api_key": "new-secret"}}}, str)
        deleted = apply_settings_payload(current, {"providers": {"gateway": {"type": "openai_compatible", "credential_action": "delete"}}}, str)

        self.assertEqual(redact_sensitive_config(current)["providers"]["gateway"]["api_key"], "sk…6789")
        self.assertEqual(retained["providers"]["gateway"]["api_key"], "sk-0123456789")
        self.assertEqual(replaced["providers"]["gateway"]["api_key"], "new-secret")
        self.assertEqual(deleted["providers"]["gateway"]["api_key"], "")
        self.assertNotIn("credential_action", deleted["providers"]["gateway"])

    def test_snapshot_includes_classification_maintenance_operational_defaults(self):
        snapshot = build_settings_snapshot({"web": {"password": "hash"}})

        classifier = snapshot["tag_classifier"]
        self.assertFalse(classifier["enabled"])
        self.assertEqual(classifier["ttl_days"], 30)
        self.assertEqual(classifier["batch_size"], 50)
        self.assertEqual(classifier["concurrency"], 5)
        self.assertEqual(classifier["maintenance"]["max_tags_per_run"], 40)

    def test_apply_settings_saves_classification_maintenance_operational_fields(self):
        current = {
            "web": {"require_login_password": False, "password": ""},
            "providers": {
                "pixiv": {"type": "pixiv", "refresh_token": "t", "user_id": 1},
                "danbooru": {"type": "danbooru"},
                "gateway": {"type": "openai_compatible", "base_url": "https://gateway.example/v1", "api_key": "k"},
            },
            "models": {"judge": {"provider": "gateway", "model": "gpt", "capabilities": ["llm"]}},
            "tag_classifier": {
                "enabled": False,
                "judges": ["judge"],
                "ttl_days": 30,
                "batch_size": 50,
                "concurrency": 5,
                "maintenance": {"max_tags_per_run": 40, "prefer_unresolved_first": True},
            },
        }
        merged = apply_settings_payload(current, {
            "tag_classifier": {
                "enabled": True,
                "judges": ["judge"],
                "ttl_days": 14,
                "batch_size": 20,
                "concurrency": 3,
                "maintenance": {"max_tags_per_run": 12},
            },
        }, str)

        self.assertTrue(merged["tag_classifier"]["enabled"])
        self.assertEqual(merged["tag_classifier"]["ttl_days"], 14)
        self.assertEqual(merged["tag_classifier"]["batch_size"], 20)
        self.assertEqual(merged["tag_classifier"]["concurrency"], 3)
        self.assertEqual(merged["tag_classifier"]["maintenance"]["max_tags_per_run"], 12)
        self.assertTrue(merged["tag_classifier"]["maintenance"]["prefer_unresolved_first"])

    def test_apply_settings_rejects_invalid_classification_maintenance_ranges(self):
        current = {
            "web": {"require_login_password": False, "password": ""},
            "providers": {
                "pixiv": {"type": "pixiv", "refresh_token": "t", "user_id": 1},
                "danbooru": {"type": "danbooru"},
            },
            "models": {},
            "tag_classifier": {"enabled": True, "ttl_days": 30, "batch_size": 50, "concurrency": 5},
        }
        with self.assertRaisesRegex(ValueError, "tag_classifier.ttl_days"):
            apply_settings_payload(current, {"tag_classifier": {"enabled": True, "ttl_days": 0}}, str)
        with self.assertRaisesRegex(ValueError, "tag_classifier.batch_size"):
            apply_settings_payload(current, {"tag_classifier": {"enabled": True, "batch_size": 0}}, str)
        with self.assertRaisesRegex(ValueError, "tag_classifier.concurrency"):
            apply_settings_payload(current, {"tag_classifier": {"enabled": True, "concurrency": 0}}, str)
        with self.assertRaisesRegex(ValueError, "tag_classifier.maintenance.max_tags_per_run"):
            apply_settings_payload(
                current,
                {"tag_classifier": {"enabled": True, "maintenance": {"max_tags_per_run": 0}}},
                str,
            )

    def test_search_first_requires_compatible_model_and_both_search_provider_pools(self):
        current = {
            "web": {"require_login_password": False, "password": ""},
            "providers": {
                "deepseek": {"type": "openai_compatible", "base_url": "https://api.deepseek.com/v1", "api_key": "k"},
                "brave": {"type": "brave_search", "api_key": "b"},
                "tavily": {"type": "tavily_search", "api_key": "t"},
            },
            "models": {"flash": {"provider": "deepseek", "model": "deepseek-v4-flash", "capabilities": ["llm"]}},
            "tag_classifier": {"judges": [], "grounded_judge": {"backend": "gemini"}},
        }

        merged = apply_settings_payload(current, {"tag_classifier": {"grounded_judge": {
            "backend": "search_first", "search_classifier_model": "flash",
            "brave_providers": ["brave"], "tavily_providers": ["tavily"],
        }}}, str)
        self.assertEqual(merged["tag_classifier"]["grounded_judge"]["backend"], "search_first")

        with self.assertRaisesRegex(ValueError, "Tavily Search"):
            apply_settings_payload(current, {"tag_classifier": {"grounded_judge": {
                "backend": "search_first", "search_classifier_model": "flash",
                "brave_providers": ["brave"], "tavily_providers": [],
            }}}, str)

    def test_apply_settings_rejects_deleting_model_still_referenced(self):
        current = {
            "web": {"require_login_password": False, "password": ""},
            "providers": {
                "pixiv": {"type": "pixiv", "refresh_token": "t", "user_id": 1},
                "danbooru": {"type": "danbooru"},
                "gateway": {"type": "openai_compatible", "base_url": "https://gateway.example/v1", "api_key": "k"},
            },
            "models": {
                "judge": {"provider": "gateway", "model": "gpt", "capabilities": ["llm"]},
                "spare": {"provider": "gateway", "model": "gpt-2", "capabilities": ["llm"]},
            },
            "tag_classifier": {"judges": ["judge"]},
            "profiler": {"ai": {"enabled": False, "model": ""}},
            "ai": {"embedding": {"enabled": False, "model": ""}, "scorer": {"enabled": False, "model": ""}},
        }
        with self.assertRaisesRegex(ValueError, "Model judge"):
            apply_settings_payload(current, {
                "models": {"spare": current["models"]["spare"]},
                "tag_classifier": {"judges": ["judge"]},
            }, str)

    def test_apply_settings_allows_deleting_unreferenced_model_and_provider(self):
        current = {
            "web": {"require_login_password": False, "password": ""},
            "providers": {
                "pixiv": {"type": "pixiv", "refresh_token": "t", "user_id": 1},
                "danbooru": {"type": "danbooru"},
                "gateway": {"type": "openai_compatible", "base_url": "https://gateway.example/v1", "api_key": "k"},
                "extra": {"type": "openai_compatible", "base_url": "https://extra.example/v1", "api_key": "x"},
            },
            "models": {
                "judge": {"provider": "gateway", "model": "gpt", "capabilities": ["llm"]},
                "spare": {"provider": "extra", "model": "gpt-2", "capabilities": ["llm"]},
            },
            "tag_classifier": {"judges": ["judge"]},
            "profiler": {"ai": {"enabled": False, "model": ""}},
            "ai": {"embedding": {"enabled": False, "model": ""}, "scorer": {"enabled": False, "model": ""}},
        }
        without_spare = apply_settings_payload(current, {
            "models": {"judge": current["models"]["judge"]},
            "providers": {
                "pixiv": current["providers"]["pixiv"],
                "danbooru": current["providers"]["danbooru"],
                "gateway": current["providers"]["gateway"],
                "extra": current["providers"]["extra"],
            },
            "tag_classifier": {"judges": ["judge"]},
        }, str)
        self.assertNotIn("spare", without_spare["models"])
        self.assertIn("extra", without_spare["providers"])

        without_extra = apply_settings_payload(without_spare, {
            "models": {"judge": without_spare["models"]["judge"]},
            "providers": {
                "pixiv": without_spare["providers"]["pixiv"],
                "danbooru": without_spare["providers"]["danbooru"],
                "gateway": without_spare["providers"]["gateway"],
            },
            "tag_classifier": {"judges": ["judge"]},
        }, str)
        self.assertNotIn("extra", without_extra["providers"])
        self.assertIn("gateway", without_extra["providers"])

    def test_apply_settings_rejects_deleting_provider_that_still_owns_models(self):
        current = {
            "web": {"require_login_password": False, "password": ""},
            "providers": {
                "pixiv": {"type": "pixiv", "refresh_token": "t", "user_id": 1},
                "danbooru": {"type": "danbooru"},
                "gateway": {"type": "openai_compatible", "base_url": "https://gateway.example/v1", "api_key": "k"},
            },
            "models": {
                "judge": {"provider": "gateway", "model": "gpt", "capabilities": ["llm"]},
            },
            "tag_classifier": {"judges": []},
        }
        with self.assertRaisesRegex(ValueError, "Provider gateway"):
            apply_settings_payload(current, {
                "providers": {
                    "pixiv": current["providers"]["pixiv"],
                    "danbooru": current["providers"]["danbooru"],
                },
                "models": current["models"],
                "tag_classifier": {"judges": []},
            }, str)


if __name__ == "__main__":
    unittest.main()

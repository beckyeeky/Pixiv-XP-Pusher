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

    def test_snapshot_repairs_malformed_random_delay(self):
        snapshot = build_settings_snapshot({"network": {"random_delay": "bad"}})

        self.assertEqual(snapshot["network"]["random_delay"], [1.0, 3.0])

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

        self.assertEqual(redacted["pixiv"]["refresh_token"], "***REDACTED***")
        self.assertEqual(redacted["notifier"]["telegram"]["bot_token"], "***REDACTED***")
        self.assertEqual(redacted["profiler"]["boost_tags"]["cat"], 1.8)


if __name__ == "__main__":
    unittest.main()

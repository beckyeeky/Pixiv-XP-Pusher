import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import config
import get_token
import task_manager
import yaml
from tag_classifier import TagClassifier
from web.settings_editor import apply_settings_payload, redact_sensitive_config


class SingletonProviderTests(unittest.TestCase):
    def test_normalize_migrates_legacy_pixiv_and_danbooru_to_typed_providers(self):
        normalized = config.normalize_config({
            "pixiv": {
                "refresh_token": "pixiv-refresh",
                "sync_token": "pixiv-sync",
                "user_id": 42,
            },
            "profiler": {
                "danbooru_login": "legacy-login",
                "danbooru_api_key": "legacy-key",
            },
            "tag_classifier": {
                "danbooru": {
                    "base_url": "https://danbooru.example",
                    "enabled": True,
                },
            },
        })

        self.assertEqual(
            config.get_singleton_provider(normalized, "pixiv"),
            {
                "type": "pixiv",
                "refresh_token": "pixiv-refresh",
                "sync_token": "pixiv-sync",
                "user_id": 42,
            },
        )
        self.assertEqual(
            config.get_singleton_provider(normalized, "danbooru"),
            {
                "type": "danbooru",
                "login": "legacy-login",
                "api_key": "legacy-key",
                "base_url": "https://danbooru.example",
            },
        )

    def test_typed_singleton_providers_remain_authoritative_over_legacy_fields(self):
        normalized = config.normalize_config({
            "pixiv": {"refresh_token": "legacy-pixiv"},
            "profiler": {"danbooru_api_key": "legacy-danbooru"},
            "providers": {
                "main": {"type": "pixiv", "refresh_token": "typed-pixiv", "user_id": 7},
                "tags": {"type": "danbooru", "api_key": "typed-danbooru", "base_url": "https://typed.example"},
            },
            "tag_classifier": {"danbooru": {"api_key": "stale-danbooru"}},
        })

        self.assertEqual(normalized["pixiv"]["refresh_token"], "typed-pixiv")
        self.assertEqual(normalized["tag_classifier"]["danbooru"]["api_key"], "typed-danbooru")
        self.assertEqual(normalized["tag_classifier"]["danbooru"]["base_url"], "https://typed.example")

    def test_normalize_rejects_duplicate_typed_singleton_providers(self):
        with self.assertRaisesRegex(ValueError, "只能配置一个 Pixiv Provider"):
            config.normalize_config({
                "providers": {
                    "one": {"type": "pixiv"},
                    "two": {"type": "pixiv"},
                },
            })

    def test_settings_rejects_duplicate_typed_singleton_providers(self):
        current = {"web": {"require_login_password": False, "password": ""}}
        payload = {
            "providers": {
                "pixiv_primary": {"type": "pixiv", "refresh_token": "first"},
                "pixiv_secondary": {"type": "pixiv", "refresh_token": "second"},
            },
            "models": {},
        }

        with self.assertRaisesRegex(ValueError, "只能配置一个 Pixiv Provider"):
            apply_settings_payload(current, payload, str)

    def test_singleton_provider_credentials_can_be_replaced_or_explicitly_deleted(self):
        current = {
            "web": {"require_login_password": False, "password": ""},
            "providers": {
                "pixiv": {
                    "type": "pixiv",
                    "refresh_token": "old-refresh",
                    "sync_token": "old-sync",
                },
                "danbooru": {"type": "danbooru", "login": "beck", "api_key": "old-key"},
            },
            "models": {},
        }
        payload = {
            "providers": {
                "pixiv": {
                    "type": "pixiv",
                    "refresh_token": "",
                    "sync_token": "new-sync",
                    "credential_actions": {"refresh_token": "delete", "sync_token": "replace"},
                },
                "danbooru": {
                    "type": "danbooru",
                    "login": "beck",
                    "api_key": "",
                    "credential_actions": {"api_key": "delete"},
                },
            },
            "models": {},
        }

        merged = apply_settings_payload(current, payload, str)

        self.assertEqual(merged["providers"]["pixiv"]["refresh_token"], "")
        self.assertEqual(merged["providers"]["pixiv"]["sync_token"], "new-sync")
        self.assertEqual(merged["providers"]["danbooru"]["api_key"], "")
        self.assertNotIn("credential_actions", merged["providers"]["pixiv"])

    def test_credential_deletion_clears_compatibility_mirrors(self):
        current = config.normalize_config({
            "pixiv": {"refresh_token": "old-refresh"},
            "profiler": {"danbooru_api_key": "old-key"},
            "tag_classifier": {"danbooru": {"api_key": "old-key"}},
            "web": {"require_login_password": False, "password": ""},
        })
        payload = {
            "providers": {
                "pixiv": {"type": "pixiv", "refresh_token": "", "credential_actions": {"refresh_token": "delete"}},
                "danbooru": {"type": "danbooru", "api_key": "", "credential_actions": {"api_key": "delete"}},
            },
            "models": {},
        }

        merged = apply_settings_payload(current, payload, str)

        self.assertEqual(merged["pixiv"]["refresh_token"], "")
        self.assertEqual(merged["profiler"]["danbooru_api_key"], "")
        self.assertEqual(merged["tag_classifier"]["danbooru"]["api_key"], "")

    def test_settings_response_masks_typed_provider_credentials(self):
        redacted = redact_sensitive_config({
            "providers": {
                "pixiv": {"type": "pixiv", "refresh_token": "pixiv-refresh"},
                "danbooru": {"type": "danbooru", "login": "beck", "api_key": "danbooru-key"},
            },
        })

        self.assertEqual(redacted["providers"]["pixiv"]["refresh_token"], "pi…resh")
        self.assertEqual(redacted["providers"]["danbooru"]["api_key"], "da…-key")
        self.assertEqual(redacted["providers"]["danbooru"]["login"], "••••")

    def test_tag_classifier_resolves_danbooru_provider_connection_details(self):
        classifier = TagClassifier({
            "providers": {
                "danbooru": {
                    "type": "danbooru",
                    "login": "beck",
                    "api_key": "secret",
                    "base_url": "https://danbooru.example",
                },
            },
            "danbooru": {"enabled": True},
        })

        self.assertTrue(classifier.danbooru_lookup.enabled)
        self.assertEqual(classifier.danbooru_lookup.login, "beck")
        self.assertEqual(classifier.danbooru_lookup.api_key, "secret")
        self.assertEqual(classifier.danbooru_lookup.base_url, "https://danbooru.example")

    def test_service_setup_resolves_pixiv_typed_provider(self):
        main_client, sync_client = MagicMock(), MagicMock()
        main_client.login, sync_client.login = AsyncMock(), AsyncMock()
        config_with_provider = {
            "providers": {
                "pixiv": {
                    "type": "pixiv",
                    "refresh_token": "main-token",
                    "sync_token": "sync-token",
                },
            },
            "network": {},
            "notifier": {"telegram": {}},
            "profiler": {},
        }

        with patch.object(task_manager, "init_db", new=AsyncMock()), \
             patch.object(task_manager, "PixivClient", side_effect=[main_client, sync_client]) as client_class, \
             patch.object(task_manager, "XPProfiler", return_value=MagicMock()), \
             patch.object(task_manager, "setup_notifiers", new=AsyncMock(return_value=[])):
            asyncio.run(task_manager.setup_services(config_with_provider))

        self.assertEqual(client_class.call_args_list[0].kwargs["refresh_token"], "main-token")
        self.assertEqual(client_class.call_args_list[1].kwargs["refresh_token"], "sync-token")

    def test_token_script_migrates_legacy_pixiv_tokens_before_replacement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("pixiv:\n  refresh_token: old-main\n  sync_token: old-sync\n  user_id: 42\n", encoding="utf-8")
            with patch.object(get_token, "__file__", str(Path(tmpdir) / "get_token.py")):
                get_token.save_to_config(None, "new-main", "99", None, "refresh_token")

            saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["providers"]["pixiv"]["refresh_token"], "new-main")
        self.assertEqual(saved["providers"]["pixiv"]["sync_token"], "old-sync")
        self.assertEqual(saved["providers"]["pixiv"]["user_id"], 42)

    def test_settings_template_keeps_singleton_providers_out_of_llm_configuration(self):
        template = (Path(__file__).resolve().parents[1] / "web" / "templates" / "settings_v2.html").read_text(encoding="utf-8")

        self.assertIn("Pixiv Provider", template)
        self.assertIn("Danbooru Provider", template)
        self.assertIn("singletonProvider('pixiv'", template)
        self.assertIn("singletonProvider('danbooru'", template)
        self.assertIn("!['pixiv', 'danbooru'].includes(provider.type)", template)
        self.assertIn("credential_actions[field]", template)
        self.assertIn("'login', 'Danbooru Login'", template)

    def test_ai_settings_template_explains_provider_model_credential_reuse(self):
        template = (Path(__file__).resolve().parents[1] / "web" / "templates" / "settings_v2.html").read_text(encoding="utf-8")

        self.assertIn("Provider（服务地址 + 凭据） → Model", template)
        self.assertIn("此 Provider 的凭据由", template)
        self.assertIn("API Key 会被此 Model 自动复用", template)
        self.assertIn("modelReferenceLabels", template)
        self.assertIn("标签审查 Judge", template)


if __name__ == "__main__":
    unittest.main()

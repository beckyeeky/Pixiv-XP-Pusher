import re
import tempfile
import unittest
from inspect import signature
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

import database as db_module
import web.app as web_app_module
from web.app import app as canonical_app
from web.app import api_gallery, gallery
from web.app_v2 import app as compat_app


class WebEntrypointTests(unittest.TestCase):
    def test_legacy_entrypoint_reexports_canonical_app(self):
        self.assertIs(canonical_app, compat_app)
        self.assertEqual(canonical_app.title, "Pixiv-XP-Pusher")

    def test_web_app_has_no_merge_conflict_markers(self):
        app_source = Path(__file__).resolve().parents[1] / "web" / "app.py"
        content = app_source.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^<<<<<<< .*", content, re.MULTILINE))
        self.assertIsNone(re.search(r"^>>>>>>> .*", content, re.MULTILINE))
        self.assertIsNone(re.search(r"^=======$", content, re.MULTILINE))

    def test_gallery_defaults_match_five_by_five_layout(self):
        self.assertEqual(signature(gallery).parameters["page"].default.default, 1)
        self.assertEqual(signature(api_gallery).parameters["limit"].default, 25)
        self.assertEqual(signature(db_module.get_push_history_paginated).parameters["limit"].default, 25)


if __name__ == "__main__":
    unittest.main()


class WebSecurityOptionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmpdir.name) / "config.yaml"
        self.config_path.write_text("{}\n", encoding="utf-8")
        self.client = TestClient(canonical_app)
        web_app_module.sessions.clear()
        web_app_module.login_attempts.clear()
        self.config_patch = patch.object(web_app_module, "CONFIG_PATH", self.config_path)
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.tmpdir.cleanup()
        web_app_module.sessions.clear()
        web_app_module.login_attempts.clear()

    def read_config(self):
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}

    def test_setup_can_disable_password_auth(self):
        response = self.client.post(
            "/setup",
            data={"auth_mode": "none", "password": "", "confirm": ""},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        cfg = self.read_config()
        self.assertFalse(cfg["web"]["require_login_password"])
        self.assertEqual(cfg["web"]["password"], "")

    def test_index_skips_login_when_password_auth_disabled(self):
        self.config_path.write_text(
            yaml.safe_dump({"web": {"require_login_password": False, "password": ""}}, allow_unicode=True),
            encoding="utf-8",
        )
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/dashboard")

    def test_settings_api_can_disable_password_auth(self):
        initial = {
            "web": {"require_login_password": True, "password": web_app_module.hash_password("oldpass")},
            "pixiv": {"user_id": 1},
            "scheduler": {"cron": "0 12 * * *"},
            "profiler": {},
            "filter": {},
            "fetcher": {"bookmark_threshold": {}},
            "notifier": {"telegram": {}},
            "network": {},
            "strategies": ["xp_search"],
        }
        self.config_path.write_text(yaml.safe_dump(initial, allow_unicode=True), encoding="utf-8")
        session_id = "test-session"
        web_app_module.sessions[session_id] = web_app_module.datetime.now()
        response = self.client.post(
            "/api/settings",
            cookies={"session_id": session_id},
            json={
                "user_id": 1,
                "cron": "0 12 * * *",
                "ip_weight_discount": 1.0,
                "danbooru_login": "",
                "danbooru_api_key": "",
                "strategies": ["xp_search"],
                "r18_mode": "safe",
                "proxy_url": "",
                "search_limit": 50,
                "date_range_days": 7,
                "bookmark_threshold_search": 100,
                "bookmark_threshold_subscription": 0,
                "bookmark_threshold_related": 50,
                "daily_limit": 20,
                "max_per_artist": 3,
                "exclude_ai": True,
                "skip_ugoira": True,
                "batch_mode": "album",
                "image_quality": 85,
                "max_image_size": 2000,
                "max_concurrency": 5,
                "requests_per_minute": 60,
                "require_login_password": False,
                "web_password": "",
                "web_password_confirm": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        cfg = self.read_config()
        self.assertFalse(cfg["web"]["require_login_password"])
        self.assertEqual(cfg["web"]["password"], "")

    def test_config_api_redacts_sensitive_values(self):
        initial = {
            "web": {"require_login_password": True, "password": "hashed-secret"},
            "pixiv": {"refresh_token": "pixiv-refresh"},
            "notifier": {"telegram": {"bot_token": "telegram-token"}},
            "profiler": {"danbooru_api_key": "db-key", "boost_tags": {"cat": 1.8}},
        }
        self.config_path.write_text(yaml.safe_dump(initial, allow_unicode=True), encoding="utf-8")
        session_id = "test-session"
        web_app_module.sessions[session_id] = web_app_module.datetime.now()

        response = self.client.get("/api/config", cookies={"session_id": session_id})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["web"]["password"], "***REDACTED***")
        self.assertEqual(payload["pixiv"]["refresh_token"], "***REDACTED***")
        self.assertEqual(payload["notifier"]["telegram"]["bot_token"], "***REDACTED***")
        self.assertEqual(payload["profiler"]["danbooru_api_key"], "***REDACTED***")
        self.assertEqual(payload["profiler"]["boost_tags"]["cat"], 1.8)

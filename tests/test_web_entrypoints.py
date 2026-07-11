import re
import asyncio
import tempfile
import unittest
from inspect import signature
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient
from starlette.requests import Request

import database as db_module
import web.app as web_app_module
from web.app import app as canonical_app
from web.app import (
    SettingsRequest, TagReviewRequest, api_gallery, api_tag_reviews, do_setup, gallery,
    get_config_section, get_classification_maintenance_status, index, save_settings,
    submit_tag_review,
)
from web.app_v2 import app as compat_app


class WebEntrypointTests(unittest.TestCase):
    def test_tag_review_apis_delegate_to_review_queue(self):
        with patch.object(web_app_module.db, "get_tag_review_queue", return_value=[{"tag": "needs_review"}]) as get_queue, \
             patch.object(web_app_module.db, "review_tag_classification") as review:
            payload = asyncio.run(api_tag_reviews(limit=25, _=None))
            response = asyncio.run(submit_tag_review(TagReviewRequest(tag="needs_review", classification="feature"), _=None))

        self.assertEqual(payload, {"items": [{"tag": "needs_review"}]})
        get_queue.assert_awaited_once_with(25)
        review.assert_awaited_once_with("needs_review", "feature")
        self.assertTrue(response["success"])

    def test_maintenance_status_api_exposes_settled_delivery_and_background_work(self):
        completion = '{"status": "succeeded", "completed_at": "2026-07-11T10:00:00"}'
        background = '{"status": "failed", "completed_at": "2026-07-11T10:01:00", "error": "offline"}'
        with patch.object(web_app_module.db, "get_state", side_effect=[completion, background]) as get_state:
            payload = asyncio.run(get_classification_maintenance_status(_=None))

        self.assertEqual(payload["completion"]["status"], "succeeded")
        self.assertEqual(payload["background"]["status"], "failed")
        self.assertEqual(payload["background"]["error"], "offline")
        self.assertEqual(
            [call.args[0] for call in get_state.await_args_list],
            ["runtime.last_maintenance_completion", "runtime.last_maintenance_background_status"],
        )

    def test_authenticated_tags_page_contains_the_review_queue_flow(self):
        session_id = "tag-review-session"
        web_app_module.sessions[session_id] = web_app_module.datetime.now()
        try:
            response = TestClient(canonical_app).get("/tags", cookies={"session_id": session_id})
        finally:
            web_app_module.sessions.pop(session_id, None)

        self.assertEqual(response.status_code, 200)
        self.assertIn("标签审核队列", response.text)
        self.assertIn("/api/tag-reviews", response.text)
        self.assertIn("下载待审核 CSV", response.text)
        self.assertIn("/api/tag-reviews/csv", response.text)
        self.assertIn("/api/classification-maintenance-status", response.text)
        self.assertIn("manual decision", response.text)
        self.assertIn("没有待审核的未解决标签", response.text)
        self.assertIn("审核队列加载失败", response.text)
        self.assertIn("正在加载审核队列", response.text)
        self.assertIn("await loadReviewQueue()", response.text)

    def test_authenticated_csv_review_flow_exports_and_applies_only_filled_rows(self):
        async def authenticated():
            return None

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(db_module, "DB_PATH", Path(tmpdir) / "pixiv_xp.db"):
            asyncio.run(db_module.init_db())
            asyncio.run(db_module.update_xp_profile({"first": 4.0, "second": 1.0}))
            asyncio.run(db_module.save_tag_classifications([
                ("first", "unresolved", "ai"),
                ("second", "unresolved", "ai"),
            ]))
            canonical_app.dependency_overrides[web_app_module.require_auth] = authenticated
            try:
                client = TestClient(canonical_app)
                export_response = client.get("/api/tag-reviews/csv")
                import_response = client.post(
                    "/api/tag-reviews/csv",
                    files={"file": ("reviews.csv", "tag,classification\nfirst,feature\nsecond,\n", "text/csv")},
                )
            finally:
                canonical_app.dependency_overrides.pop(web_app_module.require_auth, None)
            queue = asyncio.run(db_module.get_tag_review_queue())

        self.assertEqual(export_response.status_code, 200)
        self.assertIn("tag,classification,profile_weight,classification_source,evidence_summary", export_response.text)
        self.assertIn("first,,4.0,ai,", export_response.text)
        self.assertEqual(import_response.status_code, 200)
        self.assertEqual(import_response.json(), {"success": True, "processed": 1, "skipped": 1})
        self.assertEqual([item["tag"] for item in queue], ["second"])

    def test_csv_review_import_rejects_invalid_rows_without_partial_writes(self):
        async def authenticated():
            return None

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(db_module, "DB_PATH", Path(tmpdir) / "pixiv_xp.db"):
            asyncio.run(db_module.init_db())
            asyncio.run(db_module.save_tag_classifications([
                ("first", "unresolved", "ai"),
                ("second", "unresolved", "ai"),
            ]))
            canonical_app.dependency_overrides[web_app_module.require_auth] = authenticated
            try:
                response = TestClient(canonical_app).post(
                    "/api/tag-reviews/csv",
                    files={"file": ("reviews.csv", "tag,classification\nfirst,feature\nsecond,not-a-category\n", "text/csv")},
                )
            finally:
                canonical_app.dependency_overrides.pop(web_app_module.require_auth, None)
            queue = asyncio.run(db_module.get_tag_review_queue())

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["errors"][0]["line"], 3)
        self.assertEqual({item["tag"] for item in queue}, {"first", "second"})

    def test_authenticated_review_api_flow_returns_queue_and_persists_a_manual_decision(self):
        async def authenticated():
            return None

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(db_module, "DB_PATH", Path(tmpdir) / "pixiv_xp.db"):
            asyncio.run(db_module.init_db())
            asyncio.run(db_module.update_xp_profile({"needs_review": 3.0}))
            asyncio.run(db_module.save_tag_classifications([("needs_review", "unresolved", "evidence_unresolved")]))
            asyncio.run(db_module.save_tag_evidence([("needs_review", "danbooru", "character", 1.0)]))
            canonical_app.dependency_overrides[web_app_module.require_auth] = authenticated
            try:
                client = TestClient(canonical_app)
                queue_response = client.get("/api/tag-reviews")
                decision_response = client.post("/api/tag-reviews", json={
                    "tag": "needs_review", "classification": "feature",
                })
            finally:
                canonical_app.dependency_overrides.pop(web_app_module.require_auth, None)
            remaining = asyncio.run(db_module.get_tag_review_queue())
            evidence = asyncio.run(db_module.get_tag_evidence(["needs_review"]))

        self.assertEqual(queue_response.status_code, 200)
        self.assertEqual(queue_response.json()["items"][0]["tag"], "needs_review")
        self.assertEqual(decision_response.status_code, 200)
        self.assertEqual(remaining, [])
        self.assertIn({"source": "manual", "classification": "feature", "confidence": 1.0}, evidence["needs_review"])

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

    def make_request(self, cookies: dict[str, str] | None = None) -> Request:
        cookie_header = b""
        if cookies:
            cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()

        headers = []
        if cookie_header:
            headers.append((b"cookie", cookie_header))

        return Request({
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "query_string": b"",
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
        })

    def test_setup_can_disable_password_auth(self):
        response = asyncio.run(do_setup(auth_mode="none", password="", confirm=""))
        self.assertEqual(response.status_code, 303)
        cfg = self.read_config()
        self.assertFalse(cfg["web"]["require_login_password"])
        self.assertEqual(cfg["web"]["password"], "")

    def test_index_skips_login_when_password_auth_disabled(self):
        self.config_path.write_text(
            yaml.safe_dump({"web": {"require_login_password": False, "password": ""}}, allow_unicode=True),
            encoding="utf-8",
        )
        response = asyncio.run(index(self.make_request()))
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
        asyncio.run(web_app_module.require_auth(self.make_request({"session_id": session_id})))
        response = asyncio.run(
            save_settings(
                SettingsRequest(
                    user_id=1,
                    cron="0 12 * * *",
                    ip_weight_discount=1.0,
                    danbooru_login="",
                    danbooru_api_key="",
                    strategies=["xp_search"],
                    r18_mode="safe",
                    proxy_url="",
                    search_limit=50,
                    date_range_days=7,
                    bookmark_threshold_search=100,
                    bookmark_threshold_subscription=0,
                    bookmark_threshold_related=50,
                    daily_limit=20,
                    max_per_artist=3,
                    exclude_ai=True,
                    skip_ugoira=True,
                    batch_mode="album",
                    image_quality=85,
                    max_image_size=2000,
                    max_concurrency=5,
                    requests_per_minute=60,
                    require_login_password=False,
                    web_password="",
                    web_password_confirm="",
                )
            )
        )
        self.assertTrue(response["success"])
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

        asyncio.run(web_app_module.require_auth(self.make_request({"session_id": session_id})))
        payload = asyncio.run(get_config_section(section=None))
        self.assertEqual(payload["web"]["password"], "••••")
        self.assertEqual(payload["pixiv"]["refresh_token"], "pi…resh")
        self.assertEqual(payload["notifier"]["telegram"]["bot_token"], "••••")
        self.assertEqual(payload["profiler"]["danbooru_api_key"], "••••")
        self.assertEqual(payload["profiler"]["boost_tags"]["cat"], 1.8)

    def test_settings_page_does_not_embed_a_provider_secret(self):
        self.config_path.write_text(yaml.safe_dump({
            "web": {"require_login_password": False, "password": ""},
            "providers": {"gateway": {"type": "openai_compatible", "base_url": "https://example.test/v1", "api_key": "sk-super-secret"}},
            "models": {"judge": {"provider": "gateway", "model": "gpt-4o-mini"}},
            "tag_classifier": {"judges": ["judge"]},
        }, allow_unicode=True), encoding="utf-8")

        response = TestClient(canonical_app).get("/settings")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("sk-super-secret", response.text)
        self.assertIn("sk…cret", response.text)

    def test_settings_page_renders_with_sparse_config(self):
        self.config_path.write_text(
            yaml.safe_dump({"web": {"require_login_password": False, "password": ""}}, allow_unicode=True),
            encoding="utf-8",
        )

        client = TestClient(canonical_app)
        response = client.get("/settings")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Pixiv Provider", response.text)
        self.assertIn('data-section="pixiv"', response.text)
        self.assertIn("gpt-4o-mini", response.text)
        self.assertIn("text-embedding-3-small", response.text)
        self.assertIn("profiler_ai_model", response.text)
        self.assertNotIn("profiler_ai_api_key", response.text)
        self.assertIn("tag_classifier_enabled", response.text)
        self.assertIn("tag_classifier_ttl_days", response.text)
        self.assertIn("tag_classifier_max_tags_per_run", response.text)
        self.assertIn("removeProvider(", response.text)
        self.assertIn("removeModel(", response.text)

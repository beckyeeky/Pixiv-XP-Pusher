import re
import asyncio
import json
import tempfile
import unittest
from inspect import signature
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import yaml
from fastapi import HTTPException
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
from tag_relationship_judge import MERGE_PRINCIPLES_VERSION, relationship_evidence_hash


class WebEntrypointTests(unittest.TestCase):
    def test_dashboard_preview_supports_tag_and_artist_targets(self):
        async def authenticated():
            return None

        canonical_app.dependency_overrides[web_app_module.require_auth] = authenticated
        try:
            with patch.object(
                web_app_module.db,
                "get_recent_liked_illusts_for_tag",
                new=AsyncMock(return_value=[101, 102]),
            ) as tag_preview, patch.object(
                web_app_module.db,
                "get_recent_liked_illusts_for_artist",
                new=AsyncMock(return_value=[201]),
            ) as artist_preview, TestClient(canonical_app) as client:
                tag_response = client.get(
                    "/api/dashboard/tag-preview",
                    params={"kind": "tag", "value": "white_hair"},
                )
                artist_response = client.get(
                    "/api/dashboard/tag-preview",
                    params={"kind": "artist", "value": "42"},
                )
        finally:
            canonical_app.dependency_overrides.pop(web_app_module.require_auth, None)

        self.assertEqual(tag_response.json()["illust_ids"], [101, 102])
        self.assertEqual(artist_response.json()["illust_ids"], [201])
        tag_preview.assert_awaited_once_with("white_hair", limit=3)
        artist_preview.assert_awaited_once_with(42, limit=3)

    def test_authenticated_settings_page_separates_ai_provider_model_and_features(self):
        template = (Path(web_app_module.TEMPLATES_DIR) / "settings_v2.html").read_text(encoding="utf-8")
        for marker in (
            'data-section="providers"',
            'data-section="models"',
            'data-section="ai"',
            'id="section-providers"',
            'id="section-models"',
            'id="providerDialog"',
            'id="modelDialog"',
            'id="tag_classifier_search_model"',
            'id="brave_provider_selection"',
            'id="tavily_provider_selection"',
            'value="brave_search"',
            'value="tavily_search"',
            '此流程不会调用 Gemini',
            'Gemini Model 和 Gemini API Key 均不需要设置',
            '智能精排',
        ):
            self.assertIn(marker, template)
        self.assertNotIn('Gemini backend Model', template)
        self.assertNotIn('id="tag_classifier_grounded_backend"', template)

    def test_authenticated_single_tag_grounded_judge_uses_translation_and_activates_valid_result(self):
        async def authenticated():
            return None

        async def classify(tag, config):
            self.assertEqual(tag, "white_hair")
            return {
                "tag": "white_hair", "classification": "feature",
                "explanation": "A transferable visual trait.", "languages": "en",
                "usage": {"input": 11, "output": 7, "thoughts": 0, "tool_use_prompt": 3, "total": 21, "search_queries": 1},
                "status": "accepted",
            }

        canonical_app.dependency_overrides[web_app_module.require_auth] = authenticated
        try:
            maintenance = Mock()
            maintenance.classify_tag = AsyncMock(
                side_effect=lambda tag: classify(tag, {})
            )
            with patch.object(
                web_app_module,
                "ClassificationMaintenance",
                return_value=maintenance,
            ):
                with TestClient(canonical_app) as client:
                    response = client.post("/api/tag-reviews/white_hair/classify")
        finally:
            canonical_app.dependency_overrides.pop(web_app_module.require_auth, None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["classification"], "feature")
        self.assertEqual(response.json()["usage"], {
            "input": 11, "output": 7, "thoughts": 0, "tool_use_prompt": 3, "total": 21, "search_queries": 1,
        })
        maintenance.classify_tag.assert_awaited_once_with("white_hair")

    def test_grounded_judge_invalid_result_leaves_tag_in_review_queue(self):
        async def authenticated():
            return None

        canonical_app.dependency_overrides[web_app_module.require_auth] = authenticated
        try:
            maintenance = Mock()
            maintenance.classify_tag = AsyncMock(
                side_effect=ValueError("Grounded Judge 明确标为 unresolved")
            )
            with patch.object(web_app_module, "ClassificationMaintenance", return_value=maintenance):
                with TestClient(canonical_app) as client:
                    response = client.post("/api/tag-reviews/ambiguous/classify")
        finally:
            canonical_app.dependency_overrides.pop(web_app_module.require_auth, None)

        self.assertEqual(response.status_code, 422)
        maintenance.classify_tag.assert_awaited_once_with("ambiguous")

    def test_grounded_judge_does_not_replace_a_human_decision(self):
        async def authenticated():
            return None

        result = {"tag": "reviewed", "classification": "feature", "explanation": "Trait.", "languages": "en", "status": "human_override"}
        canonical_app.dependency_overrides[web_app_module.require_auth] = authenticated
        try:
            maintenance = Mock()
            maintenance.classify_tag = AsyncMock(return_value=result)
            with patch.object(web_app_module, "ClassificationMaintenance", return_value=maintenance):
                with TestClient(canonical_app) as client:
                    response = client.post("/api/tag-reviews/reviewed/classify")
        finally:
            canonical_app.dependency_overrides.pop(web_app_module.require_auth, None)

        self.assertEqual(response.status_code, 409)
        maintenance.classify_tag.assert_awaited_once_with("reviewed")

    def test_authenticated_bulk_grounded_judge_reports_mixed_outcomes(self):
        summary = {
            "attempted": 3, "accepted": 1, "unresolved": 1, "failed": 1,
            "human_override": 0,
            "usage": {"input": 4, "output": 3, "thoughts": 2, "tool_use_prompt": 1, "total": 10, "search_queries": 2},
            "items": [],
        }
        runtime_config = {"tag_classifier": {"maintenance": {
            "max_tags_per_run": 3, "min_profile_weight": 1.5, "concurrency": 2,
        }}}

        manager = Mock()
        manager.run_eligible = AsyncMock(return_value={
            **summary,
            "requested_limit": 2,
            "effective_limit": 2,
            "configured_limit": 3,
            "min_profile_weight": 1.5,
        })
        with patch.object(web_app_module, "load_config", return_value=runtime_config), \
             patch.object(web_app_module, "ClassificationMaintenance", return_value=manager):
            response = asyncio.run(web_app_module.classify_all_tag_reviews(limit=2, _=None))

        self.assertEqual(response["attempted"], 3)
        self.assertEqual(response["accepted"], 1)
        self.assertEqual(response["unresolved"], 1)
        self.assertEqual(response["failed"], 1)
        self.assertEqual(response["usage"], {"input": 4, "output": 3, "thoughts": 2, "tool_use_prompt": 1, "total": 10, "search_queries": 2})
        self.assertEqual(response["requested_limit"], 2)
        self.assertEqual(response["effective_limit"], 2)
        self.assertEqual(response["configured_limit"], 3)
        manager.run_eligible.assert_awaited_once_with(2)
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
        summary = '{"attempted": 2, "accepted": 1, "usage": {"total": 12, "search_queries": 1}}'
        with patch.object(web_app_module.db, "get_state", side_effect=[completion, background, summary]) as get_state:
            payload = asyncio.run(get_classification_maintenance_status(_=None))

        self.assertEqual(payload["completion"]["status"], "succeeded")
        self.assertEqual(payload["background"]["status"], "failed")
        self.assertEqual(payload["background"]["error"], "offline")
        self.assertEqual(payload["summary"]["usage"]["search_queries"], 1)
        self.assertEqual(
            [call.args[0] for call in get_state.await_args_list],
            ["runtime.last_maintenance_completion", "runtime.last_maintenance_background_status", "runtime.last_classification_maintenance_summary"],
        )

    def test_maintenance_status_tolerates_a_malformed_summary(self):
        with patch.object(web_app_module.db, "get_state", side_effect=[None, None, "not json"]):
            payload = asyncio.run(get_classification_maintenance_status(_=None))
        self.assertEqual(payload["summary"]["status"], "unknown")

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
        self.assertIn("标签映射候选", response.text)
        self.assertIn("/api/tag-mapping-candidates", response.text)
        self.assertIn("AI Relationship Recommendation", response.text)
        self.assertIn("item.ai_relation", response.text)

    def test_tags_page_contains_paginated_alias_manager(self):
        template = (Path(web_app_module.TEMPLATES_DIR) / "tags.html").read_text(encoding="utf-8")
        self.assertIn('id="activeAliasSearch"', template)
        self.assertIn('id="activeAliasCount"', template)
        self.assertIn('id="loadMoreAliases"', template)
        self.assertIn("activeAliasPageSize = 20", template)

    def test_tags_page_contains_preview_first_ai_batch_controls(self):
        template = (Path(web_app_module.TEMPLATES_DIR) / "tags.html").read_text(encoding="utf-8")
        self.assertIn('id="previewAiMappingBatchButton"', template)
        self.assertIn('id="aiMappingBatchPreview"', template)
        self.assertIn("预览 AI 安全建议", template)
        self.assertIn("确认批量应用", template)
        self.assertIn("/api/tag-mapping-ai-batch-preview", template)
        self.assertIn("/api/tag-mapping-ai-batch-apply", template)

    def test_tags_page_has_explicit_bounded_ai_tag_batch_control(self):
        template = (Path(web_app_module.TEMPLATES_DIR) / "tags.html").read_text(encoding="utf-8")

        self.assertIn("🤖 AI 批量处理标签", template)
        self.assertIn('id="tagAiBatchLimit"', template)
        self.assertIn('id="tagAiBatchButton"', template)
        self.assertIn("开始 AI 批量处理", template)
        self.assertIn("不会处理低于权重阈值的标签", template)

    def test_authenticated_alias_api_returns_searchable_page_and_total(self):
        aliases = [{
            "original_tag": "原神", "normalized_tag": "genshin_impact",
            "kind": "equivalent", "source": "manual", "priority": 1,
            "updated_at": "2026-07-18 10:00:00",
        }]
        with patch.object(
            web_app_module.db, "list_tag_aliases", new=AsyncMock(return_value=aliases),
        ) as list_aliases, patch.object(
            web_app_module.db, "count_tag_aliases", new=AsyncMock(return_value=17),
        ) as count_aliases:
            payload = asyncio.run(web_app_module.api_tag_aliases(
                limit=20, offset=20, q=" 原神 ", _=None,
            ))

        self.assertEqual(payload, {
            "items": aliases, "total": 17, "limit": 20, "offset": 20,
            "query": "原神",
        })
        list_aliases.assert_awaited_once_with(limit=20, offset=20, query="原神")
        count_aliases.assert_awaited_once_with(query="原神")

    def test_authenticated_mapping_review_accepts_only_an_explicit_human_decision(self):
        async def authenticated():
            return None

        canonical_app.dependency_overrides[web_app_module.require_auth] = authenticated
        try:
            with patch.object(
                web_app_module.db,
                "get_tag_mapping_candidates",
                new=AsyncMock(return_value=[{"id": 7, "original_tag": "ブルアカ"}]),
            ) as get_candidates, patch.object(
                web_app_module.db,
                "review_tag_mapping_candidate",
                new=AsyncMock(return_value={
                    "id": 7, "status": "accepted", "original_tag": "ブルアカ",
                    "normalized_tag": "blue_archive", "kind": "equivalent",
                }),
            ) as review_candidate:
                client = TestClient(canonical_app)
                queue_response = client.get("/api/tag-mapping-candidates")
                decision_response = client.post(
                    "/api/tag-mapping-candidates/7",
                    json={"decision": "accept", "kind": "equivalent"},
                )
        finally:
            canonical_app.dependency_overrides.pop(web_app_module.require_auth, None)

        self.assertEqual(queue_response.status_code, 200)
        self.assertEqual(decision_response.status_code, 200)
        get_candidates.assert_awaited_once_with(limit=100)
        review_candidate.assert_awaited_once_with(7, "accept", kind="equivalent")

    def test_authenticated_legacy_filter_batch_rejects_only_the_quarantined_rows(self):
        async def authenticated():
            return None

        canonical_app.dependency_overrides[web_app_module.require_auth] = authenticated
        try:
            with patch.object(
                web_app_module.db,
                "reject_legacy_filtered_tag_mapping_candidates",
                new=AsyncMock(return_value=4),
            ) as reject_legacy:
                with TestClient(canonical_app) as client:
                    response = client.post(
                        "/api/tag-mapping-candidates/reject-legacy-filtered",
                    )
        finally:
            canonical_app.dependency_overrides.pop(web_app_module.require_auth, None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "rejected": 4})
        reject_legacy.assert_awaited_once()

    def test_authenticated_ai_batch_preview_returns_only_safe_actions_and_confirmation_token(self):
        checks = json.dumps({
            "same_identity": True,
            "broader_narrower": False,
            "entity_franchise": False,
            "modifier_variant": False,
        })
        equivalent = {
            "id": 7, "original_tag": "白髪", "proposed_normalized_tag": "white_hair",
            "source": "test", "explanation": "translation", "occurrence_count": 2,
            "embedding_similarity": None, "original_classification": "feature",
            "target_classification": "feature", "original_weight": 2.0,
            "target_weight": 3.0, "ai_recommendation_id": 17,
            "ai_relation": "equivalent", "ai_confidence": 0.98,
            "ai_rationale": "same hair colour", "ai_canonical_tag": "white_hair",
            "ai_risk_flags": "[]", "ai_principle_checks": checks,
            "ai_principles_version": MERGE_PRINCIPLES_VERSION,
        }
        equivalent["ai_evidence_hash"] = relationship_evidence_hash(equivalent)
        distinct = {
            **equivalent, "id": 8, "original_tag": "clorinde",
            "proposed_normalized_tag": "genshin_impact", "ai_recommendation_id": 18,
            "ai_relation": "distinct", "ai_confidence": 0.99,
            "ai_rationale": "character is not franchise", "ai_canonical_tag": None,
        }
        distinct["ai_evidence_hash"] = relationship_evidence_hash(distinct)
        uncertain = {
            **equivalent, "id": 9, "original_tag": "ambiguous",
            "proposed_normalized_tag": "other", "ai_recommendation_id": 19,
            "ai_relation": "uncertain", "ai_confidence": 0.99,
        }
        uncertain["ai_evidence_hash"] = relationship_evidence_hash(uncertain)

        with patch.object(
            web_app_module.db,
            "get_tag_mapping_candidates_sync",
            return_value=[equivalent, distinct, uncertain],
        ):
            payload = asyncio.run(web_app_module.preview_tag_mapping_ai_batch(
                min_confidence=0.95, _=None,
            ))

        self.assertEqual(payload["summary"], {
            "eligible": 2, "accept_equivalent": 1, "reject": 1,
        })
        self.assertEqual(
            [(item["candidate_id"], item["decision"]) for item in payload["items"]],
            [(7, "accept_equivalent"), (8, "reject")],
        )
        self.assertEqual(payload["blocked"], {"no_actionable_recommendation": 1})
        self.assertGreater(len(payload["preview_token"]), 20)

    def test_authenticated_ai_batch_apply_requires_matching_preview_token(self):
        candidate = {
            "id": 7, "original_tag": "白髪", "proposed_normalized_tag": "white_hair",
            "source": "test", "explanation": "translation", "occurrence_count": 2,
            "embedding_similarity": None, "original_classification": "feature",
            "target_classification": "feature", "original_weight": 2.0,
            "target_weight": 3.0, "ai_recommendation_id": 17,
            "ai_relation": "equivalent", "ai_confidence": 0.98,
            "ai_rationale": "same hair colour", "ai_canonical_tag": "white_hair",
            "ai_risk_flags": "[]", "ai_principle_checks": json.dumps({
                "same_identity": True, "broader_narrower": False,
                "entity_franchise": False, "modifier_variant": False,
            }),
            "ai_principles_version": MERGE_PRINCIPLES_VERSION,
        }
        candidate["ai_evidence_hash"] = relationship_evidence_hash(candidate)
        applied = {
            "accepted_equivalent": 1, "rejected": 0, "aliases_created": 1,
            "aliases_already_active": 0, "duplicate_candidates_resolved": 0,
        }

        with patch.object(
            web_app_module.db,
            "get_tag_mapping_candidates_sync",
            return_value=[candidate],
        ), patch.object(
            web_app_module.db,
            "apply_tag_mapping_ai_batch_sync",
            return_value=applied,
        ) as apply_batch:
            preview = asyncio.run(web_app_module.preview_tag_mapping_ai_batch(
                min_confidence=0.95, _=None,
            ))
            with self.assertRaises(HTTPException) as stale:
                asyncio.run(web_app_module.apply_tag_mapping_ai_batch(
                    web_app_module.TagMappingAiBatchApplyRequest(
                        min_confidence=0.95,
                        preview_token="stale-token-long-enough",
                        confirm=True,
                    ),
                    _=None,
                ))
            confirmed = asyncio.run(web_app_module.apply_tag_mapping_ai_batch(
                web_app_module.TagMappingAiBatchApplyRequest(
                    min_confidence=0.95,
                    preview_token=preview["preview_token"],
                    confirm=True,
                ),
                _=None,
            ))

        self.assertEqual(stale.exception.status_code, 409)
        self.assertEqual(confirmed, {"success": True, **applied})
        apply_batch.assert_called_once()

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
        self.assertIn("AI 凭据、模型与用途", response.text)
        self.assertIn("Provider（服务地址 + 凭据） → Model", response.text)
        self.assertIn("API Key 会被此 Model 自动复用", response.text)

"""Web API and Telegram entry points must call the same maintenance backend.

Every Bot action here runs against a stand-in (`AsyncMock` / patched
`ClassificationMaintenance`): no Telegram client, no HTTP request, no model or
search call is created. The Web handlers are invoked through FastAPI's
TestClient with the real routes, and the maintenance object is replaced at the
same boundary the production code uses.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

from classification_maintenance import (
    OUTCOME_LABELS,
    classification_backend_view,
    outcome_line,
)

try:
    from fastapi.testclient import TestClient
    import web.app as web_app_module
    from web.app import app as canonical_app
except ImportError:  # pragma: no cover - minimal env without the Web stack
    TestClient = None
    web_app_module = None
    canonical_app = None

try:
    from notifier.telegram import TelegramNotifier
except ImportError:  # pragma: no cover - minimal env without python-telegram-bot
    TelegramNotifier = None


def native_runtime_config() -> dict:
    return {
        "providers": {
            "exa_1": {"type": "exa_search", "api_key": "exa-secret"},
            "tavily_1": {"type": "tavily_search", "api_key_env": "TAVILY_API_KEY"},
            "brave_1": {"type": "brave_search", "api_key": "brave-secret"},
            "openrouter": {"type": "openai_compatible", "base_url": "https://openrouter.ai/api/v1"},
            "deepseek": {"type": "deepseek", "base_url": "https://api.deepseek.com/v1"},
        },
        "tag_classifier": {
            "enabled": True,
            "maintenance": {
                "max_tags_per_run": 5,
                "concurrency": 1,
                "min_profile_weight": 1.0,
                "allow_automatic_external_search": False,
            },
            "grounded_judge": {
                "backend": "native_jev",
                "search_classifier_model": "deepseek_v4",
                "brave_providers": ["brave_1"],
                "tavily_providers": ["tavily_1"],
                "native_jev": {
                    "enabled": True,
                    "model": "jev-1.13",
                    "jev_provider": "openrouter",
                    "exa_providers": ["exa_1"],
                    "tavily_providers": ["tavily_1"],
                    "deepseek_search_provider": "deepseek",
                    "min_confidence": 0.9,
                    "min_agreeing_sources": 2,
                },
            },
        },
    }


SUMMARY = {
    "attempted": 5,
    "accepted": 1,
    "unresolved": 1,
    "deferred": 1,
    "failed": 0,
    "preserved": 1,
    "human_override": 1,
    "usage": {"input": 10, "output": 5, "total": 15, "search_queries": 2},
    "items": [],
}


@unittest.skipIf(TestClient is None, "fastapi is not installed")
class WebMaintenanceEntryTests(unittest.TestCase):
    def setUp(self):
        async def authenticated():
            return None

        canonical_app.dependency_overrides[web_app_module.require_auth] = authenticated
        self.addCleanup(canonical_app.dependency_overrides.pop, web_app_module.require_auth, None)

    def test_bulk_endpoint_returns_the_shared_backend_line_and_preserved_count(self):
        manager = Mock()
        manager.run_eligible = AsyncMock(return_value=dict(SUMMARY, requested_limit=5,
                                                           effective_limit=5,
                                                           configured_limit=5,
                                                           min_profile_weight=1.0))
        config = native_runtime_config()
        with patch.object(web_app_module, "load_config", return_value=config), \
             patch.object(web_app_module, "ClassificationMaintenance", return_value=manager):
            with TestClient(canonical_app) as client:
                response = client.post("/api/tag-reviews/ai-process-all?limit=5")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["backend"], "native_jev")
        self.assertEqual(payload["backend_label"], classification_backend_view(config)["backend_label"])
        self.assertEqual(payload["backend_line"], classification_backend_view(config)["line"])
        self.assertEqual(payload["preserved"], 1)
        self.assertEqual(payload["outcome_text"], outcome_line(SUMMARY, include_zero=True))
        self.assertIn(OUTCOME_LABELS["preserved"], payload["outcome_text"])
        self.assertNotIn("exa-secret", response.text)
        # The Web batch goes through exactly the shared maintenance object.
        manager.run_eligible.assert_awaited_once_with(5)

    def test_single_tag_endpoint_uses_the_shared_maintenance_object(self):
        manager = Mock()
        manager.classify_tag = AsyncMock(return_value={
            "tag": "white_hair", "classification": "feature", "explanation": "Trait.",
            "languages": "en", "status": "accepted", "usage": {"total": 3},
        })
        with patch.object(web_app_module, "load_config", return_value=native_runtime_config()), \
             patch.object(web_app_module, "ClassificationMaintenance", return_value=manager):
            with TestClient(canonical_app) as client:
                response = client.post("/api/tag-reviews/white_hair/classify")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "accepted")
        manager.classify_tag.assert_awaited_once_with("white_hair")

    def test_deferred_single_tag_reports_the_backend_without_spoiling_the_queue(self):
        from grounded_judge import GroundedJudgeDeferredError

        manager = Mock()
        manager.classify_tag = AsyncMock(side_effect=GroundedJudgeDeferredError("budget_exhausted"))
        with patch.object(web_app_module, "load_config", return_value=native_runtime_config()), \
             patch.object(web_app_module, "ClassificationMaintenance", return_value=manager):
            with TestClient(canonical_app) as client:
                response = client.post("/api/tag-reviews/white_hair/classify")

        self.assertEqual(response.status_code, 503)
        detail = response.json()["detail"]
        self.assertIn("分类暂缓", detail)
        self.assertIn("native_jev", detail)
        self.assertIn("标签保持原状态", detail)

    def test_preserved_single_tag_is_reported_as_preserved_not_accepted(self):
        manager = Mock()
        manager.classify_tag = AsyncMock(return_value={
            "tag": "white_hair", "classification": "character", "explanation": "-",
            "languages": "en", "status": "preserved",
            "preserved_classification": "feature", "preserved_source": "ai",
        })
        with patch.object(web_app_module, "load_config", return_value=native_runtime_config()), \
             patch.object(web_app_module, "ClassificationMaintenance", return_value=manager):
            with TestClient(canonical_app) as client:
                response = client.post("/api/tag-reviews/white_hair/classify")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "preserved")
        self.assertEqual(response.json()["preserved_classification"], "feature")


@unittest.skipIf(TelegramNotifier is None, "python-telegram-bot is not installed")
class TelegramTextParityTests(unittest.IsolatedAsyncioTestCase):
    def test_overview_names_the_native_backend_and_the_shared_limits(self):
        config = native_runtime_config()
        view = classification_backend_view(config)
        text = TelegramNotifier._tag_review_overview_text(7, 2, 5, 1.0, backend_view=view)
        self.assertIn("原生 Jev", text)
        self.assertIn("当前待人工决定标签：*7* 个", text)
        self.assertIn("单次配置上限 5", text)
        self.assertNotIn("Brave/Tavily 搜索与 DeepSeek 分类", text)

    def test_overview_without_a_view_keeps_the_old_contract(self):
        text = TelegramNotifier._tag_review_overview_text(3, 1, 20, 2.0)
        self.assertIn("当前待人工决定标签：*3* 个", text)
        self.assertNotIn("Brave/Tavily", text)

    async def test_batch_confirmation_and_result_use_the_shared_backend_text(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier._tag_review_batch_running = False
        notifier.batch_mode = "single"
        query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())
        config = native_runtime_config()

        with patch("database.get_tag_review_count", new=AsyncMock(return_value=4)), \
             patch("database.get_high_weight_unclassified_profile_tags",
                   new=AsyncMock(return_value=[{"tag": "a"}, {"tag": "b"}])), \
             patch("database.set_state", new=AsyncMock()), \
             patch("notifier.telegram.load_config", return_value=config), \
             patch("classification_maintenance.run_scheduled_maintenance",
                   new=AsyncMock(return_value=SUMMARY)) as run:
            await notifier._handle_menu_callback(query, "menu:tag_review:run")
            await notifier._handle_menu_callback(query, "menu:tag_review:run:pick_5")
            await notifier._handle_menu_callback(query, "menu:tag_review:run:confirm_5")

        limit_text = query.edit_message_text.await_args_list[0].args[0]
        confirm_text = query.edit_message_text.await_args_list[1].args[0]
        result_text = query.edit_message_text.await_args_list[-1].args[0]

        self.assertIn("本次安全上限", limit_text)
        self.assertIn("原生 Jev", confirm_text)
        self.assertIn("第三兜底", confirm_text)
        self.assertIn("自动外搜：已关闭", confirm_text)
        self.assertNotIn("Brave/Tavily 搜索与 DeepSeek 分类", confirm_text)
        # Every outcome counter, including preserved, is reported.
        for label in OUTCOME_LABELS.values():
            self.assertIn(label, result_text)
        self.assertIn("保留既有分类 1", result_text)
        self.assertIn("原生 Jev", result_text)
        self.assertEqual(result_text.count("已接受"), 1)
        run.assert_awaited_once_with(["a", "b"], config, ANY, concurrency=1)
        self.assertFalse(notifier._tag_review_batch_running)

    async def test_high_weight_confirmation_result_reports_preserved(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier._tag_review_batch_running = False
        query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())
        query.message = SimpleNamespace(chat_id=99)
        config = native_runtime_config()
        candidates = [{"tag": "a", "profile_weight": 3.0}]

        with patch("database.get_high_weight_unclassified_profile_tags",
                   new=AsyncMock(return_value=candidates)), \
             patch("database.get_high_weight_unclassified_profile_tags_readonly",
                   new=Mock(return_value=candidates)), \
             patch("database.set_state", new=AsyncMock()), \
             patch("notifier.telegram.load_config", return_value=config), \
             patch("classification_maintenance.run_scheduled_maintenance",
                   new=AsyncMock(return_value=SUMMARY)):
            await notifier._handle_menu_callback(query, "menu:tag_review:high_weight")
            await notifier._handle_menu_callback(query, "menu:tag_review:high_weight:confirm")

        listing = query.edit_message_text.await_args_list[0].args[0]
        result = query.edit_message_text.await_args_list[-1].args[0]
        self.assertIn("后端：", listing)
        self.assertIn("原生 Jev", listing)
        self.assertIn("保留既有分类 1", result)
        self.assertIn("人工决定优先 1", result)


if __name__ == "__main__":
    unittest.main()

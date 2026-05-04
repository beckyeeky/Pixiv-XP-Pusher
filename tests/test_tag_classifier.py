import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)

import database
from tag_classifier import TagClassification, TagClassifier


class TagClassifierTests(unittest.TestCase):
    def test_invalid_numeric_config_uses_defaults(self):
        classifier = TagClassifier(
            {"enabled": False, "ttl_days": "bad", "batch_size": 0, "concurrency": True}
        )
        self.assertEqual(classifier.ttl_days, 30)
        self.assertEqual(classifier.batch_size, 1)
        self.assertEqual(classifier.concurrency, 5)

    def test_disabled_classifier_falls_back_to_manual_ip_list(self):
        async def _run():
            classifier = TagClassifier({"enabled": False}, ip_tags=["blue_archive"])
            result = await classifier.classify_tags(["blue_archive", "pantyhose"])
            self.assertEqual(result["blue_archive"].classification, "ip")
            self.assertEqual(result["blue_archive"].source, "manual")
            self.assertEqual(result["pantyhose"].classification, "feature")

        with patch.object(database, "get_tag_classifications", new=AsyncMock(return_value={})), \
             patch.object(database, "save_tag_classifications", new=AsyncMock()):
            asyncio.run(_run())

    def test_cached_classification_is_reused(self):
        async def _run():
            classifier = TagClassifier({"enabled": False}, ip_tags=["blue_archive"])
            result = await classifier.classify_tags(["blue_archive"])
            self.assertEqual(result["blue_archive"].classification, "ip")
            self.assertEqual(result["blue_archive"].source, "ai")

        with patch.object(
            database,
            "get_tag_classifications",
            new=AsyncMock(return_value={"blue_archive": {"classification": "ip", "source": "ai"}}),
        ), patch.object(database, "save_tag_classifications", new=AsyncMock()) as mock_save:
            asyncio.run(_run())
            mock_save.assert_not_awaited()

    def test_enabled_classifier_rechecks_fallback_cache(self):
        async def _run():
            classifier = TagClassifier(
                {"enabled": True, "api_key": "test-key"},
                ip_tags=["blue_archive"],
            )
            classifier.client = object()
            classifier._classify_with_ai = AsyncMock(
                return_value={"blue_archive": TagClassification("ip", "ai")}
            )
            result = await classifier.classify_tags(["blue_archive"])
            self.assertEqual(result["blue_archive"].classification, "ip")
            classifier._classify_with_ai.assert_awaited_once()

        with patch.object(
            database,
            "get_tag_classifications",
            new=AsyncMock(return_value={"blue_archive": {"classification": "feature", "source": "fallback"}}),
        ), patch.object(database, "save_tag_classifications", new=AsyncMock()), \
             patch("tag_classifier.HAS_OPENAI", True), \
             patch("tag_classifier.AsyncOpenAI", return_value=object()):
            asyncio.run(_run())

    def test_manual_ip_list_overrides_stale_fallback_cache(self):
        async def _run():
            classifier = TagClassifier({"enabled": False}, ip_tags=["blue_archive"])
            result = await classifier.classify_tags(["blue_archive"])
            self.assertEqual(result["blue_archive"].classification, "ip")
            self.assertEqual(result["blue_archive"].source, "manual")

        with patch.object(
            database,
            "get_tag_classifications",
            new=AsyncMock(return_value={"blue_archive": {"classification": "feature", "source": "fallback"}}),
        ), patch.object(database, "save_tag_classifications", new=AsyncMock()) as mock_save:
            asyncio.run(_run())
            mock_save.assert_awaited_once()

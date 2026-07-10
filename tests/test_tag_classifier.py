import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)
sys.modules.setdefault(
    "aiosqlite",
    types.SimpleNamespace(connect=None),
)
sys.modules.setdefault(
    "aiohttp",
    types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
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
            self.assertEqual(result["blue_archive"].classification, "copyright")
            self.assertEqual(result["blue_archive"].source, "manual")
            self.assertEqual(result["pantyhose"].classification, "feature")

        with patch.object(database, "get_tag_classifications", new=AsyncMock(return_value={})), \
             patch.object(database, "save_tag_classifications", new=AsyncMock()):
            asyncio.run(_run())

    def test_cached_classification_is_reused(self):
        async def _run():
            classifier = TagClassifier({"enabled": False}, ip_tags=["blue_archive"])
            result = await classifier.classify_tags(["blue_archive"])
            self.assertEqual(result["blue_archive"].classification, "copyright")
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
                return_value={"blue_archive": TagClassification("copyright", "ai")}
            )
            result = await classifier.classify_tags(["blue_archive"])
            self.assertEqual(result["blue_archive"].classification, "copyright")
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
            self.assertEqual(result["blue_archive"].classification, "copyright")
            self.assertEqual(result["blue_archive"].source, "manual")

        with patch.object(
            database,
            "get_tag_classifications",
            new=AsyncMock(return_value={"blue_archive": {"classification": "feature", "source": "fallback"}}),
        ), patch.object(database, "save_tag_classifications", new=AsyncMock()) as mock_save:
            asyncio.run(_run())
            mock_save.assert_awaited_once()

    def test_tag_classification_normalizes_legacy_ip(self):
        classification = TagClassification("ip", "cached")

        self.assertEqual(classification.classification, "copyright")

    def test_parse_ai_response_supports_full_category_set(self):
        classifier = TagClassifier({"enabled": False})

        result = classifier._parse_ai_classifications(
            {
                "feature_tags": ["pantyhose"],
                "character_tags": ["hoshino_(blue_archive)"],
                "copyright_tags": ["blue_archive"],
                "artist_tags": ["some_artist"],
                "non_preference_tags": ["high_resolution"],
                "unresolved_tags": ["ambiguous_tag"],
            },
            [
                "pantyhose",
                "hoshino_(blue_archive)",
                "blue_archive",
                "some_artist",
                "high_resolution",
                "ambiguous_tag",
            ],
        )

        self.assertEqual(result["pantyhose"].classification, "feature")
        self.assertEqual(result["hoshino_(blue_archive)"].classification, "character")
        self.assertEqual(result["blue_archive"].classification, "copyright")
        self.assertEqual(result["some_artist"].classification, "artist")
        self.assertEqual(result["high_resolution"].classification, "non_preference")
        self.assertEqual(result["ambiguous_tag"].classification, "unresolved")

    def test_parse_ai_response_marks_conflicting_assignments_unresolved(self):
        classifier = TagClassifier({"enabled": False})

        result = classifier._parse_ai_classifications(
            {
                "feature_tags": ["blue_archive"],
                "copyright_tags": ["blue_archive"],
            },
            ["blue_archive"],
        )

        self.assertEqual(result["blue_archive"].classification, "unresolved")

    def test_enabled_classifier_marks_ai_omissions_unresolved(self):
        async def _run():
            classifier = TagClassifier(
                {"enabled": True, "api_key": "test-key"},
                ip_tags=["blue_archive"],
            )
            classifier.client = object()
            classifier._classify_with_ai = AsyncMock(
                return_value={"pantyhose": TagClassification("feature", "ai")}
            )
            result = await classifier.classify_tags(["pantyhose", "ambiguous_tag"])
            self.assertEqual(result["pantyhose"].classification, "feature")
            self.assertEqual(result["ambiguous_tag"].classification, "unresolved")

        with patch.object(database, "get_tag_classifications", new=AsyncMock(return_value={})), \
             patch.object(database, "save_tag_classifications", new=AsyncMock()), \
             patch("tag_classifier.HAS_OPENAI", True), \
             patch("tag_classifier.AsyncOpenAI", return_value=object()):
            asyncio.run(_run())


class DatabaseTagClassificationNormalizationTests(unittest.TestCase):
    class _FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        async def fetchall(self):
            return self._rows

    class _FakeConnection:
        def __init__(self, rows=None):
            self.rows = rows or []
            self.saved_items = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, *_args, **_kwargs):
            return DatabaseTagClassificationNormalizationTests._FakeCursor(self.rows)

        async def executemany(self, _sql, items):
            self.saved_items = list(items)

        async def commit(self):
            pass

    def test_get_tag_classifications_normalizes_cached_categories(self):
        async def _run():
            fake = self._FakeConnection(
                rows=[
                    ("blue_archive", "ip", "ai"),
                    ("ambiguous_tag", "bad-category", "ai"),
                ]
            )
            with patch.object(database.aiosqlite, "connect", return_value=fake):
                return await database.get_tag_classifications(
                    ["blue_archive", "ambiguous_tag"],
                    ttl_days=30,
                )

        result = asyncio.run(_run())

        self.assertEqual(result["blue_archive"]["classification"], "copyright")
        self.assertEqual(result["ambiguous_tag"]["classification"], "unresolved")

    def test_save_tag_classifications_normalizes_before_persisting(self):
        async def _run():
            fake = self._FakeConnection()
            with patch.object(database.aiosqlite, "connect", return_value=fake):
                await database.save_tag_classifications(
                    [
                        ("blue_archive", "ip", "ai"),
                        ("ambiguous_tag", "bad-category", "ai"),
                    ]
                )
            return fake.saved_items

        saved_items = asyncio.run(_run())

        self.assertEqual(
            saved_items,
            [
                ("blue_archive", "copyright", "ai"),
                ("ambiguous_tag", "unresolved", "ai"),
            ],
        )

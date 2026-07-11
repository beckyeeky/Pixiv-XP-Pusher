import asyncio
import sys
import types
import unittest
from datetime import datetime
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
from tag_categories import is_seed_category


class TagClassifierTests(unittest.TestCase):
    def test_resolves_selected_model_through_its_provider(self):
        classifier = TagClassifier({
            "enabled": False,
            "providers": {
                "gateway": {
                    "type": "openai_compatible",
                    "api_key": "profile-key",
                    "base_url": "https://judge.example/v1",
                },
            },
            "models": {"fast": {"provider": "gateway", "model": "judge-model"}},
            "judges": ["fast"],
        })

        self.assertEqual(len(classifier.judges), 1)
        self.assertEqual(classifier.judges[0]["name"], "fast")
        self.assertEqual(classifier.judges[0]["model"], "judge-model")
        self.assertEqual(classifier.api_key, "profile-key")

    def test_maintenance_reuses_fresh_machine_evidence_without_rechecking_its_sources(self):
        async def _run():
            classifier = TagClassifier({"enabled": False, "api_key": "legacy", "model": "legacy-model"})
            judge_source = f"judge:{classifier.judges[0]['identity']}"
            cached = {
                "tag": [
                    {"source": "danbooru", "classification": "character", "confidence": 1.0, "verified_at": datetime.now()},
                    {"source": judge_source, "classification": "character", "confidence": 1.0, "verified_at": datetime.now()},
                ]
            }
            classifier._collect_judge_evidence = AsyncMock(return_value={})
            classifier.danbooru_lookup.lookup = AsyncMock(return_value={})
            with patch.object(database, "get_tag_classifications", new=AsyncMock(return_value={})), \
                patch.object(database, "get_tag_evidence", new=AsyncMock(return_value=cached)), \
                patch.object(database, "save_tag_evidence", new=AsyncMock()) as save_evidence, \
                patch.object(database, "save_tag_classifications", new=AsyncMock()):
                result = await classifier.maintain_profile_tags({"tag": 1.0})
            return result, judge_source, classifier._collect_judge_evidence, classifier.danbooru_lookup.lookup, save_evidence

        result, judge_source, judges, danbooru, save_evidence = asyncio.run(_run())

        self.assertEqual(result["tag"].classification, "character")
        judges.assert_not_awaited()
        danbooru.assert_not_awaited()
        save_evidence.assert_not_awaited()

    def test_maintenance_refreshes_only_the_expired_machine_source(self):
        async def _run():
            classifier = TagClassifier({"enabled": False, "api_key": "legacy", "model": "legacy-model"})
            judge_source = f"judge:{classifier.judges[0]['identity']}"
            cached = {
                "tag": [
                    {"source": "danbooru", "classification": "character", "confidence": 1.0, "verified_at": datetime.now()},
                    {"source": judge_source, "classification": "character", "confidence": 1.0, "verified_at": datetime(2000, 1, 1)},
                ]
            }
            classifier._collect_judge_evidence = AsyncMock(return_value={"tag": [(judge_source, "character", 1.0)]})
            classifier.danbooru_lookup.lookup = AsyncMock(return_value={})
            with patch.object(database, "get_tag_classifications", new=AsyncMock(return_value={})), \
                patch.object(database, "get_tag_evidence", new=AsyncMock(return_value=cached)), \
                patch.object(database, "save_tag_evidence", new=AsyncMock()) as save_evidence, \
                patch.object(database, "save_tag_classifications", new=AsyncMock()):
                result = await classifier.maintain_profile_tags({"tag": 1.0})
            return result, judge_source, classifier._collect_judge_evidence, classifier.danbooru_lookup.lookup, save_evidence

        result, judge_source, judges, danbooru, save_evidence = asyncio.run(_run())

        self.assertEqual(result["tag"].classification, "character")
        judges.assert_awaited_once()
        self.assertEqual(judges.await_args.args[0], ["tag"])
        danbooru.assert_not_awaited()
        save_evidence.assert_awaited_once_with([("tag", judge_source, "character", 1.0)])

    def test_maintenance_selects_high_impact_unresolved_tags_and_accepts_multi_judge_consensus(self):
        async def _run():
            classifier = TagClassifier({
                "enabled": False,
                "maintenance": {"max_tags_per_run": 2, "prefer_unresolved_first": True},
                "judges": [
                    {"name": "one", "api_key": "one"},
                    {"name": "two", "api_key": "two"},
                ],
            })
            with patch.object(database, "get_tag_classifications", new=AsyncMock(return_value={
                "resolved_high": {"classification": "feature", "source": "manual"},
                "unresolved_low": {"classification": "unresolved", "source": "ai"},
                "unresolved_high": {"classification": "unresolved", "source": "ai"},
            })), patch.object(database, "get_tag_evidence", new=AsyncMock(return_value={})), \
                patch.object(database, "save_tag_evidence", new=AsyncMock()) as save_evidence, \
                patch.object(database, "save_tag_classifications", new=AsyncMock()):
                classifier._collect_judge_evidence = AsyncMock(return_value={
                    "unresolved_high": [("judge:one", "character", 1.0), ("judge:two", "character", 1.0)],
                    "unresolved_low": [("judge:one", "copyright", 1.0), ("judge:two", "copyright", 1.0)],
                })
                result = await classifier.maintain_profile_tags({
                    "resolved_high": 10.0, "unresolved_low": 1.0, "unresolved_high": 5.0,
                })
            return result, classifier._collect_judge_evidence, save_evidence

        result, judges, save_evidence = asyncio.run(_run())

        self.assertEqual(set(result), {"unresolved_high", "unresolved_low"})
        self.assertEqual(result["unresolved_high"].classification, "character")
        judges.assert_awaited_once()
        self.assertEqual(judges.await_args.args[0], ["unresolved_high", "unresolved_low"])
        self.assertTrue(any(row[0] == "unresolved_high" for row in save_evidence.await_args.args[0]))

    def test_maintenance_keeps_disagreement_unresolved_when_danbooru_is_unavailable(self):
        async def _run():
            classifier = TagClassifier({"enabled": False, "judges": [{"name": "one", "api_key": "one"}, {"name": "two", "api_key": "two"}]})
            with patch.object(database, "get_tag_classifications", new=AsyncMock(return_value={})), \
                patch.object(database, "get_tag_evidence", new=AsyncMock(return_value={"tag": [{"source": "danbooru", "classification": "feature", "confidence": 1.0}]})), \
                patch.object(database, "save_tag_evidence", new=AsyncMock()), \
                patch.object(database, "save_tag_classifications", new=AsyncMock()):
                classifier._collect_judge_evidence = AsyncMock(return_value={
                    "tag": [("judge:one", "character", 1.0), ("judge:two", "copyright", 1.0)]
                })
                result = await classifier.maintain_profile_tags({"tag": 1.0})
            return result

        result = asyncio.run(_run())
        self.assertEqual(result["tag"].classification, "unresolved")

    def test_legacy_single_model_is_one_judge_and_danbooru_errors_do_not_stop_maintenance(self):
        async def _run():
            classifier = TagClassifier({"enabled": False, "api_key": "legacy", "model": "legacy-model"})
            with patch.object(database, "get_tag_classifications", new=AsyncMock(return_value={})), \
                patch.object(database, "get_tag_evidence", new=AsyncMock(return_value={})), \
                patch.object(database, "save_tag_evidence", new=AsyncMock()), \
                patch.object(database, "save_tag_classifications", new=AsyncMock()):
                classifier._collect_judge_evidence = AsyncMock(return_value={"tag": [("judge:legacy", "feature", 1.0)]})
                classifier.danbooru_lookup.lookup = AsyncMock(side_effect=TimeoutError("offline"))
                result = await classifier.maintain_profile_tags({"tag": 1.0})
            return classifier, result

        classifier, result = asyncio.run(_run())
        self.assertEqual(len(classifier.judges), 1)
        self.assertEqual(result["tag"].classification, "unresolved")
    def test_profile_maintenance_keeps_conflicting_machine_evidence_unresolved(self):
        async def _run():
            classifier = TagClassifier({"enabled": False})
            with patch.object(database, "get_tag_evidence", new=AsyncMock(return_value={})), \
                 patch.object(database, "save_tag_evidence", new=AsyncMock()) as save_evidence, \
                 patch.object(database, "save_tag_classifications", new=AsyncMock()) as save_classifications:
                result = await classifier.maintain_profile_tags(
                    ["ambiguous_tag"],
                    evidence_lookup=AsyncMock(return_value={
                        "ambiguous_tag": [("danbooru", "character", 1.0), ("ai:test-model", "copyright", 1.0)]
                    }),
                )
            return result, save_evidence, save_classifications

        result, save_evidence, save_classifications = asyncio.run(_run())

        self.assertEqual(result["ambiguous_tag"].classification, "unresolved")
        self.assertEqual(result["ambiguous_tag"].source, "evidence_unresolved")
        save_evidence.assert_awaited_once()
        save_classifications.assert_awaited_once_with(
            [("ambiguous_tag", "unresolved", "evidence_unresolved")]
        )

    def test_profile_maintenance_manual_decision_overrides_machine_evidence(self):
        async def _run():
            classifier = TagClassifier({"enabled": False}, ip_tags=["blue_archive"])
            with patch.object(database, "get_tag_evidence", new=AsyncMock(return_value={})), \
                 patch.object(database, "save_tag_evidence", new=AsyncMock()), \
                 patch.object(database, "save_tag_classifications", new=AsyncMock()):
                return await classifier.maintain_profile_tags(
                    ["blue_archive"],
                    evidence_lookup=AsyncMock(return_value={
                        "blue_archive": [("danbooru", "character", 1.0)]
                    }),
                )

        result = asyncio.run(_run())

        self.assertEqual(result["blue_archive"].classification, "copyright")
        self.assertEqual(result["blue_archive"].source, "manual")
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

    def test_manual_non_preference_decision_is_reused_and_never_becomes_a_seed(self):
        async def _run():
            classifier = TagClassifier({"enabled": False})
            with patch.object(database, "get_tag_classifications", new=AsyncMock(return_value={
                "platform_marker": {"classification": "non_preference", "source": "manual"}
            })), patch.object(database, "save_tag_classifications", new=AsyncMock()) as save:
                result = await classifier.classify_tags(["platform_marker"])
            return result, save

        result, save = asyncio.run(_run())
        self.assertEqual(result["platform_marker"].source, "manual")
        self.assertFalse(is_seed_category(result["platform_marker"]))
        save.assert_not_awaited()

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

    def test_multi_judge_delivery_defers_new_machine_categories_to_maintenance_consensus(self):
        async def _run():
            classifier = TagClassifier({
                "enabled": True, "api_key": "legacy", "judges": [
                    {"name": "one", "api_key": "one", "base_url": "https://one.example/v1", "model": "one"},
                    {"name": "two", "api_key": "two", "base_url": "https://two.example/v1", "model": "two"},
                ],
            })
            classifier.client = object()
            classifier._classify_with_ai = AsyncMock()
            with patch.object(database, "get_tag_classifications", new=AsyncMock(return_value={})), \
                patch.object(database, "save_tag_classifications", new=AsyncMock()):
                result = await classifier.classify_tags(["new_tag"])
            return result, classifier._classify_with_ai

        result, direct_ai = asyncio.run(_run())
        self.assertEqual(result["new_tag"].classification, "unresolved")
        direct_ai.assert_not_awaited()


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

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp

import classification_maintenance as maintenance
import database
import grounded_judge
import tag_classifier
from tag_classifier import TagClassifier


class ScheduledClassificationMaintenanceTests(unittest.TestCase):
    def test_uses_the_single_tag_path_and_aggregates_usage(self):
        async def run():
            classify = AsyncMock(side_effect=[
                {"tag": "white_hair", "status": "accepted", "usage": {"total": 12, "search_queries": 1}},
                {"tag": "ambiguous", "status": "unresolved"},
            ])
            return await maintenance.run_scheduled_maintenance(
                ["white_hair", "ambiguous"], {"tag_classifier": {}}, classify,
            ), classify

        summary, classify = asyncio.run(run())

        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(summary["unresolved"], 1)
        self.assertEqual(summary["usage"]["total"], 12)
        self.assertEqual(summary["usage"]["search_queries"], 1)
        self.assertEqual(classify.await_args_list[0].args[0], "white_hair")

    def test_human_override_is_preserved_by_the_shared_activation_path(self):
        record = {
            "tag": "reviewed", "classification": "feature", "explanation": "trait", "languages": "en",
        }
        with patch.object(maintenance.db, "get_translated_tag", new=AsyncMock(return_value=None)), \
             patch.object(maintenance, "classify_single_tag", new=AsyncMock(return_value=record)), \
             patch.object(maintenance.db, "activate_ai_tag_classification", new=AsyncMock(return_value=False)):
            result = asyncio.run(maintenance.classify_and_activate_tag("reviewed", {"tag_classifier": {}}))

        self.assertEqual(result["status"], "human_override")

    def test_unresolved_result_stays_reviewable_and_keeps_its_usage(self):
        error = ValueError("Grounded Judge 明确标为 unresolved")
        error.usage = {"total": 9, "search_queries": 2}

        async def classify(_tag, _config):
            raise error

        with patch.object(maintenance.db, "mark_ai_tag_unresolved", new=AsyncMock(return_value=True)) as mark:
            summary = asyncio.run(maintenance.run_scheduled_maintenance(["ambiguous"], {}, classify))

        self.assertEqual(summary["unresolved"], 1)
        self.assertEqual(summary["usage"]["total"], 9)
        self.assertEqual(summary["items"][0]["usage"]["search_queries"], 2)
        mark.assert_awaited_once_with("ambiguous")

    def test_fresh_tag_can_be_activated_without_overriding_a_human_decision(self):
        async def run(path):
            await database.init_db()
            activated = await database.activate_ai_tag_classification("fresh", "feature", "trait", "en")
            classifications = await database.get_tag_classifications(["fresh"], ttl_days=30)
            return activated, classifications

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir, \
             patch.object(database, "DB_PATH", Path(tmpdir) / "maintenance.db"):
            activated, classifications = asyncio.run(run(tmpdir))

        self.assertTrue(activated)
        self.assertEqual(classifications["fresh"]["classification"], "feature")

    def test_scheduled_maintenance_keeps_existing_priority_before_using_grounded_path(self):
        summary = {"attempted": 2, "accepted": 1, "unresolved": 1, "failed": 0,
                   "human_override": 0, "usage": {}, "items": []}
        classifier = TagClassifier({
            "maintenance": {"max_tags_per_run": 2, "prefer_unresolved_first": True},
        })
        with patch.object(tag_classifier.db, "get_tag_classifications", new=AsyncMock(return_value={
            "resolved_high": {"classification": "feature"},
            "unresolved_low": {"classification": "unresolved"},
            "unresolved_high": {"classification": "unresolved"},
        })), patch.object(tag_classifier, "run_scheduled_maintenance", new=AsyncMock(return_value=summary)) as run, \
             patch.object(tag_classifier.db, "set_state", new=AsyncMock()) as set_state:
            result = asyncio.run(classifier.maintain_profile_tags({
                "resolved_high": 10.0, "unresolved_low": 1.0, "unresolved_high": 5.0,
            }))

        self.assertIs(result, summary)
        self.assertEqual(run.await_args.args[0], ["unresolved_high", "unresolved_low"])
        self.assertEqual(run.await_args.kwargs["concurrency"], 10)
        set_state.assert_awaited_once()

    def test_scheduled_maintenance_respects_configured_concurrency(self):
        async def run():
            active = 0
            maximum_active = 0

            async def classify(tag, _config):
                nonlocal active, maximum_active
                active += 1
                maximum_active = max(maximum_active, active)
                await asyncio.sleep(0.01)
                active -= 1
                return {"tag": tag, "status": "accepted"}

            summary = await maintenance.run_scheduled_maintenance(
                ["one", "two", "three"], {}, classify, concurrency=2,
            )
            return summary, maximum_active

        summary, maximum_active = asyncio.run(run())

        self.assertEqual(summary["accepted"], 3)
        self.assertEqual(maximum_active, 2)

    def test_gemini_judge_settings_default_to_requested_values(self):
        config = {
            "tag_classifier": {"judges": ["gemini"]},
            "models": {"gemini": {"provider": "google", "model": "gemini-flash-latest"}},
            "providers": {"google": {"type": "google", "api_key": "test-key"}},
        }

        settings = grounded_judge._selected_gemini_judge(config)

        self.assertEqual(settings.max_output_tokens, 512)
        self.assertEqual(settings.temperature, 1.0)
        self.assertEqual(settings.max_retries, 2)

    def test_gemini_judge_uses_status_specific_retry_policy(self):
        config = {
            "tag_classifier": {
                "judges": ["gemini"],
                "grounded_judge": {"retry_by_status": {"503": {"max_retries": 5, "retry_delay_seconds": 5}}},
            },
            "models": {"gemini": {"provider": "google", "model": "gemini-flash-latest"}},
            "providers": {"google": {"type": "google", "api_key": "test-key"}},
        }

        settings = grounded_judge._selected_gemini_judge(config)
        error = aiohttp.ClientResponseError(None, (), status=503)

        self.assertEqual(grounded_judge._retry_policy(error, settings), (5, 5.0))

    def test_gemini_judge_uses_minimal_thinking_for_structured_classification(self):
        config = {
            "tag_classifier": {
                "judges": ["gemini"],
                "grounded_judge": {"max_output_tokens": 1024, "thinking_level": "minimal"},
            },
            "models": {"gemini": {"provider": "google", "model": "gemini-flash-latest"}},
            "providers": {"google": {"type": "google", "api_key": "test-key"}},
        }

        settings = grounded_judge._selected_gemini_judge(config)

        self.assertEqual(settings.max_output_tokens, 1024)
        self.assertEqual(settings.thinking_level, "minimal")

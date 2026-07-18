import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tag_relationship_judge import AiRelationshipRecommendation, RelationshipJudgeResponseError


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "review_tag_mapping_ai.py"
SPEC = importlib.util.spec_from_file_location("review_tag_mapping_ai", SCRIPT_PATH)
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


class ReviewTagMappingAiTests(unittest.TestCase):
    def candidate(self):
        return {
            "id": 7,
            "original_tag": "白髪",
            "proposed_normalized_tag": "white_hair",
            "source": "legacy_ai_tag_cache",
            "explanation": "legacy proposal",
            "embedding_similarity": 0.94,
            "occurrence_count": 5,
            "original_classification": "feature",
            "target_classification": "feature",
            "original_explanation": "white hair in Japanese",
            "target_explanation": "white hair feature",
            "original_weight": 2.0,
            "target_weight": 3.0,
        }

    def test_judge_is_bounded_and_saves_advice_without_reviewing_candidate(self):
        class FakeJudge:
            identity = "deepseek:model"

            async def judge(self, candidate):
                return AiRelationshipRecommendation(
                    "equivalent", 0.98, "same", "white_hair", (),
                    {
                        "same_identity": True, "broader_narrower": False,
                        "entity_franchise": False, "modifier_variant": False,
                    },
                )

        get_candidates = Mock(return_value=[self.candidate()])
        save = AsyncMock(return_value=9)
        with patch.object(script.db, "get_tag_mapping_candidates_sync", get_candidates), \
             patch.object(script.db, "save_tag_mapping_ai_recommendation", save):
            result = asyncio.run(script.judge_candidates(
                limit=1, refresh=False, judge=FakeJudge(), concurrency=1,
            ))
        self.assertEqual(result["judged"], 1)
        get_candidates.assert_called_once_with(limit=500)
        save.assert_awaited_once()
        self.assertFalse(hasattr(script.db, "review_tag_mapping_candidate_called"))

    def test_judge_collapses_duplicate_candidate_pairs_before_calling_the_model(self):
        class FakeJudge:
            identity = "deepseek:model"

            async def judge(self, candidate):
                return AiRelationshipRecommendation(
                    "equivalent", 0.98, "same", "white_hair", (),
                    {
                        "same_identity": True, "broader_narrower": False,
                        "entity_franchise": False, "modifier_variant": False,
                    },
                )

        equivalent = self.candidate()
        search = {**self.candidate(), "id": 8, "kind": "search", "source": "legacy_tag_mapping_stats"}
        save = AsyncMock(return_value=9)
        with patch.object(script.db, "get_tag_mapping_candidates_sync", return_value=[equivalent, search]), \
             patch.object(script.db, "save_tag_mapping_ai_recommendation", save):
            result = asyncio.run(script.judge_candidates(
                limit=20, refresh=False, judge=FakeJudge(), concurrency=1,
            ))
        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["judged"], 1)
        self.assertEqual(save.await_count, 1)

    def test_judge_reuses_current_review_owned_by_duplicate_candidate(self):
        class FakeJudge:
            identity = "deepseek:model"

            async def judge(self, _candidate):
                raise AssertionError("current pair must not be judged again")

        preferred = self.candidate()
        duplicate = {
            **self.candidate(),
            "id": 8,
            "kind": "search",
            "source": "legacy_tag_mapping_stats",
            "ai_recommendation_id": 21,
            "ai_model": "deepseek:model",
            "ai_principles_version": "tag-alias-review-v1",
        }
        from tag_relationship_judge import relationship_evidence_hash
        duplicate["ai_evidence_hash"] = relationship_evidence_hash(duplicate)
        duplicate["ai_is_current"] = True

        with patch.object(
            script.db,
            "get_tag_mapping_candidates_sync",
            return_value=[preferred, duplicate],
        ), patch.object(
            script.db,
            "save_tag_mapping_ai_recommendation",
            new=AsyncMock(),
        ) as save:
            result = asyncio.run(script.judge_candidates(
                limit=20, refresh=False, judge=FakeJudge(), concurrency=1,
            ))

        self.assertEqual(result["selected"], 0)
        self.assertEqual(result["judged"], 0)
        save.assert_not_awaited()

    def test_judge_reports_safe_response_diagnostics_for_malformed_json(self):
        class FakeJudge:
            identity = "deepseek:model"

            async def judge(self, candidate):
                raise RelationshipJudgeResponseError(
                    "Relationship Judge returned invalid JSON",
                    finish_reason="length",
                    response_excerpt='{"relation":"equivalent"',
                )

        with patch.object(script.db, "get_tag_mapping_candidates_sync", return_value=[self.candidate()]):
            result = asyncio.run(script.judge_candidates(
                limit=1, refresh=False, judge=FakeJudge(), concurrency=1,
            ))
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failures"], [{
            "candidate_id": 7,
            "error": "Relationship Judge returned invalid JSON",
            "finish_reason": "length",
            "response_excerpt": '{"relation":"equivalent"',
        }])

    def test_zero_limit_fails_before_loading_a_provider(self):
        with patch.object(script, "relationship_judge_from_config") as factory:
            with self.assertRaisesRegex(ValueError, "limit must be at least 1"):
                asyncio.run(script.judge_candidates(limit=0, refresh=False))
        factory.assert_not_called()

    def test_stage_requires_one_explicit_confirmation_mode(self):
        with self.assertRaises(SystemExit):
            script.build_parser().parse_args(["stage"])
        with self.assertRaises(SystemExit):
            script.build_parser().parse_args(["stage", "--dry-run", "--confirm"])
        args = script.build_parser().parse_args(["stage", "--dry-run"])
        self.assertTrue(args.dry_run)
        self.assertFalse(args.confirm)

    def test_stage_preview_and_confirm_only_shortlist_recommendations(self):
        candidate = self.candidate()
        candidate.update({
            "ai_recommendation_id": 9,
            "ai_relation": "distinct",
            "ai_confidence": 0.98,
            "ai_principles_version": "tag-alias-review-v1",
        })
        from tag_relationship_judge import relationship_evidence_hash
        candidate["ai_evidence_hash"] = relationship_evidence_hash(candidate)
        stage = Mock(return_value=1)
        with patch.object(
            script.db, "get_tag_mapping_candidates_sync", new=Mock(return_value=[candidate]),
        ), patch.object(script.db, "stage_tag_mapping_ai_recommendations_sync", stage):
            preview = asyncio.run(script.stage_recommendations(0.95, confirm=False))
            confirmed = asyncio.run(script.stage_recommendations(0.95, confirm=True))
        self.assertEqual(preview["eligible"], 1)
        self.assertTrue(preview["dry_run"])
        stage.assert_called_once()
        self.assertEqual(confirmed["staged"], 1)


if __name__ == "__main__":
    unittest.main()

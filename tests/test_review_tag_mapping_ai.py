import asyncio
import io
import importlib.util
import unittest
from contextlib import redirect_stdout
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

    def test_apply_requires_preview_or_explicit_confirmation_mode(self):
        with self.assertRaises(SystemExit):
            script.build_parser().parse_args(["apply"])
        with self.assertRaises(SystemExit):
            script.build_parser().parse_args(["apply", "--dry-run", "--confirm"])
        args = script.build_parser().parse_args([
            "apply", "--min-confidence", "0.97", "--dry-run",
        ])
        self.assertEqual(args.command, "apply")
        self.assertEqual(args.min_confidence, 0.97)
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

    def test_apply_cli_previews_then_directly_executes_the_same_safe_plan(self):
        candidate = self.candidate()
        candidate.update({
            "ai_recommendation_id": 9,
            "ai_relation": "distinct",
            "ai_confidence": 0.98,
            "ai_principles_version": "tag-alias-review-v1",
        })
        from tag_relationship_judge import relationship_evidence_hash
        candidate["ai_evidence_hash"] = relationship_evidence_hash(candidate)
        applied = {
            "accepted_equivalent": 0,
            "rejected": 1,
            "aliases_created": 0,
            "aliases_already_active": 0,
            "duplicate_candidates_resolved": 0,
        }
        apply_batch = Mock(return_value=applied)
        with patch.object(script.db, "_init_db_sync"), patch.object(
            script.db, "get_tag_mapping_candidates_sync", return_value=[candidate],
        ), patch.object(
            script.db, "apply_tag_mapping_ai_batch_sync", apply_batch,
        ):
            preview_output = io.StringIO()
            with redirect_stdout(preview_output):
                preview_status = script.main([
                    "apply", "--min-confidence", "0.95", "--dry-run",
                ])
            confirmed_output = io.StringIO()
            with redirect_stdout(confirmed_output):
                confirmed_status = script.main([
                    "apply", "--min-confidence", "0.95", "--confirm",
                ])

        import json
        preview = json.loads(preview_output.getvalue())
        confirmed = json.loads(confirmed_output.getvalue())
        self.assertEqual(preview_status, 0)
        self.assertEqual(preview["eligible"], 1)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(confirmed_status, 0)
        self.assertEqual(confirmed["rejected"], 1)
        apply_batch.assert_called_once()

    def test_follow_ai_mode_batches_high_confidence_pending_candidates_by_recommendation(self):
        from tag_relationship_judge import relationship_evidence_hash

        equivalent = self.candidate()
        equivalent.update({
            "id": 21,
            "original_tag": "ストラップシューズ",
            "proposed_normalized_tag": "strap_shoes",
            "original_classification": "feature",
            "target_classification": None,
            "ai_recommendation_id": 31,
            "ai_relation": "equivalent",
            "ai_confidence": 1.0,
            "ai_canonical_tag": "ストラップシューズ",
            "ai_risk_flags": "[]",
            "ai_principle_checks": '{"same_identity":true,"broader_narrower":false,"entity_franchise":false,"modifier_variant":false}',
            "ai_principles_version": "tag-alias-review-v1",
        })
        equivalent["ai_evidence_hash"] = relationship_evidence_hash(equivalent)
        related = {
            **equivalent,
            "id": 22,
            "original_tag": "eula_lawrence",
            "proposed_normalized_tag": "genshin_impact",
            "ai_recommendation_id": 32,
            "ai_relation": "related",
            "ai_canonical_tag": None,
            "ai_risk_flags": '["entity_franchise","broader_narrower"]',
        }
        related["ai_evidence_hash"] = relationship_evidence_hash(related)
        risky_equivalent = {
            **equivalent,
            "id": 23,
            "original_tag": "遠坂凛",
            "proposed_normalized_tag": "rin_tosaka",
            "ai_recommendation_id": 33,
            "ai_canonical_tag": "rin_tosaka",
            "ai_risk_flags": '["category_conflict"]',
        }
        risky_equivalent["ai_evidence_hash"] = relationship_evidence_hash(risky_equivalent)

        with patch.object(script.db, "_init_db_sync"), patch.object(
            script.db,
            "get_tag_mapping_candidates_sync",
            return_value=[equivalent, related, risky_equivalent],
        ), patch.object(
            script.db, "get_tag_aliases_sync", return_value={}, create=True,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                status = script.main([
                    "apply", "--min-confidence", "1.0",
                    "--follow-ai", "--dry-run",
                ])

        import json
        payload = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(payload["eligible"], 2)
        self.assertEqual(payload["blocked"], {"risk_flags": 1})
        self.assertEqual(payload["recommendations"], [
            {
                "candidate_id": 21,
                "recommendation_id": 31,
                "original_tag": "ストラップシューズ",
                "proposed_normalized_tag": "strap_shoes",
                "decision": "accept_equivalent",
                "confidence": 1.0,
                "rationale": "",
                "alias_original": "strap_shoes",
                "normalized_tag": "ストラップシューズ",
            },
            {
                "candidate_id": 22,
                "recommendation_id": 32,
                "original_tag": "eula_lawrence",
                "proposed_normalized_tag": "genshin_impact",
                "decision": "reject",
                "confidence": 1.0,
                "rationale": "",
                "alias_original": None,
                "normalized_tag": None,
            },
        ])

    def test_follow_ai_preview_skips_a_candidate_that_would_replace_an_active_alias(self):
        from tag_relationship_judge import relationship_evidence_hash

        candidate = self.candidate()
        candidate.update({
            "id": 24,
            "original_tag": "パニシング:グレイレイヴン",
            "proposed_normalized_tag": "punishing_gray_raven",
            "original_classification": None,
            "target_classification": "copyright",
            "ai_recommendation_id": 34,
            "ai_relation": "equivalent",
            "ai_confidence": 1.0,
            "ai_canonical_tag": "パニシング:グレイレイヴン",
            "ai_risk_flags": "[]",
            "ai_principle_checks": '{"same_identity":true,"broader_narrower":false,"entity_franchise":false,"modifier_variant":false}',
            "ai_principles_version": "tag-alias-review-v1",
        })
        candidate["ai_evidence_hash"] = relationship_evidence_hash(candidate)

        with patch.object(script.db, "_init_db_sync"), patch.object(
            script.db, "get_tag_mapping_candidates_sync", return_value=[candidate],
        ), patch.object(
            script.db,
            "get_tag_aliases_sync",
            return_value={"punishing_gray_raven": "戰雙帕彌什"},
            create=True,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                status = script.main([
                    "apply", "--min-confidence", "1.0",
                    "--follow-ai", "--dry-run",
                ])

        import json
        payload = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(payload["eligible"], 0)
        self.assertEqual(payload["blocked"], {"alias_conflict": 1})
        self.assertEqual(payload["recommendations"], [])


if __name__ == "__main__":
    unittest.main()

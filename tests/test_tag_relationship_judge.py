import json
import unittest
from types import SimpleNamespace

from tag_relationship_judge import (
    MERGE_PRINCIPLES_VERSION,
    OpenAICompatibleRelationshipJudge,
    RelationshipJudgeResponseError,
    build_relationship_prompt,
    plan_ai_recommendation_staging,
    relationship_evidence_hash,
    validate_relationship_recommendation,
)


class TagRelationshipJudgeTests(unittest.TestCase):
    def candidate(self, **overrides):
        candidate = {
            "id": 7,
            "original_tag": "白髪",
            "proposed_normalized_tag": "white_hair",
            "source": "legacy_ai_tag_cache",
            "explanation": "Legacy proposal",
            "embedding_similarity": 0.94,
            "occurrence_count": 12,
            "original_classification": "feature",
            "target_classification": "feature",
            "original_language": "ja",
            "target_language": "en",
            "original_translation": "white hair",
            "target_translation": None,
            "original_explanation": "A Japanese tag for characters with white hair.",
            "target_explanation": "A visible white hair colour feature.",
            "original_weight": 2.0,
            "target_weight": 3.0,
        }
        candidate.update(overrides)
        return candidate

    def recommendation(self, **overrides):
        result = {
            "relation": "equivalent",
            "confidence": 0.98,
            "rationale": "Both forms name the same hair colour.",
            "canonical_tag": "white_hair",
            "risk_flags": [],
            "principle_checks": {
                "same_identity": True,
                "broader_narrower": False,
                "entity_franchise": False,
                "modifier_variant": False,
            },
        }
        result.update(overrides)
        return result

    def test_prompt_uses_saved_context_and_versioned_principles(self):
        prompt = build_relationship_prompt(self.candidate())
        self.assertIn("A Japanese tag for characters with white hair.", prompt)
        self.assertIn("A visible white hair colour feature.", prompt)
        self.assertIn("Legacy proposal", prompt)
        self.assertIn('"embedding_similarity": 0.94', prompt)
        self.assertIn("character and its franchise", prompt)
        self.assertIn(MERGE_PRINCIPLES_VERSION, prompt)
        self.assertIn('"白髪" or "white_hair"', prompt)
        self.assertIn('Never return the literal strings "tag_a.tag" or "tag_b.tag"', prompt)

    def test_evidence_hash_ignores_profile_weights_but_tracks_semantic_context(self):
        baseline = self.candidate()
        changed_weights = self.candidate(original_weight=99.0, target_weight=-3.0)
        changed_explanation = self.candidate(
            original_explanation="A different semantic description.",
        )

        self.assertEqual(
            relationship_evidence_hash(baseline),
            relationship_evidence_hash(changed_weights),
        )
        self.assertNotEqual(
            relationship_evidence_hash(baseline),
            relationship_evidence_hash(changed_explanation),
        )

    def test_validation_requires_exact_checks_and_pair_canonical_tag(self):
        with self.assertRaisesRegex(ValueError, "exact principle_checks"):
            validate_relationship_recommendation(
                self.recommendation(principle_checks={"same_identity": True}),
                self.candidate(),
            )
        with self.assertRaisesRegex(ValueError, "canonical_tag"):
            validate_relationship_recommendation(
                self.recommendation(canonical_tag="third_tag"), self.candidate(),
            )

    def test_openai_compatible_adapter_rejects_malformed_output(self):
        class Completions:
            async def create(self, **_kwargs):
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="not json"),
                )])

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        judge = OpenAICompatibleRelationshipJudge({
            "provider": "openai_compatible",
            "provider_name": "deepseek",
            "model": "deepseek-chat",
        }, client=client)
        with self.assertRaisesRegex(RelationshipJudgeResponseError, "invalid JSON") as error:
            import asyncio
            asyncio.run(judge.judge(self.candidate()))
        self.assertEqual(error.exception.finish_reason, None)
        self.assertEqual(error.exception.response_excerpt, "not json")

    def test_openai_compatible_adapter_requests_json_output(self):
        requests = []

        class Completions:
            async def create(self, **kwargs):
                requests.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(self_recommendation)),
                )])

        self_recommendation = self.recommendation()
        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        judge = OpenAICompatibleRelationshipJudge({
            "provider": "openai_compatible",
            "provider_name": "deepseek",
            "model": "deepseek-chat",
        }, client=client)
        import asyncio
        result = asyncio.run(judge.judge(self.candidate()))
        self.assertEqual(result.relation, "equivalent")
        self.assertEqual(requests[0]["response_format"], {"type": "json_object"})

    def test_staging_plans_safe_equivalence_and_distinct_rejection_only(self):
        equivalent = self.candidate(
            ai_recommendation_id=17,
            ai_relation="equivalent",
            ai_confidence=0.98,
            ai_canonical_tag="white_hair",
            ai_risk_flags="[]",
            ai_principle_checks=json.dumps(self.recommendation()["principle_checks"]),
            ai_principles_version=MERGE_PRINCIPLES_VERSION,
        )
        equivalent["ai_evidence_hash"] = relationship_evidence_hash(equivalent)
        distinct = self.candidate(
            id=8,
            ai_recommendation_id=18,
            ai_relation="distinct",
            ai_confidence=0.97,
            ai_canonical_tag=None,
            ai_risk_flags="[]",
            ai_principle_checks=json.dumps({
                "same_identity": False,
                "broader_narrower": False,
                "entity_franchise": True,
                "modifier_variant": False,
            }),
            ai_principles_version=MERGE_PRINCIPLES_VERSION,
        )
        distinct["ai_evidence_hash"] = relationship_evidence_hash(distinct)
        plan = plan_ai_recommendation_staging(
            [equivalent, distinct], min_confidence=0.95,
        )
        self.assertEqual(
            [(item.candidate_id, item.decision) for item in plan.decisions],
            [(7, "accept_equivalent"), (8, "reject")],
        )

    def test_staging_blocks_risky_wrong_direction_and_stale_recommendations(self):
        risky = self.candidate(
            ai_recommendation_id=20, ai_relation="equivalent", ai_confidence=0.99,
            ai_canonical_tag="white_hair", ai_risk_flags='["broader_narrower"]',
            ai_principle_checks=json.dumps(self.recommendation()["principle_checks"]),
            ai_principles_version=MERGE_PRINCIPLES_VERSION,
        )
        risky["ai_evidence_hash"] = relationship_evidence_hash(risky)
        wrong_direction = self.candidate(
            id=9, ai_recommendation_id=21, ai_relation="equivalent", ai_confidence=0.99,
            ai_canonical_tag="白髪", ai_risk_flags="[]",
            ai_principle_checks=json.dumps(self.recommendation()["principle_checks"]),
            ai_principles_version=MERGE_PRINCIPLES_VERSION,
        )
        wrong_direction["ai_evidence_hash"] = relationship_evidence_hash(wrong_direction)
        stale = self.candidate(
            id=10, ai_recommendation_id=22, ai_relation="distinct", ai_confidence=0.99,
            ai_principles_version=MERGE_PRINCIPLES_VERSION,
            ai_evidence_hash="old",
        )
        self_mapping = self.candidate(
            id=11, original_tag="white_hair", proposed_normalized_tag="white_hair",
            ai_recommendation_id=23, ai_relation="equivalent", ai_confidence=0.99,
            ai_canonical_tag="white_hair", ai_risk_flags="[]",
            ai_principle_checks=json.dumps(self.recommendation()["principle_checks"]),
            ai_principles_version=MERGE_PRINCIPLES_VERSION,
        )
        self_mapping["ai_evidence_hash"] = relationship_evidence_hash(self_mapping)
        plan = plan_ai_recommendation_staging(
            [risky, wrong_direction, stale, self_mapping], min_confidence=0.95,
        )
        self.assertEqual(plan.decisions, ())
        self.assertEqual(plan.blocked, {
            "risk_flags": 1, "canonical_direction": 1, "stale_evidence": 1,
            "self_mapping": 1,
        })

    def test_batch_confidence_has_a_hard_floor(self):
        with self.assertRaisesRegex(ValueError, "at least 0.90"):
            plan_ai_recommendation_staging([], min_confidence=0.5)


if __name__ == "__main__":
    unittest.main()

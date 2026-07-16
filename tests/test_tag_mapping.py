import json
import unittest
from types import SimpleNamespace

from tag_mapping import (
    AITagMappingCandidateGenerator,
    TagIdentityResolver,
    would_create_alias_cycle,
)


class _FakeCompletions:
    def __init__(self, payload):
        self.payload = payload

    async def create(self, **_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))
        )])


class TagMappingTests(unittest.IsolatedAsyncioTestCase):
    def test_resolver_applies_only_supplied_accepted_aliases(self):
        resolver = TagIdentityResolver({"ブルアカ": "blue_archive"})

        self.assertEqual(resolver.resolve("ブルアカ"), "blue_archive")
        self.assertEqual(resolver.resolve("着物ビキニ"), "着物ビキニ")

    def test_cycle_detection_rejects_direct_and_transitive_cycles(self):
        self.assertTrue(would_create_alias_cycle({"b": "a"}, "a", "b"))
        self.assertTrue(would_create_alias_cycle({"b": "c", "c": "a"}, "a", "b"))
        self.assertFalse(would_create_alias_cycle({"b": "c"}, "a", "b"))

    async def test_ai_adapter_returns_candidates_without_any_persistence_dependency(self):
        generator = AITagMappingCandidateGenerator.__new__(AITagMappingCandidateGenerator)
        generator.enabled = True
        generator.model = "test-model"
        generator.batch_size = 20
        generator.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions({
            "candidates": [
                {
                    "original_tag": "ブルアカ",
                    "proposed_normalized_tag": "blue_archive",
                    "kind": "equivalent",
                    "explanation": "Japanese abbreviation.",
                },
                {
                    "original_tag": "not_in_input",
                    "proposed_normalized_tag": "ignored",
                    "kind": "equivalent",
                },
                {
                    "original_tag": "same",
                    "proposed_normalized_tag": "same",
                    "kind": "equivalent",
                },
            ],
        })))

        candidates = await generator.propose(["ブルアカ", "same"])

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].original_tag, "ブルアカ")
        self.assertEqual(candidates[0].proposed_normalized_tag, "blue_archive")


if __name__ == "__main__":
    unittest.main()

import unittest
from types import SimpleNamespace

from daily_slate import DailySlateComposer, PreferenceContributions
from tag_categories import TagClassification


def candidate(item_id, *, exploration_only=False):
    return SimpleNamespace(
        id=item_id,
        tags=[],
        exploration_only=exploration_only,
    )


class DailySlateVectorExplorationTests(unittest.TestCase):
    def test_vector_candidate_is_eligible_only_for_exploration_lane(self):
        composer = DailySlateComposer({"enabled": True})
        vector = candidate(1, exploration_only=True)
        ranked = [
            vector,
            candidate(2),
            candidate(3),
            candidate(4),
            candidate(5),
        ]
        contributions = {
            item_id: PreferenceContributions(feature=feature)
            for item_id, feature in (
                (1, 10.0), (2, 0.9), (3, 0.8), (4, 0.7), (5, 0.6),
            )
        }
        result = composer.compose(
            ranked,
            limit=4,
            classifications={},
            profile={},
            contributions=contributions,
        )
        self.assertIn(vector, result.selected)
        self.assertEqual(vector.recommendation_motive, "exploration")
        self.assertEqual(contributions[vector.id].motive, "feature")
        feature_items = [
            item
            for item in result.selected
            if item.recommendation_motive == "feature"
        ]
        self.assertNotIn(vector, feature_items)

    def test_vector_identity_is_capped_even_when_absent_from_profile(self):
        composer = DailySlateComposer({
            "enabled": True, "max_per_character": 1, "exploration_ratio": 0.4,
        })
        first = candidate(1, exploration_only=True)
        second = candidate(2, exploration_only=True)
        first.tags = second.tags = ["new_character"]
        result = composer.compose(
            [first, second], limit=3,
            classifications={"new_character": TagClassification("character", "manual")},
            profile={},
            contributions={},
        )
        self.assertEqual([item.id for item in result.selected], [1])

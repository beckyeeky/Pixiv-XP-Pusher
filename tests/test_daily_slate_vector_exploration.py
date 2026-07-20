import unittest
from types import SimpleNamespace

from daily_slate import DailySlateComposer
from tag_categories import TagClassification


def candidate(item_id, feature, *, exploration_only=False):
    return SimpleNamespace(
        id=item_id,
        tags=[],
        feature_contribution=feature,
        character_contribution=0.0,
        copyright_contribution=0.0,
        exploration_only=exploration_only,
    )


class DailySlateVectorExplorationTests(unittest.TestCase):
    def test_vector_candidate_is_eligible_only_for_exploration_lane(self):
        composer = DailySlateComposer({"enabled": True})
        vector = candidate(1, 10.0, exploration_only=True)
        result = composer.compose(
            [vector, candidate(2, 0.9), candidate(3, 0.8), candidate(4, 0.7), candidate(5, 0.6)],
            limit=4,
            classifications={},
            profile={},
        )
        self.assertIn(vector, result)
        self.assertEqual(vector.recommendation_motive, "exploration")
        self.assertEqual(composer._motive(vector), "feature")
        feature_items = [item for item in result if item.recommendation_motive == "feature"]
        self.assertNotIn(vector, feature_items)

    def test_vector_identity_is_capped_even_when_absent_from_profile(self):
        composer = DailySlateComposer({
            "enabled": True, "max_per_character": 1, "exploration_ratio": 0.4,
        })
        first = candidate(1, 0.0, exploration_only=True)
        second = candidate(2, 0.0, exploration_only=True)
        first.tags = second.tags = ["new_character"]
        selected = composer.compose(
            [first, second], limit=3,
            classifications={"new_character": TagClassification("character", "manual")},
            profile={},
        )
        self.assertEqual([item.id for item in selected], [1])

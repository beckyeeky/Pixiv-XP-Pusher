import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)
from fetcher import ContentFetcher
from tag_categories import TagClassification


class FetcherPairSelectionTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = ContentFetcher(client=object(), config={})
        self.tag_classifications = {
            "blue_archive": TagClassification("copyright", "manual"),
            "genshin_impact": TagClassification("copyright", "manual"),
            "hoshino": TagClassification("character", "ai"),
            "pantyhose": TagClassification("feature", "manual"),
            "high_resolution": TagClassification("non_preference", "ai"),
            "ambiguous_tag": TagClassification("unresolved", "ai"),
        }

    def test_select_search_pairs_skips_identity_plus_identity_combinations(self):
        selected = self.fetcher._select_search_pairs(
            [
                ("blue_archive", "genshin_impact", 5.0),
                ("hoshino", "blue_archive", 4.5),
                ("blue_archive", "pantyhose", 4.0),
            ],
            max_combo_tasks=2,
            tag_classifications=self.tag_classifications,
        )

        self.assertEqual(selected, [("blue_archive", "pantyhose", 4.0)])

    def test_select_search_pairs_skips_non_seed_tags(self):
        selected = self.fetcher._select_search_pairs(
            [
                ("high_resolution", "pantyhose", 5.0),
                ("ambiguous_tag", "blue_archive", 4.5),
                ("blue_archive", "pantyhose", 4.0),
            ],
            max_combo_tasks=3,
            tag_classifications=self.tag_classifications,
        )

        self.assertEqual(selected, [("blue_archive", "pantyhose", 4.0)])

    def test_build_exploration_pairs_skips_identity_plus_identity_combinations(self):
        samples = iter(
            [
                ["blue_archive", "genshin_impact"],
                ["high_resolution", "pantyhose"],
                ["blue_archive", "pantyhose"],
            ]
        )
        self.fetcher._weighted_sample = lambda weighted_tags, k: next(samples)

        pairs = self.fetcher._build_exploration_pairs(
            [
                ("blue_archive", 1.0),
                ("genshin_impact", 0.9),
                ("pantyhose", 0.8),
            ],
            used_pairs=set(),
            count=1,
            tag_classifications=self.tag_classifications,
        )

        self.assertEqual(pairs, [("blue_archive", "pantyhose", 0.0)])


class FetcherSeedSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_discover_never_searches_with_a_tag_missing_an_accepted_category(self):
        client = AsyncMock()
        client.search_illusts = AsyncMock(return_value=[])

        class AcceptedClassificationReader:
            async def classify_tags(self, tags):
                return {
                    "known_feature": TagClassification("feature", "manual"),
                }

        fetcher = ContentFetcher(
            client=client,
            config={},
            tag_classifier=AcceptedClassificationReader(),
        )

        with patch(
            "fetcher.db.get_top_tag_pairs",
            new=AsyncMock(return_value=[("unknown_tag", "known_feature", 5.0)]),
        ):
            result = await fetcher.discover([("unknown_tag", 1.0)], limit=1)

        self.assertEqual(result, [])
        client.search_illusts.assert_not_awaited()

import sys
import types
import unittest

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)
sys.modules.setdefault(
    "aiohttp",
    types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
)
sys.modules.setdefault(
    "aiosqlite",
    types.SimpleNamespace(connect=None),
)

from fetcher import ContentFetcher
from tag_classifier import TagClassification


class FetcherPairSelectionTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = ContentFetcher(client=object(), config={})
        self.tag_classifications = {
            "blue_archive": TagClassification("ip", "manual"),
            "genshin_impact": TagClassification("ip", "manual"),
            "pantyhose": TagClassification("feature", "manual"),
        }

    def test_select_search_pairs_skips_ip_plus_ip_combinations(self):
        selected = self.fetcher._select_search_pairs(
            [
                ("blue_archive", "genshin_impact", 5.0),
                ("blue_archive", "pantyhose", 4.0),
            ],
            max_combo_tasks=2,
            tag_classifications=self.tag_classifications,
        )

        self.assertEqual(selected, [("blue_archive", "pantyhose", 4.0)])

    def test_build_exploration_pairs_skips_ip_plus_ip_combinations(self):
        samples = iter(
            [
                ["blue_archive", "genshin_impact"],
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


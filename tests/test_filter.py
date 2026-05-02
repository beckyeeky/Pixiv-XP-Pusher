from datetime import datetime
import sys
import types
import unittest

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)

from filter import ContentFilter
from pixiv_client import Illust
from tag_classifier import TagClassification


class StubTagClassifier:
    async def classify_tags(self, tags):
        return {
            "pantyhose": TagClassification("feature", "ai"),
            "white_hair": TagClassification("feature", "ai"),
            "blue_archive": TagClassification("ip", "manual"),
            "genshin_impact": TagClassification("ip", "ai"),
        }


class DisplayTagsTests(unittest.IsolatedAsyncioTestCase):
    async def test_display_tags_feature_first_and_limit_ip_count(self):
        illust = Illust(
            id=1,
            title="test",
            user_id=1,
            user_name="artist",
            tags=["blue_archive", "pantyhose", "genshin_impact", "white_hair"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/a.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        content_filter = ContentFilter(
            daily_limit=10,
            tag_classifier=StubTagClassifier(),
            display_tags_max_ip_count=1,
        )

        await content_filter._apply_display_tags(
            [illust],
            {
                "pantyhose": 0.9,
                "white_hair": 0.8,
                "blue_archive": 1.0,
                "genshin_impact": 0.7,
            },
        )

        self.assertEqual(
            illust.display_tags,
            ["pantyhose", "white_hair", "blue_archive"],
        )

    def test_display_tags_max_ip_count_accepts_string_config(self):
        content_filter = ContentFilter(display_tags_max_ip_count="3")

        self.assertEqual(content_filter.display_tags_max_ip_count, 3)

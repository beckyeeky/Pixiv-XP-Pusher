from datetime import datetime
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)

from filter import ContentFilter, calculate_match_score
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
    def test_feature_tags_receive_extra_match_weight(self):
        ip_only = Illust(
            id=1,
            title="ip-only",
            user_id=1,
            user_name="artist",
            tags=["genshin_impact"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/a.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )
        feature_match = Illust(
            id=2,
            title="feature-match",
            user_id=2,
            user_name="artist",
            tags=["genshin_impact", "pantyhose"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/b.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        xp_profile = {"top_preference": 5.0, "genshin_impact": 1.0, "pantyhose": 1.0}
        classifications = {
            "genshin_impact": TagClassification("ip", "manual"),
            "pantyhose": TagClassification("feature", "manual"),
        }

        ip_only_score = calculate_match_score(ip_only, xp_profile, tag_classifications=classifications)
        feature_score = calculate_match_score(feature_match, xp_profile, tag_classifications=classifications)

        self.assertGreater(feature_score, ip_only_score)

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

        await content_filter.apply_display_tags(
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

    async def test_ip_diversity_decay_demotes_consecutive_same_ip(self):
        illusts = [
            Illust(
                id=1,
                title="ba-top",
                user_id=1,
                user_name="artist-1",
                tags=["blue_archive", "pantyhose"],
                bookmark_count=100,
                view_count=1000,
                page_count=1,
                image_urls=["https://example.com/1.jpg"],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now(),
            ),
            Illust(
                id=2,
                title="ba-second",
                user_id=2,
                user_name="artist-2",
                tags=["blue_archive", "white_hair"],
                bookmark_count=100,
                view_count=1000,
                page_count=1,
                image_urls=["https://example.com/2.jpg"],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now(),
            ),
            Illust(
                id=3,
                title="genshin",
                user_id=3,
                user_name="artist-3",
                tags=["genshin_impact", "white_hair"],
                bookmark_count=100,
                view_count=1000,
                page_count=1,
                image_urls=["https://example.com/3.jpg"],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now(),
            ),
        ]

        content_filter = ContentFilter(
            daily_limit=10,
            max_per_artist=10,
            exclude_ai=False,
            tag_classifier=StubTagClassifier(),
            ip_diversity={"enabled": True, "decay_factor": 0.6, "floor": 0.1},
        )

        xp_profile = {
            "blue_archive": 1.0,
            "genshin_impact": 0.95,
            "pantyhose": 0.1,
            "white_hair": 0.05,
        }

        with patch("filter.db.get_pushed_ids_batch", new=AsyncMock(return_value=set())), \
             patch("filter.db.get_muted_tags", new=AsyncMock(return_value=[])), \
             patch("filter.db.get_negative_profile", new=AsyncMock(return_value={})), \
             patch("filter.db.get_blocked_tags", new=AsyncMock(return_value=[])), \
             patch("filter.db.get_blocked_artists", new=AsyncMock(return_value=[])):
            ranked = await content_filter.filter(illusts, xp_profile=xp_profile)

        self.assertEqual([illust.id for illust in ranked[:3]], [1, 3, 2])

    async def test_filter_loads_db_blocked_tags_and_artists(self):
        blocked_tag_illust = Illust(
            id=10,
            title="blocked-tag",
            user_id=10,
            user_name="artist-10",
            tags=["Blue Archive", "pantyhose"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/10.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )
        blocked_artist_illust = Illust(
            id=11,
            title="blocked-artist",
            user_id=99,
            user_name="artist-99",
            tags=["white_hair"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/11.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )
        allowed_illust = Illust(
            id=12,
            title="allowed",
            user_id=12,
            user_name="artist-12",
            tags=["white_hair"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/12.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        content_filter = ContentFilter(daily_limit=10, exclude_ai=False)

        with patch("filter.db.get_pushed_ids_batch", new=AsyncMock(return_value=set())), \
             patch("filter.db.get_muted_tags", new=AsyncMock(return_value=[])), \
             patch("filter.db.get_negative_profile", new=AsyncMock(return_value={})), \
             patch("filter.db.get_blocked_tags", new=AsyncMock(return_value=["blue_archive"])), \
             patch("filter.db.get_blocked_artists", new=AsyncMock(return_value=[(99, "artist-99")])):
            ranked = await content_filter.filter(
                [blocked_tag_illust, blocked_artist_illust, allowed_illust],
                xp_profile={"white_hair": 1.0},
            )

        self.assertEqual([illust.id for illust in ranked], [12])
        self.assertIn("blue_archive", content_filter.blacklist_tags)
        self.assertIn(99, content_filter.blocked_artist_ids)

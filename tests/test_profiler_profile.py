import json
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)

from profiler import XPProfiler


class PreferenceProfileTests(unittest.IsolatedAsyncioTestCase):
    async def _build_from_same_cached_bookmark(
        self,
        ip_weight_discount,
        *,
        boost_tags=None,
        aliases=None,
    ):
        client = AsyncMock()
        client._logged_in = False
        client.get_bookmarks = AsyncMock(return_value=[])
        profiler = XPProfiler(
            client,
            ip_tags=["blue_archive"],
            ip_weight_discount=ip_weight_discount,
            saturation_threshold=1.0,
            boost_tags=boost_tags,
        )
        cached = [{
            "illust_id": 1,
            "illust_create_date": datetime.now(),
            "tags": json.dumps(["Blue Archive"]),
        }]

        with patch(
                 "profiler.db.get_accepted_tag_aliases",
                 new=AsyncMock(return_value=aliases or {}),
             ), \
             patch("profiler.db.get_blocked_tags", new=AsyncMock(return_value=[])), \
             patch("profiler.db.get_blocked_artists", new=AsyncMock(return_value=[])), \
             patch("profiler.db.get_xp_bookmarks", new=AsyncMock(return_value=cached)), \
             patch("profiler.db.get_state", new=AsyncMock(return_value="true")), \
             patch("profiler.db.get_liked_illusts", new=AsyncMock(return_value=set())), \
             patch("profiler.db.update_xp_tag_pairs", new=AsyncMock()), \
             patch("profiler.db.get_popular_tags", new=AsyncMock(return_value=[])), \
             patch("profiler.db.update_xp_profile", new=AsyncMock()):
            return await profiler.build_profile(user_id=123, scan_limit=10)

    async def test_preference_profile_is_independent_of_ip_delivery_discount(self):
        undiscounted = await self._build_from_same_cached_bookmark(1.0)
        legacy_discount_configured = await self._build_from_same_cached_bookmark(0.1)

        self.assertEqual(legacy_discount_configured, undiscounted)
        self.assertGreater(undiscounted["blue_archive"], 0)

    async def test_explicit_boost_tags_use_the_accepted_normalized_identity(self):
        profile = await self._build_from_same_cached_bookmark(
            1.0,
            boost_tags={"ブルアカ": 2.0},
            aliases={"ブルアカ": "blue_archive"},
        )

        self.assertEqual(list(profile), ["blue_archive"])
        self.assertGreater(profile["blue_archive"], 0.1)

    async def test_cache_only_rebuild_never_calls_pixiv_or_marks_sync_complete(self):
        client = AsyncMock()
        client._logged_in = False
        client.get_bookmarks = AsyncMock(return_value=[])
        profiler = XPProfiler(client, saturation_threshold=1.0)
        cached = [{
            "illust_id": 1,
            "illust_create_date": datetime.now(),
            "tags": json.dumps(["white_hair"]),
        }]
        set_state = AsyncMock()

        with patch("profiler.db.get_accepted_tag_aliases", new=AsyncMock(return_value={})), \
             patch("profiler.db.get_blocked_tags", new=AsyncMock(return_value=[])), \
             patch("profiler.db.get_blocked_artists", new=AsyncMock(return_value=[])), \
             patch("profiler.db.get_xp_bookmarks", new=AsyncMock(return_value=cached)), \
             patch("profiler.db.get_state", new=AsyncMock(return_value=None)), \
             patch("profiler.db.set_state", new=set_state), \
             patch("profiler.db.get_liked_illusts", new=AsyncMock(return_value=set())), \
             patch("profiler.db.update_xp_tag_pairs", new=AsyncMock()), \
             patch("profiler.db.get_popular_tags", new=AsyncMock(return_value=[])), \
             patch("profiler.db.update_xp_profile", new=AsyncMock()):
            profile = await profiler.build_profile(
                user_id=123, scan_limit=10, cache_only=True,
            )

        self.assertIn("white_hair", profile)
        client.get_bookmarks.assert_not_awaited()
        set_state.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

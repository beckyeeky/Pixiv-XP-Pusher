from datetime import datetime
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)

from pixiv_client import Illust
from profiler import XPProfiler


class ProfilerFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_feedback_auto_blocks_tag_at_threshold(self):
        profiler = XPProfiler(client=None)
        illust = Illust(
            id=123,
            title="test",
            user_id=77,
            user_name="artist",
            tags=["Blue Archive"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/1.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        with patch("profiler.db.get_xp_profile", new=AsyncMock(return_value={})), \
             patch("profiler.db.adjust_tag_weight", new=AsyncMock()) as adjust_tag_weight, \
             patch("profiler.db.adjust_negative_weight", new=AsyncMock()) as adjust_negative_weight, \
             patch("profiler.db.increment_tag_dislike", new=AsyncMock(return_value=3)), \
             patch("profiler.db.block_tag", new=AsyncMock()) as block_tag, \
             patch("profiler.db.update_artist_score", new=AsyncMock()) as update_artist_score, \
             patch("profiler.db.record_feedback", new=AsyncMock()) as record_feedback:
            result = await profiler.apply_feedback(
                illust,
                "dislike",
                {"dislike_penalty": 0.3, "dislike_threshold": 3},
            )

        self.assertEqual(result["disliked_tags"], ["blue_archive"])
        self.assertEqual(result["auto_blocked_tags"], ["blue_archive"])
        adjust_tag_weight.assert_awaited_once_with("blue_archive", -0.3)
        adjust_negative_weight.assert_awaited_once_with("blue_archive", 0.3)
        block_tag.assert_awaited_once_with("blue_archive")
        update_artist_score.assert_awaited_once_with(77, -1.0)
        record_feedback.assert_awaited_once_with(123, "dislike")

    async def test_apply_feedback_does_not_reblock_existing_tag_over_threshold(self):
        profiler = XPProfiler(client=None)
        illust = Illust(
            id=124,
            title="test-2",
            user_id=88,
            user_name="artist",
            tags=["Blue Archive"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/2.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        with patch("profiler.db.get_xp_profile", new=AsyncMock(return_value={})), \
             patch("profiler.db.adjust_tag_weight", new=AsyncMock()), \
             patch("profiler.db.adjust_negative_weight", new=AsyncMock()), \
             patch("profiler.db.increment_tag_dislike", new=AsyncMock(return_value=4)), \
             patch("profiler.db.is_tag_blocked", new=AsyncMock(return_value=True)), \
             patch("profiler.db.block_tag", new=AsyncMock()) as block_tag, \
             patch("profiler.db.update_artist_score", new=AsyncMock()), \
             patch("profiler.db.record_feedback", new=AsyncMock()):
            result = await profiler.apply_feedback(
                illust,
                "dislike",
                {"dislike_penalty": 0.3, "dislike_threshold": 3},
            )

        self.assertEqual(result["auto_blocked_tags"], [])
        block_tag.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

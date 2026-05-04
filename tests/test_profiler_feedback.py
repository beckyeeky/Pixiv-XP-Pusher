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
    async def test_apply_feedback_dislike_reduces_tag_weight_without_blocking(self):
        """dislike 只降 tag 权重，不自动屏蔽 tag（原 Issue 4 修正）"""
        profiler = XPProfiler(client=None)
        illust = Illust(
            id=123,
            title="test",
            user_id=77,
            user_name="artist-san",
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
             patch("profiler.db.increment_tag_dislike", new=AsyncMock()) as increment_tag_dislike, \
             patch("profiler.db.update_artist_score", new=AsyncMock()), \
             patch("profiler.db.get_artist_score", new=AsyncMock(return_value=-1.0)), \
             patch("profiler.db.record_feedback", new=AsyncMock()):
            result = await profiler.apply_feedback(
                illust,
                "dislike",
                {"dislike_penalty": 0.3, "dislike_threshold": 3},
            )

        self.assertEqual(result["disliked_tags"], ["blue_archive"])
        self.assertEqual(result["auto_blocked_artists"], [])
        adjust_tag_weight.assert_awaited_once_with("blue_archive", -0.3)
        adjust_negative_weight.assert_awaited_once_with("blue_archive", 0.3)
        increment_tag_dislike.assert_awaited_once()
        # 不应调用 block_tag
        self.assertNotIn("block_tag", [m[0] for m in dir(AsyncMock)])

    async def test_apply_feedback_auto_blocks_artist_after_three_dislikes(self):
        """同画师 dislike 3 次 → 自动屏蔽画师"""
        profiler = XPProfiler(client=None)
        illust = Illust(
            id=124,
            title="test-2",
            user_id=77,
            user_name="artist-san",
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
             patch("profiler.db.increment_tag_dislike", new=AsyncMock()), \
             patch("profiler.db.update_artist_score", new=AsyncMock()), \
             patch("profiler.db.get_artist_score", new=AsyncMock(return_value=-3.0)), \
             patch("profiler.db.is_artist_blocked", new=AsyncMock(return_value=False)), \
             patch("profiler.db.block_artist", new=AsyncMock()) as block_artist, \
             patch("profiler.db.record_feedback", new=AsyncMock()):
            result = await profiler.apply_feedback(
                illust,
                "dislike",
                {"dislike_penalty": 0.3, "dislike_threshold": 3},
            )

        self.assertEqual(result["auto_blocked_artists"], [
            {"artist_id": 77, "artist_name": "artist-san"},
        ])
        block_artist.assert_awaited_once_with(77, "artist-san")

    async def test_apply_feedback_does_not_reblock_already_blocked_artist(self):
        """已屏蔽的画师不会重复屏蔽"""
        profiler = XPProfiler(client=None)
        illust = Illust(
            id=125,
            title="test-3",
            user_id=77,
            user_name="artist-san",
            tags=["Blue Archive"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/3.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        with patch("profiler.db.get_xp_profile", new=AsyncMock(return_value={})), \
             patch("profiler.db.adjust_tag_weight", new=AsyncMock()), \
             patch("profiler.db.adjust_negative_weight", new=AsyncMock()), \
             patch("profiler.db.increment_tag_dislike", new=AsyncMock()), \
             patch("profiler.db.update_artist_score", new=AsyncMock()), \
             patch("profiler.db.get_artist_score", new=AsyncMock(return_value=-3.0)), \
             patch("profiler.db.is_artist_blocked", new=AsyncMock(return_value=True)), \
             patch("profiler.db.block_artist", new=AsyncMock()) as block_artist, \
             patch("profiler.db.record_feedback", new=AsyncMock()):
            result = await profiler.apply_feedback(
                illust,
                "dislike",
                {"dislike_penalty": 0.3, "dislike_threshold": 3},
            )

        self.assertEqual(result["auto_blocked_artists"], [])
        block_artist.assert_not_awaited()

    async def test_apply_feedback_like_doesnt_trigger_artist_block(self):
        """like 不会触发画师屏蔽检查"""
        profiler = XPProfiler(client=None)
        illust = Illust(
            id=126,
            title="test-4",
            user_id=77,
            user_name="artist-san",
            tags=["Blue Archive"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/4.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        with patch("profiler.db.get_xp_profile", new=AsyncMock(return_value={})), \
             patch("profiler.db.adjust_tag_weight", new=AsyncMock()), \
             patch("profiler.db.adjust_negative_weight", new=AsyncMock()), \
             patch("profiler.db.increment_tag_dislike", new=AsyncMock()), \
             patch("profiler.db.update_artist_score", new=AsyncMock()), \
             patch("profiler.db.get_artist_score", new=AsyncMock()) as get_artist_score, \
             patch("profiler.db.block_artist", new=AsyncMock()) as block_artist, \
             patch("profiler.db.record_feedback", new=AsyncMock()):
            result = await profiler.apply_feedback(
                illust,
                "like",
                {"dislike_penalty": 0.3, "dislike_threshold": 3},
            )

        self.assertEqual(result["auto_blocked_artists"], [])
        block_artist.assert_not_awaited()
        # like 不应读取 artist_score（不触发画师屏蔽检查）
        get_artist_score.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

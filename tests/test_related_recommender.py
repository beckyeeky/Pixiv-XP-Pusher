import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)

from pixiv_client import Illust
from related_recommender import RelatedRecommender


def make_illust(
    illust_id: int,
    *,
    tags: list[str] | None = None,
    user_id: int = 1,
    bookmark_count: int = 100,
    ai_type: int = 0,
) -> Illust:
    return Illust(
        id=illust_id,
        title=f"illust-{illust_id}",
        user_id=user_id,
        user_name=f"artist-{user_id}",
        tags=tags or ["good"],
        tags_translated=[],
        bookmark_count=bookmark_count,
        view_count=1000,
        page_count=1,
        image_urls=[f"https://example.com/{illust_id}.jpg"],
        is_r18=False,
        ai_type=ai_type,
        create_date=datetime.now(),
    )


class RelatedRecommenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_strategy_related_uses_liked_seed_and_sorts_by_xp_plus_artist_score(self):
        client = AsyncMock()
        client.get_related_illusts = AsyncMock(
            return_value=[
                make_illust(1, tags=["good"], user_id=10, bookmark_count=200),
                make_illust(2, tags=["other"], user_id=20, bookmark_count=200),
                make_illust(3, tags=["good"], user_id=30, bookmark_count=10),
            ]
        )
        recommender = RelatedRecommender(
            client=client,
            bookmark_threshold={"related": 100},
        )

        async def fake_artist_score(user_id):
            return {10: 0.5, 20: 5.0, 30: 100.0}.get(user_id, 0.0)

        with patch("related_recommender.db.get_liked_illusts", new=AsyncMock(return_value={42})), \
             patch("related_recommender.db.get_artist_score", new=AsyncMock(side_effect=fake_artist_score)):
            result = await recommender.discover_for_strategy([("good", 2.0)], limit=2)

        client.get_related_illusts.assert_awaited_once_with(42, limit=4)
        self.assertEqual([ill.id for ill in result], [2, 1])

    async def test_chain_related_skips_invalid_candidates_and_records_chain_metadata(self):
        client = AsyncMock()
        seed = make_illust(10, tags=["seed"], user_id=1)
        client.get_related_illusts = AsyncMock(
            return_value=[
                make_illust(10, tags=["good"], user_id=2),
                make_illust(20, tags=["good"], user_id=3),
                make_illust(20, tags=["good"], user_id=3),
                make_illust(30, tags=["good"], user_id=4),
                make_illust(40, tags=["good"], user_id=5, ai_type=2),
            ]
        )
        notifier = SimpleNamespace(push_illusts=AsyncMock(return_value={20: 999}))
        profiler = SimpleNamespace(stop_words=[], _blocked_artist_ids=set())
        recommender = RelatedRecommender(
            client=client,
            config={
                "fetcher": {"bookmark_threshold": {"related": 0}},
                "filter": {"exclude_ai": True},
                "feedback": {"related_push_limit": 1},
                "tag_classifier": {},
            },
            profiler=profiler,
        )

        class StubTagClassifier:
            async def classify_tags(self, tags):
                return {}

        async def fake_is_pushed(illust_id):
            return illust_id == 30

        with patch("related_recommender.TagClassifier", return_value=StubTagClassifier()), \
             patch("related_recommender.db.get_xp_profile", new=AsyncMock(return_value={"good": 2.0})), \
             patch("related_recommender.db.is_pushed", new=AsyncMock(side_effect=fake_is_pushed)), \
             patch("related_recommender.db.get_artist_score", new=AsyncMock(return_value=0.5)), \
             patch("related_recommender.db.cache_illust", new=AsyncMock()) as mock_cache, \
             patch("related_recommender.db.mark_pushed", new=AsyncMock()) as mock_mark:
            await recommender.push_chain(seed, [notifier], parent_msg_id=123, current_depth=2)

        pushed_illusts = notifier.push_illusts.await_args.args[0]
        self.assertEqual([ill.id for ill in pushed_illusts], [20])
        notifier.push_illusts.assert_awaited_once()
        self.assertEqual(notifier.push_illusts.await_args.kwargs["reply_to_message_id"], 123)
        mock_cache.assert_awaited_once()
        self.assertEqual(mock_cache.await_args.kwargs["source"], "related_chain")
        self.assertEqual(mock_cache.await_args.kwargs["chain_depth"], 2)
        self.assertEqual(mock_cache.await_args.kwargs["chain_parent_id"], 10)
        self.assertEqual(mock_cache.await_args.kwargs["chain_msg_id"], 999)
        mock_mark.assert_awaited_once_with(20, "related_chain")


if __name__ == "__main__":
    unittest.main()

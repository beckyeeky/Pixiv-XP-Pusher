import contextlib
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)

import database
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


class StubTagClassifier:
    async def classify_tags(self, tags):
        return {}


def build_chain_recommender(client, *, push_limit=1, profiler=None):
    return RelatedRecommender(
        client=client,
        config={
            "fetcher": {"bookmark_threshold": {"related": 0}},
            "filter": {"exclude_ai": True},
            "feedback": {"related_push_limit": push_limit},
            "tag_classifier": {},
        },
        profiler=profiler or SimpleNamespace(stop_words=[], _blocked_artist_ids=set()),
    )


@contextlib.contextmanager
def patched_chain_db():
    """隔离连锁推送的 db 协作者，暴露两个写操作 mock。"""
    with patch("related_recommender.TagClassifier", return_value=StubTagClassifier()), \
         patch("related_recommender.db.get_xp_profile", new=AsyncMock(return_value={"good": 2.0})), \
         patch("related_recommender.db.is_pushed", new=AsyncMock(return_value=False)), \
         patch("related_recommender.db.get_artist_score", new=AsyncMock(return_value=0.5)), \
         patch("related_recommender.db.cache_illust", new=AsyncMock()) as cache_mock, \
         patch("related_recommender.db.mark_pushed", new=AsyncMock()) as mark_mock:
        yield SimpleNamespace(cache=cache_mock, mark=mark_mock)


def cache_kwargs_by_id(mock_cache):
    return {call.kwargs["illust_id"]: call.kwargs for call in mock_cache.await_args_list}


def sqlite_int_ids(db_path, table):
    with sqlite3.connect(db_path) as connection:
        return {row[0] for row in connection.execute(f"SELECT illust_id FROM {table}")}


def sqlite_rows(db_path, query):
    with sqlite3.connect(db_path) as connection:
        return {tuple(row) for row in connection.execute(query)}


def two_candidate_client():
    client = AsyncMock()
    client.get_related_illusts = AsyncMock(
        return_value=[
            make_illust(20, tags=["good"], user_id=3),
            make_illust(21, tags=["good"], user_id=4),
            make_illust(40, tags=["good"], user_id=5, ai_type=2),
        ]
    )
    return client


class RelatedRecommenderChainDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """连锁推送落库必须与 notifier 确认的投递事实一致。"""

    async def test_chain_push_empty_results_skips_notifiers_and_db(self):
        seed = make_illust(10, tags=["seed"], user_id=1)
        notifier = SimpleNamespace(push_illusts=AsyncMock(return_value={20: 999}))
        recommender = build_chain_recommender(two_candidate_client(), push_limit=1)

        with patched_chain_db() as mocks:
            await recommender._push_chain_results(
                [],
                [notifier],
                message_prefix="🔗 连锁推荐",
                parent_msg_id=None,
                current_depth=1,
                seed_illust=seed,
            )

        notifier.push_illusts.assert_not_awaited()
        mocks.cache.assert_not_awaited()
        mocks.mark.assert_not_awaited()

    async def test_chain_push_with_no_related_works_writes_nothing(self):
        client = AsyncMock()
        client.get_related_illusts = AsyncMock(return_value=[])
        seed = make_illust(10, tags=["seed"], user_id=1)
        notifier = SimpleNamespace(push_illusts=AsyncMock(return_value={20: 999}))
        recommender = build_chain_recommender(client, push_limit=1)

        with patched_chain_db() as mocks:
            await recommender.push_chain(seed, [notifier], parent_msg_id=1, current_depth=1)

        notifier.push_illusts.assert_not_awaited()
        mocks.cache.assert_not_awaited()
        mocks.mark.assert_not_awaited()

    async def test_chain_push_partial_delivery_marks_only_confirmed_works(self):
        client = two_candidate_client()
        seed = make_illust(10, tags=["seed"], user_id=1)
        notifier = SimpleNamespace(push_illusts=AsyncMock(return_value={20: 999}))
        recommender = build_chain_recommender(client, push_limit=2)

        with patched_chain_db() as mocks:
            await recommender.push_chain(seed, [notifier], parent_msg_id=123, current_depth=2)

        self.assertEqual(notifier.push_illusts.await_count, 1)
        self.assertEqual([ill.id for ill in notifier.push_illusts.await_args.args[0]], [20, 21])
        self.assertEqual(notifier.push_illusts.await_args.kwargs["reply_to_message_id"], 123)
        mocks.mark.assert_awaited_once_with(20, "related_chain")
        cached = cache_kwargs_by_id(mocks.cache)
        self.assertEqual(set(cached), {20, 21})
        self.assertEqual(cached[20]["chain_msg_id"], 999)
        self.assertIsNone(cached[21]["chain_msg_id"])
        self.assertEqual(cached[21]["source"], "related_chain")
        self.assertEqual(cached[21]["chain_depth"], 2)
        self.assertEqual(cached[21]["chain_parent_id"], 10)

    async def test_chain_push_all_failed_records_no_pushed_exposure(self):
        client = two_candidate_client()
        seed = make_illust(10, tags=["seed"], user_id=1)
        notifier = SimpleNamespace(push_illusts=AsyncMock(return_value={}))
        recommender = build_chain_recommender(client, push_limit=2)

        with patched_chain_db() as mocks:
            await recommender.push_chain(seed, [notifier], parent_msg_id=1, current_depth=1)

        self.assertEqual(notifier.push_illusts.await_count, 1)
        mocks.mark.assert_not_awaited()
        cached = cache_kwargs_by_id(mocks.cache)
        self.assertEqual(set(cached), {20, 21})
        for kwargs in cached.values():
            self.assertIsNone(kwargs["chain_msg_id"])

    async def test_chain_push_without_notifiers_records_no_pushed_exposure(self):
        client = two_candidate_client()
        seed = make_illust(10, tags=["seed"], user_id=1)
        recommender = build_chain_recommender(client, push_limit=2)

        with patched_chain_db() as mocks:
            await recommender.push_chain(seed, [], parent_msg_id=None, current_depth=1)

        mocks.mark.assert_not_awaited()
        cached = cache_kwargs_by_id(mocks.cache)
        self.assertEqual(set(cached), {20, 21})
        for kwargs in cached.values():
            self.assertIsNone(kwargs["chain_msg_id"])

    async def test_chain_push_multiple_notifiers_mark_each_work_once(self):
        client = two_candidate_client()
        seed = make_illust(10, tags=["seed"], user_id=1)
        first = SimpleNamespace(push_illusts=AsyncMock(return_value={20: 999}))
        second = SimpleNamespace(push_illusts=AsyncMock(return_value={20: 777}))
        recommender = build_chain_recommender(client, push_limit=1)

        with patched_chain_db() as mocks:
            await recommender.push_chain(seed, [first, second], parent_msg_id=1, current_depth=1)

        first.push_illusts.assert_awaited_once()
        second.push_illusts.assert_awaited_once()
        mocks.mark.assert_awaited_once_with(20, "related_chain")
        mocks.cache.assert_awaited_once()
        self.assertEqual(mocks.cache.await_args.kwargs["chain_msg_id"], 999)

    async def test_chain_push_notifier_exception_does_not_block_other_notifiers(self):
        client = two_candidate_client()
        seed = make_illust(10, tags=["seed"], user_id=1)
        failing = SimpleNamespace(push_illusts=AsyncMock(side_effect=RuntimeError("tg down")))
        healthy = SimpleNamespace(push_illusts=AsyncMock(return_value={20: 999}))
        recommender = build_chain_recommender(client, push_limit=1)

        with patched_chain_db() as mocks:
            await recommender.push_chain(seed, [failing, healthy], parent_msg_id=1, current_depth=1)

        healthy.push_illusts.assert_awaited_once()
        mocks.mark.assert_awaited_once_with(20, "related_chain")
        mocks.cache.assert_awaited_once()
        self.assertEqual(mocks.cache.await_args.kwargs["chain_msg_id"], 999)

    async def test_chain_push_all_notifiers_raising_records_no_pushed_exposure(self):
        client = two_candidate_client()
        seed = make_illust(10, tags=["seed"], user_id=1)
        failing = SimpleNamespace(push_illusts=AsyncMock(side_effect=RuntimeError("tg down")))
        recommender = build_chain_recommender(client, push_limit=2)

        with patched_chain_db() as mocks:
            await recommender.push_chain(seed, [failing], parent_msg_id=1, current_depth=1)

        mocks.mark.assert_not_awaited()
        cached = cache_kwargs_by_id(mocks.cache)
        self.assertEqual(set(cached), {20, 21})
        for kwargs in cached.values():
            self.assertIsNone(kwargs["chain_msg_id"])


class RelatedRecommenderChainSqliteTests(unittest.IsolatedAsyncioTestCase):
    """真实（临时）SQLite 库上确认失败候选不会进入永久曝光表。"""

    async def test_partial_delivery_exposes_only_delivered_work(self):
        client = two_candidate_client()
        seed = make_illust(10, tags=["seed"], user_id=1)
        notifier = SimpleNamespace(push_illusts=AsyncMock(return_value={20: 999}))
        recommender = build_chain_recommender(client, push_limit=2)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "chain.db"
            with patch.object(database, "DB_PATH", db_path), \
                 patch("related_recommender.TagClassifier", return_value=StubTagClassifier()):
                await database.init_db()
                await recommender.push_chain(seed, [notifier], parent_msg_id=None, current_depth=1)

                self.assertEqual(sqlite_int_ids(db_path, "exposure_ids"), {20})
                self.assertEqual(
                    sqlite_rows(db_path, "SELECT illust_id, source FROM push_history"),
                    {(20, "related_chain")},
                )
                self.assertEqual(sqlite_int_ids(db_path, "illust_cache"), {20, 21})
                self.assertTrue(await database.is_pushed(20))
                self.assertFalse(await database.is_pushed(21))
                self.assertEqual(await database.get_chain_meta(21), (1, 10, None))
                self.assertIsNotNone(await database.get_cached_illust_tags(21))

    async def test_all_failed_keeps_exposure_tables_empty(self):
        client = two_candidate_client()
        seed = make_illust(10, tags=["seed"], user_id=1)
        notifier = SimpleNamespace(push_illusts=AsyncMock(return_value={}))
        recommender = build_chain_recommender(client, push_limit=2)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "chain.db"
            with patch.object(database, "DB_PATH", db_path), \
                 patch("related_recommender.TagClassifier", return_value=StubTagClassifier()):
                await database.init_db()
                await recommender.push_chain(seed, [notifier], parent_msg_id=None, current_depth=1)

                self.assertEqual(sqlite_int_ids(db_path, "exposure_ids"), set())
                self.assertEqual(sqlite_rows(db_path, "SELECT illust_id, source FROM push_history"), set())
                self.assertEqual(sqlite_int_ids(db_path, "illust_cache"), {20, 21})
                self.assertFalse(await database.is_pushed(20))
                self.assertFalse(await database.is_pushed(21))
                self.assertEqual(await database.get_chain_meta(20), (1, 10, None))


class RelatedRecommenderChainResultBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """不可用/非法的 notifier 返回值不得被当作成功投递。"""

    async def test_unusable_notifier_results_are_not_confirmation(self):
        seed = make_illust(10, tags=["seed"], user_id=1)
        recommender = build_chain_recommender(two_candidate_client(), push_limit=2)

        # None/0/False 是标量，空列表是旧式批量返回，字符串键与候选 int ID 不匹配。
        failures = []
        for payload in (None, 0, False, [], {"20": 999}):
            notifier = SimpleNamespace(push_illusts=AsyncMock(return_value=payload))
            with patched_chain_db() as mocks:
                await recommender.push_chain(seed, [notifier], parent_msg_id=1, current_depth=1)

            if mocks.mark.await_count:
                failures.append((payload, "mark_pushed", mocks.mark.await_args_list))
            cached = cache_kwargs_by_id(mocks.cache)
            if set(cached) != {20, 21}:
                failures.append((payload, "cached_ids", sorted(cached)))
            for illust_id, kwargs in cached.items():
                if kwargs["chain_msg_id"] is not None:
                    failures.append((payload, illust_id, "chain_msg_id", kwargs["chain_msg_id"]))

        self.assertEqual(failures, [])

    async def test_invalid_message_id_is_not_recorded_as_chain_link(self):
        seed = make_illust(10, tags=["seed"], user_id=1)
        recommender = build_chain_recommender(two_candidate_client(), push_limit=2)
        notifier = SimpleNamespace(push_illusts=AsyncMock(return_value={20: 0, 21: "abc"}))

        with patched_chain_db() as mocks:
            await recommender.push_chain(seed, [notifier], parent_msg_id=1, current_depth=1)

        # 非法消息号不构成 Telegram 发送成功的确认。
        cached = cache_kwargs_by_id(mocks.cache)
        self.assertEqual(set(cached), {20, 21})
        for kwargs in cached.values():
            self.assertIsNone(kwargs["chain_msg_id"])
        mocks.mark.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

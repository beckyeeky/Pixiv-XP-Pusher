"""Historical seed rotation: pool selection, leases and cooldown accounting.

These tests use the real SQLite schema created by ``database._init_db_sync``.
Only the Pixiv client and wall-clock values are controlled by the test.
"""

import asyncio
import json
import sqlite3
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

import database
from pixiv_client import Illust
from related_recommender import RelatedRecommender

HOUR = 3600
DAY = 24 * HOUR


def make_illust(illust_id: int, *, tags: list[str] | None = None, user_id: int = 1) -> Illust:
    return Illust(
        id=illust_id,
        title=f"illust-{illust_id}",
        user_id=user_id,
        user_name=f"artist-{user_id}",
        tags=tags or ["good"],
        tags_translated=[],
        bookmark_count=100,
        view_count=1000,
        page_count=1,
        image_urls=[f"https://example.com/{illust_id}.jpg"],
        is_r18=False,
        ai_type=0,
        create_date=datetime.now(),
    )


def execute(sql: str, params: tuple = ()) -> None:
    with sqlite3.connect(database.DB_PATH) as connection:
        connection.execute(sql, params)


def fetchone(sql: str, params: tuple = ()):
    with sqlite3.connect(database.DB_PATH) as connection:
        return connection.execute(sql, params).fetchone()


def add_pref_bookmark(illust_id: int, *, user_id: int = 900001, imported: int = 1) -> None:
    execute(
        "INSERT OR REPLACE INTO preference_bookmarks (user_id, illust_id, tags, imported) VALUES (?,?,?,?)",
        (user_id, illust_id, json.dumps(["good"]), imported),
    )


def add_xp_bookmark(illust_id: int, *, user_id: int = 900002) -> None:
    execute(
        "INSERT OR REPLACE INTO xp_bookmarks (illust_id, user_id, tags, illust_create_date) VALUES (?,?,?,CURRENT_TIMESTAMP)",
        (illust_id, user_id, json.dumps(["good"])),
    )


def add_feedback(illust_id: int, action: str) -> None:
    execute(
        "INSERT OR REPLACE INTO feedback (illust_id, action, created_at) VALUES (?,?,CURRENT_TIMESTAMP)",
        (illust_id, action),
    )


def add_pref_feedback(illust_id: int, action: str, *, artist_id=None) -> None:
    execute(
        "INSERT OR REPLACE INTO preference_feedback (illust_id, action, contributions, artist_id) VALUES (?,?,?,?)",
        (illust_id, action, "{}", artist_id),
    )


def add_author(illust_id: int, artist_id: int) -> None:
    execute(
        "INSERT OR REPLACE INTO feedback_metadata (illust_id, tags, user_id, user_name, source) VALUES (?,?,?,?,?)",
        (illust_id, json.dumps(["good"]), artist_id, "artist", "test"),
    )


def add_cache_author(illust_id: int, artist_id: int) -> None:
    execute(
        "INSERT OR REPLACE INTO illust_cache (illust_id, tags, user_id, user_name) VALUES (?,?,?,?)",
        (illust_id, json.dumps(["good"]), artist_id, "artist"),
    )


def block_artist(artist_id: int) -> None:
    execute(
        "INSERT OR IGNORE INTO blocked_artists (artist_id, artist_name) VALUES (?,?)",
        (artist_id, "blocked"),
    )


def build_recommender(client, *, bookmark_threshold: int = 0) -> RelatedRecommender:
    return RelatedRecommender(
        client=client,
        config={"fetcher": {"bookmark_threshold": {"related": bookmark_threshold}}},
        bookmark_threshold={"related": bookmark_threshold},
    )


class SeedPoolSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_bookmarks_remain_seeds_after_cache_cleanup(self):
        add_pref_bookmark(101, imported=1)
        add_xp_bookmark(102)

        seeds = await database.claim_related_seeds(max_seeds=2, now=1000.0)

        self.assertEqual([row["illust_id"] for row in seeds], [101, 102])
        # No dependency on disposable bookmark scan/cache tables.
        self.assertEqual(fetchone("SELECT COUNT(*) FROM bookmarks")[0], 0)
        self.assertEqual(fetchone("SELECT COUNT(*) FROM illust_cache")[0], 0)

    async def test_explicit_dislike_wins_over_like_in_either_ledger(self):
        for illust_id in (201, 202, 203, 204):
            add_pref_bookmark(illust_id)
            add_feedback(illust_id, "like")
        add_feedback(201, "dislike")
        add_pref_feedback(202, "dislike")

        seeds = await database.claim_related_seeds(max_seeds=2, now=1000.0)

        self.assertEqual([row["illust_id"] for row in seeds], [203, 204])

    async def test_known_blocked_artist_excluded_but_unknown_or_collector_not_guessed(self):
        add_pref_bookmark(301)
        add_author(301, 77)
        add_pref_bookmark(302)
        add_cache_author(302, 78)
        add_pref_bookmark(303)
        add_pref_feedback(303, "like", artist_id=79)
        for artist_id in (77, 78, 79):
            block_artist(artist_id)

        # Collector ID happens to equal a blocked artist; it is not the author.
        add_xp_bookmark(304, user_id=77)
        # No author metadata at all: unknown is not treated as blocked.
        add_pref_bookmark(305)

        seeds = await database.claim_related_seeds(max_seeds=2, now=1000.0)

        self.assertEqual([row["illust_id"] for row in seeds], [304, 305])

    async def test_pool_exhausted_and_success_cooldown_are_not_softened(self):
        add_pref_bookmark(401)
        seeds = await database.claim_related_seeds(max_seeds=2, now=1000.0)
        await database.finish_related_seed(401, seeds[0]["claim_token"], "success", now=1001.0)

        self.assertEqual(await database.claim_related_seeds(max_seeds=2, now=1002.0), [])
        cooldown_until = fetchone("SELECT cooldown_until FROM related_seed_state WHERE illust_id=401")[0]
        self.assertEqual(cooldown_until, 1001.0 + 7 * DAY)

    async def test_claim_returns_disjoint_leases_under_concurrency(self):
        for illust_id in range(501, 506):
            add_pref_bookmark(illust_id)

        first, second = await asyncio.gather(
            database.claim_related_seeds(max_seeds=2, now=1000.0),
            database.claim_related_seeds(max_seeds=2, now=1000.0),
        )

        first_ids = {row["illust_id"] for row in first}
        second_ids = {row["illust_id"] for row in second}
        self.assertEqual(len(first_ids), 2)
        self.assertEqual(len(second_ids), 2)
        self.assertTrue(first_ids.isdisjoint(second_ids))

    async def test_claim_budget_cannot_exceed_two_or_go_negative(self):
        for illust_id in range(550, 554):
            add_pref_bookmark(illust_id)

        oversized = await database.claim_related_seeds(max_seeds=100, now=1000.0)
        negative = await database.claim_related_seeds(max_seeds=-5, now=1000.0)

        self.assertEqual(len(oversized), 2)
        self.assertEqual(negative, [])

    async def test_stale_finish_cannot_overwrite_a_newer_lease(self):
        add_pref_bookmark(601)
        old = await database.claim_related_seeds(max_seeds=1, now=1000.0)
        # Lease expires, another worker claims the same seed.
        new = await database.claim_related_seeds(
            max_seeds=1, now=1000.0 + database.RELATED_SEED_CLAIM_LEASE_SECONDS + 1
        )
        self.assertEqual(old[0]["illust_id"], new[0]["illust_id"])
        self.assertNotEqual(old[0]["claim_token"], new[0]["claim_token"])

        applied = await database.finish_related_seed(
            old[0]["illust_id"], old[0]["claim_token"], "success", now=1000.0 + 700
        )

        self.assertFalse(applied)
        cooldown_until, failure_count = fetchone(
            "SELECT cooldown_until, failure_count FROM related_seed_state WHERE illust_id=601"
        )
        self.assertEqual(cooldown_until, 0)
        self.assertEqual(failure_count, 0)
        self.assertEqual(
            fetchone("SELECT claim_token FROM related_seed_state WHERE illust_id=601")[0],
            new[0]["claim_token"],
        )

    async def test_empty_and_error_use_exponential_backoff_capped_at_seven_days(self):
        add_pref_bookmark(701)
        now = 1000.0
        expected_hours = [6, 12, 24, 48, 96, 168, 168, 168]

        for expected in expected_hours:
            claims = await database.claim_related_seeds(max_seeds=1, now=now)
            self.assertEqual(len(claims), 1)
            self.assertTrue(
                await database.finish_related_seed(
                    701, claims[0]["claim_token"], "empty", now=now
                )
            )
            cooldown_until, failure_count = fetchone(
                "SELECT cooldown_until, failure_count FROM related_seed_state WHERE illust_id=701"
            )
            self.assertEqual(cooldown_until, now + expected * HOUR)
            now = cooldown_until

        # A later success resets the failure budget and applies the success cooldown.
        claims = await database.claim_related_seeds(max_seeds=1, now=now)
        await database.finish_related_seed(701, claims[0]["claim_token"], "error", now=now)
        cooldown_until, failure_count = fetchone(
            "SELECT cooldown_until, failure_count FROM related_seed_state WHERE illust_id=701"
        )
        self.assertEqual(cooldown_until, now + 7 * DAY)
        self.assertEqual(failure_count, 9)

        success_at = cooldown_until
        claims = await database.claim_related_seeds(max_seeds=1, now=success_at)
        await database.finish_related_seed(
            701, claims[0]["claim_token"], "success", now=success_at
        )
        cooldown_until, failure_count = fetchone(
            "SELECT cooldown_until, failure_count FROM related_seed_state WHERE illust_id=701"
        )
        self.assertEqual(cooldown_until, success_at + 7 * DAY)
        self.assertEqual(failure_count, 0)


class SeedRotationOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_or_negative_limit_makes_no_request(self):
        add_pref_bookmark(801)
        client = AsyncMock()

        for limit in (0, -1):
            result = await build_recommender(client).discover_for_strategy([("good", 1.0)], limit=limit)
            self.assertEqual(result, [])

        client.get_related_illusts.assert_not_awaited()

    async def test_first_seed_filling_limit_stops_before_second_call(self):
        add_pref_bookmark(901)
        add_pref_bookmark(902)
        client = AsyncMock()
        client.get_related_illusts = AsyncMock(
            return_value=[make_illust(910), make_illust(911), make_illust(912)]
        )

        result = await build_recommender(client).discover_for_strategy([("good", 1.0)], limit=2)

        self.assertEqual([ill.id for ill in result], [910, 911])
        self.assertEqual(client.get_related_illusts.await_count, 1)
        # Only the documented Pixiv read is used; no detail/extra client method.
        self.assertEqual(
            [call[0] for call in client.method_calls], ["get_related_illusts"]
        )
        # The unneeded second claim is released without an attempt.
        states = fetchone("SELECT COUNT(*), SUM(CASE WHEN claim_token IS NULL THEN 1 ELSE 0 END) FROM related_seed_state") 
        self.assertEqual(states, (2, 2))

    async def test_at_most_two_seeds_and_two_calls_per_round(self):
        for illust_id in (1001, 1002, 1003):
            add_pref_bookmark(illust_id)
        client = AsyncMock()
        client.get_related_illusts = AsyncMock(side_effect=[[make_illust(1010)], [make_illust(1011)]])

        result = await build_recommender(client).discover_for_strategy([("good", 1.0)], limit=5)

        self.assertEqual([ill.id for ill in result], [1010, 1011])
        self.assertEqual(client.get_related_illusts.await_count, 2)
        self.assertEqual(
            [call[0] for call in client.method_calls], ["get_related_illusts", "get_related_illusts"]
        )

    async def test_cross_pool_dedupes_and_excludes_every_round_seed(self):
        add_pref_bookmark(1101)
        add_xp_bookmark(1102)
        client = AsyncMock()
        client.get_related_illusts = AsyncMock(
            side_effect=[
                [make_illust(1102), make_illust(1201), make_illust(1201), make_illust(1202)],
                [make_illust(1102), make_illust(1201), make_illust(1203)],
            ]
        )

        result = await build_recommender(client).discover_for_strategy([("good", 1.0)], limit=10)

        self.assertEqual([ill.id for ill in result], [1201, 1202, 1203])
        self.assertEqual(client.get_related_illusts.await_count, 2)

    async def test_success_cooldown_prevents_immediate_reuse(self):
        add_pref_bookmark(1301)
        client = AsyncMock()
        client.get_related_illusts = AsyncMock(return_value=[make_illust(1310)])

        recommender = build_recommender(client)
        first = await recommender.discover_for_strategy([("good", 1.0)], limit=1)
        second = await recommender.discover_for_strategy([("good", 1.0)], limit=1)

        self.assertEqual([ill.id for ill in first], [1310])
        self.assertEqual(second, [])
        self.assertEqual(client.get_related_illusts.await_count, 1)

    async def test_rate_limit_stops_round_and_applies_short_cooldown(self):
        add_pref_bookmark(1401)
        add_pref_bookmark(1402)
        client = AsyncMock()
        client.get_related_illusts = AsyncMock(side_effect=Exception("429 Too Many Requests"))

        result = await build_recommender(client).discover_for_strategy([("good", 1.0)], limit=5)

        self.assertEqual(result, [])
        self.assertEqual(client.get_related_illusts.await_count, 1)
        cooldown_until, state = fetchone(
            "SELECT cooldown_until, state FROM related_seed_state WHERE illust_id=1401"
        )
        self.assertEqual(state, "throttled")
        self.assertAlmostEqual(cooldown_until, __import__("time").time() + HOUR, delta=120)
        self.assertIsNone(
            fetchone("SELECT claim_token FROM related_seed_state WHERE illust_id=1401")[0]
        )

    async def test_claim_failure_returns_empty_without_random_fallback(self):
        add_pref_bookmark(1501)
        client = AsyncMock()
        with patch(
            "related_recommender.db.claim_related_seeds",
            new=AsyncMock(side_effect=sqlite3.OperationalError("database is locked")),
        ), patch(
            "related_recommender.db.get_liked_illusts",
            new=AsyncMock(side_effect=AssertionError("legacy random path used")),
        ):
            with self.assertLogs("related_recommender", level="WARNING"):
                result = await build_recommender(client).discover_for_strategy(
                    [("good", 1.0)], limit=5
                )

        self.assertEqual(result, [])
        client.get_related_illusts.assert_not_awaited()

    async def test_scoring_keeps_xp_plus_artist_order_and_threshold(self):
        add_pref_bookmark(1601)
        client = AsyncMock()
        client.get_related_illusts = AsyncMock(
            return_value=[
                make_illust(1610, tags=["good"], user_id=10),
                make_illust(1611, tags=["other"], user_id=20),
                make_illust(1612, tags=["good"], user_id=30),
            ]
        )

        async def artist_score(user_id):
            return {10: 0.5, 20: 5.0, 30: 100.0}.get(user_id, 0.0)

        with patch("related_recommender.db.get_artist_score", new=AsyncMock(side_effect=artist_score)):
            result = await build_recommender(client, bookmark_threshold=100).discover_for_strategy(
                [("good", 2.0)], limit=2
            )

        self.assertEqual([ill.id for ill in result], [1612, 1611])


class SchemaAndInitTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_adds_seed_state_to_old_database_without_touching_data(self):
        execute("DROP TABLE IF EXISTS related_seed_state")
        execute("INSERT OR REPLACE INTO xp_profile (tag, weight) VALUES ('good', 3.0)")
        add_pref_bookmark(1701, user_id=42, imported=1)

        database._init_db_sync()

        self.assertIsNotNone(
            fetchone("SELECT 1 FROM sqlite_master WHERE type='table' AND name='related_seed_state'")
        )
        self.assertEqual(fetchone("SELECT tag, weight FROM xp_profile WHERE tag='good'"), ("good", 3.0))
        self.assertEqual(
            fetchone("SELECT user_id, imported FROM preference_bookmarks WHERE illust_id=1701"),
            (42, 1),
        )
        self.assertEqual(fetchone("SELECT COUNT(*) FROM related_seed_state")[0], 0)

    async def test_init_is_idempotent_and_keeps_existing_seed_state(self):
        execute(
            "INSERT OR REPLACE INTO xp_profile (tag, weight) VALUES ('good', 3.0)"
        )
        execute(
            "INSERT OR REPLACE INTO related_seed_state (illust_id, state, cooldown_until, failure_count, last_attempt_at, updated_at) "
            "VALUES (1701, 'success', 123.0, 0, 100.0, 100.0)"
        )

        database._init_db_sync()
        first_profile = fetchone("SELECT tag, weight FROM xp_profile WHERE tag='good'")
        first_state = fetchone(
            "SELECT state, cooldown_until, failure_count FROM related_seed_state WHERE illust_id=1701"
        )
        database._init_db_sync()
        second_profile = fetchone("SELECT tag, weight FROM xp_profile WHERE tag='good'")
        second_state = fetchone(
            "SELECT state, cooldown_until, failure_count FROM related_seed_state WHERE illust_id=1701"
        )

        self.assertEqual(first_profile, ("good", 3.0))
        self.assertEqual(first_profile, second_profile)
        self.assertEqual(first_state, ("success", 123.0, 0))
        self.assertEqual(first_state, second_state)
        self.assertIsNotNone(
            fetchone("SELECT 1 FROM sqlite_master WHERE type='table' AND name='related_seed_state'")
        )

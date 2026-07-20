import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from pixiv_client import Illust
from semantic_vector_explorer import (
    SemanticVectorExplorer,
    cosine_similarity,
    duplicate_semantic_rate,
    preference_profile_concentration,
    slate_profile_concentration,
)


def illust(illust_id: int, tags=None) -> Illust:
    return Illust(
        id=illust_id, title=str(illust_id), user_id=illust_id, user_name="artist",
        tags=tags or ["feature"], bookmark_count=100, view_count=1000,
        page_count=1, image_urls=["https://example.test/image.jpg"],
        is_r18=False, ai_type=0, create_date=datetime.now(),
    )


class SemanticVectorMetricTests(unittest.TestCase):
    def test_cosine_and_duplicate_rate_ignore_incompatible_vectors(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertIsNone(cosine_similarity([1], [1, 0]))
        self.assertAlmostEqual(
            duplicate_semantic_rate([[1, 0], [0.99, 0.01], [0, 1]], 0.9), 1 / 3,
        )

    def test_profile_concentration_uses_selected_slate_support(self):
        result = slate_profile_concentration(
            [illust(1, ["a"]), illust(2, ["a", "b"])], {"a": 1.0, "b": 2.0},
        )
        self.assertAlmostEqual(result, 0.5)

    def test_preference_profile_concentration_uses_profile_weights(self):
        self.assertAlmostEqual(
            preference_profile_concentration({"a": 1.0, "b": 1.0, "ignored": -2.0}),
            0.5,
        )


class SemanticVectorExplorerTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieve_uses_current_cache_and_records_hydrated_candidates(self):
        detail_loader = AsyncMock(side_effect=lambda item_id: None if item_id == 3 else illust(item_id))
        explorer = SemanticVectorExplorer(
            {"enabled": True, "pool_limit": 3, "candidate_limit": 2, "min_similarity": 0.5},
            model="embed-v1", detail_loader=detail_loader,
        )
        with patch(
            "semantic_vector_explorer.db.get_current_user_embedding",
            new=AsyncMock(return_value=[1.0, 0.0]),
        ) as get_user, patch(
            "semantic_vector_explorer.db.get_vector_exploration_pool",
            new=AsyncMock(return_value=[(1, [0.9, 0.1]), (2, [0.7, 0.3]), (3, [1.0, 0.0])]),
        ) as get_pool, patch(
            "semantic_vector_explorer.db.start_vector_exploration_run", new=AsyncMock(),
        ) as start_run, patch(
            "semantic_vector_explorer.db.record_vector_exploration_candidates", new=AsyncMock(),
        ) as record:
            batch = await explorer.retrieve(user_id=7, profile={"tag": 1.0}, exclude_ids={9})

        self.assertIsNotNone(batch.run_id)
        self.assertEqual([item.id for item in batch.candidates], [1, 2])
        self.assertTrue(all(item.exploration_only for item in batch.candidates))
        self.assertTrue(all(item.source == "semantic_vector_exploration" for item in batch.candidates))
        get_user.assert_awaited_once()
        get_pool.assert_awaited_once_with("embed-v1", 3, exclude_ids={9})
        start_run.assert_awaited_once()
        self.assertEqual(start_run.await_args.kwargs["profile_concentration"], 1.0)
        rows = record.await_args.args[1]
        self.assertEqual([row["illust_id"] for row in rows], [1, 2])
        self.assertEqual([row["retrieval_rank"] for row in rows], [1, 2])

    async def test_retrieve_skips_without_current_cached_profile_vector(self):
        explorer = SemanticVectorExplorer(
            {"enabled": True}, model="embed-v1", detail_loader=AsyncMock(),
        )
        with patch(
            "semantic_vector_explorer.db.get_current_user_embedding",
            new=AsyncMock(return_value=None),
        ), patch(
            "semantic_vector_explorer.db.get_vector_exploration_pool", new=AsyncMock(),
        ) as get_pool:
            batch = await explorer.retrieve(user_id=7, profile={"tag": 1.0})
        self.assertIsNone(batch.run_id)
        self.assertEqual(batch.candidates, [])
        get_pool.assert_not_awaited()

import asyncio
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)

from notifier.base import DELIVERY_DELIVERED, DELIVERY_QUEUED, DeliveryBatchResult, DeliveryItem
from delivery_reconciliation import DeliveryReconciliationModule
from push_run import PushRun, VectorExplorationBatch, start_profile_maintenance
from push_stats import PushStats


class DeliveryResultNotifier:
    async def send_with_result(self, illusts):
        return DeliveryBatchResult([
            DeliveryItem(
                illust_id=illusts[0].id,
                status=DELIVERY_DELIVERED,
                message_id=101,
            ),
            DeliveryItem(illust_id=illusts[1].id, status=DELIVERY_QUEUED),
        ])


class MemoryDeliveryPersistence:
    def __init__(self):
        self.prepared = []
        self.committed = []

    async def prepare(self, illusts):
        self.prepared = list(illusts)

    async def commit(self, deliveries, _completed_at):
        self.committed = list(deliveries)


class PushRunDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_profile_embedding_is_ready_before_vector_exploration(self):
        stats = PushStats()
        client = AsyncMock()
        client.fetch_following.return_value = set()
        profiler = AsyncMock()
        profiler.get_top_tags.return_value = [("feature", 1.0)]
        embedder = SimpleNamespace(
            enabled=True,
            model="embed-v1",
            embed_tags=AsyncMock(return_value=[1.0, 0.0]),
        )
        runner = PushRun(
            config={
                "pixiv": {"user_id": 7},
                "profiler": {},
                "fetcher": {
                    "semantic_vector_exploration": {"enabled": True},
                },
                "filter": {"daily_slate": {"enabled": True}},
            },
            client=client,
            profiler=profiler,
            notifiers=[],
            stats=stats,
            send_summary=False,
        )
        filter_result = SimpleNamespace(selected=[], ranked=[])
        content_filter = SimpleNamespace(
            filter_with_result=AsyncMock(return_value=filter_result),
        )
        save_user_embedding = AsyncMock()
        cache_ready_at_retrieve = []

        async def retrieve(*_args, **_kwargs):
            cache_ready_at_retrieve.append(save_user_embedding.await_count > 0)
            return VectorExplorationBatch(None, [])

        with patch.object(runner, "_build_tag_classifier", return_value=None), \
             patch.object(runner, "_build_embedder", return_value=embedder), \
             patch.object(runner, "_build_ai_scorer", return_value=None), \
             patch.object(runner, "_push_filtered", new=AsyncMock()), \
             patch("push_run.ContentFetcher") as fetcher_type, \
             patch("push_run.ContentFilter", return_value=content_filter), \
             patch("push_run.SemanticVectorExplorer.retrieve", side_effect=retrieve), \
             patch("push_run.db_module.get_xp_profile", new=AsyncMock(return_value={"feature": 1.0})), \
             patch("push_run.db_module.get_current_user_embedding", new=AsyncMock(return_value=None)), \
             patch("push_run.db_module.save_user_embedding", new=save_user_embedding), \
             patch("push_run.db_module.set_state", new=AsyncMock()):
            fetcher_type.return_value.fetch_content = AsyncMock(return_value=[])
            await runner.execute()

        self.assertEqual(cache_ready_at_retrieve, [True])
        embedder.embed_tags.assert_awaited_once_with(["feature"])
        save_user_embedding.assert_awaited_once()

    async def test_active_maintenance_is_reused_instead_of_started_twice(self):
        started = asyncio.Event()
        release = asyncio.Event()
        classifier = AsyncMock()

        async def maintain(_profile):
            started.set()
            await release.wait()

        classifier.maintain_profile_tags.side_effect = maintain
        with patch("push_run.db_module.set_state", new=AsyncMock()) as set_state:
            first_task = start_profile_maintenance(classifier, {"tag": 1.0})
            await started.wait()
            second_task = start_profile_maintenance(classifier, {"tag": 1.0})
            release.set()
            await first_task
            await asyncio.sleep(0)

        self.assertIs(second_task, first_task)
        classifier.maintain_profile_tags.assert_awaited_once_with({"tag": 1.0})
        self.assertIn(
            "runtime.last_maintenance_background_status",
            [call.args[0] for call in set_state.await_args_list],
        )

    async def test_push_filtered_marks_only_delivered_items_as_pushed(self):
        stats = PushStats()
        persistence = MemoryDeliveryPersistence()
        runner = PushRun(
            config={},
            client=AsyncMock(),
            profiler=SimpleNamespace(),
            notifiers=[DeliveryResultNotifier()],
            stats=stats,
            delivery_reconciliation=DeliveryReconciliationModule(
                persistence,
                stats,
            ),
        )
        filtered = [
            SimpleNamespace(id=1, tags=["a"], user_id=10, user_name="artist", source="xp_search"),
            SimpleNamespace(id=2, tags=["b"], user_id=20, user_name="artist", source="related"),
        ]

        with patch.object(runner.stats, "record_push_success", wraps=runner.stats.record_push_success) as mock_success, \
             patch.object(runner.stats, "record_push_failed", wraps=runner.stats.record_push_failed) as mock_failed, \
             patch.object(runner.stats, "record_push_queued", wraps=runner.stats.record_push_queued) as mock_queued, \
             patch.object(runner.stats, "record_ai_error", wraps=runner.stats.record_ai_error):
            await runner._push_filtered(filtered)

        self.assertEqual([item.illust.id for item in persistence.committed], [1])
        self.assertEqual(persistence.committed[0].message_ids, (101,))
        mock_success.assert_called_once_with("xp_search")
        mock_failed.assert_not_called()
        mock_queued.assert_called_once()
        self.assertEqual(stats.push_success_count, 1)
        self.assertEqual(stats.push_failed_count, 0)
        self.assertEqual(stats.push_queued_count, 1)


if __name__ == "__main__":
    unittest.main()

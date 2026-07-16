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
from push_run import PushRun, start_profile_maintenance
from push_stats import PushStats


class DeliveryResultNotifier:
    async def send_with_result(self, illusts):
        return DeliveryBatchResult([
            DeliveryItem(illust_id=illusts[0].id, status=DELIVERY_DELIVERED),
            DeliveryItem(illust_id=illusts[1].id, status=DELIVERY_QUEUED),
        ])


class PushRunDeliveryTests(unittest.IsolatedAsyncioTestCase):
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
        runner = PushRun(
            config={},
            client=AsyncMock(),
            profiler=SimpleNamespace(),
            notifiers=[DeliveryResultNotifier()],
            stats=stats,
        )
        filtered = [
            SimpleNamespace(id=1, tags=["a"], user_id=10, user_name="artist", source="xp_search"),
            SimpleNamespace(id=2, tags=["b"], user_id=20, user_name="artist", source="related"),
        ]

        with patch("push_run.cache_illust", new=AsyncMock()), \
             patch("push_run.mark_pushed", new=AsyncMock()) as mock_mark_pushed, \
             patch.object(runner.stats, "record_push_success", wraps=runner.stats.record_push_success) as mock_success, \
             patch.object(runner.stats, "record_push_failed", wraps=runner.stats.record_push_failed) as mock_failed, \
             patch.object(runner.stats, "record_push_queued", wraps=runner.stats.record_push_queued) as mock_queued, \
             patch.object(runner.stats, "record_ai_error", wraps=runner.stats.record_ai_error), \
             patch("push_run.db_module.update_strategy_stats", new=AsyncMock()) as mock_strategy_stats, \
             patch("push_run.db_module.set_state", new=AsyncMock()):
            await runner._push_filtered(filtered)

        mock_mark_pushed.assert_awaited_once_with(1, "xp_search")
        mock_strategy_stats.assert_awaited_once_with("xp_search", is_success=False)
        mock_success.assert_called_once_with("xp_search")
        mock_failed.assert_not_called()
        mock_queued.assert_called_once()
        self.assertEqual(stats.push_success_count, 1)
        self.assertEqual(stats.push_failed_count, 0)
        self.assertEqual(stats.push_queued_count, 1)


if __name__ == "__main__":
    unittest.main()

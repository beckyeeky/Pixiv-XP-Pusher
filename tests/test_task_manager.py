import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import json
import sys
import types
from datetime import datetime, timedelta

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)

import task_manager
from push_stats import PushStats


class _ClosableClient:
    def __init__(self, maintenance_finished: asyncio.Event):
        self._maintenance_finished = maintenance_finished
        self.close = AsyncMock(side_effect=self._assert_maintenance_finished)

    async def _assert_maintenance_finished(self):
        if not self._maintenance_finished.is_set():
            raise AssertionError("shared client closed before maintenance finished")


class MainTaskRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_report_cleans_embeddings_older_than_ninety_days(self):
        notifier = AsyncMock()
        notifier.send_text = AsyncMock()

        with patch("database.get_top_xp_tags", new=AsyncMock(return_value=[])), \
             patch("database.get_all_strategy_stats", new=AsyncMock(return_value={})), \
             patch("database.sync_blocked_tags_to_xp", new=AsyncMock(return_value=0)), \
             patch("database.cleanup_old_sent_history", new=AsyncMock(return_value=0)), \
             patch("database.cleanup_old_illust_cache", new=AsyncMock(return_value=0)), \
             patch("database.cleanup_old_embeddings", new=AsyncMock(return_value=12)) as cleanup_embeddings:
            await task_manager.daily_report_task({}, [notifier])

        cleanup_embeddings.assert_awaited_once_with(days=90)
        self.assertIn("清理 12 条过期作品向量", notifier.send_text.await_args.args[0])

    async def test_run_once_waits_for_maintenance_after_successful_delivery(self):
        maintenance_finished = asyncio.Event()

        async def maintain():
            await asyncio.sleep(0)
            maintenance_finished.set()

        maintenance_task = asyncio.create_task(maintain())
        client = _ClosableClient(maintenance_finished)
        stats = PushStats()
        stats.record_push_success("xp_search")

        with patch.object(task_manager, "setup_services", new=AsyncMock(return_value=(client, client, AsyncMock(), []))), \
             patch.object(task_manager, "main_task", new=AsyncMock(return_value=stats)), \
             patch.object(task_manager, "get_latest_maintenance_task", return_value=maintenance_task), \
             patch.object(task_manager.db_module, "set_state", new=AsyncMock()) as set_state:
            result = await task_manager.run_once({})

        self.assertIs(result, stats)
        self.assertTrue(maintenance_task.done())
        self.assertTrue(maintenance_finished.is_set())
        client.close.assert_awaited_once()
        self.assertIn(
            "runtime.last_maintenance_completion",
            [call.args[0] for call in set_state.await_args_list],
        )

    async def test_run_once_records_timeout_without_invalidating_successful_delivery(self):
        maintenance_task = asyncio.create_task(asyncio.sleep(60))
        client = AsyncMock()
        stats = PushStats()
        stats.record_push_success("xp_search")

        with patch.object(task_manager, "setup_services", new=AsyncMock(return_value=(client, client, AsyncMock(), []))), \
             patch.object(task_manager, "main_task", new=AsyncMock(return_value=stats)), \
             patch.object(task_manager, "get_latest_maintenance_task", return_value=maintenance_task), \
             patch.object(task_manager, "MAINTENANCE_WAIT_SECONDS", 0.01), \
             patch.object(task_manager.db_module, "set_state", new=AsyncMock()) as set_state:
            result = await task_manager.run_once({})

        self.assertIs(result, stats)
        self.assertTrue(maintenance_task.cancelled())
        completion_values = [
            json.loads(call.args[1])
            for call in set_state.await_args_list
            if call.args[0] == "runtime.last_maintenance_completion"
        ]
        self.assertIn("timeout", [value["status"] for value in completion_values])

    async def test_run_once_records_already_settled_maintenance_after_delivery(self):
        maintenance_task = asyncio.create_task(asyncio.sleep(0))
        await maintenance_task
        client = AsyncMock()
        stats = PushStats()
        stats.record_push_success("xp_search")

        with patch.object(task_manager, "setup_services", new=AsyncMock(return_value=(client, client, AsyncMock(), []))), \
             patch.object(task_manager, "main_task", new=AsyncMock(return_value=stats)), \
             patch.object(task_manager, "get_latest_maintenance_task", return_value=maintenance_task), \
             patch.object(task_manager.db_module, "set_state", new=AsyncMock()) as set_state:
            await task_manager.run_once({})

        completion_values = [
            json.loads(call.args[1])
            for call in set_state.await_args_list
            if call.args[0] == "runtime.last_maintenance_completion"
        ]
        self.assertIn("succeeded", [value["status"] for value in completion_values])
    async def test_display_tags_max_ip_count_helper_handles_malformed_direct_config(self):
        self.assertEqual(task_manager._get_display_tags_max_ip_count({"display_tags": "bad"}), 2)
        self.assertEqual(task_manager._get_display_tags_max_ip_count({"display_tags": {"max_ip_count": "4"}}), "4")

    async def test_main_task_does_not_raise_unboundlocal_when_profile_build_fails_early(self):
        config = {
            "pixiv": {"user_id": 123},
            "profiler": {},
            "fetcher": {},
            "filter": {},
            "ai": {},
            "notifier": {},
        }

        client = AsyncMock()
        profiler = AsyncMock()
        profiler.build_profile.side_effect = RuntimeError("boom")

        with patch.object(task_manager.db_module, "set_state", new=AsyncMock()) as mock_set_state:
            stats = await task_manager.main_task(
                config=config,
                client=client,
                profiler=profiler,
                notifiers=[],
                sync_client=client,
                force=True,
            )

        self.assertIsNotNone(stats)
        self.assertEqual(mock_set_state.await_count, 2)
        first_call = mock_set_state.await_args_list[0]
        second_call = mock_set_state.await_args_list[1]
        self.assertEqual(first_call.args[0], "runtime.last_run_started_at")
        self.assertEqual(second_call.args[0], "runtime.last_run_summary")
        summary = json.loads(second_call.args[1])
        self.assertEqual(summary["fetch_count"], 0)
        self.assertEqual(summary["filtered_count"], 0)
        self.assertEqual(summary["pushed"], 0)
        self.assertEqual(summary["failed"], 0)

    async def test_main_task_sends_summary_for_scheduled_runs(self):
        config = {
            "pixiv": {"user_id": 123},
            "profiler": {},
            "fetcher": {},
            "filter": {},
            "ai": {},
            "notifier": {},
        }

        client = AsyncMock()
        profiler = AsyncMock()
        profiler.build_profile.side_effect = RuntimeError("boom")
        notifier = AsyncMock()
        notifier.send_text = AsyncMock()

        with patch.object(task_manager.db_module, "set_state", new=AsyncMock()):
            await task_manager.main_task(
                config=config,
                client=client,
                profiler=profiler,
                notifiers=[notifier],
                sync_client=client,
                force=True,
                send_summary=True,
            )

        notifier.send_text.assert_awaited_once()

    async def test_main_task_can_skip_summary_for_manual_runs(self):
        config = {
            "pixiv": {"user_id": 123},
            "profiler": {},
            "fetcher": {},
            "filter": {},
            "ai": {},
            "notifier": {},
        }

        client = AsyncMock()
        profiler = AsyncMock()
        profiler.build_profile.side_effect = RuntimeError("boom")
        notifier = AsyncMock()
        notifier.send_text = AsyncMock()

        with patch.object(task_manager.db_module, "set_state", new=AsyncMock()):
            await task_manager.main_task(
                config=config,
                client=client,
                profiler=profiler,
                notifiers=[notifier],
                sync_client=client,
                force=True,
                send_summary=False,
            )

        notifier.send_text.assert_not_awaited()

    async def test_should_skip_immediate_run_when_last_push_within_interval(self):
        now = datetime(2026, 5, 4, 20, 0, 0)
        last_push_at = now - timedelta(hours=2)

        self.assertTrue(
            task_manager._should_skip_immediate_run(
                last_push_at=last_push_at,
                min_interval=timedelta(hours=4),
                now=now,
            )
        )
        self.assertFalse(
            task_manager._should_skip_immediate_run(
                last_push_at=last_push_at,
                min_interval=timedelta(hours=1),
                now=now,
            )
        )

    async def test_run_scheduler_skips_immediate_run_when_recent_push_exists(self):
        config = {"scheduler": {}}
        scheduler = MagicMock()
        scheduler.add_job = MagicMock()
        scheduler.start = MagicMock()

        async def fake_sleep(_seconds):
            raise asyncio.CancelledError()

        async def fake_get_state(key):
            if key == "runtime.last_successful_push_at":
                return (datetime.now() - timedelta(hours=1)).isoformat()
            return None

        with patch.object(task_manager, "setup_services", new=AsyncMock(return_value=(AsyncMock(), AsyncMock(), AsyncMock(), []))), \
             patch.object(task_manager, "AsyncIOScheduler", return_value=scheduler), \
             patch.object(task_manager.DatabasePushScheduleState, "read", new=AsyncMock(return_value="0 12 * * *")), \
             patch.object(task_manager.db_module, "get_state", new=AsyncMock(side_effect=fake_get_state)), \
             patch.object(task_manager.db_module, "get_last_push_at", new=AsyncMock(return_value=None)), \
             patch.object(task_manager.asyncio, "create_task") as mock_create_task, \
             patch.object(task_manager.asyncio, "sleep", new=AsyncMock(side_effect=fake_sleep)):
            with self.assertRaises(asyncio.CancelledError):
                await task_manager.run_scheduler(config, run_immediately=True)

        mock_create_task.assert_not_called()

    async def test_run_scheduler_uses_database_schedule_without_config_cron(self):
        config = {"scheduler": {"cron": "0 12 * * *"}}
        scheduler = MagicMock()
        scheduler.add_job = MagicMock()
        scheduler.start = MagicMock()

        async def fake_sleep(_seconds):
            raise asyncio.CancelledError()

        with patch.object(task_manager, "setup_services", new=AsyncMock(return_value=(AsyncMock(), AsyncMock(), AsyncMock(), []))), \
             patch.object(task_manager, "AsyncIOScheduler", return_value=scheduler), \
             patch.object(task_manager.DatabasePushScheduleState, "read", new=AsyncMock(return_value="30 9 * * *,0 21 * * *")) as get_schedule, \
             patch.object(task_manager.asyncio, "sleep", new=AsyncMock(side_effect=fake_sleep)):
            with self.assertRaises(asyncio.CancelledError):
                await task_manager.run_scheduler(config, run_immediately=False)

        get_schedule.assert_awaited_once()
        self.assertEqual(scheduler.add_job.call_count, 3)


if __name__ == "__main__":
    unittest.main()

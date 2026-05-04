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


class MainTaskRegressionTests(unittest.IsolatedAsyncioTestCase):
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
        profiler.ai_processor.occurred_errors = []

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
        profiler.ai_processor.occurred_errors = []
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
        profiler.ai_processor.occurred_errors = []
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
        config = {"scheduler": {"cron": "0 12 * * *"}}
        scheduler = MagicMock()
        scheduler.add_job = MagicMock()
        scheduler.start = MagicMock()

        async def fake_sleep(_seconds):
            raise asyncio.CancelledError()

        async def fake_get_state(key):
            if key == "schedule_cron":
                return None
            if key == "runtime.last_successful_push_at":
                return "2026-05-04T18:30:00"
            return None

        with patch.object(task_manager, "setup_services", new=AsyncMock(return_value=(AsyncMock(), AsyncMock(), AsyncMock(), []))), \
             patch.object(task_manager, "AsyncIOScheduler", return_value=scheduler), \
             patch.object(task_manager.db_module, "get_state", new=AsyncMock(side_effect=fake_get_state)), \
             patch.object(task_manager.db_module, "get_last_push_at", new=AsyncMock(return_value=None)), \
             patch.object(task_manager.asyncio, "create_task") as mock_create_task, \
             patch.object(task_manager.asyncio, "sleep", new=AsyncMock(side_effect=fake_sleep)):
            with self.assertRaises(asyncio.CancelledError):
                await task_manager.run_scheduler(config, run_immediately=True)

        mock_create_task.assert_not_called()

    async def test_run_scheduler_warns_when_db_schedule_differs_from_config(self):
        config = {"scheduler": {"cron": "0 12 * * *"}}
        scheduler = MagicMock()
        scheduler.add_job = MagicMock()
        scheduler.start = MagicMock()

        async def fake_sleep(_seconds):
            raise asyncio.CancelledError()

        with patch.object(task_manager, "setup_services", new=AsyncMock(return_value=(AsyncMock(), AsyncMock(), AsyncMock(), []))), \
             patch.object(task_manager, "AsyncIOScheduler", return_value=scheduler), \
             patch.object(task_manager.db_module, "get_state", new=AsyncMock(return_value="0 20 * * *")), \
             patch.object(task_manager.logger, "warning") as mock_warning, \
             patch.object(task_manager.asyncio, "sleep", new=AsyncMock(side_effect=fake_sleep)):
            with self.assertRaises(asyncio.CancelledError):
                await task_manager.run_scheduler(config, run_immediately=False)

        mock_warning.assert_any_call(
            "检测到数据库中的 schedule_cron (%s) 与 config.yaml 中的 scheduler.cron (%s) 不一致；当前仍使用数据库值，请按需在 Telegram 菜单或配置文件中统一。",
            "0 20 * * *",
            "0 12 * * *",
        )


if __name__ == "__main__":
    unittest.main()

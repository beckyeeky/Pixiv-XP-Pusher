import unittest
from unittest.mock import AsyncMock, patch
import json

import task_manager


class MainTaskRegressionTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()

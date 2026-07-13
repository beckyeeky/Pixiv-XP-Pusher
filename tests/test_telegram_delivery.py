import asyncio
import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, call, patch

try:
    from notifier.telegram import TelegramNotifier
except ImportError:  # pragma: no cover - dependency may be absent in minimal envs
    TelegramNotifier = None


@unittest.skipIf(TelegramNotifier is None, "python-telegram-bot is not installed")
class TelegramDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_with_result_waits_for_worker_delivery_result(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.send_queue = asyncio.Queue()
        notifier.batch_mode = "single"

        async def fake_send_direct(self, illusts, custom_title=None, batch_mode=None):
            return [illusts[0].id]

        notifier._send_direct = MethodType(fake_send_direct, notifier)
        worker = asyncio.create_task(notifier._process_queue())
        try:
            result = await asyncio.wait_for(
                notifier.send_with_result([SimpleNamespace(id=1), SimpleNamespace(id=2)]),
                timeout=1,
            )
        finally:
            worker.cancel()
            await worker

        self.assertEqual(result.delivered_ids, [1])
        self.assertEqual(result.failed_ids, [2])
        self.assertEqual(result.queued_ids, [])

    async def test_tag_review_menu_runs_gemini_for_the_current_queue_and_reports_remaining_count(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier._tag_review_batch_running = False
        query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())
        summary = {
            "attempted": 2, "accepted": 1, "unresolved": 1, "failed": 0,
            "human_override": 0, "usage": {"total": 23, "search_queries": 2},
        }

        with patch("database.get_tag_review_count", new=AsyncMock(side_effect=[2, 1])), \
             patch("database.get_tag_review_queue", new=AsyncMock(return_value=[{"tag": "one"}, {"tag": "two"}])) as queue, \
             patch("notifier.telegram.load_config", return_value={"tag_classifier": {"maintenance": {"concurrency": 3}}}), \
             patch("notifier.telegram.run_scheduled_maintenance", new=AsyncMock(return_value=summary)) as run:
            await notifier._handle_menu_callback(query, "menu:tag_review:run")

        queue.assert_awaited_once_with(limit=2)
        run.assert_awaited_once_with(
            ["one", "two"], {"tag_classifier": {"maintenance": {"concurrency": 3}}}, concurrency=3,
        )
        self.assertFalse(notifier._tag_review_batch_running)
        self.assertIn("当前待人工决定：*1*", query.edit_message_text.await_args_list[-1].args[0])

    async def test_tag_review_menu_displays_exact_pending_count(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())

        with patch("database.get_tag_review_count", new=AsyncMock(return_value=7)), \
             patch("database.get_high_weight_unclassified_profile_tags", new=AsyncMock(return_value=[])):
            await notifier._handle_menu_callback(query, "menu:tag_review")

        self.assertIn("当前待人工决定标签：*7* 个", query.edit_message_text.await_args.args[0])

    async def test_tag_review_menu_requires_candidate_preview_before_high_weight_classification(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier._tag_review_batch_running = False
        notifier._high_weight_tag_review_snapshots = {}
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=123),
            edit_message_text=AsyncMock(),
            answer=AsyncMock(),
        )
        candidates = [
            {"tag": "high_weight", "profile_weight": 4.0, "classification": None},
            {"tag": "unresolved", "profile_weight": 3.0, "classification": "unresolved"},
        ]
        summary = {"accepted": 1, "unresolved": 1, "failed": 0}

        with patch("database.get_high_weight_unclassified_profile_tags", new=AsyncMock(side_effect=[candidates, candidates])) as select, \
             patch("notifier.telegram.load_config", return_value={"tag_classifier": {"maintenance": {"concurrency": 3}}}), \
             patch("notifier.telegram.run_scheduled_maintenance", new=AsyncMock(return_value=summary)) as run:
            await notifier._handle_menu_callback(query, "menu:tag_review:high_weight")
            await notifier._handle_menu_callback(query, "menu:tag_review:high_weight:confirm")

        self.assertIn("高权重未分类候选", query.edit_message_text.await_args_list[0].args[0])
        select.assert_has_awaits([
            call(limit=40, min_profile_weight=1.0),
            call(limit=40, min_profile_weight=1.0),
        ])
        run.assert_awaited_once_with(
            ["high_weight", "unresolved"], {"tag_classifier": {"maintenance": {"concurrency": 3}}}, concurrency=3,
        )


if __name__ == "__main__":
    unittest.main()

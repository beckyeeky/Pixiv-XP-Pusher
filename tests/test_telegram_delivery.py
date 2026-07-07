import asyncio
import unittest
from types import MethodType, SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()

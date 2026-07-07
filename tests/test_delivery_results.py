import unittest
from types import SimpleNamespace

from notifier.base import (
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_QUEUED,
    BaseNotifier,
    DeliveryBatchResult,
)


class StubNotifier(BaseNotifier):
    def __init__(self, delivered_ids):
        self.delivered_ids = delivered_ids

    async def send(self, illusts):
        return self.delivered_ids

    def format_message(self, illust):
        return ""

    def handle_feedback(self, illust_id: int, action: str) -> bool:
        return True


class DeliveryResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_base_notifier_adapts_legacy_send_ids_to_delivery_result(self):
        notifier = StubNotifier(delivered_ids=[1])
        result = await notifier.send_with_result([
            SimpleNamespace(id=1),
            SimpleNamespace(id=2),
        ])

        self.assertEqual(result.delivered_ids, [1])
        self.assertEqual(result.failed_ids, [2])
        self.assertEqual([item.status for item in result.items], [DELIVERY_DELIVERED, DELIVERY_FAILED])

    async def test_delivery_batch_result_separates_queued_delivered_failed(self):
        result = DeliveryBatchResult.queued([1, 2])

        self.assertEqual(result.accepted_ids, [1, 2])
        self.assertEqual(result.queued_ids, [1, 2])
        self.assertEqual(result.delivered_ids, [])
        self.assertEqual(result.items[0].status, DELIVERY_QUEUED)


if __name__ == "__main__":
    unittest.main()

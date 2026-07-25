import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from delivery_reconciliation import (
    DatabaseDeliveryPersistence,
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_QUEUED,
    DeliveryBatchResult,
    DeliveryItem,
    DeliveryReconciliationModule,
)
from push_stats import PushStats


class MemoryDeliveryPersistence:
    def __init__(self):
        self.prepared = []
        self.committed = []
        self.completed_at = None

    async def prepare(self, illusts):
        self.prepared = list(illusts)

    async def commit(self, deliveries, completed_at):
        self.committed = list(deliveries)
        self.completed_at = completed_at


class ResultNotifier:
    def __init__(self, result):
        self.result = result

    async def send_with_result(self, _illusts):
        return self.result


class LegacyNotifier:
    async def send(self, _illusts):
        return [2]


class FailingDeliveryPersistence(MemoryDeliveryPersistence):
    async def prepare(self, _illusts):
        raise RuntimeError("database unavailable")


class FailingCommitPersistence(MemoryDeliveryPersistence):
    async def commit(self, _deliveries, _completed_at):
        raise RuntimeError("history unavailable")


class DeliveryReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_module_reconciles_partial_multi_adapter_delivery(self):
        persistence = MemoryDeliveryPersistence()
        stats = PushStats()
        module = DeliveryReconciliationModule(persistence, stats)
        illusts = [
            SimpleNamespace(id=1, source="xp_search"),
            SimpleNamespace(id=2, source="related"),
            SimpleNamespace(id=3, source="ranking"),
        ]
        first = ResultNotifier(DeliveryBatchResult([
            DeliveryItem(1, DELIVERY_DELIVERED, message_id=101),
            DeliveryItem(2, DELIVERY_QUEUED),
            DeliveryItem(3, DELIVERY_FAILED, error="rejected"),
        ]))

        summary = await module.deliver(illusts, [first, LegacyNotifier()])

        self.assertEqual(summary.delivered_ids, (1, 2))
        self.assertEqual(summary.queued_ids, ())
        self.assertEqual(summary.failed_ids, (3,))
        self.assertEqual(stats.push_success_count, 2)
        self.assertEqual(stats.push_queued_count, 0)
        self.assertEqual(stats.push_failed_count, 1)
        self.assertEqual(
            [delivery.illust.id for delivery in persistence.committed],
            [1, 2],
        )
        self.assertEqual(persistence.committed[0].message_ids, (101,))
        self.assertEqual(persistence.committed[1].message_ids, ())

    async def test_queued_only_result_is_not_persisted_as_delivered(self):
        persistence = MemoryDeliveryPersistence()
        stats = PushStats()
        module = DeliveryReconciliationModule(persistence, stats)
        illust = SimpleNamespace(id=1, source="xp_search")

        summary = await module.reconcile(
            [illust],
            [DeliveryBatchResult.queued([1])],
        )

        self.assertEqual(summary.queued_ids, (1,))
        self.assertEqual(persistence.prepared, [illust])
        self.assertEqual(persistence.committed, [])
        self.assertEqual(stats.push_queued_count, 1)
        self.assertEqual(stats.push_success_count, 0)

    async def test_database_adapter_persists_history_attribution_and_message_link(self):
        stats = PushStats()
        module = DeliveryReconciliationModule(
            DatabaseDeliveryPersistence(),
            stats,
        )
        illust = SimpleNamespace(
            id=1,
            tags=["tag"],
            user_id=10,
            user_name="artist",
            source="xp_search",
        )
        notifier = ResultNotifier(DeliveryBatchResult([
            DeliveryItem(1, DELIVERY_DELIVERED, message_id=101),
        ]))

        with patch("database.cache_illust", new=AsyncMock()) as cache, \
             patch("database.mark_pushed", new=AsyncMock()) as mark, \
             patch("database.update_strategy_stats", new=AsyncMock()) as strategy, \
             patch("database.set_state", new=AsyncMock()) as set_state, \
             patch("database.set_chain_meta", new=AsyncMock()) as chain:
            await module.deliver([illust], [notifier])

        cache.assert_awaited_once_with(
            1,
            ["tag"],
            10,
            "artist",
            source="xp_search",
        )
        mark.assert_awaited_once_with(1, "xp_search")
        strategy.assert_awaited_once_with("xp_search", is_success=False)
        self.assertEqual(set_state.await_args.args[0], "runtime.last_successful_push_at")
        chain.assert_awaited_once_with(1, chain_depth=0, chain_msg_id=101)

    async def test_persistence_failure_is_owned_by_reconciliation_summary(self):
        stats = PushStats()
        module = DeliveryReconciliationModule(
            FailingDeliveryPersistence(),
            stats,
        )
        illust = SimpleNamespace(id=1, source="xp_search")

        summary = await module.deliver([illust], [LegacyNotifier()])

        self.assertEqual(summary.failed_ids, (1,))
        self.assertEqual(summary.delivered_ids, ())
        self.assertEqual(stats.push_failed_count, 1)

    async def test_confirmed_delivery_remains_delivered_when_history_write_fails(self):
        stats = PushStats()
        module = DeliveryReconciliationModule(
            FailingCommitPersistence(),
            stats,
        )
        illust = SimpleNamespace(id=1, source="xp_search")
        notifier = ResultNotifier(DeliveryBatchResult.from_delivered_ids([1], [1]))

        summary = await module.deliver([illust], [notifier])

        self.assertEqual(summary.delivered_ids, (1,))
        self.assertEqual(summary.failed_ids, ())
        self.assertEqual(summary.persistence_error, "history unavailable")
        self.assertEqual(stats.push_success_count, 1)
        self.assertEqual(stats.push_failed_count, 0)

    def test_delivery_fact_rejects_unknown_status(self):
        with self.assertRaisesRegex(ValueError, "未知投递状态"):
            DeliveryItem(1, "accepted")


if __name__ == "__main__":
    unittest.main()

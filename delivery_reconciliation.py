"""Delivery facts and their single reconciliation path.

Notifier adapters report observed per-work facts.  This module owns how facts
from multiple adapters become push history, strategy attribution, message
linkage, run statistics, and a stable delivery summary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, Sequence


logger = logging.getLogger(__name__)

DELIVERY_QUEUED = "queued"
DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED = "failed"
DELIVERY_STATUSES = frozenset({
    DELIVERY_QUEUED,
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
})
ATTRIBUTED_STRATEGIES = frozenset({
    "xp_search",
    "subscription",
    "ranking",
    "related",
    "engagement_artists",
})


@dataclass(frozen=True)
class DeliveryItem:
    """One adapter's observed delivery fact for one work."""

    illust_id: int
    status: str
    message_id: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in DELIVERY_STATUSES:
            raise ValueError(f"未知投递状态: {self.status}")


@dataclass
class DeliveryBatchResult:
    """Observed facts returned by one notifier adapter."""

    items: list[DeliveryItem] = field(default_factory=list)

    @classmethod
    def from_delivered_ids(
        cls,
        requested_ids: list[int],
        delivered_ids: list[int],
        message_ids: dict[int, int] | None = None,
    ) -> "DeliveryBatchResult":
        delivered_set = set(delivered_ids)
        message_ids = message_ids or {}
        return cls([
            DeliveryItem(
                illust_id=illust_id,
                status=(
                    DELIVERY_DELIVERED
                    if illust_id in delivered_set
                    else DELIVERY_FAILED
                ),
                message_id=message_ids.get(illust_id),
            )
            for illust_id in requested_ids
        ])

    @classmethod
    def queued(cls, requested_ids: list[int]) -> "DeliveryBatchResult":
        return cls([
            DeliveryItem(illust_id=illust_id, status=DELIVERY_QUEUED)
            for illust_id in requested_ids
        ])

    @classmethod
    def failed(
        cls,
        requested_ids: list[int],
        error: str | None = None,
    ) -> "DeliveryBatchResult":
        return cls([
            DeliveryItem(
                illust_id=illust_id,
                status=DELIVERY_FAILED,
                error=error,
            )
            for illust_id in requested_ids
        ])

    @property
    def accepted_ids(self) -> list[int]:
        return [
            item.illust_id
            for item in self.items
            if item.status in {DELIVERY_QUEUED, DELIVERY_DELIVERED}
        ]

    @property
    def queued_ids(self) -> list[int]:
        return [
            item.illust_id
            for item in self.items
            if item.status == DELIVERY_QUEUED
        ]

    @property
    def delivered_ids(self) -> list[int]:
        return [
            item.illust_id
            for item in self.items
            if item.status == DELIVERY_DELIVERED
        ]

    @property
    def failed_ids(self) -> list[int]:
        return [
            item.illust_id
            for item in self.items
            if item.status == DELIVERY_FAILED
        ]


@dataclass(frozen=True)
class ConfirmedDelivery:
    illust: object
    message_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class DeliverySummary:
    requested_ids: tuple[int, ...]
    delivered_ids: tuple[int, ...]
    queued_ids: tuple[int, ...]
    failed_ids: tuple[int, ...]
    persistence_error: str | None = None


class DeliveryPersistence(Protocol):
    async def prepare(self, illusts: Sequence[object]) -> None: ...

    async def commit(
        self,
        deliveries: Sequence[ConfirmedDelivery],
        completed_at: datetime,
    ) -> None: ...


class DatabaseDeliveryPersistence:
    """Production adapter for delivery-related database writes."""

    async def prepare(self, illusts: Sequence[object]) -> None:
        import database

        for illust in illusts:
            await database.cache_illust(
                illust.id,
                illust.tags,
                illust.user_id,
                illust.user_name,
                source=illust.source,
            )

    async def commit(
        self,
        deliveries: Sequence[ConfirmedDelivery],
        completed_at: datetime,
    ) -> None:
        import database

        for delivery in deliveries:
            illust = delivery.illust
            source = getattr(illust, "source", "unknown")
            await database.mark_pushed(illust.id, source)
            if source in ATTRIBUTED_STRATEGIES:
                await database.update_strategy_stats(source, is_success=False)
        await database.set_state(
            "runtime.last_successful_push_at",
            completed_at.isoformat(),
        )
        for delivery in deliveries:
            for message_id in delivery.message_ids:
                await database.set_chain_meta(
                    delivery.illust.id,
                    chain_depth=0,
                    chain_msg_id=message_id,
                )


class DeliveryReconciliationModule:
    """Deliver one Daily Slate and reconcile all adapter facts."""

    def __init__(self, persistence: DeliveryPersistence, stats):
        self._persistence = persistence
        self._stats = stats

    async def deliver(
        self,
        illusts: Sequence[object],
        notifiers: Sequence[object],
    ) -> DeliverySummary:
        requested_ids = [illust.id for illust in illusts]
        try:
            await self._persistence.prepare(illusts)
            results = []
            for notifier in notifiers:
                try:
                    send_with_result = getattr(notifier, "send_with_result", None)
                    if callable(send_with_result):
                        result = await send_with_result(list(illusts))
                    else:
                        sent_ids = await notifier.send(list(illusts))
                        result = DeliveryBatchResult.from_delivered_ids(
                            requested_ids,
                            sent_ids,
                        )
                    if not isinstance(result, DeliveryBatchResult):
                        raise TypeError(
                            f"{type(notifier).__name__} 返回了无效投递结果"
                        )
                    results.append(result)
                except Exception as exc:
                    logger.error(
                        "推送器 %s 发送失败: %s",
                        type(notifier).__name__,
                        exc,
                    )
            return await self._reconcile(illusts, results)
        except Exception as exc:
            return self._failed_summary(requested_ids, exc)

    async def reconcile(
        self,
        illusts: Sequence[object],
        results: Sequence[DeliveryBatchResult],
    ) -> DeliverySummary:
        """Reconcile already-observed facts without sending a second time."""
        requested_ids = [illust.id for illust in illusts]
        try:
            await self._persistence.prepare(illusts)
            return await self._reconcile(illusts, results)
        except Exception as exc:
            return self._failed_summary(requested_ids, exc)

    def _failed_summary(
        self,
        requested_ids: Sequence[int],
        error: Exception,
    ) -> DeliverySummary:
        logger.error("投递 reconciliation 失败: %s", error)
        for _item_id in requested_ids:
            self._stats.record_push_failed()
        requested = tuple(requested_ids)
        return DeliverySummary(
            requested_ids=requested,
            delivered_ids=(),
            queued_ids=(),
            failed_ids=requested,
            persistence_error=str(error),
        )

    async def _reconcile(
        self,
        illusts: Sequence[object],
        results: Sequence[DeliveryBatchResult],
    ) -> DeliverySummary:
        requested = tuple(illust.id for illust in illusts)
        illust_by_id = {illust.id: illust for illust in illusts}
        delivered = set()
        queued = set()
        message_ids: dict[int, list[int]] = {}
        for result in results:
            for item in result.items:
                if item.illust_id not in illust_by_id:
                    logger.warning(
                        "收到未匹配的推送结果 ID: %s，跳过 reconciliation",
                        item.illust_id,
                    )
                    continue
                if item.status == DELIVERY_DELIVERED:
                    delivered.add(item.illust_id)
                    if item.message_id is not None:
                        message_ids.setdefault(item.illust_id, []).append(
                            item.message_id
                        )
                elif item.status == DELIVERY_QUEUED:
                    queued.add(item.illust_id)

        queued -= delivered
        failed = set(requested) - delivered - queued
        ordered_delivered = tuple(item_id for item_id in requested if item_id in delivered)
        ordered_queued = tuple(item_id for item_id in requested if item_id in queued)
        ordered_failed = tuple(item_id for item_id in requested if item_id in failed)

        for item_id in ordered_queued:
            self._stats.record_push_queued()
        for item_id in ordered_delivered:
            source = getattr(illust_by_id[item_id], "source", "unknown")
            self._stats.record_push_success(source)
        for _item_id in ordered_failed:
            self._stats.record_push_failed()

        persistence_error = None
        if ordered_delivered:
            try:
                await self._persistence.commit(
                    [
                        ConfirmedDelivery(
                            illust=illust_by_id[item_id],
                            message_ids=tuple(message_ids.get(item_id, ())),
                        )
                        for item_id in ordered_delivered
                    ],
                    datetime.now(),
                )
            except Exception as exc:
                persistence_error = str(exc)
                logger.error(
                    "投递已确认，但 reconciliation 持久化失败: %s",
                    exc,
                )
            logger.info(
                "推送完成: %s/%s 个作品成功",
                len(ordered_delivered),
                len(requested),
            )
        elif ordered_queued:
            logger.warning("作品已进入发送队列，但尚未确认任何作品送达")
        else:
            logger.error("没有任何作品被成功推送")

        return DeliverySummary(
            requested_ids=requested,
            delivered_ids=ordered_delivered,
            queued_ids=ordered_queued,
            failed_ids=ordered_failed,
            persistence_error=persistence_error,
        )

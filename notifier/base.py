"""
推送器抽象基类
"""
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from delivery_reconciliation import (
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_QUEUED,
    DeliveryBatchResult,
    DeliveryItem,
)

if TYPE_CHECKING:
    from pixiv_client import Illust


class BaseNotifier(ABC):
    """推送器抽象基类"""

    CAPABILITIES = {
        "send_text": True,
        "push_illusts": False,
        "reply_thread": False,
        "topic_routing": False,
        "batch_mode": False,
        "rich_message": False,
    }
    
    @abstractmethod
    async def send(self, illusts: list["Illust"]) -> list[int]:
        """
        发送推送
        
        Args:
            illusts: 作品列表
            
        Returns:
            是否成功
        """
        pass

    def _delivery_message_ids(self, delivered_ids: list[int]) -> dict[int, int]:
        delivered_set = set(delivered_ids)
        return {
            illust_id: message_id
            for message_id, illust_id in getattr(
                self, "_message_illust_map", {}
            ).items()
            if illust_id in delivered_set
        }

    async def send_with_result(self, illusts: list["Illust"]) -> DeliveryBatchResult:
        """Send and return explicit delivery state.

        Default adapter keeps legacy notifiers working: IDs returned by send()
        are interpreted as delivered, and missing IDs as failed.
        """
        requested_ids = [illust.id for illust in illusts]
        delivered_ids = await self.send(illusts)
        return DeliveryBatchResult.from_delivered_ids(
            requested_ids,
            delivered_ids,
            self._delivery_message_ids(delivered_ids),
        )
    
    @abstractmethod
    def format_message(self, illust: "Illust") -> str:
        """
        格式化单条消息
        
        Args:
            illust: 作品对象
            
        Returns:
            格式化后的消息文本
        """
        pass
    
    @abstractmethod
    def handle_feedback(self, illust_id: int, action: str) -> bool:
        """
        处理用户反馈
        
        Args:
            illust_id: 作品ID
            action: 'like' | 'dislike'
            
        Returns:
            是否处理成功
        """
        pass


    @classmethod
    def capabilities(cls) -> dict[str, bool]:
        """返回当前推送器声明的能力边界。"""
        return dict(cls.CAPABILITIES)

    @classmethod
    def supports(cls, capability: str) -> bool:
        """查询某项能力是否受支持。"""
        return bool(cls.CAPABILITIES.get(capability, False))

    async def send_text(self, text: str, buttons: list[tuple[str, str]] | None = None) -> bool:
        """
        发送纯文本消息（可选带按钮）
        
        Args:
            text: 消息文本
            buttons: 按钮列表 [(标签, callback_data), ...]
        """
        # 默认实现不发送或仅打印
        return True

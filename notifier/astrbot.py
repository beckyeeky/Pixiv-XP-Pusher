"""
AstrBot HTTP API 推送实现
通过 astrbot_plugin_http_adapter 插件的 HTTP 接口发送消息
"""
import asyncio
import logging
import json
import base64
import io
from typing import TYPE_CHECKING, Callable, Optional

import aiohttp

from .base import BaseNotifier

if TYPE_CHECKING:
    from pixiv_client import Illust, PixivClient

logger = logging.getLogger(__name__)


class AstrBotNotifier(BaseNotifier):
    """AstrBot HTTP API 推送器"""

    CAPABILITIES = {
        **BaseNotifier.CAPABILITIES,
        "batch_mode": False,
    }
    
    
    def __init__(
        self,
        http_url: str,
        unified_msg_origin: str,
        api_key: str = None,
        on_feedback: Optional[Callable] = None,
        on_action: Optional[Callable] = None,
        client: Optional['PixivClient'] = None,
        max_pages: int = 10,
        image_quality: int = 85,
        max_image_size: int = 1500
    ):
        """
        初始化 AstrBot 推送器
        
        Args:
            http_url: HTTP API 地址 (如 http://127.0.0.1:6185)
            unified_msg_origin: 目标会话标识 (如 QQOfficial:group:123456)
            api_key: API 密钥 (如果启用了认证)
            on_feedback: 反馈回调函数
            on_action: 动作回调函数
            client: PixivClient 实例 (用于下载图片)
            max_pages: 多图作品最大页数
            image_quality: JPEG 压缩质量
            max_image_size: 图片最大边长
        """
        self.http_url = http_url.rstrip('/')
        self.unified_msg_origin = unified_msg_origin
        self.api_key = api_key
        self.on_feedback = on_feedback
        self.on_action = on_action
        self.client = client
        self.max_pages = max_pages
        self.image_quality = image_quality
        self.max_image_size = max_image_size
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._message_illust_map: dict[int, int] = {}  # msg_id -> illust_id
        
        logger.info(f"AstrBot 推送目标: {unified_msg_origin}")
    
    async def _ensure_session(self):
        """确保 HTTP session 已创建"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
    
    async def close(self):
        """关闭连接"""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def _post_message(self, message_chain: list) -> dict | None:
        """
        发送消息到 AstrBot HTTP API
        
        Args:
            message_chain: 消息链列表，格式: [{"type": "Plain", "text": "..."}, {"type": "Image", "base64": "..."}]
            
        Returns:
            API 响应或 None
        """
        await self._ensure_session()
        
        url = f"{self.http_url}/api/v1/send"
        
        payload = {
            "unified_msg_origin": self.unified_msg_origin,
            "message": message_chain
        }
        
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        try:
            async with self._session.post(url, json=payload, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    logger.debug(f"AstrBot 消息发送成功: {result}")
                    return result
                else:
                    text = await resp.text()
                    logger.error(f"AstrBot 消息发送失败 [{resp.status}]: {text}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"AstrBot HTTP 请求失败: {e}")
            return None
        except asyncio.TimeoutError:
            logger.error("AstrBot 请求超时")
            return None
    
    async def _download_and_encode_image(self, url: str) -> str | None:
        """下载图片并转为 Base64"""
        try:
            from utils import download_image_with_referer
            from PIL import Image
            
            await self._ensure_session()
            image_data = await download_image_with_referer(self._session, url)
            
            if not image_data:
                return None
            
            # 压缩处理
            with Image.open(io.BytesIO(image_data)) as img:
                # 处理透明度
                if img.mode == 'P':
                    img = img.convert('RGBA')
                if img.mode in ('RGBA', 'LA'):
                    bg = Image.new('RGB', img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[-1])
                    img = bg
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                
                # 缩放
                if max(img.size) > self.max_image_size:
                    img.thumbnail((self.max_image_size, self.max_image_size), Image.Resampling.LANCZOS)
                
                # 压缩为 JPEG
                output = io.BytesIO()
                img.save(output, format="JPEG", quality=self.image_quality, optimize=True)
                
                return base64.b64encode(output.getvalue()).decode()
                
        except Exception as e:
            logger.warning(f"AstrBot 图片处理失败: {e}")
            return None
    
    def format_message(self, illust: 'Illust') -> str:
        """格式化消息文本"""
        display_tags_list = getattr(illust, 'display_tags', illust.tags)
        tags = " ".join(f"#{t}" for t in display_tags_list[:5])
        r18_mark = "🔞 " if illust.is_r18 else ""
        ugoira_mark = "🎞️ " if getattr(illust, 'type', 'illust') == 'ugoira' else ""
        page_info = f" ({illust.page_count}P)" if illust.page_count > 1 else ""
        
        match_score = getattr(illust, 'match_score', None)
        match_line = f"🎯 匹配度: {match_score*100:.0f}%\n" if match_score is not None else ""
        
        long_mark = "📚 " if illust.page_count > self.max_pages else ""
        
        return (
            f"{long_mark}{r18_mark}{ugoira_mark}🎨 {illust.title}{page_info}\n"
            f"👤 {illust.user_name}\n"
            f"❤️ {illust.bookmark_count}\n"
            f"{match_line}"
            f"🏷️ {tags}\n"
            f"🔗 https://pixiv.net/i/{illust.id}\n\n"
            f"回复 {illust.id} 1=喜欢 2=不喜欢"
        )
    
    async def send(self, illusts: list['Illust']) -> list[int]:
        """发送推送"""
        if not illusts:
            return []
        
        success_ids = []
        
        for illust in illusts:
            try:
                message_chain = []
                
                # 1. 处理图片
                if illust.image_urls:
                    # 多图作品只发送封面
                    cover_url = illust.image_urls[0]
                    img_b64 = await self._download_and_encode_image(cover_url)
                    
                    if img_b64:
                        message_chain.append({
                            "type": "Image",
                            "base64": img_b64
                        })
                    else:
                        # 回退：使用 pixiv.cat 链接
                        from utils import get_pixiv_cat_url
                        cat_url = get_pixiv_cat_url(illust.id)
                        message_chain.append({
                            "type": "Image",
                            "url": cat_url
                        })
                
                # 2. 添加文本
                text = self.format_message(illust)
                message_chain.append({
                    "type": "Plain",
                    "text": text
                })
                
                # 3. 发送
                result = await self._post_message(message_chain)
                
                if result:
                    success_ids.append(illust.id)
                    # 如果返回了消息 ID，记录映射
                    msg_id = result.get("message_id") or result.get("msg_id")
                    if msg_id:
                        self._message_illust_map[msg_id] = illust.id
                
                # 发送间隔
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"AstrBot 发送作品 {illust.id} 失败: {e}")
        
        logger.info(f"AstrBot 推送完成: {len(success_ids)}/{len(illusts)}")
        return success_ids
    
    async def send_text(self, text: str, buttons: list[tuple[str, str]] | None = None) -> bool:
        """发送纯文本消息"""
        message_chain = [{"type": "Plain", "text": text}]
        
        # AstrBot 可能不支持按钮，忽略 buttons 参数
        if buttons:
            # 将按钮信息附加到文本末尾
            btn_text = "\n\n" + "\n".join(f"• {label}" for label, _ in buttons)
            message_chain[0]["text"] += btn_text
        
        result = await self._post_message(message_chain)
        return result is not None
    
    async def handle_feedback(self, illust_id: int, action: str) -> bool:
        """处理反馈"""
        if self.on_feedback:
            await self.on_feedback(illust_id, action)
        return True

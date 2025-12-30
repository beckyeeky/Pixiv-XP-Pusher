"""
OneBot 协议推送实现
兼容 go-cqhttp, Lagrange 等
"""
import asyncio
import logging
import json
from typing import Callable, Optional

import aiohttp

from .base import BaseNotifier
from pixiv_client import Illust
from utils import get_pixiv_cat_url
import base64

logger = logging.getLogger(__name__)


class OneBotNotifier(BaseNotifier):
    """OneBot v11 协议推送（链接模式）"""
    
    def __init__(
        self,
        ws_url: str,
        # 推送目标配置
        private_id: str | None = None,    # 私聊推送目标 QQ
        group_id: str | None = None,       # 群聊推送目标群号
        push_to_private: bool = True,      # 是否推送到私聊
        push_to_group: bool = False,       # 是否推送到群聊
        # 权限控制
        master_id: str | None = None,      # 主人 QQ（只有主人指令有效）
        on_feedback: Optional[Callable] = None
    ):
        self.ws_url = ws_url
        self.private_id = int(private_id) if private_id else None
        self.group_id = int(group_id) if group_id else None
        self.push_to_private = push_to_private and self.private_id is not None
        self.push_to_group = push_to_group and self.group_id is not None
        self.master_id = int(master_id) if master_id else None
        self.on_feedback = on_feedback
        
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._message_illust_map: dict[int, int] = {}
        self._last_illust_id: int | None = None
        
        # 日志
        targets = []
        if self.push_to_private:
            targets.append(f"私聊:{self.private_id}")
        if self.push_to_group:
            targets.append(f"群:{self.group_id}")
        logger.info(f"OneBot 推送目标: {', '.join(targets) or '无'}")
        if self.master_id:
            logger.info(f"主人 QQ: {self.master_id}")
    
    async def connect(self):
        """连接WebSocket"""
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self.ws_url)
        logger.info(f"已连接到 OneBot: {self.ws_url}")
    
    async def close(self):
        """关闭连接"""
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
    
    async def send(self, illusts: list[Illust]) -> list[int]:
        """发送推送"""
        if not illusts:
            return []
        
        if not self._ws:
            await self.connect()
        
        success_ids = []
        
        # 预先处理所有图片（下载+压缩+Base64）
        # 为了不阻塞太久，我们并发处理
        tasks = [self._prepare_illust_content(ill) for ill in illusts]
        prepared_data = await asyncio.gather(*tasks)
        
        # 尝试使用合并转发消息
        nodes = []
        for content in prepared_data:
            nodes.append(self._create_node(content))
        
        try:
            await self._send_forward(nodes)
            # 如果合并转发成功，所有作品都算成功
            success_ids = [i.id for i in illusts]
            logger.info(f"OneBot 合并转发成功 ({len(illusts)} 条)")
        except Exception as e:
            logger.error(f"合并转发失败: {e}")
            logger.info("降级为逐条发送...")
            
            # Fallback: 逐条发送
            for ill, content in zip(illusts, prepared_data):
                try:
                    await self._send_message(content)
                    success_ids.append(ill.id)
                    await asyncio.sleep(2)
                except Exception as e2:
                    logger.error(f"发送作品 {ill.id} 失败: {e2}")
        
        return success_ids
    
    async def _prepare_illust_content(self, illust: Illust) -> str:
        """下载图片并生成最终消息内容"""
        image_cq = ""
        try:
            # 优先尝试下载原图并转Base64
            # 使用列表中的第一个链接 (通常是 Original 或 Large)
            target_url = illust.image_urls[0] if illust.image_urls else None
            
            if target_url:
                # 复用 utils 中的下载函数
                from utils import download_image_with_referer
                image_data = await download_image_with_referer(self._session, target_url)
                
                # 压缩图片 (复用 PIL 逻辑)
                import io
                from PIL import Image
                
                with Image.open(io.BytesIO(image_data)) as img:
                    # 转换为 RGB
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    
                    # 限制最大尺寸 (QQ 推荐不要过大)
                    max_size = 1920
                    if max(img.size) > max_size:
                        img.thumbnail((max_size, max_size))
                    
                    # 压缩保存
                    output = io.BytesIO()
                    img.save(output, format="JPEG", quality=85)
                    jpeg_data = output.getvalue()
                    
                    b64 = base64.b64encode(jpeg_data).decode()
                    image_cq = f"[CQ:image,file=base64://{b64}]"
            
        except Exception as e:
            logger.warning(f"图片下载/处理失败 {illust.id}: {e}")
            # 失败兜底：使用 pixiv.cat 反代链接
            cat_url = get_pixiv_cat_url(illust.id)
            image_cq = f"[CQ:image,file={cat_url}]"

        # 如果上面都没生成（比如没URL），再兜底
        if not image_cq:
             cat_url = get_pixiv_cat_url(illust.id)
             image_cq = f"[CQ:image,file={cat_url}]"

        return self.format_message(illust, image_cq)
            
    async def _send_single(self, illust: Illust):
        """发送单条消息 (已弃用，逻辑合并到 send)"""
        pass
    
    def format_message(self, illust: Illust, image_cq: str = None) -> str:
        """格式化消息"""
        tags = " ".join(f"#{t}" for t in illust.tags[:5])
        r18_mark = "🔞 " if illust.is_r18 else ""
        
        # 多页提示
        page_info = f" ({illust.page_count}P)" if illust.page_count > 1 else ""
        
        # 匹配度显示
        match_score = getattr(illust, 'match_score', None)
        match_line = f"🎯 匹配度: {match_score*100:.0f}%\n" if match_score is not None else ""
        
        # 如果未传入 image_cq (兼容旧调用)，生成反代链接
        if not image_cq:
             url = get_pixiv_cat_url(illust.id)
             image_cq = f"[CQ:image,file={url}]"
        
        return (
            f"{image_cq}\n"
            f"{r18_mark}🎨 {illust.title}{page_info}\n"
            f"👤 {illust.user_name}\n"
            f"❤️ {illust.bookmark_count}\n"
            f"{match_line}"
            f"🏷️ {tags}\n"
            f"🔗 https://pixiv.net/i/{illust.id}\n\n"
            f"💬 反馈: {illust.id} 1=喜欢 2=不喜欢"
        )
    
    async def _send_message(self, content: str, target_type: str = None, target_id: int = None):
        """
        发送普通消息
        
        Args:
            content: 消息内容
            target_type: 指定目标类型 ('private'|'group')，None 则发送到所有配置目标
            target_id: 指定目标 ID，None 则使用配置
        """
        targets = []
        
        if target_type and target_id:
            # 指定目标
            targets.append((target_type, target_id))
        else:
            # 发送到所有配置目标
            if self.push_to_private:
                targets.append(("private", self.private_id))
            if self.push_to_group:
                targets.append(("group", self.group_id))
        
        for t_type, t_id in targets:
            action = "send_private_msg" if t_type == "private" else "send_group_msg"
            id_field = "user_id" if t_type == "private" else "group_id"
            
            payload = {
                "action": action,
                "params": {
                    id_field: t_id,
                    "message": content
                }
            }
            await self._ws.send_json(payload)
    
    async def _send_forward(self, nodes: list[dict]):
        """发送合并转发消息到所有配置目标"""
        targets = []
        if self.push_to_private:
            targets.append(("private", self.private_id))
        if self.push_to_group:
            targets.append(("group", self.group_id))
        
        for t_type, t_id in targets:
            action = "send_private_forward_msg" if t_type == "private" else "send_group_forward_msg"
            id_field = "user_id" if t_type == "private" else "group_id"
            
            payload = {
                "action": action,
                "params": {
                    id_field: t_id,
                    "messages": nodes
                }
            }
            await self._ws.send_json(payload)
    
    def _create_node(self, content: str) -> dict:
        """创建转发节点"""
        return {
            "type": "node",
            "data": {
                "name": "Pixiv推送",
                "uin": "10000",
                "content": content
            }
        }
    
    async def close(self):
        """关闭连接"""
        if self._session:
            await self._session.close()
        if self._ws:
            await self._ws.close()
        self._running = False

    
    async def handle_feedback(self, illust_id: int, action: str) -> bool:
        """处理反馈"""
        if self.on_feedback:
            await self.on_feedback(illust_id, action)
        return True
    
    async def start_listening(self):
        """监听消息（用于反馈处理）"""
        if not self._ws:
            await self.connect()
        
        self._running = True
        
        while self._running:
            try:
                msg = await self._ws.receive()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    await self._process_message(data)
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    break
            except Exception as e:
                logger.error(f"消息处理错误: {e}")
    
    async def _process_message(self, data: dict):
        """处理收到的消息"""
        if data.get("post_type") != "message":
            return
        
        # 获取发送者 QQ
        sender_id = data.get("sender", {}).get("user_id") or data.get("user_id")
        raw_message = data.get("raw_message", "").strip()
        
        # 主人权限验证：只有主人的指令才有效
        if self.master_id and sender_id != self.master_id:
            return
        
        # 解析反馈命令：ID 1 = 喜欢，ID 2 = 不喜欢
        # 支持格式：
        #   123456 1   (喜欢作品 123456)
        #   123456 2   (不喜欢作品 123456)
        parts = raw_message.split()
        if len(parts) == 2:
            try:
                illust_id = int(parts[0])
                action_code = parts[1]
                
                if action_code == "1":
                    await self.handle_feedback(illust_id, "like")
                    # 回复到私聊（主人）
                    await self._send_message(f"❤️ 已记录对作品 {illust_id} 的喜欢", "private", sender_id)
                    return
                elif action_code == "2":
                    await self.handle_feedback(illust_id, "dislike")
                    await self._send_message(f"👎 已记录对作品 {illust_id} 的不喜欢", "private", sender_id)
                    return
            except ValueError:
                pass
    
    async def stop_listening(self):
        """停止监听"""
        self._running = False

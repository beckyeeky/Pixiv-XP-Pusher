"""
Telegram 推送实现
"""
import asyncio
import logging
from io import BytesIO
from typing import Callable, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CallbackQueryHandler

from .base import BaseNotifier
from pixiv_client import Illust, PixivClient
from utils import get_pixiv_cat_url

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

logger = logging.getLogger(__name__)


class TelegramNotifier(BaseNotifier):
    """Telegram Bot 推送"""
    
    def __init__(
        self,
        bot_token: str,
        chat_ids: list[str] | str,           # 支持单个或多个 chat_id
        client: Optional[PixivClient] = None,
        multi_page_mode: str = "cover_link",
        allowed_users: list[str] | None = None,  # 允许发送反馈的用户 ID
        thread_id: int | None = None,          # Telegram Topic (Thread) ID
        on_feedback: Optional[Callable] = None,
        on_action: Optional[Callable] = None
    ):
        self.bot = Bot(token=bot_token)
        # 支持单个或多个 chat_id
        if isinstance(chat_ids, str):
            self.chat_ids = [chat_ids] if chat_ids else []
        else:
            self.chat_ids = [str(c) for c in chat_ids if c]
        
        self.client = client
        self.multi_page_mode = multi_page_mode
        # 允许的用户（空=所有人）
        self.allowed_users = set(int(u) for u in allowed_users if u) if allowed_users else None
        self.on_feedback = on_feedback
        self.on_action = on_action
        self._app: Optional[Application] = None
        # 消息ID -> illust_id 映射（用于回复快捷反馈）
        self._message_illust_map: dict[int, int] = {}
        self.thread_id = thread_id  # Topic 支持
        
        # 日志
        logger.info(f"Telegram 推送目标: {', '.join(self.chat_ids) or '无'}")
        if self.allowed_users:
            logger.info(f"允许反馈的用户: {self.allowed_users}")

    async def stop_polling(self):
        """停止Bot轮询"""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    def _compress_image(self, image_data: bytes, max_size: int = 9 * 1024 * 1024) -> bytes:
        """智能压缩图片到指定大小以下 (默认 9MB)"""
        if not HAS_PILLOW:
            if len(image_data) > max_size:
                logger.warning(f"图片过大 ({len(image_data)} bytes) 且未安装 Pillow，无法压缩，发送可能失败。请 pip install Pillow")
            return image_data
            
        try:
            # 必须检查尺寸 (Telegram 限制 width + height <= 10000)
            # 即使文件大小很小，尺寸超标也会报 Photo_invalid_dimensions
            with Image.open(BytesIO(image_data)) as img:
                w, h = img.size
                need_resize = False
                
                # 检查尺寸
                if w + h > 10000:
                    scale = 9500 / (w + h)
                    img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
                    need_resize = True
                    logger.info(f"图片尺寸过大 ({w}x{h})，自动缩放到 {img.size[0]}x{img.size[1]}")
                elif w / h > 20 or h / w > 20: # 比例过长
                    # 比例问题比较难搞，通常需要裁剪或填充，暂时简单缩放长边
                    max_side = 5000
                    if max(w, h) > max_side:
                        img.thumbnail((max_side, max_side))
                        need_resize = True
                        logger.info(f"图片比例极端 ({w}x{h})，缩放到 {img.size[0]}x{img.size[1]}")

                # 如果没有调整尺寸且文件大小也合格，直接返回原图
                if not need_resize and len(image_data) <= max_size:
                    return image_data
                
                # 开始压缩处理
                logger.info(f"正在处理图片 (原始大小: {len(image_data)/1024/1024:.2f}MB, 尺寸: {w}x{h})...")
                
                # 转换色彩空间
                if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                    bg = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode != 'RGBA':
                        img = img.convert('RGBA')
                    bg.paste(img, mask=img.split()[3])
                    img = bg
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                    
                output = BytesIO()
                
                # 策略1：降低 JPEG 质量 (90 -> 50)
                quality = 90
                while quality >= 50:
                    output.seek(0)
                    output.truncate()
                    img.save(output, format='JPEG', quality=quality)
                    size = output.tell()
                    if size <= max_size:
                        logger.info(f"压缩成功: 质量={quality}, 大小={size/1024/1024:.2f}MB")
                        return output.getvalue()
                    quality -= 10
                
                # 策略2：继续缩放 (质量已降到50但仍超标)
                scale = 0.8
                while scale >= 0.3:
                    new_size = (int(img.width * scale), int(img.height * scale))
                    resized = img.resize(new_size, Image.Resampling.LANCZOS)
                    output.seek(0)
                    output.truncate()
                    resized.save(output, format='JPEG', quality=60)
                    size = output.tell()
                    if size <= max_size:
                        logger.info(f"压缩成功: 缩放={scale:.1f}, 大小={size/1024/1024:.2f}MB")
                        return output.getvalue()
                    scale -= 0.2
                    
                logger.warning("压缩失败：图片实在太大了")
                return image_data

        except Exception as e:
            logger.error(f"处理图片出错: {e}")
            return image_data
        except Exception as e:
            logger.error(f"压缩图片出错: {e}")
            return image_data
    
    async def start_polling(self):
        """启动Bot轮询（用于接收反馈）"""
        from telegram.ext import MessageHandler, filters
        
        self._app = Application.builder().token(self.bot.token).build()
        
        # 处理按钮回调
        async def callback_handler(update, context):
            query = update.callback_query
            user_id = query.from_user.id
            
            # 权限验证
            if self.allowed_users and user_id not in self.allowed_users:
                await query.answer("❌ 你没有权限操作", show_alert=True)
                return
            
            await query.answer()
            
            data = query.data
            
            if data.startswith("retry_ai:"):
                # 处理重试动作
                if self.on_action:
                    error_id = int(data.split(":")[1])
                    await self.on_action("retry_ai", error_id)
                    await query.edit_message_text("🔄 已提交重试请求，请稍候...")
                else:
                    await query.message.reply_text("❌ 未配置动作处理")
                return

            if ":" in data:
                action, illust_id = data.split(":")
                if action in ("like", "dislike"):
                    await self.handle_feedback(int(illust_id), action)
                    
                    emoji = "❤️" if action == "like" else "👎"
                    try:
                        await query.edit_message_reply_markup(reply_markup=None)
                        await query.message.reply_text(f"{emoji} 已记录反馈")
                    except Exception:
                        pass
        
        # 处理回复消息（1=喜欢, 2=不喜欢）
        async def reply_handler(update, context):
            message = update.message
            if not message or not message.reply_to_message:
                return
            
            user_id = message.from_user.id
            
            # 权限验证
            if self.allowed_users and user_id not in self.allowed_users:
                return
            
            text = message.text.strip()
            reply_msg_id = message.reply_to_message.message_id
            
            # 查找对应的 illust_id
            illust_id = self._message_illust_map.get(reply_msg_id)
            if not illust_id:
                return
            
            if text == "1":
                await self.handle_feedback(illust_id, "like")
                await message.reply_text("❤️ 已记录喜欢")
            elif text == "2":
                await self.handle_feedback(illust_id, "dislike")
                await message.reply_text("👎 已记录不喜欢")
        
        self._app.add_handler(CallbackQueryHandler(callback_handler))
        self._app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, reply_handler))
        
        # 真正启动 Bot (非阻塞模式)
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        logger.info("Telegram Bot 轮询已启动")
    
    async def send(self, illusts: list[Illust]) -> list[int]:
        """发送推送"""
        if not illusts:
            return []
        
        success_ids = []
        
        for illust in illusts:
            try:
                is_sent = await self._send_single(illust)
                if is_sent:
                    success_ids.append(illust.id)
                await asyncio.sleep(1)  # 避免触发限流
            except Exception as e:
                logger.error(f"发送作品 {illust.id} 失败: {e}")
        
        return success_ids
        
    async def send_text(self, text: str, buttons: list[tuple[str, str]] | None = None) -> bool:
        """发送文本消息到所有目标"""
        markup = None
        if buttons:
            kb = [[InlineKeyboardButton(label, callback_data=data)] for label, data in buttons]
            markup = InlineKeyboardMarkup(kb)
        
        success = True
        for chat_id in self.chat_ids:
            try:
                await self.bot.send_message(chat_id, text, reply_markup=markup)
            except Exception as e:
                logger.error(f"Telegram 发送文本到 {chat_id} 失败: {e}")
                success = False
        return success
    
    async def _send_single(self, illust: Illust) -> bool:
        """发送单个作品"""
        caption = self.format_message(illust)
        keyboard = self._build_keyboard(illust.id)
        
        if illust.page_count == 1 or self.multi_page_mode == "cover_link":
            # 单图或封面+链接模式
            return await self._send_photo(illust, caption, keyboard)
        else:
            # 多图批量发送模式
            return await self._send_media_group(illust, caption, keyboard)
    
    async def _send_photo(self, illust: Illust, caption: str, keyboard: InlineKeyboardMarkup) -> bool:
        """发送单张图片到所有目标"""
        any_success = False
        # 先下载图片（如果可以）
        image_data = None
        if self.client and illust.image_urls:
            try:
                image_data = await self.client.download_image(illust.image_urls[0])
                if image_data:
                    image_data = self._compress_image(image_data)
            except Exception as e:
                logger.warning(f"下载图片失败: {e}")
        
        # 发送到所有 chat_id
        for chat_id in self.chat_ids:
            sent_message = None
            try:
                if image_data:
                    sent_message = await self.bot.send_photo(
                        chat_id=chat_id,
                        photo=BytesIO(image_data),
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=self.thread_id
                    )
                else:
                    # Fallback: 使用反代链接
                    proxy_url = get_pixiv_cat_url(illust.id)
                    sent_message = await self.bot.send_photo(
                        chat_id=chat_id,
                        photo=proxy_url,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=self.thread_id
                    )
                
                if sent_message:
                    self._message_illust_map[sent_message.message_id] = illust.id
                    any_success = True
            except Exception as e:
                logger.error(f"发送到 {chat_id} 失败: {e}")
        
        # 限制映射大小，避免内存泄漏
        if len(self._message_illust_map) > 200:
            oldest_keys = list(self._message_illust_map.keys())[:100]
            for k in oldest_keys:
                del self._message_illust_map[k]
        
        return any_success
    
    async def _send_media_group(self, illust: Illust, caption: str, keyboard: InlineKeyboardMarkup) -> bool:
        """发送多图到所有目标"""
        media = []
        any_success = False
        
        for i, url in enumerate(illust.image_urls[:10]):
            try:
                if self.client:
                    image_data = await self.client.download_image(url)
                    if image_data:
                        image_data = self._compress_image(image_data)
                    photo = BytesIO(image_data)
                else:
                    photo = get_pixiv_cat_url(illust.id, i)
                
                media.append(InputMediaPhoto(
                    media=photo,
                    caption=caption if i == 0 else None,
                    parse_mode="HTML" if i == 0 else None
                ))
            except Exception as e:
                logger.warning(f"获取第{i+1}页失败: {e}")
        
        if media:
            for chat_id in self.chat_ids:
                try:
                    await self.bot.send_media_group(
                        chat_id=chat_id,
                        media=media,
                        message_thread_id=self.thread_id
                    )
                    # MediaGroup不支持按钮，单独发送
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=f"作品 #{illust.id} 的操作：",
                        reply_markup=keyboard,
                        message_thread_id=self.thread_id
                    )
                    any_success = True
                except Exception as e:
                    logger.error(f"发送 MediaGroup 到 {chat_id} 失败: {e}")
        return any_success
    
    def format_message(self, illust: Illust) -> str:
        """格式化消息"""
        tags = " ".join(f"#{t}" for t in illust.tags[:5])
        r18_mark = "🔞 " if illust.is_r18 else ""
        
        # 获取匹配度（如果有）
        match_score = getattr(illust, 'match_score', None)
        match_line = f"🎯 匹配度: {match_score*100:.0f}%\n" if match_score is not None else ""
        
        return (
            f"{r18_mark}🎨 <b>{illust.title}</b>\n"
            f"👤 {illust.user_name} (ID: {illust.user_id})\n"
            f"❤️ {illust.bookmark_count} | 👀 {illust.view_count}\n"
            f"{match_line}"
            f"🏷️ {tags}\n"
            f"🔗 <a href=\"https://pixiv.net/i/{illust.id}\">原图链接</a>"
        )
    
    def _build_keyboard(self, illust_id: int) -> InlineKeyboardMarkup:
        """构建反馈按钮"""
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❤️ 喜欢", callback_data=f"like:{illust_id}"),
                InlineKeyboardButton("👎 不喜欢", callback_data=f"dislike:{illust_id}"),
            ],
            [
                InlineKeyboardButton("🔗 查看原图", url=f"https://pixiv.net/i/{illust_id}"),
            ]
        ])
    
    async def handle_feedback(self, illust_id: int, action: str) -> bool:
        """处理反馈回调"""
        if self.on_feedback:
            await self.on_feedback(illust_id, action)
        return True
    


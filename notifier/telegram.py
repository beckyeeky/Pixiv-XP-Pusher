"""
Telegram 推送实现
"""
import asyncio
import logging
from io import BytesIO
from typing import Callable, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import BadRequest
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


async def _retry_on_flood(coro_func, max_retries=3):
    """
    Retry a coroutine on Flood Control errors and network errors.
    coro_func should be a callable that returns a coroutine (not the coroutine itself).
    """
    from telegram.error import RetryAfter, NetworkError, TimedOut

    # 网络错误关键词（httpx 错误）
    network_error_keywords = [
        "ConnectError", "RemoteProtocolError", "disconnected",
        "TimeoutException", "ConnectionResetError", "ConnectionRefusedError"
    ]

    for attempt in range(max_retries):
        try:
            return await coro_func()
        except RetryAfter as e:
            wait_time = e.retry_after + 1  # Add 1 second buffer
            logger.info(f"Flood control: Sleeping for {wait_time}s to avoid conflict...")
            await asyncio.sleep(wait_time)
        except (NetworkError, TimedOut) as e:
            # Telegram 库的网络错误
            wait_time = 3 * (attempt + 1)  # 递增等待：3s, 6s, 9s
            logger.warning(f"网络错误 (尝试 {attempt+1}/{max_retries}): {e}，{wait_time}s 后重试...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            error_msg = str(e)
            # 检查是否为 Flood Control
            if "Flood control exceeded" in error_msg:
                import re
                match = re.search(r"Retry in (\d+)", error_msg)
                wait_time = int(match.group(1)) + 1 if match else 10
                logger.info(f"Flood control: Sleeping for {wait_time}s to avoid conflict...")
                await asyncio.sleep(wait_time)
            # 检查是否为网络错误
            elif any(kw in error_msg for kw in network_error_keywords):
                wait_time = 3 * (attempt + 1)
                logger.warning(f"网络错误 (尝试 {attempt+1}/{max_retries}): {type(e).__name__}，{wait_time}s 后重试...")
                await asyncio.sleep(wait_time)
            else:
                raise  # Re-raise non-retryable errors

    # Final attempt without catching
    return await coro_func()


class TelegramNotifier(BaseNotifier):
    """Telegram Bot 推送"""

    def __init__(
        self,
        bot_token: str,
        chat_ids: list[str] | str,           # 支持单个或多个 chat_id
        client: Optional[PixivClient] = None,
        multi_page_mode: str = "cover_link",
        allowed_users: list[str] | None = None,  # 允许发送反馈的用户 ID
        thread_id: int | None = None,          # Telegram Topic (Thread) ID (默认)
        on_feedback: Optional[Callable] = None,
        on_action: Optional[Callable] = None,
        proxy_url: str | None = None,             # HTTP 代理地址
        max_pages: int = 10,
        image_quality: int = 85,               # JPEG 压缩质量 (默认 85)
        max_image_size: int = 2000,            # 最大边长 (默认 2000px)
        topic_rules: dict | None = None,       # Topic 分流规则 {category: topic_id}
        topic_tag_mapping: dict | None = None, # 标签到分类的映射 {category: [tags]}
        # 批量模式配置
        batch_mode: str = "single",            # single / telegraph
        batch_show_title: bool = True,
        batch_show_artist: bool = True,
        batch_show_tags: bool = True,
    ):
        # Auto-detect proxy if not provided
        if not proxy_url:
            import urllib.request
            sys_proxies = urllib.request.getproxies()
            proxy_url = sys_proxies.get("https") or sys_proxies.get("http")
            if proxy_url:
                logger.info(f"TelegramNotifier using system proxy: {proxy_url}")

        from telegram.request import HTTPXRequest
        request = HTTPXRequest(proxy=proxy_url) if proxy_url else None
        self.bot = Bot(token=bot_token, request=request)

        # 支持单个或多个 chat_id，并去重防止重复发送
        if isinstance(chat_ids, str):
            self.chat_ids = [chat_ids] if chat_ids else []
        else:
            # 去重：转换为 set 再转回 list
            self.chat_ids = list(dict.fromkeys(str(c) for c in chat_ids if c))

        self.client = client
        self.multi_page_mode = multi_page_mode
        # 允许的用户（空=所有人）
        self.allowed_users = set(int(u) for u in allowed_users if u) if allowed_users else None
        self.on_feedback = on_feedback
        self.on_action = on_action
        self.proxy_url = proxy_url
        self.max_pages = max_pages
        self.image_quality = image_quality
        self.max_image_size = max_image_size
        self._app: Optional[Application] = None
        # 消息ID -> illust_id 映射（用于回复快捷反馈）
        self._message_illust_map: dict[int, int] = {}
        self.thread_id = thread_id  # 默认 Topic

        # Topic 智能分流
        self.topic_rules = topic_rules or {}
        self.topic_tag_mapping = topic_tag_mapping or {}

        # 批量模式
        self.batch_mode = batch_mode
        self.batch_show_title = batch_show_title
        self.batch_show_artist = batch_show_artist
        self.batch_show_tags = batch_show_tags
        self._telegraph = None  # Telegraph 客户端（延迟初始化）
        self._pending_input = None  # 等待用户输入的状态

        # 日志
        logger.info(f"Telegram 推送目标: {', '.join(self.chat_ids) or '无'}")
        if self.allowed_users:
            logger.info(f"允许反馈的用户: {self.allowed_users}")
        if self.topic_rules:
            logger.info(f"Topic 分流规则: {list(self.topic_rules.keys())}")
        if self.batch_mode == "telegraph":
            logger.info("批量模式: Telegraph")

        # 推送队列
        self.send_queue = asyncio.Queue()
        self.worker_task = asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        """推送队列消费者"""
        logger.info("推送队列 worker 启动")
        while True:
            try:
                task = await self.send_queue.get()
                illusts, custom_title, batch_mode = task

                try:
                    # 调用原始发送逻辑，使用任务指定的 batch_mode
                    sent_ids = await self._send_direct(illusts, custom_title, batch_mode)
                    # 标记已推送
                    if sent_ids:
                        try:
                            import database as db
                            for ill in illusts:
                                if ill.id in sent_ids:
                                    source = getattr(ill, 'source', 'unknown')
                                    await db.mark_pushed(ill.id, source)
                        except Exception as e:
                            logger.warning(f"队列内标记推送状态失败: {e}")
                except Exception as e:
                    logger.error(f"推送任务执行失败: {e}")

                self.send_queue.task_done()

                # 批次间歇，避免刷屏
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                logger.info("推送队列 worker 停止")
                break
            except Exception as e:
                logger.error(f"推送队列 worker 异常: {e}")
                await asyncio.sleep(5)

    async def _send_typing(self, chat_id: int):
        """发送 typing 状态"""
        try:
            await self.bot.send_chat_action(chat_id=chat_id, action='typing')
        except Exception as e:
            logger.debug(f"发送 typing 状态失败: {e}")

    async def _keep_typing(self, chat_id: int):
        """保持 typing 状态（每4秒发送一次）"""
        try:
            while True:
                await self._send_typing(chat_id)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    def _resolve_topic_id(self, illust: Illust) -> int | None:
        """根据作品标签匹配 Topic ID"""
        if not self.topic_rules:
            return self.thread_id  # 使用默认 topic

        illust_tags_lower = {t.lower() for t in illust.tags}

        # 优先检查 R18
        if illust.is_r18 and "r18" in self.topic_rules:
            return self.topic_rules["r18"]

        # 检查标签映射
        for category, tags in self.topic_tag_mapping.items():
            if category in self.topic_rules:
                for tag in tags:
                    if tag.lower() in illust_tags_lower:
                        return self.topic_rules[category]

        # 返回默认 topic
        return self.topic_rules.get("default", self.thread_id)

    def _build_main_menu(self) -> InlineKeyboardMarkup:
        """构建主菜单"""
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚀 推送", callback_data="menu:push"),
                InlineKeyboardButton("📊 统计", callback_data="menu:stats"),
            ],
            [
                InlineKeyboardButton("🎯 XP画像", callback_data="menu:xp"),
                InlineKeyboardButton("📦 批量", callback_data="menu:batch"),
            ],
            [
                InlineKeyboardButton("🚫 屏蔽", callback_data="menu:block"),
                InlineKeyboardButton("🔕 静音", callback_data="menu:mute"),
            ],
            [
                InlineKeyboardButton("⚙️ 设置", callback_data="menu:settings"),
            ],
        ])

    def _build_batch_menu(self) -> InlineKeyboardMarkup:
        """构建批量设置菜单"""
        mode_text = "📦 批量" if self.batch_mode == "telegraph" else "📄 逐条"
        title_icon = "✅" if self.batch_show_title else "❌"
        artist_icon = "✅" if self.batch_show_artist else "❌"
        tags_icon = "✅" if self.batch_show_tags else "❌"

        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📄 逐条", callback_data="menu:batch:single"),
                InlineKeyboardButton(f"📦 批量", callback_data="menu:batch:telegraph"),
            ],
            [
                InlineKeyboardButton(f"标题{title_icon}", callback_data="menu:batch:title"),
                InlineKeyboardButton(f"画师{artist_icon}", callback_data="menu:batch:artist"),
                InlineKeyboardButton(f"标签{tags_icon}", callback_data="menu:batch:tags"),
            ],
            [InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")],
        ])

    def _build_settings_menu(self, config: dict) -> InlineKeyboardMarkup:
        """构建设置菜单"""
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🤖 AI过滤", callback_data="menu:set:ai"),
                InlineKeyboardButton("🔞 R18模式", callback_data="menu:set:r18"),
            ],
            [
                InlineKeyboardButton("📊 每日上限", callback_data="menu:set:limit"),
                InlineKeyboardButton("📅 推送时间", callback_data="menu:set:schedule"),
            ],
            [InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")],
        ])

    def _build_block_menu(self) -> InlineKeyboardMarkup:
        """构建屏蔽管理菜单"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 查看屏蔽列表", callback_data="menu:block:list")],
            [
                InlineKeyboardButton("🏷️ 标签屏蔽", callback_data="menu:block:tag"),
                InlineKeyboardButton("🎨 画师屏蔽", callback_data="menu:block:artist"),
            ],
            [InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")],
        ])

    def _read_config(self) -> dict:
        """读取配置文件"""
        import yaml
        import os
        config_path = "config.yaml"
        if not os.path.exists(config_path): return {}
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except:
            return {}

    def _save_config_value(self, *args):
        """保存配置值 _save_config_value("filter", "daily_limit", 30)"""
        import yaml
        import os

        if len(args) < 2: return
        keys = args[:-1]
        value = args[-1]

        config_path = "config.yaml"
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}

            # Navigate to leaf
            current = config
            for key in keys[:-1]:
                if key not in current: current[key] = {}
                current = current[key]
            current[keys[-1]] = value

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, allow_unicode=True, sort_keys=False)
            logger.info(f"配置已更新: {keys} = {value}")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def _save_batch_config(self):
        """保存批量配置"""
        self._save_config_value("notifier", "telegram", "batch_mode", self.batch_mode)
        self._save_config_value("notifier", "telegram", "batch_show_title", self.batch_show_title)
        self._save_config_value("notifier", "telegram", "batch_show_artist", self.batch_show_artist)
        self._save_config_value("notifier", "telegram", "batch_show_tags", self.batch_show_tags)

    async def _handle_menu_callback(self, query, data: str):
        """处理菜单回调"""
        import database as db

        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        sub_action = parts[2] if len(parts) > 2 else ""

        # 主菜单
        if action == "main":
            await query.edit_message_text(
                "🤖 *XP Pusher 控制面板*",
                reply_markup=self._build_main_menu(),
                parse_mode="Markdown"
            )

        # 立即推送
        elif action == "push":
            # 显示与 /push 命令相同的交互式菜单
            await query.edit_message_text(
                "🚀 *推送模式选择*\n\n请选择要执行的推送类型:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 今日精选推送", callback_data="push:today")],
                    [InlineKeyboardButton("🎨 画师作品集", callback_data="push:artist")],
                    [InlineKeyboardButton("📌 指定作品ID", callback_data="push:illust")],
                    [InlineKeyboardButton("⬅️ 取消", callback_data="menu:main")],
                ]),
                parse_mode="Markdown"
            )

        # 统计
        elif action == "stats":
            stats = await db.get_all_strategy_stats()
            lines = ["📊 *策略表现*\n"]
            strategy_names = {
                "xp_search": "XP搜索",
                "search": "XP搜索(旧)",
                "subscription": "订阅更新",
                "ranking": "排行榜",
                "related": "关联推荐"
            }
            for strategy, data in stats.items():
                name = strategy_names.get(strategy, strategy)
                if name == strategy and "_" in name:
                    name = name.replace("_", "\\_")
                rate = f"{data['rate']:.1%}" if data['total'] > 0 else "N/A"
                lines.append(f"• *{name}*: {data['success']}/{data['total']} ({rate})")

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")
            ]])
            await query.edit_message_text("\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")

        # XP画像
        elif action == "xp":
            top_tags = await db.get_top_xp_tags(15)
            lines = ["🎯 *XP 画像 Top 15*\n"]
            for i, (tag, weight) in enumerate(top_tags, 1):
                lines.append(f"{i}. `{tag}` ({weight:.2f})")

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")
            ]])
            await query.edit_message_text("\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")

        # 批量设置
        elif action == "batch":
            if not sub_action:
                mode_icon = "📦" if self.batch_mode == "telegraph" else "📄"
                text = f"📦 *批量模式设置*\n\n当前模式: {mode_icon} `{self.batch_mode}`"
                await query.edit_message_text(text, reply_markup=self._build_batch_menu(), parse_mode="Markdown")
            elif sub_action == "single":
                self.batch_mode = "single"
                self._save_batch_config()
                await query.edit_message_text("✅ 已切换为逐条发送模式 (已保存)", reply_markup=self._build_batch_menu())
            elif sub_action == "telegraph":
                self.batch_mode = "telegraph"
                self._save_batch_config()
                await query.edit_message_text("✅ 已切换为批量模式 (已保存)", reply_markup=self._build_batch_menu())
            elif sub_action == "title":
                self.batch_show_title = not self.batch_show_title
                self._save_batch_config()
                await query.edit_message_reply_markup(reply_markup=self._build_batch_menu())
            elif sub_action == "artist":
                self.batch_show_artist = not self.batch_show_artist
                self._save_batch_config()
                await query.edit_message_reply_markup(reply_markup=self._build_batch_menu())
            elif sub_action == "tags":
                self.batch_show_tags = not self.batch_show_tags
                self._save_batch_config()
                await query.edit_message_reply_markup(reply_markup=self._build_batch_menu())

        # 静音管理
        elif action == "mute":
            import database as db
            if not sub_action:
                muted = await db.get_muted_tags(active_only=True)
                lines = ["🔕 *静音标签* (24小时，可提前撤销)\n"]
                if muted:
                    lines.append("当前静音中:")
                    for tag, until_ts in muted[:12]:
                        lines.append(f"  • `{tag}` → `{until_ts}`")
                else:
                    lines.append("_暂无静音标签_\n\n用法：`/mute <tag>`")

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ 添加静音", callback_data="menu:mute:add")],
                    [InlineKeyboardButton("❎ 取消静音", callback_data="menu:mute:remove")],
                    [InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")],
                ])
                await query.edit_message_text("\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")

            elif sub_action == "add":
                await query.edit_message_text(
                    "🔕 请回复要静音的标签名称\n\n_静音 24 小时（包括批量模式）。支持 #号，自动归一化_",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 取消", callback_data="menu:mute")]]),
                    parse_mode="Markdown"
                )
                self._pending_input = {"type": "mute_tag", "chat_id": query.message.chat_id}

            elif sub_action == "remove":
                muted = await db.get_muted_tags(active_only=True)
                if not muted:
                    await query.edit_message_text(
                        "🔕 当前没有静音标签",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data="menu:mute")]]),
                        parse_mode="Markdown"
                    )
                    return

                # 交互式：列出按钮让你点
                rows = []
                row = []
                for (tag, until_ts) in muted[:12]:
                    row.append(InlineKeyboardButton(f"❎ {tag}", callback_data=f"menu:mute:unmute:{tag}"))
                    if len(row) == 2:
                        rows.append(row)
                        row = []
                if row:
                    rows.append(row)
                rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="menu:mute")])

                await query.edit_message_text(
                    "选择要取消静音的标签：",
                    reply_markup=InlineKeyboardMarkup(rows),
                    parse_mode="Markdown"
                )

            elif sub_action == "unmute" and len(parts) >= 4:
                tag = ":".join(parts[3:])
                ok = await db.unmute_tag(tag)
                await query.answer("✅ 已取消静音" if ok else "⚠️ 未找到该静音标签")

                # 返回静音首页
                muted = await db.get_muted_tags(active_only=True)
                lines = ["🔕 *静音标签* (24小时，可提前撤销)\n"]
                if muted:
                    lines.append("当前静音中:")
                    for t, until_ts in muted[:12]:
                        lines.append(f"  • `{t}` → `{until_ts}`")
                else:
                    lines.append("_暂无静音标签_\n\n用法：`/mute <tag>`")

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ 添加静音", callback_data="menu:mute:add")],
                    [InlineKeyboardButton("❎ 取消静音", callback_data="menu:mute:remove")],
                    [InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")],
                ])
                await query.edit_message_text("\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")

        # 屏蔽管理
        elif action == "block":
            if not sub_action:
                await query.edit_message_text(
                    "🚫 *屏蔽管理*",
                    reply_markup=self._build_block_menu(),
                    parse_mode="Markdown"
                )
            elif sub_action == "list":
                blocked_tags = await db.get_blocked_tags()
                blocked_artists = await db.get_blocked_artists()

                lines = ["📋 *屏蔽列表*\n"]
                if blocked_tags:
                    lines.append("🏷️ 标签:")
                    for tag in blocked_tags[:10]:
                        lines.append(f"  • `{tag}`")
                if blocked_artists:
                    lines.append("\n🎨 画师:")
                    for artist_id, name in blocked_artists[:10]:
                        lines.append(f"  • {name} (`{artist_id}`)")
                if not blocked_tags and not blocked_artists:
                    lines.append("_暂无屏蔽_")

                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ 返回", callback_data="menu:block")
                ]])
                await query.edit_message_text("\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")
            elif sub_action == "tag":
                await query.edit_message_text(
                    "🏷️ 请回复要屏蔽的标签名称\n\n_直接发送标签名即可_",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ 取消", callback_data="menu:block")
                    ]]),
                    parse_mode="Markdown"
                )
                # 设置状态等待输入
                self._pending_input = {"type": "block_tag", "chat_id": query.message.chat_id}
            elif sub_action == "artist":
                await query.edit_message_text(
                    "🎨 请回复要屏蔽的画师ID\n\n_发送画师ID (数字)_",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ 取消", callback_data="menu:block")
                    ]]),
                    parse_mode="Markdown"
                )
                self._pending_input = {"type": "block_artist", "chat_id": query.message.chat_id}

        # 设置
        elif action == "settings" or action == "set":
            config = self._read_config()

            if not sub_action:
                await query.edit_message_text(
                    "⚙️ *设置*\n\n_部分设置修改后需重启生效_",
                    reply_markup=self._build_settings_menu(config),
                    parse_mode="Markdown"
                )
            elif sub_action == "ai":
                # 切换 AI 过滤 (filter.exclude_ai)
                current = config.get("filter", {}).get("exclude_ai", False)
                new_val = not current
                self._save_config_value("filter", "exclude_ai", new_val)
                # 刷新并重新读取
                config = self._read_config()
                await query.edit_message_text(
                    f"✅ AI 过滤已 {'开启' if new_val else '关闭'}",
                    reply_markup=self._build_settings_menu(config)
                )
            elif sub_action == "r18":
                # 循环切换 mixed -> r18_only -> safe
                current = config.get("filter", {}).get("r18_mode", "mixed")
                modes = ["mixed", "r18_only", "safe"]
                try:
                    next_mode = modes[(modes.index(current) + 1) % len(modes)]
                except:
                    next_mode = "mixed"

                self._save_config_value("filter", "r18_mode", next_mode)
                config = self._read_config()
                await query.edit_message_text(
                    f"✅ R18 模式已切换为: `{next_mode}`",
                    reply_markup=self._build_settings_menu(config),
                    parse_mode="Markdown"
                )
            elif sub_action == "limit":
                msg = await query.edit_message_text(
                    "📊 请回复每日推送上限 (数字)\n\n_例如: 30_",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ 取消", callback_data="menu:settings")
                    ]]),
                    parse_mode="Markdown"
                )
                prompt_id = msg.message_id if hasattr(msg, "message_id") else query.message.message_id
                self._pending_input = {"type": "set_limit", "chat_id": query.message.chat_id, "prompt_id": prompt_id}
            elif sub_action == "schedule":
                if self.on_action:
                    await self.on_action("show_schedule", None)
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ 返回", callback_data="menu:settings")
                ]])
                await query.edit_message_text(
                    "📅 推送时间设置请使用 `/schedule` 命令",
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )



    async def stop_polling(self):
        """停止Bot轮询"""
        if self._app:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                if self._app.running:
                    await self._app.stop()
                await self._app.shutdown()
                self._app = None  # 清理引用，允许重新初始化
                logger.info("Telegram Bot 轮询已停止")
            except Exception as e:
                logger.error(f"停止 Telegram 轮询时出错: {e}")
                self._app = None  # 即使出错也清理引用

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

                # 检查尺寸 (优先使用配置的 max_image_size)
                max_dim = self.max_image_size
                if w > max_dim or h > max_dim:
                    img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                    need_resize = True
                    logger.info(f"图片尺寸过大 ({w}x{h})，自动缩放到 {img.size[0]}x{img.size[1]}")
                elif w + h > 10000:
                    scale = 9500 / (w + h)
                    img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
                    need_resize = True
                    logger.info(f"图片尺寸超限 ({w}x{h})，缩放到 {img.size[0]}x{img.size[1]}")
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

                # 策略1：降低 JPEG 质量 (从配置的 quality 到 50)
                quality = self.image_quality
                min_quality = 50
                while quality >= min_quality:
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

    async def start_polling(self):
        """启动Bot轮询（用于接收反馈）"""
        from telegram.ext import MessageHandler, filters, CommandHandler
        from apscheduler.triggers.cron import CronTrigger

        from telegram.request import HTTPXRequest

        # 增加超时以减少 "Server disconnected" 错误
        # 长轮询需要更长的 read_timeout（Telegram 服务端默认最多等待 50 秒）
        request_kwargs = {
            "read_timeout": 60,
            "write_timeout": 30,
            "connect_timeout": 30,
            "pool_timeout": 30,
        }
        if self.proxy_url:
            request_kwargs["proxy"] = self.proxy_url

        request = HTTPXRequest(**request_kwargs)
        builder = Application.builder().token(self.bot.token).request(request)

        self._app = builder.build()

        # 处理按钮回调
        async def callback_handler(update, context):
            query = update.callback_query
            user_id = query.from_user.id

            # 权限验证
            # 权限验证
            if self.allowed_users and user_id not in self.allowed_users:
                await query.answer(f"❌ 无权限 (ID: {user_id})", show_alert=True)
                return

            # 检测回调是否过期（Telegram 限制回调查询必须在 48 秒内响应）
            is_query_expired = False
            try:
                await query.answer()
            except Exception as e:
                error_msg = str(e).lower()
                is_query_expired = "query is too old" in error_msg or "too old" in error_msg
                if is_query_expired:
                    logger.warning(f"回调查询已过期 (用户 {user_id})，将使用消息回复方式确认")
                else:
                    logger.debug(f"回调应答失败: {e}")

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

            # ===== 菜单回调处理 =====
            if data.startswith("menu:"):
                await self._handle_menu_callback(query, data)
                return

            # ===== 推送菜单回调处理 =====
            if data.startswith("push"):
                await _handle_push_callback(query, data)
                return

            # ===== 搜索向导回调处理 =====
            if data.startswith("search_"):
                await _handle_search_callback(query, data)
                return

            # ===== 屏蔽管理回调处理 =====
            if data.startswith(("block_", "unblock:")) and not data.startswith(("block_artist", "unblock_artist")):
                await _handle_block_callback(query, data)
                return

            # ===== 画师屏蔽管理回调处理 =====
            if data.startswith(("block_artist", "unblock_artist")):
                await _handle_block_artist_callback(query, data)
                return

            # ===== 定时任务设置回调处理 =====
            if data.startswith("schedule_"):
                await _handle_schedule_callback(query, data)
                return

            if data == "batch_like":
                # 显示作品选择按钮
                import database as db
                illust_ids = await db.get_batch_all_illust_ids(
                    query.message.message_id,
                    str(query.message.chat_id)
                )
                if illust_ids:
                    keyboard = self._build_batch_select_keyboard("like", len(illust_ids))
                    await query.edit_message_reply_markup(reply_markup=keyboard)
                return

            if data == "batch_dislike":
                import database as db
                illust_ids = await db.get_batch_all_illust_ids(
                    query.message.message_id,
                    str(query.message.chat_id)
                )
                if illust_ids:
                    keyboard = self._build_batch_select_keyboard("dislike", len(illust_ids))
                    await query.edit_message_reply_markup(reply_markup=keyboard)
                return

            if data.startswith("batch_select:"):
                # 格式: batch_select:like:3
                import database as db
                parts = data.split(":")
                action = parts[1]  # like or dislike
                index = int(parts[2])  # 1-based

                illust_id = await db.get_batch_illust_id(
                    query.message.message_id,
                    str(query.message.chat_id),
                    index
                )
                if illust_id:
                    await self.handle_feedback(illust_id, action, chat_id=query.message.chat_id)
                    emoji = "❤️" if action == "like" else "👎"
                    await query.message.reply_text(f"{emoji} 已记录 #{index} 的反馈")

                # 恢复原始按钮
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("❤️ 喜欢", callback_data="batch_like"),
                        InlineKeyboardButton("👎 不喜欢", callback_data="batch_dislike"),
                    ]
                ])
                await query.edit_message_reply_markup(reply_markup=keyboard)
                return

            if data.startswith("batch_all:"):
                # 格式: batch_all:like
                import database as db
                action = data.split(":")[1]

                illust_ids = await db.get_batch_all_illust_ids(
                    query.message.message_id,
                    str(query.message.chat_id)
                )
                for illust_id in illust_ids:
                    await self.handle_feedback(illust_id, action, chat_id=query.message.chat_id)

                emoji = "❤️" if action == "like" else "👎"
                await query.message.reply_text(f"{emoji} 已对全部 {len(illust_ids)} 个作品记录反馈")
                await query.edit_message_reply_markup(reply_markup=None)
                return

            if data == "batch_cancel":
                # 恢复原始按钮
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("❤️ 喜欢", callback_data="batch_like"),
                        InlineKeyboardButton("👎 不喜欢", callback_data="batch_dislike"),
                    ]
                ])
                await query.edit_message_reply_markup(reply_markup=keyboard)
                return

            if ":" in data:
                action, illust_id = data.split(":")
                if action in ("like", "dislike", "follow"):
                    try:
                        # 1. 乐观更新：先改界面，让用户觉得"秒回"
                        try:
                            current_markup = query.message.reply_markup
                            if current_markup and current_markup.inline_keyboard:
                                new_keyboard = []
                                for row in current_markup.inline_keyboard:
                                    new_row = []
                                    for btn in row:
                                        # 创建新按钮对象，更新文字
                                        new_text = btn.text
                                        if action == "like" and "收藏" in btn.text:
                                            new_text = "✅ 已收藏"
                                        elif action == "follow" and "关注" in btn.text:
                                            new_text = "✅ 已关注"
                                        elif action == "dislike" and "不喜欢" in btn.text:
                                            new_text = "✅ 已屏蔽"

                                        # 保持原有的 callback_data 或 url
                                        if btn.callback_data:
                                            new_btn = InlineKeyboardButton(new_text, callback_data=btn.callback_data)
                                        else:
                                            new_btn = InlineKeyboardButton(new_text, url=btn.url)
                                        new_row.append(new_btn)
                                    new_keyboard.append(new_row)

                                try:
                                    await query.edit_message_reply_markup(
                                        reply_markup=InlineKeyboardMarkup(new_keyboard)
                                    )
                                except BadRequest as e:
                                    # 忽略"未修改"错误（用户可能狂点）
                                    if "Message is not modified" not in str(e):
                                        logger.warning(f"更新按钮UI警告: {e}")
                        except Exception as e:
                            logger.error(f"更新按钮UI失败: {e}")

                        # 2. 异步队列：后台执行耗时的 API 操作
                        async def _background_task():
                            try:
                                await self.handle_feedback(int(illust_id), action, chat_id=query.message.chat_id)
                            except Exception as e:
                                logger.error(f"后台处理反馈失败 ({action} {illust_id}): {e}")
                                # 如果失败了，发个消息通知用户（因为按钮已经变成绿色了，得告诉他其实没成功）
                                try:
                                    await self.bot.send_message(
                                        chat_id=query.message.chat_id,
                                        text=f"⚠️ 操作同步到 Pixiv 失败: {e}",
                                        reply_to_message_id=query.message.message_id
                                    )
                                except:
                                    pass

                        # 扔进 asyncio 循环，不等待结果
                        asyncio.create_task(_background_task())

                    except Exception as e:
                        logger.error(f"处理反馈流程异常: {e}")

        # 处理回复消息（1=喜欢, 2=不喜欢, 或输入内容）
        async def reply_handler(update, context):
            message = update.message
            if not message:
                return

            user_id = message.from_user.id

            # 权限验证
            if self.allowed_users and user_id not in self.allowed_users:
                return

            text = message.text.strip()
            chat_id = message.chat_id

            # ===== 处理 Push 会话 =====
            push_session = self._push_sessions.get(user_id)
            if push_session:
                step = push_session.get("step")

                # 保存用户输入消息ID
                if "user_message_ids" not in push_session:
                    push_session["user_message_ids"] = []
                push_session["user_message_ids"].append(message.message_id)

                if step == "input_artist_id":
                    if not text.isdigit():
                        await message.reply_text("❌ 画师ID必须是数字")
                        return

                    artist_id = int(text)
                    # 删除消息并执行
                    await _delete_push_messages(user_id, chat_id)
                    if user_id in self._push_sessions:
                        del self._push_sessions[user_id]

                    # 执行画师推送
                    await _handle_push_direct(user_id, chat_id, ["a", str(artist_id)])
                    return

                elif step == "input_illust_id":
                    if not text.isdigit():
                        await message.reply_text("❌ 作品ID必须是数字")
                        return

                    illust_id = int(text)
                    # 删除消息并执行
                    await _delete_push_messages(user_id, chat_id)
                    if user_id in self._push_sessions:
                        del self._push_sessions[user_id]

                    # 执行作品推送
                    await _handle_push_direct(user_id, chat_id, [str(illust_id)])
                    return

            # ===== 处理搜索向导会话 =====
            search_session = self._search_sessions.get(user_id)
            if search_session:
                step = search_session.get("step")

                # 保存用户输入消息ID用于后续删除
                if "user_message_ids" not in search_session:
                    search_session["user_message_ids"] = []
                search_session["user_message_ids"].append(message.message_id)

                if step == "input_batch":
                    # 处理批次输入
                    if not text.isdigit():
                        await message.reply_text("❌ 请输入数字（1-10）")
                        return
                    batch_num = int(text)
                    if batch_num < 1 or batch_num > 10:
                        await message.reply_text("❌ 批次范围 1-10")
                        return

                    search_session["offset"] = (batch_num - 1) * 20
                    search_session["step"] = "input_keywords"

                    dr = search_session.get("date_range", 0)
                    date_text = "不限" if dr == 0 else f"近{dr}天"

                    msg = await message.reply_text(
                        f"🔍 *交互式搜索向导*\n\n"
                        f"第 3/3 步：请输入搜索关键词\n"
                        f"📅 时间: {date_text}\n"
                        f"📄 批次: 第 {batch_num} 批 ({search_session['offset']+1}-{search_session['offset']+20})\n\n"
                        f"输入格式：\n"
                        f"• 单关键词: `白发`\n"
                        f"• 多关键词: `白发|黑丝|红瞳`\n"
                        f"（用 | 分隔，#号会自动去除）\n\n"
                        f"直接回复此消息即可",
                        parse_mode="Markdown"
                    )
                    # 保存消息ID
                    if "message_ids" not in search_session:
                        search_session["message_ids"] = []
                    search_session["message_ids"].append(msg.message_id)
                    return

                elif step == "input_keywords":
                    # 处理关键词输入
                    keywords = [k.strip().replace('#', '') for k in text.split("|") if k.strip()]
                    if not keywords:
                        await message.reply_text("❌ 请输入有效的搜索关键词")
                        return

                    date_range = search_session.get("date_range", 0)
                    offset = search_session.get("offset", 0)

                    # 删除向导消息和用户输入消息
                    await _delete_search_guide_messages(user_id, chat_id)

                    await _do_search(user_id, chat_id, keywords, date_range, offset)
                    return

            # ===== 处理等待输入 =====
            if self._pending_input and self._pending_input.get("chat_id") == message.chat_id:
                pending = self._pending_input
                input_type = pending.get("type") if pending else None
                self._pending_input = None  # 清除状态，避免死循环

                try:
                    if input_type == "block_tag":
                        from database import block_tag
                        await block_tag(text)
                        # 删除用户输入消息
                        try:
                            await self.bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
                        except Exception:
                            pass
                        await message.reply_text(f"✅ 已屏蔽标签: `{text}`", parse_mode="Markdown")

                    elif input_type == "mute_tag":
                        from utils import normalize_tag
                        from database import mute_tag
                        tag = normalize_tag(text.replace('#', ''))
                        until_ts = await mute_tag(tag, hours=24)
                        # 删除用户输入消息
                        try:
                            await self.bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
                        except Exception:
                            pass
                        await message.reply_text(f"🔕 已静音标签: `{tag}`\n⏳ 截止: `{until_ts}`", parse_mode="Markdown")

                    elif input_type == "block_artist":
                        if not text.isdigit():
                            await message.reply_text("❌ 画师ID必须是数字")
                            return
                        from database import block_artist
                        await block_artist(int(text))
                        # 删除用户输入消息
                        try:
                            await self.bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
                        except Exception:
                            pass
                        await message.reply_text(f"✅ 已屏蔽画师: `{text}`", parse_mode="Markdown")

                    elif input_type == "set_limit":
                        if not text.isdigit():
                            await message.reply_text("❌ 必须输入数字")
                            return
                        limit = int(text)
                        # 更新配置
                        self._save_config_value("filter", "daily_limit", limit)

                        # 删除提示消息与用户输入消息（Streaming）
                        try:
                            prompt_id = pending.get("prompt_id") if isinstance(pending, dict) else None
                            if prompt_id:
                                await self.bot.delete_message(chat_id=message.chat_id, message_id=prompt_id)
                        except Exception:
                            pass
                        try:
                            await self.bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
                        except Exception:
                            pass

                        await message.reply_text(f"✅ 每日推送上限已设置为: `{limit}`", parse_mode="Markdown")

                    elif input_type == "schedule_add":
                        # 添加时间点
                        import re
                        if not re.match(r'^\d{1,2}:\d{2}$', text):
                            await message.reply_text("❌ 格式错误，请使用 HH:MM (如 14:30)")
                            return
                        h, m = text.split(":")
                        new_cron = f"{m} {h} * * *"

                        # 读取当前配置
                        config = self._read_config()
                        current = config.get("schedule", "")

                        if current and "," in current:
                            # 已经是多个时间点，追加
                            schedule_data = f"{current},{new_cron}"
                        elif current:
                            # 单个时间点，转为多个
                            schedule_data = f"{current},{new_cron}"
                        else:
                            schedule_data = new_cron

                        if self.on_action:
                            await self.on_action("update_schedule", schedule_data)
                            # 删除用户输入消息
                            try:
                                await self.bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
                            except Exception:
                                pass
                            await message.reply_text(f"✅ 已添加推送时间: `{text}`", parse_mode="Markdown")
                        else:
                            await message.reply_text("⚠️ 未配置 Action 回调")

                    elif input_type == "schedule_custom":
                        # 自定义 Cron
                        try:
                            CronTrigger.from_crontab(text)
                            if self.on_action:
                                await self.on_action("update_schedule", text)
                                # 删除用户输入消息
                                try:
                                    await self.bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
                                except Exception:
                                    pass
                                await message.reply_text(f"✅ 定时任务已更新: `{text}`", parse_mode="Markdown")
                            else:
                                await message.reply_text("⚠️ 未配置 Action 回调")
                        except ValueError:
                            await message.reply_text("❌ 无效的 Cron 表达式，格式: `分 时 日 月 周`", parse_mode="Markdown")


                except Exception as e:
                    await message.reply_text(f"❌ 操作失败: {e}")

                return

            if not message.reply_to_message:
                return

            reply_msg_id = message.reply_to_message.message_id

            # 查找对应的 illust_id
            illust_id = self._message_illust_map.get(reply_msg_id)
            if not illust_id:
                return

            if text == "1":
                await self.handle_feedback(illust_id, "like", chat_id=message.chat_id)
                await message.reply_text("❤️ 已记录喜欢")
            elif text == "2":
                await self.handle_feedback(illust_id, "dislike", chat_id=message.chat_id)
                await message.reply_text("👎 已记录不喜欢")

        # /restart 指令 - 重启服务
        async def cmd_restart(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            # 保存用户消息ID用于删除
            user_msg_id = update.message.message_id
            chat_id = update.message.chat_id

            await update.message.reply_text("🔄 正在通过 systemctl 重启服务...")
            logger.info(f"用户 {user_id} 触发 systemctl 重启")

            # 删除用户的 /restart 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception as e:
                logger.debug(f"删除重启命令失败: {e}")

            # 使用 systemctl 重启服务（systemd 管理）
            import subprocess
            import asyncio
            # 延迟一点点确保消息发送出去
            await asyncio.sleep(1)
            try:
                subprocess.Popen(["systemctl", "restart", "pixiv-pusher"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                logger.error(f"systemctl 重启失败: {e}")

        # /push 指令 - 交互式推送菜单
        async def cmd_push(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                logger.warning(f"用户 {user_id} 尝试执行 /push 但被拒绝 (Allowed: {self.allowed_users})")
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id
            args = context.args

            # 删除用户的 /push 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /push 命令失败: {e}")

            # 有参数时直接处理（向后兼容）
            if args:
                await _handle_push_direct(user_id, chat_id, args)
                return

            # 无参数时显示交互式菜单
            self._push_sessions[user_id] = {"step": "select_mode", "message_ids": [], "user_message_ids": []}

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 今日精选推送", callback_data="push:today")],
                [InlineKeyboardButton("🎨 画师作品集", callback_data="push:artist")],
                [InlineKeyboardButton("📌 指定作品ID", callback_data="push:illust")],
                [InlineKeyboardButton("❌ 取消", callback_data="push_cancel")],
            ])

            msg = await update.message.reply_text(
                "🚀 *推送模式选择*\n\n请选择要执行的推送类型:",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            self._push_sessions[user_id]["message_ids"].append(msg.message_id)

        async def _handle_push_direct(user_id: int, chat_id: int, args: list):
            """直接处理带参数的 push 命令"""
            typing_task = asyncio.create_task(self._keep_typing(chat_id))
            try:
                if args[0].isdigit():
                    # 推送指定作品
                    illust_id = int(args[0])
                    status_msg = await self.bot.send_message(chat_id, f"🔍 正在获取作品 {illust_id}...")

                    try:
                        if self.client:
                            illust = await self.client.get_illust_detail(illust_id)
                            if illust:
                                await self.bot.edit_message_text(
                                    f"📨 正在推送: {illust.title}...",
                                    chat_id=chat_id,
                                    message_id=status_msg.message_id
                                )
                                sent = await self.send([illust])
                                # 删除状态消息
                                try:
                                    await self.bot.delete_message(chat_id, status_msg.message_id)
                                except:
                                    pass
                                if sent:
                                    await self.bot.send_message(chat_id, f"✅ 推送成功: {illust.title}")
                                else:
                                    await self.bot.send_message(chat_id, "❌ 推送失败")
                            else:
                                await self.bot.edit_message_text(
                                    f"❌ 未找到作品 {illust_id}",
                                    chat_id=chat_id,
                                    message_id=status_msg.message_id
                                )
                        else:
                            await self.bot.edit_message_text(
                                "⚠️ Pixiv 客户端未初始化",
                                chat_id=chat_id,
                                message_id=status_msg.message_id
                            )
                    except Exception as e:
                        logger.error(f"手动推送 {illust_id} 失败: {e}")
                        await self.bot.edit_message_text(
                            f"❌ 推送失败: {e}",
                            chat_id=chat_id,
                            message_id=status_msg.message_id
                        )

                elif len(args) > 1 and args[0] == "a" and args[1].isdigit():
                    # 推送指定画师近1年的随机作品
                    artist_id = int(args[1])
                    status_msg = await self.bot.send_message(chat_id, f"🔍 正在获取画师 {artist_id} 的作品库...")

                    try:
                        if self.client:
                            from datetime import datetime, timedelta, timezone
                            import random
                            one_year_ago = datetime.now(timezone.utc) - timedelta(days=365)
                            illusts = await self.client.get_user_illusts(artist_id, since=one_year_ago, limit=100)

                            if illusts:
                                sample_size = min(20, len(illusts))
                                sampled = random.sample(illusts, sample_size)
                                await self.bot.edit_message_text(
                                    f"🎲 正在为您生成画师 {artist_id} 的精选集... (抽取了 {sample_size}/{len(illusts)} 张)",
                                    chat_id=chat_id,
                                    message_id=status_msg.message_id
                                )

                                original_mode = self.batch_mode
                                self.batch_mode = "telegraph"
                                custom_title = f"画师 {artist_id} 精选集"
                                sent_ids = await self.send(sampled, custom_title)
                                self.batch_mode = original_mode

                                # 删除状态消息
                                try:
                                    await self.bot.delete_message(chat_id, status_msg.message_id)
                                except:
                                    pass

                                if sent_ids:
                                    await self.bot.send_message(chat_id, f"✅ 画师作品集生成完毕 (共 {sample_size} 张图，已加入队列)")
                                else:
                                    await self.bot.send_message(chat_id, "❌ 生成画师作品集失败")
                            else:
                                await self.bot.edit_message_text(
                                    f"❌ 未找到画师 {artist_id} 在近一年内的公开作品",
                                    chat_id=chat_id,
                                    message_id=status_msg.message_id
                                )
                        else:
                            await self.bot.edit_message_text(
                                "⚠️ Pixiv 客户端未初始化",
                                chat_id=chat_id,
                                message_id=status_msg.message_id
                            )
                    except Exception as e:
                        logger.error(f"画师随机推送 {artist_id} 失败: {e}")
                        await self.bot.edit_message_text(
                            f"❌ 推送失败: {e}",
                            chat_id=chat_id,
                            message_id=status_msg.message_id
                        )
                else:
                    # 触发全量推送任务
                    await self.bot.send_message(chat_id, "🚀 收到指令，正在启动推送任务...")
                    if self.on_action:
                        await self.on_action("run_task", None)
                    else:
                        await self.bot.send_message(chat_id, "⚠️ 内部错误: 未配置 Action 回调")
            finally:
                typing_task.cancel()

        # 处理 push 相关回调
        async def _handle_push_callback(query, data: str):
            """处理推送菜单回调"""
            user_id = query.from_user.id
            chat_id = query.message.chat_id

            if data == "push_cancel":
                # 删除所有消息
                await _delete_push_messages(user_id, chat_id)
                if user_id in self._push_sessions:
                    del self._push_sessions[user_id]
                await query.answer("已取消")
                return

            if data == "push:today":
                # 今日精选推送
                await query.edit_message_text("🚀 正在启动今日精选推送...")
                if self.on_action:
                    await self.on_action("run_task", None)
                else:
                    await query.edit_message_text("⚠️ 内部错误: 未配置 Action 回调")
                if user_id in self._push_sessions:
                    del self._push_sessions[user_id]
                return

            if data == "push:artist":
                session = self._push_sessions.get(user_id, {})
                session["step"] = "input_artist_id"

                await query.edit_message_text(
                    "🎨 *画师作品集*\n\n请输入画师ID:\n\n_例: `16419396`_",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 取消", callback_data="push_cancel")]]),
                    parse_mode="Markdown"
                )
                return

            if data == "push:illust":
                session = self._push_sessions.get(user_id, {})
                session["step"] = "input_illust_id"

                await query.edit_message_text(
                    "📌 *指定作品推送*\n\n请输入作品ID:\n\n_例: `12345678`_",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 取消", callback_data="push_cancel")]]),
                    parse_mode="Markdown"
                )
                return

        async def _delete_push_messages(user_id: int, chat_id: int):
            """删除 push 会话的所有消息"""
            session = self._push_sessions.get(user_id)
            if not session:
                return
            # 删除向导消息
            for msg_id in session.get("message_ids", []):
                try:
                    await self.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except:
                    pass
            # 删除用户输入消息
            for msg_id in session.get("user_message_ids", []):
                try:
                    await self.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except:
                    pass

        # Push 会话状态存储
        self._push_sessions = {}  # user_id -> {step, message_ids, user_message_ids}

        # 搜索会话状态存储
        self._search_sessions = {}  # user_id -> {step, date_range, offset, keywords, message_ids, user_message_ids}

        # /search 指令 - 交互式定向搜图
        async def cmd_search(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id

            # 删除用户的 /search 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /search 命令失败: {e}")

            # 检查是否有直接参数（旧模式兼容）
            args = context.args
            if args:
                # 旧模式：直接搜索
                search_input = " ".join(args)
                keywords = [k.strip().replace('#', '') for k in search_input.split("|") if k.strip()]
                if keywords:
                    await _do_search(user_id, chat_id, keywords, date_range_days=0, offset=0)
                    return

            # 新模式：启动交互式向导
            self._search_sessions[user_id] = {"step": "select_time", "message_ids": [], "user_message_ids": []}

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 不限时间", callback_data="search_time:0")],
                [InlineKeyboardButton("📅 最近一年", callback_data="search_time:365")],
                [InlineKeyboardButton("📅 最近一月", callback_data="search_time:30")],
                [InlineKeyboardButton("📅 最近一周", callback_data="search_time:7")],
                [InlineKeyboardButton("❌ 取消", callback_data="search_cancel")]
            ])

            msg = await update.message.reply_text(
                "🔍 *交互式搜索向导*\n\n"
                "第 1/3 步：请选择时间范围\n"
                "（默认按收藏数从高到低排序）",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            # 保存消息ID用于后续删除
            self._search_sessions[user_id]["message_ids"].append(msg.message_id)

        async def _do_search(user_id: int, chat_id: int, keywords: list, date_range_days: int, offset: int):
            """执行实际搜索（Streaming模式：中间状态消息自动删除）"""
            if not keywords:
                await self.bot.send_message(chat_id, "❌ 关键词不能为空")
                return

            # 收集所有需要删除的状态消息ID
            status_message_ids = []
            user_message_ids = []

            # 扩展关键词 (通过翻译反查 + 模糊匹配)
            import database as db_mod
            expanded_keywords = []
            has_expansion = False
            for k in keywords:
                # 1. 首先尝试精确匹配翻译名 -> 原标签名
                original = await db_mod.get_original_tag(k)
                if original and original.lower() != k.lower():
                    expanded_keywords.append(original)
                    has_expansion = True
                    continue
                
                # 2. 如果用户输入的是日文标签，检查是否需要补充其他翻译形式
                # 模糊搜索相似标签
                similar_tags = await db_mod.search_tags_with_translation(k, limit=3)
                if similar_tags and len(similar_tags) > 0:
                    # 找到相似标签，使用第一个匹配
                    matched_name = similar_tags[0][0]  # name
                    matched_trans = similar_tags[0][1]  # translated_name
                    if matched_name.lower() != k.lower():
                        expanded_keywords.append(matched_name)
                        has_expansion = True
                        continue
                
                # 3. 没有扩展，使用原关键词
                expanded_keywords.append(k)

            if has_expansion:
                 expansion_msg = await self.bot.send_message(
                     chat_id,
                     f"🔍 智能关联: {' | '.join(keywords)} → {' | '.join(expanded_keywords)}"
                 )
                 status_message_ids.append(expansion_msg.message_id)
                 keywords = expanded_keywords

            # 获取会话中的向导消息ID和用户输入消息ID
            session = self._search_sessions.get(user_id, {})
            status_message_ids = session.get("message_ids", []).copy()
            user_message_ids = session.get("user_message_ids", []).copy()

            msg = await self.bot.send_message(
                chat_id,
                f"🔍 搜索: {' | '.join(keywords)}\n"
                f"📅 时间: {'不限' if date_range_days == 0 else f'近{date_range_days}天'}\n"
                f"📄 批次: 第 {offset//20 + 1} 批 ({offset+1}-{offset+20})"
            )
            status_message_ids.append(msg.message_id)

            typing_task = asyncio.create_task(self._keep_typing(chat_id))
            try:
                if self.client:
                    filter_cfg = self._read_config().get("filter", {})
                    content_type = filter_cfg.get("content_type", "all")

                    # 计算需要获取的数量（偏移 + 20）
                    limit = offset + 20

                    # 搜索作品
                    illusts = await self.client.search_illusts(
                        tags=keywords,
                        bookmark_threshold=0,
                        date_range_days=date_range_days if date_range_days > 0 else None,
                        limit=limit,
                        content_type=content_type
                    )

                    if not illusts or len(illusts) <= offset:
                        await self.bot.send_message(chat_id, f"❌ 未找到足够的作品（仅找到 {len(illusts) if illusts else 0} 张）")
                        return

                    # 截取指定批次
                    batch = illusts[offset:offset+20]
                    
                    # 从搜索结果中提取并保存标签翻译
                    try:
                        import database as db_mod
                        tag_translations = []
                        for ill in batch:
                            for tag_name, tag_trans in zip(ill.tags or [], ill.tags_translated or []):
                                tag_trans = (tag_trans or "").strip()
                                if tag_trans and tag_name:
                                    tag_translations.append((tag_name, tag_trans))
                        if tag_translations:
                            asyncio.create_task(db_mod.save_tag_translations(tag_translations))
                            logger.debug(f"从搜索结果保存了 {len(tag_translations)} 个标签翻译")
                    except Exception as e:
                        logger.debug(f"保存搜索结果标签翻译失败: {e}")

                    # 过滤已推送的
                    filtered = [ill for ill in batch if not await db_mod.is_pushed(ill.id)]

                    if not filtered:
                        await self.bot.send_message(
                            chat_id,
                            f"⚠️ 该批次 {len(batch)} 张图都已推送过\n"
                            f"尝试获取下一批: /search 然后选择批次 {offset//20 + 2}"
                        )
                        return

                    # 发送进度消息
                    progress_msg = await self.bot.send_message(chat_id, f"📦 找到 {len(filtered)} 张符合条件的作品，生成画册...")
                    status_message_ids.append(progress_msg.message_id)

                    original_mode = self.batch_mode
                    self.batch_mode = "telegraph"
                    search_title = f"{' | '.join(keywords)} (第{offset//20+1}批)"
                    sent_ids = await self.send(filtered, search_title)
                    self.batch_mode = original_mode

                    # Streaming清理：删除所有状态消息和用户输入，只保留最终结果
                    for msg_id in status_message_ids:
                        try:
                            await self.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                        except Exception:
                            pass
                    for msg_id in user_message_ids:
                        try:
                            await self.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                        except Exception:
                            pass

                    if sent_ids:
                        msg = f"✅ 推送完成！共 {len(sent_ids)} 张\n"
                        msg += f"\n继续获取下一批：\n/search 然后选批次 {offset//20 + 2}"
                        await self.bot.send_message(chat_id, msg)
                    else:
                        await self.bot.send_message(chat_id, "❌ 画册生成失败")
                else:
                    await self.bot.send_message(chat_id, "⚠️ Pixiv 客户端未初始化")
            except Exception as e:
                logger.error(f"搜索失败: {e}")
                await self.bot.send_message(chat_id, f"❌ 搜索失败: {e}")
            finally:
                typing_task.cancel()

            # 清理会话
            if user_id in self._search_sessions:
                del self._search_sessions[user_id]

        async def _delete_search_guide_messages(user_id: int, chat_id: int):
            """删除搜索向导的所有消息（包括用户输入）"""
            session = self._search_sessions.get(user_id)
            if not session:
                return
            # 删除向导消息
            message_ids = session.get("message_ids", [])
            for msg_id in message_ids:
                try:
                    await self.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception as e:
                    logger.debug(f"删除向导消息 {msg_id} 失败: {e}")
            # 删除用户输入消息
            user_message_ids = session.get("user_message_ids", [])
            for msg_id in user_message_ids:
                try:
                    await self.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception as e:
                    logger.debug(f"删除用户消息 {msg_id} 失败: {e}")

        # 处理搜索向导的回调
        async def _handle_search_callback(query, data: str):
            user_id = query.from_user.id
            chat_id = query.message.chat_id

            if data == "search_cancel":
                # 删除所有向导消息
                await _delete_search_guide_messages(user_id, chat_id)
                if user_id in self._search_sessions:
                    del self._search_sessions[user_id]
                await query.answer("搜索已取消")
                return

            if data.startswith("search_time:"):
                days = int(data.split(":")[1])
                # 保留已有的 message_ids
                session = self._search_sessions.get(user_id, {})
                message_ids = session.get("message_ids", [])
                self._search_sessions[user_id] = {
                    "step": "input_batch",
                    "date_range": days,
                    "message_ids": message_ids
                }

                await query.edit_message_text(
                    f"🔍 *交互式搜索向导*\n\n"
                    f"第 2/3 步：请输入批次编号\n"
                    f"📅 已选择: {'不限时间' if days == 0 else f'近{days}天'}\n\n"
                    f"输入格式：数字 1-10\n"
                    f"• 1 = 第1-20张（热门）\n"
                    f"• 2 = 第21-40张\n"
                    f"• 3 = 第41-60张\n"
                    f"...\n\n"
                    f"直接回复此消息即可",
                    parse_mode="Markdown"
                )

            elif data.startswith("search_batch:"):
                batch_num = int(data.split(":")[1])
                session = self._search_sessions.get(user_id, {})
                session["offset"] = (batch_num - 1) * 20
                session["step"] = "input_keywords"

                dr = session.get("date_range", 0)
                date_text = "不限" if dr == 0 else f"近{dr}天"

                await query.edit_message_text(
                    f"🔍 *交互式搜索向导*\n\n"
                    f"第 3/3 步：请输入搜索关键词\n"
                    f"📅 时间: {date_text}\n"
                    f"📄 批次: 第 {batch_num} 批\n\n"
                    f"输入格式：\n"
                    f"• 单关键词: `白发`\n"
                    f"• 多关键词: `白发|黑丝|红瞳`\n"
                    f"（用 | 分隔，会自动去掉#号）\n\n"
                    f"直接回复此消息即可",
                    parse_mode="Markdown"
                )
        async def cmd_schedule(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id

            # 删除用户的 /schedule 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /schedule 命令失败: {e}")

            args = context.args
            if args:
                # 有参数时直接设置（向后兼容）
                input_str = " ".join(args)

                # 解析时间格式
                import re
                time_pattern = re.compile(r'^(\d{1,2}:\d{2})(,\d{1,2}:\d{2})*$')

                if time_pattern.match(input_str.replace(" ", "")):
                    times = [t.strip() for t in input_str.replace(" ", "").split(",")]
                    cron_list = []
                    for t in times:
                        h, m = t.split(":")
                        cron_list.append(f"{m} {h} * * *")
                    schedule_data = ",".join(cron_list)
                    display_times = ", ".join(times)
                else:
                    try:
                        CronTrigger.from_crontab(input_str)
                        schedule_data = input_str
                        display_times = input_str
                    except ValueError:
                        await update.message.reply_text("❌ 格式错误，请使用 `9:30` 或 Cron 表达式", parse_mode="Markdown")
                        return

                try:
                    if self.on_action:
                        await self.on_action("update_schedule", schedule_data)
                        await update.message.reply_text(f"✅ 定时任务已更新为: `{display_times}`", parse_mode="Markdown")
                    else:
                        await update.message.reply_text("⚠️ 内部错误: 未配置 Action 回调")
                except Exception as e:
                    await update.message.reply_text(f"❌ 设置失败: {e}")
                return

            # 无参数时显示交互式时间选择器
            await _show_schedule_menu(update.message)

        async def _show_schedule_menu(message):
            """显示定时任务设置菜单"""
            # 读取当前配置
            config = self._read_config()
            schedule = config.get("schedule", "45 9/3 * * *")  # 默认

            # 解析 cron 为友好显示
            display_time = _cron_to_friendly(schedule)

            lines = [
                "⏰ *推送时间设置*\n",
                f"当前: `{display_time}`\n",
                "选择预设时间或自定义:"
            ]

            # 预设时间按钮
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🌅 早晨 9:30", callback_data="schedule_set:9:30"),
                    InlineKeyboardButton("🌆 晚上 21:00", callback_data="schedule_set:21:00"),
                ],
                [
                    InlineKeyboardButton("☀️ 早+晚 (9:30,21:00)", callback_data="schedule_set:9:30,21:00"),
                ],
                [
                    InlineKeyboardButton("🕐 每小时推送", callback_data="schedule_set:0 * * * *"),
                    InlineKeyboardButton("🕘 每3小时推送", callback_data="schedule_set:0 */3 * * *"),
                ],
                [
                    InlineKeyboardButton("➕ 添加时间点", callback_data="schedule_add"),
                    InlineKeyboardButton("📝 自定义Cron", callback_data="schedule_custom"),
                ],
                [InlineKeyboardButton("⬅️ 返回菜单", callback_data="menu:main")],
            ])

            await message.reply_text(
                "\n".join(lines),
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

        def _cron_to_friendly(cron_str: str) -> str:
            """将 cron 表达式转换为友好显示"""
            # 处理多个 cron（逗号分隔）
            if "," in cron_str:
                crons = cron_str.split(",")
                return "; ".join([_cron_to_friendly(c) for c in crons])

            parts = cron_str.split()
            if len(parts) != 5:
                return cron_str  # 无法解析，返回原样

            m, h, dom, mon, dow = parts

            # 简单映射
            if dom == "*" and mon == "*" and dow == "*":
                if m == "0" and h == "*":
                    return "每小时整点"
                if m == "0" and h.startswith("*/"):
                    interval = h[2:]
                    return f"每{interval}小时整点"
                if "," in h:
                    hours = h.split(",")
                    return f"每天 {', '.join([f'{h}:{m}' for h in hours])}"
                if h.isdigit() and m.isdigit():
                    return f"每天 {h}:{m.zfill(2)}"

            return cron_str  # 复杂表达式返回原样

        # 处理 schedule 相关回调
        async def _handle_schedule_callback(query, data: str):
            """处理定时任务设置回调"""
            chat_id = query.message.chat_id

            if data == "schedule_add":
                await query.edit_message_text(
                    "⏰ 请回复要添加的时间点\n\n格式: `HH:MM` (24小时制)\n例: `14:30` 表示下午2点半",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 取消", callback_data="schedule_cancel")]]),
                    parse_mode="Markdown"
                )
                self._pending_input = {"type": "schedule_add", "chat_id": chat_id}
                return

            if data == "schedule_custom":
                await query.edit_message_text(
                    "📝 请回复 Cron 表达式\n\n格式: `分 时 日 月 周`\n例: `30 9,21 * * *` (每天9:30和21:30)",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 取消", callback_data="schedule_cancel")]]),
                    parse_mode="Markdown"
                )
                self._pending_input = {"type": "schedule_custom", "chat_id": chat_id}
                return

            if data == "schedule_cancel":
                await _show_schedule_menu(query.message)
                return

            if data.startswith("schedule_set:"):
                time_str = data.split(":", 1)[1]

                # 转换为 cron
                if ":" in time_str and "/" not in time_str:
                    # 友好格式: 9:30 或 9:30,21:00
                    times = time_str.split(",")
                    cron_list = []
                    for t in times:
                        h, m = t.split(":")
                        cron_list.append(f"{m} {h} * * *")
                    schedule_data = ",".join(cron_list)
                    display = time_str
                else:
                    # 已经是 cron
                    schedule_data = time_str
                    display = _cron_to_friendly(time_str)

                try:
                    if self.on_action:
                        await self.on_action("update_schedule", schedule_data)
                        await query.edit_message_text(
                            f"✅ 定时任务已更新为: `{display}`",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data="schedule_cancel")]]),
                            parse_mode="Markdown"
                        )
                    else:
                        await query.answer("⚠️ 未配置 Action 回调", show_alert=True)
                except Exception as e:
                    await query.answer(f"❌ 设置失败: {e}", show_alert=True)

        # /xp 指令 - 查看 XP 画像
        async def cmd_xp(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id

            # 删除用户的 /xp 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /xp 命令失败: {e}")

            try:
                from database import get_top_xp_tags
                top_tags = await get_top_xp_tags(15)

                if not top_tags:
                    await update.message.reply_text("📊 暂无 XP 画像数据")
                    return

                lines = ["🎯 *您的 XP 画像 Top 15*\n"]
                for i, (tag, weight) in enumerate(top_tags, 1):
                    bar = "█" * min(int(weight), 10)
                    # Tag 用反引号包裹防止解析错误
                    lines.append(f"{i}. `{tag}` {bar} ({weight:.1f})")

                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ 获取失败: {e}")

        # /stats 指令 - 查看 MAB 策略统计
        async def cmd_stats(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id

            # 删除用户的 /stats 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /stats 命令失败: {e}")

            try:
                from database import get_all_strategy_stats
                stats = await get_all_strategy_stats()

                if not stats:
                    await update.message.reply_text("📊 暂无策略统计数据")
                    return

                lines = ["📈 *MAB 策略表现*\n"]
                # 映射必须覆盖 fetcher.py 中所有的 key
                strategy_names = {
                    "xp_search": "XP搜索",
                    "search": "XP搜索(旧)",
                    "subscription": "订阅更新",
                    "ranking": "排行榜"
                }

                for strategy, data in stats.items():
                    name = strategy_names.get(strategy, strategy)
                    # 如果 fallback 到原始 key，必须转义下划线以免 markdown 解析错误
                    if name == strategy and "_" in name:
                        name = name.replace("_", "\\_")

                    rate_pct = data["rate"] * 100
                    lines.append(f"• *{name}*: {data['success']}/{data['total']} ({rate_pct:.1f}%)")

                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ 获取失败: {e}")

        # /status 指令 - 查看系统状态
        async def cmd_status(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id

            # 删除用户的 /status 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /status 命令失败: {e}")

            # 获取队列状态
            queue_info = "未知"
            try:
                if self.on_action:
                    # 通过回调获取队列状态
                    import asyncio
                    future = asyncio.Future()
                    
                    async def _get_status():
                        try:
                            result = await self.on_action("get_status", None)
                            future.set_result(result)
                        except Exception as e:
                            future.set_result(None)
                    
                    asyncio.create_task(_get_status())
                    # 等待最多2秒
                    try:
                        status_data = await asyncio.wait_for(future, timeout=2.0)
                        if status_data:
                            queue_info = f"{status_data.get('queue_used', '?')}/30"
                        else:
                            queue_info = "无法获取"
                    except asyncio.TimeoutError:
                        queue_info = "获取超时"
                else:
                    queue_info = "未配置回调"
            except Exception as e:
                queue_info = f"获取失败: {e}"

            # 读取配置信息
            try:
                from config import load_config
                cfg = load_config()
                filter_cfg = cfg.get("filter", {})
                fetcher_cfg = cfg.get("fetcher", {})
                
                daily_limit = filter_cfg.get("daily_limit", 20)
                date_range = fetcher_cfg.get("date_range_days", 7)
                bookmark_threshold = fetcher_cfg.get("bookmark_threshold", {}).get("search", 1000)
                match_threshold = fetcher_cfg.get("match_score", {}).get("min_threshold", 0.4)
                exclude_ai = filter_cfg.get("exclude_ai", True)
                r18_mode = filter_cfg.get("r18_mode", "mixed")
                
                lines = [
                    "📊 *系统状态*\n",
                    f"*推送队列*: `{queue_info}`",
                    f"*每日上限*: `{daily_limit}`",
                    f"*时间范围*: `{date_range}` 天",
                    f"*收藏阈值*: `{bookmark_threshold}`",
                    f"*匹配阈值*: `{match_threshold}`",
                    f"*AI过滤*: `{'开启' if exclude_ai else '关闭'}`",
                    f"*R18模式*: `{r18_mode}`",
                ]
                
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ 获取状态失败: {e}")

        # /block 指令 - 交互式标签屏蔽管理
        async def cmd_block(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id

            # 删除用户的 /block 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /block 命令失败: {e}")

            args = context.args
            if args:
                # 有参数时直接屏蔽（向后兼容）
                tag = " ".join(args).strip()
                try:
                    from database import block_tag
                    await block_tag(tag)
                    await update.message.reply_text(f"✅ 已屏蔽标签: `{tag}`", parse_mode="Markdown")
                except Exception as e:
                    await update.message.reply_text(f"❌ 屏蔽失败: {e}")
                return

            # 无参数时显示交互式菜单
            await _show_block_menu(update.message)

        async def _show_block_menu(message, page: int = 0):
            """显示标签屏蔽管理菜单"""
            from database import get_blocked_tags
            blocked = await get_blocked_tags()

            lines = ["🚫 *标签屏蔽管理*\n"]

            # 分页显示
            per_page = 12
            total_pages = (len(blocked) + per_page - 1) // per_page if blocked else 1
            page = max(0, min(page, total_pages - 1))

            start = page * per_page
            end = start + per_page
            page_items = blocked[start:end]

            if blocked:
                lines.append(f"当前屏蔽 *{len(blocked)}* 个标签 (第 {page+1}/{total_pages} 页):\n")
            else:
                lines.append("_暂无屏蔽标签_\n")

            # 构建按钮网格
            rows = []
            row = []
            for tag in page_items:
                # 标签名截断显示
                display_tag = tag[:10] + ".." if len(tag) > 10 else tag
                row.append(InlineKeyboardButton(f"❎ {display_tag}", callback_data=f"block_remove:{tag}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            # 分页按钮
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"block_page:{page-1}"))
            if page < total_pages - 1:
                nav_row.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"block_page:{page+1}"))
            if nav_row:
                rows.append(nav_row)

            # 操作按钮
            rows.append([
                InlineKeyboardButton("➕ 添加标签", callback_data="block_add"),
            ])
            rows.append([InlineKeyboardButton("⬅️ 返回菜单", callback_data="menu:main")])

            keyboard = InlineKeyboardMarkup(rows)

            await message.reply_text(
                "\n".join(lines),
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

        # /unblock 指令 - 交互式取消屏蔽
        async def cmd_unblock(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id

            # 删除用户的 /unblock 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /unblock 命令失败: {e}")

            args = context.args
            if args:
                # 有参数时直接取消屏蔽（向后兼容）
                tag = " ".join(args).strip()
                try:
                    from database import unblock_tag
                    result = await unblock_tag(tag)
                    if result:
                        await update.message.reply_text(f"✅ 已取消屏蔽标签: `{tag}`", parse_mode="Markdown")
                    else:
                        await update.message.reply_text(f"⚠️ 该标签未在屏蔽列表中: `{tag}`", parse_mode="Markdown")
                except Exception as e:
                    await update.message.reply_text(f"❌ 取消屏蔽失败: {e}")
                return

            # 无参数时显示交互式选择列表
            await _show_unblock_menu(update.message)

        async def _show_unblock_menu(message, page: int = 0):
            """显示取消屏蔽选择菜单"""
            from database import get_blocked_tags
            blocked = await get_blocked_tags()

            if not blocked:
                await message.reply_text(
                    "🚫 当前没有屏蔽的标签",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")]])
                )
                return

            lines = ["❎ *选择要取消屏蔽的标签*\n"]

            # 分页显示
            per_page = 12
            total_pages = (len(blocked) + per_page - 1) // per_page
            page = max(0, min(page, total_pages - 1))

            start = page * per_page
            end = start + per_page
            page_items = blocked[start:end]

            lines.append(f"共 {len(blocked)} 个标签 (第 {page+1}/{total_pages} 页):\n")

            # 构建按钮网格
            rows = []
            row = []
            for tag in page_items:
                display_tag = tag[:10] + ".." if len(tag) > 10 else tag
                row.append(InlineKeyboardButton(f"❎ {display_tag}", callback_data=f"unblock:{tag}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            # 分页按钮
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"unblock_page:{page-1}"))
            if page < total_pages - 1:
                nav_row.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"unblock_page:{page+1}"))
            if nav_row:
                rows.append(nav_row)

            rows.append([InlineKeyboardButton("⬅️ 返回菜单", callback_data="menu:main")])

            keyboard = InlineKeyboardMarkup(rows)

            await message.reply_text(
                "\n".join(lines),
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

        # 处理 block/unblock 回调
        async def _handle_block_callback(query, data: str):
            """处理屏蔽管理相关回调"""
            user_id = query.from_user.id
            chat_id = query.message.chat_id

            if data == "block_add":
                await query.edit_message_text(
                    "🚫 请回复要屏蔽的标签名称\n\n_支持 #号，自动归一化_",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 取消", callback_data="block_cancel")]]),
                    parse_mode="Markdown"
                )
                self._pending_input = {"type": "block_tag", "chat_id": chat_id}
                return

            if data == "block_cancel":
                await _show_block_menu(query.message)
                return

            if data.startswith("block_remove:"):
                tag = data.split(":", 1)[1]
                try:
                    from database import unblock_tag
                    await unblock_tag(tag)
                    await query.answer(f"✅ 已取消屏蔽: {tag}")
                except Exception as e:
                    await query.answer(f"❌ 失败: {e}", show_alert=True)
                    return
                # 刷新菜单
                await _show_block_menu(query.message)
                return

            if data.startswith("block_page:"):
                page = int(data.split(":", 1)[1])
                await _show_block_menu(query.message, page)
                return

            if data.startswith("unblock:"):
                tag = data.split(":", 1)[1]
                try:
                    from database import unblock_tag
                    result = await unblock_tag(tag)
                    if result:
                        await query.answer(f"✅ 已取消屏蔽: {tag}")
                    else:
                        await query.answer(f"⚠️ 未找到: {tag}")
                except Exception as e:
                    await query.answer(f"❌ 失败: {e}", show_alert=True)
                    return
                # 刷新菜单
                await _show_unblock_menu(query.message)
                return

            if data.startswith("unblock_page:"):
                page = int(data.split(":", 1)[1])
                await _show_unblock_menu(query.message, page)
                return

        # /mute 指令 - 临时静音标签（默认24小时），交互式
        async def cmd_mute(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id

            # 删除用户的 /mute 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /mute 命令失败: {e}")

            args = context.args
            import database as db

            # 有参数：直接静音（保持向后兼容）
            if args:
                raw = " ".join(args).strip()
                from utils import normalize_tag
                tag = normalize_tag(raw.replace('#', ''))
                until_ts = await db.mute_tag(tag, hours=24)
                await update.message.reply_text(
                    f"🔕 已静音标签: `{tag}`\n"
                    f"⏳ 截止: `{until_ts}`\n"
                    f"_可在菜单中提前撤销_",
                    parse_mode="Markdown"
                )
                return

            # 无参数：进入交互式菜单
            muted = await db.get_muted_tags(active_only=True)
            lines = ["🔕 *静音管理*\n"]
            if muted:
                lines.append("当前静音中:\n")
                for tag, until_ts in muted[:10]:
                    lines.append(f"  • `{tag}` → `{until_ts}`")
            else:
                lines.append("_暂无静音标签_")

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ 添加静音标签", callback_data="menu:mute:add")],
                [InlineKeyboardButton("❎ 取消静音标签", callback_data="menu:mute:remove")],
                [InlineKeyboardButton("📋 查看全部列表", callback_data="menu:mute")],
            ])
            await update.message.reply_text("\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")

        # /unmute 指令 - 提前撤销静音，交互式
        async def cmd_unmute(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id

            # 删除用户的 /unmute 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /unmute 命令失败: {e}")

            args = context.args
            import database as db

            # 有参数：直接取消（保持向后兼容）
            if args:
                raw = " ".join(args).strip()
                from utils import normalize_tag
                tag = normalize_tag(raw.replace('#', ''))
                ok = await db.unmute_tag(tag)
                await update.message.reply_text(
                    "✅ 已取消静音" if ok else "⚠️ 该标签当前未静音",
                    parse_mode="Markdown"
                )
                return

            # 无参数：进入交互式选择
            muted = await db.get_muted_tags(active_only=True)
            if not muted:
                await update.message.reply_text(
                    "🔕 当前没有静音标签\n\n"
                    "使用 `/mute` 添加静音",
                    parse_mode="Markdown"
                )
                return

            # 构建交互式按钮列表
            rows = []
            row = []
            for (tag, until_ts) in muted[:12]:
                row.append(InlineKeyboardButton(f"❎ {tag}", callback_data=f"menu:mute:unmute:{tag}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            await update.message.reply_text(
                "🔕 *选择要取消静音的标签：*",
                reply_markup=InlineKeyboardMarkup(rows),
                parse_mode="Markdown"
            )

        # /help 指令 - 帮助信息
        async def cmd_help(update, context):
            chat_id = update.message.chat_id

            # 删除用户的 /help 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /help 命令失败: {e}")

            help_text = (
                "*🤖 Bot 指令帮助*\n\n"
                "`/menu` - 📋 打开控制面板\n"
                "`/push` - 🚀 立即触发推送\n"
                "`/push <ID>` - 📌 推送指定作品\n"
                "`/push a <画师ID>` - 🎨 画师随机作品集\n"
                "`/search <关键词>` - 🔍 定向搜图 (支持多关键词用|分隔)\n"
                "`/xp` - 🎯 查看 XP 画像 (Top Tags)\n"
                "`/stats` - 📈 查看策略成功率\n"
                "`/schedule` - ⏰ 查看/修改定时时间\n"
                "`/block <tag>` - 🚫 屏蔽标签\n"
                "`/unblock <tag>` - ✅ 取消屏蔽标签\n"
                "`/mute [tag]` - 🔕 静音标签24小时（无参数进入交互式菜单）\n"
                "`/unmute [tag]` - 🔔 取消静音（无参数进入选择列表）\n"
                "`/block_artist <id>` - 🚫 屏蔽画师\n"
                "`/unblock_artist <id>` - ✅ 取消屏蔽画师\n"
                "`/batch` - 📦 批量模式设置\n"
                "`/help` - ℹ️ 显示此帮助\n\n"
                "*💡 推荐使用 /menu 菜单操作*"
            )
            await update.message.reply_text(help_text, parse_mode="Markdown")

        # /menu 和 /start 指令 - 打开控制面板
        async def cmd_menu(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id

            # 删除用户的 /menu 或 /start 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /menu 命令失败: {e}")

            await update.message.reply_text(
                "🤖 *XP Pusher 控制面板*",
                reply_markup=self._build_main_menu(),
                parse_mode="Markdown"
            )

        # /batch 指令 - 批量模式设置
        async def cmd_batch(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id

            # 删除用户的 /batch 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /batch 命令失败: {e}")

            args = context.args

            if not args:
                # 显示当前状态
                mode_emoji = "📦" if self.batch_mode == "telegraph" else "📄"
                status = (
                    f"*📦 批量模式设置*\n\n"
                    f"{mode_emoji} 当前模式: `{self.batch_mode}`\n"
                    f"📝 显示标题: `{'✅' if self.batch_show_title else '❌'}`\n"
                    f"🎨 显示画师: `{'✅' if self.batch_show_artist else '❌'}`\n"
                    f"🏷️ 显示标签: `{'✅' if self.batch_show_tags else '❌'}`\n\n"
                    "*用法:*\n"
                    "`/batch on` - 开启 Telegraph 批量模式\n"
                    "`/batch off` - 关闭批量模式\n"
                    "`/batch title on|off` - 开关标题\n"
                    "`/batch artist on|off` - 开关画师\n"
                    "`/batch tags on|off` - 开关标签"
                )
                await update.message.reply_text(status, parse_mode="Markdown")
                return

            cmd = args[0].lower()

            if cmd == "on":
                self.batch_mode = "telegraph"
                await update.message.reply_text("✅ 批量模式已开启 (Telegraph)")
            elif cmd == "off":
                self.batch_mode = "single"
                await update.message.reply_text("✅ 批量模式已关闭 (逐条发送)")
            elif cmd in ("title", "artist", "tags"):
                if len(args) < 2:
                    await update.message.reply_text(f"❌ 用法: `/batch {cmd} on|off`", parse_mode="Markdown")
                    return
                value = args[1].lower() in ("on", "true", "1", "yes")
                if cmd == "title":
                    self.batch_show_title = value
                elif cmd == "artist":
                    self.batch_show_artist = value
                elif cmd == "tags":
                    self.batch_show_tags = value
                await update.message.reply_text(f"✅ {cmd} 显示已{'开启' if value else '关闭'}")
            else:
                await update.message.reply_text("❌ 未知参数，使用 `/batch` 查看帮助", parse_mode="Markdown")

        # /block_artist 指令 - 屏蔽画师
        async def cmd_block_artist(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return

            chat_id = update.message.chat_id

            # 删除用户的 /block_artist 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /block_artist 命令失败: {e}")

            args = context.args
            if args:
                # 有参数时直接屏蔽（向后兼容）
                try:
                    artist_id = int(args[0])
                    artist_name = " ".join(args[1:]).strip() if len(args) > 1 else None

                    from database import block_artist
                    await block_artist(artist_id, artist_name)
                    await update.message.reply_text(f"✅ 已屏蔽画师: `{artist_id}`" + (f" ({artist_name})" if artist_name else ""), parse_mode="Markdown")
                except ValueError:
                    await update.message.reply_text("❌ 画师 ID 必须是数字")
                except Exception as e:
                    await update.message.reply_text(f"❌ 屏蔽失败: {e}")
                return

            # 无参数时显示交互式菜单
            await _show_block_artist_menu(update.message)

        async def _show_block_artist_menu(message, page: int = 0):
            """显示画师屏蔽管理菜单"""
            from database import get_blocked_artists
            blocked = await get_blocked_artists()

            lines = ["🎨 *画师屏蔽管理*\n"]

            # 分页显示
            per_page = 10
            total_pages = (len(blocked) + per_page - 1) // per_page if blocked else 1
            page = max(0, min(page, total_pages - 1))

            start = page * per_page
            end = start + per_page
            page_items = blocked[start:end]

            if blocked:
                lines.append(f"当前屏蔽 *{len(blocked)}* 个画师 (第 {page+1}/{total_pages} 页):\n")
            else:
                lines.append("_暂无屏蔽画师_\n")

            # 构建按钮网格
            rows = []
            row = []
            for artist_id, name in page_items:
                display_name = name[:8] + ".." if len(name) > 8 else name
                row.append(InlineKeyboardButton(f"❎ {display_name}", callback_data=f"block_artist_remove:{artist_id}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            # 分页按钮
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"block_artist_page:{page-1}"))
            if page < total_pages - 1:
                nav_row.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"block_artist_page:{page+1}"))
            if nav_row:
                rows.append(nav_row)

            # 操作按钮
            rows.append([InlineKeyboardButton("➕ 添加画师", callback_data="block_artist_add")])
            rows.append([InlineKeyboardButton("⬅️ 返回菜单", callback_data="menu:main")])

            keyboard = InlineKeyboardMarkup(rows)

            await message.reply_text(
                "\n".join(lines),
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

        # /unblock_artist 指令 - 交互式取消屏蔽画师
        async def cmd_unblock_artist(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return
            
            chat_id = update.message.chat_id
            
            # 删除用户的 /unblock_artist 命令
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                logger.debug(f"删除 /unblock_artist 命令失败: {e}")
            
            args = context.args
            if args:
                # 有参数时直接取消屏蔽（向后兼容）
                try:
                    artist_id = int(args[0])

                    from database import unblock_artist
                    result = await unblock_artist(artist_id)
                    if result:
                        await update.message.reply_text(f"✅ 已取消屏蔽画师: `{artist_id}`", parse_mode="Markdown")
                    else:
                        await update.message.reply_text(f"⚠️ 该画师未在屏蔽列表中: `{artist_id}`", parse_mode="Markdown")
                except ValueError:
                    await update.message.reply_text("❌ 画师 ID 必须是数字")
                except Exception as e:
                    await update.message.reply_text(f"❌ 取消屏蔽失败: {e}")
                return

            # 无参数时显示交互式选择列表
            await _show_unblock_artist_menu(update.message)

        async def _show_unblock_artist_menu(message, page: int = 0):
            """显示取消画师屏蔽选择菜单"""
            from database import get_blocked_artists
            blocked = await get_blocked_artists()

            if not blocked:
                await message.reply_text(
                    "🎨 当前没有屏蔽的画师",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")]])
                )
                return

            lines = ["❎ *选择要取消屏蔽的画师*\n"]

            # 分页显示
            per_page = 10
            total_pages = (len(blocked) + per_page - 1) // per_page
            page = max(0, min(page, total_pages - 1))

            start = page * per_page
            end = start + per_page
            page_items = blocked[start:end]

            lines.append(f"共 {len(blocked)} 个画师 (第 {page+1}/{total_pages} 页):\n")

            # 构建按钮网格
            rows = []
            row = []
            for artist_id, name in page_items:
                display_name = name[:8] + ".." if len(name) > 8 else name
                row.append(InlineKeyboardButton(f"❎ {display_name}", callback_data=f"unblock_artist:{artist_id}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            # 分页按钮
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"unblock_artist_page:{page-1}"))
            if page < total_pages - 1:
                nav_row.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"unblock_artist_page:{page+1}"))
            if nav_row:
                rows.append(nav_row)

            rows.append([InlineKeyboardButton("⬅️ 返回菜单", callback_data="menu:main")])

            keyboard = InlineKeyboardMarkup(rows)

            await message.reply_text(
                "\n".join(lines),
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

        # 处理画师屏蔽相关回调
        async def _handle_block_artist_callback(query, data: str):
            """处理画师屏蔽管理相关回调"""
            user_id = query.from_user.id
            chat_id = query.message.chat_id

            if data == "block_artist_add":
                await query.edit_message_text(
                    "🎨 请回复要屏蔽的画师ID\n\n_可附带画师名称，格式: `12345 画师名`_",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 取消", callback_data="block_artist_cancel")]]),
                    parse_mode="Markdown"
                )
                self._pending_input = {"type": "block_artist", "chat_id": chat_id}
                return

            if data == "block_artist_cancel":
                await _show_block_artist_menu(query.message)
                return

            if data.startswith("block_artist_remove:"):
                artist_id = int(data.split(":", 1)[1])
                try:
                    from database import unblock_artist
                    await unblock_artist(artist_id)
                    await query.answer(f"✅ 已取消屏蔽画师: {artist_id}")
                except Exception as e:
                    await query.answer(f"❌ 失败: {e}", show_alert=True)
                    return
                # 刷新菜单
                await _show_block_artist_menu(query.message)
                return

            if data.startswith("block_artist_page:"):
                page = int(data.split(":", 1)[1])
                await _show_block_artist_menu(query.message, page)
                return

            if data.startswith("unblock_artist:"):
                artist_id = int(data.split(":", 1)[1])
                try:
                    from database import unblock_artist
                    result = await unblock_artist(artist_id)
                    if result:
                        await query.answer(f"✅ 已取消屏蔽画师: {artist_id}")
                    else:
                        await query.answer(f"⚠️ 未找到画师: {artist_id}")
                except Exception as e:
                    await query.answer(f"❌ 失败: {e}", show_alert=True)
                    return
                # 刷新菜单
                await _show_unblock_artist_menu(query.message)
                return

            if data.startswith("unblock_artist_page:"):
                page = int(data.split(":", 1)[1])
                await _show_unblock_artist_menu(query.message, page)
                return

        self._app.add_handler(CommandHandler("push", cmd_push))
        self._app.add_handler(CommandHandler("restart", cmd_restart))
        self._app.add_handler(CommandHandler("schedule", cmd_schedule))
        self._app.add_handler(CommandHandler("xp", cmd_xp))
        self._app.add_handler(CommandHandler("stats", cmd_stats))
        self._app.add_handler(CommandHandler("status", cmd_status))
        self._app.add_handler(CommandHandler("block", cmd_block))
        self._app.add_handler(CommandHandler("unblock", cmd_unblock))
        self._app.add_handler(CommandHandler("mute", cmd_mute))
        self._app.add_handler(CommandHandler("unmute", cmd_unmute))
        self._app.add_handler(CommandHandler("block_artist", cmd_block_artist))
        self._app.add_handler(CommandHandler("unblock_artist", cmd_unblock_artist))
        self._app.add_handler(CommandHandler("batch", cmd_batch))
        self._app.add_handler(CommandHandler("search", cmd_search))
        self._app.add_handler(CommandHandler("menu", cmd_menu))
        self._app.add_handler(CommandHandler("start", cmd_menu))  # /start 也打开菜单
        self._app.add_handler(CommandHandler("help", cmd_help))
        self._app.add_handler(CallbackQueryHandler(callback_handler))
        self._app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, reply_handler))

        # 添加错误处理器，捕获轮询过程中的错误
        async def error_handler(update, context):
            """处理 Bot 轮询过程中的错误"""
            logger.error(f"Telegram 轮询错误: {context.error}")
            # 对于网络错误，updater 会自动重试，这里只做记录

        self._app.add_error_handler(error_handler)

        # 真正启动 Bot (非阻塞模式)
        await self._app.initialize()
        await self._app.start()

        # 注册菜单指令 (需在启动后)
        try:
            from telegram import BotCommand
            commands = [
                BotCommand("menu", "📋 控制面板"),
                BotCommand("push", "🚀 立即推送"),
                BotCommand("search", "🔍 定向搜图"),
                BotCommand("xp", "🎯 查看XP画像"),
                BotCommand("stats", "📈 策略表现"),
                BotCommand("schedule", "⏰ 定时任务"),
                BotCommand("block", "🚫 屏蔽标签"),
                BotCommand("mute", "🔕 静音标签24h"),
                BotCommand("unmute", "🔔 取消静音"),
                BotCommand("block_artist", "🎨 屏蔽画师"),
                BotCommand("batch", "📦 批量模式"),
                BotCommand("help", "ℹ️ 帮助信息"),
            ]
            await self._app.bot.set_my_commands(commands)
            logger.info("✅ Telegram 指令菜单已注册")
        except Exception as e:
            logger.error(f"注册指令菜单失败: {e}")

        # 轮询级别的错误回调（非异步）
        self._consecutive_errors = 0

        def polling_error_callback(error):
            """处理轮询过程中的网络错误（updater 会自动重试）"""
            self._consecutive_errors += 1
            logger.warning(f"Telegram 轮询网络错误 (第 {self._consecutive_errors} 次): {error}")

        # 启动轮询，配置更健壮的参数
        await self._app.updater.start_polling(
            poll_interval=1.0,           # 轮询间隔（秒）
            timeout=30,                  # 长轮询超时（秒）
            drop_pending_updates=True,   # 启动时丢弃旧的待处理更新，避免处理过期消息
            error_callback=polling_error_callback,  # 轮询错误回调
        )
        logger.info("Telegram Bot 轮询已启动（已配置自动重连）")

        # 启动健康检查后台任务
        if not getattr(self, "_polling_health_task", None) or self._polling_health_task.done():
            self._polling_health_task = asyncio.create_task(self._polling_health_check())

    async def _restart_polling(self) -> bool:
        """更稳健的轮询重启（带退避与重试）"""
        max_retries = 3
        delay = 2
        for attempt in range(max_retries):
            try:
                await self.stop_polling()
            except Exception as e:
                logger.warning(f"stop_polling 出错: {e}")

            await asyncio.sleep(1)

            try:
                await self.start_polling()
                logger.info("✅ Telegram 轮询已重启")
                self._consecutive_errors = 0
                return True
            except Exception as e:
                logger.error(f"重启轮询失败 (尝试 {attempt+1}/{max_retries}): {e}")
                await asyncio.sleep(delay)
                delay *= 2

        logger.error("Telegram 轮询多次重启失败，进入失败保护")
        return False

    async def _polling_health_check(self):
        """后台健康检查：监控轮询状态，自动重启"""
        await asyncio.sleep(60)  # 启动后等待一分钟再开始检查

        while True:
            try:
                await asyncio.sleep(60)  # 每分钟检查一次

                if not self._app or not self._app.updater:
                    logger.warning("Telegram 应用实例不存在，跳过健康检查")
                    continue

                # 检查 updater 是否还在运行
                if not self._app.updater.running:
                    logger.error("🔄 检测到 Telegram 轮询已停止，正在尝试重启...")
                    await self._restart_polling()
                else:
                    # 轮询正常运行，重置错误计数
                    if self._consecutive_errors > 0:
                        logger.info(f"Telegram 轮询恢复正常 (之前累计 {self._consecutive_errors} 次错误)")
                        self._consecutive_errors = 0

            except asyncio.CancelledError:
                logger.info("健康检查任务已取消")
                break
            except Exception as e:
                logger.error(f"健康检查异常: {e}")

    async def stop_polling(self):
        """停止 Bot 轮询（用于健康检查重启）"""
        try:
            if self._polling_health_task and not self._polling_health_task.done():
                self._polling_health_task.cancel()

            if self._app:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                    logger.info("Telegram updater 已停止")

                # 停止 application
                if self._app.running:
                    await self._app.stop()
                    logger.info("Telegram application 已停止")

                # 关闭 application
                await self._app.shutdown()
                logger.info("Telegram application 已关闭")

                self._app = None
        except Exception as e:
            logger.error(f"停止 Telegram 轮询时出错: {e}")

    async def send(self, illusts: list[Illust], custom_title: str = None) -> list[int]:
        """发送推送 (异步队列)"""
        if not illusts:
            return []

        # 将任务加入队列，同时捕获当前的 batch_mode 避免竞态
        await self.send_queue.put((illusts, custom_title, self.batch_mode))
        logger.info(f"已将 {len(illusts)} 个作品加入推送队列 (mode={self.batch_mode})")

        # 返回占位符，表示已接受 (避免阻塞调用方)
        return [-1]

    async def _send_direct(self, illusts: list[Illust], custom_title: str = None, batch_mode: str = None) -> list[int]:
        """直接发送推送 (内部方法)"""
        if not illusts:
            return []

        # 使用传入的 batch_mode，若未指定则回退到 self.batch_mode
        mode = batch_mode if batch_mode is not None else self.batch_mode

        # Telegraph 批量模式
        if mode == "telegraph" and len(illusts) > 1:
            return await self._send_batch_telegraph(illusts, custom_title)

        # 逐条发送模式
        success_ids = []

        for illust in illusts:
            try:
                is_sent = await self._send_single(illust)
                if is_sent:
                    success_ids.append(illust.id)
                await asyncio.sleep(2.0)  # 避免触发限流 (增加到2s)
            except Exception as e:
                logger.error(f"发送作品 {illust.id} 失败: {e}")

        return success_ids

    async def _init_telegraph(self):
        """延迟初始化 Telegraph 客户端"""
        if self._telegraph is None:
            try:
                from telegraph import Telegraph
                self._telegraph = Telegraph()
                self._telegraph.create_account(short_name='PixivXP')
                logger.info("Telegraph 客户端初始化成功")
            except Exception as e:
                logger.error(f"Telegraph 初始化失败: {e}")
                self._telegraph = False  # 标记为失败，避免重复尝试

    async def _send_batch_telegraph(self, illusts: list[Illust], custom_title: str = None) -> list[int]:
        """Telegraph 批量发送模式"""
        import database as db

        # 初始化 Telegraph
        await self._init_telegraph()
        if not self._telegraph:
            logger.warning("Telegraph 不可用，降级为逐条发送")
            return await self._send_batch_fallback(illusts)

        typing_task = None
        if self.chat_ids:
            typing_task = asyncio.create_task(self._keep_typing(int(self.chat_ids[0])))
        try:
            # 构建标题
            if custom_title:
                header = f"📚 {custom_title} ({len(illusts)}张)"
                page_title = custom_title
            else:
                header = f"📚 今日推送 ({len(illusts)}张)"
                page_title = f"Pixiv 推送 - {len(illusts)}张"

            lines = [header + "\n"]
            import html

            # 创建 Telegraph 页面
            telegraph_url = None
            try:
                content = await self._build_telegraph_content(illusts)
                response = self._telegraph.create_page(
                    title=page_title,
                    html_content=content
                )
                telegraph_url = f"https://telegra.ph/{response['path']}"
                lines.append(f"\n🔗 <a href='{telegraph_url}'>查看详情</a>")
            except Exception as e:
                logger.warning(f"创建 Telegraph 页面失败: {e}")
                lines.append(f"\n🔗 <i>(详情页创建失败)</i>")

            text = "\n".join(lines)

            # 构建反馈按钮
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("❤️ 喜欢", callback_data="batch_like"),
                    InlineKeyboardButton("👎 不喜欢", callback_data="batch_dislike"),
                ]
            ])

            # 发送消息
            success_ids = []
            for chat_id in self.chat_ids:
                try:
                    msg = await _retry_on_flood(lambda: self.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=self.thread_id,
                        disable_web_page_preview=False
                    ))
                    if msg:
                        # 保存映射
                        await db.save_batch_mapping(msg.message_id, chat_id, illusts)
                        success_ids = [i.id for i in illusts]  # 批量模式视为全部成功
                        logger.info(f"Telegraph 批量消息已发送: {len(illusts)} 个作品")
                except Exception as e:
                    logger.error(f"发送批量消息到 {chat_id} 失败: {e}")

            return success_ids
        finally:
            if typing_task:
                typing_task.cancel()

    async def _upload_image(self, session, url: str) -> str | None:
        """下载并上传图片到 Telegraph"""
        try:
            from utils import download_image_with_referer
            import aiohttp
            from PIL import Image
            import io

            # 1. 下载
            image_data = await download_image_with_referer(
                session,
                url,
                semaphore=self.client.download_semaphore if self.client else None,
                proxy=self.proxy_url
            )
            if not image_data:
                logger.warning(f"下载失败: {url}")
                return None

            # 2. 转换与压缩 (Telegraph 限制 5MB，且要求格式正确)
            # 我们统一转换为 JPEG 以避免 PNG/WebP 兼容问题
            try:
                with Image.open(io.BytesIO(image_data)) as img:
                    # 修复透明度
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    if img.mode in ('RGBA', 'LA'):
                        bg = Image.new('RGB', img.size, (255, 255, 255))
                        bg.paste(img, mask=img.split()[-1])
                        img = bg
                    elif img.mode != 'RGB':
                        img = img.convert('RGB')

                    # 尺寸限制 (Telegraph 虽无明确尺寸限制但过大会失败)
                    if max(img.size) > 2560: # 2K
                         img.thumbnail((2560, 2560), Image.Resampling.LANCZOS)

                    output = io.BytesIO()
                    img.save(output, format="JPEG", quality=90, optimize=True)

                    # 再次检查大小，确保 < 5MB
                    if output.tell() > 5 * 1024 * 1024:
                         output.seek(0)
                         output.truncate()
                         img.save(output, format="JPEG", quality=75, optimize=True)

                    image_data = output.getvalue()
            except Exception as e:
                logger.warning(f"图片转换失败 {url}: {e}，尝试直接上传")

            # 3. 上传
            data = aiohttp.FormData()
            data.add_field('file', image_data, filename='image.jpeg', content_type='image/jpeg')

            async with session.post('https://telegra.ph/upload', data=data) as resp:
                if resp.status == 200:
                    json_resp = await resp.json()
                    if isinstance(json_resp, list) and len(json_resp) > 0:
                        src = json_resp[0].get('src')
                        # logger.info(f"Telegraph 上传成功: {src}")
                        return src
                    else:
                        logger.warning(f"Telegraph 响应格式异常: {json_resp}")
                else:
                    logger.warning(f"Telegraph 上传失败 {resp.status}: {await resp.text()}")
        except Exception as e:
            logger.warning(f"Telegraph 处理异常 {url}: {e}")
        return None

    async def _build_telegraph_content(self, illusts: list[Illust]) -> str:
        """构建 Telegraph 页面内容 (并发上传图片)"""
        import aiohttp
        import asyncio
        import html

        # 准备结果容器 (为了保持顺序)
        results = [None] * len(illusts)

        async def process_one(idx, illust, sem, session):
            async with sem:
                img_src = None
                # 尝试上传图片
                if illust.image_urls:
                    # 优先使用 medium 以减小体积和加快速度 (Telegraph 也不需要原图)
                    target_url = illust.image_urls[0].replace("original", "medium") if "original" in illust.image_urls[0] else illust.image_urls[0]
                    # 如果原图太大，Telegraph 也会拒收 (限制 5MB)
                    # 这里的 target_url 是 pixiv 的 url

                    src_path = await self._upload_image(session, target_url)
                    if src_path:
                        img_src = f"https://telegra.ph{src_path}"
                    else:
                        # 失败回退到反代
                        img_src = get_pixiv_cat_url(illust.id)

                # 构建 HTML 片段
                parts = []
                if img_src:
                    parts.append(f'<img src="{img_src}"/>')

                safe_title = html.escape(illust.title)
                safe_user = html.escape(illust.user_name)

                parts.append(f'<h4>#{idx} {safe_title}</h4>')
                parts.append(f'<p>画师: <a href="https://pixiv.net/users/{illust.user_id}">{safe_user}</a></p>')
                parts.append(f'<p>❤️ {illust.bookmark_count} | 👁 {illust.view_count}</p>')
                parts.append(f'<p><a href="https://pixiv.net/i/{illust.id}">Pixiv 原图</a></p>')
                parts.append('<hr/>')

                results[idx-1] = "".join(parts)

        # 限制并发
        sem = asyncio.Semaphore(5)
        async with aiohttp.ClientSession() as session:
            tasks = [process_one(i, ill, sem, session) for i, ill in enumerate(illusts, 1)]
            await asyncio.gather(*tasks)

        return "".join([r for r in results if r])

    async def _send_batch_fallback(self, illusts: list[Illust]) -> list[int]:
        """批量模式降级：逐条发送"""
        success_ids = []
        for illust in illusts:
            try:
                if await self._send_single(illust):
                    success_ids.append(illust.id)
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"发送作品 {illust.id} 失败: {e}")
        return success_ids

    def _build_batch_select_keyboard(self, action: str, count: int) -> InlineKeyboardMarkup:
        """构建作品选择按钮"""
        rows = []
        # 每行最多 5 个按钮
        for i in range(0, count, 5):
            row = []
            for j in range(i, min(i + 5, count)):
                row.append(InlineKeyboardButton(
                    str(j + 1),
                    callback_data=f"batch_select:{action}:{j + 1}"
                ))
            rows.append(row)

        # 添加全选和取消按钮
        rows.append([
            InlineKeyboardButton("✅ 全部" + ("喜欢" if action == "like" else "不喜欢"),
                               callback_data=f"batch_all:{action}"),
            InlineKeyboardButton("❌ 取消", callback_data="batch_cancel"),
        ])

        return InlineKeyboardMarkup(rows)

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

    async def push_illusts(
        self,
        illusts: list,
        message_prefix: str = "",
        reply_to_message_id: int | None = None
    ) -> dict[int, int]:
        """
        推送作品列表（用于连锁推荐等场景）

        Args:
            illusts: 作品列表
            message_prefix: 消息前缀，会添加到 caption 开头
            reply_to_message_id: 要回复的消息 ID（用于形成消息链）

        Returns:
            dict[illust_id, message_id]: 成功发送的作品 ID 到消息 ID 的映射
        """
        if not illusts:
            return {}

        result_map = {}  # illust_id -> message_id

        for illust in illusts:
            try:
                # 构建 caption
                caption = self.format_message(illust)
                if message_prefix:
                    caption = f"{message_prefix}\n\n{caption}"

                keyboard = self._build_keyboard(illust)
                topic_id = self._resolve_topic_id(illust)

                # 下载图片
                image_data = None
                if self.client and illust.image_urls:
                    try:
                        image_data = await self.client.download_image(illust.image_urls[0])
                        if image_data:
                            image_data = self._compress_image(image_data)
                    except Exception as e:
                        logger.warning(f"下载图片失败: {e}")

                # 发送到第一个 chat_id（通常连锁推送只发给触发者所在的 chat）
                # 如果需要广播给所有 chat，可以改为遍历
                chat_id = self.chat_ids[0] if self.chat_ids else None
                if not chat_id:
                    continue

                sent_message = None
                try:
                    if image_data:
                        sent_message = await _retry_on_flood(lambda: self.bot.send_photo(
                            chat_id=chat_id,
                            photo=BytesIO(image_data),
                            caption=caption,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                            message_thread_id=topic_id,
                            reply_to_message_id=reply_to_message_id,
                            read_timeout=60,
                            write_timeout=60
                        ))
                    else:
                        from utils import get_pixiv_cat_url
                        proxy_url = get_pixiv_cat_url(illust.id)
                        sent_message = await _retry_on_flood(lambda: self.bot.send_photo(
                            chat_id=chat_id,
                            photo=proxy_url,
                            caption=caption,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                            message_thread_id=topic_id,
                            reply_to_message_id=reply_to_message_id,
                            read_timeout=60,
                            write_timeout=60
                        ))

                    if sent_message:
                        self._message_illust_map[sent_message.message_id] = illust.id
                        result_map[illust.id] = sent_message.message_id
                        logger.info(f"🔗 连锁推送成功: {illust.id} -> msg_id={sent_message.message_id}")

                except Exception as e:
                    logger.error(f"连锁推送到 {chat_id} 失败: {e}")

                await asyncio.sleep(1)  # 避免触发限流

            except Exception as e:
                logger.error(f"处理连锁作品 {illust.id} 失败: {e}")

        return result_map

    async def _send_single(self, illust: Illust) -> bool:
        """发送单个作品"""
        caption = self.format_message(illust)
        keyboard = self._build_keyboard(illust)

        # 动态 Topic ID
        topic_id = self._resolve_topic_id(illust)

        if getattr(illust, 'type', 'illust') == 'ugoira':
            return await self._send_video(illust, caption, keyboard, topic_id)

        # 多页逻辑
        if illust.page_count > self.max_pages:
            # 超过阈值：强制降级为封面模式
            # 在 caption 之后追加"长篇内容"提示
            long_caption = caption.replace("🎨", "📚 [长篇精选] 🎨")
            long_caption += f"\n\n<i>(本作品共 {illust.page_count} 页，仅展示封面)</i>"
            return await self._send_photo(illust, long_caption, keyboard, topic_id)

        if illust.page_count == 1 or self.multi_page_mode == "cover_link":
            # 单图或强制封面模式
            return await self._send_photo(illust, caption, keyboard, topic_id)
        else:
            # 多图打包模式 (2 到 max_pages 页)
            return await self._send_media_group(illust, caption, keyboard, topic_id)

    async def _send_photo(self, illust: Illust, caption: str, keyboard: InlineKeyboardMarkup, topic_id: int | None = None) -> bool:
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

        # 检测是否为 R18 内容（标签、标题、画师名）
        r18_keywords = ("r-18", "r18", "r-18g", "🔞")
        text_to_check = " ".join([
            " ".join(t.lower() for t in (illust.tags or [])),
            (illust.title or "").lower(),
            (illust.user_name or "").lower()
        ])
        has_r18_keyword = any(kw in text_to_check for kw in r18_keywords)
        is_r18 = bool(getattr(illust, "is_r18", False) or has_r18_keyword)

        # 发送到所有 chat_id
        for chat_id in self.chat_ids:
            sent_message = None
            try:
                if image_data:
                    sent_message = await _retry_on_flood(lambda: self.bot.send_photo(
                        chat_id=chat_id,
                        photo=BytesIO(image_data),
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=topic_id,
                        read_timeout=60,
                        write_timeout=60,
                        has_spoiler=is_r18
                    ))
                else:
                    # Fallback: 使用反代链接
                    proxy_url = get_pixiv_cat_url(illust.id)
                    sent_message = await _retry_on_flood(lambda: self.bot.send_photo(
                        chat_id=chat_id,
                        photo=proxy_url,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=self.thread_id,
                        read_timeout=60,
                        write_timeout=60,
                        has_spoiler=is_r18
                    ))

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

    async def _send_video(self, illust: Illust, caption: str, keyboard: InlineKeyboardMarkup, topic_id: int | None = None) -> bool:
        """发送动图视频 (优先PixivCat，失败则尝试本地转码)"""
        any_success = False
        video_url = f"https://pixiv.cat/{illust.id}.mp4"

        # 检测是否为 R18 内容（标签、标题、画师名）
        r18_keywords = ("r-18", "r18", "r-18g", "🔞")
        text_to_check = " ".join([
            " ".join(t.lower() for t in (illust.tags or [])),
            (illust.title or "").lower(),
            (illust.user_name or "").lower()
        ])
        has_r18_keyword = any(kw in text_to_check for kw in r18_keywords)
        is_r18 = bool(getattr(illust, "is_r18", False) or has_r18_keyword)

        # 缓存本地转码结果，避免重复下载转换
        local_mp4_bytes = None

        for chat_id in self.chat_ids:
            try:
                # 1. 如果已有本地数据，直接发送
                if local_mp4_bytes:
                    video_file = BytesIO(local_mp4_bytes)
                    video_file.name = f"{illust.id}.mp4"

                    await _retry_on_flood(lambda: self.bot.send_animation(
                        chat_id=chat_id,
                        animation=video_file,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=topic_id,
                        read_timeout=60,
                        write_timeout=60,
                        has_spoiler=is_r18
                    ))
                    any_success = True
                    continue

                # 2. 尝试反代 URL
                try:
                    sent = await _retry_on_flood(lambda: self.bot.send_animation(
                        chat_id=chat_id,
                        animation=video_url,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=topic_id,
                        read_timeout=60,
                        write_timeout=60,
                        has_spoiler=is_r18
                    ))
                    if sent:
                        self._message_illust_map[sent.message_id] = illust.id
                        any_success = True
                        continue
                except Exception:
                    # 如果 URL 发送失败，进入转码流程
                    pass

                # 3. 尝试本地转码 (仅当反代失败且尚未转码时)
                if not local_mp4_bytes and self.client:
                    logger.info(f"反代链接不可用，尝试本地转码作品 {illust.id}...")
                    try:
                        meta = await self.client.get_ugoira_metadata(illust.id)
                        if meta and meta.get('ugoira_metadata'):
                            u_meta = meta['ugoira_metadata']
                            zip_url = u_meta['zip_urls']['medium']
                            frames = u_meta['frames']

                            logger.info(f"正在下载动图包: {zip_url}")
                            zip_data = await self.client.download_image(zip_url)
                            if zip_data:
                                from utils import convert_ugoira_to_mp4
                                logger.info(f"正在转换 MP4 ({len(zip_data)} bytes)...")
                                local_mp4_bytes = convert_ugoira_to_mp4(zip_data, frames)
                    except Exception as exc:
                        logger.error(f"本地转码失败: {exc}")

                # 4. 如果转码成功，重试发送
                if local_mp4_bytes:
                    video_file = BytesIO(local_mp4_bytes)
                    video_file.name = f"{illust.id}.mp4"

                    sent = await _retry_on_flood(lambda: self.bot.send_animation(
                        chat_id=chat_id,
                        animation=video_file,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=topic_id,
                        read_timeout=120,
                        write_timeout=120,
                        has_spoiler=is_r18
                    ))
                    if sent:
                        self._message_illust_map[sent.message_id] = illust.id
                        any_success = True
                    continue

                # 5. 最终降级：发送封面
                raise Exception("所有动图发送方式均失败")

            except Exception as e:
                logger.warning(f"发送动图到 {chat_id} 失败: {e}")
                # 降级尝试发送封面
                try:
                   fallback_cap = caption + f"\n(⚠️ 动图发送失败，<a href='{video_url}'>点击观看</a>)"
                   await self._send_photo(illust, fallback_cap, keyboard)
                   any_success = True
                except:
                   pass
        return any_success

    async def _send_media_group(self, illust: Illust, caption: str, keyboard: InlineKeyboardMarkup, topic_id: int | None = None) -> bool:
        """发送多图到所有目标"""
        media = []
        any_success = False

        # 检测是否为 R18 内容（标签、标题、画师名）
        r18_keywords = ("r-18", "r18", "r-18g", "🔞")
        text_to_check = " ".join([
            " ".join(t.lower() for t in (illust.tags or [])),
            (illust.title or "").lower(),
            (illust.user_name or "").lower()
        ])
        has_r18_keyword = any(kw in text_to_check for kw in r18_keywords)
        is_r18 = bool(getattr(illust, "is_r18", False) or has_r18_keyword)

        # 限制在 max_pages 以内 (且不能超过 TG API 的 10 张限制)
        limit = min(self.max_pages, 10, len(illust.image_urls))
        for i, url in enumerate(illust.image_urls[:limit]):
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
                    parse_mode="HTML" if i == 0 else None,
                    has_spoiler=is_r18
                ))
            except Exception as e:
                logger.warning(f"获取第{i+1}页失败: {e}")

        if media:
            for chat_id in self.chat_ids:
                try:
                    await _retry_on_flood(lambda: self.bot.send_media_group(
                        chat_id=chat_id,
                        media=media,
                        message_thread_id=self.thread_id,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=60
                    ))
                    any_success = True  # 图片发送成功即视为成功

                    # MediaGroup不支持按钮，单独发送 (允许失败)
                    try:
                        await _retry_on_flood(lambda: self.bot.send_message(
                            chat_id=chat_id,
                            text=f"作品 #{illust.id} 的操作：",
                            reply_markup=keyboard,
                            message_thread_id=self.thread_id
                        ))
                    except Exception as e:
                        logger.warning(f"发送操作按钮到 {chat_id} 失败: {e}")

                except Exception as e:
                    logger.error(f"发送 MediaGroup 到 {chat_id} 失败: {e}")
        return any_success

    def format_message(self, illust: Illust) -> str:
        """格式化消息"""
        display_tags_list = getattr(illust, 'display_tags', illust.tags)
        tags = " ".join(f"#{t}" for t in display_tags_list[:5])
        r18_mark = "🔞 " if illust.is_r18 else ""
        ugoira_mark = "🎞️ " if getattr(illust, 'type', 'illust') == 'ugoira' else ""

        # 获取匹配度（如果有）
        match_score = getattr(illust, 'match_score', None)
        match_line = f"🎯 匹配度: {match_score*100:.0f}%\n" if match_score is not None else ""

        return (
            f"{r18_mark}{ugoira_mark}🎨 <b>{illust.title}</b>\n"
            f"👤 {illust.user_name} (ID: {illust.user_id})\n"
            f"❤️ {illust.bookmark_count} | 👀 {illust.view_count}\n"
            f"{match_line}"
            f"🏷️ {tags}\n"
            f"🔗 <a href=\"https://pixiv.net/i/{illust.id}\">原图链接</a>"
        )

    def _build_keyboard(self, illust: Illust) -> InlineKeyboardMarkup:
        """构建反馈按钮 (Vivi增强版)"""
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❤️ 收藏(公开)", callback_data=f"like:{illust.id}"),
                InlineKeyboardButton("👤 关注画师", callback_data=f"follow:{illust.user_id}")
            ],
            [
                InlineKeyboardButton("👎 不喜欢", callback_data=f"dislike:{illust.id}"),
                InlineKeyboardButton("🔗 Pixiv", url=f"https://www.pixiv.net/artworks/{illust.id}")
            ]
        ])

    async def handle_feedback(self, illust_id: int, action: str, chat_id: int | None = None) -> bool:
        """处理反馈回调 (Vivi增强版: 同步Pixiv操作)"""
        typing_task = None
        if action == "follow" and chat_id:
            typing_task = asyncio.create_task(self._keep_typing(chat_id))
        try:
            # 1. 调用原有的XP更新逻辑
            if self.on_feedback:
                await self.on_feedback(illust_id, action)

            # 2. 同步到Pixiv API
            if self.client:
                try:
                    if action == "like":
                        await self.client.add_bookmark(illust_id, private=False)
                        logger.info(f"[Pixiv] 公开收藏: {illust_id}")
                    elif action == "follow":
                        # 对于 follow，illust_id 参数实际上是 user_id（从 callback_data 传递过来的）
                        user_id = illust_id
                        try:
                            result = await self.client.api.user_follow_add(user_id, restrict='public')
                            logger.info(f"[Pixiv] user_follow_add API调用完成，user_id={user_id}, result={result}")

                            # 验证是否真的关注了
                            await asyncio.sleep(1)  # 等待API同步
                            user_detail = await self.client.api.user_detail(user_id)
                            is_followed = user_detail.get('user', {}).get('is_followed', False)
                            logger.info(f"[Pixiv] 验证关注状态: user_id={user_id}, is_followed={is_followed}")

                            if is_followed:
                                logger.info(f"[Pixiv] 关注画师成功(已验证): {user_id}")
                            else:
                                logger.error(f"[Pixiv] 关注画师失败: API调用后is_followed仍为False")
                        except Exception as e:
                            logger.error(f"[Pixiv] 关注画师异常: {e}")
                            import traceback
                            logger.error(traceback.format_exc())
                except Exception as e:
                    logger.error(f"[Pixiv] 操作失败: {e}")

            return True
        finally:
            if typing_task:
                typing_task.cancel()



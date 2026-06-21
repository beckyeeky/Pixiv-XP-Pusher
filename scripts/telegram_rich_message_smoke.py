"""Smoke-test Telegram Bot API Rich Message with a real liked Pixiv image.

By default this is a dry run. Pass --send to actually send to Telegram.
"""

from __future__ import annotations

import argparse
import asyncio
import random
from pathlib import Path
from typing import Any

import aiohttp

from config import load_config
from pixiv_client import PixivClient
from telegram_rich import build_input_rich_message


def _redact_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 12:
        return "***"
    return f"{token[:6]}...{token[-4:]}"


def _first_chat_id(chat_ids: Any) -> str | None:
    if isinstance(chat_ids, list):
        for chat_id in chat_ids:
            if chat_id:
                return str(chat_id)
        return None
    if chat_ids:
        return str(chat_ids)
    return None


async def _pick_liked_illust_id() -> int:
    import database as db

    liked_ids = await db.get_liked_illusts()
    if not liked_ids:
        raise RuntimeError("本地 feedback 表里没有 action='like' 的作品；请用 --illust-id 指定一个作品 ID")
    return int(random.choice(list(liked_ids)))


async def _send_rich_message(token: str, chat_id: str, payload: dict[str, Any], proxy_url: str | None) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/sendRichMessage"
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, proxy=proxy_url) as resp:
            data = await resp.json(content_type=None)
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or data)
    return data.get("result") or {}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Send or preview a Telegram Rich Message with a liked Pixiv image.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--illust-id", type=int, help="Pixiv illustration ID; defaults to a random locally liked item")
    parser.add_argument("--chat-id", help="Override notifier.telegram.chat_ids[0]")
    parser.add_argument("--send", action="store_true", help="Actually send the message. Without this, only prints a dry-run summary.")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    tg_cfg = cfg.get("notifier", {}).get("telegram", {})
    pixiv_cfg = cfg.get("pixiv", {})
    network_cfg = cfg.get("network", {})

    token = tg_cfg.get("bot_token") or ""
    chat_id = args.chat_id or _first_chat_id(tg_cfg.get("chat_ids") or tg_cfg.get("chat_id"))
    proxy_url = tg_cfg.get("proxy_url") or network_cfg.get("proxy_url")
    if not token:
        raise RuntimeError("config.yaml 缺少 notifier.telegram.bot_token")
    if not chat_id:
        raise RuntimeError("config.yaml 缺少 notifier.telegram.chat_ids，或使用 --chat-id 指定")

    illust_id = args.illust_id or await _pick_liked_illust_id()
    client = PixivClient(
        refresh_token=pixiv_cfg.get("refresh_token"),
        proxy_url=network_cfg.get("proxy_url"),
    )
    await client.login()
    illust = await client.get_illust_detail(illust_id)
    if not illust:
        raise RuntimeError(f"无法获取 Pixiv 作品详情: {illust_id}")

    rich_message = build_input_rich_message(illust, extra_note="Rich Message smoke test")
    payload = {
        "chat_id": chat_id,
        "rich_message": rich_message,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "Pixiv", "url": f"https://www.pixiv.net/artworks/{illust.id}"},
            ]]
        },
    }
    thread_id = tg_cfg.get("thread_id")
    if thread_id is not None:
        payload["message_thread_id"] = thread_id

    print("Telegram Rich Message smoke test")
    print(f"  token: {_redact_token(token)}")
    print(f"  chat_id: {chat_id}")
    print(f"  illust: {illust.id} - {illust.title} / {illust.user_name}")
    print(f"  html_length: {len(rich_message['html'])}")
    if not args.send:
        print("  mode: dry-run (pass --send to send)")
        return 0

    result = await _send_rich_message(token, chat_id, payload, proxy_url)
    print(f"  sent message_id: {result.get('message_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

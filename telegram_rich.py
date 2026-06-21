"""Helpers for Telegram Bot API Rich Message payloads."""

from __future__ import annotations

from html import escape
from typing import Any


RICH_MESSAGE_DEFAULTS = {
    "enabled": False,
    "fallback_to_photo": True,
}


def normalize_rich_message_config(value: Any) -> dict[str, bool]:
    """Return a stable rich_message config block."""
    if not isinstance(value, dict):
        value = {}
    return {
        "enabled": bool(value.get("enabled", RICH_MESSAGE_DEFAULTS["enabled"])),
        "fallback_to_photo": bool(value.get("fallback_to_photo", RICH_MESSAGE_DEFAULTS["fallback_to_photo"])),
    }


def pixiv_cat_image_url(illust_id: int, page: int = 0) -> str:
    if page <= 0:
        return f"https://pixiv.cat/{illust_id}.jpg"
    return f"https://pixiv.cat/{illust_id}-{page + 1}.jpg"


def is_r18_illust(illust: Any) -> bool:
    r18_keywords = ("r-18", "r18", "r-18g", "🔞")
    tags = [str(t).lower() for t in getattr(illust, "tags", []) or []]
    text_to_check = " ".join([
        " ".join(tags),
        str(getattr(illust, "title", "") or "").lower(),
        str(getattr(illust, "user_name", "") or "").lower(),
    ])
    return bool(getattr(illust, "is_r18", False) or any(kw in text_to_check for kw in r18_keywords))


def build_rich_message_html(illust: Any, *, image_url: str | None = None, extra_note: str | None = None) -> str:
    """Build conservative Rich Message HTML for a Pixiv illustration."""
    illust_id = int(getattr(illust, "id"))
    title = escape(str(getattr(illust, "title", "") or "Untitled"))
    user_name = escape(str(getattr(illust, "user_name", "") or "Unknown"))
    user_id = escape(str(getattr(illust, "user_id", "") or ""))
    bookmark_count = escape(str(getattr(illust, "bookmark_count", 0) or 0))
    view_count = escape(str(getattr(illust, "view_count", 0) or 0))
    display_tags = getattr(illust, "display_tags", None) or getattr(illust, "tags", []) or []
    tags = " ".join(f"#{escape(str(tag))}" for tag in display_tags[:5])
    tags = tags or "无标签"
    match_score = getattr(illust, "match_score", None)
    image_url = image_url or pixiv_cat_image_url(illust_id)
    spoiler_attr = " tg-spoiler" if is_r18_illust(illust) else ""
    marks = []
    if is_r18_illust(illust):
        marks.append("R18")
    if getattr(illust, "type", "illust") == "ugoira":
        marks.append("Ugoira")
    prefix = f"[{' / '.join(marks)}] " if marks else ""

    lines = [
        f'<figure><img src="{escape(image_url, quote=True)}"{spoiler_attr}/>'
        f"<figcaption><b>{prefix}{title}</b><cite>{user_name}</cite></figcaption></figure>",
        f"<p>画师: {user_name} (ID: {user_id})</p>",
        f"<p>收藏: {bookmark_count} | 浏览: {view_count}</p>",
    ]
    if match_score is not None:
        try:
            lines.append(f"<p>匹配度: {float(match_score) * 100:.0f}%</p>")
        except (TypeError, ValueError):
            pass
    lines.append(f"<p>标签: {tags}</p>")
    if extra_note:
        lines.append(f"<p><i>{escape(extra_note)}</i></p>")
    lines.append(f'<p><a href="https://www.pixiv.net/artworks/{illust_id}">原图链接</a></p>')
    return "\n".join(lines)


def build_input_rich_message(illust: Any, *, image_url: str | None = None, extra_note: str | None = None) -> dict[str, Any]:
    return {
        "html": build_rich_message_html(illust, image_url=image_url, extra_note=extra_note),
    }

import sys
import types
import unittest
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda *args, **kwargs: {}, dump=lambda *args, **kwargs: None))

import config
from telegram_rich import build_input_rich_message, build_rich_message_html, normalize_rich_message_config


@dataclass
class DummyIllust:
    id: int = 123456
    title: str = "A <danger> & test"
    user_id: int = 42
    user_name: str = "Artist & Co"
    tags: list[str] = None
    bookmark_count: int = 100
    view_count: int = 200
    page_count: int = 1
    image_urls: list[str] = None
    is_r18: bool = False
    ai_type: int = 0
    create_date: datetime = datetime(2026, 6, 21)
    type: str = "illust"

    def __post_init__(self):
        if self.tags is None:
            self.tags = ["tag<1>", "R-18"]
        if self.image_urls is None:
            self.image_urls = ["https://example.com/a.jpg"]


class TelegramRichMessageTests(unittest.TestCase):
    def test_normalize_rich_message_defaults_to_disabled_with_fallback_and_photo_mode(self):
        self.assertEqual(
            normalize_rich_message_config(None),
            {"enabled": False, "fallback_to_photo": True, "image_mode": "photo"},
        )

    def test_normalize_rich_message_accepts_legacy_config_and_rejects_bad_mode(self):
        self.assertEqual(
            normalize_rich_message_config({"enabled": True, "fallback_to_photo": False}),
            {"enabled": True, "fallback_to_photo": False, "image_mode": "photo"},
        )
        self.assertEqual(normalize_rich_message_config({"image_mode": "bad"})["image_mode"], "photo")
        self.assertEqual(normalize_rich_message_config({"image_mode": "hybrid"})["image_mode"], "hybrid")

    def test_config_normalization_adds_telegram_rich_message_defaults(self):
        cfg = config.normalize_config({"notifier": {"telegram": {}}})
        self.assertEqual(
            cfg["notifier"]["telegram"]["rich_message"],
            {"enabled": False, "fallback_to_photo": True, "image_mode": "photo"},
        )

    def test_build_rich_message_html_escapes_text_and_marks_spoiler(self):
        html = build_rich_message_html(DummyIllust())
        self.assertIn("A &lt;danger&gt; &amp; test", html)
        self.assertIn("Artist &amp; Co", html)
        self.assertIn("tag&lt;1&gt;", html)
        self.assertIn("tg-spoiler", html)
        self.assertIn("https://pixiv.cat/123456.jpg", html)
        self.assertIn("https://www.pixiv.net/artworks/123456", html)

    def test_build_input_rich_message_uses_html_syntax(self):
        payload = build_input_rich_message(DummyIllust(is_r18=False, tags=["safe"]))
        self.assertEqual(list(payload), ["html"])
        self.assertNotIn("tg-spoiler", payload["html"])

    def test_notifier_rich_sender_keeps_reply_markup_and_reply_parameters(self):
        source = (Path(__file__).resolve().parents[1] / "notifier" / "telegram.py").read_text(encoding="utf-8")
        self.assertIn('payload["reply_markup"] = reply_markup', source)
        self.assertIn('payload["reply_parameters"] = {"message_id": reply_to_message_id}', source)
        self.assertIn('self._message_illust_map[message_id] = illust.id', source)

    def test_rich_command_uses_inline_keyboard_controls(self):
        source = (Path(__file__).resolve().parents[1] / "notifier" / "telegram.py").read_text(encoding="utf-8")
        self.assertIn("def _build_rich_menu", source)
        self.assertIn('callback_data="menu:set:rich_fallback"', source)
        self.assertIn('callback_data=f"menu:set:rich_mode:{mode}"', source)
        self.assertIn('reply_markup=self._build_rich_menu()', source)

    def test_related_push_respects_rich_modes(self):
        source = (Path(__file__).resolve().parents[1] / "notifier" / "telegram.py").read_text(encoding="utf-8")
        self.assertIn('self.rich_message_image_mode == "rich_card"', source)
        self.assertIn('self.rich_message_image_mode == "hybrid"', source)
        self.assertIn('extra_note=message_prefix or None', source)
        self.assertIn('reply_to_message_id=reply_to_message_id', source)

    def test_photo_mode_still_uses_send_photo_for_clickable_images(self):
        source = (Path(__file__).resolve().parents[1] / "notifier" / "telegram.py").read_text(encoding="utf-8")
        self.assertIn('self.rich_message_image_mode == "rich_card"', source)
        self.assertIn('return await self._send_photo(illust, caption, keyboard, topic_id)', source)
        self.assertIn('return result_map if return_message_map else any_success', source)


if __name__ == "__main__":
    unittest.main()

import re
import unittest
from inspect import signature
from pathlib import Path

import database as db_module
from web.app import app as canonical_app
from web.app import api_gallery, gallery
from web.app_v2 import app as compat_app


class WebEntrypointTests(unittest.TestCase):
    def test_legacy_entrypoint_reexports_canonical_app(self):
        self.assertIs(canonical_app, compat_app)
        self.assertEqual(canonical_app.title, "Pixiv-XP-Pusher")

    def test_web_app_has_no_merge_conflict_markers(self):
        app_source = Path(__file__).resolve().parents[1] / "web" / "app.py"
        content = app_source.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^<<<<<<< .*", content, re.MULTILINE))
        self.assertIsNone(re.search(r"^>>>>>>> .*", content, re.MULTILINE))
        self.assertIsNone(re.search(r"^=======$", content, re.MULTILINE))

    def test_gallery_defaults_match_five_by_five_layout(self):
        self.assertEqual(signature(gallery).parameters["page"].default.default, 1)
        self.assertEqual(signature(api_gallery).parameters["limit"].default, 25)
        self.assertEqual(signature(db_module.get_push_history_paginated).parameters["limit"].default, 25)


if __name__ == "__main__":
    unittest.main()

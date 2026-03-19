import re
import unittest
from pathlib import Path

from web.app import app as canonical_app
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


if __name__ == "__main__":
    unittest.main()

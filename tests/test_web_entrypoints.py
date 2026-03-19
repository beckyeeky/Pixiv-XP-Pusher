import unittest

from web.app import app as canonical_app
from web.app_v2 import app as compat_app


class WebEntrypointTests(unittest.TestCase):
    def test_legacy_entrypoint_reexports_canonical_app(self):
        self.assertIs(canonical_app, compat_app)
        self.assertEqual(canonical_app.title, "Pixiv-XP-Pusher")


if __name__ == "__main__":
    unittest.main()

import importlib
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path


class _AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class PixivClientCompatTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        sys.modules.setdefault("aiohttp", types.SimpleNamespace(ClientSession=object))
        sys.modules.setdefault("pixivpy_async", types.SimpleNamespace(AppPixivAPI=object))
        cls.pixiv_client_module = importlib.import_module("pixiv_client")

    def build_client(self, api):
        client = self.pixiv_client_module.PixivClient.__new__(self.pixiv_client_module.PixivClient)
        client.api = api
        client.rate_limiter = _AsyncContext()
        client._logged_in = True
        client._token_last_refresh = datetime.now()
        client._unsupported_api_params_logged = set()
        client._parse_illust = lambda item: types.SimpleNamespace(bookmark_count=item["total_bookmarks"])
        client.login = lambda: None
        return client

    async def test_search_illusts_drops_unsupported_content_type(self):
        captured = {}

        class LegacySearchApi:
            async def search_illust(self, word, search_target, sort, start_date=None):
                captured.update({
                    "word": word,
                    "search_target": search_target,
                    "sort": sort,
                    "start_date": start_date,
                })
                return {"illusts": [{"total_bookmarks": 10}], "next_url": None}

            def parse_qs(self, _url):
                return None

        client = self.build_client(LegacySearchApi())
        results = await client.search_illusts(
            tags=["blue_archive"],
            bookmark_threshold=0,
            date_range_days=0,
            limit=1,
            content_type="manga",
        )

        self.assertEqual(len(results), 1)
        self.assertNotIn("content_type", captured)
        self.assertEqual(captured["word"], "blue_archive")

    async def test_get_ranking_drops_unsupported_content_type(self):
        captured = {}

        class LegacyRankingApi:
            async def illust_ranking(self, mode, date=None):
                captured.update({"mode": mode, "date": date})
                return {"illusts": [{"total_bookmarks": 10}], "next_url": None}

            def parse_qs(self, _url):
                return None

        client = self.build_client(LegacyRankingApi())
        results = await client.get_ranking(
            mode="day",
            date="2026-07-08",
            limit=1,
            content_type="illust",
        )

        self.assertEqual(len(results), 1)
        self.assertNotIn("content_type", captured)
        self.assertEqual(captured["mode"], "day")


if __name__ == "__main__":
    unittest.main()

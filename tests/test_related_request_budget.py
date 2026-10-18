"""Request budget for scheduled related seeds.

The orchestration tests use the real ``PixivClient.get_related_illusts`` with a
fake Pixiv API object, so the retry/pagination wrapper and JSON error handling
are exercised rather than mocking ``get_related_illusts``. The
``RealSDKTransportBudgetTests`` go one level lower: the real
``pixivpy_async.AppPixivAPI`` (including its own ``Net.fetch`` retry decorator)
with only the HTTP session and ``ClientManager`` replaced, so a physical
``session.get`` count is asserted. Neither class covers host network behaviour.
"""

import asyncio
import importlib
import json
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch


def _load_pixivpy():
    """Load the installed ``pixivpy_async``; other modules only plant stubs."""
    module = sys.modules.get("pixivpy_async")
    if module is not None and hasattr(module, "__path__"):
        return module
    for name in [n for n in list(sys.modules) if n.split(".")[0] == "pixivpy_async"]:
        del sys.modules[name]
    try:
        return importlib.import_module("pixivpy_async")
    except Exception:  # pragma: no cover - environment without the SDK
        stub = types.SimpleNamespace(AppPixivAPI=object)
        sys.modules["pixivpy_async"] = stub
        return stub


pixivpy_async = _load_pixivpy()

import database
import pixiv_client
from related_recommender import RelatedRecommender


class _AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def raw_illust(illust_id: int, *, user_id: int = 10) -> dict:
    return {
        "id": illust_id,
        "title": f"illust-{illust_id}",
        "user": {"id": user_id, "name": f"artist-{user_id}"},
        "tags": [{"name": "good"}],
        "total_bookmarks": 100,
        "total_view": 1000,
        "page_count": 1,
        "image_urls": {"large": f"https://example.com/{illust_id}.jpg"},
        "illust_ai_type": 0,
        "create_date": "2026-10-01T00:00:00+00:00",
    }


class FakeRelatedAPI:
    """Fake pixivpy API: records every underlying related request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def illust_related(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    @staticmethod
    def parse_qs(next_url):
        if not next_url:
            return None
        return {"illust_id": 9001, "offset": 30}


def build_real_client(api) -> pixiv_client.PixivClient:
    client = pixiv_client.PixivClient.__new__(pixiv_client.PixivClient)
    client.api = api
    client.rate_limiter = _AsyncContext()
    client._logged_in = True
    client._token_last_refresh = datetime.now()
    client.refresh_token = None
    client._pending_tag_translations = []
    client._unsupported_api_params_logged = set()
    return client


def add_pref_bookmark(illust_id: int) -> None:
    with sqlite3.connect(database.DB_PATH) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO preference_bookmarks "
            "(user_id, illust_id, tags, imported) VALUES (?,?,?,?)",
            (900001, illust_id, json.dumps(["good"]), 1),
        )


def fetchone(sql: str, params: tuple = ()):
    with sqlite3.connect(database.DB_PATH) as connection:
        return connection.execute(sql, params).fetchone()


def build_recommender(client) -> RelatedRecommender:
    return RelatedRecommender(
        client=client,
        config={"fetcher": {"bookmark_threshold": {"related": 0}}},
        bookmark_threshold={"related": 0},
    )


# Real pixivpy_async error envelope: HTTP status is not preserved by the library.
RATE_LIMIT_ERROR = {
    "error": {
        "user_message": "",
        "message": "Rate Limit",
        "reason": "",
        "user_message_lang": "en_US",
        "code": 0,
    }
}

# Same body plus the HTTP status the upstream sometimes embeds in the JSON.
RATE_LIMIT_WITH_STATUS = {
    "error": dict(RATE_LIMIT_ERROR["error"], status=429)
}


class FakeHTTPResponse:
    """Minimal aiohttp response for the SDK's own ``Net.fetch`` coroutine."""

    def __init__(self, payload=None, *, fail=None, status=200):
        self.payload = payload
        self.fail = fail
        self.status = status

    async def __aenter__(self):
        if self.fail is not None:
            raise self.fail
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self.payload


class RecordingSession:
    """``aiohttp.ClientSession`` stand-in; records physical transport calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.get_calls = []
        self.post_calls = []

    def _next(self):
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            return FakeHTTPResponse(fail=item)
        return FakeHTTPResponse(item)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._next()

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._next()


class RecordingClientManager:
    """Patched ``pixivpy_async.net.ClientManager``; captures SDK conn options."""

    instances = []

    def __init__(self, session, **options):
        self.session = session
        self.options = options
        RecordingClientManager.instances.append(self)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


def build_sdk_client(responses, *, proxy=None):
    """Real ``AppPixivAPI`` with fake session I/O (no sockets, no SDK stubs)."""
    session = RecordingSession(responses)
    api = pixivpy_async.AppPixivAPI(client=session, proxy=proxy)
    api.set_auth("dummy-access-token")
    return session, api


class ClientRequestBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_page_no_retry_stops_even_when_next_url_exists(self):
        api = FakeRelatedAPI([
            {"illusts": [raw_illust(910)], "next_url": "https://example.com/next?offset=30"},
            {"illusts": [raw_illust(911)], "next_url": None},
        ])
        client = build_real_client(api)

        result = await client.get_related_illusts(
            9001, limit=20, single_page=True, no_retry=True
        )

        self.assertEqual([ill.id for ill in result], [910])
        self.assertEqual(len(api.calls), 1)

    async def test_default_call_keeps_retry_and_pagination(self):
        api = FakeRelatedAPI([
            RuntimeError("temporary upstream failure"),
            {"illusts": [raw_illust(920)], "next_url": "https://example.com/next?offset=30"},
            {"illusts": [raw_illust(921)], "next_url": None},
        ])
        client = build_real_client(api)

        with patch("utils.asyncio.sleep", new=AsyncMock()):
            result = await client.get_related_illusts(9001, limit=31)

        self.assertEqual([ill.id for ill in result], [920, 921])
        self.assertEqual(len(api.calls), 3)

    async def test_api_error_payload_raises_instead_of_returning_empty(self):
        api = FakeRelatedAPI([RATE_LIMIT_ERROR])
        client = build_real_client(api)

        with self.assertRaises(pixiv_client.PixivAPIError) as raised:
            await client.get_related_illusts(9001, limit=20, single_page=True, no_retry=True)

        self.assertIn("Rate Limit", str(raised.exception))
        self.assertEqual(len(api.calls), 1)


class ScheduledSeedRequestBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_seed_uses_one_page_and_does_not_read_next_page(self):
        add_pref_bookmark(9001)
        api = FakeRelatedAPI([
            {"illusts": [raw_illust(910)], "next_url": "https://example.com/next?offset=30"},
            {"illusts": [raw_illust(911)], "next_url": None},
        ])
        client = build_real_client(api)

        result = await build_recommender(client).discover_for_strategy(
            [("good", 1.0)], limit=20
        )

        self.assertEqual([ill.id for ill in result], [910])
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.calls[0]["illust_id"], 9001)

    async def test_rate_limit_error_stops_before_second_seed(self):
        add_pref_bookmark(9001)
        add_pref_bookmark(9002)
        api = FakeRelatedAPI([RATE_LIMIT_ERROR])
        client = build_real_client(api)

        result = await build_recommender(client).discover_for_strategy(
            [("good", 1.0)], limit=20
        )

        self.assertEqual(result, [])
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.calls[0]["illust_id"], 9001)
        state, cooldown_until = fetchone(
            "SELECT state, cooldown_until FROM related_seed_state WHERE illust_id=9001"
        )
        self.assertEqual(state, "throttled")
        self.assertGreater(cooldown_until, 0)
        # The unrequested second seed lease is released without cooldown.
        second = fetchone(
            "SELECT claim_token, cooldown_until FROM related_seed_state WHERE illust_id=9002"
        )
        self.assertEqual(second, (None, 0))

    async def test_chain_default_keeps_original_client_contract(self):
        api = FakeRelatedAPI([
            {"illusts": [raw_illust(930)], "next_url": "https://example.com/next?offset=30"},
            {"illusts": [raw_illust(931)], "next_url": None},
        ])
        client = build_real_client(api)
        observed = AsyncMock(wraps=client.get_related_illusts)
        client.get_related_illusts = observed
        recommender = RelatedRecommender(client=client, config={})
        seed = pixiv_client.Illust(
            id=9001,
            title="seed",
            user_id=1,
            user_name="artist",
            tags=["good"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=[],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        with patch("related_recommender.db.get_artist_score", new=AsyncMock(return_value=0.0)):
            await recommender.push_chain(seed, [], parent_msg_id=None, current_depth=1)

        self.assertEqual(observed.await_args.args, (9001,))
        self.assertEqual(observed.await_args.kwargs, {"limit": 20})
        self.assertEqual(len(api.calls), 1)


class RealSDKTransportBudgetTests(unittest.IsolatedAsyncioTestCase):
    """Physical ``session.get`` budget with the real SDK and fake session I/O.

    These tests fail if the SDK's own ``Net.fetch`` retry re-sends a request or
    if the default chain swallows/raises on an error body differently from the
    baseline. They cannot cover host network behaviour (DNS/TLS/real Pixiv).
    """

    def setUp(self):
        if not hasattr(pixivpy_async, "__path__"):
            self.skipTest("pixivpy_async is not installed")
        RecordingClientManager.instances.clear()

    def transport_patches(self):
        return (
            patch("pixivpy_async.net.ClientManager", RecordingClientManager),
            patch("asyncio.sleep", new=AsyncMock()),
        )

    async def test_read_timeout_costs_exactly_one_transport_request(self):
        add_pref_bookmark(9101)
        session, api = build_sdk_client(
            [asyncio.TimeoutError("synthetic read timeout")],
            proxy="http://127.0.0.1:7890",
        )
        client = build_real_client(api)
        manager_patch, sleep_patch = self.transport_patches()

        with manager_patch, sleep_patch:
            result = await build_recommender(client).discover_for_strategy(
                [("good", 1.0)], limit=20
            )

        self.assertEqual(result, [])
        self.assertEqual(
            len(session.get_calls), 1, "SDK retry re-sent a timed-out related read"
        )
        url, kwargs = session.get_calls[0]
        self.assertTrue(url.endswith("/v2/illust/related"))
        self.assertEqual(kwargs["params"], {"illust_id": 9101, "filter": "for_ios"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer dummy-access-token")
        # Proxy semantics still come from the SDK's own ClientManager options.
        self.assertEqual(
            RecordingClientManager.instances[0].options["proxy"], "http://127.0.0.1:7890"
        )
        # The shared API instance keeps the SDK's decorated (retrying) fetch.
        self.assertNotIn("fetch", api.__dict__)
        state, claim_token, cooldown_until = fetchone(
            "SELECT state, claim_token, cooldown_until FROM related_seed_state "
            "WHERE illust_id=9101"
        )
        self.assertEqual(state, "error")
        self.assertIsNone(claim_token)
        self.assertGreater(cooldown_until, 0)

    async def test_429_body_with_http_status_makes_one_request_and_stops_round(self):
        add_pref_bookmark(9201)
        add_pref_bookmark(9202)
        session, api = build_sdk_client([RATE_LIMIT_WITH_STATUS])
        client = build_real_client(api)
        manager_patch, sleep_patch = self.transport_patches()

        with manager_patch, sleep_patch:
            result = await build_recommender(client).discover_for_strategy(
                [("good", 1.0)], limit=20
            )

        self.assertEqual(result, [])
        self.assertEqual(len(session.get_calls), 1)
        state, cooldown_until = fetchone(
            "SELECT state, cooldown_until FROM related_seed_state WHERE illust_id=9201"
        )
        self.assertEqual(state, "throttled")
        self.assertGreater(cooldown_until, 0)
        # The unrequested second seed is released without a read or cooldown.
        self.assertEqual(
            fetchone(
                "SELECT claim_token, cooldown_until FROM related_seed_state "
                "WHERE illust_id=9202"
            ),
            (None, 0),
        )

    async def test_next_url_is_not_followed_by_the_scheduled_seed_read(self):
        add_pref_bookmark(9301)
        session, api = build_sdk_client([
            {
                "illusts": [raw_illust(9302)],
                "next_url": "https://app-api.pixiv.net/v2/illust/related?illust_id=9301&offset=30",
            },
            {"illusts": [raw_illust(9303)], "next_url": None},
        ])
        client = build_real_client(api)
        manager_patch, sleep_patch = self.transport_patches()

        with manager_patch, sleep_patch:
            result = await build_recommender(client).discover_for_strategy(
                [("good", 1.0)], limit=20
            )

        self.assertEqual([illust.id for illust in result], [9302])
        self.assertEqual(len(session.get_calls), 1)

    async def test_default_chain_error_body_returns_empty_after_one_request(self):
        session, api = build_sdk_client([RATE_LIMIT_WITH_STATUS])
        client = build_real_client(api)
        manager_patch, sleep_patch = self.transport_patches()

        with manager_patch, sleep_patch:
            result = await client.get_related_illusts(9001, limit=20)

        self.assertEqual(result, [])
        self.assertEqual(
            len(session.get_calls), 1, "default chain amplified an API-error body"
        )

    async def test_default_chain_keeps_partial_page_when_later_page_errors(self):
        session, api = build_sdk_client([
            {
                "illusts": [raw_illust(9401)],
                "next_url": "https://app-api.pixiv.net/v2/illust/related?illust_id=9001&offset=30",
            },
            RATE_LIMIT_WITH_STATUS,
        ])
        client = build_real_client(api)
        manager_patch, sleep_patch = self.transport_patches()

        with manager_patch, sleep_patch:
            result = await client.get_related_illusts(9001, limit=31)

        self.assertEqual([illust.id for illust in result], [9401])
        self.assertEqual(len(session.get_calls), 2)

    async def test_default_chain_still_retries_transport_errors(self):
        session, api = build_sdk_client([
            asyncio.TimeoutError("transient read timeout"),
            {"illusts": [raw_illust(9501)], "next_url": None},
        ])
        client = build_real_client(api)
        manager_patch, sleep_patch = self.transport_patches()

        with manager_patch, sleep_patch:
            result = await client.get_related_illusts(9001, limit=20)

        self.assertEqual([illust.id for illust in result], [9501])
        self.assertEqual(
            len(session.get_calls), 2, "shared SDK retry must stay available to the chain"
        )

    async def test_token_refresh_is_a_separate_budget_from_the_seed_read(self):
        add_pref_bookmark(9601)
        auth_body = {
            "response": {
                "access_token": "refreshed-token",
                "refresh_token": "refresh-2",
                "user": {"id": 1, "name": "offline"},
            }
        }
        session, api = build_sdk_client([
            auth_body,
            {"illusts": [raw_illust(9602)], "next_url": None},
        ])
        client = build_real_client(api)
        client.refresh_token = "refresh-1"
        client._token_last_refresh = datetime.now() - timedelta(hours=2)
        manager_patch, sleep_patch = self.transport_patches()

        with manager_patch, sleep_patch:
            result = await build_recommender(client).discover_for_strategy(
                [("good", 1.0)], limit=20
            )

        self.assertEqual([illust.id for illust in result], [9602])
        self.assertEqual(len(session.post_calls), 1, "expected one auth refresh request")
        self.assertEqual(len(session.get_calls), 1, "expected one related read")
        self.assertEqual(
            session.get_calls[0][1]["headers"]["Authorization"],
            "Bearer refreshed-token",
        )


if __name__ == "__main__":
    unittest.main()

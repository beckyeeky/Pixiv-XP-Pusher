"""Regression tests for the optional Native Jev (search + System One) backend.

Network is fully mocked: every client takes an injected JSON requester.
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SECRET = "test-exa-secret-value"


def run(coro):
    return asyncio.run(coro)


def _response(provider="exa", pool_id="exa-1", query="q", urls=("https://example.test/a",),
              snippets=("サンプルタグ is a visual feature.",), usage=None):
    from search_grounded_judge import SearchResponse

    return SearchResponse(
        provider=provider, pool_id=pool_id, query=query,
        sources=[{"url": url, "title": f"title {index}"} for index, url in enumerate(urls)],
        snippets=list(snippets), usage=usage or {},
    )


class SecretResolutionTests(unittest.TestCase):
    def test_provider_key_wins_over_environment(self):
        from native_jev_judge import resolve_search_credential

        with patch.dict(os.environ, {"EXA_API_KEY": "env-value"}):
            self.assertEqual(
                resolve_search_credential({"api_key": "provider-value"}, env_names=("EXA_API_KEY",)),
                "provider-value",
            )

    def test_environment_is_used_when_the_provider_entry_is_empty(self):
        from native_jev_judge import resolve_search_credential

        with patch.dict(os.environ, {"EXA_API_KEY": SECRET}):
            self.assertEqual(
                resolve_search_credential({"api_key": ""}, env_names=("EXA_API_KEY",)), SECRET,
            )

    def test_env_file_is_the_last_supported_source(self):
        from native_jev_judge import resolve_search_credential

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "exa.env"
            path.write_text(f"EXA_API_KEY={SECRET}\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("EXA_API_KEY", None)
                self.assertEqual(
                    resolve_search_credential({}, env_names=("EXA_API_KEY",), env_file=path), SECRET,
                )

    def test_missing_credential_is_reported_without_any_value(self):
        from native_jev_judge import resolve_search_credential

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXA_API_KEY", None)
            self.assertEqual(resolve_search_credential({}, env_names=("EXA_API_KEY",)), "")

    def test_provider_api_key_env_name_is_honoured(self):
        from native_jev_judge import resolve_search_credential

        with patch.dict(os.environ, {"MY_EXA": SECRET}):
            self.assertEqual(
                resolve_search_credential(
                    {"api_key_env": "MY_EXA"}, env_names=("EXA_API_KEY",),
                ),
                SECRET,
            )

    def test_credential_source_description_never_contains_the_value(self):
        from native_jev_judge import describe_credential_source

        with patch.dict(os.environ, {"EXA_API_KEY": SECRET}):
            described = describe_credential_source({"api_key": ""}, env_names=("EXA_API_KEY",))
        self.assertEqual(described["source"], "env")
        self.assertEqual(described["env"], "EXA_API_KEY")
        self.assertNotIn(SECRET, json.dumps(described))


class ExaSearchClientTests(unittest.TestCase):
    def test_exa_rest_request_shape_and_parsing(self):
        from native_jev_judge import ExaSearchClient
        from search_grounded_judge import SearchCredentialPool, SearchPoolConfig

        seen = {}

        async def requester(method, url, headers, *, params=None, json_body=None, timeout_seconds=30):
            seen.update({"method": method, "url": url, "headers": headers,
                         "json": json_body, "params": params})
            return 200, {"results": [
                {"url": "https://dic.pixiv.net/a/1", "title": "タグ", "text": "サンプルタグ は特徴です。"},
            ]}, {}

        pool = SearchCredentialPool([SearchPoolConfig("exa-1", SECRET, request_limit=10)])
        client = ExaSearchClient(pool, request_json=requester, num_results=3, max_characters=2500)
        result = run(client.search('"サンプルタグ" pixiv タグ 意味'))

        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url"], "https://api.exa.ai/search")
        self.assertEqual(seen["headers"]["x-api-key"], SECRET)
        self.assertNotIn("Authorization", seen["headers"])
        self.assertEqual(seen["json"]["numResults"], 3)
        self.assertEqual(seen["json"]["contents"]["text"]["maxCharacters"], 2500)
        self.assertEqual(result.provider, "exa")
        self.assertEqual(result.sources[0]["url"], "https://dic.pixiv.net/a/1")
        self.assertTrue(result.snippets)

    def test_exa_empty_result_is_no_evidence(self):
        from native_jev_judge import ExaSearchClient
        from search_grounded_judge import SearchCredentialPool, SearchNoEvidence, SearchPoolConfig

        async def requester(*_args, **_kwargs):
            return 200, {"results": []}, {}

        client = ExaSearchClient(
            SearchCredentialPool([SearchPoolConfig("exa-1", SECRET)]), request_json=requester,
        )
        with self.assertRaises(SearchNoEvidence):
            run(client.search("q"))

    def test_exa_auth_and_rate_limit_failures_are_classified_and_redacted(self):
        from native_jev_judge import ExaSearchClient
        from search_grounded_judge import (
            PoolQuotaExhausted, PoolUnavailable, SearchCredentialPool, SearchPoolConfig,
        )

        def build(status):
            async def requester(*_args, **_kwargs):
                return status, {"error": "invalid api key abc"}, {}

            return ExaSearchClient(
                SearchCredentialPool([SearchPoolConfig("exa-1", SECRET)]), request_json=requester,
            )

        with self.assertRaises(PoolUnavailable) as unauthorized:
            run(build(401).search("q"))
        self.assertNotIn(SECRET, str(unauthorized.exception))
        with self.assertRaises(PoolQuotaExhausted):
            run(build(429).search("q"))

    def test_two_exa_accounts_balance_by_utilization(self):
        from native_jev_judge import ExaSearchClient
        from search_grounded_judge import SearchCredentialPool, SearchPoolConfig

        async def requester(*_args, **_kwargs):
            return 200, {"results": [{"url": "https://example.test", "text": "x"}]}, {}

        pool = SearchCredentialPool([
            SearchPoolConfig("exa-a", "key-a", request_limit=4),
            SearchPoolConfig("exa-b", "key-b", request_limit=4),
        ], initial_requests_used={"exa-a": 2})
        client = ExaSearchClient(pool, request_json=requester)

        async def three():
            return [(await client.search("q")).pool_id for _ in range(3)]

        self.assertEqual(run(three()), ["exa-b", "exa-b", "exa-a"])


class DeepSeekSearchClientTests(unittest.TestCase):
    def test_deepseek_search_request_shape_and_parsing(self):
        from native_jev_judge import DeepSeekSearchClient
        from search_grounded_judge import SearchCredentialPool, SearchPoolConfig

        seen = {}

        async def requester(method, url, headers, *, params=None, json_body=None, timeout_seconds=30):
            seen.update({"method": method, "url": url, "headers": headers, "json": json_body})
            return 200, {"results": [{
                "url": "https://dic.pixiv.net/a/2", "title": "サンプルタグ",
                "summary": "サンプルタグ の説明", "snippet": "サンプルタグ は特徴。",
            }]}, {}

        client = DeepSeekSearchClient(
            SearchCredentialPool([SearchPoolConfig("deepseek-1", "ds-key")]),
            request_json=requester, base_url="https://api.deepseek.com/v1",
        )
        result = run(client.search('"サンプルタグ" pixiv'))

        self.assertEqual(seen["url"], "https://api.deepseek.com/v1/tools/search")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer ds-key")
        self.assertEqual(seen["json"]["query"], '"サンプルタグ" pixiv')
        self.assertEqual(result.provider, "deepseek_search")
        self.assertEqual(result.sources[0]["url"], "https://dic.pixiv.net/a/2")


class JevClientTests(unittest.TestCase):
    def _client(self, status=200, body=None, capture=None, key="or-key"):
        from native_jev_judge import SystemOneJevClient

        async def requester(method, url, headers, *, params=None, json_body=None, timeout_seconds=45):
            if capture is not None:
                capture.update({"method": method, "url": url, "headers": headers, "json": json_body})
            return status, body or {}, {}

        return SystemOneJevClient(key, request_json=requester)

    def test_request_uses_systemone_choice_question_and_compact_evidence(self):
        capture = {}
        body = {
            "model": "typesafe/jev-1.13-20260917",
            "answers": {"category": {
                "type": "choice", "choice": "feature", "confidence": 0.97,
                "probabilities": {"feature": 0.97, "character": 0.03},
            }},
            "usage": {"input_tokens": 120, "output_tokens": 8, "cost": 0.0002},
        }
        client = self._client(body=body, capture=capture)
        evidence = _response(snippets=["サンプルタグ is a visual feature."])
        choice = run(client.classify("サンプルタグ", evidence))

        self.assertEqual(capture["url"], "https://openrouter.ai/api/v1/systemone")
        self.assertEqual(capture["headers"]["Authorization"], "Bearer or-key")
        self.assertEqual(capture["json"]["model"], "jev-1.13")
        self.assertEqual(capture["json"]["state"]["tag"], "サンプルタグ")
        self.assertEqual(capture["json"]["state"]["evidence"][0]["url"], "https://example.test/a")
        question = capture["json"]["questions"]["category"]
        self.assertEqual(question["type"], "choice")
        self.assertEqual(
            set(question["criteria"]),
            {"feature", "character", "copyright", "artist", "non_preference", "unresolved"},
        )
        self.assertEqual(choice.category, "feature")
        self.assertAlmostEqual(choice.confidence, 0.97)
        self.assertEqual(choice.model, "typesafe/jev-1.13-20260917")
        self.assertEqual(choice.usage["input_tokens"], 120)
        self.assertAlmostEqual(choice.usage["cost"], 0.0002)

    def test_unknown_choice_and_bad_confidence_are_protocol_errors(self):
        from native_jev_judge import JevProtocolError

        bad_choice = {"answers": {"category": {"type": "choice", "choice": "banana", "confidence": 1}}}
        with self.assertRaises(JevProtocolError):
            run(self._client(body=bad_choice).classify("t", _response()))
        bad_confidence = {"answers": {"category": {"type": "choice", "choice": "feature", "confidence": 7}}}
        with self.assertRaises(JevProtocolError):
            run(self._client(body=bad_confidence).classify("t", _response()))

    def test_rate_limit_and_outage_are_distinct_redacted_errors(self):
        from native_jev_judge import JevRateLimited, JevUnavailable

        with self.assertRaises(JevRateLimited) as limited:
            run(self._client(status=429, body={"error": "slow down"}).classify("t", _response()))
        self.assertNotIn("or-key", str(limited.exception))
        with self.assertRaises(JevUnavailable):
            run(self._client(status=503).classify("t", _response()))

    def test_evidence_text_is_bounded(self):
        capture = {}
        body = {"answers": {"category": {"type": "choice", "choice": "feature", "confidence": 0.99}}}
        client = self._client(body=body, capture=capture)
        long_snippet = "サンプルタグ" + "あ" * 5000
        run(client.classify("サンプルタグ", _response(snippets=[long_snippet])))
        text = capture["json"]["state"]["evidence"][0]["text"]
        self.assertLessEqual(len(text), 2500)


class PreviewCliTests(unittest.TestCase):
    def _module(self):
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "scripts" / "preview_native_jev.py"
        spec = importlib.util.spec_from_file_location("preview_native_jev_candidate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_defaults_are_read_only(self):
        module = self._module()
        args = module.parse_args(["--tags", "A, b"])
        self.assertFalse(args.apply)
        self.assertFalse(args.confirm_apply)
        self.assertEqual(args.limit, 5)
        self.assertEqual(module._split_tags(args.tags), ["a", "b"])

    def test_overrides_do_not_mutate_the_loaded_config(self):
        module = self._module()
        base = {"tag_classifier": {"grounded_judge": {"native_jev": {"enabled": True}}}}
        args = module.parse_args(["--tags", "a", "--audit-log", "/tmp/audit.jsonl"])
        effective = module._effective_config(args, base)
        self.assertEqual(
            effective["tag_classifier"]["grounded_judge"]["native_jev"]["audit_log_path"],
            "/tmp/audit.jsonl",
        )
        self.assertNotIn(
            "audit_log_path", base["tag_classifier"]["grounded_judge"]["native_jev"],
        )

    def test_preview_report_is_labelled_as_a_local_estimate(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        module = self._module()
        from native_jev_judge import NativeJevPolicy

        async def evaluate(tag, _translation=None):
            return {
                "tag": tag, "status": "accepted", "classification": "feature",
                "sources": [{"engine": "exa", "category": "feature", "confidence": 0.97}],
                "url_report": {"shared_urls": ["https://a.test/1"], "distinct_urls": 1},
                "search_trace": [{"engine": "exa", "outcome": "evidence"}],
                "usage": {"jev_calls": 1, "search_queries": 1},
            }

        runtime = SimpleNamespace(
            judge=SimpleNamespace(evaluate=evaluate),
            policy=NativeJevPolicy(min_confidence=0.95),
            router=SimpleNamespace(_engines={}),
        )
        with patch.object(module, "build_configured_native_jev_judge", return_value=runtime):
            report = run(module._preview(["タグ"], {"tag_classifier": {}}))

        self.assertTrue(report["dry_run"])
        self.assertEqual(report["quota_estimates"]["engines"], [])
        self.assertIn("not queried", report["quota_estimates"]["caveat"])
        self.assertEqual(report["policy"]["min_confidence"], 0.95)
        self.assertEqual(report["tags"][0]["status"], "accepted")
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("sk-", text)

    def test_apply_requires_the_second_confirmation(self):
        module = self._module()

        async def fake_run(_args):
            raise AssertionError("apply branch must not reach the network")

        args = module.parse_args(["--tags", "a", "--apply"])
        with patch.object(module, "_select_tags", fake_run):
            with self.assertRaises(SystemExit):
                run(module.run(args))


if __name__ == "__main__":
    unittest.main()


class StateStoreTests(unittest.TestCase):
    def test_engine_cooldown_survives_process_restart(self):
        from native_jev_judge import NativeJevStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            clock = [1000.0]
            first = NativeJevStateStore(path, clock=lambda: clock[0])
            first.cool("engine:exa", 60, "rate_limited")
            self.assertTrue(first.is_cooling("engine:exa"))
            second = NativeJevStateStore(path, clock=lambda: clock[0])
            self.assertTrue(second.is_cooling("engine:exa"))
            clock[0] += 61
            self.assertFalse(second.is_cooling("engine:exa"))

    def test_decision_cache_expires_and_round_trips(self):
        from native_jev_judge import NativeJevStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            clock = [0.0]
            store = NativeJevStateStore(path, clock=lambda: clock[0])
            store.put_decision("tag_a", {"classification": "feature", "confidence": 0.97}, ttl_seconds=10)
            self.assertEqual(store.get_decision("tag_a")["classification"], "feature")
            clock[0] = 11
            self.assertIsNone(store.get_decision("tag_a"))

    def test_state_file_never_contains_credentials(self):
        from native_jev_judge import NativeJevStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            store = NativeJevStateStore(path, clock=lambda: 0.0)
            store.cool("engine:exa", 10, "quota_exhausted")
            store.put_decision("tag_a", {"classification": "feature"}, ttl_seconds=60)
            self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))


class BudgetTests(unittest.TestCase):
    def test_request_budget_stops_after_the_window_allowance(self):
        from native_jev_judge import RequestBudget

        clock = [0.0]
        budget = RequestBudget(3, window_seconds=100, clock=lambda: clock[0])
        self.assertTrue(all(budget.try_consume() for _ in range(3)))
        self.assertFalse(budget.try_consume())
        clock[0] = 101
        self.assertTrue(budget.try_consume())

    def test_concurrency_budget_caps_in_flight_requests(self):
        from native_jev_judge import ConcurrencyBudget

        budget = ConcurrencyBudget(2)
        in_flight = 0
        peak = 0

        async def work():
            nonlocal in_flight, peak
            async with budget.slot():
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.01)
                in_flight -= 1

        async def main():
            await asyncio.gather(*(work() for _ in range(6)))

        run(main())
        self.assertLessEqual(peak, 2)
        self.assertEqual(budget.peak(), 2)


class RouterTests(unittest.TestCase):
    def _router(self, clients, state=None, required_sources=2):
        from native_jev_judge import NativeSearchRouter

        return NativeSearchRouter(
            clients, cooldowns=state, required_sources=required_sources,
            primary=("exa", "tavily"), fallback=("deepseek_search",),
        )

    def test_exa_and_tavily_run_first_and_deepseek_is_not_called(self):
        from native_jev_judge import SearchEvidence

        calls = []

        class Client:
            def __init__(self, name, snippet):
                self.name = name
                self.snippet = snippet

            async def search(self, query):
                calls.append(self.name)
                return _response(provider=self.name, pool_id=f"{self.name}-1", query=query,
                                 snippets=[self.snippet])

        router = self._router({
            "exa": Client("exa", "サンプルタグ は特徴です。"),
            "tavily": Client("tavily", "サンプルタグ は特徴。"),
            "deepseek_search": Client("deepseek_search", "サンプルタグ 特徴"),
        })
        result = run(router.gather("サンプルタグ", "q"))

        self.assertEqual(calls, ["exa", "tavily"])
        self.assertEqual([item.engine for item in result.evidences], ["exa", "tavily"])
        self.assertEqual(len(result.evidences[0].__dict__), len(result.evidences[0].__dict__))

    def test_deepseek_is_used_only_when_the_primary_pool_cannot_supply_two_sources(self):
        from native_jev_judge import NativeJevStateStore
        from search_grounded_judge import PoolQuotaExhausted

        calls = []

        class Exhausted:
            async def search(self, _query):
                calls.append("exa")
                raise PoolQuotaExhausted("done")

        class Tavily:
            async def search(self, query):
                calls.append("tavily")
                return _response(provider="tavily", pool_id="tavily-1", query=query,
                                 snippets=["サンプルタグ は特徴。"])

        class DeepSeek:
            async def search(self, query):
                calls.append("deepseek_search")
                return _response(provider="deepseek_search", pool_id="deepseek-1", query=query,
                                 snippets=["サンプルタグ の説明。"])

        with tempfile.TemporaryDirectory() as temp_dir:
            state = NativeJevStateStore(Path(temp_dir) / "state.json", clock=lambda: 0.0)
            router = self._router(
                {"exa": Exhausted(), "tavily": Tavily(), "deepseek_search": DeepSeek()},
                state=state,
            )
            result = run(router.gather("サンプルタグ", "q"))
            self.assertEqual(calls, ["exa", "tavily", "deepseek_search"])
            self.assertTrue(state.is_cooling("engine:exa"))
        self.assertEqual([item.engine for item in result.evidences], ["tavily", "deepseek_search"])
        outcomes = {item["engine"]: item["outcome"] for item in result.trace}
        self.assertEqual(outcomes["exa"], "quota_exhausted")

    def test_engine_in_cooldown_is_skipped_without_any_request(self):
        from native_jev_judge import NativeJevStateStore

        calls = []

        class Client:
            async def search(self, query):
                calls.append("tavily")
                return _response(provider="tavily", pool_id="tavily-1", query=query,
                                 snippets=["サンプルタグ は特徴。"])

        with tempfile.TemporaryDirectory() as temp_dir:
            state = NativeJevStateStore(Path(temp_dir) / "state.json", clock=lambda: 0.0)
            state.cool("engine:exa", 300, "rate_limited")
            router = self._router({"exa": Client(), "tavily": Client()}, state=state)
            result = run(router.gather("サンプルタグ", "q"))

        self.assertEqual(calls, ["tavily"])
        outcomes = {item["engine"]: item["outcome"] for item in result.trace}
        self.assertEqual(outcomes["exa"], "cooldown")

    def test_unmentioned_tag_marks_evidence_low_relevance(self):
        from native_jev_judge import SearchEvidence

        class Client:
            async def search(self, query):
                return _response(provider="exa", pool_id="exa-1", query=query,
                                 snippets=["An unrelated article about something else."])

        evidence = run(Client().search("q"))
        built = SearchEvidence.from_response(evidence, "サンプルタグ", require_tag_mention=True)
        self.assertFalse(built.tag_mentioned)

    def test_each_engine_pool_request_is_counted_as_one_search_query(self):
        class Client:
            async def search(self, query):
                return _response(provider="exa", pool_id="exa-1", query=query,
                                 snippets=["サンプルタグ は特徴。"])

        router = self._router({"exa": Client(), "tavily": Client()})
        result = run(router.gather("サンプルタグ", "q"))
        self.assertEqual(result.search_queries(), 2)


class AggregationTests(unittest.TestCase):
    def _sources(self, *entries):
        from native_jev_judge import JevChoice

        return [
            {"engine": engine, "pool_id": f"{engine}-1", "urls": tuple(urls),
             "choice": JevChoice(category=category, confidence=confidence,
                                 probabilities={}, model="typesafe/jev-1.13-20260917", usage={})}
            for engine, category, confidence, urls in entries
        ]

    def test_two_sources_above_threshold_activate_and_record_url_overlap(self):
        from native_jev_judge import evaluate_sources

        decision = evaluate_sources(
            "タグ",
            self._sources(
                ("exa", "feature", 0.98, ("https://a.test/1", "https://a.test/2")),
                ("tavily", "feature", 1.0, ("https://a.test/1", "https://b.test/3")),
            ),
            min_confidence=0.9, min_agreeing_sources=2,
        )
        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["classification"], "feature")
        self.assertEqual(decision["agreement"]["agreeing_sources"], ["exa", "tavily"])
        self.assertEqual(decision["url_report"]["shared_urls"], ["https://a.test/1"])
        self.assertEqual(decision["url_report"]["distinct_urls"], 3)
        self.assertEqual(decision["url_report"]["independence"], "partial_overlap")

    def test_identical_urls_are_not_independent_page_evidence(self):
        from native_jev_judge import evaluate_sources

        decision = evaluate_sources(
            "タグ",
            self._sources(
                ("exa", "feature", 0.99, ("https://a.test/1",)),
                ("tavily", "feature", 0.99, ("https://a.test/1",)),
            ),
            min_confidence=0.9, min_agreeing_sources=2,
        )
        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["url_report"]["independence"], "shared_pages")
        self.assertEqual(decision["corroboration_basis"], "same_pages")

    def test_single_source_is_never_enough(self):
        from native_jev_judge import evaluate_sources

        decision = evaluate_sources(
            "タグ", self._sources(("exa", "feature", 1.0, ("https://a.test/1",))),
            min_confidence=0.9, min_agreeing_sources=2,
        )
        self.assertEqual(decision["status"], "hold")
        self.assertEqual(decision["reason"], "insufficient_agreement")

    def test_confidence_equal_to_the_threshold_does_not_activate(self):
        from native_jev_judge import evaluate_sources

        decision = evaluate_sources(
            "タグ",
            self._sources(
                ("exa", "feature", 0.9, ("https://a.test/1",)),
                ("tavily", "feature", 0.9, ("https://b.test/2",)),
            ),
            min_confidence=0.9, min_agreeing_sources=2,
        )
        self.assertEqual(decision["status"], "hold")

    def test_an_opposite_explicit_classification_blocks_activation(self):
        from native_jev_judge import evaluate_sources

        decision = evaluate_sources(
            "タグ",
            self._sources(
                ("exa", "feature", 0.99, ("https://a.test/1",)),
                ("tavily", "character", 0.99, ("https://b.test/2",)),
                ("deepseek_search", "feature", 0.99, ("https://c.test/3",)),
            ),
            min_confidence=0.9, min_agreeing_sources=2,
        )
        self.assertEqual(decision["status"], "hold")
        self.assertEqual(decision["reason"], "conflicting_classification")

    def test_unresolved_from_a_third_source_does_not_block_activation(self):
        from native_jev_judge import evaluate_sources

        decision = evaluate_sources(
            "タグ",
            self._sources(
                ("exa", "feature", 0.98, ("https://a.test/1",)),
                ("tavily", "feature", 0.97, ("https://b.test/2",)),
                ("deepseek_search", "unresolved", 0.99, ("https://c.test/3",)),
            ),
            min_confidence=0.9, min_agreeing_sources=2,
        )
        self.assertEqual(decision["status"], "accepted")

    def test_two_sources_unresolved_never_activate(self):
        from native_jev_judge import evaluate_sources

        decision = evaluate_sources(
            "タグ",
            self._sources(
                ("exa", "unresolved", 0.99, ("https://a.test/1",)),
                ("tavily", "unresolved", 0.99, ("https://b.test/2",)),
            ),
            min_confidence=0.9, min_agreeing_sources=2,
        )
        self.assertEqual(decision["status"], "hold")
        self.assertEqual(decision["reason"], "model_unresolved")


class FakeRouter:
    def __init__(self, evidences, trace=None):
        self.evidences = evidences
        self.trace = trace or []
        self.calls = []

    async def gather(self, tag, query):
        from native_jev_judge import RouterResult

        self.calls.append((tag, query))
        return RouterResult(evidences=list(self.evidences), trace=list(self.trace))


class FakeJev:
    def __init__(self, choices=None, errors=None):
        from native_jev_judge import JevChoice

        self._choices = choices or [JevChoice("feature", 0.98, {}, "typesafe/jev-1.13-20260917", {})]
        self._errors = errors or []
        self.calls = []

    async def classify(self, tag, evidence):
        self.calls.append(evidence.engine)
        if self._errors:
            error = self._errors.pop(0)
            if error is not None:
                raise error
        index = min(len(self.calls) - 1, len(self._choices) - 1)
        return self._choices[index]


def _evidence(engine="exa", urls=("https://a.test/1",), mentioned=True, snippets=None):
    from native_jev_judge import SearchEvidence

    return SearchEvidence(
        engine=engine, pool_id=f"{engine}-1", query="q",
        sources=tuple({"url": url, "title": "t"} for url in urls),
        snippets=tuple(snippets or [f"サンプルタグ は特徴です。 ({engine})"]),
        tag_mentioned=mentioned, usage={},
    )


def _policy(**overrides):
    from native_jev_judge import NativeJevPolicy

    values = dict(
        min_confidence=0.9, min_agreeing_sources=2, unresolved_cooldown_seconds=3600,
        rate_limit_cooldown_seconds=600, decision_cache_ttl_seconds=3600,
        max_concurrent_requests=2, per_run_request_budget=10, max_retries=0,
    )
    values.update(overrides)
    return NativeJevPolicy(**values)


class JudgeBehaviourTests(unittest.TestCase):
    def _judge(self, evidences, jev=None, policy=None, clock=None, existing=None, state=None,
               auditor=None, trace=None):
        from native_jev_judge import NativeJevJudge, NativeJevStateStore

        temp_dir = tempfile.mkdtemp()
        state = state or NativeJevStateStore(Path(temp_dir) / "state.json", clock=clock or (lambda: 0.0))
        router = FakeRouter(evidences, trace=trace)
        judge = NativeJevJudge(
            router, jev or FakeJev(), policy or _policy(), state=state,
            clock=clock or (lambda: 0.0), existing_lookup=existing, auditor=auditor,
        )
        return judge, router

    def test_two_agreeing_sources_produce_an_accepted_decision(self):
        judge, router = self._judge([_evidence("exa"), _evidence("tavily")])
        result = run(judge.evaluate("サンプルタグ"))
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["classification"], "feature")
        self.assertEqual(result["usage"]["search_queries"], 2)
        self.assertEqual(result["usage"]["jev_calls"], 2)
        self.assertEqual(len(router.calls), 1)

    def test_repeat_call_within_cache_ttl_makes_no_provider_requests(self):
        judge, router = self._judge([_evidence("exa"), _evidence("tavily")])
        run(judge.evaluate("サンプルタグ"))
        calls = len(router.calls)
        second = run(judge.evaluate("サンプルタグ"))
        self.assertEqual(second["status"], "cached")
        self.assertEqual(len(router.calls), calls)

    def test_no_evidence_holds_the_tag_and_starts_a_cooldown(self):
        """A hold starts the short backoff *and* the durable pause.

        Contract note (jev-queue-fixes): the second call is now reported as
        ``paused`` instead of ``cooldown`` because the durable pause -- which
        has no expiry -- is checked first.  What this test pins is unchanged:
        the same tag is not re-evaluated and no request is spent.
        """
        jev = FakeJev()
        judge, router = self._judge([], jev=jev)
        first = run(judge.evaluate("サンプルタグ"))
        self.assertEqual(first["status"], "hold")
        self.assertEqual(first["reason"], "no_evidence")
        self.assertTrue(first["automatic_pause"])
        self.assertEqual(jev.calls, [])
        self.assertTrue(judge._state.is_cooling("tag:サンプルタグ"))
        second = run(judge.evaluate("サンプルタグ"))
        self.assertEqual(second["status"], "paused")
        self.assertEqual(second["reason"], "no_evidence")
        self.assertEqual(len(router.calls), 1)

    def test_cooldown_expiry_alone_never_retries_a_paused_tag(self):
        """Contract note (jev-queue-fixes): replaces the old "retried" test.

        The old assertion ``cooldown expires -> the tag is retried`` is exactly
        the loop this candidate removes: a completed attempt without a verdict
        must not come back on a timer.  Expiry alone now stays deferred, and
        only an explicit single-tag retry token buys one more execution.
        """
        from native_jev_judge import pause_retry_token

        clock = [0.0]
        judge, router = self._judge([], clock=lambda: clock[0])
        run(judge.evaluate("サンプルタグ"))
        clock[0] = 3601
        self.assertFalse(judge._state.is_cooling("tag:サンプルタグ"))
        again = run(judge.evaluate("サンプルタグ"))
        self.assertEqual(again["status"], "paused")
        self.assertEqual(len(router.calls), 1)

        with pause_retry_token(["サンプルタグ"]):
            explicit = run(judge.evaluate("サンプルタグ"))
        self.assertEqual(explicit["status"], "hold")
        self.assertEqual(len(router.calls), 2)
        # The token was consumed: a second implicit call is deferred again.
        self.assertEqual(run(judge.evaluate("サンプルタグ"))["status"], "paused")
        self.assertEqual(len(router.calls), 2)

    def test_unavailable_search_infrastructure_defers_instead_of_holding(self):
        judge, _router = self._judge(
            [], trace=[{"engine": "exa", "outcome": "quota_exhausted"},
                       {"engine": "tavily", "outcome": "cooldown"}],
        )
        result = run(judge.evaluate("サンプルタグ"))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "search_unavailable")

    def test_low_relevance_evidence_is_never_sent_to_the_model(self):
        jev = FakeJev()
        judge, _router = self._judge([_evidence("exa", mentioned=False)], jev=jev)
        result = run(judge.evaluate("サンプルタグ"))
        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["reason"], "low_relevance")
        self.assertEqual(jev.calls, [])

    def test_budget_exhaustion_defers_without_writing_a_classification(self):
        judge, _router = self._judge(
            [_evidence("exa"), _evidence("tavily")], policy=_policy(per_run_request_budget=1),
        )
        result = run(judge.evaluate("サンプルタグ"))
        self.assertIn(result["status"], {"deferred", "hold"})
        self.assertNotEqual(result["status"], "accepted")

    def test_jev_rate_limit_cools_the_model_and_defers(self):
        from native_jev_judge import JevChoice, JevRateLimited, NativeJevJudge, NativeJevStateStore

        jev = FakeJev(errors=[JevRateLimited("429"), JevChoice("feature", 0.99, {}, "m", {})])
        temp_dir = tempfile.mkdtemp()
        state = NativeJevStateStore(Path(temp_dir) / "state.json", clock=lambda: 0.0)
        judge = NativeJevJudge(
            FakeRouter([_evidence("exa"), _evidence("tavily")]), jev, _policy(),
            state=state, clock=lambda: 0.0,
        )
        result = run(judge.evaluate("サンプルタグ"))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "jev_rate_limited")
        self.assertTrue(state.is_cooling("engine:jev"))

    def test_invalid_model_answer_on_one_source_holds_the_decision(self):
        from native_jev_judge import JevProtocolError

        jev = FakeJev(errors=[JevProtocolError("bad"), None])
        judge, _router = self._judge([_evidence("exa"), _evidence("tavily")], jev=jev)
        result = run(judge.evaluate("サンプルタグ"))
        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["reason"], "insufficient_agreement")

    def test_existing_classification_short_circuits_every_provider(self):
        async def existing(_tag):
            return {"classification": "character", "source": "manual"}

        judge, router = self._judge([_evidence("exa"), _evidence("tavily")], existing=existing)
        result = run(judge.evaluate("サンプルタグ"))
        self.assertEqual(result["status"], "skipped_existing")
        self.assertEqual(result["classification"], "character")
        self.assertEqual(router.calls, [])

    def test_audit_record_excludes_credentials_headers_and_traces(self):
        from native_jev_judge import AuditLog

        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            audit = AuditLog(audit_path)
            judge, _router = self._judge([_evidence("exa"), _evidence("tavily")], auditor=audit)
            run(judge.evaluate("サンプルタグ"))
            text = audit_path.read_text(encoding="utf-8")
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertTrue(records)
        self.assertTrue(all("authorization" not in json.dumps(item).lower() for item in records))
        self.assertNotIn(SECRET, text)
        self.assertNotIn("sk-", text)

    def test_decision_records_engine_and_url_provenance(self):
        judge, _router = self._judge([
            _evidence("exa", urls=("https://a.test/1", "https://a.test/2")),
            _evidence("tavily", urls=("https://a.test/1",)),
        ])
        result = run(judge.evaluate("サンプルタグ"))
        self.assertEqual(result["url_report"]["shared_urls"], ["https://a.test/1"])
        self.assertEqual(result["sources"][0]["engine"], "exa")
        self.assertEqual(result["sources"][0]["confidence"], 0.98)
        self.assertEqual(result["sources"][0]["model"], "typesafe/jev-1.13-20260917")


class AuditTransportTests(unittest.TestCase):
    def test_search_request_and_response_are_audited_without_credentials(self):
        from native_jev_judge import AuditLog, AuditedSearchClient

        class Client:
            async def search(self, query):
                return _response(provider="exa", pool_id="exa-1", query=query,
                                 snippets=["サンプルタグ は特徴。"])

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "audit.jsonl"
            audited = AuditedSearchClient(Client(), "exa", AuditLog(path))
            run(audited.search('"サンプルタグ" pixiv'))
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([item["event"] for item in records], ["search_request", "search_response"])
        self.assertEqual(records[0]["query"], '"サンプルタグ" pixiv')
        self.assertEqual(records[1]["urls"], ["https://example.test/a"])
        self.assertTrue(records[1]["snippets"])
        text = json.dumps(records, ensure_ascii=False).lower()
        for forbidden in ("authorization", "x-api-key", "api_key", "traceback"):
            self.assertNotIn(forbidden, text)

    def test_search_error_audits_only_a_controlled_code(self):
        from native_jev_judge import AuditLog, AuditedSearchClient

        class Client:
            async def search(self, _query):
                raise RuntimeError("boom " + "x" * 1000)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "audit.jsonl"
            audited = AuditedSearchClient(Client(), "tavily", AuditLog(path))
            with self.assertRaises(RuntimeError):
                run(audited.search("q"))
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            record = records[-1]

        self.assertEqual([item["event"] for item in records], ["search_request", "search_error"])
        self.assertEqual(record["error_type"], "RuntimeError")
        self.assertEqual(record["error_code"], "unexpected_error")
        # Upstream error text is never persisted: it may echo the credential.
        self.assertNotIn("error", record)
        self.assertNotIn("boom", json.dumps(record))
        self.assertNotIn("Traceback", json.dumps(record))

    def test_jev_request_and_answer_are_audited(self):
        from native_jev_judge import AuditLog, AuditedJevClient, JevChoice

        class Client:
            endpoint = "https://openrouter.ai/api/v1/systemone"
            model = "jev-1.13"
            _max_evidence_chars = 2500

            async def classify(self, tag, evidence):
                return JevChoice("feature", 0.97, {"feature": 0.97},
                                 "typesafe/jev-1.13-20260917",
                                 {"input_tokens": 10, "output_tokens": 2, "cost": 0.001})

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "audit.jsonl"
            run(AuditedJevClient(Client(), AuditLog(path)).classify("タグ", _response()))
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([item["event"] for item in records], ["jev_request", "jev_response"])
        self.assertEqual(records[0]["request"]["model"], "jev-1.13")
        self.assertEqual(records[1]["category"], "feature")
        self.assertNotIn("Authorization", json.dumps(records))
        self.assertNotIn("Bearer", json.dumps(records))


class ProductionAdapterTests(unittest.TestCase):
    def _config(self, **native_overrides):
        native = {
            "enabled": True,
            "model": "jev-1.13",
            "jev_provider": "openrouter",
            "exa_providers": ["exa_1"],
            "tavily_providers": ["tavily_1"],
            "deepseek_search_provider": "deepseek",
            "min_confidence": 0.9,
            "min_agreeing_sources": 2,
            "quota_state_path": "quota.json",
            "state_path": "state.json",
        }
        native.update(native_overrides)
        return {
            "providers": {
                "exa_1": {"type": "exa_search", "api_key": "exa-key"},
                "tavily_1": {"type": "tavily_search", "api_key": "tavily-key"},
                "deepseek": {"type": "deepseek", "api_key": "llm-key"},
                "openrouter": {"type": "openai_compatible", "api_key": "or-key",
                               "base_url": "https://openrouter.ai/api/v1"},
            },
            "models": {"flash": {"provider": "deepseek", "model": "deepseek-v4-flash"}},
            "tag_classifier": {
                "judges": ["flash"],
                "grounded_judge": {
                    "backend": "native_jev",
                    "search_classifier_model": "flash",
                    "tavily_providers": ["tavily_1"],
                    "native_jev": native,
                },
            },
        }

    def test_builder_requires_an_explicit_enable_flag(self):
        import native_jev_judge as module

        config = self._config(enabled=False)
        with self.assertRaises(ValueError):
            module.build_configured_native_jev_judge(config)

    def test_builder_reports_the_missing_search_credential_without_values(self):
        import native_jev_judge as module

        config = self._config()
        config["providers"]["exa_1"]["api_key"] = ""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXA_API_KEY", None)
            with self.assertRaises(ValueError) as raised:
                module.build_configured_native_jev_judge(config)
        self.assertIn("exa", str(raised.exception).lower())

    def test_builder_derives_the_systemone_endpoint_from_the_provider_base_url(self):
        import native_jev_judge as module

        module._NATIVE_RUNTIMES.clear()
        runtime = module.build_configured_native_jev_judge(self._config())
        self.assertEqual(runtime.jev.endpoint, "https://openrouter.ai/api/v1/systemone")
        self.assertEqual(runtime.policy.model, "jev-1.13")

    def test_builder_uses_standard_environment_variables_as_a_fallback(self):
        import native_jev_judge as module

        module._NATIVE_RUNTIMES.clear()
        config = self._config()
        for name in ("exa_1", "tavily_1", "deepseek", "openrouter"):
            config["providers"][name]["api_key"] = ""
        with patch.dict(os.environ, {
            "EXA_API_KEY": "env-exa", "TAVILY_API_KEY": "env-tavily",
            "DEEPSEEK_API_KEY": "env-deepseek", "OPENROUTER_API_KEY": "env-openrouter",
        }):
            runtime = module.build_configured_native_jev_judge(config)
        self.assertTrue(runtime.jev is not None)

    def test_adapter_returns_a_complete_classification_record(self):
        import native_jev_judge as module

        module._NATIVE_RUNTIMES.clear()
        runtime = module.build_configured_native_jev_judge(self._config())
        runtime.judge._router = FakeRouter([_evidence("exa"), _evidence("tavily")])
        runtime.judge._jev = FakeJev()
        runtime.judge._state = module.NativeJevStateStore(
            Path(tempfile.mkdtemp()) / "state.json", clock=lambda: 0.0,
        )
        record = run(runtime.classify("サンプルタグ", "sample tag"))

        from grounded_judge import validate_ai_classification_record

        validated = validate_ai_classification_record(record, "サンプルタグ")
        self.assertEqual(validated["classification"], "feature")
        self.assertEqual(record["backend"], "native_jev")
        self.assertEqual(record["languages"], "ja")
        self.assertEqual(record["classifier_model"], "jev:jev-1.13")
        self.assertEqual(record["source_urls"], ["https://a.test/1"])
        self.assertEqual(record["usage"]["jev_calls"], 2)
        self.assertEqual(record["activation"]["corroboration_basis"], "same_pages")

    def test_adapter_maps_holds_to_value_error_and_deferrals_to_deferred_error(self):
        import native_jev_judge as module

        module._NATIVE_RUNTIMES.clear()
        runtime = module.build_configured_native_jev_judge(self._config())

        runtime.judge._router = FakeRouter([])
        runtime.judge._state = module.NativeJevStateStore(
            Path(tempfile.mkdtemp()) / "state.json", clock=lambda: 0.0,
        )
        with self.assertRaises(ValueError):
            run(runtime.classify("タグ", None))

        runtime.judge._router = FakeRouter(
            [], trace=[{"engine": "exa", "outcome": "quota_exhausted"}],
        )
        runtime.judge._state = module.NativeJevStateStore(
            Path(tempfile.mkdtemp()) / "state2.json", clock=lambda: 0.0,
        )
        with self.assertRaises(module.GroundedJudgeDeferredError):
            run(runtime.classify("タグ", None))

    def test_adapter_preserves_an_existing_explicit_classification(self):
        import native_jev_judge as module

        module._NATIVE_RUNTIMES.clear()
        runtime = module.build_configured_native_jev_judge(self._config())

        async def existing(_tag):
            return {"classification": "artist", "source": "manual"}

        runtime.judge._existing_lookup = existing
        with self.assertRaises(module.ExistingClassificationPreserved) as raised:
            run(runtime.classify("タグ", None))
        self.assertEqual(raised.exception.classification, "artist")


class ConfigNormalizationTests(unittest.TestCase):
    def _base(self, **native):
        return {
            "providers": {
                "pixiv": {"type": "pixiv", "refresh_token": "t"},
                "danbooru": {"type": "danbooru"},
                "deepseek": {"type": "deepseek", "api_key": "k"},
                "openrouter": {"type": "openai_compatible", "api_key": "or",
                               "base_url": "https://openrouter.ai/api/v1"},
                "exa_1": {"type": "exa_search", "api_key": "exa"},
                "tavily_1": {"type": "tavily_search", "api_key": "tavily"},
            },
            "models": {"flash": {"provider": "deepseek", "model": "deepseek-v4-flash"}},
            "tag_classifier": {
                "judges": ["flash"],
                "grounded_judge": {
                    "backend": "native_jev",
                    "search_classifier_model": "flash",
                    "tavily_providers": ["tavily_1"],
                    "native_jev": {"enabled": True, "exa_providers": ["exa_1", "missing"],
                                   "tavily_providers": [], **native},
                },
            },
        }

    def test_native_jev_backend_is_normalized_and_judges_are_cleared(self):
        import config

        cfg = config.normalize_config(self._base())
        grounded = cfg["tag_classifier"]["grounded_judge"]
        self.assertEqual(grounded["backend"], "native_jev")
        self.assertEqual(cfg["tag_classifier"]["judges"], [])
        self.assertEqual(grounded["exa_providers"], ["exa_1"])
        self.assertEqual(grounded["native_jev"]["exa_providers"], ["exa_1"])
        self.assertEqual(grounded["native_jev"]["min_agreeing_sources"], 2)
        self.assertAlmostEqual(grounded["native_jev"]["min_confidence"], 0.9)
        self.assertEqual(grounded["native_jev"]["model"], "jev-1.13")
        self.assertEqual(grounded["native_jev"]["jev_provider"], "openrouter")

    def test_default_configuration_keeps_the_legacy_backend(self):
        import config

        cfg = config.normalize_config({
            "providers": {"pixiv": {"type": "pixiv"}, "danbooru": {"type": "danbooru"}},
        })
        grounded = cfg["tag_classifier"]["grounded_judge"]
        self.assertEqual(grounded["backend"], "gemini")
        self.assertNotIn("native_jev", grounded)
        self.assertFalse(cfg["tag_classifier"]["maintenance"]["allow_automatic_external_search"])

    def test_exa_provider_type_is_accepted_by_the_graph(self):
        import config
        from provider_model_graph import ProviderModelGraph

        cfg = config.normalize_config(self._base())
        ProviderModelGraph.from_config(cfg).validate()

    def test_native_jev_graph_requires_at_least_one_search_provider(self):
        import config
        from provider_model_graph import ProviderModelGraph

        raw = self._base()
        raw["tag_classifier"]["grounded_judge"]["native_jev"]["exa_providers"] = []
        raw["tag_classifier"]["grounded_judge"]["tavily_providers"] = []
        cfg = config.normalize_config(raw)
        with self.assertRaises(ValueError):
            ProviderModelGraph.from_config(cfg).validate()

    def test_tag_classifier_treats_native_jev_as_maintenance_only(self):
        import config
        from tag_classifier import TagClassifier

        cfg = config.normalize_config(self._base())
        classifier = TagClassifier(cfg["tag_classifier"] | {
            "providers": cfg["providers"], "models": cfg["models"],
        })
        self.assertTrue(classifier.search_first_enabled)
        self.assertFalse(classifier.legacy_judge_enabled)


class DatabaseSafetyTests(unittest.TestCase):
    def _runtime(self, evidences, jev=None, *, existing=True):
        import native_jev_judge as module

        policy = _policy()
        state = module.NativeJevStateStore(
            Path(tempfile.mkdtemp()) / "state.json", clock=lambda: 0.0,
        )
        judge = module.NativeJevJudge(
            FakeRouter(evidences), jev or FakeJev(), policy, state=state, clock=lambda: 0.0,
            existing_lookup=module.database_existing_classification if existing else None,
        )
        return module.ConfiguredNativeJevJudge(
            judge, policy=policy, jev=SimpleNamespace(endpoint="x"), router=judge._router,
            state=state,
        )

    def test_existing_ai_classification_is_never_overwritten(self):
        import database as db
        from grounded_judge import ExistingClassificationPreserved
        from tag_categories import TAG_CATEGORY_CHARACTER

        run(db.init_db())
        run(db.activate_ai_tag_classification("existing_ai", "character", "human-approved", "ja"))

        runtime = self._runtime([_evidence("exa"), _evidence("tavily")])
        with self.assertRaises(ExistingClassificationPreserved) as raised:
            run(runtime.classify("existing_ai", None))
        self.assertEqual(raised.exception.classification, "character")
        cached = run(db.get_tag_classifications(["existing_ai"], ttl_days=3650))
        self.assertEqual(cached["existing_ai"]["classification"], TAG_CATEGORY_CHARACTER)

    def test_accepted_decision_writes_once_with_native_provenance(self):
        import database as db
        from classification_maintenance import classify_and_activate_tag
        from tag_categories import TAG_CATEGORY_FEATURE
        from unittest.mock import patch

        import native_jev_judge as module

        run(db.init_db())
        runtime = self._runtime([_evidence("exa"), _evidence("tavily")])
        config = {"tag_classifier": {"grounded_judge": {"backend": "native_jev"}}}
        with patch.object(module, "build_configured_native_jev_judge", return_value=runtime):
            result = run(classify_and_activate_tag("fresh_tag", config))
        self.assertEqual(result["status"], "accepted")

        cached = run(db.get_tag_classifications(["fresh_tag"], ttl_days=3650))
        self.assertEqual(cached["fresh_tag"]["classification"], TAG_CATEGORY_FEATURE)
        record = run(db.get_ai_tag_classification_record("fresh_tag"))
        provenance = record["grounding_provenance"]
        self.assertEqual(provenance["backend"], "native_jev")
        self.assertEqual(provenance["activation"]["agreeing_sources"], ["exa", "tavily"])
        self.assertTrue(provenance["activation"]["engine_agreement_is_not_independent_web_evidence"])

    def test_hold_keeps_the_tag_reviewable_without_a_classification(self):
        import database as db
        from classification_maintenance import classify_and_activate_tag
        from tag_categories import TAG_CATEGORY_UNRESOLVED
        from unittest.mock import patch

        import native_jev_judge as module

        run(db.init_db())
        runtime = self._runtime([_evidence("exa", mentioned=False)])
        config = {"tag_classifier": {"grounded_judge": {"backend": "native_jev"}}}
        with patch.object(module, "build_configured_native_jev_judge", return_value=runtime):
            with self.assertRaises(ValueError):
                run(classify_and_activate_tag("held_tag", config))

        cached = run(db.get_tag_classifications(["held_tag"], ttl_days=3650))
        self.assertNotIn("held_tag", cached)
        self.assertIsNone(run(db.get_ai_tag_classification_record("held_tag")))

        # The scheduled maintenance path is what keeps the tag in the human
        # review queue; it records unresolved rather than a classification.
        from classification_maintenance import run_scheduled_maintenance

        fresh_runtime = self._runtime([_evidence("exa", mentioned=False)])
        with patch.object(module, "build_configured_native_jev_judge", return_value=fresh_runtime):
            summary = run(run_scheduled_maintenance(
                ["held_tag"], config, classify=classify_and_activate_tag,
            ))
        self.assertEqual(summary["unresolved"], 1)
        self.assertEqual(run(db.get_tag_review_count()), 1)


class DisabledBackendTests(unittest.TestCase):
    def test_disabled_native_backend_defers_instead_of_marking_tags_unresolved(self):
        import asyncio

        from grounded_judge import GroundedJudgeDeferredError, classify_single_tag

        config = {
            "providers": {
                "exa_1": {"type": "exa_search", "api_key": "exa-key"},
                "openrouter": {"type": "openai_compatible", "api_key": "or-key",
                               "base_url": "https://openrouter.ai/api/v1"},
            },
            "models": {},
            "tag_classifier": {"grounded_judge": {
                "backend": "native_jev",
                "native_jev": {"enabled": False, "exa_providers": ["exa_1"]},
            }},
        }
        with self.assertRaises(GroundedJudgeDeferredError):
            asyncio.run(classify_single_tag("タグ", None, config))


class ExampleConfigTests(unittest.TestCase):
    def test_shipped_example_config_stays_on_the_legacy_backend(self):
        import yaml

        import config

        example = Path(__file__).resolve().parents[1] / "config.example.yaml"
        raw = yaml.safe_load(example.read_text(encoding="utf-8"))
        normalized = config.normalize_config(raw)
        grounded = normalized["tag_classifier"]["grounded_judge"]
        self.assertEqual(grounded["backend"], "search_first")
        self.assertNotIn("native_jev", grounded)
        self.assertFalse(normalized["tag_classifier"]["maintenance"]["allow_automatic_external_search"])
        config.ProviderModelGraph.from_config(normalized).validate()


class MaintenanceIntegrationTests(unittest.TestCase):
    def test_preserved_outcome_is_counted_without_touching_the_database(self):
        import database as db
        from classification_maintenance import run_scheduled_maintenance
        from grounded_judge import ExistingClassificationPreserved

        async def classify(tag, _config):
            raise ExistingClassificationPreserved(classification="character", source="manual")

        with patch.object(db, "mark_ai_tag_unresolved") as mark:
            summary = run(run_scheduled_maintenance(["タグ"], {}, classify=classify))
        self.assertEqual(summary["preserved"], 1)
        self.assertEqual(summary["unresolved"], 0)
        self.assertFalse(mark.called)

    def test_grounding_provenance_keeps_the_native_backend_and_activation_summary(self):
        from classification_maintenance import grounding_provenance

        provenance = grounding_provenance({
            "backend": "native_jev",
            "classifier_model": "jev:jev-1.13",
            "search_provider": "exa+tavily",
            "search_pool_id": "native_jev:exa_1+native_jev:tavily_1",
            "source_urls": ["https://a.test/1"],
            "evidence_excerpt": ["サンプルタグ は特徴。"],
            "search_trace": [{"provider": "exa", "outcome": "evidence"}],
            "activation": {"agreeing_sources": ["exa", "tavily"], "shared_urls": []},
            "usage": {"jev_calls": 2, "search_queries": 2},
        })
        self.assertEqual(provenance["backend"], "native_jev")
        self.assertEqual(provenance["activation"]["agreeing_sources"], ["exa", "tavily"])


class QuotaEstimateTests(unittest.TestCase):
    def test_quota_estimates_are_marked_local_and_not_upstream_verified(self):
        from native_jev_judge import estimate_quota_status

        statuses = estimate_quota_status(
            {"exa": [{"pool_id": "native_jev:exa_1", "request_limit": 1000, "requests_used": 12,
                      "exhausted": False}]},
            checked_at=0.0,
        )
        entry = statuses[0]
        self.assertEqual(entry["engine"], "exa")
        self.assertTrue(entry["estimated"])
        self.assertFalse(entry["upstream_verified"])
        self.assertEqual(entry["basis"], "local_monthly_counter")
        self.assertGreater(entry["remaining_estimate"], 0)


if __name__ == "__main__":
    unittest.main()

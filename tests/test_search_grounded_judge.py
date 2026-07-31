import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class SearchCredentialPoolTests(unittest.TestCase):
    def test_moves_to_the_next_pool_after_the_current_pool_hits_its_free_limit(self):
        from search_grounded_judge import SearchCredentialPool, SearchPoolConfig

        pool = SearchCredentialPool([
            SearchPoolConfig("brave-a", "secret-a", request_limit=1),
            SearchPoolConfig("brave-b", "secret-b", request_limit=1),
        ])

        async def request(selected):
            return selected.pool_id

        async def run():
            return [await pool.search(request), await pool.search(request)]

        self.assertEqual(asyncio.run(run()), ["brave-a", "brave-b"])

    def test_retries_stay_on_the_request_pool_before_a_quota_error_switches_pool(self):
        from search_grounded_judge import (
            PoolQuotaExhausted,
            SearchCredentialPool,
            SearchPoolConfig,
        )

        pool = SearchCredentialPool([
            SearchPoolConfig("brave-a", "secret-a"),
            SearchPoolConfig("brave-b", "secret-b"),
        ])
        seen = []

        async def request(selected):
            seen.append(selected.pool_id)
            if len(seen) == 1:
                raise TimeoutError("temporary")
            if len(seen) == 2:
                raise PoolQuotaExhausted("free allowance exhausted")
            return selected.pool_id

        self.assertEqual(asyncio.run(pool.search(request, retries=1)), "brave-b")
        self.assertEqual(seen, ["brave-a", "brave-a", "brave-b"])

    def test_no_evidence_does_not_try_another_pool_before_provider_fallback(self):
        from search_grounded_judge import SearchCredentialPool, SearchNoEvidence, SearchPoolConfig

        pool = SearchCredentialPool([
            SearchPoolConfig("brave-a", "secret-a"),
            SearchPoolConfig("brave-b", "secret-b"),
        ])
        seen = []

        async def request(selected):
            seen.append(selected.pool_id)
            raise SearchNoEvidence("empty")

        with self.assertRaises(SearchNoEvidence):
            asyncio.run(pool.search(request))
        self.assertEqual(seen, ["brave-a"])
        self.assertEqual(pool.status()[0]["requests_used"], 1)

    def test_persisted_monthly_usage_skips_a_pool_that_has_already_reached_its_limit(self):
        from search_grounded_judge import SearchCredentialPool, SearchPoolConfig

        pool = SearchCredentialPool([
            SearchPoolConfig("brave-a", "secret-a", request_limit=2),
            SearchPoolConfig("brave-b", "secret-b", request_limit=2),
        ], initial_requests_used={"brave-a": 2})

        async def request(selected):
            return selected.pool_id

        self.assertEqual(asyncio.run(pool.search(request)), "brave-b")

    def test_concurrent_requests_cannot_exceed_a_pool_free_limit(self):
        from search_grounded_judge import PoolQuotaExhausted, SearchCredentialPool, SearchPoolConfig

        pool = SearchCredentialPool([SearchPoolConfig("brave-a", "secret-a", request_limit=1)])
        entered = []
        release = asyncio.Event()

        async def request(selected):
            entered.append(selected.pool_id)
            await release.wait()
            return selected.pool_id

        async def run():
            tasks = [asyncio.create_task(pool.search(request)) for _ in range(3)]
            while len(entered) < 1:
                await asyncio.sleep(0)
            release.set()
            return await asyncio.gather(*tasks, return_exceptions=True)

        outcomes = asyncio.run(run())
        self.assertEqual(entered, ["brave-a"])
        self.assertEqual(outcomes.count("brave-a"), 1)
        self.assertEqual(sum(isinstance(item, PoolQuotaExhausted) for item in outcomes), 2)
        self.assertEqual(pool.status()[0]["requests_used"], 1)


class SearchErrorDiagnosticsTests(unittest.TestCase):
    def test_brave_422_keeps_the_redacted_api_validation_detail(self):
        from search_grounded_judge import SearchError, _raise_for_search_status

        with self.assertRaisesRegex(SearchError, r"HTTP 422.*maximum_number_of_tokens"):
            _raise_for_search_status(
                "Brave", 422,
                {"error": {"detail": "maximum_number_of_tokens must be at least 1024"}},
            )


class SearchGroundedJudgeTests(unittest.TestCase):
    def test_falls_back_from_brave_to_tavily_and_returns_a_valid_classification(self):
        from search_grounded_judge import (
            SearchGroundedJudge,
            SearchNoEvidence,
            SearchResponse,
        )

        class EmptyBrave:
            async def search(self, _query):
                raise SearchNoEvidence("no Brave evidence")

        class Tavily:
            async def search(self, query):
                return SearchResponse(
                    provider="tavily", pool_id="tavily-a", query=query,
                    sources=[{"url": "https://example.test/hiyuki", "title": "Hiyuki"}],
                    snippets=["Hiyuki is a playable character from Wuthering Waves."],
                )

        class Classifier:
            async def classify(self, tag, translation, evidence):
                self.received = (tag, translation, evidence.provider)
                return {
                    "tag": tag,
                    "classification": "character",
                    "explanation": "The supplied source identifies Hiyuki as a playable character.",
                    "languages": "en",
                }

        classifier = Classifier()
        judge = SearchGroundedJudge(EmptyBrave(), Tavily(), classifier)
        result = asyncio.run(judge.classify("hiyuki", "日雪"))

        self.assertEqual(result["classification"], "character")
        self.assertEqual(result["search_provider"], "tavily")
        self.assertEqual(result["usage"]["search_queries"], 2)
        self.assertEqual(result["search_trace"][0]["provider"], "brave")
        self.assertEqual(result["search_trace"][0]["outcome"], "no_evidence")
        self.assertEqual(classifier.received, ("hiyuki", "日雪", "tavily"))

    def test_returns_unresolved_when_no_provider_supplies_evidence(self):
        from search_grounded_judge import SearchGroundedJudge, SearchNoEvidence

        class Empty:
            async def search(self, _query):
                raise SearchNoEvidence("empty")

        class NeverCalled:
            async def classify(self, *_args):
                raise AssertionError("classifier must not run without evidence")

        result = asyncio.run(SearchGroundedJudge(Empty(), Empty(), NeverCalled()).classify("ambiguous", None))

        self.assertEqual(result["classification"], "unresolved")
        self.assertEqual(result["reason"], "no_search_evidence")

    def test_distinguishes_a_model_unresolved_decision_from_an_invalid_model_record(self):
        from search_grounded_judge import SearchGroundedJudge, SearchResponse

        class Evidence:
            async def search(self, query):
                return SearchResponse(
                    "brave", "brave-a", query, [{"url": "https://example.test"}],
                    ["The source does not resolve this ambiguous tag."],
                )

        class Classifier:
            async def classify(self, tag, _translation, _evidence):
                return {
                    "tag": tag, "classification": "unresolved",
                    "explanation": "The evidence is ambiguous.", "languages": "en",
                }

        result = asyncio.run(SearchGroundedJudge(Evidence(), Evidence(), Classifier()).classify("ambiguous", None))

        self.assertEqual(result["reason"], "model_unresolved")
        self.assertEqual(result["model_classification"], "unresolved")
        self.assertEqual(result["source_urls"], ["https://example.test"])
        self.assertEqual(result["evidence_excerpt"], ["The source does not resolve this ambiguous tag."])

    def test_builds_production_runtime_from_search_provider_pools_and_one_model(self):
        import search_grounded_judge as module

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            module, "AsyncOpenAI", return_value=SimpleNamespace(),
        ):
            module._PRODUCTION_RUNTIMES.clear()
            runtime = module.build_configured_search_grounded_judge({
                "providers": {
                    "brave_1": {"type": "brave_search", "api_key": "brave-key"},
                    "tavily_1": {"type": "tavily_search", "api_key": "tavily-key"},
                    "deepseek": {"type": "deepseek", "api_key": "llm-key", "base_url": "https://api.deepseek.com/v1"},
                },
                "models": {"flash": {"provider": "deepseek", "model": "deepseek-v4-flash", "capabilities": ["llm"]}},
                "tag_classifier": {
                    "judges": ["flash"],
                    "grounded_judge": {
                        "backend": "search_first", "brave_providers": ["brave_1"],
                        "search_classifier_model": "flash",
                        "tavily_providers": ["tavily_1"],
                        "quota_state_path": str(Path(temp_dir) / "quota.json"),
                    },
                },
            })

        self.assertIsInstance(runtime, module.ConfiguredSearchGroundedJudge)

    def test_production_adapter_preserves_grounding_diagnostics_and_model(self):
        import search_grounded_judge as module

        judge = SimpleNamespace(classify=lambda _tag, _translation: None)

        async def classify(_tag, _translation):
            return {
                "tag": "white_hair", "classification": "feature",
                "explanation": "A visual trait.", "languages": "en",
                "search_provider": "brave", "search_pool_id": "brave-a",
                "source_urls": ["https://example.test/white-hair"],
                "evidence_excerpt": ["White hair is a visual trait."],
                "search_trace": [{"provider": "brave", "outcome": "success"}],
                "usage": {"input": 10, "total": 10},
            }

        judge.classify = classify
        adapter = module.ConfiguredSearchGroundedJudge(judge, model="deepseek-v4-flash")
        result = asyncio.run(adapter.classify("white_hair", None))

        self.assertEqual(result["classifier_model"], "deepseek-v4-flash")
        self.assertEqual(result["search_provider"], "brave")
        self.assertEqual(result["source_urls"], ["https://example.test/white-hair"])


class SearchProviderAdapterTests(unittest.TestCase):
    def test_brave_llm_context_uses_one_japanese_search_and_returns_extracted_snippets(self):
        from search_grounded_judge import BraveLLMContextClient, SearchCredentialPool, SearchPoolConfig

        seen = {}

        async def request_json(method, url, headers, *, params=None, json_body=None, timeout_seconds=30):
            seen.update(method=method, url=url, headers=headers, params=params, json_body=json_body)
            return 200, {
                "grounding": {"generic": [{
                    "url": "https://example.test/hiyuki",
                    "title": "Hiyuki", "snippets": ["Hiyuki is a playable character."],
                }]},
            }, {"X-RateLimit-Remaining": "1,999"}

        client = BraveLLMContextClient(
            SearchCredentialPool([SearchPoolConfig("brave-a", "secret")]), request_json,
        )
        result = asyncio.run(client.search('"hiyuki"'))

        self.assertEqual(result.provider, "brave")
        self.assertEqual(result.snippets, ["Hiyuki is a playable character."])
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["params"]["search_lang"], "jp")
        self.assertEqual(seen["params"]["maximum_number_of_urls"], 5)
        self.assertNotIn("secret", str(result))

    def test_tavily_uses_advanced_search_and_reports_its_credit_usage(self):
        from search_grounded_judge import SearchCredentialPool, SearchPoolConfig, TavilySearchClient

        seen = {}

        async def request_json(method, url, headers, *, params=None, json_body=None, timeout_seconds=30):
            seen.update(method=method, url=url, headers=headers, params=params, json_body=json_body)
            return 200, {
                "results": [{"url": "https://example.test/tag", "title": "Tag", "content": "A visual trait."}],
                "usage": {"credits": 2},
            }, {}

        client = TavilySearchClient(
            SearchCredentialPool([SearchPoolConfig("tavily-a", "secret")]), request_json,
        )
        result = asyncio.run(client.search('"trait"'))

        self.assertEqual(result.provider, "tavily")
        self.assertEqual(result.usage, {"search_credits": 2})
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["json_body"]["search_depth"], "advanced")
        self.assertEqual(seen["json_body"]["max_results"], 5)

    def test_deepseek_flash_requests_json_classification_from_only_supplied_evidence(self):
        from search_grounded_judge import DeepSeekFlashClassifier, SearchResponse

        seen = {}

        class Completions:
            async def create(self, **kwargs):
                seen.update(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='''{
                    "tag": "hiyuki", "classification": "character",
                    "explanation": "The supplied snippet identifies a playable character.", "languages": "en"
                }'''))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        classifier = DeepSeekFlashClassifier("secret", client=client)
        evidence = SearchResponse(
            "brave", "brave-a", "query", [{"url": "https://example.test"}],
            ["Hiyuki is a playable character."],
        )
        result = asyncio.run(classifier.classify("hiyuki", "日雪", evidence))

        self.assertEqual(result["classification"], "character")
        self.assertEqual(seen["model"], "deepseek-v4-flash")
        self.assertEqual(seen["temperature"], 0.0)
        self.assertEqual(seen["response_format"], {"type": "json_object"})
        self.assertIn("Do not use knowledge outside the supplied evidence", seen["messages"][0]["content"])
        self.assertIn("languages must be exactly one primary ISO language code string", seen["messages"][0]["content"])
        self.assertIn("non_preference: platform, rating, event, or metadata", seen["messages"][0]["content"])


class ShadowEvaluationTests(unittest.TestCase):
    def test_shadow_report_compares_expected_categories_without_persisting_classifications(self):
        from search_grounded_judge import run_shadow_evaluation

        class Judge:
            async def classify(self, tag, translation):
                if tag == "ambiguous":
                    return {
                        "tag": tag, "classification": "unresolved", "reason": "model_unresolved",
                        "error": "safe diagnostic", "model_response_excerpt": '{"classification":"unresolved"}',
                        "evidence_excerpt": ["ambiguous evidence"], "source_urls": ["https://example.test"],
                    }
                return {"tag": tag, "classification": "feature", "search_pool_id": "brave-a"}

        report = asyncio.run(run_shadow_evaluation([
            {"tag": "white_hair", "translation": "white hair", "expected_classification": "feature"},
            {"tag": "ambiguous", "expected_classification": "character"},
        ], Judge(), pool_statuses=lambda: [{"pool_id": "brave-a", "requests_used": 2}]))

        self.assertEqual(report["total"], 2)
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["unresolved"], 1)
        self.assertEqual(report["agreement_rate"], 0.5)
        self.assertEqual(report["items"][1]["error"], "safe diagnostic")
        self.assertEqual(report["items"][1]["evidence_excerpt"], ["ambiguous evidence"])
        self.assertNotIn("api_key", str(report))

    def test_shadow_report_separately_measures_priority_tags(self):
        from search_grounded_judge import run_shadow_evaluation

        class Judge:
            async def classify(self, tag, translation):
                classification = "feature" if tag == "important" else "unresolved"
                return {"tag": tag, "classification": classification}

        report = asyncio.run(run_shadow_evaluation([
            {"tag": "important", "expected_classification": "feature", "priority": True, "profile_weight": 6.5},
            {"tag": "long_tail", "expected_classification": "character", "priority": False, "profile_weight": 0},
        ], Judge()))

        self.assertEqual(report["priority_metrics"], {
            "total": 1,
            "with_expected_classification": 1,
            "matched": 1,
            "unresolved": 0,
            "agreement_rate": 1.0,
            "unresolved_rate": 0.0,
        })
        self.assertEqual(report["items"][0]["profile_weight"], 6.5)
        self.assertTrue(report["items"][0]["priority"])


class ShadowSampleExportTests(unittest.TestCase):
    def test_export_marks_only_top_weighted_manual_labels_as_priority(self):
        from scripts.export_tag_shadow_manual import export_manual_labels

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "pixiv_xp.db"
            output_path = root / "input.jsonl"
            with sqlite3.connect(db_path) as db:
                db.executescript("""
                    CREATE TABLE tag_classification_cache (
                        normalized_tag TEXT PRIMARY KEY, classification TEXT, source TEXT
                    );
                    CREATE TABLE xp_profile (tag TEXT PRIMARY KEY, weight REAL);
                    CREATE TABLE tag_translations (name TEXT PRIMARY KEY, translated_name TEXT);
                """)
                db.executemany(
                    "INSERT INTO tag_classification_cache VALUES (?, ?, ?)",
                    [("high", "feature", "manual"), ("low", "character", "manual"), ("none", "copyright", "manual"), ("negative", "feature", "manual"), ("ai", "feature", "ai")],
                )
                db.executemany("INSERT INTO xp_profile VALUES (?, ?)", [("high", 5.0), ("low", 1.0), ("negative", -7.0)])
                db.execute("INSERT INTO tag_translations VALUES (?, ?)", ("high", "高"))

            count = export_manual_labels(db_path, output_path, priority_limit=1)
            rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(count, 4)
        self.assertEqual([row["tag"] for row in rows], ["negative", "high", "low", "none"])
        self.assertEqual(rows[0], {
            "tag": "negative", "translation": None, "expected_classification": "feature",
            "profile_weight": -7.0, "priority": True,
        })
        self.assertFalse(rows[1]["priority"])
        self.assertEqual(rows[3]["profile_weight"], 0.0)


class MonthlyQuotaUsageLedgerTests(unittest.TestCase):
    def test_persists_redacted_pool_usage_for_the_current_month(self):
        from scripts.run_search_judge_shadow import MonthlyQuotaUsageLedger

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "quota-state.json"
            ledger = MonthlyQuotaUsageLedger(path, month="2026-07")
            ledger.save([
                {"pool_id": "brave-1", "requests_used": 17},
                {"pool_id": "tavily-1", "requests_used": 2},
            ])
            restored = MonthlyQuotaUsageLedger(path, month="2026-07")

            self.assertEqual(restored.initial_usage(), {"brave-1": 17, "tavily-1": 2})
            self.assertNotIn("api_key", path.read_text(encoding="utf-8"))

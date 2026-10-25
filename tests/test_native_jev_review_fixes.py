"""Round-2 regression tests for the independent review findings.

Every test here targets one reproducible review finding:

* B1  saving unrelated settings must keep ``backend: native_jev`` and its subtree
* B2  activation/hold must not overwrite a decision that landed while waiting
* B3  budget/cooldown/unconfigured results defer instead of holding + cooling
* B4  the default preview is read-only and never creates or migrates a database
* C1  the confidence floor is 0.9 (strictly greater) and cache is policy-bound
* C3  an upstream reflection of any-format credential never reaches the audit log
* C2/C4 one cross-process task lock serialises a native batch or CLI request
* D1  a third-priority engine is consulted after classification, never to
      outvote two explicit opposing categories
* D2  primary engine order follows available quota/cooldown, not a fixed engine

The tests use the real SQLite schema through ``database`` and mock only the
network boundary; no production database, config or credential is touched.
"""

import asyncio
import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run(coro):
    return asyncio.run(coro)


def _response(engine="exa", urls=("https://example.test/a",),
              snippets=("サンプルタグ は特徴です。",)):
    from search_grounded_judge import SearchResponse

    return SearchResponse(
        provider=engine, pool_id=f"{engine}-1", query="q",
        sources=[{"url": url, "title": "t"} for url in urls],
        snippets=list(snippets), usage={},
    )


def _evidence(engine="exa", mentioned=True, urls=("https://example.test/a",)):
    from native_jev_judge import SearchEvidence

    return SearchEvidence(
        engine=engine, pool_id=f"{engine}-1", query="q",
        sources=tuple({"url": url, "title": "t"} for url in urls),
        snippets=("サンプルタグ は特徴です。",), tag_mentioned=mentioned, usage={},
    )


class FakeJev:
    def __init__(self, choices=None):
        from native_jev_judge import JevChoice

        self._choices = list(
            choices or [JevChoice("feature", 0.98, {}, "fake/jev", {})]
        )
        self.calls = []

    async def classify(self, tag, evidence):
        self.calls.append(evidence.engine)
        index = min(len(self.calls) - 1, len(self._choices) - 1)
        return self._choices[index]


class FakeRouter:
    """Router double that can also serve a third-priority engine."""

    def __init__(self, evidences, trace=None, fallback=None):
        self.evidences = list(evidences)
        self.trace = list(trace or [])
        self.fallback = fallback
        self.calls = []
        self.third_calls = []

    async def gather(self, tag, query):
        from native_jev_judge import RouterResult

        self.calls.append((tag, query))
        return RouterResult(evidences=list(self.evidences), trace=list(self.trace))

    def fallback_engines(self):
        return ("deepseek_search",) if self.fallback is not None else ()

    async def gather_one(self, tag, query, engine, trace=None):
        from native_jev_judge import RouterResult

        self.third_calls.append(engine)
        target = trace if trace is not None else []
        target.append({"engine": engine, "outcome": "evidence"})
        return RouterResult(
            evidences=[self.fallback] if self.fallback is not None else [], trace=target,
        )


def _policy(**overrides):
    from native_jev_judge import NativeJevPolicy

    values = dict(min_confidence=0.9, min_agreeing_sources=2)
    values.update(overrides)
    return NativeJevPolicy(**values)


class _IsolatedDatabase(unittest.TestCase):
    """Pin ``database.DB_PATH`` at a throw-away file with the real schema."""

    def setUp(self):
        import database
        import native_jev_judge

        self._directory = tempfile.TemporaryDirectory()
        self._previous_path = database.DB_PATH
        database.DB_PATH = Path(self._directory.name) / "review.sqlite3"
        database._init_db_sync()
        # The builder caches runtimes per signature; never reuse across tests.
        native_jev_judge._NATIVE_RUNTIMES.clear()

    def tearDown(self):
        import database
        import native_jev_judge

        native_jev_judge._NATIVE_RUNTIMES.clear()
        database.DB_PATH = self._previous_path
        self._directory.cleanup()

    def _judge(self, router, jev=None, policy=None, state=None, auditor=None):
        from native_jev_judge import NativeJevJudge, NativeJevStateStore

        temp = Path(self._directory.name)
        state = state or NativeJevStateStore(temp / "state.json", clock=lambda: 0.0)
        return NativeJevJudge(
            router, jev or FakeJev(), policy or _policy(), state=state,
            clock=lambda: 0.0, auditor=auditor,
        )


def _runtime(judge, policy=None):
    from native_jev_judge import ConfiguredNativeJevJudge

    return ConfiguredNativeJevJudge(
        judge, policy=policy or judge._policy, jev=judge._jev,
        router=judge._router, state=judge._state,
    )


class ActivationConcurrencyTests(_IsolatedDatabase):
    """B2: a decision that lands during the wait must survive."""

    def _racing_runtime(self, config_extra=None):
        import database as db
        import native_jev_judge as n

        gate = asyncio.Event()
        seen = {}

        class RacingJev:
            async def classify(self, tag, evidence):
                # A separate actor (human or another AI run) resolves the tag
                # while this operation is inside its model call.
                await db.activate_ai_tag_classification(
                    tag, "character", "concurrent prior accepted decision", "ja",
                )
                seen["row"] = await db.get_ai_tag_classification_record(tag)
                await asyncio.wait_for(gate.wait(), timeout=5)
                return n.JevChoice("feature", 0.98, {}, "fake/jev", {})

        judge = n.NativeJevJudge(
            FakeRouter([_evidence("exa"), _evidence("tavily")]), RacingJev(), _policy(),
            state=n.NativeJevStateStore(Path(self._directory.name) / "race.json", clock=lambda: 0.0),
            existing_lookup=n.database_existing_classification,
        )
        return _runtime(judge), gate, seen

    def test_activation_keeps_a_decision_that_landed_while_waiting(self):
        import database as db
        import native_jev_judge as module
        import classification_maintenance as cm

        runtime, gate, seen = self._racing_runtime()
        config = {"tag_classifier": {"grounded_judge": {"backend": "native_jev"}}}

        async def scenario():
            with patch.object(module, "build_configured_native_jev_judge", return_value=runtime):
                task = asyncio.create_task(cm.classify_and_activate_tag("race-accept", config))
                for _ in range(500):
                    if "row" in seen:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(seen["row"]["classification"], "character")
                self.assertEqual(seen["row"]["explanation"], "concurrent prior accepted decision")
                gate.set()
                return await task

        result = run(scenario())
        row = run(db.get_ai_tag_classification_record("race-accept"))
        self.assertEqual(row["classification"], "character")
        self.assertEqual(row["explanation"], "concurrent prior accepted decision")
        self.assertNotEqual(result["status"], "accepted")

    def test_legacy_backend_keeps_last_writer_wins_semantics(self):
        """The new atomic guard is Native-Jev-only; default behaviour is unchanged."""
        import database as db

        async def scenario():
            await db.activate_ai_tag_classification("legacy_tag", "character", "first", "ja")
            overwritten = await db.activate_ai_tag_classification(
                "legacy_tag", "feature", "second", "ja",
            )
            row_after_legacy = await db.get_ai_tag_classification_record("legacy_tag")
            refused = await db.activate_ai_tag_classification(
                "legacy_tag", "copyright", "third", "ja", preserve_resolved=True,
            )
            row_after_native = await db.get_ai_tag_classification_record("legacy_tag")
            marked = await db.mark_ai_tag_unresolved("legacy_tag", preserve_resolved=True)
            row_after_hold = await db.get_ai_tag_classification_record("legacy_tag")
            cached = await db.get_tag_classifications(["legacy_tag"], ttl_days=3650)
            return (overwritten, row_after_legacy, refused, row_after_native,
                    marked, row_after_hold, cached)

        overwritten, row_after_legacy, refused, row_after_native, marked, row_after_hold, cached = run(
            scenario()
        )
        self.assertTrue(overwritten)
        self.assertEqual(row_after_legacy["classification"], "feature")
        # Native Jev opt-in: neither activation nor hold may replace a resolved row.
        self.assertFalse(refused)
        self.assertEqual(row_after_native["classification"], "feature")
        self.assertFalse(marked)
        self.assertEqual(row_after_hold["classification"], "feature")
        self.assertEqual(cached["legacy_tag"]["classification"], "feature")

    def test_native_path_passes_the_atomic_option_to_the_database(self):
        import classification_maintenance as maintenance
        from unittest.mock import AsyncMock

        record = {
            "tag": "native_tag", "classification": "feature",
            "explanation": "trait", "languages": "en",
        }

        async def scenario(config):
            with patch.object(
                maintenance.db, "get_translated_tag", new=AsyncMock(return_value=None)
            ), patch.object(
                maintenance, "classify_single_tag", new=AsyncMock(return_value=record)
            ), patch.object(
                maintenance.db, "activate_ai_tag_classification",
                new=AsyncMock(return_value=True),
            ) as activate:
                await maintenance.classify_and_activate_tag("native_tag", config)
            return activate.await_args.kwargs

        native_kwargs = run(scenario(
            {"tag_classifier": {"grounded_judge": {"backend": "native_jev"}}}
        ))
        legacy_kwargs = run(scenario(
            {"tag_classifier": {"grounded_judge": {"backend": "search_first"}}}
        ))
        self.assertTrue(native_kwargs["preserve_resolved"])
        self.assertFalse(legacy_kwargs["preserve_resolved"])

    def test_holding_never_downgrades_a_concurrent_decision(self):
        import database as db
        import native_jev_judge as module
        import classification_maintenance as cm
        from native_jev_judge import NativeJevJudge, NativeJevStateStore, RouterResult

        class RacingRouter:
            async def gather(self, tag, query):
                await db.activate_ai_tag_classification(
                    tag, "character", "concurrent prior accepted decision", "ja",
                )
                return RouterResult(evidences=[_evidence("exa", mentioned=False)], trace=[])

        judge = NativeJevJudge(
            RacingRouter(), FakeJev(), _policy(),
            state=NativeJevStateStore(Path(self._directory.name) / "hold.json", clock=lambda: 0.0),
            clock=lambda: 0.0,
        )
        runtime = _runtime(judge)
        config = {"tag_classifier": {"grounded_judge": {"backend": "native_jev"}}}
        with patch.object(module, "build_configured_native_jev_judge", return_value=runtime):
            summary = run(cm.run_scheduled_maintenance(["race-hold"], config))

        cached = run(db.get_tag_classifications(["race-hold"], ttl_days=3650))
        self.assertEqual(cached["race-hold"]["classification"], "character")
        self.assertEqual(summary["unresolved"], 0)
        self.assertEqual(summary["preserved"], 1)
        self.assertEqual(summary["items"][0]["status"], "preserved")


class ReadOnlyPreviewTests(_IsolatedDatabase):
    """B4: the default preview only reads, and aborts instead of creating."""

    def _module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "review_fix_preview", ROOT / "scripts" / "preview_native_jev.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _seed(self):
        import aiosqlite
        import database as db

        async def seed():
            await db.init_db()
            async with aiosqlite.connect(db.DB_PATH) as connection:
                await connection.execute(
                    "INSERT OR REPLACE INTO xp_profile (tag, weight) VALUES (?, ?)",
                    ("サンプルタグ", 2.0),
                )
                await connection.execute(
                    "INSERT OR REPLACE INTO xp_profile (tag, weight) VALUES (?, ?)",
                    ("低重みタグ", 0.2),
                )
                await connection.commit()

        run(seed())

    def _digest(self):
        import database as db

        return hashlib.sha256(Path(db.DB_PATH).read_bytes()).hexdigest()

    def test_default_preview_reads_the_queue_without_writing(self):
        self._seed()
        module = self._module()
        before = self._digest()
        selected = run(module._select_tags(module.parse_args([])))
        self.assertEqual(selected, ["サンプルタグ"])
        self.assertEqual(self._digest(), before, "默认预览不得改动数据库文件")

    def test_preview_aborts_when_the_database_is_absent(self):
        import database as db

        missing = Path(self._directory.name) / "absent.sqlite3"
        db.DB_PATH = missing
        module = self._module()
        with self.assertRaises(SystemExit):
            run(module._select_tags(module.parse_args([])))
        self.assertFalse(missing.exists(), "预览不得创建数据库")

    def test_preview_aborts_on_a_schema_mismatch_without_migrating(self):
        import database as db

        broken = Path(self._directory.name) / "broken.sqlite3"
        with sqlite3.connect(broken) as connection:
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
        db.DB_PATH = broken
        before = hashlib.sha256(broken.read_bytes()).hexdigest()
        module = self._module()
        with self.assertRaises(SystemExit):
            run(module._select_tags(module.parse_args([])))
        self.assertEqual(hashlib.sha256(broken.read_bytes()).hexdigest(), before)
        with sqlite3.connect(broken) as connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(tables, {"unrelated"})

    def test_explicit_tags_never_open_the_database(self):
        import database as db

        db.DB_PATH = Path(self._directory.name) / "never-opened.sqlite3"
        module = self._module()
        self.assertEqual(
            run(module._select_tags(module.parse_args(["--tags", "タグA,タグB"]))),
            ["タグa", "タグb"],
        )
        self.assertFalse(Path(db.DB_PATH).exists())


class ThresholdAndCacheTests(_IsolatedDatabase):
    """C1: the 0.9 floor is enforced and a cache belongs to one policy."""

    def test_policy_and_config_cannot_lower_the_threshold_below_the_floor(self):
        import config as config_module
        from native_jev_judge import NativeJevPolicy

        policy = NativeJevPolicy.from_mapping({"min_confidence": 0.5})
        self.assertEqual(policy.min_confidence, 0.9)

        normalized = config_module.normalize_config({
            "tag_classifier": {"grounded_judge": {
                "backend": "native_jev",
                "native_jev": {"enabled": True, "min_confidence": 0.5, "exa_providers": []},
            }},
        })
        self.assertEqual(
            normalized["tag_classifier"]["grounded_judge"]["native_jev"]["min_confidence"], 0.9,
        )

    def test_activation_needs_confidence_strictly_above_the_threshold(self):
        from native_jev_judge import JevChoice, evaluate_sources

        def sources(confidence):
            return [
                {"engine": engine, "choice": JevChoice("feature", confidence, {}, "m")}
                for engine in ("exa", "tavily")
            ]

        self.assertEqual(
            evaluate_sources("t", sources(0.89), min_confidence=0.9)["status"], "hold",
        )
        self.assertEqual(
            evaluate_sources("t", sources(0.9), min_confidence=0.9)["status"], "hold",
        )
        self.assertEqual(
            evaluate_sources("t", sources(0.91), min_confidence=0.9)["status"], "accepted",
        )

    def test_a_decision_cached_under_a_lower_threshold_is_not_reused(self):
        from native_jev_judge import NativeJevStateStore

        state = NativeJevStateStore(Path(self._directory.name) / "legacy_cache.json")
        state.put_decision(
            "キャッシュタグ",
            {
                "tag": "キャッシュタグ", "classification": "feature", "confidence": 0.55,
                "agreement": {
                    "category": "feature", "agreeing_sources": ["exa", "tavily"],
                    "agreeing_confidences": {"exa": 0.55, "tavily": 0.6},
                    "min_confidence": 0.5,
                },
            },
            ttl_seconds=3600,
        )
        router = FakeRouter([_evidence("exa"), _evidence("tavily")])
        judge = self._judge(router, state=state)
        result = run(judge.evaluate("キャッシュタグ"))
        self.assertNotEqual(result["status"], "cached")
        self.assertTrue(router.calls, "策略变化后必须重新检索")

    def test_a_decision_cached_by_the_current_policy_is_still_reused(self):
        router = FakeRouter([_evidence("exa"), _evidence("tavily")])
        judge = self._judge(router)
        first = run(judge.evaluate("再 Cache タグ"))
        calls = len(router.calls)
        second = run(judge.evaluate("再 Cache タグ"))
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "cached")
        self.assertEqual(len(router.calls), calls)

    def test_cache_written_by_a_stronger_policy_survives_a_weaker_reader(self):
        """Revalidation refuses stored confidences that fail the current gate."""
        from native_jev_judge import NativeJevStateStore

        state = NativeJevStateStore(Path(self._directory.name) / "strong_cache.json")
        judge = self._judge(FakeRouter([_evidence("exa"), _evidence("tavily")]), state=state)
        run(judge.evaluate("強いキャッシュ"))
        fingerprint = judge._policy.cache_fingerprint()
        stored = state.get_decision("強いキャッシュ")
        self.assertIsNotNone(stored)
        self.assertEqual(
            state.get_decision("強いキャッシュ", policy_fingerprint=fingerprint),
            state.get_decision("強いキャッシュ"),
        )
        self.assertIsNone(
            state.get_decision("強いキャッシュ", policy_fingerprint="another-policy"),
        )


class ReflectionLeakTests(_IsolatedDatabase):
    """C3: an upstream echo of any-format credential never reaches an artefact."""

    KEY = "synthetic-review-exa-token-123456"  # public test value, not a credential

    def _leaking_client(self):
        from native_jev_judge import ExaSearchClient
        from search_grounded_judge import SearchCredentialPool, SearchPoolConfig

        async def requester(*_args, **_kwargs):
            return 400, {"api_key": self.KEY, "message": "invalid key"}, {}

        pool = SearchCredentialPool([SearchPoolConfig("review", self.KEY)])
        return ExaSearchClient(pool, request_json=requester)

    def test_reflected_credential_never_reaches_the_audit_log(self):
        from native_jev_judge import AuditLog, AuditedSearchClient
        from search_grounded_judge import SearchError

        path = Path(self._directory.name) / "audit.jsonl"
        audited = AuditedSearchClient(
            self._leaking_client(), "exa", AuditLog(path, secrets=[self.KEY]),
        )
        with self.assertRaises(SearchError):
            run(audited.search("q"))
        text = path.read_text(encoding="utf-8")
        self.assertNotIn(self.KEY, text)
        record = json.loads(text.splitlines()[-1])
        self.assertEqual(record["event"], "search_error")
        self.assertEqual(record["error_code"], "search_error")
        self.assertEqual(record["http_status"], 400)

    def test_reflected_credential_is_blocked_even_without_a_registered_secret(self):
        from native_jev_judge import AuditLog, AuditedSearchClient
        from search_grounded_judge import SearchError

        path = Path(self._directory.name) / "audit_unregistered.jsonl"
        audited = AuditedSearchClient(self._leaking_client(), "exa", AuditLog(path))
        with self.assertRaises(SearchError):
            run(audited.search("q"))
        self.assertNotIn(self.KEY, path.read_text(encoding="utf-8"))

    def test_search_trace_and_provenance_never_carry_upstream_text(self):
        import classification_maintenance as cm
        import native_jev_judge as module
        from native_jev_judge import NativeJevJudge, NativeJevStateStore, NativeSearchRouter
        from search_grounded_judge import SearchError

        key = self.KEY

        class ReflectingEngine:
            """Raises the exact message shape a provider reflection produces."""

            async def search(self, _query):
                exc = SearchError(
                    f'Exa 搜索请求失败 (HTTP 400); detail={{"api_key":"{key}","message":"invalid"}}'
                )
                exc.http_status = 400
                raise exc

        router = NativeSearchRouter(
            {"exa": ReflectingEngine(), "tavily": ReflectingEngine()},
            primary=("exa", "tavily"), fallback=("deepseek_search",),
        )
        judge = NativeJevJudge(
            router, FakeJev(), _policy(),
            state=NativeJevStateStore(Path(self._directory.name) / "trace.json", clock=lambda: 0.0),
            clock=lambda: 0.0,
        )
        result = run(judge.evaluate("反射タグ"))
        payload = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(key, payload)
        self.assertEqual(result["status"], "deferred")
        for entry in result["search_trace"]:
            self.assertNotIn("error", entry)
        self.assertEqual(result["search_trace"][0]["error_code"], "search_error")
        self.assertEqual(result["search_trace"][0]["http_status"], 400)
        provenance = cm.grounding_provenance(result)
        self.assertEqual(provenance["search_trace"][0]["error_code"], "search_error")
        self.assertNotIn(key, json.dumps(provenance, ensure_ascii=False))


class ThirdSourceFallbackTests(_IsolatedDatabase):
    """D1: DeepSeek is a third source after classification, never a tie-breaker."""

    def _choices(self):
        from native_jev_judge import JevChoice

        return [
            JevChoice("feature", 0.98, {}, "m1", {}),
            JevChoice("feature", 0.89, {}, "m1", {}),
            JevChoice("feature", 0.97, {}, "m1", {}),
        ]

    def test_one_high_one_low_evidence_reaches_the_third_engine(self):
        from native_jev_judge import NativeJevJudge, NativeJevStateStore

        router = FakeRouter(
            [_evidence("exa"), _evidence("tavily", urls=("https://example.test/b",))],
            fallback=_evidence("deepseek_search", urls=("https://example.test/c",)),
        )
        jev = FakeJev(self._choices())
        judge = NativeJevJudge(
            router, jev, _policy(),
            state=NativeJevStateStore(Path(self._directory.name) / "third.json", clock=lambda: 0.0),
            clock=lambda: 0.0,
        )
        result = run(judge.evaluate("サードタグ"))
        self.assertEqual(router.third_calls, ["deepseek_search"])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["classification"], "feature")
        self.assertEqual(sorted(result["agreement"]["agreeing_sources"]), ["deepseek_search", "exa"])
        self.assertEqual(result["usage"]["jev_calls"], 3)

    def test_two_explicit_opposing_categories_hold_without_a_third_request(self):
        from native_jev_judge import JevChoice, NativeJevJudge, NativeJevStateStore

        router = FakeRouter(
            [_evidence("exa"), _evidence("tavily", urls=("https://example.test/b",))],
            fallback=_evidence("deepseek_search"),
        )
        jev = FakeJev([JevChoice("feature", 0.98, {}, "m", {}),
                       JevChoice("character", 0.98, {}, "m", {})])
        judge = NativeJevJudge(
            router, jev, _policy(),
            state=NativeJevStateStore(Path(self._directory.name) / "conflict.json", clock=lambda: 0.0),
            clock=lambda: 0.0,
        )
        result = run(judge.evaluate("対立タグ"))
        self.assertEqual(result["status"], "hold")
        self.assertIn(result["reason"], {"conflicting_classification", "insufficient_agreement"})
        self.assertEqual(router.third_calls, [], "已有明确对立类别时不得再花一次第三票")
        self.assertEqual(jev.calls, ["exa", "tavily"])


class EngineOrderTests(_IsolatedDatabase):
    """D2: the primary tier is ordered by usable quota and cooldown."""

    class _Pool:
        def __init__(self, used, limit):
            self.used, self.limit = used, limit

        def status(self):
            return [{
                "pool_id": "p", "request_limit": self.limit,
                "requests_used": self.used, "exhausted": self.used >= self.limit,
            }]

    class _Engine:
        def __init__(self, name, calls, pool=None):
            self.name, self.calls, self._pool = name, calls, pool

        async def search(self, query):
            self.calls.append(self.name)
            return _response(self.name)

    def test_nearly_exhausted_engine_is_not_asked_first(self):
        from native_jev_judge import NativeSearchRouter

        calls = []
        router = NativeSearchRouter({
            "exa": self._Engine("exa", calls, self._Pool(995, 1000)),
            "tavily": self._Engine("tavily", calls, self._Pool(10, 500)),
        })
        result = run(router.gather("タグ", "q"))
        self.assertEqual(calls, ["tavily", "exa"])
        # Both engines are still asked once: the two-source threshold is intact.
        self.assertEqual(len(result.evidences), 2)

    def test_healthy_equal_engines_keep_the_configured_order(self):
        from native_jev_judge import NativeSearchRouter

        calls = []
        router = NativeSearchRouter({
            "exa": self._Engine("exa", calls, self._Pool(0, 1000)),
            "tavily": self._Engine("tavily", calls, self._Pool(0, 500)),
        })
        run(router.gather("タグ", "q"))
        self.assertEqual(calls, ["exa", "tavily"])

    def test_cooling_engine_is_ordered_last_but_still_recorded(self):
        from native_jev_judge import NativeSearchRouter, NativeJevStateStore

        calls = []
        state = NativeJevStateStore(Path(self._directory.name) / "order_state.json", clock=lambda: 0.0)
        state.cool("engine:exa", 300, "rate_limited")
        router = NativeSearchRouter(
            {"exa": self._Engine("exa", calls), "tavily": self._Engine("tavily", calls)},
            cooldowns=state,
        )
        result = run(router.gather("タグ", "q"))
        self.assertEqual(calls, ["tavily"])
        outcomes = {item["engine"]: item["outcome"] for item in result.trace}
        self.assertEqual(outcomes["exa"], "cooldown")


class DeferralTests(_IsolatedDatabase):
    """B3: operational limits defer; only a completed search may hold a tag."""

    def test_exhausted_budget_defers_without_cooling_the_tag(self):
        from native_jev_judge import NativeJevJudge, NativeJevStateStore, NativeSearchRouter, RequestBudget

        calls = []
        budget = RequestBudget(0)
        router = NativeSearchRouter(
            {name: Engine(calls) for name in ("exa", "tavily", "deepseek_search")},
            budget=budget,
        )
        state = NativeJevStateStore(Path(self._directory.name) / "budget.json", clock=lambda: 0.0)
        judge = NativeJevJudge(router, FakeJev(), _policy(), state=state, budget=budget,
                               clock=lambda: 0.0)
        result = run(judge.evaluate("予算タグ"))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "budget_exhausted")
        self.assertEqual(calls, [])
        self.assertFalse(state.is_cooling("tag:予算タグ"))

    def test_engine_cooldown_defers_without_cooling_or_unresolving(self):
        import database as db
        from native_jev_judge import NativeJevJudge, NativeJevStateStore, NativeSearchRouter

        state = NativeJevStateStore(Path(self._directory.name) / "cooldown.json", clock=lambda: 0.0)
        for engine in ("exa", "tavily", "deepseek_search"):
            state.cool(f"engine:{engine}", 600, "transient_error")
        calls = []
        router = NativeSearchRouter({name: Engine(calls) for name in ("exa", "tavily", "deepseek_search")},
                                    cooldowns=state)
        judge = NativeJevJudge(router, FakeJev(), _policy(), state=state, clock=lambda: 0.0)
        result = run(judge.evaluate("冷却タグ"))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "engine_cooldown")
        self.assertEqual(calls, [])
        self.assertFalse(state.is_cooling("tag:冷却タグ"))

        summary = run(self._maintenance(calls_for_db=True, state=state))
        self.assertEqual(summary["unresolved"], 0)
        for item in summary["items"]:
            self.assertNotEqual(item["status"], "unresolved")
        self.assertIsNone(run(db.get_ai_tag_classification_record("冷却タグ")))

    def test_unconfigured_engine_combination_defers_before_any_request(self):
        from native_jev_judge import NativeJevJudge, NativeJevStateStore, NativeSearchRouter

        calls = []
        router = NativeSearchRouter(
            {"exa": Engine(calls)}, cooldowns=None,
        )
        judge = NativeJevJudge(
            router, FakeJev(), _policy(),
            state=NativeJevStateStore(Path(self._directory.name) / "single.json", clock=lambda: 0.0),
            clock=lambda: 0.0,
        )
        result = run(judge.evaluate("単一タグ"))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "insufficient_engines_configured")
        self.assertEqual(calls, [])
        self.assertEqual(result["usage"]["search_queries"], 0)

    def test_a_completed_search_without_evidence_still_holds(self):
        from native_jev_judge import NativeJevJudge, NativeJevStateStore, NativeSearchRouter

        calls = []
        state = NativeJevStateStore(Path(self._directory.name) / "hold_ok.json", clock=lambda: 0.0)
        router = NativeSearchRouter(
            {name: EmptyEngine(calls, name) for name in ("exa", "tavily")}, cooldowns=state,
        )
        judge = NativeJevJudge(router, FakeJev(), _policy(), state=state, clock=lambda: 0.0)
        result = run(judge.evaluate("無証拠タグ"))
        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["reason"], "no_evidence")
        self.assertTrue(state.is_cooling("tag:無証拠タグ"))

    def _maintenance(self, calls_for_db=False, state=None):
        import native_jev_judge as module
        import classification_maintenance as cm
        from native_jev_judge import NativeJevJudge, NativeSearchRouter

        calls = []
        router = NativeSearchRouter({name: Engine(calls) for name in ("exa", "tavily", "deepseek_search")},
                                    cooldowns=state)
        judge = NativeJevJudge(router, FakeJev(), _policy(), state=state, clock=lambda: 0.0)
        config = {"tag_classifier": {"grounded_judge": {"backend": "native_jev"}}}

        async def scenario():
            with patch.object(module, "build_configured_native_jev_judge",
                              return_value=_runtime(judge)):
                return await cm.run_scheduled_maintenance(["冷却タグ"], config)

        return scenario()


class Engine:
    """Search engine double returning evidence for the tag."""

    def __init__(self, calls):
        self.calls = calls

    async def search(self, query):
        self.calls.append("engine")
        return _response("exa")


class EmptyEngine:
    """Search engine double that answers with no usable snippet."""

    def __init__(self, calls, name="exa"):
        self.calls = calls
        self.name = name

    async def search(self, query):
        self.calls.append(self.name)
        from search_grounded_judge import SearchNoEvidence

        raise SearchNoEvidence("no results")


class TaskLockTests(_IsolatedDatabase):
    """C2/C4: one cross-process task lock; a timeout defers instead of racing."""

    _HOLDER = '''
import sys
sys.path.insert(0, {root!r})
from native_jev_judge import NativeJevTaskLock
lock = NativeJevTaskLock({path!r}, timeout_seconds=10)
assert lock.acquire(), "holder could not acquire the lock"
print("READY", flush=True)
sys.stdin.readline()
lock.release()
'''

    _PROBE = '''
import sys
sys.path.insert(0, {root!r})
from native_jev_judge import NativeJevTaskLock
print("ACQUIRED" if NativeJevTaskLock({path!r}, timeout_seconds=0.0).acquire() else "BUSY")
'''

    def _spawn(self, script):
        return subprocess.Popen(
            [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )

    def test_second_process_times_out_and_defers_the_whole_batch(self):
        import classification_maintenance as maintenance

        lock_path = Path(self._directory.name) / "native.lock"
        holder = self._spawn(self._HOLDER.format(root=str(ROOT), path=str(lock_path)))
        try:
            self.assertEqual(holder.stdout.readline().strip(), "READY")

            config = {"tag_classifier": {"grounded_judge": {
                "backend": "native_jev",
                "native_jev": {
                    "enabled": True, "lock_path": str(lock_path), "lock_timeout_seconds": 0.2,
                    "state_path": str(Path(self._directory.name) / "state.json"),
                },
            }}}
            called = []

            async def classify(tag, _config):
                called.append(tag)
                raise AssertionError("不得在锁被占用时发起分类")

            summary = run(maintenance.ClassificationMaintenance(
                config, classify=classify,
            ).run_tags(["tag_a", "tag_b"]))

            self.assertEqual(summary["lock"], "timeout")
            self.assertEqual(summary["deferred"], 2)
            self.assertEqual(
                [item["reason"] for item in summary["items"]],
                ["task_lock_timeout", "task_lock_timeout"],
            )
            self.assertEqual(called, [])
        finally:
            try:
                holder.stdin.write("go\n")
                holder.stdin.flush()
            except (BrokenPipeError, ValueError):
                pass
            holder.wait(timeout=15)

        probe = self._spawn(self._PROBE.format(root=str(ROOT), path=str(lock_path)))
        try:
            self.assertEqual(probe.stdout.readline().strip(), "ACQUIRED")
        finally:
            probe.wait(timeout=15)

    def test_lock_is_reentrant_inside_one_process(self):
        from native_jev_judge import NativeJevTaskLock

        path = Path(self._directory.name) / "reentrant.lock"
        outer = NativeJevTaskLock(path, timeout_seconds=0.1)
        inner = NativeJevTaskLock(path, timeout_seconds=0.1)
        self.assertTrue(outer.acquire())
        self.assertTrue(inner.acquire())
        inner.release()
        # Still held by the outer acquisition.
        probe = self._spawn(self._PROBE.format(root=str(ROOT), path=str(path)))
        try:
            self.assertEqual(probe.stdout.readline().strip(), "BUSY")
        finally:
            probe.wait(timeout=15)
        outer.release()

    def test_preview_state_is_isolated_while_the_ledger_and_lock_are_shared(self):
        import importlib.util
        from native_jev_judge import budget_semantics, native_task_lock_path

        spec = importlib.util.spec_from_file_location(
            "review_fix_preview_scope", ROOT / "scripts" / "preview_native_jev.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        base = {"tag_classifier": {"grounded_judge": {
            "backend": "native_jev",
            "native_jev": {
                "enabled": True,
                "state_path": "data/native_jev_state.json",
                "quota_state_path": "data/search_judge_quota_usage.json",
                "per_run_request_budget": 12,
            },
        }}}
        config = module._effective_config(module.parse_args([]), base)
        native = config["tag_classifier"]["grounded_judge"]["native_jev"]

        self.assertEqual(native["state_path"], "data/native_jev_state.json.preview")
        self.assertEqual(native["quota_state_path"], "data/search_judge_quota_usage.json")
        # One lock serialises preview, maintenance and manual runs.
        self.assertEqual(native_task_lock_path(base), native_task_lock_path(config))
        semantics = budget_semantics(config)
        self.assertTrue(semantics["is_soft_limit"])
        self.assertFalse(semantics["upstream_verified"])
        self.assertEqual(semantics["scope"], "process_local")


class SettingsPreservationTests(unittest.TestCase):
    """B1: an unrelated save keeps the backend and subtree it cannot configure."""

    def _current(self):
        return {
            "web": {"enabled": True, "require_login_password": True, "password": "hash", "port": 8000},
            "providers": {
                "brave_1": {"type": "brave_search", "api_key": "brave-key"},
                "tavily_1": {"type": "tavily_search", "api_key": "tavily-key"},
                "exa_1": {"type": "exa_search", "api_key": "exa-key"},
                "openrouter": {
                    "type": "openai_compatible", "api_key": "or-key",
                    "base_url": "https://openrouter.ai/api/v1",
                },
            },
            "models": {
                "deepseek_v4": {
                    "provider": "openrouter", "model": "deepseek-v4-flash",
                    "capabilities": ["llm"],
                },
            },
            "tag_classifier": {
                "enabled": True, "ttl_days": 30, "batch_size": 50, "concurrency": 5,
                "maintenance": {"max_tags_per_run": 40, "min_profile_weight": 1.0},
                "grounded_judge": {
                    "backend": "native_jev",
                    "search_classifier_model": "deepseek_v4",
                    "brave_providers": ["brave_1"],
                    "tavily_providers": ["tavily_1"],
                    "quota_state_path": "data/search_judge_quota_usage.json",
                    "native_jev": {
                        "enabled": True,
                        "model": "jev-1.13",
                        "exa_providers": ["exa_1"],
                        "state_path": "data/native_jev_state.json",
                        "per_run_request_budget": 12,
                        "min_confidence": 0.95,
                        "audit_log_path": "data/native_jev_audit.jsonl",
                    },
                },
            },
        }

    def _payload(self, current, mutate):
        from web.settings_editor import build_settings_snapshot

        payload = copy.deepcopy(build_settings_snapshot(current))
        mutate(payload)
        return payload

    def test_saving_an_unrelated_field_keeps_backend_and_subtree(self):
        from web.settings_editor import apply_settings_payload

        current = self._current()

        def mutate(payload):
            payload["web"]["port"] = 8100
            # Exactly what the old settings page submitted for an untouched form.
            payload["tag_classifier"]["grounded_judge"]["backend"] = "search_first"

        merged = apply_settings_payload(current, self._payload(current, mutate), lambda p: "hashed")
        grounded = merged["tag_classifier"]["grounded_judge"]
        configured = current["tag_classifier"]["grounded_judge"]["native_jev"]
        self.assertEqual(grounded["backend"], "native_jev")
        for key, value in configured.items():
            self.assertEqual(grounded["native_jev"][key], value, f"{key} 被子树覆盖丢失")
        self.assertEqual(merged["web"]["port"], 8100)

    def test_payload_without_tag_classifier_cannot_touch_the_backend(self):
        from web.settings_editor import apply_settings_payload

        current = self._current()
        merged = apply_settings_payload(current, {"network": {"max_concurrency": 7}}, lambda p: "hashed")
        grounded = merged["tag_classifier"]["grounded_judge"]
        self.assertEqual(grounded["backend"], "native_jev")
        self.assertTrue(grounded["native_jev"]["enabled"])
        self.assertEqual(merged["network"]["max_concurrency"], 7)

    def test_an_explicit_native_section_can_still_switch_the_backend(self):
        from web.settings_editor import apply_settings_payload

        current = self._current()

        def mutate(payload):
            payload["tag_classifier"]["grounded_judge"]["backend"] = "search_first"
            payload["tag_classifier"]["grounded_judge"]["native_jev"] = {"enabled": False}

        merged = apply_settings_payload(current, self._payload(current, mutate), lambda p: "hashed")
        grounded = merged["tag_classifier"]["grounded_judge"]
        self.assertEqual(grounded["backend"], "search_first")
        self.assertFalse(grounded["native_jev"]["enabled"])

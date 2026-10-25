"""Round-4 regression tests for the four remaining compatibility defects.

The four cases are the concrete counter-examples recorded by the third
independent review in ``jev-round3-review`` (its scripts and logs are reproduced
unchanged in ``jev-round4-regression/review-scripts``):

R1  ``classification_maintenance.run_scheduled_maintenance`` converted *every*
    generic backend failure into a silent ``deferred`` item, so the legacy
    ``search_first`` backend lost its documented round-2 behaviour
    (``failed`` + an ``unresolved`` review-queue marker).
R2  The closed HTTP transport conversion swallowed local timeouts into a plain
    ``SearchTransportError``, so ``SearchCredentialPool`` no longer retried a
    timeout (one request instead of two), and an undecodable body was turned
    into an empty result ("no evidence") instead of an unreadable answer.
R3  ``SystemOneJevClient`` let ``SearchTransportError`` escape, so the batch,
    the single-tag review and the full CLI preview all surfaced an
    uncontrolled transport error and lost the search usage already spent
    (``search_queries`` reported 0 while two searches really ran).
R4  ``SearchCredentialPool.refresh_from_ledger`` adopted the on-disk counter of
    a *new* month but did not advance the quota period, so the following
    reservation reset it: a disk baseline of 8 became 1 instead of 9.

Everything below walks a real entry point (maintenance batch, single-tag
review, CLI preview) through the real lock, state ledger, monthly quota ledger,
database and audit path.  Only two things are doubled: the search transport of
the *engine* clients (an injected ``_request_json`` that keeps the exact
HTTP-shaped contract) and the System One classifier (a fixed ``JevChoice``).
The transport tests are the exception: they drive the real
``_aiohttp_request_json`` and the real ``SystemOneJevClient`` against a
127.0.0.1 loopback server, so the aiohttp failure path is genuine.  Those tests
are skipped inside ``tests/run_offline.py`` (which forbids sockets); nothing
here contacts a provider or reads a production config, database or credential,
and a green run is not evidence about the real providers.
"""

import asyncio
import importlib.util
import json
import os
import socket
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import classification_maintenance as cm  # noqa: E402
import database as db  # noqa: E402
import native_jev_judge as n  # noqa: E402
import search_grounded_judge as s  # noqa: E402

TOKEN = "synthetic-round4-marker"  # marker, not a credential
OFFLINE_RUNNER = os.environ.get("JEV_OFFLINE_RUNNER") == "1"
LOOPBACK_SKIP = (
    "needs a real 127.0.0.1 socket; run this module under pytest instead of the "
    "socket-blocking offline runner"
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Synthetic configuration / doubles
# ---------------------------------------------------------------------------

def native_config(folder, *, audit: bool = False, **overrides) -> dict:
    """Native config with synthetic in-config credentials and short timeouts."""
    native = {
        "enabled": True,
        "state_path": str(Path(folder) / "state.json"),
        "quota_state_path": str(Path(folder) / "quota.json"),
        "lock_path": str(Path(folder) / "task.lock"),
        "lock_timeout_seconds": 0.05,
        "exa_providers": ["exa"],
        "tavily_providers": ["tavily"],
        "jev_provider": "jev",
        "per_run_request_budget": 100,
        "quota_estimates": {"exa_monthly_requests": 100, "tavily_monthly_requests": 100},
        "jev_endpoint": "https://example.invalid/systemone",
    }
    if audit:
        native["audit_log_path"] = str(Path(folder) / "audit.jsonl")
    native.update(overrides)
    return {
        "providers": {
            "exa": {"type": "exa_search", "api_key": "synthetic-exa-key-0001"},
            "tavily": {"type": "tavily_search", "api_key": "synthetic-tavily-key-0001"},
            "jev": {"type": "openai_compatible", "base_url": "https://example.invalid",
                    "api_key": "synthetic-jev-key-0001"},
        },
        "tag_classifier": {"grounded_judge": {"backend": "native_jev", "native_jev": native}},
    }


def wire(runtime, calls):
    """Replace only the network boundary of every configured engine."""
    for engine, client in runtime.router._engines.items():
        inner = getattr(client, "_inner", client)

        async def transport(method, url, headers, *, json_body=None, engine=engine, **kw):
            calls.append(engine)
            query = (json_body or {}).get("query") or ""
            text = query + " は特徴です。"
            key = "text" if engine == "exa" else "content"
            return 200, {"results": [{"url": f"https://example.test/{engine}", key: text}]}, {}

        inner._request_json = transport
    jev = getattr(runtime.jev, "_inner", runtime.jev)

    async def classify(tag, evidence):
        return n.JevChoice("feature", 0.98, {}, "synthetic/jev", {})

    jev.classify = classify


def ledger_usage(folder) -> dict:
    return s.MonthlyQuotaUsageLedger(Path(folder) / "quota.json").initial_usage()


def optional_text(path) -> str:
    """Read an evidence file that only exists once something was written."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _load_preview_cli():
    """Load scripts/preview_native_jev.py without requiring a package import."""
    spec = importlib.util.spec_from_file_location(
        "round4_preview_native_jev", ROOT / "scripts" / "preview_native_jev.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _NativeCase(unittest.TestCase):
    """Isolated database, state, quota ledger and lock for one test."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.folder = Path(self._directory.name)
        self._previous_path = db.DB_PATH
        db.DB_PATH = self.folder / "native.sqlite3"
        db._init_db_sync()
        n._NATIVE_RUNTIMES.clear()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        n._NATIVE_RUNTIMES.clear()
        db.DB_PATH = self._previous_path
        self._directory.cleanup()

    def build(self, config):
        return n.build_configured_native_jev_judge(config)


#: Captured at import time, i.e. before the pytest/offline guard replaces them,
#: so the wrapper below can delegate loopback traffic to the real syscall.
_PRISTINE_CONNECT = socket.socket.connect
_PRISTINE_CONNECT_EX = socket.socket.connect_ex


class _Loopback:
    """Run the inner block with 127.0.0.1 sockets allowed and nothing else."""

    def __enter__(self):
        original = _PRISTINE_CONNECT
        original_ex = _PRISTINE_CONNECT_EX
        self._allowed = []

        def loopback_connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                if str(address[0]) not in {"127.0.0.1", "::1"}:
                    raise AssertionError(f"non-loopback connection: {address!r}")
                self._allowed.append(address)
            return original(sock, address)

        def loopback_connect_ex(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                if str(address[0]) not in {"127.0.0.1", "::1"}:
                    raise AssertionError(f"non-loopback connection: {address!r}")
                self._allowed.append(address)
            return original_ex(sock, address)

        self._patches = [
            patch.object(socket.socket, "connect", loopback_connect),
            patch.object(socket.socket, "connect_ex", loopback_connect_ex),
        ]
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *exc_info):
        for item in reversed(self._patches):
            item.stop()
        return False


async def _serve(handler):
    """Start a loopback server and return (server, url)."""
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}/"


# ---------------------------------------------------------------------------
# R1: generic backend failure -- legacy vs native contract
# ---------------------------------------------------------------------------

class MaintenanceExceptionSemanticsTests(_NativeCase):

    def test_search_first_generic_failure_stays_failed_and_marks_unresolved(self):
        """Round-2 semantics: a legacy outage is a failed tag, not a deferral."""
        config = native_config(self.folder, audit=False)
        config["tag_classifier"]["grounded_judge"]["backend"] = "search_first"

        async def broken(_tag, _config):
            raise RuntimeError("synthetic-legacy-failure")

        summary = run(cm.ClassificationMaintenance(config, classify=broken).run_tags(
            ["legacy-tag"],
        ))
        item = summary["items"][0]

        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["error"], "synthetic-legacy-failure",
                         "legacy 后端保留原有可诊断信息")
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["deferred"], 0,
                         "search_first 不得被 native 的 deferred 语义覆盖")
        row = run(db.get_tag_classifications(["legacy-tag"])).get("legacy-tag") or {}
        self.assertEqual(row.get("classification"), "unresolved",
                         "旧语义必须把失败标签重新放回人工复核队列")
        self.assertEqual(row.get("source"), "ai")
        self.assertNotIn("error_code", item)

    def test_search_first_failure_keeps_a_human_decision(self):
        """The legacy marker still refuses to overwrite a manual classification."""
        config = native_config(self.folder, audit=False)
        config["tag_classifier"]["grounded_judge"]["backend"] = "search_first"
        with sqlite3.connect(db.DB_PATH) as con:
            con.execute(
                "INSERT INTO tag_classification_cache (normalized_tag, classification, source)"
                " VALUES (?, 'character', 'manual')", ("human-tag",),
            )

        async def broken(_tag, _config):
            raise RuntimeError("synthetic-legacy-failure")

        summary = run(cm.ClassificationMaintenance(config, classify=broken).run_tags(
            ["human-tag"],
        ))
        self.assertEqual(summary["items"][0]["status"], "preserved")
        self.assertEqual(summary["preserved"], 1)
        row = run(db.get_tag_classifications(["human-tag"])).get("human-tag") or {}
        self.assertEqual(row.get("classification"), "character")

    def test_native_generic_failure_still_defers_without_raw_text(self):
        """The Native Jev outage contract of round 3 must not regress."""
        config = native_config(self.folder, audit=True)

        async def explode(_tag, _config):
            raise RuntimeError(f"upstream said {TOKEN} in a header")

        summary = run(cm.ClassificationMaintenance(config, classify=explode).run_tags(
            ["tag-boom"],
        ))
        item = summary["items"][0]
        self.assertEqual(item["status"], "deferred")
        self.assertEqual(item["reason"], "internal_error")
        self.assertEqual(item["error_code"], "unexpected_error")
        self.assertEqual(summary["failed"], 0)
        persisted = run(db.get_state(cm.SUMMARY_STATE_KEY)) or ""
        audit = optional_text(self.folder / "audit.jsonl")
        state = optional_text(self.folder / "state.json")
        for text in (json.dumps(summary, ensure_ascii=False), persisted, audit, state):
            self.assertNotIn(TOKEN, text)
        entry = run(db.get_tag_classifications(["tag-boom"])).get("tag-boom")
        self.assertIsNone(entry, "运维故障不得写入 unresolved")


# ---------------------------------------------------------------------------
# R2: shared HTTP timeout conversion vs the credential-pool retry contract
# ---------------------------------------------------------------------------

class SharedTransportTimeoutTests(_NativeCase):

    def test_pool_retries_a_timeout_on_the_same_pool(self):
        """Non-negotiable contract: the first timeout is retried once."""
        calls = []

        async def request(_selected):
            calls.append(_selected.pool_id)
            if len(calls) == 1:
                raise asyncio.TimeoutError("synthetic read timeout")
            return 200, {"ok": True}, {}

        pool = s.SearchCredentialPool([s.SearchPoolConfig("search", "synthetic-only", 10)])
        status, body, _headers = run(pool.search(request, retries=1))

        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(len(calls), 2, "超时必须在同一 Pool 上重试一次")
        self.assertEqual(pool.status()[0]["requests_used"], 2,
                         "两次真实请求都必须计入用量")

    @unittest.skipIf(OFFLINE_RUNNER, LOOPBACK_SKIP)
    def test_real_timeout_then_success_uses_two_requests(self):
        """The real aiohttp timeout must stay retryable through the closed error."""
        counts = {"requests": 0}

        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                counts["requests"] += 1
                if counts["requests"] == 1:
                    await asyncio.sleep(0.30)
                body = b'{"ok": true}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                )
                await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        async def scenario():
            server, url = await _serve(handler)
            pool = s.SearchCredentialPool(
                [s.SearchPoolConfig("search", "synthetic-only", 10)],
            )

            async def request(_selected):
                # 40 ms > the first 120 ms stall, < the second (immediate) answer.
                return await s._aiohttp_request_json(
                    "GET", url, {}, timeout_seconds=0.04,
                )

            try:
                return await pool.search(request, retries=1), pool
            finally:
                server.close()
                await server.wait_closed()

        with _Loopback():
            (status, body, _headers), pool = run(scenario())

        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(counts["requests"], 2, "第一次真实 40ms 超时后必须重试成功")
        self.assertEqual(pool.status()[0]["requests_used"], 2)

    @unittest.skipIf(OFFLINE_RUNNER, LOOPBACK_SKIP)
    def test_real_timeout_is_a_controlled_timeout_error(self):
        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                await asyncio.sleep(0.30)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        async def scenario():
            server, url = await _serve(handler)
            try:
                return await s._aiohttp_request_json("GET", url, {}, timeout_seconds=0.04)
            finally:
                server.close()
                await server.wait_closed()

        with _Loopback():
            with self.assertRaises(s.SearchTransportError) as caught:
                run(scenario())

        error = caught.exception
        self.assertIsInstance(error, TimeoutError, "超时类别必须可识别")
        self.assertEqual(str(error), "搜索请求超时（传输层）")
        self.assertNotIn("Timeout", str(error))
        self.assertNotIn("synthetic", str(error))

    @unittest.skipIf(OFFLINE_RUNNER, LOOPBACK_SKIP)
    def test_undecodable_body_is_not_an_empty_result(self):
        """A body that cannot be decoded is unreadable, not "no evidence"."""

        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                body = b"\xff\xfe"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async def scenario():
            server, url = await _serve(handler)
            try:
                return await s._aiohttp_request_json("GET", url, {})
            finally:
                server.close()
                await server.wait_closed()

        with _Loopback():
            with self.assertRaises(s.SearchTransportError) as caught:
                run(scenario())
        self.assertNotIsInstance(caught.exception, s.SearchNoEvidence)

    @unittest.skipIf(OFFLINE_RUNNER, LOOPBACK_SKIP)
    def test_plain_invalid_json_keeps_the_legacy_empty_result(self):
        """Not widened this round: a non-JSON body still yields ``{}``."""

        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                body = b"NOT-JSON"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async def scenario():
            server, url = await _serve(handler)
            try:
                return await s._aiohttp_request_json("GET", url, {})
            finally:
                server.close()
                await server.wait_closed()

        with _Loopback():
            status, body, _headers = run(scenario())
        self.assertEqual(status, 200)
        self.assertEqual(body, {})

    @unittest.skipIf(OFFLINE_RUNNER, LOOPBACK_SKIP)
    def test_search_first_reports_a_transport_failure_as_unavailable(self):
        """The legacy judge must not file a corrupt answer as "no evidence"."""  # noqa: D401

        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                body = b"\xff\xfe"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        class NeverCalled:
            async def classify(self, *_args):
                raise AssertionError("证据不可读时不得调用模型")

        async def scenario():
            server, url = await _serve(handler)
            pool = lambda name: s.SearchCredentialPool(  # noqa: E731
                [s.SearchPoolConfig(name, "synthetic-only", 10)],
            )
            brave = s.BraveLLMContextClient(pool("brave"), endpoint=url)
            tavily = s.TavilySearchClient(pool("tavily"), endpoint=url)
            try:
                return await s.SearchGroundedJudge(
                    brave, tavily, NeverCalled(),
                ).classify("tag", None)
            finally:
                server.close()
                await server.wait_closed()

        with _Loopback():
            outcome = run(scenario())

        self.assertEqual(outcome["reason"], "search_unavailable")
        self.assertNotEqual(outcome["reason"], "no_search_evidence")
        self.assertTrue(
            all(item["outcome"] != "no_evidence" for item in outcome["search_trace"]),
            "传输失败不得记为 no_evidence",
        )
        self.assertEqual(outcome["usage"]["search_queries"], 2)


# ---------------------------------------------------------------------------
# R3: Native System One transport failure -> one availability outcome
# ---------------------------------------------------------------------------

class JevTransportAvailabilityTests(_NativeCase):

    async def _malformed_header_server(self):
        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write((
                    "HTTP/1.1 200 OK\r\nBad Header: " + TOKEN +
                    "\r\nContent-Length: 2\r\n\r\n{}"
                ).encode())
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        return await _serve(handler)

    def _use_real_system_one(self, runtime, endpoint):
        jev = getattr(runtime.jev, "_inner", runtime.jev)
        jev.classify = n.SystemOneJevClient.classify.__get__(jev)
        jev.endpoint = endpoint
        jev._request_json = s._aiohttp_request_json
        return jev

    @unittest.skipIf(OFFLINE_RUNNER, LOOPBACK_SKIP)
    def test_transport_failure_defers_batch_single_and_preview(self):
        preview_cli = _load_preview_cli()
        config = native_config(self.folder, audit=True)

        async def scenario():
            server, url = await self._malformed_header_server()
            try:
                runtime = self.build(config)
                calls = []
                wire(runtime, calls)
                self._use_real_system_one(runtime, url)
                summary = await cm.ClassificationMaintenance(config).run_tags(
                    ["jev-batch"],
                )

                single_runtime = self.build(config)
                single_calls = []
                wire(single_runtime, single_calls)
                self._use_real_system_one(single_runtime, url)
                try:
                    await cm.ClassificationMaintenance(config).classify_tag("jev-single")
                except Exception as exc:  # noqa: BLE001 - the contract is "deferred"
                    single = exc
                else:  # pragma: no cover - a passed exception would be a defect
                    single = None

                with sqlite3.connect(db.DB_PATH) as con:
                    con.execute(
                        "INSERT INTO xp_profile(tag,weight) VALUES (?,?)",
                        ("jev-preview", 2),
                    )
                args = preview_cli.parse_args(["--tags", "jev-preview"])
                preview_cfg = preview_cli._effective_config(args, config)
                preview_runtime = self.build(preview_cfg)
                wire(preview_runtime, [])
                self._use_real_system_one(preview_runtime, url)
                with patch.object(preview_cli, "load_config", return_value=config):
                    report = await preview_cli.run(args)
                return summary, calls, single, report
            finally:
                server.close()
                await server.wait_closed()

        with _Loopback() as loopback:
            summary, calls, single, report = run(scenario())
        self.assertTrue(loopback._allowed, "测试必须真的连过本地回环套接字")

        item = summary["items"][0]
        self.assertEqual(item["status"], "deferred", "原生传输失败是可用性延后，不是失败")
        self.assertEqual(item["error"], "Native Jev 暂不可用：jev_unavailable",
                         "只允许封闭的可用性文本")
        self.assertNotIn("error_code", item)
        self.assertEqual(summary["unresolved"], 0)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(len(calls), 2, "两次真实搜索已完成")
        self.assertEqual(summary["usage"]["search_queries"], 2,
                         "已完成的搜索次数不得被清零")
        self.assertEqual(item["usage"]["search_queries"], 2)

        self.assertIsNotNone(single, "单标签入口必须是受控的 deferred 异常")
        self.assertIsInstance(single, n.GroundedJudgeDeferredError)
        self.assertEqual(str(single), "Native Jev 暂不可用：jev_unavailable")
        self.assertEqual(getattr(single, "usage", {}).get("search_queries"), 2)

        self.assertEqual(report["tags"][0]["status"], "deferred")
        self.assertEqual(report["tags"][0]["reason"], "jev_unavailable")
        self.assertEqual(report["tags"][0]["usage"]["search_queries"], 2)

        persisted = run(db.get_state(cm.SUMMARY_STATE_KEY)) or ""
        audit = optional_text(self.folder / "audit.jsonl")
        state = optional_text(self.folder / "state.json")
        for text in (
            json.dumps(summary, ensure_ascii=False), json.dumps(report, ensure_ascii=False),
            persisted, audit, state, str(single),
        ):
            self.assertNotIn(TOKEN, text, "上游头部内容不得进入任何持久化输出")
            self.assertNotIn("Bad Header", text)
        rows = run(db.get_tag_classifications(["jev-batch", "jev-single", "jev-preview"]))
        self.assertEqual(rows, {}, "传输失败不得写入 unresolved 记录")


# ---------------------------------------------------------------------------
# R4: monthly ledger baseline and quota period must be adopted together
# ---------------------------------------------------------------------------

class QuotaLedgerMonthSyncTests(_NativeCase):
    """The monthly baseline and the month it was read for move together.

    ``current_month`` is patched before the runtime is built, because the pool
    binds that callable at construction time -- exactly the long-lived-runtime
    situation the defect describes.
    """

    def _month(self, month: list[str]):
        return patch.object(
            s.MonthlyQuotaUsageLedger, "current_month", lambda self: month[0],
        )

    def test_new_month_continues_from_the_disk_baseline(self):
        month = ["2026-10"]
        config = native_config(self.folder)
        calls: list[str] = []
        with self._month(month):
            runtime = self.build(config)
            wire(runtime, calls)
            self.assertEqual(runtime.pools[0]._active_quota_period, "2026-10")
            run(cm.ClassificationMaintenance(config).run_tags(["month-old"]))
            self.assertEqual(ledger_usage(self.folder).get("native_jev:exa"), 1)

            month[0] = "2026-11"
            s.MonthlyQuotaUsageLedger(self.folder / "quota.json").save([
                {"pool_id": "native_jev:exa", "requests_used": 8},
                {"pool_id": "native_jev:tavily", "requests_used": 8},
            ])
            summary = run(cm.ClassificationMaintenance(config).run_tags(["month-new"]))
            usage = ledger_usage(self.folder)
            status = {entry["pool_id"]: entry["requests_used"]
                      for entry in runtime.pools[0].status()}

        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(usage, {"native_jev:exa": 9, "native_jev:tavily": 9},
                         "新月份必须从磁盘 8 继续计数为 9，而不是清零为 1")
        self.assertEqual(status["native_jev:exa"], 9)
        self.assertEqual(runtime.pools[0]._active_quota_period, "2026-11",
                         "月份必须与计数一起同步，下一次预留才不会再次清零")

    def test_same_month_adoption_still_counts_to_three(self):
        month = ["2026-10"]
        config = native_config(self.folder)
        with self._month(month):
            runtime = self.build(config)
            calls: list[str] = []
            wire(runtime, calls)
            run(cm.ClassificationMaintenance(config).run_tags(["tag-one"]))
            self.assertEqual(ledger_usage(self.folder)["native_jev:exa"], 1)

            s.MonthlyQuotaUsageLedger(self.folder / "quota.json").save([
                {"pool_id": "native_jev:exa", "requests_used": 2},
                {"pool_id": "native_jev:tavily", "requests_used": 2},
            ])
            run(cm.ClassificationMaintenance(config).run_tags(["tag-two"]))
            usage = ledger_usage(self.folder)

        self.assertEqual(usage, {"native_jev:exa": 3, "native_jev:tavily": 3},
                         "同月仍须采纳磁盘基准并继续计数")
        self.assertEqual(runtime.pools[0]._active_quota_period, "2026-10")

    def test_new_month_baseline_above_the_limit_is_not_reset(self):
        month = ["2026-10"]
        config = native_config(
            self.folder,
            quota_estimates={"exa_monthly_requests": 10, "tavily_monthly_requests": 10},
        )
        calls: list[str] = []
        with self._month(month):
            runtime = self.build(config)
            wire(runtime, calls)
            run(cm.ClassificationMaintenance(config).run_tags(["before-limit"]))
            calls.clear()
            month[0] = "2026-11"
            s.MonthlyQuotaUsageLedger(self.folder / "quota.json").save([
                {"pool_id": "native_jev:exa", "requests_used": 10},
                {"pool_id": "native_jev:tavily", "requests_used": 10},
            ])
            summary = run(cm.ClassificationMaintenance(config).run_tags(["after-limit"]))
            usage = ledger_usage(self.folder)

        self.assertEqual(calls, [], "新月份的额度守卫不得被清零绕过")
        self.assertEqual(summary["items"][0]["status"], "deferred")
        self.assertEqual(usage, {"native_jev:exa": 10, "native_jev:tavily": 10})


if __name__ == "__main__":
    unittest.main()

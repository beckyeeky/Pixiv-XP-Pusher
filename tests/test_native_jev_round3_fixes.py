"""Round-3 regression tests for the second independent review (R1-R4, D2).

Each test walks a real entry point (maintenance batch, single-tag review, CLI
preview) through the real lock, state ledger, quota ledger, database and audit
path.  Only two things are doubled:

* the search transport (an injected ``_request_json`` that keeps the exact
  HTTP-shaped contract), and
* the System One classifier (a fixed answer with the real ``JevChoice`` shape).

The malformed-header test is the exception: it drives the *real*
``_aiohttp_request_json`` against a 127.0.0.1 loopback server, so the aiohttp
failure path is genuine.  Nothing here contacts a provider or reads a
production config, database or credential; a green run is not evidence about
the real providers.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
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

TOKEN = "synthetic-review-only"  # marker, not a credential


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


def wire(runtime, calls, *, outage=(), delay=0.0):
    """Replace only the network boundary of every configured engine."""
    for engine, client in runtime.router._engines.items():
        inner = getattr(client, "_inner", client)

        async def transport(method, url, headers, *, json_body=None, engine=engine, **kw):
            calls.append(engine)
            if delay:
                await asyncio.sleep(delay)
            if engine in outage:
                return 503, {"error": "synthetic outage"}, {}
            query = (json_body or {}).get("query") or ""
            text = query + " は特徴です。"
            key = "text" if engine == "exa" else "content"
            return 200, {"results": [{"url": f"https://example.test/{engine}", key: text}]}, {}

        inner._request_json = transport
    jev = getattr(runtime.jev, "_inner", runtime.jev)

    async def classify(tag, evidence):
        import native_jev_judge as n

        return n.JevChoice("feature", 0.98, {}, "synthetic/jev", {})

    jev.classify = classify


def ledger_usage(folder) -> dict:
    from search_grounded_judge import MonthlyQuotaUsageLedger

    return MonthlyQuotaUsageLedger(Path(folder) / "quota.json").initial_usage()


def state_snapshot(folder) -> dict:
    path = Path(folder) / "state.json"
    if not path.exists():
        return {"cache": {}, "cooldowns": {}}
    return json.loads(path.read_text(encoding="utf-8"))


CHILD = r'''
import asyncio, json, os, sys
ROOT = os.environ["JEV_ROOT"]
CFG = json.loads(os.environ["JEV_CFG"])
TAG = os.environ["JEV_TAG"]
OUTAGE = {item for item in os.environ.get("JEV_OUTAGE", "").split(",") if item}
sys.path.insert(0, ROOT)
import database
database.DB_PATH = CFG.pop("db_path")
import native_jev_judge as n
import classification_maintenance as cm

def wire(rt, calls):
    for engine, client in rt.router._engines.items():
        inner = getattr(client, "_inner", client)
        async def transport(method, url, headers, *, json_body=None, engine=engine, **kw):
            calls.append(engine)
            if engine in OUTAGE:
                return 503, {"error": "synthetic outage"}, {}
            query = (json_body or {}).get("query") or ""
            text = query + " は特徴です。"
            key = "text" if engine == "exa" else "content"
            return 200, {"results": [{"url": "https://example.test/" + engine, key: text}]}, {}
        inner._request_json = transport
    jev = getattr(rt.jev, "_inner", rt.jev)
    async def classify(tag, evidence):
        return n.JevChoice("feature", 0.98, {}, "synthetic/jev", {})
    jev.classify = classify

async def main():
    rt = n.build_configured_native_jev_judge(CFG)
    calls = []
    wire(rt, calls)
    summary = await cm.ClassificationMaintenance(CFG).run_tags([TAG])
    print(json.dumps({
        "accepted": summary["accepted"], "calls": calls,
        "statuses": [item["status"] for item in summary["items"]],
    }, ensure_ascii=False))

asyncio.run(main())
'''

HOLDER = '''
import sys
sys.path.insert(0, {root!r})
from native_jev_judge import NativeJevTaskLock
lock = NativeJevTaskLock({path!r}, timeout_seconds=10)
assert lock.acquire(), "holder could not acquire the lock"
print("READY", flush=True)
sys.stdin.readline()
lock.release()
'''


class _NativeCase(unittest.TestCase):
    """Isolated database, state, quota ledger and lock for one test."""

    def setUp(self):
        import database
        import native_jev_judge

        self._directory = tempfile.TemporaryDirectory()
        self.folder = Path(self._directory.name)
        self._previous_path = database.DB_PATH
        database.DB_PATH = self.folder / "native.sqlite3"
        database._init_db_sync()
        native_jev_judge._NATIVE_RUNTIMES.clear()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import database
        import native_jev_judge

        native_jev_judge._NATIVE_RUNTIMES.clear()
        database.DB_PATH = self._previous_path
        self._directory.cleanup()

    # -- helpers ---------------------------------------------------------
    def build(self, config):
        import native_jev_judge

        return native_jev_judge.build_configured_native_jev_judge(config)

    def run_child(self, config: dict, tag: str, *, outage=()) -> dict:
        payload = dict(config)
        payload["db_path"] = str(Path(config_db_path()))
        child = self.folder / "child_task.py"
        child.write_text(CHILD, encoding="utf-8")
        environment = {
            **os.environ,
            "JEV_ROOT": str(ROOT),
            "JEV_CFG": json.dumps(payload, ensure_ascii=False),
            "JEV_TAG": tag,
            "JEV_OUTAGE": ",".join(outage),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = subprocess.run(
            [sys.executable, str(child)], capture_output=True, text=True,
            env=environment, timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def hold_lock_in_another_process(self, lock_path: Path):
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER.format(root=str(ROOT), path=str(lock_path))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(holder.stdout.readline().strip(), "READY")

        def release():
            try:
                holder.stdin.write("go\n")
                holder.stdin.flush()
            except (BrokenPipeError, ValueError):
                pass
            holder.wait(timeout=20)

        self.addCleanup(release)
        return holder


def config_db_path() -> Path:
    import database

    return Path(database.DB_PATH)


import classification_maintenance as cm  # noqa: E402
import database as db  # noqa: E402

from search_grounded_judge import _aiohttp_request_json  # noqa: E402


def preview_module():
    """Load the CLI module without executing its main entry point."""
    spec = importlib.util.spec_from_file_location(
        "round3_preview_native_jev", ROOT / "scripts" / "preview_native_jev.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lock_path_of(config: dict) -> Path:
    return Path(config["tag_classifier"]["grounded_judge"]["native_jev"]["lock_path"])


# ---------------------------------------------------------------------------
# R1: a long-lived runtime must adopt the newest on-disk snapshot
# ---------------------------------------------------------------------------

class SnapshotRefreshTests(_NativeCase):
    """R1: parent warm -> child process -> parent reuses the same runtime."""

    def test_cached_parent_runtime_adopts_the_snapshot_of_another_process(self):
        config = native_config(self.folder)
        runtime = self.build(config)
        calls = []
        wire(runtime, calls)

        warm = run(cm.ClassificationMaintenance(config).run_tags(["タグ-warmup"]))
        self.assertEqual(warm["accepted"], 1)
        self.assertEqual(
            ledger_usage(self.folder),
            {"native_jev:exa": 1, "native_jev:tavily": 1},
        )

        child = self.run_child(config, "タグ-child")
        self.assertEqual(child["accepted"], 1)
        self.assertEqual(
            ledger_usage(self.folder),
            {"native_jev:exa": 2, "native_jev:tavily": 2},
        )

        self.assertIs(self.build(config), runtime, "同一配置必须复用同一长期 runtime")
        calls.clear()
        parent = run(cm.ClassificationMaintenance(config).run_tags(["タグ-parent"]))
        self.assertEqual(parent["accepted"], 1)
        self.assertEqual(calls, ["exa", "tavily"])
        # Three real requests per engine, because the parent continued from the
        # ledger the child wrote instead of its stale in-memory counter.
        self.assertEqual(
            ledger_usage(self.folder),
            {"native_jev:exa": 3, "native_jev:tavily": 3},
        )
        self.assertIsNotNone(
            runtime.state.get_decision(
                "タグ-child", policy_fingerprint=runtime.policy.cache_fingerprint(),
            ),
            "父进程的长期 runtime 必须看到子进程写入的缓存",
        )
        self.assertIn("タグ-child", state_snapshot(self.folder)["cache"])

    def test_new_cooldown_from_another_process_is_respected_by_the_cached_runtime(self):
        config = native_config(self.folder)
        runtime = self.build(config)
        calls = []
        wire(runtime, calls)
        run(cm.ClassificationMaintenance(config).run_tags(["タグ-warmup"]))

        child = self.run_child(config, "タグ-cool", outage=["exa"])
        self.assertEqual(child["statuses"], ["deferred"])
        self.assertIn("engine:exa", state_snapshot(self.folder)["cooldowns"])

        self.assertIs(self.build(config), runtime)
        calls.clear()
        result = run(cm.ClassificationMaintenance(config).run_tags(["タグ-after"]))
        self.assertEqual(calls, ["tavily"], "冷却中的引擎不得被再次请求")
        self.assertTrue(runtime.state.is_cooling("engine:exa"), "新冷却必须已进入内存快照")
        self.assertEqual(result["items"][0]["status"], "deferred")

    def test_ledger_is_adopted_not_merged_with_the_local_counter(self):
        """A re-read is the baseline; ``max(local, disk)`` would inflate it."""
        from search_grounded_judge import MonthlyQuotaUsageLedger

        config = native_config(self.folder)
        runtime = self.build(config)
        calls = []
        wire(runtime, calls)
        run(cm.ClassificationMaintenance(config).run_tags(["タグ-one"]))
        self.assertEqual(ledger_usage(self.folder)["native_jev:exa"], 1)

        # Another process writes a smaller, but authoritative, month counter.
        MonthlyQuotaUsageLedger(self.folder / "quota.json").save(
            [{"pool_id": "native_jev:exa", "requests_used": 0},
             {"pool_id": "native_jev:tavily", "requests_used": 0}],
        )
        runtime.refresh_from_disk()
        status = {entry["pool_id"]: entry["requests_used"]
                  for entry in runtime.pools[0].status()}
        self.assertEqual(status["native_jev:exa"], 0, "必须采纳磁盘基准而不是本地最大值")

        run(cm.ClassificationMaintenance(config).run_tags(["タグ-two"]))
        self.assertEqual(ledger_usage(self.folder)["native_jev:exa"], 1)


# ---------------------------------------------------------------------------
# R2: one lock contract for Web single, batch and CLI
# ---------------------------------------------------------------------------

class OneLockContractTests(_NativeCase):
    def test_sibling_async_tasks_in_one_process_do_not_share_the_lock(self):
        config = native_config(self.folder, lock_timeout_seconds=0.05)
        runtime = self.build(config)
        calls = []
        wire(runtime, calls, delay=0.25)
        maintenance = cm.ClassificationMaintenance(config)

        async def scenario():
            first = asyncio.create_task(maintenance.run_tags(["タグ-one"]))
            await asyncio.sleep(0.08)  # the first task owns the lock now
            second = await maintenance.run_tags(["タグ-two"])
            return await first, second

        first, second = run(scenario())
        self.assertEqual(first["accepted"], 1)
        self.assertEqual(second["items"][0]["status"], "deferred")
        self.assertEqual(second["items"][0]["reason"], "task_lock_timeout")
        self.assertEqual(len(calls), 2, "被锁挡下的同 PID 任务不得发起任何请求")

    def test_batch_subtasks_may_reuse_their_own_runs_lock(self):
        """The run's own sub-tasks inherit the run: no self-deadlock."""
        config = native_config(self.folder)
        runtime = self.build(config)
        calls = []
        wire(runtime, calls)
        single_entry = cm.ClassificationMaintenance(config)

        async def classify(tag, _config):
            # Owner of the lock is the enclosing batch; this sub-task is a
            # different asyncio task in the same process and must still pass.
            return await single_entry.classify_tag(tag)

        summary = run(cm.ClassificationMaintenance(config, classify=classify).run_tags(
            ["タグ-a", "タグ-b", "タグ-c"],
        ))
        self.assertEqual(summary["accepted"], 3)
        self.assertEqual([item["status"] for item in summary["items"]], ["accepted"] * 3)
        self.assertEqual(summary["deferred"], 0)
        self.assertEqual(len(calls), 6)

    def test_single_tag_entry_defers_while_another_process_holds_the_lock(self):
        config = native_config(self.folder)
        called = []

        async def classify(tag, _config):
            called.append(tag)
            return {"tag": tag, "status": "accepted"}

        self.hold_lock_in_another_process(lock_path_of(config))
        result = run(cm.ClassificationMaintenance(config, classify=classify).classify_tag(
            "タグ-single",
        ))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "task_lock_timeout")
        self.assertEqual(called, [], "Web 单标签入口不得绕过任务锁")

    def test_preview_cli_aborts_while_another_process_holds_the_lock(self):
        config = native_config(self.folder)
        with sqlite3.connect(db.DB_PATH) as connection:
            connection.execute(
                "INSERT INTO xp_profile (tag, weight) VALUES (?, ?)",
                ("タグ-preview", 2.0),
            )
            connection.commit()
        preview = preview_module()
        self.hold_lock_in_another_process(lock_path_of(config))
        with patch.object(preview, "load_config", return_value=config):
            with self.assertRaises(SystemExit):
                run(preview.run(preview.parse_args(["--tags", "タグ-preview"])))
        self.assertEqual(ledger_usage(self.folder), {}, "被锁挡下的预览不得消耗请求")


# ---------------------------------------------------------------------------
# R3: the whole explicit-tag preview path is read-only about the database
# ---------------------------------------------------------------------------

class ExplicitPreviewExistenceTests(_NativeCase):
    def test_missing_database_aborts_without_creating_it(self):
        config = native_config(self.folder)
        db.DB_PATH = self.folder / "must-not-create.sqlite3"
        preview = preview_module()

        async def scenario():
            calls = []
            runtime = self.build(preview._effective_config(
                preview.parse_args(["--tags", "タグ-preview"]), config,
            ))
            wire(runtime, calls)
            with patch.object(preview, "load_config", return_value=config):
                with self.assertRaises(SystemExit):
                    await preview.run(preview.parse_args(["--tags", "タグ-preview"]))
            return calls

        calls = run(scenario())
        self.assertFalse(Path(db.DB_PATH).exists(), "只读预览不得创建数据库文件")
        self.assertEqual(calls, [], "存在性检查失败后不得发起任何请求")

    def test_unknown_explicit_tag_aborts_and_leaves_the_file_untouched(self):
        config = native_config(self.folder)
        with sqlite3.connect(db.DB_PATH) as connection:
            connection.execute(
                "INSERT INTO xp_profile (tag, weight) VALUES (?, ?)", ("タグ-known", 1.0),
            )
            connection.commit()
        before = hashlib.sha256(Path(db.DB_PATH).read_bytes()).hexdigest()
        preview = preview_module()

        async def scenario():
            calls = []
            runtime = self.build(preview._effective_config(
                preview.parse_args(["--tags", "タグ-unknown"]), config,
            ))
            wire(runtime, calls)
            with patch.object(preview, "load_config", return_value=config):
                with self.assertRaises(SystemExit):
                    await preview.run(preview.parse_args(["--tags", "タグ-unknown"]))
            return calls

        calls = run(scenario())
        self.assertEqual(calls, [])
        self.assertEqual(hashlib.sha256(Path(db.DB_PATH).read_bytes()).hexdigest(), before)

    def test_known_explicit_tag_still_previews(self):
        config = native_config(self.folder)
        with sqlite3.connect(db.DB_PATH) as connection:
            connection.execute(
                "INSERT INTO xp_profile (tag, weight) VALUES (?, ?)", ("タグ-known", 2.0),
            )
            connection.commit()
        preview = preview_module()

        async def scenario():
            calls = []
            runtime = self.build(preview._effective_config(
                preview.parse_args(["--tags", "タグ-known"]), config,
            ))
            wire(runtime, calls)
            with patch.object(preview, "load_config", return_value=config):
                return await preview.run(preview.parse_args(["--tags", "タグ-known"])), calls

        report, calls = run(scenario())
        self.assertEqual(report["tags"][0]["status"], "accepted")
        self.assertEqual(calls, ["exa", "tavily"])
        self.assertTrue(report["quota_estimates"]["engines"])


# ---------------------------------------------------------------------------
# R4: the transport boundary and the maintenance fallback stay closed
# ---------------------------------------------------------------------------

class TransportBoundaryTests(_NativeCase):
    def test_malformed_response_header_is_deferred_without_any_token(self):
        """Real aiohttp, loopback only, with a synthetic marker (not a credential).

        ``tests/conftest.py`` refuses every AF_INET connection so no test can
        reach a provider.  This case re-enables the raw connect *only* for
        127.0.0.1/::1 so the genuine aiohttp header-parser failure can be
        produced against a local server; any other address still raises.
        """
        import _socket
        import socket

        raw_connect = _socket.socket.connect
        raw_connect_ex = _socket.socket.connect_ex
        allowed = []

        def loopback_connect(sock, address):
            host = str(address[0]) if isinstance(address, tuple) else ""
            if sock.family in (socket.AF_INET, socket.AF_INET6) and host not in {"127.0.0.1", "::1"}:
                raise AssertionError(f"只允许回环连接: {address!r}")
            allowed.append(address)
            return raw_connect(sock, address)

        def loopback_connect_ex(sock, address):
            host = str(address[0]) if isinstance(address, tuple) else ""
            if sock.family in (socket.AF_INET, socket.AF_INET6) and host not in {"127.0.0.1", "::1"}:
                raise AssertionError(f"只允许回环连接: {address!r}")
            return raw_connect_ex(sock, address)

        config = native_config(self.folder, audit=True)
        runtime = self.build(config)
        wire(runtime, [])
        engine = runtime.router._engines["exa"]
        client = getattr(engine, "_inner", engine)

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

        async def scenario():
            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            client.endpoint = f"http://127.0.0.1:{port}/"
            client._request_json = _aiohttp_request_json
            try:
                return await cm.ClassificationMaintenance(config).run_tags(
                    ["タグ-transport"],
                )
            finally:
                server.close()
                await server.wait_closed()

        with patch.object(socket.socket, "connect", loopback_connect), \
                patch.object(socket.socket, "connect_ex", loopback_connect_ex):
            summary = run(scenario())
        self.assertTrue(allowed, "测试必须真的连过本地回环套接字")
        self.assertTrue(all(str(address[0]) in {"127.0.0.1", "::1"} for address in allowed))
        item = summary["items"][0]
        self.assertEqual(item["status"], "deferred")
        self.assertEqual(summary["unresolved"], 0)

        persisted = run(db.get_state(cm.SUMMARY_STATE_KEY)) or ""
        audit = (self.folder / "audit.jsonl").read_text(encoding="utf-8")
        state = (self.folder / "state.json").read_text(encoding="utf-8")
        row = run(db.get_tag_classifications(["タグ-transport"])).get("タグ-transport")

        self.assertNotIn(TOKEN, json.dumps(summary, ensure_ascii=False))
        self.assertNotIn(TOKEN, persisted)
        self.assertNotIn(TOKEN, audit)
        self.assertNotIn(TOKEN, state)
        self.assertIsNone(row, "传输失败不得写入 unresolved 记录")
        self.assertNotIn("Invalid header", json.dumps(summary, ensure_ascii=False))
        self.assertIn("transport_error", audit)

    def test_unexpected_backend_failure_defers_without_raw_text_or_unresolved(self):
        config = native_config(self.folder)

        async def explode(_tag, _config):
            raise RuntimeError(f"upstream said {TOKEN} in a header")

        summary = run(cm.ClassificationMaintenance(config, classify=explode).run_tags(
            ["タグ-boom"],
        ))
        item = summary["items"][0]
        self.assertEqual(item["status"], "deferred")
        self.assertEqual(item["error_code"], "unexpected_error")
        self.assertEqual(summary["unresolved"], 0)
        self.assertNotIn(TOKEN, json.dumps(summary, ensure_ascii=False))
        self.assertNotIn(TOKEN, run(db.get_state(cm.SUMMARY_STATE_KEY)) or "")
        self.assertIsNone(run(db.get_tag_classifications(["タグ-boom"])).get("タグ-boom"))


# ---------------------------------------------------------------------------
# D2: auditing is a logging concern, never a quota/ordering concern
# ---------------------------------------------------------------------------

class AuditWrapperContractTests(_NativeCase):
    def test_audit_switch_does_not_change_order_or_cli_report(self):
        from native_jev_judge import estimate_quota_status
        from search_grounded_judge import MonthlyQuotaUsageLedger

        preview = preview_module()
        results = {}
        for audit in (False, True):
            folder = self.folder / ("audit-on" if audit else "audit-off")
            folder.mkdir(parents=True, exist_ok=True)
            config = native_config(folder, audit=audit)
            MonthlyQuotaUsageLedger(folder / "quota.json").save([
                {"pool_id": "native_jev:exa", "requests_used": 99},
                {"pool_id": "native_jev:tavily", "requests_used": 0},
            ])
            runtime = self.build(config)
            statuses = preview._pool_statuses(runtime)
            results[audit] = {
                "order": runtime.router.primary_order(),
                "statuses": statuses,
                "estimate": estimate_quota_status(statuses, checked_at="t"),
            }

        self.assertEqual(results[False]["order"], ("tavily", "exa"))
        self.assertEqual(results[True]["order"], results[False]["order"])
        self.assertEqual(results[True]["statuses"], results[False]["statuses"])
        self.assertEqual(results[True]["estimate"], results[False]["estimate"])
        self.assertTrue(results[True]["statuses"], "开启审计后 CLI 配额报告不得变空")


if __name__ == "__main__":
    unittest.main()

"""Queue-pause hardening round (candidate jev-queue-fixes).

These tests pin the behaviours the independent review of ``jev-queue-pause``
rejected.  Every test drives the real entry points: the real SQLite schema via
``database``, the real on-disk native state file and the real maintenance /
CLI code.  Only the HTTP requester of the transport clients is replaced, with
the same signature the production client uses, and the fixture in
``tests/conftest.py`` forbids any AF_INET connection.

Contract under test:

* a single-tag manual retry is a *one-shot bypass*, not a permanent resume:
  infrastructure deferrals, exceptions and cancellation keep the original
  pause/since/attempts untouched, and only a successful classification clears
  the pause;
* a second ``hold`` keeps ``since`` and increments ``attempts``; a failed
  (deferred) attempt does not increment anything;
* the execution boundary re-checks the pause after the task lock is held and
  the snapshot refreshed, and the bypass is consumed by exactly one execution,
  so a batch, ``run_tags`` or a reviewed batch never spends a request on a
  paused tag;
* queue selection has no permanent scan cap: pages are bounded but the scan
  advances to the requested size or to the end of the database, and the two
  read-only queue previews share one implementation;
* an automatic profile run excludes only tags with an explicit classification;
  an ``unresolved`` row without pause/attempt evidence stays selectable;
* queue inspection never creates a database file.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import pytest

import classification_maintenance as maintenance
import database as db
import native_jev_judge as native
from classification_maintenance import ClassificationMaintenance
from grounded_judge import GroundedJudgeDeferredError

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# fixtures / doubles
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_script", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(state_path: Path, *, cap: int = 5, ttl_days: int = 30) -> dict:
    return {
        "providers": {
            "offline_exa": {"type": "exa_search", "api_key": "offline-fixture-only"},
            "offline_tavily": {"type": "tavily_search", "api_key": "offline-fixture-only"},
            "offline_jev": {"type": "openai_compatible", "api_key": "offline-fixture-only",
                            "base_url": "https://openrouter.invalid/api/v1"},
        },
        "tag_classifier": {
            "ttl_days": ttl_days,
            "maintenance": {"max_tags_per_run": cap, "concurrency": 1,
                            "min_profile_weight": 0.0,
                            "allow_automatic_external_search": True},
            "grounded_judge": {"backend": "native_jev", "native_jev": {
                "enabled": True,
                "state_path": str(state_path),
                "quota_state_path": str(state_path.parent / "quota.json"),
                "lock_path": str(state_path.parent / "native.lock"),
                "lock_timeout_seconds": 0.5,
                "per_run_request_budget": 200,
                "exa_providers": ["offline_exa"], "tavily_providers": ["offline_tavily"],
                "jev_provider": "offline_jev",
            }},
        },
    }


def _state_path(config: dict) -> Path:
    return Path(config["tag_classifier"]["grounded_judge"]["native_jev"]["state_path"])


def _seed(weights: dict[str, float], *, unresolved_age: int | None = None) -> None:
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO xp_profile (tag, weight) VALUES (?, ?)",
            list(weights.items()),
        )
        if unresolved_age is not None:
            connection.executemany(
                "INSERT OR REPLACE INTO tag_classification_cache "
                "(normalized_tag, classification, source, updated_at) "
                "VALUES (?, 'unresolved', 'ai', datetime('now', ?))",
                [(tag, f"-{unresolved_age} days") for tag in weights],
            )


class Transport:
    """Same call signature as the production ``_aiohttp_request_json``."""

    def __init__(self) -> None:
        self.mode = "hold"
        self.calls: list[str] = []
        self.reached = asyncio.Event()

    async def __call__(self, method, url, headers, *, json_body, timeout_seconds):
        assert method == "POST"
        assert isinstance(headers, dict) and timeout_seconds > 0
        if "questions" in json_body:
            self.calls.append("jev")
            choice = "feature" if self.mode == "accept" else "unresolved"
            return 200, {"answers": {"category": {"choice": choice, "confidence": 0.99}}}, {}
        engine = "exa" if "numResults" in json_body else "tavily"
        self.calls.append(engine)
        self.reached.set()
        if self.mode == "outage":
            return 503, {"error": "offline simulated service outage"}, {}
        if self.mode == "hang":
            await asyncio.sleep(30)
        tag = json_body["query"].split('"')[1]
        return 200, {"results": [{
            "url": f"https://{engine}.example.test/tag", "title": tag,
            "text": tag + " feature", "content": tag + " feature",
        }]}, {}


@pytest.fixture
def transport(monkeypatch) -> Transport:
    stub = Transport()
    original = native.build_configured_native_jev_judge

    def build(config):
        runtime = original(config)
        for client in runtime.router._engines.values():
            client._request_json = stub
        runtime.jev._request_json = stub
        return runtime

    monkeypatch.setattr(native, "build_configured_native_jev_judge", build)
    native._NATIVE_RUNTIMES.clear()
    return stub


def _pause_of(config: dict, tag: str) -> dict | None:
    return native.NativeJevStateStore(_state_path(config)).pause_entry(tag)


def _hold(manager, tag: str = "mystery") -> dict:
    """First complete attempt without a verdict: creates the durable pause."""
    summary = run(manager.run_tags([tag]))
    assert summary["unresolved"] == 1, summary
    entry = _pause_of(manager.config, tag)
    assert entry is not None and entry["attempts"] == 1
    return entry


def _expire_cooldowns(config: dict) -> None:
    """Advance only the documented backoff, leaving the pause untouched."""
    path = _state_path(config)
    payload = json.loads(path.read_text())
    for key, value in payload.get("cooldowns", {}).items():
        if key.startswith("engine:") or key.startswith("tag:"):
            value["until"] = time.time() - 1
    path.write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# 1. manual retry: one-shot bypass, never an early permanent resume
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("warm", [False, True])
def test_infra_deferral_keeps_the_original_pause(tmp_path, transport, warm):
    config = _config(tmp_path / "state.json")
    _seed({"mystery": 10})
    manager = ClassificationMaintenance(config)
    original = _hold(manager)
    if not warm:
        native._NATIVE_RUNTIMES.clear()
    transport.mode = "outage"
    transport.calls.clear()
    with pytest.raises(GroundedJudgeDeferredError):
        run(manager.classify_tag("mystery"))
    after = _pause_of(config, "mystery")
    print("INFRA_DEFERRAL", {"warm": warm, "requests": transport.calls,
                             "before": original, "after": after})
    assert after == original, "a deferred retry must not touch the durable pause"
    assert transport.calls, "the explicitly requested retry must actually have been attempted"


def test_cancelled_retry_keeps_the_original_pause(tmp_path, transport):
    config = _config(tmp_path / "state.json")
    _seed({"mystery": 10})
    manager = ClassificationMaintenance(config)
    original = _hold(manager)
    transport.mode = "hang"
    transport.reached.clear()

    async def scenario():
        task = asyncio.create_task(manager.classify_tag("mystery"))
        await asyncio.wait_for(transport.reached.wait(), timeout=5)
        await asyncio.sleep(0.01)  # the search request is in flight now
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    after = _pause_of(config, "mystery")
    print("CANCELLED_RETRY", {"before": original, "after": after})
    assert after == original, "cancellation must not drop the pause"


def test_repeated_hold_keeps_since_and_increments_attempts(tmp_path, transport):
    config = _config(tmp_path / "state.json")
    _seed({"mystery": 10})
    manager = ClassificationMaintenance(config)
    first = _hold(manager)
    time.sleep(0.01)
    transport.calls.clear()
    with pytest.raises(ValueError):
        run(manager.classify_tag("mystery"))
    second = _pause_of(config, "mystery")
    print("ATTEMPT_AUDIT", {"first": first, "second": second, "requests": transport.calls})
    assert second["attempts"] == first["attempts"] + 1
    assert second["since"] == first["since"]
    assert second["last_attempt_at"] > first["last_attempt_at"]
    assert len(transport.calls) == 4  # exactly one more complete attempt


def test_network_failure_does_not_increment_attempts(tmp_path, transport):
    config = _config(tmp_path / "state.json")
    _seed({"mystery": 10})
    manager = ClassificationMaintenance(config)
    first = _hold(manager)
    transport.mode = "outage"
    with pytest.raises(GroundedJudgeDeferredError):
        run(manager.classify_tag("mystery"))
    assert _pause_of(config, "mystery") == first


def test_successful_single_retry_clears_only_that_pause(tmp_path, transport):
    config = _config(tmp_path / "state.json")
    _seed({"mystery": 10, "other": 9})
    manager = ClassificationMaintenance(config)
    first = _hold(manager)
    native.NativeJevStateStore(_state_path(config)).pause("other", "no_evidence")
    transport.mode = "accept"
    result = run(manager.classify_tag("mystery"))
    print("SINGLE_RETRY_OK", {"result": result.get("status"), "cleared": result.get("resumed_pause")})
    assert result["status"] == "accepted"
    assert result["resumed_pause"]["reason"] == first["reason"]
    state = native.NativeJevStateStore(_state_path(config))
    assert not state.is_paused("mystery")
    assert state.is_paused("other")


def test_bypass_is_consumed_by_one_execution_only(tmp_path, transport):
    """The token allows exactly one attempt: a following batch stays deferred."""
    config = _config(tmp_path / "state.json")
    _seed({"mystery": 10})
    manager = ClassificationMaintenance(config)
    _hold(manager)
    transport.calls.clear()
    with pytest.raises(ValueError):  # this attempt re-holds the tag
        run(manager.classify_tag("mystery"))
    assert len(transport.calls) == 4
    transport.calls.clear()
    _expire_cooldowns(config)
    summary = run(manager.run_tags(["mystery"]))
    print("BYPASS_ONCE", {"summary": summary, "requests": transport.calls})
    assert summary["attempted"] == 1 and summary["deferred"] == 1
    assert not transport.calls


# ---------------------------------------------------------------------------
# 2. batch entries never bypass a durable pause
# ---------------------------------------------------------------------------

def test_run_tags_never_retries_a_paused_tag_after_its_cooldown_expires(tmp_path, transport):
    config = _config(tmp_path / "state.json")
    _seed({"mystery": 10})
    manager = ClassificationMaintenance(config)
    _hold(manager)
    _expire_cooldowns(config)
    transport.calls.clear()
    summary = run(manager.run_tags(["mystery"]))
    entry = _pause_of(config, "mystery")
    print("RUN_TAGS_PAUSED", {"summary": summary, "requests": transport.calls, "pause": entry})
    assert not transport.calls
    assert summary["deferred"] == 1
    assert entry["attempts"] == 1


def test_reviewed_batch_rechecks_the_pause_after_its_preview(tmp_path, transport, monkeypatch):
    config = _config(tmp_path / "state.json")
    _seed({"mystery": 10})
    manager = ClassificationMaintenance(config)
    _state_path(config).write_text(json.dumps({"cache": {}, "cooldowns": {
        "tag:mystery": {"reason": "model_unresolved", "until": time.time() - 1000}}}))
    migration = _load_script("maintain_high_weight_tags")
    original_preview = manager.preview

    async def pause_lands_after_preview(limit):
        rows = await original_preview(limit)
        imported = migration.import_legacy_pauses(config, apply=True)
        assert imported["applied"] and imported["imported"][0]["tag"] == "mystery"
        return rows

    monkeypatch.setattr(manager, "preview", pause_lands_after_preview)
    summary = run(manager.run_reviewed(["mystery"]))
    print("REVIEWED_TOCTOU", {"summary": summary, "requests": transport.calls})
    assert not transport.calls, "the reviewed batch ignored a pause authored after its preview"


# ---------------------------------------------------------------------------
# 3. no permanent scan cap; one shared selection for both read-only previews
# ---------------------------------------------------------------------------

def _pause_entries(tags, now: float) -> dict:
    return {
        tag: {"reason": "model_unresolved", "source": "native_hold", "since": now,
              "last_attempt_at": now, "attempts": 1}
        for tag in tags
    }


def test_thousand_pauses_do_not_starve_the_batch(tmp_path):
    config = _config(tmp_path / "state.json")
    _seed({f"tag{i:04d}": float(2000 - i) for i in range(1, 1006)})
    now = time.time()
    _state_path(config).write_text(json.dumps({
        "cache": {}, "cooldowns": {},
        "pauses": _pause_entries([f"tag{i:04d}" for i in range(1, 1001)], now),
    }))
    selected = [row["tag"] for row in run(ClassificationMaintenance(config).preview(5))]
    print("THOUSAND_PAUSES", {"selected": selected})
    assert selected == ["tag1001", "tag1002", "tag1003", "tag1004", "tag1005"]


def test_read_only_previews_share_one_selection(tmp_path):
    config = _config(tmp_path / "state.json")
    _seed({f"tag{i:04d}": float(2000 - i) for i in range(1, 206)})
    now = time.time()
    _state_path(config).write_text(json.dumps({
        "cache": {}, "cooldowns": {},
        "pauses": _pause_entries([f"tag{i:04d}" for i in range(1, 201)], now),
    }))
    args = _load_script("preview_native_jev").parse_args([])
    cli_tags = run(_load_script("preview_native_jev")._select_tags(args, config))
    manager_tags = [row["tag"] for row in run(ClassificationMaintenance(config).preview(5))]
    print("SHARED_SELECTION", {"cli": cli_tags, "maintenance": manager_tags})
    assert cli_tags == manager_tags == ["tag0201", "tag0202", "tag0203", "tag0204", "tag0205"]


# ---------------------------------------------------------------------------
# 4. automatic profile selection: only explicit classifications are excluded
# ---------------------------------------------------------------------------

def test_recent_unresolved_row_stays_selectable_for_automatic_profile(tmp_path):
    config = _config(tmp_path / "state.json")
    _seed({"never_attempted": 10}, unresolved_age=1)
    manager = ClassificationMaintenance(config)
    preview = [row["tag"] for row in run(manager.preview(5))]
    selected = run(manager._select_profile_tags({"never_attempted": 10}))
    print("HISTORY_UNRESOLVED", {"preview": preview, "profile": selected})
    assert preview == selected == ["never_attempted"]


def test_explicit_classification_still_wins_over_unresolved_priority(tmp_path):
    config = _config(tmp_path / "state.json")
    _seed({"resolved": 10, "pending": 5}, unresolved_age=1)
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.execute(
            "UPDATE tag_classification_cache SET classification = 'feature' WHERE normalized_tag = 'resolved'"
        )
    manager = ClassificationMaintenance(config)
    selected = run(manager._select_profile_tags({"resolved": 10, "pending": 5}))
    print("PROFILE_EXPLICIT_ONLY", {"selected": selected})
    assert selected == ["pending"]


# ---------------------------------------------------------------------------
# 5. queue inspection never creates a database
# ---------------------------------------------------------------------------

def test_manager_preview_does_not_create_a_missing_database(tmp_path, monkeypatch):
    config = _config(tmp_path / "state.json")
    missing = tmp_path / "absent" / "missing.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", missing)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        run(ClassificationMaintenance(config).preview(5))
    print("PREVIEW_READONLY", {"created": missing.exists()})
    assert not missing.exists()


def test_maintain_cli_selection_does_not_initialize_a_database(tmp_path, monkeypatch):
    config = _config(tmp_path / "state.json")
    script = _load_script("maintain_high_weight_tags")
    monkeypatch.setattr(script, "load_config", lambda *a, **k: config)
    missing = tmp_path / "fresh" / "missing.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", missing)
    result = run(script.select_candidates(5, 0))
    print("MAINTAIN_READONLY", {"returned": result, "created": missing.exists()})
    assert result == [] and not missing.exists()


def test_read_only_preview_reports_a_schema_mismatch_without_writing(tmp_path, monkeypatch):
    config = _config(tmp_path / "state.json")
    empty = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty).close()  # a real file, but no schema at all
    monkeypatch.setattr(db, "DB_PATH", empty)
    before = empty.read_bytes()
    with pytest.raises((db.DatabaseSchemaMismatchError, sqlite3.OperationalError)):
        run(ClassificationMaintenance(config).preview(5))
    assert empty.read_bytes() == before


# ---------------------------------------------------------------------------
# 6. the paid include-paused diagnostic stays an isolated, synthetic case
# ---------------------------------------------------------------------------

def test_preview_include_paused_is_a_paid_but_isolated_diagnostic(tmp_path, transport, monkeypatch):
    config = _config(tmp_path / "state.json")
    _seed({"mystery": 10})
    store = native.NativeJevStateStore(_state_path(config))
    store.pause("mystery", "model_unresolved")
    store.cool("tag:mystery", 86400, "model_unresolved")
    before = _state_path(config).read_bytes()
    script = _load_script("preview_native_jev")
    monkeypatch.setattr(script, "load_config", lambda *a, **k: config)
    report = run(script.run(script.parse_args(["--include-paused"])))
    print("INCLUDE_PAUSED", {"requests": transport.calls, "status": report["tags"][0]["status"],
                             "scope": report["state"]["scope"]})
    assert len(transport.calls) == 4
    assert _state_path(config).read_bytes() == before
    assert Path(str(_state_path(config)) + ".preview").exists()
    assert report["state"]["scope"] == "preview_only"

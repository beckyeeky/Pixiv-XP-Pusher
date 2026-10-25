"""Queue starvation and automatic-pause regressions (candidate jev-queue-pause).

Every test drives the *real* entry point: the real SQLite schema through
``database`` and the real on-disk native state file.  Only search/System One is
replaced by a fake router/jev, and no production database, config, credential or
network is touched.

The contract under test:

* only a completed native classification attempt without a verdict (a native
  ``hold``) records a durable automatic pause;
* deferred/error/quota/lock outcomes never pause;
* every automatic selection entry (``preview``/``run_eligible``/``run_profile``)
  excludes paused *and* cooling tags before applying its limit, and backfills
  from lower-weight candidates instead of returning a short batch;
* a pause never expires because the 24h cooldown or the decision-cache TTL
  expired;
* one explicitly clicked tag may be retried, and only that tag is resumed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import classification_maintenance as maintenance
import database
import native_jev_judge as native
from classification_maintenance import ClassificationMaintenance


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

def _config(state_path: Path, *, max_tags: int = 5, min_weight: float = 1.0) -> dict:
    return {
        "tag_classifier": {
            "ttl_days": 30,
            "grounded_judge": {
                "backend": "native_jev",
                "native_jev": {"state_path": str(state_path)},
            },
            "maintenance": {
                "max_tags_per_run": max_tags,
                "min_profile_weight": min_weight,
                "concurrency": 2,
                "allow_automatic_external_search": True,
            },
        }
    }


def _seed_profile(weights: dict[str, float]) -> None:
    connection = sqlite3.connect(database.DB_PATH)
    try:
        connection.executemany(
            "INSERT OR REPLACE INTO xp_profile (tag, weight) VALUES (?, ?)",
            list(weights.items()),
        )
        connection.commit()
    finally:
        connection.close()


def _seed_unresolved(tag: str, *, age_days: int, source: str = "ai") -> None:
    connection = sqlite3.connect(database.DB_PATH)
    try:
        connection.execute(
            """
            INSERT OR REPLACE INTO tag_classification_cache
                (normalized_tag, classification, source, updated_at)
            VALUES (?, 'unresolved', ?, datetime('now', ?))
            """,
            (tag, source, f"-{age_days} days"),
        )
        connection.commit()
    finally:
        connection.close()


def _write_state(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _tags(items: list[dict]) -> list[str]:
    return [item["tag"] for item in items]


def _response(engine: str = "exa", mentioned: str = "サンプルタグ"):
    from search_grounded_judge import SearchResponse

    return SearchResponse(
        provider=engine, pool_id=f"{engine}-1", query="q",
        sources=[{"url": f"https://example.test/{engine}", "title": "t"}],
        snippets=[f"{mentioned} は特徴です。"], usage={},
    )


def _evidence(engine: str = "exa"):
    from native_jev_judge import SearchEvidence

    return SearchEvidence.from_response(_response(engine, "mystery_tag"), "mystery_tag")


class FakeJev:
    def __init__(self, category: str = "feature", confidence: float = 0.98):
        self._category = category
        self._confidence = confidence

    async def classify(self, tag, evidence):
        from native_jev_judge import JevChoice

        return JevChoice(self._category, self._confidence, {}, "fake/jev", {})


class FakeRouter:
    def __init__(self, evidences, trace=None):
        self._evidences = list(evidences)
        self._trace = list(trace or [])

    async def gather(self, tag, query):
        from native_jev_judge import RouterResult

        return RouterResult(evidences=list(self._evidences), trace=list(self._trace))

    def fallback_engines(self):
        return ()


def _policy():
    from native_jev_judge import NativeJevPolicy

    return NativeJevPolicy(min_confidence=0.9, min_agreeing_sources=2)


# ---------------------------------------------------------------------------
# 1. selection excludes pauses/cooling before the limit, then backfills
# ---------------------------------------------------------------------------

def test_paused_high_weight_tags_are_skipped_and_backfilled(tmp_path):
    state_path = tmp_path / "native.json"
    _seed_profile({f"tag{index:02d}": float(100 - index) for index in range(1, 21)})
    store = native.NativeJevStateStore(state_path)
    for index in range(1, 11):
        assert store.pause(f"tag{index:02d}", "insufficient_agreement")

    manager = ClassificationMaintenance(_config(state_path))
    selected = asyncio.run(manager.preview(5))

    assert _tags(selected) == ["tag11", "tag12", "tag13", "tag14", "tag15"]


def test_run_eligible_reports_paused_and_cooling_skips(tmp_path):
    state_path = tmp_path / "native.json"
    _seed_profile({f"tag{index:02d}": float(100 - index) for index in range(1, 9)})
    store = native.NativeJevStateStore(state_path)
    store.pause("tag01", "no_evidence")
    store.pause("tag02", "model_unresolved")
    store.cool("tag:tag03", 3600, "low_relevance")

    async def classify(tag, _config):
        return {"tag": tag, "status": "accepted", "usage": {}}

    manager = ClassificationMaintenance(_config(state_path), classify=classify)
    summary = asyncio.run(manager.run_eligible(3))

    attempted = [item["tag"] for item in summary["items"]]
    assert attempted == ["tag04", "tag05", "tag06"]
    assert summary["skipped_paused"] == 2
    assert summary["skipped_cooling"] == 1


def test_preview_returns_only_available_items_when_fewer_than_limit(tmp_path):
    state_path = tmp_path / "native.json"
    _seed_profile({"high_a": 9.0, "high_b": 8.0, "low_a": 3.0, "low_b": 2.0})
    store = native.NativeJevStateStore(state_path)
    store.pause("high_a", "no_evidence")
    store.pause("high_b", "low_relevance")

    selected = asyncio.run(ClassificationMaintenance(_config(state_path)).preview(5))

    assert _tags(selected) == ["low_a", "low_b"]


def test_profile_selection_excludes_paused_tags_and_still_fills_the_batch(tmp_path, monkeypatch):
    state_path = tmp_path / "native.json"
    store = native.NativeJevStateStore(state_path)
    store.pause("paused_high", "conflicting_classification")
    manager = ClassificationMaintenance(
        _config(state_path, max_tags=2), classify=AsyncMock(),
    )
    monkeypatch.setattr(
        database, "get_tag_classifications", AsyncMock(return_value={}),
    )
    run_summary = {"attempted": 2, "items": []}
    run_tags = AsyncMock(return_value=run_summary)
    monkeypatch.setattr(maintenance, "run_scheduled_maintenance", run_tags)

    result = asyncio.run(manager.run_profile(
        {"paused_high": 50.0, "next_high": 9.0, "next_low": 3.0},
    ))

    assert result["skipped_paused"] == 1
    assert result["skipped_cooling"] == 0
    assert run_tags.await_args.args[0] == ["next_high", "next_low"]


# ---------------------------------------------------------------------------
# 2. pause durability and expiry
# ---------------------------------------------------------------------------

def test_pause_survives_a_process_restart(tmp_path):
    state_path = tmp_path / "native.json"
    _seed_profile({"paused": 10.0, "fresh": 2.0})
    native.NativeJevStateStore(state_path).pause("paused", "insufficient_agreement")

    restarted_store = native.NativeJevStateStore(state_path)
    assert restarted_store.is_paused("paused")

    manager = ClassificationMaintenance(_config(state_path))
    assert _tags(asyncio.run(manager.preview(5))) == ["fresh"]


def test_expired_cooldown_and_cache_ttl_do_not_resume_a_pause(tmp_path):
    state_path = tmp_path / "native.json"
    _seed_profile({"paused": 10.0, "fresh": 2.0})
    now = time.time()
    _write_state(state_path, {
        "schema_version": 1,
        "cache": {"paused": {"expires_at": now - 999, "decision": {"classification": "feature"}}},
        "cooldowns": {"tag:paused": {"until": now - 999, "reason": "no_evidence"}},
        "pauses": {"paused": {
            "reason": "no_evidence", "source": "native_hold",
            "since": now - 200000, "last_attempt_at": now - 200000, "attempts": 1,
        }},
    })

    store = native.NativeJevStateStore(state_path)
    assert store.is_paused("paused")
    assert not store.is_cooling("tag:paused")

    manager = ClassificationMaintenance(_config(state_path))
    assert _tags(asyncio.run(manager.preview(5))) == ["fresh"]
    summary = asyncio.run(manager.run_eligible(5))
    assert "paused" not in [item["tag"] for item in summary["items"]]
    assert summary["skipped_paused"] == 1
    assert summary["skipped_cooling"] == 0


# ---------------------------------------------------------------------------
# 3. only a real native hold pauses
# ---------------------------------------------------------------------------

def test_native_hold_records_a_durable_pause_without_an_expiry(tmp_path):
    state_path = tmp_path / "native.json"
    store = native.NativeJevStateStore(state_path, clock=lambda: 1000.0)
    judge = native.NativeJevJudge(
        FakeRouter([_evidence("exa"), _evidence("tavily")]),
        FakeJev("unresolved", 0.99), _policy(), state=store, clock=lambda: 1000.0,
    )

    result = asyncio.run(judge.evaluate("mystery_tag"))

    assert result["status"] == "hold"
    assert result["automatic_pause"] is True
    entry = store.pause_entry("mystery_tag")
    assert entry is not None
    assert entry["reason"] == "model_unresolved"
    assert entry["source"] == "native_hold"
    assert "until" not in entry  # a pause is not a timed retry
    assert set(entry) <= {
        "reason", "source", "since", "last_attempt_at", "attempts", "imported_from",
    }
    assert native.NativeJevStateStore(state_path).is_paused("mystery_tag")


def test_search_quota_failure_defers_without_pausing(tmp_path):
    state_path = tmp_path / "native.json"
    store = native.NativeJevStateStore(state_path)
    judge = native.NativeJevJudge(
        FakeRouter([], trace=[{"engine": "exa", "outcome": "quota_exhausted"}]),
        FakeJev(), _policy(), state=store,
    )

    result = asyncio.run(judge.evaluate("mystery_tag"))

    assert result["status"] == "deferred"
    assert result["reason"] == "search_unavailable"
    assert store.pause_entry("mystery_tag") is None
    assert not store.is_cooling("tag:mystery_tag")


def test_network_exception_defers_without_creating_a_state_file(tmp_path):
    state_path = tmp_path / "native.json"
    _seed_profile({"mystery_tag": 5.0})

    class ExplodingRouter:
        async def gather(self, tag, query):
            raise OSError("network down")

    judge = native.NativeJevJudge(
        ExplodingRouter(), FakeJev(), _policy(),
        state=native.NativeJevStateStore(state_path),
    )
    runtime = native.ConfiguredNativeJevJudge(
        judge, policy=judge._policy, jev=judge._jev, router=judge._router,
        state=judge._state,
    )

    async def classify(tag, config):
        return await runtime.classify(tag)

    manager = ClassificationMaintenance(_config(state_path), classify=classify)
    summary = asyncio.run(manager.run_eligible(5))

    assert summary["deferred"] == 1
    assert summary["attempted"] == 1
    assert not state_path.exists()
    assert not native.NativeJevStateStore(state_path).is_paused("mystery_tag")


@pytest.mark.parametrize(
    "reason",
    ["budget_exhausted", "task_lock_timeout", "internal_error", "jev_rate_limited",
     "search_unavailable", "engine_cooldown", "insufficient_engines_configured"],
)
def test_non_hold_reasons_never_pause(tmp_path, reason):
    state_path = tmp_path / "native.json"
    store = native.NativeJevStateStore(state_path)

    assert store.pause("mystery_tag", reason) is None
    assert store.pause_entry("mystery_tag") is None


# ---------------------------------------------------------------------------
# 4. explicit single-tag retry vs. batch entries
# ---------------------------------------------------------------------------

def test_manual_single_tag_click_retries_only_that_tag(tmp_path):
    state_path = tmp_path / "native.json"
    _seed_profile({"paused_one": 9.0, "paused_two": 8.0})
    store = native.NativeJevStateStore(state_path)
    store.pause("paused_one", "no_evidence")
    store.pause("paused_two", "insufficient_agreement")

    classify = AsyncMock(return_value={
        "tag": "paused_one", "status": "accepted",
        "classification": "feature", "usage": {},
    })
    manager = ClassificationMaintenance(_config(state_path), classify=classify)
    result = asyncio.run(manager.classify_tag("paused_one"))

    assert result["status"] == "accepted"
    assert result["resumed_pause"]["reason"] == "no_evidence"
    reloaded = native.NativeJevStateStore(state_path)
    assert not reloaded.is_paused("paused_one")
    assert reloaded.is_paused("paused_two")  # the other paused tag is untouched
    # Only the clicked tag became eligible again; the other one still is not.
    assert _tags(asyncio.run(manager.preview(5))) == ["paused_one"]


def test_reviewed_batch_does_not_silently_retry_paused_tags(tmp_path):
    state_path = tmp_path / "native.json"
    _seed_profile({"paused_one": 9.0, "fresh": 4.0})
    native.NativeJevStateStore(state_path).pause("paused_one", "no_evidence")
    manager = ClassificationMaintenance(_config(state_path), classify=AsyncMock())

    with pytest.raises(ValueError, match="no longer eligible"):
        asyncio.run(manager.run_reviewed(["paused_one"]))

    assert native.NativeJevStateStore(state_path).is_paused("paused_one")


def test_preview_never_resumes_or_writes_pause_state(tmp_path):
    state_path = tmp_path / "native.json"
    _seed_profile({"paused_one": 9.0, "fresh": 4.0})
    native.NativeJevStateStore(state_path).pause("paused_one", "no_evidence")
    before = state_path.read_bytes()

    asyncio.run(ClassificationMaintenance(_config(state_path)).preview(5))

    assert state_path.read_bytes() == before
    assert "resumed" not in json.loads(before)


def test_preview_does_not_create_a_missing_state_file(tmp_path):
    state_path = tmp_path / "missing.json"
    _seed_profile({"fresh": 4.0})

    assert _tags(asyncio.run(
        ClassificationMaintenance(_config(state_path)).preview(5)
    )) == ["fresh"]
    assert not state_path.exists()


# ---------------------------------------------------------------------------
# 5. untouched history and existing decisions
# ---------------------------------------------------------------------------

def test_historical_unresolved_without_attempt_evidence_stays_selectable(tmp_path):
    state_path = tmp_path / "native.json"
    _seed_profile({"historical": 6.0, "recent": 5.0, "never_seen": 4.0})
    _seed_unresolved("historical", age_days=60)
    _seed_unresolved("recent", age_days=1)

    selected = asyncio.run(ClassificationMaintenance(_config(state_path)).preview(5))

    # The highest-weight queue does not apply the classification TTL, so a
    # 60-day-old unresolved row is still offered -- that is exactly why a pause
    # may never be inferred from "it is unresolved".
    assert _tags(selected) == ["historical", "recent", "never_seen"]
    assert not state_path.exists()  # a preview never fabricates attempt evidence


def test_pause_and_resume_never_rewrite_existing_classifications(tmp_path):
    state_path = tmp_path / "native.json"
    _seed_profile({"manual_tag": 9.0, "accepted_tag": 8.0})
    connection = sqlite3.connect(database.DB_PATH)
    try:
        connection.executemany(
            """
            INSERT OR REPLACE INTO tag_classification_cache
                (normalized_tag, classification, source, updated_at)
            VALUES (?, ?, ?, '2026-01-01 00:00:00')
            """,
            [("manual_tag", "feature", "manual"), ("accepted_tag", "character", "ai")],
        )
        connection.commit()
    finally:
        connection.close()

    store = native.NativeJevStateStore(state_path)
    store.pause("manual_tag", "no_evidence")
    store.resume("manual_tag")

    with sqlite3.connect(database.DB_PATH) as connection:
        rows = dict(connection.execute(
            "SELECT normalized_tag, classification || '|' || source || '|' || updated_at "
            "FROM tag_classification_cache"
        ))
    assert rows == {
        "manual_tag": "feature|manual|2026-01-01 00:00:00",
        "accepted_tag": "character|ai|2026-01-01 00:00:00",
    }


# ---------------------------------------------------------------------------
# 6. conservative migration from existing hold cooldowns
# ---------------------------------------------------------------------------

def test_legacy_hold_cooldowns_import_only_provable_holds(tmp_path):
    state_path = tmp_path / "native.json"
    now = time.time()
    _write_state(state_path, {
        "schema_version": 1,
        "cache": {},
        "cooldowns": {
            "tag:hold_active": {"until": now + 3600, "reason": "low_relevance"},
            "tag:hold_expired": {"until": now - 3600, "reason": "insufficient_agreement"},
            "tag:infra": {"until": now + 3600, "reason": "jev_rate_limited"},
            "engine:jev": {"until": now + 3600, "reason": "jev_rate_limited"},
        },
    })
    store = native.NativeJevStateStore(state_path)

    dry = store.import_legacy_holds()
    assert [item["tag"] for item in dry] == ["hold_active", "hold_expired"]
    assert store.pause_entry("hold_active") is None

    applied = store.import_legacy_holds(dry_run=False)
    assert [item["tag"] for item in applied] == ["hold_active", "hold_expired"]
    reloaded = native.NativeJevStateStore(state_path)
    assert reloaded.is_paused("hold_active")
    assert reloaded.is_paused("hold_expired")
    assert not reloaded.is_paused("infra")
    assert not reloaded.is_paused("jev")
    entry = reloaded.pause_entry("hold_expired")
    assert entry["source"] == "legacy_cooldown_import"
    assert entry["reason"] == "insufficient_agreement"
    assert entry["imported_from"] == "tag_cooldown"


def test_import_is_idempotent_and_keeps_existing_pauses(tmp_path):
    state_path = tmp_path / "native.json"
    now = time.time()
    _write_state(state_path, {
        "schema_version": 1,
        "cache": {},
        "cooldowns": {"tag:hold": {"until": now + 3600, "reason": "no_evidence"}},
        "pauses": {"already": {
            "reason": "low_relevance", "source": "native_hold",
            "since": now - 10, "last_attempt_at": now - 10, "attempts": 1,
        }},
    })
    store = native.NativeJevStateStore(state_path)
    store.import_legacy_holds(dry_run=False)
    first = store.pause_entry("hold")
    store.import_legacy_holds(dry_run=False)

    assert store.pause_entry("hold") == first  # no churn on a second import
    assert store.is_paused("already")


def test_pause_state_contains_no_credential_material(tmp_path):
    state_path = tmp_path / "native.json"
    store = native.NativeJevStateStore(state_path)
    store.pause("mystery_tag", "no_evidence")
    text = state_path.read_text(encoding="utf-8")

    assert "api_key" not in text
    assert "Bearer" not in text
    assert "sk-" not in text
    entry = json.loads(text)["pauses"]["mystery_tag"]
    assert entry["reason"] == "no_evidence"
    assert isinstance(entry["attempts"], int)

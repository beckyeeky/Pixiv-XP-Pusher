"""Additional queue-pause surfaces: review annotation, CLIs, migration entry."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import pytest

import classification_maintenance as maintenance
import database
import native_jev_judge as native
from classification_maintenance import ClassificationMaintenance

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(state_path: Path) -> dict:
    return {
        "tag_classifier": {
            "ttl_days": 30,
            "grounded_judge": {
                "backend": "native_jev",
                "native_jev": {"state_path": str(state_path), "lock_timeout_seconds": 0.2},
            },
            "maintenance": {"max_tags_per_run": 5, "min_profile_weight": 1.0},
        }
    }


def _seed(weights: dict[str, float]) -> None:
    connection = sqlite3.connect(database.DB_PATH)
    try:
        connection.executemany(
            "INSERT OR REPLACE INTO xp_profile (tag, weight) VALUES (?, ?)",
            list(weights.items()),
        )
        connection.commit()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# review-queue annotation (what the Web page renders)
# ---------------------------------------------------------------------------

def test_review_queue_annotation_marks_only_paused_rows(tmp_path):
    state_path = tmp_path / "native.json"
    native.NativeJevStateStore(state_path).pause("paused", "conflicting_classification")
    config = _config(state_path)
    items = [{"tag": "paused", "profile_weight": 9.0}, {"tag": "other", "profile_weight": 3.0}]

    annotated = maintenance.annotate_review_queue(items, config)

    assert annotated[0]["automatic_pause"]["reason"] == "conflicting_classification"
    assert annotated[0]["automatic_pause"]["source"] == "native_hold"
    assert "automatic_pause" not in annotated[1]
    # Input rows are copied, never mutated in place.
    assert "automatic_pause" not in items[0]


def test_review_queue_annotation_is_a_noop_without_state(tmp_path):
    items = [{"tag": "unknown_state"}]
    assert maintenance.annotate_review_queue(items, _config(tmp_path / "absent.json")) == items
    assert not (tmp_path / "absent.json").exists()


def test_pause_line_names_the_reason_without_claiming_a_retry(tmp_path):
    entry = native.NativeJevStateStore(tmp_path / "native.json").pause("t", "no_evidence")

    line = maintenance.pause_line(entry)

    assert "自动暂停" in line
    assert "no_evidence" in line
    assert "手动重试" in line
    assert "冷却" not in line


# ---------------------------------------------------------------------------
# maintenance CLI: conservative import of provable historical holds
# ---------------------------------------------------------------------------

def test_maintain_cli_import_is_a_dry_run_by_default(tmp_path, monkeypatch):
    script = _load_script("maintain_high_weight_tags")
    state_path = tmp_path / "native.json"
    monkeypatch.setattr(script, "load_config", lambda *a, **k: _config(state_path))
    now = time.time()
    state_path.write_text(json.dumps({
        "schema_version": 1, "cache": {},
        "cooldowns": {"tag:legacy": {"until": now - 10, "reason": "insufficient_agreement"}},
    }), encoding="utf-8")
    before = state_path.read_bytes()

    result = asyncio.run(script.run(script.parse_args(["--import-legacy-pauses"])))

    assert [item["tag"] for item in result["import"]["imported"]] == ["legacy"]
    assert result["import"]["applied"] is False
    assert state_path.read_bytes() == before


def test_maintain_cli_import_writes_only_after_confirm(tmp_path, monkeypatch):
    script = _load_script("maintain_high_weight_tags")
    state_path = tmp_path / "native.json"
    monkeypatch.setattr(script, "load_config", lambda *a, **k: _config(state_path))
    now = time.time()
    state_path.write_text(json.dumps({
        "schema_version": 1, "cache": {},
        "cooldowns": {
            "tag:legacy": {"until": now - 10, "reason": "no_evidence"},
            "engine:jev": {"until": now + 10, "reason": "jev_rate_limited"},
        },
    }), encoding="utf-8")

    result = asyncio.run(
        script.run(script.parse_args(["--import-legacy-pauses", "--confirm-import"]))
    )

    assert result["import"]["applied"] is True
    store = native.NativeJevStateStore(state_path)
    assert store.is_paused("legacy")
    assert not store.is_paused("jev")
    assert store.pause_entry("legacy")["imported_from"] == "tag_cooldown"


def test_maintain_cli_refuses_confirm_without_the_import_flag(tmp_path):
    script = _load_script("maintain_high_weight_tags")

    with pytest.raises(ValueError, match="--import-legacy-pauses"):
        asyncio.run(script.run(script.parse_args(["--confirm-import"])))


# ---------------------------------------------------------------------------
# preview CLI: a read-only batch entry obeys the same exclusions
# ---------------------------------------------------------------------------

def _preview_args(**overrides) -> argparse.Namespace:
    values = {"tags": None, "limit": 2, "min_weight": 1.0, "include_paused": False}
    values.update(overrides)
    return argparse.Namespace(**values)


def test_preview_cli_skips_paused_and_cooling_candidates(tmp_path):
    script = _load_script("preview_native_jev")
    state_path = tmp_path / "native.json"
    _seed({"paused_one": 9.0, "cooling_one": 8.0, "usable_one": 7.0, "usable_two": 6.0})
    store = native.NativeJevStateStore(state_path)
    store.pause("paused_one", "no_evidence")
    store.cool("tag:cooling_one", 3600, "low_relevance")
    before = state_path.read_bytes()

    selected = asyncio.run(script._select_tags(_preview_args(), _config(state_path)))

    assert selected == ["usable_one", "usable_two"]
    assert state_path.read_bytes() == before  # a preview never writes pause state


def test_preview_cli_explicit_tag_list_is_never_filtered(tmp_path):
    script = _load_script("preview_native_jev")
    state_path = tmp_path / "native.json"
    native.NativeJevStateStore(state_path).pause("paused_one", "no_evidence")

    selected = asyncio.run(script._select_tags(
        _preview_args(tags="paused_one", limit=2), _config(state_path),
    ))

    assert selected == ["paused_one"]


def test_preview_cli_include_paused_flag_is_opt_in(tmp_path):
    script = _load_script("preview_native_jev")
    state_path = tmp_path / "native.json"
    _seed({"paused_one": 9.0, "usable_one": 7.0})
    native.NativeJevStateStore(state_path).pause("paused_one", "no_evidence")

    selected = asyncio.run(script._select_tags(
        _preview_args(include_paused=True), _config(state_path),
    ))

    assert selected == ["paused_one", "usable_one"]


# ---------------------------------------------------------------------------
# run_eligible: paused/cooling never starve the batch, counters are honest
# ---------------------------------------------------------------------------

def test_run_eligible_counts_only_skips_seen_inside_the_scanned_window(tmp_path):
    state_path = tmp_path / "native.json"
    _seed({"high_ok": 5.0, "paused_low": 1.5})
    native.NativeJevStateStore(state_path).pause("paused_low", "no_evidence")

    async def classify(tag, _config):
        return {"tag": tag, "status": "accepted", "usage": {}}

    summary = asyncio.run(
        ClassificationMaintenance(_config(state_path), classify=classify).run_eligible(5)
    )

    # paused_low is inside the scanned window (only two candidates) so it is
    # counted, and the batch still processed the one available tag.
    assert summary["skipped_paused"] == 1
    assert [item["tag"] for item in summary["items"]] == ["high_ok"]

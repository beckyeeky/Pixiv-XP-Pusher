#!/usr/bin/env python3
"""Preview one batch of tags with the optional Native Jev backend.

The default mode is strictly read-only: it searches, asks System One per source
and prints the activation decision without touching the database.  `--apply`
is intentionally double-gated because this candidate is not licensed to write
production rows by itself.

Examples:
    python3 scripts/preview_native_jev.py --tags "魅惑の顔,キス"
    python3 scripts/preview_native_jev.py --limit 5 --min-weight 1.0 --output report.json
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from classification_maintenance import ClassificationMaintenance  # noqa: E402
from config import load_config  # noqa: E402
from native_jev_judge import (  # noqa: E402
    budget_semantics,
    build_configured_native_jev_judge,
    engine_pool_status,
    estimate_quota_status,
    native_task_lock_for_config,
    native_task_lock_path,
    refresh_native_runtime_snapshot,
)
from utils import normalize_tag  # noqa: E402

#: Preview decisions/cooldowns never share the production state file, while the
#: monthly usage ledger stays the one keyed by credential.
DEFAULT_PREVIEW_STATE_SUFFIX = ".preview"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--tags", help="comma separated tags; when omitted the review queue is read")
    parser.add_argument("--limit", type=int, default=5, help="max candidates from the queue (default: 5)")
    parser.add_argument("--min-weight", type=float, default=1.0, help="min |profile weight| (default: 1.0)")
    parser.add_argument("--output", type=Path, help="write the JSON report here (default: stdout)")
    parser.add_argument("--audit-log", type=Path, help="override native_jev.audit_log_path for this run")
    parser.add_argument("--state-path", type=Path, help="override native_jev.state_path for this run")
    parser.add_argument("--apply", action="store_true",
                        help="persist gated classifications (requires --confirm-apply)")
    parser.add_argument("--confirm-apply", action="store_true",
                        help="second explicit confirmation for --apply")
    return parser.parse_args(argv)


def _split_tags(value: str | None) -> list[str]:
    if not value:
        return []
    return list(dict.fromkeys(
        normalized for item in value.split(",") if (normalized := normalize_tag(item))
    ))


async def _select_tags(args: argparse.Namespace) -> list[str]:
    """Resolve the preview queue without ever writing to the database."""
    explicit = _split_tags(args.tags)
    if explicit:
        return explicit[: max(1, int(args.limit))]
    import database as db

    try:
        # Read-only connection: no init_db, no migration, no file creation.
        candidates = db.get_high_weight_unclassified_profile_tags_readonly(
            max(1, int(args.limit)), float(args.min_weight),
        )
    except db.DatabaseNotInitializedError as exc:
        raise SystemExit(f"只读预览中止：{exc}") from exc
    except db.DatabaseSchemaMismatchError as exc:
        raise SystemExit(f"只读预览中止：{exc}") from exc
    return [item["tag"] for item in candidates]


async def _assert_tags_exist_readonly(tags: list[str]) -> None:
    """Reject an explicit tag list the read-only database does not confirm.

    The check itself must never create the database: a missing file or an
    incompatible schema aborts the whole command before any request is spent.
    """
    import database as db

    try:
        found = db.profile_tags_exist_readonly(tags)
    except db.DatabaseNotInitializedError as exc:
        raise SystemExit(f"只读预览中止：{exc}") from exc
    except db.DatabaseSchemaMismatchError as exc:
        raise SystemExit(f"只读预览中止：{exc}") from exc
    missing = [tag for tag in tags if not found.get(tag)]
    if missing:
        shown = "、".join(missing[:10])
        more = f" 等 {len(missing)} 个" if len(missing) > 10 else ""
        raise SystemExit(f"只读预览中止：标签不在画像库中：{shown}{more}")


def preview_state_path(base: dict) -> str:
    """Separate cache/cooldown file for preview runs."""
    grounded = ((base.get("tag_classifier") or {}).get("grounded_judge") or {})
    native = grounded.get("native_jev") if isinstance(grounded, dict) else {}
    native = native if isinstance(native, dict) else {}
    production = str(native.get("state_path") or "data/native_jev_state.json")
    return production + DEFAULT_PREVIEW_STATE_SUFFIX


def _effective_config(args: argparse.Namespace, base: dict) -> dict:
    config = copy.deepcopy(base)
    grounded = config.setdefault("tag_classifier", {}).setdefault("grounded_judge", {})
    native = grounded.setdefault("native_jev", {})
    if args.audit_log:
        native["audit_log_path"] = str(args.audit_log)
    # The monthly usage ledger (quota_state_path) is intentionally NOT
    # overridden: usage is counted per credential, not per run mode.
    native["state_path"] = str(args.state_path) if args.state_path else preview_state_path(base)
    # ...and the task lock is deliberately the production one, so a preview and
    # a maintenance batch can never update the shared ledger at the same time.
    if not str(native.get("lock_path") or "").strip():
        native["lock_path"] = str(native_task_lock_path(base))
    return config


def _policy_report(policy) -> dict:
    return {
        "model": policy.model,
        "min_confidence": policy.min_confidence,
        "min_agreeing_sources": policy.min_agreeing_sources,
        "require_tag_mention": policy.require_tag_mention,
        "unresolved_cooldown_seconds": policy.unresolved_cooldown_seconds,
        "rate_limit_cooldown_seconds": policy.rate_limit_cooldown_seconds,
        "decision_cache_ttl_seconds": policy.decision_cache_ttl_seconds,
        "per_run_request_budget": policy.per_run_request_budget,
        "max_concurrent_requests": policy.max_concurrent_requests,
        "top_k": policy.top_k,
    }


def _pool_statuses(runtime) -> dict:
    """Local quota status per engine, identical whether auditing is on or off."""
    router = getattr(runtime, "router", None)
    engines = getattr(router, "_engines", {}) if router is not None else {}
    statuses = {}
    for engine in ("exa", "tavily", "deepseek_search"):
        client = engines.get(engine)
        if client is None:
            continue
        entries = engine_pool_status(client)
        if entries:
            statuses[engine] = entries
    return statuses


async def _preview(tags: list[str], config: dict) -> dict:
    runtime = build_configured_native_jev_judge(config)
    rows = []
    for tag in tags:
        result = await runtime.judge.evaluate(tag)
        rows.append({
            "tag": tag,
            "status": result.get("status"),
            "classification": result.get("classification"),
            "reason": result.get("reason"),
            "sources": result.get("sources") or [],
            "url_report": result.get("url_report") or {},
            "search_trace": result.get("search_trace") or [],
            "usage": result.get("usage") or {},
            "cooldown_seconds": result.get("cooldown_seconds"),
        })
    checked_at = datetime.now(timezone.utc).isoformat()
    native_cfg = ((config.get("tag_classifier") or {}).get("grounded_judge") or {}).get(
        "native_jev"
    ) or {}
    return {
        "generated_at": checked_at,
        "backend": "native_jev",
        "dry_run": True,
        "policy": _policy_report(runtime.policy),
        "state": {
            "path": str(native_cfg.get("state_path") or ""),
            "scope": "preview_only",
            "isolated_from_production_decisions": True,
        },
        "task_lock": {
            "path": str(getattr(runtime, "task_lock", None).path)
            if getattr(runtime, "task_lock", None) is not None else "",
            "scope": "process_and_cross_process_single_task",
        },
        "budget": budget_semantics(config),
        "quota_estimates": {
            "engines": estimate_quota_status(_pool_statuses(runtime), checked_at=checked_at),
            "caveat": (
                "Local monthly counters only. Upstream remaining quota was not queried, "
                "so every figure is an estimate and may drift from the provider dashboard."
            ),
        },
        "tags": rows,
    }


async def run(args: argparse.Namespace) -> dict:
    base = load_config(args.config)
    grounded = ((base.get("tag_classifier") or {}).get("grounded_judge") or {})
    if grounded.get("backend") != "native_jev":
        raise SystemExit("当前配置未选择 native_jev 后端，预览中止")
    config = _effective_config(args, base)
    tags = await _select_tags(args)
    if not tags:
        raise SystemExit("没有可预览的标签")
    if _split_tags(args.tags):
        # An explicit list is confirmed against the read-only database first:
        # the whole command aborts (and never creates the file) when a tag is
        # unknown or the database cannot be read.
        await _assert_tags_exist_readonly(tags)
    # One CLI request is one task: it serialises against maintenance and other
    # previews because both share the monthly usage ledger.
    lock = native_task_lock_for_config(config)
    if lock is not None and not await lock.acquire_async():
        raise SystemExit("另一个 Native Jev 任务正在进行，预览已延后（未发起任何请求）")
    try:
        refresh_native_runtime_snapshot(config)
        report = await _preview(tags, config)
        if args.apply:
            if not args.confirm_apply:
                raise SystemExit("--apply 需要同时传入 --confirm-apply")
            report["dry_run"] = False
            report["apply_summary"] = await ClassificationMaintenance(config).run_tags(tags)
    finally:
        if lock is not None:
            lock.release()
    return report


def main(argv=None) -> int:
    args = parse_args(argv)
    report = asyncio.run(run(args))
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"报告已写入 {args.output}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

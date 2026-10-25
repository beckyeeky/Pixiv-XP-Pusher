"""Shared one-tag Grounded Judge maintenance operations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

import database as db
from grounded_judge import (
    ExistingClassificationPreserved,
    GroundedJudgeDeferredError,
    classify_single_tag,
    validate_ai_classification_record,
)
from native_jev_judge import (
    DEFAULT_JEV_ENDPOINT,
    ENGINE_ENV_NAMES,
    _join_endpoint,
    describe_credential_source,
    error_code,
    native_state_store,
    native_task_lock_for_config,
    pause_retry_token,
    read_native_state_store,
    refresh_native_runtime_snapshot,
)
from tag_categories import TAG_CATEGORY_UNRESOLVED
from utils import normalize_tag


USAGE_KEYS = ("input", "output", "thoughts", "tool_use_prompt", "total", "search_queries")
SUMMARY_STATE_KEY = "runtime.last_classification_maintenance_summary"
GROUNDING_TEXT_FIELDS = ("classifier_model", "search_provider", "search_pool_id")

#: One database page of the review queue.  A page is deliberately bounded so a
#: single call never materialises the whole profile, but it is *not* a scan cap:
#: selection keeps advancing page by page until the batch is full or the
#: database ends, so a large pause set can never permanently starve a batch.
_QUEUE_PAGE_SIZE = 200


def _bounded_text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


ACTIVATION_KEYS = (
    "agreeing_sources", "opposing_sources", "min_confidence", "agreeing_confidences",
    "corroboration_basis", "shared_urls", "distinct_urls", "url_independence",
    "engine_agreement_is_not_independent_web_evidence",
)


def _bounded_activation(activation: dict) -> dict:
    """Keep the bounded, non-secret activation evidence for later review."""
    bounded = {}
    for key in ACTIVATION_KEYS:
        value = activation.get(key)
        if isinstance(value, (list, tuple)):
            items = [_bounded_text(item, 300) for item in list(value)[:5]]
            bounded[key] = [item for item in items if item]
        elif isinstance(value, dict):
            pairs = {}
            for name, item in list(value.items())[:8]:
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    pairs[_bounded_text(name, 60)] = round(float(item), 4)
                else:
                    text = _bounded_text(item, 120)
                    if text:
                        pairs[_bounded_text(name, 60)] = text
            if pairs:
                bounded[key] = pairs
        elif isinstance(value, (int, float, bool)):
            bounded[key] = value
        else:
            text = _bounded_text(value, 120)
            if text:
                bounded[key] = text
    return bounded


def grounding_provenance(result: dict) -> dict:
    """Build the bounded, non-secret provenance stored on one AI decision."""
    if not isinstance(result, dict) or not any(
        result.get(key) for key in (*GROUNDING_TEXT_FIELDS, "source_urls", "evidence_excerpt", "search_trace")
    ):
        return {}
    provenance = {"schema_version": 1, "backend": _bounded_text(result.get("backend"), 40) or "search_first"}
    for key in GROUNDING_TEXT_FIELDS:
        value = _bounded_text(result.get(key), 200)
        if value:
            provenance[key] = value
    provenance["source_urls"] = [
        value for item in (result.get("source_urls") or [])[:5]
        if (value := _bounded_text(item, 1000))
    ]
    provenance["evidence_excerpt"] = [
        value for item in (result.get("evidence_excerpt") or [])[:3]
        if (value := _bounded_text(item, 500))
    ]
    provenance["search_trace"] = []
    for item in (result.get("search_trace") or [])[:4]:
        if not isinstance(item, dict):
            continue
        trace = {}
        for key in ("provider", "engine", "outcome", "pool_id", "error_code"):
            value = _bounded_text(item.get(key), 200)
            if value:
                trace[key] = value
        if trace:
            provenance["search_trace"].append(trace)
    activation = result.get("activation")
    if isinstance(activation, dict):
        bounded_activation = _bounded_activation(activation)
        if bounded_activation:
            provenance["activation"] = bounded_activation
    usage = {}
    raw_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    for key in USAGE_KEYS:
        if key not in raw_usage:
            continue
        try:
            usage[key] = max(0, int(raw_usage[key]))
        except (TypeError, ValueError):
            continue
    provenance["usage"] = usage
    return provenance


def _positive_int(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MaintenancePolicy:
    max_tags_per_run: int = 40
    min_profile_weight: float = 0.0
    prefer_unresolved_first: bool = True
    concurrency: int = 10

    @classmethod
    def from_config(cls, config: dict) -> "MaintenancePolicy":
        classifier = config.get("tag_classifier") if isinstance(config, dict) else {}
        classifier = classifier if isinstance(classifier, dict) else {}
        maintenance = classifier.get("maintenance")
        maintenance = maintenance if isinstance(maintenance, dict) else {}
        try:
            minimum = float(maintenance.get("min_profile_weight", 0.0) or 0.0)
        except (TypeError, ValueError):
            minimum = 0.0
        return cls(
            max_tags_per_run=_positive_int(maintenance.get("max_tags_per_run", 40), 40),
            min_profile_weight=minimum,
            prefer_unresolved_first=bool(
                maintenance.get("prefer_unresolved_first", True)
            ),
            concurrency=_positive_int(maintenance.get("concurrency", 10), 10),
        )


def empty_usage() -> dict[str, int]:
    return {key: 0 for key in USAGE_KEYS}


def native_backend_active(config: dict) -> bool:
    """True when this run uses the optional Native Jev backend."""
    classifier = config.get("tag_classifier") if isinstance(config, dict) else {}
    grounded = classifier.get("grounded_judge") if isinstance(classifier, dict) else {}
    return isinstance(grounded, dict) and grounded.get("backend") == "native_jev"


async def _mark_unresolved(tag: str, config: dict) -> bool:
    """Write the review-queue marker, keeping the legacy call shape intact."""
    if native_backend_active(config):
        return await db.mark_ai_tag_unresolved(tag, preserve_resolved=True)
    return await db.mark_ai_tag_unresolved(tag)


async def classify_and_activate_tag(tag: str, config: dict) -> dict:
    """Run the exact Grounded Judge operation used by one manual review."""
    translation = await db.get_translated_tag(tag)
    raw_result = await classify_single_tag(tag, translation, config)
    result = validate_ai_classification_record(raw_result, tag)
    provenance = grounding_provenance(raw_result)
    activated = await db.activate_ai_tag_classification(
        result["tag"], result["classification"], result["explanation"], result["languages"],
        grounding_provenance=provenance,
        # Only Native Jev opts into the atomic "do not overwrite a decision that
        # landed while we waited" rule; legacy backends keep their semantics.
        preserve_resolved=native_backend_active(config),
    )
    if isinstance(raw_result.get("usage"), dict):
        result["usage"] = raw_result["usage"]
    if not activated:
        existing = await db.get_tag_classifications([result["tag"]], ttl_days=3650)
        entry = existing.get(result["tag"]) or {}
        if entry.get("source") != "ai":
            # A human decision -- or a refusal without a visible AI row, which
            # is the legacy contract -- is reported as human_override.
            return {**result, "status": "human_override"}
        return {
            **result, "status": "preserved",
            "preserved_classification": entry.get("classification"),
            "preserved_source": entry.get("source"),
        }
    return {**result, "status": "accepted"}


async def run_scheduled_maintenance(
    tags: list[str], config: dict,
    classify: Callable[[str, dict], Awaitable[dict]] = classify_and_activate_tag,
    concurrency: int = 10,
) -> dict:
    """Classify selected tags concurrently and retain a reviewable outcome for every tag."""
    try:
        concurrency = max(1, int(concurrency))
    except (TypeError, ValueError):
        concurrency = 10
    summary = {"attempted": len(tags), "accepted": 0, "unresolved": 0, "deferred": 0, "failed": 0,
               "human_override": 0, "preserved": 0, "usage": empty_usage(), "items": []}
    semaphore = asyncio.Semaphore(concurrency)

    async def classify_one(tag: str) -> dict:
        async with semaphore:
            return await classify(tag, config)

    results = await asyncio.gather(
        *(classify_one(tag) for tag in tags), return_exceptions=True
    )
    for tag, result in zip(tags, results):
        try:
            if isinstance(result, Exception):
                raise result
            item = result
        except GroundedJudgeDeferredError as exc:
            item = {"tag": tag, "status": "deferred", "error": str(exc),
                    "usage": getattr(exc, "usage", {})}
        except ExistingClassificationPreserved as exc:
            item = {"tag": tag, "status": "preserved",
                    "classification": getattr(exc, "classification", "") or None,
                    "source": getattr(exc, "source", "") or None,
                    "usage": getattr(exc, "usage", {})}
        except ValueError as exc:
            preserved_over = not await _mark_unresolved(tag, config)
            item = {
                "tag": tag,
                "status": "preserved" if preserved_over else "unresolved",
                "error": str(exc), "usage": getattr(exc, "usage", {}),
            }
        except Exception as exc:
            if native_backend_active(config):
                # Operational failure at the backend boundary.  ``str(exc)`` is
                # never persisted (an upstream message may quote a header that
                # echoes the credential) and the tag is NOT marked unresolved: an
                # outage is not a classification verdict, so the run defers.
                item = {
                    "tag": tag,
                    "status": "deferred",
                    "reason": "internal_error",
                    "error_code": error_code(exc),
                }
            else:
                # Legacy backends (search_first) keep their original contract: a
                # backend failure is a *failed* tag, and the tag goes back to the
                # human review queue.  Only a human decision survives that write.
                preserved_over = not await _mark_unresolved(tag, config)
                item = {
                    "tag": tag,
                    "status": "preserved" if preserved_over else "failed",
                    "error": str(exc),
                }
        status = item.get("status", "failed")
        if status not in ("accepted", "unresolved", "deferred", "failed", "human_override", "preserved"):
            status = "failed"
            item["status"] = status
        summary[status] += 1
        usage = item.get("usage") or {}
        for key in USAGE_KEYS:
            summary["usage"][key] += int(usage.get(key) or 0)
        summary["items"].append(item)
    return summary


class ClassificationMaintenance:
    """Own one complete bounded Classification Maintenance lifecycle."""

    def __init__(
        self,
        config: dict,
        *,
        classify: Callable[[str, dict], Awaitable[dict]] = classify_and_activate_tag,
        database_module=db,
    ):
        self.config = config if isinstance(config, dict) else {}
        self.policy = MaintenancePolicy.from_config(self.config)
        self._classify = classify
        self._db = database_module

    async def classify_tag(self, tag: str, *, retry_paused: bool = True) -> dict:
        """Run one manual review under the same single-task lock as a batch.

        A single explicitly clicked tag is the one place that may retry an
        automatically paused tag: that click *is* the operator's retry.  The
        retry is granted as a **one-shot bypass token**, not as an early
        ``resume()``: the durable pause (``since``/``attempts``) stays exactly
        as authored until a classification actually succeeds.  An
        infrastructure deferral, a backend exception and a cancellation
        therefore leave the pause untouched, and a repeated ``hold`` keeps
        ``since`` while incrementing ``attempts``.  Only the successfully
        accepted attempt clears the pause (and the tag's own backoff), and only
        for this one tag -- a batch entry never resumes anything.
        """
        normalized = normalize_tag(tag)
        if not normalized:
            raise ValueError("必须提供有效标签")
        lock = native_task_lock_for_config(self.config)
        if lock is not None and not await lock.acquire_async():
            # The Web single-tag entry must obey exactly the contract the batch
            # and the CLI obey: it may not spend a request or touch the shared
            # ledger while another task owns it.
            return {"tag": normalized, "status": "deferred",
                    "reason": "task_lock_timeout", "usage": {}}
        try:
            refresh_native_runtime_snapshot(self.config)
            paused = self.pause_state(normalized) if retry_paused else None
            token = pause_retry_token([normalized]) if paused else None
            if token is not None:
                with token:
                    result = await self._classify(normalized, self.config)
            else:
                result = await self._classify(normalized, self.config)
            if paused and str(result.get("status") or "") == "accepted":
                cleared = self._clear_pause_after_success(normalized)
                if cleared:
                    result = {**result, "resumed_pause": cleared}
            return result
        finally:
            if lock is not None:
                lock.release()

    def _clear_pause_after_success(self, tag: str) -> dict | None:
        """Clear this one tag's pause and backoff after a successful verdict.

        Called only once the classification was accepted, while the single
        task lock is still held, so the write cannot race a batch that authored
        a newer pause in the meantime.
        """
        store = native_state_store(self.config)
        if store is None:
            return None
        try:
            store.reload()  # adopt the newest authored snapshot under the lock
            cleared = store.resume(tag)
            store.clear_cooldown(f"tag:{tag}")
        except OSError:
            return None
        return cleared or None

    def pause_state(self, tag: str) -> dict | None:
        """Read-only pause view for one tag (review list, Web hints)."""
        store = read_native_state_store(self.config)
        return None if store is None else store.pause_entry(tag)

    def _queue_exclusions(self) -> tuple[frozenset[str], frozenset[str]]:
        """(paused, cooling) for the current on-disk snapshot.

        Reading never writes: a preview must not create a state file, and it
        must never resume a pause.
        """
        try:
            store = read_native_state_store(self.config)
        except OSError:
            return frozenset(), frozenset()
        if store is None:
            return frozenset(), frozenset()
        return store.paused_tags(), store.cooling_tags()

    async def preview(
        self,
        limit: int | None = None,
        *,
        min_profile_weight: float | None = None,
    ) -> list[dict]:
        # A preview is queue inspection: it must never create or migrate the
        # database, so it reads through the read-only connection.
        selection = await self._select_available(limit, min_profile_weight, read_only=True)
        return selection["items"]

    async def select_eligible(
        self,
        limit: int | None = None,
        *,
        min_profile_weight: float | None = None,
    ) -> dict:
        """Preview plus the paused/cooling skip counters, for one operator hint."""
        return await self._select_available(limit, min_profile_weight)

    async def available_candidates(
        self, limit: int, min_profile_weight: float = 0.0, *, include_paused: bool = False,
    ) -> list[dict]:
        """Queue inspection for the maintenance/CLI previews (no per-run cap).

        The single shared implementation is what ``preview_native_jev`` and
        ``maintain_high_weight_tags`` both call, so the two read-only surfaces
        can never scan, exclude or paginate differently.  It reads the database
        through ``mode=ro``: a missing file or an incompatible schema aborts
        without creating anything.

        ``include_paused`` is the diagnostics-only switch of the preview CLI: it
        keeps paused/cooling tags in the returned batch (which then costs a real
        request per tag) and still writes nothing.
        """
        selection = await self._select_available(
            limit, min_profile_weight, capped=False, read_only=True,
            exclude_unavailable=not include_paused,
        )
        return selection["items"]

    def _queue_page_fetcher(self, read_only: bool):
        """Return ``async (limit, minimum, offset) -> rows`` for one queue page.

        ``read_only`` uses the ``mode=ro`` accessor of the real ``database``
        module.  An injected database double (unit tests) has no such accessor,
        so it keeps the documented async contract it substitutes for.
        """
        if read_only and self._db is db:
            getter = db.get_high_weight_unclassified_profile_tags_readonly

            async def fetch(limit: int, minimum: float, offset: int = 0):
                # The first page keeps exactly the same call shape as the
                # writable accessor (only limit/min_profile_weight); offset is
                # added only when a scan actually advances.
                if offset:
                    return await asyncio.to_thread(
                        getter, limit=limit, min_profile_weight=minimum, offset=offset,
                    )
                return await asyncio.to_thread(
                    getter, limit=limit, min_profile_weight=minimum,
                )

            return fetch

        async def fetch(limit: int, minimum: float, offset: int = 0):
            if offset:
                return await self._db.get_high_weight_unclassified_profile_tags(
                    limit=limit, min_profile_weight=minimum, offset=offset,
                )
            return await self._db.get_high_weight_unclassified_profile_tags(
                limit=limit, min_profile_weight=minimum,
            )

        return fetch

    async def _select_available(
        self,
        limit: int | None = None,
        min_profile_weight: float | None = None,
        *,
        capped: bool = True,
        read_only: bool = False,
        exclude_unavailable: bool = True,
    ) -> dict:
        """Weight-ordered candidates that may still be processed.

        Paused and cooling tags are filtered out of the *page* before the batch
        is filled, and the shortfall is backfilled from lower-weight candidates
        in the database's own ``ABS(weight) DESC, tag`` order (this never
        re-ranks candidates).  Pages are bounded (``_QUEUE_PAGE_SIZE``) but the
        loop keeps advancing until the batch is full or the profile ends, so
        there is no permanent scan cap: 1000 paused high-weight tags no longer
        hide the five usable rows below them.

        ``skipped_paused``/``skipped_cooling`` keep their original meaning: how
        many of the rows inspected inside this call were excluded.
        """
        effective_limit = (
            self._effective_limit(limit) if capped
            else max(1, int(limit if isinstance(limit, int) else self.policy.max_tags_per_run))
        )
        minimum = (
            self.policy.min_profile_weight
            if min_profile_weight is None else float(min_profile_weight)
        )
        fetch = self._queue_page_fetcher(read_only)
        paused, cooling = self._queue_exclusions() if exclude_unavailable else (frozenset(), frozenset())
        if not paused and not cooling:
            rows = await fetch(effective_limit, minimum)
            return {"items": rows, "skipped_paused": 0, "skipped_cooling": 0}
        page_size = max(effective_limit, _QUEUE_PAGE_SIZE)
        offset = 0
        available: list[dict] = []
        skipped_paused = skipped_cooling = 0
        while len(available) < effective_limit:
            rows = await fetch(page_size, minimum, offset)
            for row in rows:
                tag = row.get("tag")
                if tag in paused:
                    skipped_paused += 1
                    continue
                if tag in cooling:
                    skipped_cooling += 1
                    continue
                available.append(row)
                if len(available) >= effective_limit:
                    break
            offset += len(rows)
            if len(rows) < page_size:
                break  # end of the profile: nothing left to backfill from
        return {
            "items": available[:effective_limit],
            "skipped_paused": skipped_paused,
            "skipped_cooling": skipped_cooling,
        }

    async def run_eligible(self, limit: int | None = None) -> dict:
        selection = await self._select_available(limit)
        candidates = selection["items"]
        summary = await self.run_tags(item["tag"] for item in candidates)
        effective_limit = self._effective_limit(limit)
        return {
            **summary,
            "requested_limit": limit if isinstance(limit, int) else effective_limit,
            "effective_limit": effective_limit,
            "configured_limit": self.policy.max_tags_per_run,
            "min_profile_weight": self.policy.min_profile_weight,
            "skipped_paused": selection["skipped_paused"],
            "skipped_cooling": selection["skipped_cooling"],
        }

    async def run_reviewed(
        self,
        tags: list[str],
        *,
        limit: int | None = None,
    ) -> dict:
        approved = list(dict.fromkeys(
            normalized for tag in tags if (normalized := normalize_tag(tag))
        ))
        current = await self.preview(limit)
        current_tags = {item["tag"] for item in current}
        stale = [tag for tag in approved if tag not in current_tags]
        if stale:
            raise ValueError(
                f"reviewed tags are no longer eligible: {', '.join(stale)}"
            )
        return await self.run_tags(approved)

    async def run_profile(self, profile: list[str] | dict[str, float]) -> dict:
        # Manual classify_tag/run_reviewed remain explicit paid operations.
        maintenance = self.config.get("tag_classifier", {}).get("maintenance", {})
        if maintenance.get("allow_automatic_external_search") is not True:
            return {"attempted": 0, "accepted": 0, "unresolved": 0,
                    "deferred": 0, "failed": 0, "human_override": 0, "preserved": 0,
                    "usage": empty_usage(), "items": [],
                    "automatic_external_search": False}
        selected = await self._select_profile_tags_detailed(profile)
        summary = await self.run_tags(selected["tags"])
        if selected["skipped_paused"] or selected["skipped_cooling"]:
            summary = {
                **summary,
                "skipped_paused": selected["skipped_paused"],
                "skipped_cooling": selected["skipped_cooling"],
            }
        return summary

    async def run_tags(self, tags) -> dict:
        selected = list(dict.fromkeys(
            normalized for tag in tags if (normalized := normalize_tag(tag))
        ))[:self.policy.max_tags_per_run]
        lock = native_task_lock_for_config(self.config)
        if lock is not None and not await lock.acquire_async():
            # Another process owns the native state ledger right now: defer the
            # whole batch instead of letting two snapshots overwrite each other.
            return {
                "attempted": len(selected), "accepted": 0, "unresolved": 0,
                "deferred": len(selected), "failed": 0, "human_override": 0,
                "preserved": 0, "usage": empty_usage(), "lock": "timeout",
                "items": [
                    {"tag": tag, "status": "deferred", "reason": "task_lock_timeout",
                     "usage": {}}
                    for tag in selected
                ],
            }
        try:
            # The lock is held: adopt the newest on-disk snapshot before any
            # decision, cooldown or counter from this run is written back.
            refresh_native_runtime_snapshot(self.config)
            summary = await run_scheduled_maintenance(
                selected,
                self.config,
                self._classify,
                concurrency=self.policy.concurrency,
            )
        finally:
            if lock is not None:
                lock.release()
        await self._db.set_state(
            SUMMARY_STATE_KEY,
            json.dumps(summary, ensure_ascii=False),
        )
        return summary

    async def _select_profile_tags(
        self,
        profile: list[str] | dict[str, float],
    ) -> list[str]:
        """Backwards-compatible view: just the selected tags."""
        return (await self._select_profile_tags_detailed(profile))["tags"]

    async def _select_profile_tags_detailed(
        self,
        profile: list[str] | dict[str, float],
    ) -> dict:
        """Weight-ordered profile tags that may still be classified.

        Paused/cooling tags are removed before ``max_tags_per_run`` is applied,
        and the batch is backfilled from the next-weight candidates, so an
        automatic profile run cannot starve behind a growing pause set.  The
        ordering rule itself (unresolved first, then |weight|) is unchanged.

        Only a tag with an *explicit* classification is excluded.  An
        ``unresolved`` row is not evidence that the tag was ever attempted: the
        automatic path must stay consistent with the SQL queue (which compares
        the classification value, not its age) and with the review queue -- a
        one-day-old ``unresolved`` row without pause/attempt evidence remains
        selectable, exactly like a tag that was never seen.
        """
        if not isinstance(profile, dict):
            profile = {tag: 1.0 for tag in profile}

        normalized_profile = {
            normalized: float(weight)
            for tag, weight in profile.items()
            if (normalized := normalize_tag(tag))
        }
        candidates = [
            tag
            for tag, weight in normalized_profile.items()
            if abs(weight) >= self.policy.min_profile_weight
        ]
        cached = await self._db.get_tag_classifications(
            candidates,
            ttl_days=self._tag_ttl_days(),
        )

        def recorded(tag: str) -> dict:
            entry = cached.get(tag)
            return entry if isinstance(entry, dict) else {}

        def settled(tag: str) -> bool:
            """True only for an explicit (non-``unresolved``) classification.

            The cache query already returns every manual and every resolved AI
            row regardless of age, so this test matches the SQL queue exactly:
            an ``unresolved`` row is never treated as a decision.
            """
            value = recorded(tag).get("classification")
            return bool(value) and value != TAG_CATEGORY_UNRESOLVED

        def priority(tag: str):
            unresolved = (
                recorded(tag).get("classification") == TAG_CATEGORY_UNRESOLVED
            )
            return (
                0 if self.policy.prefer_unresolved_first and unresolved else 1,
                -abs(normalized_profile[tag]),
                tag,
            )

        candidates = [tag for tag in candidates if not settled(tag)]
        paused, cooling = self._queue_exclusions()
        excluded = paused | cooling
        if excluded:
            skipped_paused = sum(1 for tag in candidates if tag in paused)
            skipped_cooling = sum(
                1 for tag in candidates if tag in cooling and tag not in paused
            )
            candidates = [tag for tag in candidates if tag not in excluded]
        else:
            skipped_paused = skipped_cooling = 0
        return {
            "tags": sorted(candidates, key=priority)[:self.policy.max_tags_per_run],
            "skipped_paused": skipped_paused,
            "skipped_cooling": skipped_cooling,
        }

    def _effective_limit(self, requested: int | None) -> int:
        if requested is None:
            return self.policy.max_tags_per_run
        return min(self.policy.max_tags_per_run, _positive_int(requested, 1))

    def _tag_ttl_days(self) -> int:
        classifier = self.config.get("tag_classifier")
        classifier = classifier if isinstance(classifier, dict) else {}
        return _positive_int(classifier.get("ttl_days", 30), 30)


# ---------------------------------------------------------------------------
# Shared presentation helpers (Web settings, Web tags page, Telegram)
#
# Every operator-facing surface renders the backend facts and the outcome
# counters from these functions, so the three surfaces cannot drift apart.
# They only *read* an already normalized configuration: no gate, decision,
# credential or database value is changed here, and no credential value is
# ever returned (only which mechanism supplied it).
# ---------------------------------------------------------------------------

OUTCOME_STATUSES = (
    "accepted", "unresolved", "deferred", "failed", "preserved", "human_override",
)

OUTCOME_LABELS = {
    "accepted": "已接受",
    "unresolved": "未解决",
    "deferred": "暂缓",
    "failed": "失败",
    "preserved": "保留既有分类",
    "human_override": "人工决定优先",
}

BACKEND_LABELS = {
    "native_jev": "原生 Jev 后端（native_jev）",
    "search_first": "Search-first 后端（search_first）",
    "gemini": "旧版 Gemini Grounded Judge",
    "": "未显式设置",
}

CREDENTIAL_SOURCE_LABELS = {
    "provider": "Provider 凭据",
    "env": "环境变量",
    "env_file": "环境文件",
    "missing": "缺少凭据",
}

SHORT_BACKEND_LABELS = {
    "native_jev": "原生 Jev",
    "search_first": "Search-first",
    "gemini": "旧版 Judge",
    "": "未设置后端",
}

AUTOMATIC_SEARCH_LABELS = {True: "已启用", False: "已关闭"}


def outcome_counts(summary: dict | None) -> dict[str, int]:
    """Return every outcome status, including ones the summary omits."""
    source = summary if isinstance(summary, dict) else {}
    counts: dict[str, int] = {}
    for status in OUTCOME_STATUSES:
        try:
            counts[status] = max(0, int(source.get(status) or 0))
        except (TypeError, ValueError):
            counts[status] = 0
    return counts


def outcome_line(
    summary: dict | None, *, include_zero: bool = False, separator: str = "、",
) -> str:
    """One neutral, complete line of outcome counters for Web and Telegram."""
    counts = outcome_counts(summary)
    parts = [
        f"{OUTCOME_LABELS[status]} {counts[status]}"
        for status in OUTCOME_STATUSES
        if include_zero or counts[status]
    ]
    return separator.join(parts) or "未处理任何标签"


def outcome_lines_behind(summary: dict | None) -> list[dict[str, int | str]]:
    """Stable (label, count) pairs so a template can render them in order."""
    counts = outcome_counts(summary)
    return [
        {"status": status, "label": OUTCOME_LABELS[status], "count": counts[status]}
        for status in OUTCOME_STATUSES
    ]


# ---------------------------------------------------------------------------
# Automatic pause presentation
#
# A pause is reported in operator terms: "this tag already had a complete
# attempt and no verdict, so automatic batches skip it".  It is deliberately
# NOT described as a cooldown (which expires) and NOT as an unresolved verdict
# (the human review queue is where the tag lives).
# ---------------------------------------------------------------------------

PAUSE_REASON_LABELS = {
    "no_evidence": "未找到可用证据",
    "low_relevance": "证据与标签不相关",
    "model_unresolved": "模型判定无法确定",
    "insufficient_agreement": "两个来源未达成一致",
    "conflicting_classification": "来源分类互相冲突",
}


def pause_line(entry: dict | None) -> str:
    """One bounded, operator-facing sentence describing a pause, or ''."""
    if not isinstance(entry, dict) or not entry:
        return ""
    reason = str(entry.get("reason") or "")
    label = PAUSE_REASON_LABELS.get(reason, reason or "原因未记录")
    attempts = entry.get("attempts")
    try:
        attempted = f"，已尝试 {max(0, int(attempts))} 次"
    except (TypeError, ValueError):
        attempted = ""
    return f"自动暂停：{label}（{reason or 'unknown'}）{attempted}；自动批量跳过，单项手动重试可再次尝试"


def annotate_review_queue(items: list[dict], config: dict) -> list[dict]:
    """Attach ``automatic_pause`` to paused rows only; never writes state."""
    try:
        store = read_native_state_store(config)
    except OSError:
        return items
    if store is None or not items:
        return items
    paused = store.paused_tags()
    if not paused:
        return items
    annotated = []
    for item in items:
        entry = store.pause_entry(item.get("tag")) if item.get("tag") in paused else None
        annotated.append({**item, "automatic_pause": entry} if entry else item)
    return annotated


def _reference_names(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _pool_facts(
    providers: dict, engine: str, names: list[str], env_file: str,
) -> list[dict]:
    """Describe each configured pool account without exposing its credential."""
    facts = []
    for name in names:
        provider = providers.get(name)
        provider = provider if isinstance(provider, dict) else {}
        source = describe_credential_source(
            provider,
            env_names=ENGINE_ENV_NAMES.get(engine, ()),
            env_file=env_file,
        )
        credential = str(source.get("source") or "missing")
        env_name = str(source.get("env") or "")
        label = CREDENTIAL_SOURCE_LABELS.get(credential, credential)
        facts.append({
            "engine": engine,
            "name": name,
            "credential_source": credential,
            "credential_label": f"{label} {env_name}".strip(),
        })
    return facts


def _display_jev_endpoint(providers: dict, jev_provider: str) -> str:
    """Return the endpoint the runtime would call, from configuration only.

    This mirrors ``native_jev_judge``'s provider-base-url rule without reading
    credentials, importing a client, or opening a connection: display code may
    only restate what the stored configuration already determines.
    """
    provider = providers.get(jev_provider) if jev_provider else None
    base_url = str(provider.get("base_url") or "") if isinstance(provider, dict) else ""
    return _join_endpoint(base_url, "/systemone") or DEFAULT_JEV_ENDPOINT


def classification_backend_view(config: dict | None) -> dict:
    """Describe the effective classification backend for display only."""
    config = config if isinstance(config, dict) else {}
    classifier = config.get("tag_classifier")
    classifier = classifier if isinstance(classifier, dict) else {}
    grounded = classifier.get("grounded_judge")
    grounded = grounded if isinstance(grounded, dict) else {}
    maintenance = classifier.get("maintenance")
    maintenance = maintenance if isinstance(maintenance, dict) else {}
    providers = config.get("providers")
    providers = providers if isinstance(providers, dict) else {}
    native = grounded.get("native_jev")
    native = native if isinstance(native, dict) else {}

    backend = str(grounded.get("backend") or "").strip().lower()
    if backend not in BACKEND_LABELS:
        backend = ""
    native_requested = backend == "native_jev"
    native_enabled = native.get("enabled") is True
    auto_search = maintenance.get("allow_automatic_external_search") is True

    policy = MaintenancePolicy.from_config(config)
    env_file = str(native.get("env_file") or grounded.get("env_file") or "").strip()
    jev_provider = str(native.get("jev_provider") or "").strip()
    jev_model = str(native.get("model") or "jev-1.13").strip() or "jev-1.13"
    jev_endpoint = str(native.get("jev_endpoint") or "").strip() or _display_jev_endpoint(
        providers, jev_provider
    )
    classifier_model = str(grounded.get("search_classifier_model") or "").strip()
    deepseek_fallback = str(native.get("deepseek_search_provider") or "").strip()
    # The runtime falls back to the shared Search-first pools when the Native
    # section leaves its own pool empty; the display must not claim otherwise.
    exa_names = _reference_names(
        native.get("exa_providers") or grounded.get("exa_providers")
    )
    tavily_names = _reference_names(
        native.get("tavily_providers") or grounded.get("tavily_providers")
    )
    brave_names = _reference_names(grounded.get("brave_providers"))

    try:
        min_confidence = float(native.get("min_confidence", 0.9))
    except (TypeError, ValueError):
        min_confidence = 0.9
    try:
        min_sources = max(2, int(native.get("min_agreeing_sources", 2)))
    except (TypeError, ValueError):
        min_sources = 2
    quota = native.get("quota_estimates")
    quota = quota if isinstance(quota, dict) else {}

    pools: list[dict] = []
    if native_requested:
        pools.extend(_pool_facts(providers, "exa", exa_names, env_file))
        pools.extend(_pool_facts(providers, "tavily", tavily_names, env_file))
    elif backend == "search_first":
        pools.extend(_pool_facts(providers, "brave", brave_names, env_file))
        pools.extend(_pool_facts(providers, "tavily", tavily_names, env_file))

    if native_requested:
        order = []
        order.append(f"Exa（{('、'.join(exa_names)) or '未选择'}）")
        order.append(f"Tavily（{('、'.join(tavily_names)) or '未选择'}）")
        order.append(
            f"DeepSeek Search（{deepseek_fallback or '未选择'}）仅第三兜底"
        )
        search_order_text = " → ".join(order)
        classifier_text = f"原生 Jev 模型 {jev_model}"
        line = (
            f"当前分类后端：{BACKEND_LABELS['native_jev']}"
            f"{'，已启用' if native_enabled else '，未启用（维护任务暂缓）'}。"
            f"检索顺序：Exa 与 Tavily 优先，DeepSeek Search 仅作第三兜底；"
            f"分类由 {jev_model} 完成。自动外搜：{AUTOMATIC_SEARCH_LABELS[auto_search]}。"
        )
    elif backend == "search_first":
        search_order_text = " → ".join([
            f"Brave（{('、'.join(brave_names)) or '未选择'}）",
            f"Tavily（{('、'.join(tavily_names)) or '未选择'}）仅 Brave 无可用证据时",
            f"分类模型（{classifier_model or '未选择'}）",
        ])
        classifier_text = f"所选兼容模型 {classifier_model or '未选择'}"
        line = (
            f"当前分类后端：{BACKEND_LABELS['search_first']}。"
            f"检索顺序：Brave 优先，Tavily 仅在 Brave 无可用证据时调用；"
            f"分类由 {classifier_model or '未选择'} 完成。"
            f"自动外搜：{AUTOMATIC_SEARCH_LABELS[auto_search]}。"
        )
    elif backend == "gemini":
        search_order_text = "旧版 Gemini Google Search Grounding"
        classifier_text = "旧版 Gemini Grounded Judge"
        line = (
            f"当前分类后端：{BACKEND_LABELS['gemini']}（旧版）。"
            f"如需改用 Search-first 或原生 Jev，请在设置页显式选择并保存。"
        )
    else:
        search_order_text = "未设置"
        classifier_text = "未设置"
        line = (
            "当前未显式设置分类后端；普通保存会保持现状，"
            "只有显式选择后端后才会改变。"
        )

    facts = [
        {"label": "分类后端", "value": BACKEND_LABELS[backend]},
        {"label": "检索顺序", "value": search_order_text},
        {"label": "分类执行者", "value": classifier_text},
        {
            "label": "自动外搜",
            "value": AUTOMATIC_SEARCH_LABELS[auto_search]
            + ("；仅手动或明确确认的分类会发起检索" if not auto_search else "；画像维护可自动发起检索"),
        },
        {
            "label": "维护上限",
            "value": (
                f"每次最多 {policy.max_tags_per_run} 条、并发 {policy.concurrency}、"
                f"最低绝对画像权重 {policy.min_profile_weight:g}"
            ),
        },
    ]
    if native_requested:
        facts.extend([
            {
                "label": "Jev 连接",
                "value": (
                    f"{jev_provider or '未选择 Provider'}"
                    f"；端点 {jev_endpoint}"
                ),
            },
            {
                "label": "判定门槛",
                "value": (
                    f"置信度严格大于 {min_confidence:g} 且至少 {min_sources} 个不同搜索引擎一致，"
                    "存在高置信冲突时不自动写入"
                ),
            },
            {
                "label": "本地额度估算",
                "value": (
                    f"Exa {int(quota.get('exa_monthly_requests', 1000))} / "
                    f"Tavily {int(quota.get('tavily_monthly_requests', 500))} / "
                    f"DeepSeek {int(quota.get('deepseek_search_monthly_requests', 200))}"
                    " 次每月（仅本地估算，不代表上游剩余额度）"
                ),
            },
        ])

    notes: list[str] = []
    if native_requested and not native_enabled:
        notes.append("原生 Jev 未被启用：维护任务会记为暂缓，不会写入分类。")
    if not classifier.get("enabled"):
        notes.append("标签维护开关当前关闭；人工单条分类仍可执行。")
    if native_requested:
        notes.append("多个引擎给出同一类别并不等于独立网页证据；共享来源会记录在 provenance 中。")
    notes.append("本地额度计数与滚动窗口预算都是软限额，不保证上游不再计费。")
    if pools and any(pool["credential_source"] == "missing" for pool in pools):
        notes.append("存在缺少凭据的搜索账号；请求该账号时会被跳过。")

    return {
        "backend": backend,
        "backend_label": BACKEND_LABELS[backend],
        "short_label": SHORT_BACKEND_LABELS[backend],
        "configured": bool(backend),
        "legacy": backend == "gemini",
        "maintenance_enabled": classifier.get("enabled") is True,
        "native_requested": native_requested,
        "native_enabled": native_enabled,
        "automatic_external_search": auto_search,
        "search_order_text": search_order_text,
        "jev": {
            "model": jev_model,
            "provider": jev_provider,
            "endpoint": jev_endpoint,
            "env_file": env_file,
        },
        "classifier_model": classifier_model,
        "deepseek_search_provider": deepseek_fallback,
        "pools": pools,
        "thresholds": {
            "min_confidence": min_confidence,
            "min_agreeing_sources": min_sources,
        },
        "maintenance": {
            "max_tags_per_run": policy.max_tags_per_run,
            "concurrency": policy.concurrency,
            "min_profile_weight": policy.min_profile_weight,
            "prefer_unresolved_first": policy.prefer_unresolved_first,
        },
        "quota": {
            "exa_monthly_requests": int(quota.get("exa_monthly_requests", 1000) or 1000),
            "tavily_monthly_requests": int(quota.get("tavily_monthly_requests", 500) or 500),
            "deepseek_search_monthly_requests": int(
                quota.get("deepseek_search_monthly_requests", 200) or 200
            ),
        },
        "facts": facts,
        "notes": notes,
        "line": line,
    }


def backend_views_for_settings(config: dict | None) -> dict:
    """Describe both selectable backends once, so the settings page can swap text."""
    base = config if isinstance(config, dict) else {}
    views = {}
    for backend in ("search_first", "native_jev"):
        variant = json.loads(json.dumps(base, default=str))
        classifier = variant.setdefault("tag_classifier", {})
        classifier = classifier if isinstance(classifier, dict) else {}
        variant["tag_classifier"] = classifier
        grounded = classifier.setdefault("grounded_judge", {})
        grounded = grounded if isinstance(grounded, dict) else {}
        classifier["grounded_judge"] = grounded
        grounded["backend"] = backend
        if backend == "native_jev":
            native = grounded.setdefault("native_jev", {})
            if not isinstance(native, dict):
                native = {}
                grounded["native_jev"] = native
            native.setdefault("enabled", True)
        views[backend] = classification_backend_view(variant)
    return views

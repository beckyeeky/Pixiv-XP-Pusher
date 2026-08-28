"""Brave/Tavily-grounded tag classification for shadow and production maintenance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Protocol

import aiohttp

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    AsyncOpenAI = None

from grounded_judge import GroundedJudgeDeferredError, validate_ai_classification_record


class SearchError(Exception):
    """A search provider could not return a usable result."""


class SearchNoEvidence(SearchError):
    """A successful request had no sufficiently relevant evidence."""


class PoolQuotaExhausted(SearchError):
    """A quota pool cannot make another free search request."""


class PoolUnavailable(SearchError):
    """A credential is invalid or temporarily unavailable and must not be reused."""


@dataclass(frozen=True)
class SearchPoolConfig:
    """One independently billed search credential, never logged with its key."""

    pool_id: str
    api_key: str
    request_limit: int | None = None


@dataclass
class _PoolState:
    requests_used: int = 0
    exhausted: bool = False


class MonthlyQuotaUsageLedger:
    """Persist current-month pool usage locally without storing API credentials."""

    def __init__(self, path: Path, *, month: str | None = None):
        self._path = path
        self._fixed_month = month
        self._lock = threading.Lock()

    def current_month(self) -> str:
        """Return the local calendar month used for the active quota period."""
        return self._fixed_month or datetime.now().strftime("%Y-%m")

    def _read_unlocked(self) -> dict:
        if not self._path.exists():
            return {"months": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Quota 用量账本无法读取: {self._path}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("months"), dict):
            raise ValueError(f"Quota 用量账本格式无效: {self._path}")
        return data

    def initial_usage(self) -> dict[str, int]:
        with self._lock:
            month_usage = self._read_unlocked()["months"].get(self.current_month(), {})
        if not isinstance(month_usage, dict):
            raise ValueError(f"Quota 用量账本月份格式无效: {self._path}")
        return {str(pool_id): max(0, int(used or 0)) for pool_id, used in month_usage.items()}

    def save(self, statuses: list[Mapping[str, object]]) -> None:
        with self._lock:
            data = self._read_unlocked()
            month_usage = data["months"].setdefault(self.current_month(), {})
            for status in statuses:
                pool_id = str(status.get("pool_id") or "").strip()
                if pool_id:
                    month_usage[pool_id] = max(0, int(status.get("requests_used") or 0))
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(self._path.suffix + ".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self._path)


class SearchCredentialPool:
    """Utilization-balanced routing across independent credential pools.

    A retry remains on the selected pool. A quota failure makes that pool
    unavailable for the rest of the process and immediately advances to the
    next pool. The caller supplies the concrete network request.
    """

    def __init__(
        self, pools: list[SearchPoolConfig], *,
        initial_requests_used: Mapping[str, int] | None = None,
        on_usage_change: Callable[[list[dict]], None] | None = None,
        quota_period: Callable[[], str] | None = None,
    ):
        if not pools:
            raise ValueError("至少需要一个搜索 Quota Pool")
        if len({pool.pool_id for pool in pools}) != len(pools):
            raise ValueError("搜索 Quota Pool ID 不能重复")
        if any(not pool.api_key.strip() for pool in pools):
            raise ValueError("搜索 Quota Pool 缺少 API Key")
        self._pools = tuple(pools)
        initial_requests_used = initial_requests_used or {}
        self._states = {}
        for pool in pools:
            used = max(0, int(initial_requests_used.get(pool.pool_id, 0) or 0))
            self._states[pool.pool_id] = _PoolState(
                requests_used=used,
                exhausted=pool.request_limit is not None and used >= pool.request_limit,
            )
        self._next_index = 0
        self._selection_lock = asyncio.Lock()
        self._on_usage_change = on_usage_change
        self._quota_period = quota_period
        self._active_quota_period = quota_period() if quota_period else None

    def _reset_for_new_quota_period(self) -> None:
        if self._quota_period is None:
            return
        current_period = self._quota_period()
        if current_period == self._active_quota_period:
            return
        for state in self._states.values():
            state.requests_used = 0
            state.exhausted = False
        self._next_index = 0
        self._active_quota_period = current_period
        if self._on_usage_change:
            self._on_usage_change(self.status())

    def status(self) -> list[dict[str, int | str | bool | None]]:
        """Return redacted, report-safe pool usage."""
        return [
            {
                "pool_id": pool.pool_id,
                "request_limit": pool.request_limit,
                "requests_used": self._states[pool.pool_id].requests_used,
                "exhausted": self._states[pool.pool_id].exhausted,
            }
            for pool in self._pools
        ]

    def _select_pool(self) -> tuple[int, SearchPoolConfig]:
        selected: tuple[float, int, SearchPoolConfig] | None = None
        for offset in range(len(self._pools)):
            index = (self._next_index + offset) % len(self._pools)
            pool = self._pools[index]
            state = self._states[pool.pool_id]
            if state.exhausted:
                continue
            utilization = (
                state.requests_used / pool.request_limit
                if pool.request_limit is not None
                else float(state.requests_used)
            )
            if selected is None or utilization < selected[0]:
                selected = (utilization, index, pool)
        if selected is not None:
            return selected[1], selected[2]
        raise PoolQuotaExhausted("所有搜索 Quota Pool 的免费额度均已耗尽")

    def _reserve(self, index: int, pool: SearchPoolConfig) -> None:
        """Reserve one provider request before it can be submitted concurrently."""
        state = self._states[pool.pool_id]
        state.requests_used += 1
        if pool.request_limit is not None and state.requests_used >= pool.request_limit:
            state.exhausted = True
            self._next_index = (index + 1) % len(self._pools)
        else:
            self._next_index = (index + 1) % len(self._pools)
        if self._on_usage_change:
            self._on_usage_change(self.status())

    async def _reserve_next_pool(self) -> tuple[int, SearchPoolConfig]:
        async with self._selection_lock:
            self._reset_for_new_quota_period()
            index, pool = self._select_pool()
            self._reserve(index, pool)
            return index, pool

    def _exhaust(self, index: int, pool: SearchPoolConfig) -> None:
        state = self._states[pool.pool_id]
        state.exhausted = True
        if pool.request_limit is not None:
            state.requests_used = max(state.requests_used, pool.request_limit)
        self._next_index = (index + 1) % len(self._pools)
        if self._on_usage_change:
            self._on_usage_change(self.status())

    async def search(
        self,
        request: Callable[[SearchPoolConfig], Awaitable[object]],
        *,
        retries: int = 1,
    ) -> object:
        """Execute one request without exposing credentials to the caller's logs."""
        retries = max(0, int(retries))
        last_error: Exception | None = None
        for _ in range(len(self._pools)):
            try:
                index, pool = await self._reserve_next_pool()
            except PoolQuotaExhausted as exc:
                last_error = exc
                break
            for attempt in range(retries + 1):
                if attempt > 0:
                    try:
                        async with self._selection_lock:
                            state = self._states[pool.pool_id]
                            if state.exhausted:
                                raise PoolQuotaExhausted("已选 Quota Pool 的免费额度均已耗尽")
                            self._reserve(index, pool)
                    except PoolQuotaExhausted as exc:
                        last_error = exc
                        break
                try:
                    result = await request(pool)
                except (PoolQuotaExhausted, PoolUnavailable) as exc:
                    last_error = exc
                    self._exhaust(index, pool)
                    break
                except (TimeoutError, asyncio.TimeoutError) as exc:
                    last_error = exc
                    if attempt < retries:
                        continue
                    break
                except SearchNoEvidence:
                    # The provider accepted the request, so it consumes quota
                    # even when the query produced no usable snippets. Do not
                    # try another independent pool for the same provider.
                    raise
                except SearchError as exc:
                    last_error = exc
                    break
                else:
                    return result
            if not self._states[pool.pool_id].exhausted:
                self._next_index = (index + 1) % len(self._pools)
        if last_error is not None:
            raise last_error
        raise SearchError("搜索请求失败")


@dataclass(frozen=True)
class SearchResponse:
    provider: str
    pool_id: str
    query: str
    sources: list[dict]
    snippets: list[str]
    usage: dict[str, int] = field(default_factory=dict)


class SearchClient(Protocol):
    async def search(self, query: str) -> SearchResponse: ...


class TagClassifierClient(Protocol):
    async def classify(self, tag: str, translation: str | None, evidence: SearchResponse) -> dict: ...


def build_tag_search_query(tag: str, translation: str | None) -> str:
    """Use the canonical tag and Pixiv translation without changing identity."""
    parts = [f'"{tag}"']
    if translation and translation.strip() and translation.strip() != tag:
        parts.append(f'"{translation.strip()}"')
    parts.append("Pixiv character copyright artist feature")
    return " ".join(parts)


JsonRequester = Callable[..., Awaitable[tuple[int, dict, dict[str, str]]]]


async def _aiohttp_request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout_seconds: float = 30,
) -> tuple[int, dict, dict[str, str]]:
    """Small HTTP boundary that keeps concrete API clients independently testable."""
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, url, headers=headers, params=params, json=json_body,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            try:
                body = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                body = {}
            return response.status, body if isinstance(body, dict) else {}, dict(response.headers)


def _safe_error_detail(body: Mapping | None) -> str:
    """Return a bounded server validation message without request credentials."""
    if not body:
        return ""
    try:
        serialized = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return f"; detail={serialized[:1200]}"


def _raise_for_search_status(provider: str, status: int, body: Mapping | None = None) -> None:
    detail = _safe_error_detail(body)
    if status in {429, 432}:
        raise PoolQuotaExhausted(f"{provider} Quota Pool 已达到额度或限流{detail}")
    if status in {401, 403}:
        raise PoolUnavailable(f"{provider} Quota Pool 凭据不可用{detail}")
    if status >= 500 or status in {408, 409}:
        raise SearchError(f"{provider} 搜索暂时不可用 (HTTP {status}){detail}")
    if status >= 400:
        raise SearchError(f"{provider} 搜索请求失败 (HTTP {status}){detail}")


class BraveLLMContextClient:
    """Brave LLM Context adapter with one bounded Japanese-language search."""

    endpoint = "https://api.search.brave.com/res/v1/llm/context"

    def __init__(
        self, pool: SearchCredentialPool, request_json: JsonRequester = _aiohttp_request_json,
        *, timeout_seconds: float = 30, endpoint: str | None = None,
    ):
        self._pool = pool
        self._request_json = request_json
        self._timeout_seconds = timeout_seconds
        self._endpoint = endpoint or self.endpoint

    async def search(self, query: str) -> SearchResponse:
        async def request(selected: SearchPoolConfig) -> SearchResponse:
            status, body, _headers = await self._request_json(
                "GET", self._endpoint,
                {"Accept": "application/json", "X-Subscription-Token": selected.api_key},
                params={
                    # Brave LLM Context uses its own `jp` enum, not the ISO `ja` code.
                    "q": query, "country": "JP", "search_lang": "jp", "count": 5,
                    "maximum_number_of_urls": 5, "maximum_number_of_tokens": 1536,
                    "maximum_number_of_snippets": 8, "context_threshold_mode": "strict",
                }, timeout_seconds=self._timeout_seconds,
            )
            _raise_for_search_status("Brave", status, body)
            generic = ((body.get("grounding") or {}).get("generic") or [])
            sources, snippets = [], []
            for item in generic:
                if not isinstance(item, dict):
                    continue
                source = {key: item[key] for key in ("url", "title") if item.get(key)}
                if source:
                    sources.append(source)
                snippets.extend(str(value).strip() for value in item.get("snippets", []) if str(value).strip())
            if not snippets:
                raise SearchNoEvidence("Brave 未返回可用的搜索证据")
            return SearchResponse("brave", selected.pool_id, query, sources, snippets)

        return await self._pool.search(request)


class TavilySearchClient:
    """Tavily Advanced Search adapter used only when Brave has no usable evidence."""

    endpoint = "https://api.tavily.com/search"

    def __init__(
        self, pool: SearchCredentialPool, request_json: JsonRequester = _aiohttp_request_json,
        *, timeout_seconds: float = 30, endpoint: str | None = None,
    ):
        self._pool = pool
        self._request_json = request_json
        self._timeout_seconds = timeout_seconds
        self._endpoint = endpoint or self.endpoint

    async def search(self, query: str) -> SearchResponse:
        async def request(selected: SearchPoolConfig) -> SearchResponse:
            status, body, _headers = await self._request_json(
                "POST", self._endpoint,
                {"Content-Type": "application/json", "Authorization": f"Bearer {selected.api_key}"},
                json_body={
                    "query": query, "search_depth": "advanced", "max_results": 5,
                    "topic": "general", "include_raw_content": False,
                }, timeout_seconds=self._timeout_seconds,
            )
            _raise_for_search_status("Tavily", status, body)
            sources, snippets = [], []
            for item in body.get("results") or []:
                if not isinstance(item, dict):
                    continue
                source = {key: item[key] for key in ("url", "title") if item.get(key)}
                if source:
                    sources.append(source)
                content = str(item.get("content") or "").strip()
                if content:
                    snippets.append(content)
            if not snippets:
                raise SearchNoEvidence("Tavily 未返回可用的搜索证据")
            credits = ((body.get("usage") or {}).get("credits") or 0)
            return SearchResponse(
                "tavily", selected.pool_id, query, sources, snippets,
                {"search_credits": int(credits)},
            )

        return await self._pool.search(request)


def _extract_json_object(content: str) -> dict:
    content = str(content or "").strip()
    if content.startswith("```"):
        lines = content.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        content = "\n".join(lines).strip()
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("DeepSeek Flash 返回无效 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("DeepSeek Flash 返回不是 JSON 对象")
    return value


def _safe_excerpt(value, limit: int = 1000) -> str:
    """Keep enough diagnostic context without persisting unbounded responses."""
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value or "").strip()[:limit]


def _evidence_diagnostics(evidence: SearchResponse) -> dict:
    return {
        "source_urls": [source.get("url") for source in evidence.sources if source.get("url")],
        "evidence_excerpt": [str(snippet).strip()[:500] for snippet in evidence.snippets[:3] if str(snippet).strip()],
    }


class DeepSeekFlashClassifier:
    """Conservative OpenAI-compatible classifier constrained to search evidence."""

    def __init__(
        self, api_key: str, *, model: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com/v1", timeout_seconds: float = 45,
        max_output_tokens: int = 1024, client=None,
    ):
        if not str(api_key).strip():
            raise ValueError("DeepSeek Flash 缺少 API Key")
        self._model = model
        self._max_output_tokens = max(128, int(max_output_tokens))
        if client is not None:
            self._client = client
        else:
            if AsyncOpenAI is None:
                raise ValueError("openai dependency is unavailable")
            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)

    async def classify(self, tag: str, translation: str | None, evidence: SearchResponse) -> dict:
        source_text = "\n".join(
            f"- {snippet[:600]}" for snippet in evidence.snippets[:5]
        )
        sources = "\n".join(
            f"- {source.get('title') or source.get('url')}: {source.get('url', '')}" for source in evidence.sources[:5]
        )
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{
                "role": "system",
                "content": (
                    "You classify one normalized Pixiv tag. Do not use knowledge outside the supplied evidence. "
                    "If the evidence is missing, contradictory, or does not identify one category, return unresolved. "
                    "Categories: feature: transferable visual trait, clothing, pose, composition, or subject; "
                    "character: one fictional person; copyright: a franchise, series, game, anime, manga, or source work; "
                    "artist: a creator; non_preference: platform, rating, event, or metadata. "
                    "Return JSON only with tag, classification, explanation, languages. "
                    "languages must be exactly one primary ISO language code string, for example ja or en, never an array. "
                    "Keep explanation under 240 characters."
                ),
            }, {
                "role": "user",
                "content": (
                    f"Raw normalized tag: {tag}\n"
                    f"Pixiv translation (context only): {translation or 'none'}\n"
                    "Allowed classification: feature, character, copyright, artist, non_preference, unresolved.\n"
                    f"Sources:\n{sources or '- no source metadata'}\nEvidence:\n{source_text}"
                ),
            }],
            temperature=0.0,
            max_tokens=self._max_output_tokens,
            response_format={"type": "json_object"},
        )
        choice = response.choices[0]
        content = choice.message.content
        try:
            return _extract_json_object(content)
        except ValueError as exc:
            exc.response_excerpt = _safe_excerpt(content)
            exc.finish_reason = getattr(choice, "finish_reason", None)
            raise


class SearchGroundedJudge:
    """One bounded Brave-first, Tavily-fallback classification decision."""

    def __init__(self, brave: SearchClient, tavily: SearchClient, classifier: TagClassifierClient):
        self._brave = brave
        self._tavily = tavily
        self._classifier = classifier

    async def classify(self, tag: str, translation: str | None) -> dict:
        query = build_tag_search_query(tag, translation)
        evidence = None
        errors = []
        search_trace = []
        search_errors: list[Exception] = []
        search_attempts = 0
        for provider_name, client in (("brave", self._brave), ("tavily", self._tavily)):
            try:
                search_attempts += 1
                evidence = await client.search(query)
            except SearchError as exc:
                search_errors.append(exc)
                errors.append(str(exc))
                search_trace.append({"provider": provider_name, "outcome": "no_evidence", "error": str(exc)})
                continue
            if evidence.snippets:
                search_trace.append({"provider": evidence.provider, "outcome": "evidence", "pool_id": evidence.pool_id})
                break
            errors.append("empty_search_response")
            search_trace.append({"provider": provider_name, "outcome": "no_evidence", "error": "empty_search_response"})
            evidence = None
        if evidence is None:
            reason = (
                "search_unavailable"
                if search_errors and all(not isinstance(exc, SearchNoEvidence) for exc in search_errors)
                else "no_search_evidence"
            )
            return {
                "tag": tag,
                "classification": "unresolved",
                "reason": reason,
                "errors": errors,
                "search_trace": search_trace,
                "usage": {"search_queries": search_attempts},
            }
        raw_record = None
        try:
            raw_record = await self._classifier.classify(tag, translation, evidence)
            if str(raw_record.get("classification") or "").strip().lower() == "unresolved":
                return {
                    "tag": tag,
                    "classification": "unresolved",
                    "reason": "model_unresolved",
                    "error": str(raw_record.get("explanation") or "model returned unresolved"),
                    "model_classification": "unresolved",
                    "model_response_excerpt": _safe_excerpt(raw_record),
                    "search_provider": evidence.provider,
                    "search_pool_id": evidence.pool_id,
                    "search_trace": search_trace,
                    **_evidence_diagnostics(evidence),
                    "usage": {"search_queries": search_attempts, **evidence.usage},
                }
            record = validate_ai_classification_record(raw_record, tag)
        except ValueError as exc:
            return {
                "tag": tag,
                "classification": "unresolved",
                "reason": "invalid_model_record",
                "error": str(exc),
                "model_classification": (str(raw_record.get("classification") or "").strip() or None)
                if isinstance(raw_record, dict) else None,
                "model_response_excerpt": _safe_excerpt(
                    raw_record if raw_record is not None else getattr(exc, "response_excerpt", "")
                ),
                "finish_reason": getattr(exc, "finish_reason", None),
                "search_provider": evidence.provider,
                "search_pool_id": evidence.pool_id,
                "search_trace": search_trace,
                **_evidence_diagnostics(evidence),
                "usage": {"search_queries": search_attempts, **evidence.usage},
            }
        return {
            **record,
            "search_provider": evidence.provider,
            "search_pool_id": evidence.pool_id,
            "search_trace": search_trace,
            **_evidence_diagnostics(evidence),
            "usage": {"search_queries": search_attempts, **evidence.usage},
        }


class ConfiguredSearchGroundedJudge:
    """Production adapter that converts conservative shadow outcomes into activation records."""

    def __init__(self, judge: SearchGroundedJudge, *, model: str = ""):
        self._judge = judge
        self._model = str(model or "").strip()

    async def classify(self, tag: str, translation: str | None) -> dict:
        result = await self._judge.classify(tag, translation)
        usage = result.get("usage") or {}
        if result.get("classification") == "unresolved":
            if result.get("reason") == "search_unavailable":
                error = GroundedJudgeDeferredError("Grounded Judge 搜索基础设施暂不可用")
                error.usage = usage
                raise error
            error = ValueError("Grounded Judge 明确标为 unresolved")
            error.usage = usage
            raise error
        record = validate_ai_classification_record(result, tag)
        diagnostics = {
            key: result.get(key)
            for key in (
                "search_provider", "search_pool_id", "source_urls",
                "evidence_excerpt", "search_trace",
            )
            if result.get(key) is not None
        }
        return {
            **record,
            **diagnostics,
            "classifier_model": self._model,
            "usage": usage,
        }


_PRODUCTION_RUNTIMES: dict[str, ConfiguredSearchGroundedJudge] = {}
_PRODUCTION_RUNTIME_LOCK = threading.Lock()


def _production_signature(config: dict) -> str:
    classifier = config.get("tag_classifier") if isinstance(config.get("tag_classifier"), dict) else {}
    grounded = classifier.get("grounded_judge") if isinstance(classifier.get("grounded_judge"), dict) else {}
    classifier_model = str(grounded.get("search_classifier_model") or "").strip()
    providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
    models = config.get("models") if isinstance(config.get("models"), dict) else {}
    names = [*grounded.get("brave_providers", []), *grounded.get("tavily_providers", [])]
    selected = {
        "grounded": grounded,
        "classifier_model": classifier_model,
        "models": {classifier_model: models.get(classifier_model)},
        "providers": {name: providers.get(name) for name in names},
    }
    model = models.get(classifier_model)
    if isinstance(model, dict):
        selected["providers"][model.get("provider")] = providers.get(model.get("provider"))
    return hashlib.sha256(json.dumps(selected, sort_keys=True, default=str).encode()).hexdigest()


def build_configured_search_grounded_judge(config: dict) -> ConfiguredSearchGroundedJudge:
    """Build or reuse the process-wide production search pools for one normalized config."""
    signature = _production_signature(config)
    with _PRODUCTION_RUNTIME_LOCK:
        cached = _PRODUCTION_RUNTIMES.get(signature)
        if cached:
            return cached
        classifier = config.get("tag_classifier") if isinstance(config.get("tag_classifier"), dict) else {}
        grounded = classifier.get("grounded_judge") if isinstance(classifier.get("grounded_judge"), dict) else {}
        classifier_model = str(grounded.get("search_classifier_model") or "").strip()
        if not classifier_model:
            raise ValueError("Search-first Grounded Judge 需要选择一个分类 Model")
        from config import resolve_model
        model = resolve_model(config, classifier_model, "llm")
        if model.get("provider") not in {"deepseek", "openai", "openai_compatible", "local"}:
            raise ValueError("Search-first Grounded Judge 分类 Model 必须兼容 OpenAI Chat Completions")
        providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}

        def search_pools(field: str, provider_type: str, limit_field: str) -> list[SearchPoolConfig]:
            result = []
            limit = int(grounded.get(limit_field) or (1000 if provider_type == "brave_search" else 500))
            for name in grounded.get(field, []):
                provider = providers.get(name)
                if not isinstance(provider, dict) or provider.get("type") != provider_type:
                    continue
                key = str(provider.get("api_key") or "").strip()
                if key:
                    result.append(SearchPoolConfig(name, key, request_limit=limit))
            if not result:
                raise ValueError(f"Search-first Grounded Judge 缺少可用的 {provider_type} Provider")
            return result

        ledger = MonthlyQuotaUsageLedger(Path(grounded.get("quota_state_path") or "data/search_judge_quota_usage.json"))
        initial_usage = ledger.initial_usage()
        brave_pool = SearchCredentialPool(
            search_pools("brave_providers", "brave_search", "brave_request_limit"),
            initial_requests_used=initial_usage, on_usage_change=ledger.save,
            quota_period=ledger.current_month,
        )
        tavily_pool = SearchCredentialPool(
            search_pools("tavily_providers", "tavily_search", "tavily_request_limit"),
            initial_requests_used=initial_usage, on_usage_change=ledger.save,
            quota_period=ledger.current_month,
        )
        timeout = float(grounded.get("timeout_seconds") or 45)
        brave_provider = providers[grounded["brave_providers"][0]]
        tavily_provider = providers[grounded["tavily_providers"][0]]
        base_url = str(model.get("base_url") or "").strip()
        if not base_url and model.get("provider") == "deepseek":
            base_url = "https://api.deepseek.com/v1"
        runtime = ConfiguredSearchGroundedJudge(SearchGroundedJudge(
            BraveLLMContextClient(brave_pool, timeout_seconds=timeout, endpoint=brave_provider.get("base_url") or None),
            TavilySearchClient(tavily_pool, timeout_seconds=timeout, endpoint=tavily_provider.get("base_url") or None),
            DeepSeekFlashClassifier(
                str(model.get("api_key") or ""), model=str(model.get("model") or "deepseek-v4-flash"),
                base_url=base_url or "https://api.deepseek.com/v1", timeout_seconds=timeout,
                max_output_tokens=int(grounded.get("max_output_tokens") or 1024),
            ),
        ), model=str(model.get("model") or ""))
        _PRODUCTION_RUNTIMES.clear()
        _PRODUCTION_RUNTIMES[signature] = runtime
        return runtime


async def run_shadow_evaluation(
    items: list[dict], judge: SearchGroundedJudge | object, *,
    pool_statuses: Callable[[], list[dict]] | None = None,
    concurrency: int = 3,
) -> dict:
    """Evaluate classifications without activating or updating any tag record."""
    concurrency = max(1, int(concurrency))
    semaphore = asyncio.Semaphore(concurrency)

    async def evaluate(item: dict) -> dict:
        tag = str(item.get("tag") or "").strip()
        if not tag:
            return {"classification": "unresolved", "reason": "missing_tag"}
        async with semaphore:
            try:
                result = await judge.classify(tag, item.get("translation"))
            except Exception as exc:  # shadow work must continue for later rows
                result = {"tag": tag, "classification": "unresolved", "reason": "judge_error", "error": str(exc)}
        expected = str(item.get("expected_classification") or "").strip() or None
        return {
            "tag": tag,
            "expected_classification": expected,
            "profile_weight": float(item.get("profile_weight") or 0),
            "priority": bool(item.get("priority")),
            "classification": result.get("classification", "unresolved"),
            "reason": result.get("reason"),
            "search_provider": result.get("search_provider"),
            "search_pool_id": result.get("search_pool_id"),
            "source_urls": result.get("source_urls", []),
            "evidence_excerpt": result.get("evidence_excerpt", []),
            "error": result.get("error"),
            "model_classification": result.get("model_classification"),
            "model_response_excerpt": result.get("model_response_excerpt"),
            "finish_reason": result.get("finish_reason"),
            "search_trace": result.get("search_trace", []),
        }

    outcomes = await asyncio.gather(*(evaluate(item) for item in items))
    def metrics(rows: list[dict]) -> dict:
        expected_rows = [row for row in rows if row.get("expected_classification")]
        matched = sum(
            row["classification"] == row["expected_classification"]
            for row in expected_rows
        )
        unresolved = sum(row["classification"] == "unresolved" for row in rows)
        return {
            "total": len(rows),
            "with_expected_classification": len(expected_rows),
            "matched": matched,
            "unresolved": unresolved,
            "agreement_rate": (matched / len(expected_rows)) if expected_rows else None,
            "unresolved_rate": (unresolved / len(rows)) if rows else 0.0,
        }

    overall_metrics = metrics(outcomes)
    return {
        **overall_metrics,
        "priority_metrics": metrics([item for item in outcomes if item["priority"]]),
        "items": outcomes,
        "pool_statuses": pool_statuses() if pool_statuses else [],
    }

"""Search-first tag classification for shadow evaluation.

This module deliberately does not alter the production Gemini Grounded Judge.
It provides a bounded, provider-agnostic path that can be evaluated before it
is wired into Classification Maintenance.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Protocol

import aiohttp

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    AsyncOpenAI = None

from grounded_judge import validate_ai_classification_record


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


class SearchCredentialPool:
    """Quota-first routing across independent credential pools.

    A retry remains on the selected pool. A quota failure makes that pool
    unavailable for the rest of the process and immediately advances to the
    next pool. The caller supplies the concrete network request.
    """

    def __init__(self, pools: list[SearchPoolConfig], *, initial_requests_used: Mapping[str, int] | None = None):
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
        for offset in range(len(self._pools)):
            index = (self._next_index + offset) % len(self._pools)
            pool = self._pools[index]
            state = self._states[pool.pool_id]
            if not state.exhausted:
                return index, pool
        raise PoolQuotaExhausted("所有搜索 Quota Pool 的免费额度均已耗尽")

    def _reserve(self, index: int, pool: SearchPoolConfig) -> None:
        """Reserve one provider request before it can be submitted concurrently."""
        state = self._states[pool.pool_id]
        state.requests_used += 1
        if pool.request_limit is not None and state.requests_used >= pool.request_limit:
            state.exhausted = True
            self._next_index = (index + 1) % len(self._pools)
        else:
            self._next_index = index

    async def _reserve_next_pool(self) -> tuple[int, SearchPoolConfig]:
        async with self._selection_lock:
            index, pool = self._select_pool()
            self._reserve(index, pool)
            return index, pool

    def _exhaust(self, index: int, pool: SearchPoolConfig) -> None:
        self._states[pool.pool_id].exhausted = True
        self._next_index = (index + 1) % len(self._pools)

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
    if status == 429:
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
        *, timeout_seconds: float = 30,
    ):
        self._pool = pool
        self._request_json = request_json
        self._timeout_seconds = timeout_seconds

    async def search(self, query: str) -> SearchResponse:
        async def request(selected: SearchPoolConfig) -> SearchResponse:
            status, body, _headers = await self._request_json(
                "GET", self.endpoint,
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
        *, timeout_seconds: float = 30,
    ):
        self._pool = pool
        self._request_json = request_json
        self._timeout_seconds = timeout_seconds

    async def search(self, query: str) -> SearchResponse:
        async def request(selected: SearchPoolConfig) -> SearchResponse:
            status, body, _headers = await self._request_json(
                "POST", self.endpoint,
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
        client=None,
    ):
        if not str(api_key).strip():
            raise ValueError("DeepSeek Flash 缺少 API Key")
        self._model = model
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
            max_tokens=512,
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
        search_attempts = 0
        for provider_name, client in (("brave", self._brave), ("tavily", self._tavily)):
            try:
                search_attempts += 1
                evidence = await client.search(query)
            except SearchError as exc:
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
            return {
                "tag": tag,
                "classification": "unresolved",
                "reason": "no_search_evidence",
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

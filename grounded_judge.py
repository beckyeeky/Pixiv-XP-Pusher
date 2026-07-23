"""Gemini Search Grounding integration for one Tag review decision."""

import json
import re
import asyncio
import logging
from dataclasses import dataclass

import aiohttp

from tag_categories import TAG_CATEGORY_UNRESOLVED, normalize_tag_category


_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]+)?$", re.IGNORECASE)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeminiJudgeConfig:
    api_key: str
    model: str
    timeout_seconds: int
    max_output_tokens: int
    temperature: float
    thinking_level: str
    max_retries: int
    retry_delay_seconds: float
    retry_by_status: dict[int, tuple[int, float]]


def _selected_gemini_judge(config: dict) -> GeminiJudgeConfig:
    classifier = config.get("tag_classifier") if isinstance(config.get("tag_classifier"), dict) else {}
    judges = classifier.get("judges") if isinstance(classifier.get("judges"), list) else []
    models = config.get("models") if isinstance(config.get("models"), dict) else {}
    providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
    if len(judges) != 1 or not isinstance(models.get(judges[0]), dict):
        raise ValueError("Grounded Judge 需要恰好选择一个 Gemini Model")
    model = models[judges[0]]
    provider = providers.get(model.get("provider"))
    if not isinstance(provider, dict) or provider.get("type") != "google":
        raise ValueError("Grounded Judge 必须使用 type 为 google 的 Gemini Provider")
    api_key = str(provider.get("api_key") or "").strip()
    model_name = str(model.get("model") or "").strip()
    if not api_key or not model_name:
        raise ValueError("Grounded Judge 缺少 Gemini API Key 或模型名称")
    settings = classifier.get("grounded_judge") if isinstance(classifier.get("grounded_judge"), dict) else {}
    return GeminiJudgeConfig(
        api_key=api_key,
        model=model_name,
        timeout_seconds=_positive_int(settings.get("timeout_seconds"), 45),
        max_output_tokens=_positive_int(settings.get("max_output_tokens"), 512),
        temperature=_temperature(settings.get("temperature"), 1.0),
        thinking_level=_thinking_level(settings.get("thinking_level"), "medium"),
        max_retries=_nonnegative_int(settings.get("max_retries"), 2),
        retry_delay_seconds=_nonnegative_float(settings.get("retry_delay_seconds"), 1.0),
        retry_by_status=_retry_by_status(settings.get("retry_by_status")),
    )


def _positive_int(value, default: int) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _nonnegative_int(value, default: int) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _nonnegative_float(value, default: float) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _temperature(value, default: float) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        return min(2.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


def _thinking_level(value, default: str) -> str:
    level = str(value or "").strip().lower()
    return level if level in {"minimal", "low", "medium", "high"} else default


def _retry_by_status(value) -> dict[int, tuple[int, float]]:
    """Parse optional per-HTTP-status retry overrides from YAML."""
    if not isinstance(value, dict):
        return {}
    result: dict[int, tuple[int, float]] = {}
    for raw_status, policy in value.items():
        try:
            status = int(raw_status)
        except (TypeError, ValueError):
            continue
        if not 100 <= status <= 599 or not isinstance(policy, dict):
            continue
        result[status] = (
            _nonnegative_int(policy.get("max_retries"), 2),
            _nonnegative_float(policy.get("retry_delay_seconds"), 1.0),
        )
    return result


def _is_retryable_error(error: Exception) -> bool:
    if isinstance(error, (asyncio.TimeoutError, aiohttp.ClientConnectionError)):
        return True
    return isinstance(error, aiohttp.ClientResponseError) and (
        error.status == 408 or error.status == 429 or error.status >= 500
    )


def _retry_policy(error: Exception, judge: GeminiJudgeConfig) -> tuple[int, float]:
    """Return the retry budget and base delay for this retryable error."""
    if isinstance(error, aiohttp.ClientResponseError):
        return judge.retry_by_status.get(error.status, (judge.max_retries, judge.retry_delay_seconds))
    return judge.max_retries, judge.retry_delay_seconds


def _build_grounding_prompt(tag: str, translation: str | None) -> str:
    context = f"Pixiv 官方翻译（仅作上下文）：{translation}" if translation else "没有可用的 Pixiv 官方翻译。"
    return f"""Classify exactly one normalized raw Pixiv tag with Google Search Grounding.
Raw normalized tag (the classification identity): {tag}
{context}

Return JSON only with exactly these fields: tag, classification, explanation, languages.
classification must be one of feature, character, copyright, artist, non_preference, unresolved.
languages must be one primary ISO language code. Do not classify a translation as a separate tag."""


def _extract_response_text(response: dict) -> str:
    parts = response.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


def _extract_usage(response: dict) -> dict:
    usage = response.get("usageMetadata") or {}
    metadata = response.get("candidates", [{}])[0].get("groundingMetadata", {}) or {}
    return {
        "input": usage.get("promptTokenCount", 0), "output": usage.get("candidatesTokenCount", 0),
        "thoughts": usage.get("thoughtsTokenCount", 0), "tool_use_prompt": usage.get("toolUsePromptTokenCount", 0),
        "total": usage.get("totalTokenCount", 0), "search_queries": len(metadata.get("webSearchQueries") or []),
    }


def validate_ai_classification_record(record: dict, tag: str) -> dict:
    """Reject incomplete, mismatched, and unresolved Judge results before activation."""
    required = ("tag", "classification", "explanation", "languages")
    if not isinstance(record, dict) or any(not isinstance(record.get(key), str) or not record[key].strip() for key in required):
        raise ValueError("Grounded Judge 返回缺少必填 AI Classification Record 字段")
    if record["tag"] != tag:
        raise ValueError("Grounded Judge 返回了不同的标签")
    category = normalize_tag_category(record["classification"])
    if str(record["classification"]).strip().lower() == TAG_CATEGORY_UNRESOLVED:
        raise ValueError("Grounded Judge 明确标为 unresolved")
    if category == TAG_CATEGORY_UNRESOLVED or category != record["classification"]:
        raise ValueError("Grounded Judge 未给出有效的 Tag Category")
    if not _LANGUAGE.fullmatch(record["languages"]):
        raise ValueError("Grounded Judge 返回的 languages 不是主 ISO 语言代码")
    return {**record, "classification": category}


async def classify_single_tag(tag: str, translation: str | None, config: dict) -> dict:
    """Call the configured sole Grounded Judge backend and validate its record."""
    classifier = config.get("tag_classifier") if isinstance(config.get("tag_classifier"), dict) else {}
    settings = classifier.get("grounded_judge") if isinstance(classifier.get("grounded_judge"), dict) else {}
    if settings.get("backend") == "search_first":
        from search_grounded_judge import build_configured_search_grounded_judge
        return await build_configured_search_grounded_judge(config).classify(tag, translation)
    judge = _selected_gemini_judge(config)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{judge.model}:generateContent?key={judge.api_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": _build_grounding_prompt(tag, translation)}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": judge.temperature,
            "maxOutputTokens": judge.max_output_tokens,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": judge.thinking_level},
        },
    }
    attempt = 0
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=judge.timeout_seconds),
                ) as response:
                    response.raise_for_status()
                    raw = await response.json()
            break
        except Exception as exc:
            if not _is_retryable_error(exc):
                raise
            max_retries, retry_delay_seconds = _retry_policy(exc, judge)
            if attempt >= max_retries:
                raise
            attempt += 1
            status = getattr(exc, "status", "network/timeout")
            delay = retry_delay_seconds * (2 ** (attempt - 1))
            logger.warning(
                "Gemini Grounded Judge 请求失败 (HTTP %s)，第 %s/%s 次重试将在 %.1fs 后进行",
                status, attempt, max_retries, delay,
            )
            await asyncio.sleep(delay)
    usage = _extract_usage(raw)
    try:
        record = json.loads(_extract_response_text(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        exc.usage = usage
        raise ValueError("Grounded Judge 返回的 JSON 无效") from exc
    try:
        record = validate_ai_classification_record(record, tag)
    except ValueError as exc:
        exc.usage = usage
        raise
    return {**record, "usage": usage}

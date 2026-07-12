"""Gemini Search Grounding integration for one Tag review decision."""

import json
import re
from dataclasses import dataclass

import aiohttp

from tag_categories import TAG_CATEGORY_UNRESOLVED, normalize_tag_category


_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]+)?$", re.IGNORECASE)


@dataclass(frozen=True)
class GeminiJudgeConfig:
    api_key: str
    model: str


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
    return GeminiJudgeConfig(api_key=api_key, model=model_name)


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
    if category == TAG_CATEGORY_UNRESOLVED or category != record["classification"]:
        raise ValueError("Grounded Judge 未给出有效的 Tag Category")
    if not _LANGUAGE.fullmatch(record["languages"]):
        raise ValueError("Grounded Judge 返回的 languages 不是主 ISO 语言代码")
    return {**record, "classification": category}


async def classify_single_tag(tag: str, translation: str | None, config: dict) -> dict:
    """Call the configured sole Gemini Grounded Judge and validate its record."""
    judge = _selected_gemini_judge(config)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{judge.model}:generateContent?key={judge.api_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": _build_grounding_prompt(tag, translation)}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=45)) as response:
            response.raise_for_status()
            raw = await response.json()
    try:
        record = json.loads(_extract_response_text(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Grounded Judge 返回的 JSON 无效") from exc
    return {**validate_ai_classification_record(record, tag), "usage": _extract_usage(raw)}

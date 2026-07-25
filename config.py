import copy
import logging
from pathlib import Path
from typing import Any

import yaml
from provider_model_graph import (
    MODEL_CAPABILITIES,
    NON_MODEL_PROVIDER_TYPES,
    OPENAI_CHAT_PROVIDER_TYPES,
    SINGLETON_PROVIDER_TYPES,
    ProviderModelGraph,
    model_capabilities,
    validate_singleton_providers,
)
from proxy_utils import normalize_proxy_url
from telegram_rich import normalize_rich_message_config

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config.yaml")
KNOWN_MODEL_CATALOGS = {
    "llm": [
        "gpt-4o-mini",
        "gpt-4.1-mini",
        "deepseek-v4-flash",
        "claude-3-5-haiku-latest",
        "gemini-2.0-flash",
    ],
    "embedding": [
        "text-embedding-3-small",
        "text-embedding-3-large",
        "text-embedding-ada-002",
    ],
}


def get_known_model_catalog(capability: str) -> list[str]:
    """Return the known model catalog for an LLM or Embedding capability."""
    if capability not in KNOWN_MODEL_CATALOGS:
        raise ValueError(f"不支持的 Model catalog capability: {capability}")
    return list(KNOWN_MODEL_CATALOGS[capability])


def _model_capabilities(model: dict) -> list[str]:
    return model_capabilities(model)


def _migrate_one_function_model(
    function_cfg: dict,
    *,
    function_label: str,
    capability: str,
    providers: dict,
    models: dict,
    default_model: str,
    provider_name: str,
    model_ref: str,
) -> None:
    """Move one product function's inline provider credentials behind a shared Model."""
    if not isinstance(function_cfg, dict):
        return
    selected = str(function_cfg.get("model") or "").strip()
    if selected in models:
        return

    legacy_keys = ("provider", "api_key", "base_url")
    has_legacy_settings = any(str(function_cfg.get(key) or "").strip() for key in legacy_keys)
    if not has_legacy_settings and not function_cfg.get("enabled"):
        return

    providers.setdefault(provider_name, {
        "type": str(function_cfg.get("provider") or "openai"),
        "api_key": str(function_cfg.get("api_key") or ""),
        "base_url": str(function_cfg.get("base_url") or ""),
    })
    models.setdefault(model_ref, {
        "provider": provider_name,
        "model": selected or default_model,
        "capabilities": [capability],
    })
    function_cfg["model"] = model_ref
    for key in legacy_keys:
        function_cfg.pop(key, None)
    logger.info("已将旧版 %s 配置迁移为 Provider 和 Model", function_label)


def _migrate_legacy_function_models(normalized: dict, providers: dict, models: dict) -> None:
    """Move old inline Embedding/Scorer credentials behind shared Models."""
    ai = normalized.get("ai")
    if isinstance(ai, dict):
        for function_name, capability, default_model in (
            ("embedding", "embedding", "text-embedding-3-small"),
            ("scorer", "llm", "gpt-4o-mini"),
        ):
            _migrate_one_function_model(
                ai.get(function_name),
                function_label=f"ai.{function_name}",
                capability=capability,
                providers=providers,
                models=models,
                default_model=default_model,
                provider_name=f"{function_name}_provider",
                model_ref=f"{function_name}_default",
            )


def _migrate_legacy_tag_mapping_config(normalized: dict, providers: dict, models: dict) -> None:
    """Move retired profiler.ai settings to the candidate-only tag mapping function."""
    profiler = normalized.get("profiler")
    legacy = profiler.pop("ai", None) if isinstance(profiler, dict) else None
    if "tag_mapping" not in normalized and isinstance(legacy, dict):
        normalized["tag_mapping"] = legacy
        logger.info("已将旧版 profiler.ai 配置迁移为 candidate-only tag_mapping")
    tag_mapping = normalized.get("tag_mapping")
    _migrate_one_function_model(
        tag_mapping,
        function_label="tag_mapping",
        capability="llm",
        providers=providers,
        models=models,
        default_model="gpt-4o-mini",
        provider_name="tag_mapping_provider",
        model_ref="tag_mapping_default",
    )


def _coerce_int(value: Any, *, default: int, field_name: str) -> int:
    """尽量将配置值转换为整数；失败时回退默认值。"""
    if isinstance(value, bool):
        logger.warning("配置项 %s 不能为布尔值，已回退为默认值 %s", field_name, default)
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            logger.warning("配置项 %s 为空字符串，已回退为默认值 %s", field_name, default)
            return default
        try:
            return int(stripped)
        except ValueError:
            logger.warning("配置项 %s=%r 不是合法整数，已回退为默认值 %s", field_name, value, default)
            return default

    logger.warning("配置项 %s 的类型 %s 不受支持，已回退为默认值 %s", field_name, type(value).__name__, default)
    return default


def normalize_config(config: dict) -> dict:
    """对常见错误配置做非破坏性规范化，避免运行实例因格式问题崩溃。"""
    normalized = copy.deepcopy(config or {})

    scheduler_cfg = normalized.get("scheduler")
    if isinstance(scheduler_cfg, dict):
        # Main push times are database-owned so live bot changes do not drift
        # from a second cron value in config.yaml.
        scheduler_cfg.pop("cron", None)

    filter_cfg = normalized.setdefault("filter", {})
    raw_daily_limit = filter_cfg.get("daily_limit", 20)
    if not isinstance(raw_daily_limit, int):
        filter_cfg["daily_limit"] = _coerce_int(
            raw_daily_limit,
            default=20,
            field_name="filter.daily_limit",
        )

    display_tags_cfg = filter_cfg.get("display_tags")
    if display_tags_cfg is None:
        display_tags_cfg = {}
        filter_cfg["display_tags"] = display_tags_cfg
    elif not isinstance(display_tags_cfg, dict):
        logger.warning("配置项 filter.display_tags 必须是对象，已回退为默认值")
        display_tags_cfg = {}
        filter_cfg["display_tags"] = display_tags_cfg

    max_ip_count = _coerce_int(
        display_tags_cfg.get("max_ip_count", 2),
        default=2,
        field_name="filter.display_tags.max_ip_count",
    )
    display_tags_cfg["max_ip_count"] = max(0, max_ip_count)

    def _normalize_diversity_block(name: str, default_decay: float) -> dict:
        raw_cfg = filter_cfg.get(name)
        if raw_cfg is None:
            raw_cfg = {}
            filter_cfg[name] = raw_cfg
        elif not isinstance(raw_cfg, dict):
            logger.warning("配置项 filter.%s 必须是对象，已回退为默认值", name)
            raw_cfg = {}
            filter_cfg[name] = raw_cfg

        raw_cfg["enabled"] = bool(raw_cfg.get("enabled", False))
        decay_value = raw_cfg.get("decay_factor", default_decay)
        floor_value = raw_cfg.get("floor", 0.1)

        try:
            if isinstance(decay_value, bool):
                raise ValueError
            raw_cfg["decay_factor"] = min(1.0, max(0.0, float(decay_value)))
        except (TypeError, ValueError):
            logger.warning("配置项 filter.%s.decay_factor 非法，已回退为默认值 %s", name, default_decay)
            raw_cfg["decay_factor"] = default_decay

        try:
            if isinstance(floor_value, bool):
                raise ValueError
            raw_cfg["floor"] = min(1.0, max(0.0, float(floor_value)))
        except (TypeError, ValueError):
            logger.warning("配置项 filter.%s.floor 非法，已回退为默认值 0.1", name)
            raw_cfg["floor"] = 0.1

        return raw_cfg

    _normalize_diversity_block("author_diversity", 0.5)
    _normalize_diversity_block("ip_diversity", 0.6)

    tag_classifier_cfg = normalized.get("tag_classifier")
    if tag_classifier_cfg is None:
        tag_classifier_cfg = {}
        normalized["tag_classifier"] = tag_classifier_cfg
    elif not isinstance(tag_classifier_cfg, dict):
        logger.warning("配置项 tag_classifier 必须是对象，已回退为默认值")
        tag_classifier_cfg = {}
        normalized["tag_classifier"] = tag_classifier_cfg

    tag_classifier_cfg.setdefault("enabled", False)

    tag_mapping_cfg = normalized.get("tag_mapping")
    if tag_mapping_cfg is not None and not isinstance(tag_mapping_cfg, dict):
        logger.warning("配置项 tag_mapping 必须是对象，已回退为默认值")
        tag_mapping_cfg = {}
        normalized["tag_mapping"] = tag_mapping_cfg

    providers = normalized.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    models = normalized.get("models")
    if not isinstance(models, dict):
        models = {}
    _migrate_legacy_function_models(normalized, providers, models)

    # The previous profile vocabulary was unshipped.  Convert it while reading
    # so existing local configs gain a first-class Provider and Model instead
    # of carrying credentials on a Judge selection.
    legacy_profiles = normalized.pop("judge_profiles", {})
    if isinstance(legacy_profiles, dict):
        for profile_name, profile in legacy_profiles.items():
            if not isinstance(profile_name, str) or not profile_name.strip() or not isinstance(profile, dict):
                continue
            profile_name = profile_name.strip()
            provider_name = f"judge_profile_{profile_name}"
            providers.setdefault(provider_name, {
                "type": str(profile.get("provider") or "openai_compatible"),
                "api_key": str(profile.get("api_key") or ""),
                "base_url": str(profile.get("base_url") or "https://api.deepseek.com/v1"),
            })
            models.setdefault(profile_name, {
                "provider": provider_name,
                "model": str(profile.get("model") or "deepseek-v4-flash"),
            })

    # Current single-model installs are migrated on load.  Inline multi-Judge
    # configs were never shipped and deliberately have no compatibility path.
    # Read the legacy key before it is migrated to candidate-only tag_mapping.
    legacy_classifier_key = str(tag_classifier_cfg.get("api_key") or "").strip()
    if not legacy_classifier_key:
        legacy_classifier_key = str(
            normalized.get("profiler", {}).get("ai", {}).get("api_key") or ""
        ).strip()
    if not models and not tag_classifier_cfg.get("judges") and legacy_classifier_key:
        provider_name = "tag_classifier_provider"
        model_name = "tag_classifier_default"
        providers[provider_name] = {
            "type": str(tag_classifier_cfg.get("provider") or "openai_compatible"),
            "api_key": legacy_classifier_key,
            "base_url": str(tag_classifier_cfg.get("base_url") or "https://api.deepseek.com/v1"),
        }
        models[model_name] = {"provider": provider_name, "model": str(tag_classifier_cfg.get("model") or "deepseek-v4-flash")}
        tag_classifier_cfg["judges"] = [model_name]
        logger.info("已将单模型 tag_classifier 配置迁移为 Provider 和 Model")

    _migrate_legacy_tag_mapping_config(normalized, providers, models)
    tag_mapping_cfg = normalized.setdefault("tag_mapping", {})
    tag_mapping_cfg["enabled"] = bool(tag_mapping_cfg.get("enabled", False))
    tag_mapping_cfg["batch_size"] = max(1, _coerce_int(
        tag_mapping_cfg.get("batch_size", 50), default=50,
        field_name="tag_mapping.batch_size",
    ))
    tag_mapping_cfg["review_concurrency"] = max(1, _coerce_int(
        tag_mapping_cfg.get("review_concurrency", 3), default=3,
        field_name="tag_mapping.review_concurrency",
    ))
    tag_mapping_cfg["review_max_output_tokens"] = max(128, _coerce_int(
        tag_mapping_cfg.get("review_max_output_tokens", 1024), default=1024,
        field_name="tag_mapping.review_max_output_tokens",
    ))
    try:
        if isinstance(tag_mapping_cfg.get("review_temperature", 0.0), bool):
            raise ValueError
        tag_mapping_cfg["review_temperature"] = min(
            2.0, max(0.0, float(tag_mapping_cfg.get("review_temperature", 0.0)))
        )
    except (TypeError, ValueError):
        logger.warning("配置项 tag_mapping.review_temperature 非法，已回退为 0.0")
        tag_mapping_cfg["review_temperature"] = 0.0
    for legacy_key in ("filter_meaningless", "merge_synonyms", "concurrency"):
        tag_mapping_cfg.pop(legacy_key, None)

    legacy_pixiv = normalized.get("pixiv")
    legacy_pixiv = legacy_pixiv if isinstance(legacy_pixiv, dict) else {}
    if not any(
        isinstance(provider, dict) and provider.get("type") == "pixiv"
        for provider in providers.values()
    ):
        providers.setdefault("pixiv", {
            "type": "pixiv",
            "refresh_token": legacy_pixiv.get("refresh_token", ""),
            "sync_token": legacy_pixiv.get("sync_token", ""),
            "user_id": legacy_pixiv.get("user_id", 0),
        })

    legacy_profiler = normalized.get("profiler")
    legacy_profiler = legacy_profiler if isinstance(legacy_profiler, dict) else {}
    legacy_danbooru = tag_classifier_cfg.get("danbooru")
    legacy_danbooru = legacy_danbooru if isinstance(legacy_danbooru, dict) else {}
    if not any(
        isinstance(provider, dict) and provider.get("type") == "danbooru"
        for provider in providers.values()
    ):
        providers.setdefault("danbooru", {
            "type": "danbooru",
            "login": legacy_danbooru.get("login") or legacy_profiler.get("danbooru_login", ""),
            "api_key": legacy_danbooru.get("api_key") or legacy_profiler.get("danbooru_api_key", ""),
            "base_url": legacy_danbooru.get("base_url") or "https://danbooru.donmai.us",
        })

    normalized_providers = {}
    for name, provider in providers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(provider, dict):
            logger.warning("providers 中存在无效 Provider，已跳过")
            continue
        provider_type = str(provider.get("type") or "openai_compatible")
        normalized_provider = {"type": provider_type}
        if provider_type == "pixiv":
            normalized_provider.update({
                "refresh_token": str(provider.get("refresh_token") or ""),
                "sync_token": str(provider.get("sync_token") or ""),
                "user_id": provider.get("user_id", 0),
            })
        elif provider_type == "danbooru":
            normalized_provider.update({
                "login": str(provider.get("login") or ""),
                "api_key": str(provider.get("api_key") or ""),
                "base_url": str(provider.get("base_url") or "https://danbooru.donmai.us"),
            })
        else:
            normalized_provider.update({
                "api_key": str(provider.get("api_key") or ""),
                "base_url": str(provider.get("base_url") or ""),
            })
        normalized_providers[name.strip()] = normalized_provider
    normalized_models = {}
    for name, model in models.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(model, dict):
            logger.warning("models 中存在无效 Model，已跳过")
            continue
        provider_name = str(model.get("provider") or "").strip()
        model_name = str(model.get("model") or "").strip()
        if (
            provider_name not in normalized_providers
            or normalized_providers[provider_name].get("type") in NON_MODEL_PROVIDER_TYPES
            or not model_name
        ):
            logger.warning("Model %s 引用了不存在 Provider 或缺少模型名称，已跳过", name)
            continue
        normalized_models[name.strip()] = {
            "provider": provider_name,
            "model": model_name,
            "capabilities": _model_capabilities(model),
        }
    normalized["providers"] = normalized_providers
    normalized["models"] = normalized_models
    validate_singleton_providers(normalized_providers)
    for legacy_key in ("api_key", "base_url", "model", "provider"):
        tag_classifier_cfg.pop(legacy_key, None)
    tag_classifier_cfg["ttl_days"] = max(1, _coerce_int(
        tag_classifier_cfg.get("ttl_days", 30),
        default=30,
        field_name="tag_classifier.ttl_days",
    ))
    tag_classifier_cfg["batch_size"] = max(1, _coerce_int(
        tag_classifier_cfg.get("batch_size", 50),
        default=50,
        field_name="tag_classifier.batch_size",
    ))
    tag_classifier_cfg["concurrency"] = max(1, _coerce_int(
        tag_classifier_cfg.get("concurrency", 5),
        default=5,
        field_name="tag_classifier.concurrency",
    ))
    danbooru_cfg = tag_classifier_cfg.setdefault("danbooru", {})
    if not isinstance(danbooru_cfg, dict):
        logger.warning("配置项 tag_classifier.danbooru 必须是对象，已回退为默认值")
        danbooru_cfg = {}
        tag_classifier_cfg["danbooru"] = danbooru_cfg
    danbooru_cfg.setdefault("enabled", False)
    danbooru_cfg.setdefault("login", "")
    danbooru_cfg.setdefault("api_key", "")
    danbooru_cfg.setdefault("base_url", "https://danbooru.donmai.us")
    danbooru_cfg["timeout_seconds"] = max(1, _coerce_int(
        danbooru_cfg.get("timeout_seconds", 15), default=15,
        field_name="tag_classifier.danbooru.timeout_seconds",
    ))

    maintenance_cfg = tag_classifier_cfg.setdefault("maintenance", {})
    if not isinstance(maintenance_cfg, dict):
        maintenance_cfg = {}
        tag_classifier_cfg["maintenance"] = maintenance_cfg
    maintenance_cfg["max_tags_per_run"] = max(1, _coerce_int(
        maintenance_cfg.get("max_tags_per_run", 40), default=40,
        field_name="tag_classifier.maintenance.max_tags_per_run",
    ))
    maintenance_cfg["concurrency"] = max(1, _coerce_int(
        maintenance_cfg.get("concurrency", 10), default=10,
        field_name="tag_classifier.maintenance.concurrency",
    ))
    try:
        maintenance_cfg["min_profile_weight"] = abs(float(maintenance_cfg.get("min_profile_weight", 1.0)))
    except (TypeError, ValueError):
        maintenance_cfg["min_profile_weight"] = 1.0
    maintenance_cfg["prefer_unresolved_first"] = bool(maintenance_cfg.get("prefer_unresolved_first", True))

    grounded_judge_cfg = tag_classifier_cfg.setdefault("grounded_judge", {})
    if not isinstance(grounded_judge_cfg, dict):
        logger.warning("配置项 tag_classifier.grounded_judge 必须是对象，已回退为默认值")
        grounded_judge_cfg = {}
        tag_classifier_cfg["grounded_judge"] = grounded_judge_cfg
    grounded_judge_cfg["timeout_seconds"] = max(1, _coerce_int(
        grounded_judge_cfg.get("timeout_seconds", 45), default=45,
        field_name="tag_classifier.grounded_judge.timeout_seconds",
    ))
    grounded_judge_cfg["max_output_tokens"] = max(1, _coerce_int(
        grounded_judge_cfg.get("max_output_tokens", 512), default=512,
        field_name="tag_classifier.grounded_judge.max_output_tokens",
    ))
    try:
        if isinstance(grounded_judge_cfg.get("temperature", 1.0), bool):
            raise ValueError
        grounded_judge_cfg["temperature"] = min(2.0, max(0.0, float(grounded_judge_cfg.get("temperature", 1.0))))
    except (TypeError, ValueError):
        logger.warning("配置项 tag_classifier.grounded_judge.temperature 非法，已回退为默认值 1.0")
        grounded_judge_cfg["temperature"] = 1.0
    thinking_level = str(grounded_judge_cfg.get("thinking_level", "medium") or "").strip().lower()
    if thinking_level not in {"minimal", "low", "medium", "high"}:
        logger.warning("配置项 tag_classifier.grounded_judge.thinking_level 非法，已回退为默认值 medium")
        thinking_level = "medium"
    grounded_judge_cfg["thinking_level"] = thinking_level
    grounded_judge_cfg["max_retries"] = max(0, _coerce_int(
        grounded_judge_cfg.get("max_retries", 2), default=2,
        field_name="tag_classifier.grounded_judge.max_retries",
    ))
    try:
        if isinstance(grounded_judge_cfg.get("retry_delay_seconds", 1.0), bool):
            raise ValueError
        grounded_judge_cfg["retry_delay_seconds"] = max(0.0, float(grounded_judge_cfg.get("retry_delay_seconds", 1.0)))
    except (TypeError, ValueError):
        logger.warning("配置项 tag_classifier.grounded_judge.retry_delay_seconds 非法，已回退为默认值 1.0")
        grounded_judge_cfg["retry_delay_seconds"] = 1.0
    retry_by_status = grounded_judge_cfg.setdefault("retry_by_status", {})
    if not isinstance(retry_by_status, dict):
        logger.warning("配置项 tag_classifier.grounded_judge.retry_by_status 必须是对象，已回退为空对象")
        retry_by_status = {}
    normalized_retry_by_status = {}
    for raw_status, policy in retry_by_status.items():
        try:
            status = int(raw_status)
        except (TypeError, ValueError):
            logger.warning("retry_by_status 包含无效 HTTP 状态码 %r，已跳过", raw_status)
            continue
        if not 100 <= status <= 599 or not isinstance(policy, dict):
            logger.warning("retry_by_status[%r] 必须是 HTTP 状态码对应的对象，已跳过", raw_status)
            continue
        try:
            if isinstance(policy.get("retry_delay_seconds", grounded_judge_cfg["retry_delay_seconds"]), bool):
                raise ValueError
            retry_delay_seconds = max(0.0, float(
                policy.get("retry_delay_seconds", grounded_judge_cfg["retry_delay_seconds"])
            ))
        except (TypeError, ValueError):
            logger.warning(
                "配置项 tag_classifier.grounded_judge.retry_by_status.%s.retry_delay_seconds 非法，已使用默认值",
                status,
            )
            retry_delay_seconds = grounded_judge_cfg["retry_delay_seconds"]
        normalized_retry_by_status[str(status)] = {
            "max_retries": max(0, _coerce_int(
                policy.get("max_retries", grounded_judge_cfg["max_retries"]),
                default=grounded_judge_cfg["max_retries"],
                field_name=f"tag_classifier.grounded_judge.retry_by_status.{status}.max_retries",
            )),
            "retry_delay_seconds": retry_delay_seconds,
        }
    grounded_judge_cfg["retry_by_status"] = normalized_retry_by_status
    backend = str(grounded_judge_cfg.get("backend", "gemini") or "").strip().lower()
    if backend not in {"gemini", "search_first"}:
        logger.warning("配置项 tag_classifier.grounded_judge.backend 非法，已回退为 gemini")
        backend = "gemini"
    grounded_judge_cfg["backend"] = backend
    for field_name, provider_type in (
        ("brave_providers", "brave_search"),
        ("tavily_providers", "tavily_search"),
    ):
        references = grounded_judge_cfg.get(field_name, [])
        if not isinstance(references, list):
            references = []
        grounded_judge_cfg[field_name] = list(dict.fromkeys(
            name.strip() for name in references
            if isinstance(name, str)
            and name.strip() in normalized_providers
            and normalized_providers[name.strip()].get("type") == provider_type
        ))
    grounded_judge_cfg["brave_request_limit"] = max(1, _coerce_int(
        grounded_judge_cfg.get("brave_request_limit", 1000), default=1000,
        field_name="tag_classifier.grounded_judge.brave_request_limit",
    ))
    grounded_judge_cfg["tavily_request_limit"] = max(1, _coerce_int(
        grounded_judge_cfg.get("tavily_request_limit", 500), default=500,
        field_name="tag_classifier.grounded_judge.tavily_request_limit",
    ))
    grounded_judge_cfg["quota_state_path"] = str(
        grounded_judge_cfg.get("quota_state_path") or "data/search_judge_quota_usage.json"
    )
    search_classifier_model = str(grounded_judge_cfg.get("search_classifier_model") or "").strip()
    search_model = normalized_models.get(search_classifier_model, {})
    search_provider = normalized_providers.get(search_model.get("provider"), {}) if isinstance(search_model, dict) else {}
    grounded_judge_cfg["search_classifier_model"] = (
        search_classifier_model
        if search_classifier_model in normalized_models
        and "llm" in normalized_models[search_classifier_model].get("capabilities", [])
        and search_provider.get("type") in OPENAI_CHAT_PROVIDER_TYPES
        else ""
    )

    judges = tag_classifier_cfg.get("judges", [])
    if not isinstance(judges, list):
        logger.warning("配置项 tag_classifier.judges 必须是 Judge Profile 名称列表，已回退为空列表")
        judges = []
    normalized_judges = []
    for judge_name in judges:
        if not isinstance(judge_name, str):
            logger.warning("内嵌 Judge 配置已不支持，请改用 Model 名称引用")
            continue
        name = judge_name.strip()
        if name not in normalized_models:
            logger.warning("tag_classifier.judges 引用了不存在的 Model %s，已跳过", name)
            continue
        normalized_judges.append(name)
    # Search-first fully replaces the legacy Gemini/multi-Judge evidence path.
    # Keeping stale Judge references here would still initialize and call them
    # during delivery even though maintenance uses the Search-first backend.
    tag_classifier_cfg["judges"] = [] if backend == "search_first" else normalized_judges

    daily_slate_cfg = filter_cfg.setdefault("daily_slate", {})
    if not isinstance(daily_slate_cfg, dict):
        logger.warning("配置项 filter.daily_slate 必须是对象，已回退为默认值")
        daily_slate_cfg = {}
        filter_cfg["daily_slate"] = daily_slate_cfg
    daily_slate_cfg.setdefault("enabled", True)
    for key, default in (("feature_ratio", 0.55), ("character_ratio", 0.15), ("copyright_ratio", 0.10), ("exploration_ratio", 0.20)):
        try:
            daily_slate_cfg[key] = max(0.0, min(1.0, float(daily_slate_cfg.get(key, default))))
        except (TypeError, ValueError):
            daily_slate_cfg[key] = default
    daily_slate_cfg["max_per_character"] = max(1, _coerce_int(daily_slate_cfg.get("max_per_character", 2), default=2, field_name="filter.daily_slate.max_per_character"))
    daily_slate_cfg["max_per_copyright"] = max(1, _coerce_int(daily_slate_cfg.get("max_per_copyright", 4), default=4, field_name="filter.daily_slate.max_per_copyright"))

    notifier_cfg = normalized.get("notifier")
    if notifier_cfg is None:
        notifier_cfg = {}
        normalized["notifier"] = notifier_cfg
    elif not isinstance(notifier_cfg, dict):
        logger.warning("配置项 notifier 必须是对象，已回退为默认值")
        notifier_cfg = {}
        normalized["notifier"] = notifier_cfg

    telegram_cfg = notifier_cfg.get("telegram")
    if telegram_cfg is None:
        telegram_cfg = {}
        notifier_cfg["telegram"] = telegram_cfg
    elif not isinstance(telegram_cfg, dict):
        logger.warning("配置项 notifier.telegram 必须是对象，已回退为默认值")
        telegram_cfg = {}
        notifier_cfg["telegram"] = telegram_cfg

    telegram_cfg["rich_message"] = normalize_rich_message_config(
        telegram_cfg.get("rich_message")
    )
    telegram_cfg["proxy_url"] = normalize_proxy_url(telegram_cfg.get("proxy_url"))

    # Keep older scorer/provider fallbacks working from the migrated Model.
    tag_mapping_runtime = resolve_tag_mapping_config(normalized)
    shared_key = str(tag_mapping_runtime.get("api_key") or "").strip()
    if shared_key:
        scorer_cfg = normalized.get("ai", {}).get("scorer")
        if isinstance(scorer_cfg, dict) and not str(scorer_cfg.get("api_key") or "").strip():
            scorer_cfg["api_key"] = shared_key
            logger.debug("ai.scorer.api_key 已从 profiler Model 继承")

        for provider in normalized_providers.values():
            if (
                provider.get("type") not in {"pixiv", "danbooru"}
                and "api_key" in provider
                and not str(provider.get("api_key") or "").strip()
            ):
                provider["api_key"] = shared_key

    profiler_cfg = normalized.get("profiler", {})
    if isinstance(profiler_cfg, dict):
        if not str(danbooru_cfg.get("login", "")).strip():
            danbooru_cfg["login"] = profiler_cfg.get("danbooru_login", "")
        if not str(danbooru_cfg.get("api_key", "")).strip():
            danbooru_cfg["api_key"] = profiler_cfg.get("danbooru_api_key", "")

    pixiv_provider = get_singleton_provider(normalized, "pixiv")
    if pixiv_provider:
        normalized["pixiv"] = {
            "refresh_token": pixiv_provider.get("refresh_token", ""),
            "sync_token": pixiv_provider.get("sync_token", ""),
            "user_id": pixiv_provider.get("user_id", 0),
        }
    danbooru_provider = get_singleton_provider(normalized, "danbooru")
    if danbooru_provider:
        for key in ("login", "api_key", "base_url"):
            danbooru_cfg[key] = danbooru_provider.get(key, "")
        if isinstance(profiler_cfg, dict):
            profiler_cfg["danbooru_login"] = danbooru_provider.get("login", "")
            profiler_cfg["danbooru_api_key"] = danbooru_provider.get("api_key", "")

    return normalized


def get_singleton_provider(config: dict, provider_type: str) -> dict:
    """Return the single provider of a capability type, if configured."""
    return ProviderModelGraph.from_config(config).singleton_provider(provider_type)


def get_compatible_models(config: dict, capability: str) -> dict[str, dict]:
    """Return shared Models that can be selected by a product function."""
    return ProviderModelGraph.from_config(config).compatible_models(capability)


def resolve_model(config: dict, model_ref: str, capability: str | None = None) -> dict:
    """Resolve a Model reference into runtime credentials and provider details."""
    return ProviderModelGraph.from_config(config).resolve_model(model_ref, capability)


def resolve_tag_mapping_config(config: dict) -> dict:
    """Resolve the candidate-only tag mapping function against its LLM Model."""
    mapping_cfg = config.get("tag_mapping") if isinstance(config, dict) else None
    if not isinstance(mapping_cfg, dict):
        return {}
    resolved = copy.deepcopy(mapping_cfg)
    model_ref = str(mapping_cfg.get("model") or "").strip()
    models = config.get("models") if isinstance(config, dict) else None
    if (
        model_ref
        and isinstance(models, dict)
        and model_ref in models
        and model_ref in get_compatible_models(config, "llm")
    ):
        resolved.update(resolve_model(config, model_ref, "llm"))
    return resolved


def load_config(path: Path = CONFIG_PATH) -> dict:
    """加载配置文件"""
    if not path.exists():
        # Fallback to example if exists? No, just log error
        logger.error(f"配置文件未找到: {path}")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        if not isinstance(config, dict):
            logger.error("配置文件根节点必须是对象/映射，当前类型为: %s", type(config).__name__)
            return {}
        return normalize_config(config)
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}")
        return {}

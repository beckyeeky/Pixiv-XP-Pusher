"""Settings editing helpers for the Web UI.

This module owns the settings page shape, merge semantics, validation, and
password handling so routes and templates do not need to understand the full
configuration tree.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from config import validate_singleton_providers


SENSITIVE_CONFIG_KEYS = {
    "password",
    "web_password",
    "bot_token",
    "api_key",
    "token",
    "refresh_token",
    "access_token",
    "secret",
}


SETTINGS_DEFAULTS: dict[str, Any] = {
    "pixiv": {
        "refresh_token": "",
        "sync_token": "",
        "user_id": 0,
    },
    "strategies": ["xp_search", "related", "ranking", "subscription"],
    "scheduler": {
        "cron": "0 12 * * *",
        "coalesce": True,
        "daily_report_cron": "0 0 * * *",
    },
    "network": {
        "max_concurrency": 5,
        "random_delay": [1.0, 3.0],
        "requests_per_minute": 60,
    },
    "feedback": {
        "like_boost": 0.5,
        "dislike_penalty": 0.3,
        "dislike_threshold": 3,
        "max_chain_depth": 3,
        "related_push_limit": 1,
    },
    "notifier": {
        "types": ["telegram"],
        "max_pages": 10,
        "multi_page_mode": "cover_link",
        "telegram": {
            "bot_token": "",
            "chat_ids": [],
            "allowed_users": [],
            "thread_id": None,
            "proxy_url": None,
            "image_quality": 85,
            "max_image_size": 2000,
            "batch_mode": "single",
            "rich_message": {"enabled": False, "fallback_to_photo": True, "image_mode": "photo"},
            "topic_rules": {},
            "topic_tag_mapping": {},
        },
        "onebot": {
            "ws_url": "",
            "private_id": None,
            "group_id": None,
            "push_to_private": True,
            "push_to_group": False,
            "master_id": None,
        },
        "astrbot": {
            "http_url": "",
            "unified_msg_origin": "",
            "api_key": "",
            "image_quality": 85,
            "max_image_size": 1500,
        },
    },
    "profiler": {
        "scan_limit": 1000,
        "discovery_rate": 0.1,
        "time_decay_days": 180,
        "saturation_threshold": 0.5,
        "top_n": 20,
        "include_private": True,
        "ip_weight_discount": 1.0,
        "danbooru_login": "",
        "danbooru_api_key": "",
        "stop_words": [],
        "ai": {
            "enabled": False,
            "model": "",
            "concurrency": 10,
            "batch_size": 200,
            "filter_meaningless": True,
            "merge_synonyms": True,
        },
    },
    "ai": {
        "embedding": {
            "enabled": False,
            "model": "",
            "dimensions": 256,
            "semantic_weight": 0.3,
            "cache_ttl_days": 30,
        },
        "scorer": {
            "enabled": False,
            "model": "",
            "max_candidates": 50,
            "score_weight": 0.3,
        },
    },
    "providers": {},
    "models": {},
    "tag_classifier": {
        "enabled": False,
        "judges": [],
        "ttl_days": 30,
        "batch_size": 50,
        "concurrency": 5,
    },
    "filter": {
        "match_score": {
            "min_threshold": 0.1,
            "weight_in_sort": 0.6,
        },
        "daily_limit": 20,
        "exclude_ai": False,
        "skip_ugoira": True,
        "content_type": "illust",
        "r18_mode": "mixed",
        "max_per_artist": 3,
        "artist_boost": 0.3,
        "min_create_days": 30,
        "shuffle_factor": 0.15,
        "exploration_ratio": 0.2,
        "display_tags": {
            "max_ip_count": 2,
        },
        "author_diversity": {
            "enabled": True,
            "decay_factor": 0.5,
            "floor": 0.1,
        },
        "ip_diversity": {
            "enabled": True,
            "decay_factor": 0.6,
            "floor": 0.1,
        },
        "source_boost": {
            "xp_search": 1.0,
            "subscription": 1.1,
            "ranking": 0.9,
            "related": 1.15,
            "engagement_artists": 1.2,
        },
        "blacklist_tags": [],
    },
    "fetcher": {
        "bookmark_threshold": {
            "search": 1000,
            "subscription": 0,
            "related": 0,
        },
        "date_range_days": 7,
        "dynamic_threshold": {
            "min": 100,
            "rate": 0.05,
        },
        "search_limit": 50,
        "ranking": {
            "enabled": True,
            "modes": ["day", "week", "month"],
            "limit": 100,
        },
        "match_score": {
            "min_threshold": 0.4,
            "weight_in_sort": 0.5,
        },
        "mab_limits": {
            "min_quota": 0.2,
            "max_quota": 0.6,
        },
        "subscribed_artists": [],
    },
    "web": {
        "enabled": True,
        "require_login_password": True,
        "password": "",
        "port": 8000,
    },
}


def merge_config_replace_lists(base: Any, override: Any) -> Any:
    """Deep merge dictionaries; lists and scalars are replaced by override."""
    if isinstance(base, dict) and isinstance(override, dict):
        result = deepcopy(base)
        for key, value in override.items():
            if key in result:
                result[key] = merge_config_replace_lists(result[key], value)
            else:
                result[key] = deepcopy(value)
        return result
    return deepcopy(override)


def build_settings_snapshot(raw_config: Any) -> dict:
    """Return a complete, template-safe configuration snapshot."""
    merged = merge_config_replace_lists(
        SETTINGS_DEFAULTS,
        raw_config if isinstance(raw_config, dict) else {},
    )

    network_cfg = merged.setdefault("network", {})
    random_delay = network_cfg.get("random_delay", [1.0, 3.0])
    if not isinstance(random_delay, list) or len(random_delay) < 2:
        network_cfg["random_delay"] = [1.0, 3.0]

    strategies = merged.get("strategies")
    if isinstance(strategies, str):
        merged["strategies"] = [strategies]
    elif not isinstance(strategies, list):
        merged["strategies"] = []

    return merged


def _mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 6:
        return "••••"
    return f"{text[:2]}…{text[-4:]}"


def _is_masked_secret(value: Any) -> bool:
    return isinstance(value, str) and (value == "••••" or "…" in value)


def _uses_secret_fingerprint(path: tuple[str, ...], key: str) -> bool:
    if key in {"refresh_token", "sync_token"} and path == ("pixiv",):
        return True
    if path == ("profiler",) and key in {"danbooru_login", "danbooru_api_key"}:
        return True
    if path == ("tag_classifier", "danbooru") and key in {"login", "api_key"}:
        return True
    return len(path) >= 2 and path[0] == "providers" and key in {
        "login", "api_key", "refresh_token", "sync_token",
    }


def redact_sensitive_config(data: Any, path: tuple[str, ...] = ()) -> Any:
    """Recursively redact credentials for API responses."""
    if isinstance(data, dict):
        redacted: dict[str, Any] = {}
        for key, value in data.items():
            key_lower = str(key).lower()
            if _uses_secret_fingerprint(path, key_lower) or any(sensitive in key_lower for sensitive in SENSITIVE_CONFIG_KEYS):
                redacted[key] = _mask_secret(value) if _uses_secret_fingerprint(path, key_lower) else ("••••" if value else "")
            else:
                redacted[key] = redact_sensitive_config(value, (*path, str(key)))
        return redacted
    if isinstance(data, list):
        return [redact_sensitive_config(item, path) for item in data]
    return data


def apply_settings_payload(
    current_config: dict,
    payload: dict,
    password_hasher: Callable[[str], str],
) -> dict:
    """Validate and merge a full settings payload into the current config."""
    if not isinstance(payload, dict):
        raise ValueError("配置内容必须是对象")

    current = current_config if isinstance(current_config, dict) else {}
    payload = deepcopy(payload)
    _preserve_masked_secrets(current, payload)
    _preserve_or_delete_provider_credentials(current, payload)
    merged = merge_config_replace_lists(current, payload)
    from config import normalize_config

    merged = normalize_config(merged)
    _validate_provider_model_config(merged)
    web_cfg = merged.setdefault("web", {})
    current_web_cfg = current.get("web", {}) if isinstance(current.get("web"), dict) else {}

    web_cfg["require_login_password"] = bool(web_cfg.get("require_login_password", True))
    web_password = (payload.get("web_password") or "").strip()
    web_password_confirm = (payload.get("web_password_confirm") or "").strip()

    if web_cfg["require_login_password"]:
        existing_password = current_web_cfg.get("password", "")
        if web_password or web_password_confirm:
            if web_password != web_password_confirm:
                raise ValueError("Web 登录密码两次输入不一致")
            if len(web_password) < 6:
                raise ValueError("Web 登录密码至少 6 位")
            web_cfg["password"] = password_hasher(web_password)
        elif not web_cfg.get("password"):
            if existing_password:
                web_cfg["password"] = existing_password
            else:
                raise ValueError("请设置 Web 登录密码（至少 6 位）")
    else:
        web_cfg["password"] = ""

    merged.pop("web_password", None)
    merged.pop("web_password_confirm", None)
    return merged


def _preserve_or_delete_provider_credentials(current: dict, payload: dict) -> None:
    """Apply the Provider credential modal's explicit replace/delete semantics."""
    providers = payload.get("providers")
    if not isinstance(providers, dict):
        return
    existing = current.get("providers") if isinstance(current.get("providers"), dict) else {}
    for name, submitted in providers.items():
        if not isinstance(submitted, dict):
            continue
        old = existing.get(name) if isinstance(existing.get(name), dict) else {}
        actions = submitted.pop("credential_actions", None)
        legacy_action = submitted.pop("credential_action", None)
        if not isinstance(actions, dict):
            actions = {}
        if legacy_action:
            actions.setdefault("api_key", legacy_action)
        for key in ("login", "api_key", "refresh_token", "sync_token"):
            action = actions.get(key)
            value = submitted.get(key)
            if action == "delete":
                submitted[key] = ""
            elif value is None or value == "" or _is_masked_secret(value):
                if key in old:
                    submitted[key] = old[key]


def _preserve_masked_secrets(current: Any, submitted: Any) -> None:
    """A settings read returns fingerprints, so sending one back must not overwrite it."""
    if not isinstance(current, dict) or not isinstance(submitted, dict):
        return
    for key, value in submitted.items():
        old = current.get(key)
        if any(token in str(key).lower() for token in SENSITIVE_CONFIG_KEYS):
            if old is not None and (value == "" or _is_masked_secret(value)):
                submitted[key] = old
        elif isinstance(old, dict) and isinstance(value, dict):
            _preserve_masked_secrets(old, value)


def _validate_provider_model_config(config: dict) -> None:
    providers = config.get("providers", {})
    models = config.get("models", {})
    if not isinstance(providers, dict) or not isinstance(models, dict):
        raise ValueError("Providers 和 Models 必须是对象")
    allowed_types = {"openai", "deepseek", "anthropic", "google", "openai_compatible", "local", "pixiv", "danbooru"}
    singleton_labels = {"pixiv": "Pixiv", "danbooru": "Danbooru"}
    singleton_counts = {key: 0 for key in singleton_labels}
    for name, provider in providers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(provider, dict):
            raise ValueError("每个 Provider 都需要名称和配置")
        provider_type = provider.get("type")
        if provider_type not in allowed_types:
            raise ValueError(f"Provider {name} 的类型无效")
        if provider_type in singleton_counts:
            singleton_counts[provider_type] += 1
        if provider_type == "openai_compatible" and not str(provider.get("base_url") or "").strip():
            raise ValueError(f"自定义 Provider {name} 需要 Base URL")
    for provider_type, count in singleton_counts.items():
        if count > 1:
            raise ValueError(f"只能配置一个 {singleton_labels[provider_type]} Provider")
    validate_singleton_providers(providers)
    for name, model in models.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(model, dict):
            raise ValueError("每个 Model 都需要名称和配置")
        if model.get("provider") not in providers:
            raise ValueError(f"Model {name} 必须引用一个已配置 Provider")
        if providers[model["provider"]].get("type") in {"pixiv", "danbooru"}:
            raise ValueError(f"Model {name} 必须引用 LLM Provider")
        if not str(model.get("model") or "").strip():
            raise ValueError(f"Model {name} 需要模型名称")
        capabilities = model.get("capabilities", model.get("capability", ["llm"]))
        if isinstance(capabilities, str):
            capabilities = [capabilities]
        if not isinstance(capabilities, list) or not capabilities or any(
            capability not in {"llm", "embedding"} for capability in capabilities
        ):
            raise ValueError(f"Model {name} 的能力必须是 llm 或 embedding")
    classifier = config.get("tag_classifier", {})
    if isinstance(classifier, dict):
        judges = classifier.get("judges", [])
        if not isinstance(judges, list) or any(name not in models for name in judges):
            raise ValueError("Judge 必须选择已配置的 Model")
    function_selections = [
        ("ai", "embedding", "embedding"),
        ("ai", "scorer", "llm"),
        ("profiler", "ai", "llm"),
    ]
    for section_name, function_name, capability in function_selections:
        section = config.get(section_name, {})
        function_cfg = section.get(function_name, {}) if isinstance(section, dict) else None
        model_ref = function_cfg.get("model") if isinstance(function_cfg, dict) else None
        if not isinstance(function_cfg, dict) or not function_cfg.get("enabled"):
            continue
        label = f"{section_name}.{function_name}.model"
        if not model_ref or model_ref not in models:
            raise ValueError(f"{label} 必须选择 {capability} Model")
        model_capabilities = models[model_ref].get("capabilities", ["llm"])
        if capability not in model_capabilities:
            raise ValueError(f"{label} 必须选择 {capability} Model")

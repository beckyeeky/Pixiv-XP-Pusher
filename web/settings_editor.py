"""Settings editing helpers for the Web UI.

This module owns the settings page shape, merge semantics, validation, and
password handling so routes and templates do not need to understand the full
configuration tree.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable


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
            "enabled": True,
            "provider": "openai",
            "api_key": "",
            "base_url": "",
            "model": "gpt-4o-mini",
            "concurrency": 10,
            "batch_size": 200,
            "filter_meaningless": True,
            "merge_synonyms": True,
        },
    },
    "ai": {
        "embedding": {
            "enabled": False,
            "provider": "openai",
            "api_key": "",
            "base_url": "",
            "model": "text-embedding-3-small",
            "dimensions": 256,
            "semantic_weight": 0.3,
            "cache_ttl_days": 30,
        },
        "scorer": {
            "enabled": False,
            "provider": "openai",
            "api_key": "",
            "base_url": "",
            "model": "gpt-4o-mini",
            "max_candidates": 50,
            "score_weight": 0.3,
        },
    },
    "tag_classifier": {
        "enabled": False,
        "api_key": "",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
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


def redact_sensitive_config(data: Any) -> Any:
    """Recursively redact credentials for API responses."""
    if isinstance(data, dict):
        redacted: dict[str, Any] = {}
        for key, value in data.items():
            key_lower = str(key).lower()
            if any(sensitive in key_lower for sensitive in SENSITIVE_CONFIG_KEYS):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = redact_sensitive_config(value)
        return redacted
    if isinstance(data, list):
        return [redact_sensitive_config(item) for item in data]
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
    merged = merge_config_replace_lists(current, payload)
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

"""Settings editing helpers for the Web UI.

This module owns the settings page shape, merge semantics, validation, and
password handling so routes and templates do not need to understand the full
configuration tree.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Callable

from provider_model_graph import ProviderModelGraph


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
    },
    "profiler": {
        "scan_limit": 1000,
        "discovery_rate": 0.1,
        "time_decay_days": 180,
        "saturation_threshold": 0.5,
        "top_n": 20,
        "include_private": True,
        "boost_tags": {},
        "danbooru_login": "",
        "danbooru_api_key": "",
        "stop_words": [],
    },
    "tag_mapping": {
        "enabled": False,
        "model": "",
        "batch_size": 50,
        "review_concurrency": 3,
        "review_temperature": 0.0,
        "review_max_output_tokens": 1024,
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
        "maintenance": {
            "max_tags_per_run": 40,
            "min_profile_weight": 1.0,
            "concurrency": 10,
            "prefer_unresolved_first": True,
            "allow_automatic_external_search": False,
        },
        "grounded_judge": {
            # Empty means "not explicitly configured": the settings page must
            # show it as unset and keep the stored backend instead of writing a
            # backend the operator never chose.
            "backend": "",
            "search_classifier_model": "",
            "brave_providers": [],
            "tavily_providers": [],
            "exa_providers": [],
            "brave_request_limit": 1000,
            "tavily_request_limit": 500,
            "quota_state_path": "data/search_judge_quota_usage.json",
            "temperature": 0.0,
            "max_output_tokens": 1024,
        },
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

    notifier = merged.get("notifier", {})
    if isinstance(notifier, dict):
        for obsolete in ("onebot", "astrbot", "email"):
            notifier.pop(obsolete, None)
        raw_notifier = raw_config.get("notifier", {}) if isinstance(raw_config, dict) else {}
        raw_notifier = raw_notifier if isinstance(raw_notifier, dict) else {}
        channels = raw_notifier.get("types")
        if channels is None:
            channels = [raw_notifier.get("type", "telegram")]
        if isinstance(channels, str):
            channels = [channels]
        notifier["types"] = [name for name in channels if name == "telegram"]
        notifier.pop("type", None)

    scheduler_cfg = merged.setdefault("scheduler", {})
    if isinstance(scheduler_cfg, dict):
        scheduler_cfg.pop("cron", None)

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
    _reject_non_mapping_sections(payload)
    _validate_submitted_native_fields(payload)
    _preserve_masked_secrets(current, payload)
    _preserve_or_delete_provider_credentials(current, payload)
    merged = merge_config_replace_lists(current, payload)
    # The main push schedule belongs to system_state so Telegram and OneBot
    # update one live, durable schedule. Keep only maintenance settings in
    # config.yaml and accept old clients that still submit cron.
    scheduler = merged.get("scheduler")
    if isinstance(scheduler, dict):
        scheduler.pop("cron", None)
    # Providers/Models are whole maps in Settings: omitting an entry deletes it,
    # but each retained entry still deep-merges with the previous value.
    if isinstance(payload.get("providers"), dict):
        current_providers = current.get("providers") if isinstance(current.get("providers"), dict) else {}
        merged["providers"] = {
            name: merge_config_replace_lists(current_providers.get(name, {}), value)
            if isinstance(value, dict) else deepcopy(value)
            for name, value in payload["providers"].items()
        }
    if isinstance(payload.get("models"), dict):
        current_models = current.get("models") if isinstance(current.get("models"), dict) else {}
        merged["models"] = {
            name: merge_config_replace_lists(current_models.get(name, {}), value)
            if isinstance(value, dict) else deepcopy(value)
            for name, value in payload["models"].items()
        }
    _preserve_native_jev_backend(current, merged, payload)
    from config import normalize_config

    _validate_classification_maintenance_fields(merged)
    ProviderModelGraph.from_config(merged).validate_deletion_references()
    merged = normalize_config(merged)
    ProviderModelGraph.from_config(merged).validate()
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


NATIVE_JEV_MIN_CONFIDENCE = 0.9
NATIVE_JEV_MIN_AGREEING_SOURCES = 2
NATIVE_JEV_EDITABLE_FIELDS = (
    "enabled", "model", "jev_provider", "jev_endpoint",
    "exa_providers", "tavily_providers", "deepseek_search_provider", "env_file",
    "min_confidence", "min_agreeing_sources", "require_tag_mention", "quota_estimates",
)


def _reject_non_mapping_sections(payload: dict) -> None:
    """Refuse sections submitted as ``null`` instead of "not mentioned".

    ``merge_config_replace_lists`` replaces a mapping with whatever the payload
    carries, and ``normalize_config`` then falls back to defaults.  A
    ``null`` would therefore silently wipe a configured Native Jev subtree
    (backend reset to the legacy default), while the operator only meant "send
    the rest of the form".  Only an omitted key means "keep the stored value";
    an explicit ``null``/list/scalar is a malformed edit and is rejected.
    """
    sections = (
        (("tag_classifier",), "tag_classifier"),
        (("tag_classifier", "grounded_judge"), "tag_classifier.grounded_judge"),
        (
            ("tag_classifier", "grounded_judge", "native_jev"),
            "tag_classifier.grounded_judge.native_jev",
        ),
    )
    for path, label in sections:
        parent = payload
        present = True
        for key in path[:-1]:
            if not isinstance(parent, dict) or key not in parent:
                present = False
                break
            parent = parent[key]
        if not present or not isinstance(parent, dict) or path[-1] not in parent:
            continue
        if not isinstance(parent[path[-1]], dict):
            raise ValueError(f"配置项 {label} 必须是对象，不能提交 null 或其他类型")


def _submitted_native_section(payload: Any) -> tuple[dict | None, dict]:
    """Return (grounded_judge, native_jev) exactly as the payload submitted them."""
    classifier = payload.get("tag_classifier") if isinstance(payload, dict) else None
    grounded = classifier.get("grounded_judge") if isinstance(classifier, dict) else None
    if not isinstance(grounded, dict):
        return None, {}
    native = grounded.get("native_jev")
    return grounded, (native if isinstance(native, dict) else {})


def _preserve_native_jev_backend(current: dict, merged: dict, payload: dict) -> None:
    """Keep the Native Jev backend unless the payload explicitly leaves it.

    Two different saves reach this function:

    * a native-unaware client save -- it submits no ``native_jev`` section, or
      it still submits the historical ``backend: search_first`` that the old
      settings page wrote for an untouched form;
    * a native-aware save from the settings page -- it now renders the backend
      select, the Jev connection, the Exa/Tavily pools and the thresholds.

    Only the second kind may change the backend, and leaving ``native_jev``
    additionally requires ``native_jev.enabled: false`` in the same payload.
    Everything else keeps ``backend: native_jev``.  The native fields the
    payload *did* submit are merged normally (the deep merge keeps every
    unsubmitted key), so an explicit edit is never undone by restoring the whole
    previous subtree.
    """
    current_classifier = current.get("tag_classifier")
    current_grounded = (
        current_classifier.get("grounded_judge")
        if isinstance(current_classifier, dict) else None
    )
    if not isinstance(current_grounded, dict) or current_grounded.get("backend") != "native_jev":
        return
    submitted_grounded, submitted_native = _submitted_native_section(payload)
    if submitted_grounded is None:
        return
    submitted_backend = str(submitted_grounded.get("backend") or "").strip().lower()
    explicit_leave = (
        submitted_backend in {"search_first", "gemini"}
        and submitted_native.get("enabled") is False
    )
    if explicit_leave:
        return
    merged_classifier = merged.setdefault("tag_classifier", {})
    if not isinstance(merged_classifier, dict):
        merged_classifier = {}
        merged["tag_classifier"] = merged_classifier
    merged_grounded = merged_classifier.setdefault("grounded_judge", {})
    if not isinstance(merged_grounded, dict):
        merged_grounded = {}
        merged_classifier["grounded_judge"] = merged_grounded
    merged_grounded["backend"] = "native_jev"
    if not isinstance(merged_grounded.get("native_jev"), dict):
        previous = current_grounded.get("native_jev")
        merged_grounded["native_jev"] = (
            deepcopy(previous) if isinstance(previous, dict) else {}
        )


def _validate_submitted_native_fields(payload: dict) -> None:
    """Reject explicit native edits that would weaken the fixed decision gates.

    ``config.normalize_config`` already raises a too-low threshold back to the
    floor; this turns that silent correction into an error the operator can see,
    so a save never looks successful while the stored gate differs from the
    submitted one.
    """
    _grounded, native = _submitted_native_section(payload)
    if not native:
        return
    if "min_confidence" in native:
        raw = native.get("min_confidence")
        if isinstance(raw, bool):
            raise ValueError("原生 Jev 的最低置信度必须是 0.9 到 1 之间的数字")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError("原生 Jev 的最低置信度必须是 0.9 到 1 之间的数字") from None
        if not math.isfinite(value):
            raise ValueError("原生 Jev 的最低置信度必须是有限数字（不能是 nan 或无穷大）")
        if value < NATIVE_JEV_MIN_CONFIDENCE:
            raise ValueError(
                "原生 Jev 的最低置信度不能低于 0.9：写入要求置信度严格大于该值，"
                "降低门槛会削弱双来源一致规则"
            )
        if value > 1.0:
            raise ValueError("原生 Jev 的最低置信度不能大于 1")
    if "min_agreeing_sources" in native:
        raw = native.get("min_agreeing_sources")
        if isinstance(raw, bool):
            raise ValueError("原生 Jev 的最少一致来源数必须是不小于 2 的整数")
        if isinstance(raw, float) and (not math.isfinite(raw) or not raw.is_integer()):
            raise ValueError("原生 Jev 的最少一致来源数必须是不小于 2 的整数")
        if isinstance(raw, str) and not raw.strip().lstrip("+").isdigit():
            raise ValueError("原生 Jev 的最少一致来源数必须是不小于 2 的整数")
        try:
            sources = int(raw)
        except (TypeError, ValueError):
            raise ValueError("原生 Jev 的最少一致来源数必须是不小于 2 的整数") from None
        if sources < NATIVE_JEV_MIN_AGREEING_SOURCES:
            raise ValueError(
                "原生 Jev 的最少一致来源数不能小于 2：至少需要两个不同搜索引擎一致才允许自动写入"
            )
    if "env_file" in native:
        text = str(native.get("env_file") or "")
        if "\n" in text or "\r" in text:
            raise ValueError("原生 Jev 的凭据环境文件路径不能包含换行")
        if "=" in text:
            # A KEY=VALUE pair belongs inside the file; storing it as the path
            # would both break loading and leak a credential into config.yaml.
            raise ValueError(
                "原生 Jev 的凭据环境文件请填写文件路径，不要把 KEY=VALUE 内容写进配置"
            )


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


def _positive_int_field(value: Any, field_name: str) -> None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} 必须是正整数") from None
    if number < 1:
        raise ValueError(f"{field_name} 必须是正整数")


def _validate_classification_maintenance_fields(config: dict) -> None:
    classifier = config.get("tag_classifier")
    if not isinstance(classifier, dict):
        return
    if "ttl_days" in classifier:
        _positive_int_field(classifier.get("ttl_days"), "tag_classifier.ttl_days")
    if "batch_size" in classifier:
        _positive_int_field(classifier.get("batch_size"), "tag_classifier.batch_size")
    if "concurrency" in classifier:
        _positive_int_field(classifier.get("concurrency"), "tag_classifier.concurrency")
    maintenance = classifier.get("maintenance")
    if isinstance(maintenance, dict) and "max_tags_per_run" in maintenance:
        _positive_int_field(
            maintenance.get("max_tags_per_run"),
            "tag_classifier.maintenance.max_tags_per_run",
        )
    if isinstance(maintenance, dict) and "min_profile_weight" in maintenance:
        try:
            float(maintenance.get("min_profile_weight"))
        except (TypeError, ValueError) as exc:
            raise ValueError("tag_classifier.maintenance.min_profile_weight 必须是数字") from exc

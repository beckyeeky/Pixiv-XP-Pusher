import copy
import logging
from pathlib import Path
from typing import Any

import yaml
from proxy_utils import normalize_proxy_url
from telegram_rich import normalize_rich_message_config

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config.yaml")


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
    tag_classifier_cfg.setdefault("api_key", "")
    tag_classifier_cfg.setdefault("base_url", "https://api.deepseek.com/v1")
    tag_classifier_cfg.setdefault("model", "deepseek-v4-flash")
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
    try:
        maintenance_cfg["min_profile_weight"] = float(maintenance_cfg.get("min_profile_weight", 0.0))
    except (TypeError, ValueError):
        maintenance_cfg["min_profile_weight"] = 0.0
    maintenance_cfg["prefer_unresolved_first"] = bool(maintenance_cfg.get("prefer_unresolved_first", True))

    judges = tag_classifier_cfg.get("judges", [])
    if not isinstance(judges, list):
        logger.warning("配置项 tag_classifier.judges 必须是列表，已回退为单模型")
        judges = []
    normalized_judges = []
    for index, judge in enumerate(judges):
        if not isinstance(judge, dict):
            continue
        item = dict(judge)
        item.setdefault("name", f"judge_{index + 1}")
        item.setdefault("provider", "openai")
        item.setdefault("api_key", "")
        item.setdefault("base_url", tag_classifier_cfg["base_url"])
        item.setdefault("model", tag_classifier_cfg["model"])
        normalized_judges.append(item)
    tag_classifier_cfg["judges"] = normalized_judges

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

    # === 全局 API Key 继承：profiler.ai → scorer / tag_classifier ===
    shared_key = (
        normalized.get("profiler", {}).get("ai", {}).get("api_key", "").strip()
    )
    if shared_key:
        # scorer
        scorer_cfg = normalized.get("ai", {}).get("scorer")
        if isinstance(scorer_cfg, dict) and not scorer_cfg.get("api_key", "").strip():
            scorer_cfg["api_key"] = shared_key
            logger.debug("ai.scorer.api_key 已从 profiler.ai 继承")

        # tag_classifier
        if not tag_classifier_cfg.get("api_key", "").strip():
            tag_classifier_cfg["api_key"] = shared_key
            logger.debug("tag_classifier.api_key 已从 profiler.ai 继承")
        for judge in tag_classifier_cfg["judges"]:
            if not str(judge.get("api_key", "")).strip():
                judge["api_key"] = shared_key

    profiler_cfg = normalized.get("profiler", {})
    if isinstance(profiler_cfg, dict):
        if not str(danbooru_cfg.get("login", "")).strip():
            danbooru_cfg["login"] = profiler_cfg.get("danbooru_login", "")
        if not str(danbooru_cfg.get("api_key", "")).strip():
            danbooru_cfg["api_key"] = profiler_cfg.get("danbooru_api_key", "")

    return normalized


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

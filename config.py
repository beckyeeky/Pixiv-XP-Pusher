import copy
import logging
from pathlib import Path
from typing import Any

import yaml

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

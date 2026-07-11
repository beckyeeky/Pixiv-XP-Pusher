#!/usr/bin/env python3
"""用现有配置填充最新配置模板，生成新的 config 文件。"""

from __future__ import annotations

import argparse
import copy
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


def create_yaml() -> YAML:
    """创建保留模板注释、引号和缩进的 YAML 读写器。"""
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def merge_template(template: Any, values: Any, *, keep_unknown: bool = False) -> Any:
    """以 template 为结构基准，用 values 覆盖相同字段。

    映射会递归合并，列表和值会整体替换。默认忽略模板中不存在的字段，
    这样生成的文件不会继续携带已废弃的配置；传入 keep_unknown 时保留它们。
    """
    if not isinstance(template, Mapping) or not isinstance(values, Mapping):
        return copy.deepcopy(values)

    # 空映射是模板为自由形式字典预留的位置，例如 boost_tags 和 topic_rules。
    # 其中的键由用户自行定义，不应被当作“模板外字段”丢弃。
    if not template:
        return copy.deepcopy(values)

    # 从模板副本开始修改，保留 ruamel.yaml 附着在映射和键上的注释。
    merged = copy.deepcopy(template)
    for key, template_value in template.items():
        if key in values:
            merged[key] = merge_template(
                template_value, values[key], keep_unknown=keep_unknown
            )

    if keep_unknown:
        for key, value in values.items():
            if key not in template:
                merged[key] = copy.deepcopy(value)

    return merged


def find_unknown_fields(template: Any, values: Any) -> dict[str, Any]:
    """返回 values 中未在 template 定义的字段，并保留其原始层级和值。"""
    if not isinstance(template, Mapping) or not isinstance(values, Mapping):
        return {}

    # 空映射允许用户自定义任意键，不能报告为模板外字段。
    if not template:
        return {}

    unknown: dict[str, Any] = {}
    for key, value in values.items():
        if key not in template:
            unknown[key] = value
            continue
        nested_unknown = find_unknown_fields(template[key], value)
        if nested_unknown:
            unknown[key] = nested_unknown
    return unknown


def migrate_legacy_judge_settings(template: Mapping, source: Mapping) -> tuple[dict[str, Any], list[str]]:
    """将旧版 tag_classifier 连接信息迁移至 providers/models。"""
    migrated = copy.deepcopy(source)
    messages: list[str] = []
    old_classifier = migrated.get("tag_classifier")
    template_classifier = template.get("tag_classifier")
    template_providers = template.get("providers")
    template_models = template.get("models")
    if not all(isinstance(item, Mapping) for item in (old_classifier, template_classifier, template_providers, template_models)):
        return migrated, messages

    judge_names = template_classifier.get("judges", [])
    if not isinstance(judge_names, list) or not judge_names or not isinstance(judge_names[0], str):
        return migrated, messages
    target_model = judge_names[0]
    template_model = template_models.get(target_model)
    if not isinstance(template_model, Mapping):
        return migrated, messages
    target_provider = template_model.get("provider")
    if not isinstance(target_provider, str) or target_provider not in template_providers:
        return migrated, messages

    legacy_keys = ("provider", "api_key", "base_url", "model")
    legacy_values = {key: old_classifier.pop(key) for key in legacy_keys if key in old_classifier}
    if not legacy_values:
        return migrated, messages

    providers = migrated.setdefault("providers", {})
    models = migrated.setdefault("models", {})
    target_provider_cfg = providers.setdefault(target_provider, {})
    target_model_cfg = models.setdefault(target_model, {})
    if "provider" in legacy_values:
        target_provider_cfg.setdefault("type", legacy_values["provider"])
        messages.append(f"tag_classifier.provider -> providers.{target_provider}.type")
    if "api_key" in legacy_values:
        target_provider_cfg.setdefault("api_key", legacy_values["api_key"])
        messages.append(f"tag_classifier.api_key -> providers.{target_provider}.api_key")
    if "base_url" in legacy_values:
        target_provider_cfg.setdefault("base_url", legacy_values["base_url"])
        messages.append(f"tag_classifier.base_url -> providers.{target_provider}.base_url")
    if "model" in legacy_values:
        target_model_cfg.setdefault("provider", target_provider)
        target_model_cfg.setdefault("model", legacy_values["model"])
        messages.append(f"tag_classifier.model -> models.{target_model}.model")
    return migrated, messages


def load_mapping(path: Path, yaml: YAML) -> Mapping:
    try:
        with path.open("r", encoding="utf-8") as stream:
            content = yaml.load(stream) or {}
    except Exception as exc:
        raise ValueError(f"无效 YAML：{path}") from exc
    if not isinstance(content, Mapping):
        raise ValueError(f"配置根节点必须是映射：{path}")
    return content


def dump_yaml(yaml: YAML, content: Any) -> str:
    stream = StringIO()
    yaml.dump(content, stream)
    return stream.getvalue()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将指定配置的值填入最新 config.example.yaml，生成新配置文件。"
    )
    parser.add_argument("source", type=Path, help="现有配置文件，例如 config.yaml")
    parser.add_argument(
        "-e", "--example", type=Path, default=Path("config.example.yaml"),
        help="配置模板路径（默认：config.example.yaml）",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("config.generated.yaml"),
        help="输出路径（默认：config.generated.yaml）",
    )
    parser.add_argument(
        "--keep-unknown", action="store_true",
        help="保留模板中未定义的旧配置字段",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    yaml = create_yaml()
    try:
        template = load_mapping(args.example, yaml)
        source = load_mapping(args.source, yaml)
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}")
        return 1

    source, migrations = migrate_legacy_judge_settings(template, source)
    result = merge_template(template, source, keep_unknown=args.keep_unknown)
    unknown_fields = find_unknown_fields(template, source)
    try:
        with args.output.open("w", encoding="utf-8") as stream:
            yaml.dump(result, stream)
    except OSError as exc:
        print(f"错误：无法写入 {args.output}：{exc}")
        return 1

    print(f"已生成 {args.output}（模板：{args.example}；来源：{args.source}）")
    if migrations:
        print("已迁移旧版字段：")
        print("\n".join(f"- {message}" for message in migrations))
    if unknown_fields:
        print("以下字段未在模板中找到：")
        print(dump_yaml(yaml, unknown_fields).rstrip())
    else:
        print("所有源配置字段均可在模板中找到。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

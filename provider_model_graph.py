"""Provider/Model graph rules shared by configuration and settings surfaces.

The graph is the authoritative boundary for Provider types, Model capabilities,
runtime resolution, and references from product functions.  Callers should not
need to reconstruct those relationships themselves.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


SINGLETON_PROVIDER_TYPES = frozenset({"pixiv", "danbooru"})
MODEL_CAPABILITIES = frozenset({"llm", "embedding"})
ALLOWED_PROVIDER_TYPES = frozenset({
    "openai",
    "deepseek",
    "anthropic",
    "google",
    "openai_compatible",
    "local",
    "pixiv",
    "danbooru",
    "brave_search",
    "tavily_search",
    "exa_search",
})
PROVIDER_TYPE_LABELS = {
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "anthropic": "Anthropic",
    "google": "Google",
    "brave_search": "Brave Search",
    "tavily_search": "Tavily Search",
    "exa_search": "Exa Search",
    "local": "本地服务",
    "openai_compatible": "自定义 OpenAI-compatible",
}
NON_MODEL_PROVIDER_TYPES = frozenset({
    "pixiv",
    "danbooru",
    "brave_search",
    "tavily_search",
    "exa_search",
})
OPENAI_CHAT_PROVIDER_TYPES = frozenset({
    "openai",
    "deepseek",
    "openai_compatible",
    "local",
})

MODEL_REFERENCES = (
    (("tag_classifier", "judges"), "标签审查 Judge", None, True),
    (("tag_mapping", "model"), "标签映射候选", "llm", False),
    (("ai", "embedding", "model"), "语义嵌入", "embedding", False),
    (("ai", "scorer", "model"), "智能精排", "llm", False),
    (
        ("tag_classifier", "grounded_judge", "search_classifier_model"),
        "Search-first 标签分类",
        "llm",
        False,
    ),
)
SEARCH_PROVIDER_REFERENCES = (
    (("tag_classifier", "grounded_judge", "brave_providers"), "brave_search", "Brave Search"),
    (("tag_classifier", "grounded_judge", "tavily_providers"), "tavily_search", "Tavily Search"),
    (("tag_classifier", "grounded_judge", "exa_providers"), "exa_search", "Exa Search"),
)
# Search-first keeps the historical Brave + Tavily requirement; Exa is only
# consumed by the optional Native Jev backend.
SEARCH_FIRST_PROVIDER_REFERENCES = SEARCH_PROVIDER_REFERENCES[:2]
# Native Jev pools live under its own section so that a Search-first save can
# never rewrite the pools the other backend depends on.
NATIVE_SEARCH_PROVIDER_REFERENCES = (
    (("tag_classifier", "grounded_judge", "native_jev", "exa_providers"),
     "exa_search", "Exa Search", "native_jev_exa_selection"),
    (("tag_classifier", "grounded_judge", "native_jev", "tavily_providers"),
     "tavily_search", "Tavily Search", "native_jev_tavily_selection"),
)
NATIVE_DEEPSEEK_PROVIDER_TYPES = frozenset({"deepseek", "openai_compatible", "local"})
# Single Provider references owned by the Native section.  A rename in the
# settings dialog must rewrite them together with the pools, or the stored
# configuration keeps pointing at the deleted name.
NATIVE_SINGLE_PROVIDER_REFERENCES = (
    (
        ("tag_classifier", "grounded_judge", "native_jev", "jev_provider"),
        "Jev 连接",
    ),
    (
        (
            "tag_classifier",
            "grounded_judge",
            "native_jev",
            "deepseek_search_provider",
        ),
        "第三顺位 DeepSeek Search",
    ),
)


def model_capabilities(model: dict) -> list[str]:
    """Return normalized, unique Model capabilities."""
    raw_capabilities = model.get("capabilities", model.get("capability", ["llm"]))
    if isinstance(raw_capabilities, str):
        raw_capabilities = [raw_capabilities]
    capabilities = [
        capability.strip()
        for capability in raw_capabilities
        if isinstance(capability, str) and capability.strip() in MODEL_CAPABILITIES
    ] if isinstance(raw_capabilities, list) else []
    return list(dict.fromkeys(capabilities or ["llm"]))


def _path_value(config: dict, path: tuple[str, ...], default: Any = None) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


@dataclass(frozen=True)
class ProviderModelGraph:
    """Read-only view over a configuration's Provider/Model relationships."""

    providers: dict[str, dict]
    models: dict[str, dict]
    config: dict

    @classmethod
    def from_config(cls, config: dict) -> "ProviderModelGraph":
        source = config if isinstance(config, dict) else {}
        providers = source.get("providers")
        models = source.get("models")
        return cls(
            providers=providers if isinstance(providers, dict) else {},
            models=models if isinstance(models, dict) else {},
            config=source,
        )

    def singleton_provider(self, provider_type: str) -> dict:
        matches = [
            provider for provider in self.providers.values()
            if isinstance(provider, dict) and provider.get("type") == provider_type
        ]
        return deepcopy(matches[0]) if len(matches) == 1 else {}

    def compatible_models(self, capability: str) -> dict[str, dict]:
        if capability not in MODEL_CAPABILITIES:
            raise ValueError(f"不支持的 Model capability: {capability}")
        return {
            name: deepcopy(model)
            for name, model in self.models.items()
            if isinstance(model, dict) and capability in model_capabilities(model)
        }

    def resolve_model(self, model_ref: str, capability: str | None = None) -> dict:
        model = self.models.get(model_ref)
        if not isinstance(model, dict):
            raise ValueError(f"未找到 Model: {model_ref}")
        if capability is not None and model_ref not in self.compatible_models(capability):
            raise ValueError(f"Model {model_ref} 不兼容 {capability} function")
        provider_name = model.get("provider")
        provider = self.providers.get(provider_name)
        if not isinstance(provider, dict):
            raise ValueError(f"Model {model_ref} 引用了不存在的 Provider: {provider_name}")
        resolved = deepcopy(model)
        resolved.update({
            "provider_name": provider_name,
            "provider": provider.get("type", "openai_compatible"),
            "api_key": provider.get("api_key", ""),
            "base_url": provider.get("base_url", ""),
        })
        return resolved

    def validate_deletion_references(self) -> None:
        """Reject submitted maps that remove an object still in use."""
        if not isinstance(self.config.get("providers"), dict) or not isinstance(
            self.config.get("models"), dict
        ):
            return
        judges = _path_value(self.config, ("tag_classifier", "judges"), [])
        if isinstance(judges, list):
            for judge_name in judges:
                if judge_name not in self.models:
                    raise ValueError(f"Model {judge_name} 仍被 Judge 引用，无法删除")

        for path, _label, _capability, multiple in MODEL_REFERENCES[1:4]:
            if multiple:
                continue
            model_ref = str(_path_value(self.config, path, "") or "").strip()
            if model_ref and model_ref not in self.models:
                reference = ".".join(path)
                raise ValueError(f"Model {model_ref} 仍被 {reference} 引用，无法删除")

        for model_name, model in self.models.items():
            if not isinstance(model, dict):
                continue
            provider_name = model.get("provider")
            if provider_name not in self.providers:
                raise ValueError(
                    f"Provider {provider_name} 仍被 Model {model_name} 引用，无法删除"
                )

        grounded = _path_value(self.config, ("tag_classifier", "grounded_judge"), {})
        if not isinstance(grounded, dict) or grounded.get("backend") != "native_jev":
            return
        native = grounded.get("native_jev")
        native = native if isinstance(native, dict) else {}
        for path, _provider_type, label, _target in NATIVE_SEARCH_PROVIDER_REFERENCES:
            names = _path_value(self.config, path, [])
            for name in names if isinstance(names, list) else ():
                if isinstance(name, str) and name and name not in self.providers:
                    raise ValueError(
                        f"Provider {name} 仍被 Native Jev 的 {label} 账号池引用，无法删除"
                    )
        jev_provider = str(native.get("jev_provider") or "").strip()
        if jev_provider and jev_provider not in self.providers:
            raise ValueError(
                f"Provider {jev_provider} 仍被 Native Jev 的 OpenRouter 连接引用，无法删除"
            )
        deepseek_provider = str(native.get("deepseek_search_provider") or "").strip()
        if deepseek_provider and deepseek_provider not in self.providers:
            raise ValueError(
                f"Provider {deepseek_provider} 仍被 Native Jev 的第三顺位 DeepSeek Search 引用，无法删除"
            )

    def validate(self) -> None:
        """Validate a normalized graph and all enabled function selections."""
        if not isinstance(self.config.get("providers"), dict) or not isinstance(
            self.config.get("models"), dict
        ):
            raise ValueError("Providers 和 Models 必须是对象")

        singleton_labels = {"pixiv": "Pixiv", "danbooru": "Danbooru"}
        singleton_counts = {key: 0 for key in singleton_labels}
        for name, provider in self.providers.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(provider, dict):
                raise ValueError("每个 Provider 都需要名称和配置")
            provider_type = provider.get("type")
            if provider_type not in ALLOWED_PROVIDER_TYPES:
                raise ValueError(f"Provider {name} 的类型无效")
            if provider_type in singleton_counts:
                singleton_counts[provider_type] += 1
            if provider_type == "openai_compatible" and not str(
                provider.get("base_url") or ""
            ).strip():
                raise ValueError(f"自定义 Provider {name} 需要 Base URL")
        for provider_type, count in singleton_counts.items():
            if count > 1:
                raise ValueError(
                    f"只能配置一个 {singleton_labels[provider_type]} Provider"
                )
        validate_singleton_providers(self.providers)

        for name, model in self.models.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(model, dict):
                raise ValueError("每个 Model 都需要名称和配置")
            provider_name = model.get("provider")
            if provider_name not in self.providers:
                raise ValueError(f"Model {name} 必须引用一个已配置 Provider")
            if self.providers[provider_name].get("type") in NON_MODEL_PROVIDER_TYPES:
                raise ValueError(f"Model {name} 必须引用 LLM Provider")
            if not str(model.get("model") or "").strip():
                raise ValueError(f"Model {name} 需要模型名称")
            raw_capabilities = model.get(
                "capabilities", model.get("capability", ["llm"])
            )
            if isinstance(raw_capabilities, str):
                raw_capabilities = [raw_capabilities]
            if (
                not isinstance(raw_capabilities, list)
                or not raw_capabilities
                or any(capability not in MODEL_CAPABILITIES for capability in raw_capabilities)
            ):
                raise ValueError(f"Model {name} 的能力必须是 llm 或 embedding")

        classifier = self.config.get("tag_classifier", {})
        if isinstance(classifier, dict):
            judges = classifier.get("judges", [])
            if not isinstance(judges, list) or any(name not in self.models for name in judges):
                raise ValueError("Judge 必须选择已配置的 Model")
            grounded = classifier.get("grounded_judge", {})
            if isinstance(grounded, dict) and grounded.get("backend") == "search_first":
                search_model_name = grounded.get("search_classifier_model")
                search_model = self.models.get(search_model_name, {})
                search_provider = self.providers.get(
                    search_model.get("provider"), {}
                ) if isinstance(search_model, dict) else {}
                if (
                    not search_model
                    or "llm" not in search_model.get("capabilities", [])
                    or search_provider.get("type") not in OPENAI_CHAT_PROVIDER_TYPES
                ):
                    raise ValueError(
                        "Search-first 必须选择 OpenAI Chat Completions 兼容的 LLM Model"
                    )
                for path, provider_type, label in SEARCH_FIRST_PROVIDER_REFERENCES:
                    selected = _path_value(self.config, path, [])
                    if not isinstance(selected, list) or not selected or any(
                        self.providers.get(name, {}).get("type") != provider_type
                        for name in selected
                    ):
                        raise ValueError(
                            f"Search-first 至少需要一个有效的 {label} Provider"
                        )
            elif isinstance(grounded, dict) and grounded.get("backend") == "native_jev":
                native = grounded.get("native_jev")
                native = native if isinstance(native, dict) else {}
                search_names = []
                for field_name, provider_type in (
                    ("exa_providers", "exa_search"),
                    ("tavily_providers", "tavily_search"),
                ):
                    selected = native.get(field_name) or []
                    if not isinstance(selected, list):
                        raise ValueError(f"Native Jev 的 {field_name} 必须是 Provider 列表")
                    for name in selected:
                        if self.providers.get(name, {}).get("type") != provider_type:
                            raise ValueError(
                                f"Native Jev 的 {field_name} 引用了类型不匹配的 Provider {name}"
                            )
                        search_names.append(name)
                if not search_names:
                    raise ValueError("Native Jev 至少需要一个有效的 Exa 或 Tavily Provider")
                jev_provider = self.providers.get(str(native.get("jev_provider") or ""), {})
                if jev_provider.get("type") not in OPENAI_CHAT_PROVIDER_TYPES:
                    raise ValueError("Native Jev 必须引用 OpenAI-compatible 的 OpenRouter Provider")
                if not str(jev_provider.get("base_url") or "").strip():
                    raise ValueError("Native Jev 的 OpenRouter Provider 需要 Base URL")

        for path, _label, capability, multiple in MODEL_REFERENCES[1:4]:
            if multiple:
                continue
            function_cfg = _path_value(self.config, path[:-1], {})
            if not isinstance(function_cfg, dict) or not function_cfg.get("enabled"):
                continue
            model_ref = function_cfg.get("model")
            label = ".".join(path)
            if not model_ref or model_ref not in self.models:
                raise ValueError(f"{label} 必须选择 {capability} Model")
            if capability not in self.models[model_ref].get("capabilities", ["llm"]):
                raise ValueError(f"{label} 必须选择 {capability} Model")


def validate_singleton_providers(providers: dict) -> None:
    """Reject malformed typed Provider configuration before runtime."""
    counts = {provider_type: 0 for provider_type in SINGLETON_PROVIDER_TYPES}
    for provider in providers.values() if isinstance(providers, dict) else ():
        if isinstance(provider, dict) and provider.get("type") in counts:
            counts[provider["type"]] += 1
    labels = {"pixiv": "Pixiv", "danbooru": "Danbooru"}
    for provider_type, count in counts.items():
        if count != 1:
            raise ValueError(f"必须配置且只能配置一个 {labels[provider_type]} Provider")


def settings_rules() -> dict:
    """Return the graph descriptor consumed by the settings browser adapter."""
    return {
        "singleton_provider_types": sorted(SINGLETON_PROVIDER_TYPES),
        "non_model_provider_types": sorted(NON_MODEL_PROVIDER_TYPES),
        "openai_chat_provider_types": sorted(OPENAI_CHAT_PROVIDER_TYPES),
        "editable_provider_types": [
            {"type": provider_type, "label": label}
            for provider_type, label in PROVIDER_TYPE_LABELS.items()
        ],
        "model_references": [
            {
                "path": list(path),
                "label": label,
                "capability": capability,
                "multiple": multiple,
            }
            for path, label, capability, multiple in MODEL_REFERENCES
        ],
        "search_provider_references": [
            {
                "path": list(path),
                "provider_type": provider_type,
                "label": label,
                "target_id": f"{path[-1][:-1]}_selection",
            }
            for path, provider_type, label in SEARCH_PROVIDER_REFERENCES
        ],
        "native_search_provider_references": [
            {
                "path": list(path),
                "provider_type": provider_type,
                "label": label,
                "target_id": target_id,
            }
            for path, provider_type, label, target_id in NATIVE_SEARCH_PROVIDER_REFERENCES
        ],
        "native_connection_provider_types": sorted(OPENAI_CHAT_PROVIDER_TYPES),
        "native_single_provider_references": [
            {"path": list(path), "label": label}
            for path, label in NATIVE_SINGLE_PROVIDER_REFERENCES
        ],
        "native_deepseek_provider_types": sorted(NATIVE_DEEPSEEK_PROVIDER_TYPES),
        "native_jev_defaults": {
            "model": "jev-1.13",
            "endpoint": "https://openrouter.ai/api/v1/systemone",
            "min_confidence": 0.9,
            "min_agreeing_sources": 2,
        },
    }

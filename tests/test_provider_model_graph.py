import unittest

from provider_model_graph import ProviderModelGraph, settings_rules


class ProviderModelGraphTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "providers": {
                "pixiv": {"type": "pixiv"},
                "danbooru": {"type": "danbooru"},
                "gateway": {
                    "type": "openai_compatible",
                    "base_url": "https://example.invalid/v1",
                    "api_key": "secret",
                },
                "brave": {"type": "brave_search"},
                "tavily": {"type": "tavily_search"},
            },
            "models": {
                "judge": {
                    "provider": "gateway",
                    "model": "gpt",
                    "capabilities": ["llm"],
                },
                "embed": {
                    "provider": "gateway",
                    "model": "embed",
                    "capabilities": ["embedding"],
                },
            },
            "tag_classifier": {
                "judges": ["judge"],
                "grounded_judge": {
                    "backend": "search_first",
                    "search_classifier_model": "judge",
                    "brave_providers": ["brave"],
                    "tavily_providers": ["tavily"],
                },
            },
            "tag_mapping": {"enabled": True, "model": "judge"},
            "ai": {
                "embedding": {"enabled": True, "model": "embed"},
                "scorer": {"enabled": True, "model": "judge"},
            },
        }

    def test_graph_validates_and_resolves_runtime_model(self):
        graph = ProviderModelGraph.from_config(self.config)

        graph.validate()
        resolved = graph.resolve_model("judge", "llm")

        self.assertEqual(resolved["provider_name"], "gateway")
        self.assertEqual(resolved["provider"], "openai_compatible")
        self.assertEqual(resolved["api_key"], "secret")

    def test_settings_descriptor_exposes_the_same_reference_graph(self):
        rules = settings_rules()

        labels = {reference["label"] for reference in rules["model_references"]}
        provider_types = {
            option["type"] for option in rules["editable_provider_types"]
        }

        self.assertIn("标签审查 Judge", labels)
        self.assertIn("Search-first 标签分类", labels)
        self.assertEqual(
            set(rules["openai_chat_provider_types"]),
            {"openai", "deepseek", "openai_compatible", "local"},
        )
        self.assertNotIn("pixiv", provider_types)
        self.assertNotIn("danbooru", provider_types)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

import config
from ai_scorer import AIScorer
from embedder import Embedder
from push_run import PushRun
from web.settings_editor import apply_settings_payload


class ModelSelectionTests(unittest.TestCase):
    def test_normalize_models_keeps_capabilities_and_defaults_llm(self):
        normalized = config.normalize_config({
            "providers": {
                "gateway": {"type": "openai_compatible", "api_key": "key"},
            },
            "models": {
                "judge": {"provider": "gateway", "model": "gpt"},
                "embedding": {
                    "provider": "gateway",
                    "model": "embed",
                    "capabilities": ["embedding"],
                },
            },
        })

        self.assertEqual(normalized["models"]["judge"]["capabilities"], ["llm"])
        self.assertEqual(normalized["models"]["embedding"]["capabilities"], ["embedding"])

    def test_compatible_models_only_returns_models_for_requested_function(self):
        normalized = config.normalize_config({
            "providers": {
                "gateway": {"type": "openai_compatible", "api_key": "key"},
            },
            "models": {
                "judge": {"provider": "gateway", "model": "gpt", "capability": "llm"},
                "embedding": {"provider": "gateway", "model": "embed", "capability": "embedding"},
                "both": {"provider": "gateway", "model": "multi", "capabilities": ["llm", "embedding"]},
            },
        })

        self.assertEqual(set(config.get_compatible_models(normalized, "embedding")), {"embedding", "both"})
        self.assertEqual(set(config.get_compatible_models(normalized, "llm")), {"judge", "both"})

    def test_resolve_model_merges_provider_credentials_and_rejects_incompatible_model(self):
        normalized = config.normalize_config({
            "providers": {
                "gateway": {
                    "type": "openai_compatible",
                    "api_key": "provider-key",
                    "base_url": "https://gateway.example/v1",
                },
            },
            "models": {
                "embedding": {"provider": "gateway", "model": "embed", "capability": "embedding"},
            },
        })

        resolved = config.resolve_model(normalized, "embedding", "embedding")
        self.assertEqual(resolved["api_key"], "provider-key")
        self.assertEqual(resolved["base_url"], "https://gateway.example/v1")
        self.assertEqual(resolved["model"], "embed")
        with self.assertRaises(ValueError):
            config.resolve_model(normalized, "embedding", "llm")

    def test_normalize_migrates_legacy_embedding_and_scorer_configs(self):
        normalized = config.normalize_config({
            "ai": {
                "embedding": {
                    "enabled": True,
                    "provider": "openai_compatible",
                    "api_key": "embedding-key",
                    "base_url": "https://embed.example/v1",
                    "model": "embedding-v1",
                },
                "scorer": {
                    "enabled": True,
                    "provider": "openai_compatible",
                    "api_key": "scorer-key",
                    "base_url": "https://score.example/v1",
                    "model": "scorer-v1",
                },
            },
        })

        embedding_ref = normalized["ai"]["embedding"]["model"]
        scorer_ref = normalized["ai"]["scorer"]["model"]
        self.assertIn(embedding_ref, normalized["models"])
        self.assertIn(scorer_ref, normalized["models"])
        self.assertEqual(normalized["models"][embedding_ref]["capabilities"], ["embedding"])
        self.assertEqual(normalized["models"][scorer_ref]["capabilities"], ["llm"])
        self.assertEqual(config.resolve_model(normalized, embedding_ref, "embedding")["api_key"], "embedding-key")
        self.assertEqual(config.resolve_model(normalized, scorer_ref, "llm")["api_key"], "scorer-key")

    def test_push_run_builders_resolve_selected_models(self):
        config_data = config.normalize_config({
            "providers": {
                "gateway": {"type": "openai_compatible", "api_key": "key", "base_url": "https://example/v1"},
            },
            "models": {
                "embedding": {"provider": "gateway", "model": "embed", "capability": "embedding"},
                "scorer": {"provider": "gateway", "model": "score", "capability": "llm"},
            },
            "ai": {
                "embedding": {"enabled": True, "model": "embedding"},
                "scorer": {"enabled": True, "model": "scorer"},
            },
        })
        runner = PushRun.__new__(PushRun)
        runner.config = config_data
        with patch("embedder.HAS_OPENAI", True), patch("embedder.AsyncOpenAI", return_value=object()), \
             patch("ai_scorer.HAS_OPENAI", True), patch("ai_scorer.AsyncOpenAI", return_value=object()):
            embedder = runner._build_embedder()
            scorer = runner._build_ai_scorer()

        self.assertEqual(embedder.model, "embed")
        self.assertEqual(embedder.provider, "openai_compatible")
        self.assertEqual(scorer.model, "score")
        self.assertEqual(scorer.provider, "openai_compatible")

    def test_settings_reject_incompatible_function_selection(self):
        current = {
            "web": {"require_login_password": False, "password": ""},
            "providers": {
                "gateway": {"type": "openai_compatible", "base_url": "https://gateway.example/v1"},
            },
            "models": {
                "judge": {"provider": "gateway", "model": "gpt", "capabilities": ["llm"]},
            },
            "ai": {"embedding": {"enabled": True, "model": "judge"}},
        }

        with self.assertRaisesRegex(ValueError, "ai.embedding.model"):
            apply_settings_payload(current, {}, str)


if __name__ == "__main__":
    unittest.main()

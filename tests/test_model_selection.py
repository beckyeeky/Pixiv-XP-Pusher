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

    def test_known_model_catalogs_do_not_mix_capabilities(self):
        llm_catalog = config.get_known_model_catalog("llm")
        embedding_catalog = config.get_known_model_catalog("embedding")

        self.assertIn("gpt-4o-mini", llm_catalog)
        self.assertIn("text-embedding-3-small", embedding_catalog)
        self.assertNotIn("text-embedding-3-small", llm_catalog)
        self.assertNotIn("gpt-4o-mini", embedding_catalog)
        with self.assertRaises(ValueError):
            config.get_known_model_catalog("vision")

    def test_normalize_migrates_legacy_profiler_ai_to_candidate_only_tag_mapping(self):
        normalized = config.normalize_config({
            "profiler": {
                "ai": {
                    "enabled": True,
                    "provider": "openai_compatible",
                    "api_key": "profiler-key",
                    "base_url": "https://profiler.example/v1",
                    "model": "gpt-profiler",
                    "concurrency": 8,
                    "batch_size": 50,
                },
            },
        })

        model_ref = normalized["tag_mapping"]["model"]
        self.assertIn(model_ref, normalized["models"])
        self.assertEqual(normalized["models"][model_ref]["capabilities"], ["llm"])
        self.assertNotIn("ai", normalized["profiler"])
        self.assertNotIn("api_key", normalized["tag_mapping"])
        self.assertNotIn("base_url", normalized["tag_mapping"])
        self.assertNotIn("provider", normalized["tag_mapping"])
        self.assertNotIn("concurrency", normalized["tag_mapping"])
        resolved = config.resolve_tag_mapping_config(normalized)
        self.assertEqual(resolved["api_key"], "profiler-key")
        self.assertEqual(resolved["base_url"], "https://profiler.example/v1")
        self.assertEqual(resolved["model"], "gpt-profiler")
        self.assertEqual(resolved["batch_size"], 50)

    def test_settings_reject_incompatible_tag_mapping_model(self):
        current = {
            "web": {"require_login_password": False, "password": ""},
            "providers": {
                "gateway": {"type": "openai_compatible", "base_url": "https://gateway.example/v1"},
            },
            "models": {
                "embed_only": {"provider": "gateway", "model": "embed", "capabilities": ["embedding"]},
            },
            "tag_mapping": {"enabled": True, "model": "embed_only"},
        }

        with self.assertRaisesRegex(ValueError, "tag_mapping.model"):
            apply_settings_payload(current, {}, str)

    def test_task_manager_does_not_construct_a_mapping_generator_in_profile_runtime(self):
        from task_manager import setup_services

        config_data = config.normalize_config({
            "providers": {
                "pixiv": {"type": "pixiv", "refresh_token": "token", "user_id": 1},
                "danbooru": {"type": "danbooru"},
                "gateway": {
                    "type": "openai_compatible",
                    "api_key": "profiler-runtime-key",
                    "base_url": "https://profiler-runtime.example/v1",
                },
            },
            "models": {
                "profiler_llm": {
                    "provider": "gateway",
                    "model": "gpt-runtime",
                    "capabilities": ["llm"],
                },
            },
            "tag_mapping": {"enabled": True, "model": "profiler_llm"},
            "web": {"require_login_password": False, "password": ""},
        })

        class DummyClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def login(self):
                return None

        async def fake_setup_notifiers(*_args, **_kwargs):
            return []

        with patch("task_manager.init_db", return_value=None), \
             patch("task_manager.PixivClient", DummyClient), \
             patch("task_manager.setup_notifiers", side_effect=fake_setup_notifiers):
            result = __import__("asyncio").run(setup_services(config_data))

        profiler = result[2]
        self.assertFalse(hasattr(profiler, "ai_processor"))


if __name__ == "__main__":
    unittest.main()

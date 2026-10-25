"""Settings/Tags surface adaptation for the two classification backends.

The load → snapshot → UI collect/save → normalize path is exercised with the
*rendered page*: the real Jinja templates are rendered from the real settings
snapshot, the page script runs in Node against a DOM shim, and the payload that
`collectConfig()` actually produced is fed into `apply_settings_payload`.

Nothing here calls a network, a model, a search engine, Telegram or production
configuration: every path is a temporary fixture.
"""

from __future__ import annotations

import copy
import json
import unittest

from classification_maintenance import (
    OUTCOME_LABELS,
    OUTCOME_STATUSES,
    backend_views_for_settings,
    classification_backend_view,
    outcome_counts,
    outcome_line,
)
from config import get_known_model_catalog, normalize_config
from provider_model_graph import settings_rules
from web.settings_editor import (
    apply_settings_payload,
    build_settings_snapshot,
    redact_sensitive_config,
)
from tests.ui_render_probe import node_available, render_page, run_collect_probe


def native_config() -> dict:
    return {
        "web": {"require_login_password": False, "password": ""},
        "providers": {
            "pixiv": {"type": "pixiv", "refresh_token": "pixiv-token", "user_id": 1},
            "danbooru": {"type": "danbooru"},
            "exa_1": {"type": "exa_search", "api_key": "exa-secret-value"},
            "exa_2": {"type": "exa_search", "api_key": ""},
            "tavily_1": {"type": "tavily_search", "api_key_env": "TAVILY_API_KEY"},
            "brave_1": {"type": "brave_search", "api_key": "brave-secret-value"},
            "openrouter": {
                "type": "openai_compatible",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "sk-openrouter-secret",
            },
            "deepseek": {
                "type": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "deepseek-secret",
            },
        },
        "models": {
            "deepseek_v4": {
                "provider": "openrouter",
                "model": "deepseek-v4-flash",
                "capabilities": ["llm"],
            },
        },
        "tag_classifier": {
            "enabled": True,
            "ttl_days": 30,
            "batch_size": 50,
            "concurrency": 5,
            "maintenance": {
                "max_tags_per_run": 5,
                "concurrency": 1,
                "min_profile_weight": 1.0,
                "allow_automatic_external_search": False,
                "prefer_unresolved_first": True,
            },
            "grounded_judge": {
                "backend": "native_jev",
                "search_classifier_model": "deepseek_v4",
                "brave_providers": ["brave_1"],
                "tavily_providers": ["tavily_1"],
                "brave_request_limit": 1000,
                "tavily_request_limit": 500,
                "quota_state_path": "data/search_judge_quota_usage.json",
                "native_jev": {
                    "enabled": True,
                    "model": "jev-1.13",
                    "jev_provider": "openrouter",
                    "jev_endpoint": "",
                    "exa_providers": ["exa_1"],
                    "tavily_providers": ["tavily_1"],
                    "deepseek_search_provider": "deepseek",
                    "min_confidence": 0.9,
                    "min_agreeing_sources": 2,
                    "top_k": 3,
                    "max_evidence_chars": 2500,
                    "require_tag_mention": True,
                    "state_path": "data/native_jev_state.json",
                    "per_run_request_budget": 12,
                    "audit_log_path": "data/native_jev_audit.jsonl",
                    "env_file": "/secure/native-jev.env",
                    "quota_estimates": {
                        "exa_monthly_requests": 1000,
                        "tavily_monthly_requests": 500,
                        "deepseek_search_monthly_requests": 200,
                    },
                },
            },
        },
        "filter": {"daily_limit": 20},
    }


def settings_context() -> dict:
    """Context identical to the /settings route, built from the real helpers."""
    current = native_config()
    snapshot = build_settings_snapshot(redact_sensitive_config(current))
    views = backend_views_for_settings(snapshot)
    views["current"] = classification_backend_view(current)
    return {
        "active_page": "settings",
        "config": snapshot,
        "push_schedule": "每日 00:00",
        "known_llm_models": get_known_model_catalog("llm"),
        "known_embedding_models": get_known_model_catalog("embedding"),
        "provider_model_rules": settings_rules(),
        "classification_backend": views["current"],
        "classification_backend_views": views,
    }


def tags_context(config: dict | None = None) -> dict:
    config = config if config is not None else native_config()
    return {
        "active_page": "tags",
        "config": config,
        "classification_backend": classification_backend_view(config),
        "outcome_labels": OUTCOME_LABELS,
    }


def rendered_settings_page() -> str:
    return render_page("settings_v2.html", settings_context())


def collect_from_rendered_page(scenarios: list[dict]) -> dict:
    return run_collect_probe(rendered_settings_page(), scenarios)


SCENARIOS = [
    {"name": "baseline"},
    {"name": "unrelated", "set": {"filter_daily_limit": "33"}},
    {
        "name": "native_edit",
        "set": {
            "native_jev_model": "jev-1.14",
            "native_jev_min_confidence": "0.95",
            "native_jev_env_file": "/secure/other.env",
        },
    },
    {"name": "auto_search_on", "check": {"tag_classifier_automatic_external_search": True}},
    {"name": "leave_native", "backend": "search_first", "check": {"native_jev_enabled": False}},
    {
        "name": "pool_add",
        "togglePool": [{
            "path": ["tag_classifier", "grounded_judge", "native_jev", "exa_providers"],
            "name": "exa_2",
            "enabled": True,
        }],
    },
    {"name": "floor_attempt", "set": {"native_jev_min_confidence": "0.5"}},
]


@unittest.skipUnless(node_available(), "node is required for the DOM collect probe")
class RenderedSettingsCollectTests(unittest.TestCase):
    """The page script's own payload must survive the real save path."""

    @classmethod
    def setUpClass(cls):
        cls.probe = collect_from_rendered_page(SCENARIOS)
        cls.results = {item["name"]: item for item in cls.probe["results"]}

    def payload(self, name: str) -> dict:
        result = self.results[name]
        self.assertIsNone(result.get("error"), result.get("error"))
        self.assertIsNotNone(result["payload"])
        return result["payload"]

    def save(self, name: str, current: dict | None = None) -> dict:
        current = current if current is not None else native_config()
        return apply_settings_payload(current, self.payload(name), str)

    def test_no_scenario_crashed_and_every_payload_has_the_backend_select(self):
        for name, result in self.results.items():
            with self.subTest(scenario=name):
                self.assertIsNone(result.get("error"), result.get("error"))
                self.assertIn("tag_classifier", result["payload"])

    def test_untouched_form_round_trips_without_losing_the_native_subtree(self):
        payload = self.payload("baseline")
        grounded = payload["tag_classifier"]["grounded_judge"]
        self.assertEqual(grounded["backend"], "native_jev")
        self.assertTrue(grounded["native_jev"]["enabled"])
        self.assertEqual(grounded["native_jev"]["state_path"], "data/native_jev_state.json")
        self.assertEqual(grounded["native_jev"]["per_run_request_budget"], 12)
        self.assertEqual(grounded["native_jev"]["audit_log_path"], "data/native_jev_audit.jsonl")
        self.assertEqual(grounded["native_jev"]["exa_providers"], ["exa_1"])

        merged = self.save("baseline")
        saved = merged["tag_classifier"]["grounded_judge"]
        self.assertEqual(saved["backend"], "native_jev")
        self.assertTrue(saved["native_jev"]["enabled"])
        self.assertEqual(saved["native_jev"]["state_path"], "data/native_jev_state.json")
        self.assertEqual(saved["native_jev"]["min_confidence"], 0.9)
        # Credentials are preserved, never replaced by the masked fingerprint.
        self.assertEqual(merged["providers"]["exa_1"]["api_key"], "exa-secret-value")
        self.assertEqual(merged["providers"]["openrouter"]["api_key"], "sk-openrouter-secret")

    def test_unrelated_edit_keeps_backend_and_native_section(self):
        payload = self.payload("unrelated")
        self.assertEqual(payload["filter"]["daily_limit"], 33)
        merged = self.save("unrelated")
        self.assertEqual(merged["filter"]["daily_limit"], 33)
        grounded = merged["tag_classifier"]["grounded_judge"]
        self.assertEqual(grounded["backend"], "native_jev")
        self.assertEqual(
            grounded["native_jev"], normalize_config(copy.deepcopy(native_config()))
            ["tag_classifier"]["grounded_judge"]["native_jev"],
        )

    def test_explicit_native_edit_is_saved_and_undeclared_keys_survive(self):
        payload = self.payload("native_edit")
        native_payload = payload["tag_classifier"]["grounded_judge"]["native_jev"]
        self.assertEqual(native_payload["model"], "jev-1.14")
        self.assertAlmostEqual(native_payload["min_confidence"], 0.95)
        self.assertEqual(native_payload["env_file"], "/secure/other.env")

        merged = self.save("native_edit")
        native = merged["tag_classifier"]["grounded_judge"]["native_jev"]
        self.assertEqual(native["model"], "jev-1.14")
        self.assertAlmostEqual(native["min_confidence"], 0.95)
        self.assertEqual(native["env_file"], "/secure/other.env")
        self.assertEqual(native["per_run_request_budget"], 12)
        self.assertEqual(native["state_path"], "data/native_jev_state.json")
        self.assertEqual(native["top_k"], 3)
        self.assertEqual(merged["tag_classifier"]["grounded_judge"]["backend"], "native_jev")

    def test_automatic_external_search_toggle_does_not_move_the_backend(self):
        payload = self.payload("auto_search_on")
        self.assertTrue(payload["tag_classifier"]["maintenance"]["allow_automatic_external_search"])
        merged = self.save("auto_search_on")
        self.assertTrue(
            merged["tag_classifier"]["maintenance"]["allow_automatic_external_search"]
        )
        self.assertEqual(merged["tag_classifier"]["grounded_judge"]["backend"], "native_jev")
        self.assertTrue(merged["tag_classifier"]["grounded_judge"]["native_jev"]["enabled"])

    def test_explicit_two_key_action_can_leave_the_native_backend(self):
        payload = self.payload("leave_native")
        self.assertEqual(payload["tag_classifier"]["grounded_judge"]["backend"], "search_first")
        self.assertFalse(payload["tag_classifier"]["grounded_judge"]["native_jev"]["enabled"])
        merged = self.save("leave_native")
        grounded = merged["tag_classifier"]["grounded_judge"]
        self.assertEqual(grounded["backend"], "search_first")
        self.assertFalse(grounded["native_jev"]["enabled"])
        self.assertEqual(grounded["search_classifier_model"], "deepseek_v4")
        self.assertEqual(grounded["brave_providers"], ["brave_1"])
        self.assertEqual(grounded["tavily_providers"], ["tavily_1"])

    def test_pool_selection_is_rendered_and_saved(self):
        exa_html = self.results["pool_add"]["poolHtml"]["native_jev_exa_selection"]
        self.assertIn("exa_1", exa_html)
        self.assertIn("exa_2", exa_html)
        self.assertIn(
            "brave_1", self.results["pool_add"]["poolHtml"]["brave_provider_selection"]
        )
        payload = self.payload("pool_add")
        self.assertEqual(
            payload["tag_classifier"]["grounded_judge"]["native_jev"]["exa_providers"],
            ["exa_1", "exa_2"],
        )
        merged = self.save("pool_add")
        self.assertEqual(
            merged["tag_classifier"]["grounded_judge"]["native_jev"]["exa_providers"],
            ["exa_1", "exa_2"],
        )

    def test_submitted_threshold_below_the_floor_is_rejected_instead_of_downgraded(self):
        with self.assertRaisesRegex(ValueError, "最低置信度"):
            self.save("floor_attempt")


class SettingsTemplateContractTests(unittest.TestCase):
    """The rendered page must expose the real backend, not the old fixed text."""

    @classmethod
    def setUpClass(cls):
        cls.html = rendered_settings_page()

    def test_backend_select_reflects_and_edits_the_stored_backend(self):
        self.assertIn('id="tag_classifier_backend"', self.html)
        self.assertIn('<option value="native_jev" selected>', self.html)
        self.assertNotIn('groundedJudge.backend = \'search_first\'', self.html)
        self.assertIn("未显式设置（保存时保持现有后端）", self.html)

    def test_native_and_search_first_controls_are_rendered(self):
        for control in (
            "native_jev_enabled", "native_jev_provider", "native_jev_model",
            "native_jev_endpoint", "native_jev_env_file", "native_jev_exa_selection",
            "native_jev_tavily_selection", "native_jev_deepseek_provider",
            "native_jev_min_confidence", "native_jev_min_agreeing_sources",
            "native_jev_quota_exa", "tag_classifier_automatic_external_search",
            "tag_classifier_max_tags_per_run", "tag_classifier_concurrency",
            "tag_classifier_min_profile_weight", "tag_classifier_search_model",
            "brave_provider_selection", "tavily_provider_selection",
            # The rules expose an Exa target too; a missing element used to abort
            # the whole settings script with a TypeError.
            "exa_provider_selection",
        ):
            with self.subTest(control=control):
                self.assertIn(f'id="{control}"', self.html)

    def test_rendered_page_shows_the_effective_backend_without_secrets(self):
        self.assertIn("原生 Jev 后端（native_jev）", self.html)
        self.assertIn("DeepSeek Search", self.html)
        self.assertIn("仅第三兜底", self.html)
        self.assertIn("自动外搜", self.html)
        self.assertIn("仅本地估算", self.html)
        for secret in ("sk-openrouter-secret", "exa-secret-value", "brave-secret-value", "deepseek-secret"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, self.html)

    def test_threshold_floor_is_visible_in_the_control(self):
        self.assertIn('min="0.9"', self.html)
        self.assertIn('min="2"', self.html)


class TagsPageRenderTests(unittest.TestCase):
    def test_tags_page_describes_the_native_backend_and_drops_the_old_claim(self):
        html = render_page("tags.html", tags_context())
        self.assertIn("原生 Jev 后端（native_jev）", html)
        self.assertIn("自动外搜：已关闭", html)
        self.assertIn("原生 Jev 已启用", html)
        self.assertIn("本次未覆盖", html)  # preserved wording is reachable in JS
        self.assertNotIn("使用 Brave/Tavily 搜索与 DeepSeek 分类", html)
        for status in OUTCOME_STATUSES:
            self.assertIn(OUTCOME_LABELS[status], html)

    def test_tags_page_describes_search_first_when_that_backend_is_active(self):
        config = native_config()
        config["tag_classifier"]["grounded_judge"]["backend"] = "search_first"
        html = render_page("tags.html", tags_context(config))
        self.assertIn("Search-first 后端（search_first）", html)
        self.assertIn("Brave", html)
        self.assertNotIn("原生 Jev 后端（native_jev）", html)


class BackendViewTests(unittest.TestCase):
    def test_native_view_reports_order_jev_and_thresholds(self):
        view = classification_backend_view(native_config())
        self.assertEqual(view["backend"], "native_jev")
        self.assertTrue(view["native_enabled"])
        self.assertFalse(view["automatic_external_search"])
        self.assertEqual(view["jev"]["model"], "jev-1.13")
        self.assertEqual(view["maintenance"]["max_tags_per_run"], 5)
        self.assertEqual(view["maintenance"]["concurrency"], 1)
        self.assertAlmostEqual(view["maintenance"]["min_profile_weight"], 1.0)
        self.assertIn("第三兜底", view["search_order_text"])
        self.assertIn("Jev", view["line"])
        pool_sources = {(pool["engine"], pool["name"]): pool["credential_source"] for pool in view["pools"]}
        self.assertEqual(pool_sources[("exa", "exa_1")], "provider")
        self.assertEqual(pool_sources[("tavily", "tavily_1")], "missing")
        # No credential value may appear anywhere in the view.
        serialized = json.dumps(view, ensure_ascii=False)
        for secret in ("exa-secret-value", "sk-openrouter-secret", "deepseek-secret"):
            self.assertNotIn(secret, serialized)

    def test_unset_and_legacy_backends_are_described_without_claiming_a_switch(self):
        unset = classification_backend_view({"tag_classifier": {}})
        self.assertEqual(unset["backend"], "")
        self.assertIn("未显式设置", unset["line"])
        legacy = classification_backend_view(
            {"tag_classifier": {"grounded_judge": {"backend": "gemini"}}}
        )
        self.assertTrue(legacy["legacy"])
        self.assertIn("旧版", legacy["line"])

    def test_backend_views_cover_both_selectable_backends(self):
        views = backend_views_for_settings(native_config())
        self.assertEqual(views["native_jev"]["backend"], "native_jev")
        self.assertEqual(views["search_first"]["backend"], "search_first")
        for view in views.values():
            self.assertTrue(view["line"])
            self.assertTrue(view["facts"])

    def test_outcome_line_covers_preserved_and_all_statuses(self):
        summary = {
            "attempted": 4, "accepted": 1, "deferred": 1, "preserved": 1,
            "human_override": 1, "failed": 0, "unresolved": 0,
        }
        self.assertEqual(set(outcome_counts(summary)), set(OUTCOME_STATUSES))
        line = outcome_line(summary, include_zero=True)
        for status in OUTCOME_STATUSES:
            self.assertIn(OUTCOME_LABELS[status], line)
        self.assertIn("保留既有分类 1", line)
        compact = outcome_line(summary)
        self.assertIn("保留既有分类 1", compact)
        self.assertNotIn("失败 0", compact)


@unittest.skipUnless(node_available(), "node is required for the DOM collect probe")
class SearchFirstRenderedPageTests(unittest.TestCase):
    """A page rendered for search_first must not rewrite the dormant native section."""

    @classmethod
    def setUpClass(cls):
        config = native_config()
        config["tag_classifier"]["grounded_judge"]["backend"] = "search_first"
        context = settings_context()
        from classification_maintenance import backend_views_for_settings, classification_backend_view
        from web.settings_editor import build_settings_snapshot, redact_sensitive_config

        snapshot = build_settings_snapshot(redact_sensitive_config(config))
        views = backend_views_for_settings(snapshot)
        views["current"] = classification_backend_view(config)
        context.update({
            "config": snapshot,
            "classification_backend": views["current"],
            "classification_backend_views": views,
        })
        cls.config = config
        cls.probe = run_collect_probe(render_page("settings_v2.html", context), [
            {"name": "baseline"},
            {"name": "edit_model", "set": {"tag_classifier_search_model": "deepseek_v4"}},
        ])
        cls.results = {item["name"]: item for item in cls.probe["results"]}

    def test_unrelated_save_keeps_search_first_and_the_dormant_native_section(self):
        payload = self.results["baseline"]["payload"]
        self.assertIsNone(self.results["baseline"].get("error"), self.results["baseline"].get("error"))
        self.assertEqual(payload["tag_classifier"]["grounded_judge"]["backend"], "search_first")
        merged = apply_settings_payload(self.config, payload, str)
        grounded = merged["tag_classifier"]["grounded_judge"]
        self.assertEqual(grounded["backend"], "search_first")
        native = grounded["native_jev"]
        self.assertTrue(native["enabled"])  # dormant but preserved verbatim
        self.assertEqual(native["exa_providers"], ["exa_1"])
        self.assertEqual(native["per_run_request_budget"], 12)
        self.assertEqual(grounded["brave_providers"], ["brave_1"])


class SavePathGuardTests(unittest.TestCase):
    """Legacy clients and empty payloads may never lose the native configuration."""
    def test_empty_or_unrelated_api_payload_keeps_the_whole_configuration(self):
        current = native_config()
        for payload in ({}, {"network": {"max_concurrency": 7}}, {"web": {"require_login_password": False}}):
            with self.subTest(payload=payload):
                merged = apply_settings_payload(current, payload, str)
                grounded = merged["tag_classifier"]["grounded_judge"]
                self.assertEqual(grounded["backend"], "native_jev")
                native = grounded["native_jev"]
                self.assertTrue(native["enabled"])
                for key, value in current["tag_classifier"]["grounded_judge"]["native_jev"].items():
                    self.assertEqual(native[key], value, f"{key} 丢失")

    def test_legacy_search_first_payload_without_native_section_cannot_downgrade(self):
        current = native_config()
        merged = apply_settings_payload(
            current,
            {"tag_classifier": {"grounded_judge": {"backend": "search_first"}}},
            str,
        )
        grounded = merged["tag_classifier"]["grounded_judge"]
        self.assertEqual(grounded["backend"], "native_jev")
        self.assertTrue(grounded["native_jev"]["enabled"])

    def test_explicit_action_can_still_switch_and_keeps_the_section_dormant(self):
        current = native_config()
        merged = apply_settings_payload(
            current,
            {"tag_classifier": {"grounded_judge": {
                "backend": "search_first",
                "native_jev": {"enabled": False},
            }}},
            str,
        )
        grounded = merged["tag_classifier"]["grounded_judge"]
        self.assertEqual(grounded["backend"], "search_first")
        self.assertFalse(grounded["native_jev"]["enabled"])
        self.assertEqual(grounded["native_jev"]["exa_providers"], ["exa_1"])

    def test_weak_gates_and_credentials_in_the_env_file_path_are_rejected(self):
        current = native_config()
        for native in (
            {"min_confidence": 0.5},
            {"min_confidence": "high"},
            {"min_agreeing_sources": 1},
            {"min_agreeing_sources": True},
            {"env_file": "EXA_API_KEY=abc"},
        ):
            with self.subTest(native=native):
                with self.assertRaises(ValueError):
                    apply_settings_payload(
                        current,
                        {"tag_classifier": {"grounded_judge": {"native_jev": native}}},
                        str,
                    )

    def test_deleting_a_provider_still_used_by_native_pools_is_refused(self):
        current = native_config()
        payload = copy.deepcopy(build_settings_snapshot(redact_sensitive_config(current)))
        del payload["providers"]["exa_1"]
        with self.assertRaisesRegex(ValueError, "Exa Search"):
            apply_settings_payload(current, payload, str)

    def test_deleting_the_openrouter_connection_is_refused(self):
        current = native_config()
        payload = copy.deepcopy(build_settings_snapshot(redact_sensitive_config(current)))
        payload["providers"].pop("openrouter")
        payload["models"] = {}
        with self.assertRaisesRegex(ValueError, "OpenRouter"):
            apply_settings_payload(current, payload, str)


if __name__ == "__main__":
    unittest.main()

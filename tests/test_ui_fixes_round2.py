"""Second-round UI-adaptation fixes (R1-R6) for the classification surfaces.

Scope of this module:

* R1 -- a settings payload that submits ``tag_classifier: null`` (or a null
  ``grounded_judge`` / ``native_jev``, or any non-mapping) is *rejected*, not
  treated like an omission.  An omission still must not clear the stored Native
  subtree, and an explicit edit / the two-key "leave" action still work.
* R2 -- the tags page distinguishes "unknown outcome" from a definite deferral.
* R3 -- renaming a Provider rewrites the Native pools *and* the single
  ``jev_provider`` / ``deepseek_search_provider`` references.
* R4 -- the pool checkboxes are built with DOM APIs and ``addEventListener``;
  no Provider name is interpolated into an inline handler.
* R5 -- the display view reuses the runtime fallback/endpoint rules.
* R6 -- the plain-text (``parse_mode=None``) Telegram branch carries no
  Markdown escapes, while the Markdown branch keeps escaping.

The checks after R1 (finite confidence, integer source count) are covered here
too.  Everything is offline: no Provider, model, search engine, Telegram client
or production configuration is contacted.
"""

from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from classification_maintenance import (
    OUTCOME_LABELS,
    classification_backend_view,
    outcome_line,
)
from config import normalize_config
from tests.test_ui_backend_adaptation import (
    collect_from_rendered_page,
    native_config,
    rendered_settings_page,
    tags_context,
)
from tests.test_surface_backend_parity import SUMMARY, native_runtime_config
from tests.ui_render_probe import node_available, render_page
from web.settings_editor import (
    apply_settings_payload,
    build_settings_snapshot,
    redact_sensitive_config,
)

try:
    from notifier.telegram import TelegramNotifier
except ImportError:  # pragma: no cover - minimal env without python-telegram-bot
    TelegramNotifier = None


def _grounded(config: dict) -> dict:
    return config["tag_classifier"]["grounded_judge"]


# --------------------------------------------------------------------------
# R1: null is not "omitted"
# --------------------------------------------------------------------------

class NonMappingSectionTests(unittest.TestCase):
    """``null`` must never silently wipe the Native Jev subtree."""

    def assert_rejected(self, payload: dict) -> None:
        current = native_config()
        with self.assertRaises(ValueError) as ctx:
            apply_settings_payload(current, payload, str)
        self.assertIn("必须是对象", str(ctx.exception))

    def test_null_classifier_grounded_and_native_sections_are_rejected(self):
        for payload in (
            {"tag_classifier": None},
            {"tag_classifier": {"grounded_judge": None}},
            {"tag_classifier": {"grounded_judge": {"native_jev": None}}},
            {"tag_classifier": []},
            {"tag_classifier": "native_jev"},
            {"tag_classifier": {"grounded_judge": ["native_jev"]}},
            {"tag_classifier": {"grounded_judge": {"native_jev": False}}},
        ):
            with self.subTest(payload=payload):
                self.assert_rejected(payload)

    def test_rejection_does_not_mutate_the_submitted_payload(self):
        payload = {"tag_classifier": None, "filter": {"daily_limit": 33}}
        snapshot = copy.deepcopy(payload)
        with self.assertRaises(ValueError):
            apply_settings_payload(native_config(), payload, str)
        self.assertEqual(payload, snapshot)

    def test_omitted_sections_and_an_empty_payload_keep_the_native_subtree(self):
        current = native_config()
        for payload in ({}, {"tag_classifier": {}}, {"filter": {"daily_limit": 33}}):
            with self.subTest(payload=payload):
                merged = apply_settings_payload(copy.deepcopy(current), payload, str)
                grounded = _grounded(merged)
                self.assertEqual(grounded["backend"], "native_jev")
                self.assertIs(grounded["native_jev"]["enabled"], True)
                self.assertEqual(grounded["native_jev"]["exa_providers"], ["exa_1"])
                self.assertEqual(grounded["native_jev"]["state_path"], "data/native_jev_state.json")

    def test_explicit_edit_survives_and_an_undeclared_key_is_kept(self):
        merged = apply_settings_payload(
            native_config(),
            {"tag_classifier": {"grounded_judge": {"native_jev": {"model": "jev-1.14"}}}},
            str,
        )
        native = _grounded(merged)["native_jev"]
        self.assertEqual(native["model"], "jev-1.14")
        self.assertEqual(native["exa_providers"], ["exa_1"])
        self.assertEqual(native["max_evidence_chars"], 2500)

    def test_section_without_backend_keeps_the_backend_and_still_edits(self):
        merged = apply_settings_payload(
            native_config(),
            {"tag_classifier": {"grounded_judge": {"native_jev": {"enabled": False}}}},
            str,
        )
        self.assertEqual(_grounded(merged)["backend"], "native_jev")
        self.assertIs(_grounded(merged)["native_jev"]["enabled"], False)

    def test_two_key_action_can_still_leave_the_native_backend(self):
        merged = apply_settings_payload(
            native_config(),
            {"tag_classifier": {"grounded_judge": {
                "backend": "search_first",
                "native_jev": {"enabled": False},
            }}},
            str,
        )
        self.assertEqual(_grounded(merged)["backend"], "search_first")
        self.assertIs(_grounded(merged)["native_jev"]["enabled"], False)


class NativeThresholdValidationTests(unittest.TestCase):
    """The fixed gates accept only finite floats and real integers."""

    def save(self, **native_fields) -> dict:
        return apply_settings_payload(
            native_config(),
            {"tag_classifier": {"grounded_judge": {"native_jev": dict(native_fields)}}},
            str,
        )

    def assert_rejected(self, needle: str, **native_fields) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.save(**native_fields)
        self.assertIn(needle, str(ctx.exception))

    def test_confidence_must_be_finite(self):
        for value in ("nan", "NaN", float("nan"), float("inf"), "-inf", "1e999"):
            with self.subTest(value=value):
                self.assert_rejected("有限数字", min_confidence=value)

    def test_confidence_floor_and_ceiling_are_unchanged(self):
        self.assert_rejected("不能低于 0.9", min_confidence=0.5)
        self.assert_rejected("不能大于 1", min_confidence=1.5)
        self.assertAlmostEqual(self.save(min_confidence="0.95")["tag_classifier"]
                               ["grounded_judge"]["native_jev"]["min_confidence"], 0.95)
        self.assertAlmostEqual(self.save(min_confidence=0.9)["tag_classifier"]
                               ["grounded_judge"]["native_jev"]["min_confidence"], 0.9)
        self.assertAlmostEqual(self.save(min_confidence=1)["tag_classifier"]
                               ["grounded_judge"]["native_jev"]["min_confidence"], 1.0)

    def test_source_count_must_be_an_integer_at_least_two(self):
        for value in (2.9, True, False, "nan", float("nan"), float("inf"), "2.9", "abc"):
            with self.subTest(value=value):
                self.assert_rejected("不小于 2 的整数", min_agreeing_sources=value)
        # An integral 1 keeps the older, more specific refusal.
        self.assert_rejected("不能小于 2", min_agreeing_sources=1)
        self.assert_rejected("不能小于 2", min_agreeing_sources=0)

    def test_integral_source_counts_are_accepted(self):
        for value, expected in ((2, 2), ("3", 3), (4.0, 4)):
            with self.subTest(value=value):
                native = self.save(min_agreeing_sources=value)["tag_classifier"]["grounded_judge"]["native_jev"]
                self.assertEqual(native["min_agreeing_sources"], expected)
                self.assertIsInstance(native["min_agreeing_sources"], int)


# --------------------------------------------------------------------------
# R5: the display view reuses the runtime rules
# --------------------------------------------------------------------------

class BackendViewAlignmentTests(unittest.TestCase):
    def config_with_empty_native_pools(self) -> dict:
        config = native_config()
        grounded = _grounded(config)
        grounded["native_jev"]["exa_providers"] = []
        grounded["native_jev"]["tavily_providers"] = []
        grounded["exa_providers"] = ["exa_1"]
        grounded["tavily_providers"] = ["tavily_1"]
        return config

    def test_empty_native_pools_fall_back_to_the_shared_pools(self):
        config = self.config_with_empty_native_pools()
        grounded = _grounded(config)
        # The runtime reads exactly this expression (native_jev_judge:2400):
        runtime_exa = list(
            grounded["native_jev"].get("exa_providers")
            or grounded.get("exa_providers")
            or []
        )
        view = classification_backend_view(normalize_config(config))
        self.assertEqual(view["search_order_text"].split(" → ")[0], "Exa（exa_1）")
        self.assertIn("Tavily（tavily_1）", view["search_order_text"])
        pools = {(pool["engine"], pool["name"]) for pool in view["pools"]}
        self.assertIn(("exa", "exa_1"), pools)
        self.assertIn(("tavily", "tavily_1"), pools)
        self.assertEqual(view["jev"]["model"], "jev-1.13")
        self.assertEqual(runtime_exa, ["exa_1"])

    def test_endpoint_follows_the_provider_base_url_rule(self):
        # native_jev_judge: explicit jev_endpoint, else _join_endpoint(base_url, "/systemone")
        cases = [
            ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/systemone"),
            ("https://openrouter.ai/api/v1/", "https://openrouter.ai/api/v1/systemone"),
            ("https://gateway.example/api", "https://gateway.example/api/v1/systemone"),
            ("", "https://openrouter.ai/api/v1/systemone"),
        ]
        for base_url, expected in cases:
            with self.subTest(base_url=base_url):
                config = native_config()
                config["providers"]["openrouter"]["base_url"] = base_url
                _grounded(config)["native_jev"]["jev_endpoint"] = ""
                view = classification_backend_view(config)
                self.assertEqual(view["jev"]["endpoint"], expected)
                facts = json.dumps(view["facts"], ensure_ascii=False)
                self.assertIn(f"端点 {expected}", facts)

    def test_explicit_endpoint_wins_over_the_provider_base_url(self):
        config = native_config()
        _grounded(config)["native_jev"]["jev_endpoint"] = "https://systemone.example/explicit"
        view = classification_backend_view(config)
        self.assertEqual(view["jev"]["endpoint"], "https://systemone.example/explicit")

    def test_env_file_falls_back_to_the_grounded_section(self):
        config = native_config()
        _grounded(config)["native_jev"]["env_file"] = ""
        _grounded(config)["env_file"] = "/secure/shared.env"
        view = classification_backend_view(config)
        self.assertEqual(view["jev"]["env_file"], "/secure/shared.env")

    def test_display_helper_never_prints_a_secret_from_the_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "jev.env"
            env.write_text("EXA_API_KEY=REVIEW_ONLY_NOT_A_REAL_SECRET\n")
            config = native_config()
            config["providers"]["exa_1"]["api_key"] = ""
            _grounded(config)["native_jev"]["env_file"] = str(env)
            with patch.dict("os.environ", {}, clear=True):
                view = classification_backend_view(config)
            serialized = json.dumps(view, ensure_ascii=False)
            self.assertIn("env_file", serialized)
            self.assertNotIn("REVIEW_ONLY_NOT_A_REAL_SECRET", serialized)
            for secret in ("exa-secret-value", "sk-openrouter-secret", "deepseek-secret"):
                self.assertNotIn(secret, serialized)


# --------------------------------------------------------------------------
# R6: plain-text messages carry no escapes; Markdown branch still escapes
# --------------------------------------------------------------------------

@unittest.skipIf(TelegramNotifier is None, "python-telegram-bot is not installed")
class TelegramPlainTextTests(unittest.IsolatedAsyncioTestCase):
    async def _high_weight(self, query=None):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier._tag_review_batch_running = False
        notifier._high_weight_tag_review_snapshots = {}
        query = query or SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())
        query.message = SimpleNamespace(chat_id=99)
        candidates = [{"tag": "a", "profile_weight": 3.0}]
        # ``preview()`` reads the queue through the read-only accessor (a
        # preview must never create/migrate the database), so the queue double
        # has to stand in for that call as well.
        with patch("database.get_high_weight_unclassified_profile_tags",
                   new=AsyncMock(return_value=candidates)), \
             patch("database.get_high_weight_unclassified_profile_tags_readonly",
                   new=Mock(return_value=candidates)), \
             patch("database.set_state", new=AsyncMock()), \
             patch("notifier.telegram.load_config", return_value=native_runtime_config()), \
             patch("classification_maintenance.run_scheduled_maintenance",
                   new=AsyncMock(return_value=SUMMARY)):
            await notifier._handle_menu_callback(query, "menu:tag_review:high_weight")
            await notifier._handle_menu_callback(query, "menu:tag_review:high_weight:confirm")
        return query

    async def test_high_weight_messages_are_plain_text_without_markdown_escapes(self):
        query = await self._high_weight()
        calls = query.edit_message_text.await_args_list
        self.assertEqual(len(calls), 3)
        for call in calls:
            with self.subTest(text=call.args[0][:40]):
                self.assertIsNone(call.kwargs.get("parse_mode"))
                self.assertNotIn("\\", call.args[0])
        listing = calls[0].args[0]
        self.assertIn("native_jev", listing)  # raw identifiers, not native\_jev
        self.assertIn("exa_1", listing)
        self.assertIn("第三兜底", listing)

    async def test_high_weight_result_keeps_every_outcome_counter(self):
        query = await self._high_weight()
        result = query.edit_message_text.await_args_list[-1].args[0]
        self.assertEqual(result, "\n".join([
            "🤖 高权重候选分类完成",
            "",
            "后端：原生 Jev 后端（native_jev）",
            outcome_line(SUMMARY, include_zero=True),
        ]))
        for label in OUTCOME_LABELS.values():
            self.assertIn(label, result)
        self.assertIn("保留既有分类 1", result)
        self.assertIn("人工决定优先 1", result)

    async def test_markdown_branch_still_escapes_and_sets_parse_mode(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier._tag_review_batch_running = False
        query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())
        with patch("notifier.telegram.load_config", return_value=native_runtime_config()):
            await notifier._handle_menu_callback(query, "menu:tag_review:run:pick_5")
        call = query.edit_message_text.await_args_list[-1]
        self.assertEqual(call.kwargs.get("parse_mode"), "Markdown")
        self.assertIn("native\\_jev", call.args[0])
        self.assertIn("exa\\_1", call.args[0])


# --------------------------------------------------------------------------
# R3/R4: real page script (Node DOM shim) and template contracts
# --------------------------------------------------------------------------

@unittest.skipUnless(node_available(), "node is required for the DOM collect probe")
class ProviderRenameProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = collect_from_rendered_page([{
            "name": "rename_native",
            "renameProvider": [
                {"from": "exa_1", "to": "exa renamed"},
                {"from": "tavily_1", "to": "tavily-2"},
                {"from": "openrouter", "to": "router2"},
                {"from": "deepseek", "to": "ds2"},
            ],
        }])
        cls.result = cls.probe["results"][0]

    def test_rename_rewrites_pools_single_references_and_models(self):
        self.assertIsNone(self.result.get("error"), self.result.get("error"))
        payload = self.result["payload"]
        grounded = payload["tag_classifier"]["grounded_judge"]
        native = grounded["native_jev"]
        self.assertEqual(native["exa_providers"], ["exa renamed"])
        self.assertEqual(native["tavily_providers"], ["tavily-2"])
        self.assertEqual(native["jev_provider"], "router2")
        self.assertEqual(native["deepseek_search_provider"], "ds2")
        self.assertEqual(payload["models"]["deepseek_v4"]["provider"], "router2")

    def test_saved_configuration_has_no_dangling_reference(self):
        merged = apply_settings_payload(native_config(), self.result["payload"], str)
        grounded = _grounded(merged)
        native = grounded["native_jev"]
        names = list(merged["providers"])
        self.assertNotIn("exa_1", names)
        for reference in (native["exa_providers"] + native["tavily_providers"]
                          + [native["jev_provider"], native["deepseek_search_provider"]]):
            self.assertIn(reference, names)
        self.assertEqual(grounded["backend"], "native_jev")

    def test_node_shim_does_not_claim_a_real_dom(self):
        # The shim has no event system: R4's click behaviour is verified in the
        # browser (see jev-ui-fixes/browser/r4-apostrophe.json), not here.
        page = rendered_settings_page()
        self.assertNotIn("onchange='togglePoolProvider", page)
        self.assertIn("input.addEventListener('change'", page)


@unittest.skipUnless(node_available(), "node is required for the DOM collect probe")
class PoolRenderingContractTests(unittest.TestCase):
    def test_pool_checkboxes_use_dom_creation_and_escaped_text(self):
        page = rendered_settings_page()
        self.assertIn("document.createElement('label')", page)
        self.assertIn("text.textContent = name", page)
        # No Provider name may be interpolated into an attribute or JS literal.
        self.assertNotIn("togglePoolProvider(${JSON.stringify", page)
        self.assertNotIn("onchange='togglePoolProvider", page)

    def test_apostrophe_provider_name_is_rendered_as_text(self):
        probe = collect_from_rendered_page([{"name": "baseline"}])
        html = probe["results"][0]["poolHtml"]["native_jev_exa_selection"]
        self.assertIn("exa_1", html)
        self.assertIn("exa_2", html)


class TagsErrorSemanticsTests(unittest.TestCase):
    """R2 wording contract; the click/fetch behaviour is browser-verified."""

    @classmethod
    def setUpClass(cls):
        cls.page = render_page("tags.html", tags_context())

    def test_unknown_outcome_is_not_reported_as_unchanged(self):
        self.assertIn("分类结果未确认", self.page)
        self.assertIn("请刷新核对标签状态", self.page)
        self.assertIn("服务器响应无法解析", self.page)

    def test_only_a_definite_deferral_says_the_classification_was_not_changed(self):
        self.assertIn("本次未改分类", self.page)
        self.assertIn("alreadySaysKept", self.page)  # no duplicated "保持原状态"
        self.assertIn("=== 503", self.page)  # explicit status branches are kept
        self.assertIn("status === 'deferred'", self.page)
        self.assertIn("status === 'preserved'", self.page)

    def test_json_failure_is_guarded_before_any_status_is_claimed(self):
        self.assertIn("parseError", self.page)
        self.assertNotIn("catch (error) {\n        setReviewItemPending(tag, false);\n"
                         "        feedback.textContent = `分类暂缓或未完成", self.page)


class SettingsSurfaceContractTests(unittest.TestCase):
    """The two surfaces must describe the same fallback facts."""

    def test_rules_expose_the_native_single_provider_references(self):
        from provider_model_graph import settings_rules

        rules = settings_rules()
        paths = [item["path"] for item in rules["native_single_provider_references"]]
        self.assertIn(["tag_classifier", "grounded_judge", "native_jev", "jev_provider"], paths)
        self.assertIn(
            ["tag_classifier", "grounded_judge", "native_jev", "deepseek_search_provider"],
            paths,
        )
        page = rendered_settings_page()
        self.assertIn("native_single_provider_references", page)
        self.assertIn("native_search_provider_references", page)

    def test_settings_snapshot_keeps_an_unset_backend_visible(self):
        snapshot = build_settings_snapshot(redact_sensitive_config(native_config()))
        grounded = snapshot["tag_classifier"]["grounded_judge"]
        self.assertEqual(grounded["backend"], "native_jev")
        self.assertEqual(grounded["native_jev"]["min_agreeing_sources"], 2)


if __name__ == "__main__":
    unittest.main()

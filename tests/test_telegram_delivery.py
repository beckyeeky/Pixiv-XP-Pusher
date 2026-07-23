import asyncio
import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, call, patch

try:
    from notifier.telegram import TelegramNotifier
except ImportError:  # pragma: no cover - dependency may be absent in minimal envs
    TelegramNotifier = None


@unittest.skipIf(TelegramNotifier is None, "python-telegram-bot is not installed")
class TelegramDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_with_result_waits_for_worker_delivery_result(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.send_queue = asyncio.Queue()
        notifier.batch_mode = "single"

        async def fake_send_direct(self, illusts, custom_title=None, batch_mode=None):
            return [illusts[0].id]

        notifier._send_direct = MethodType(fake_send_direct, notifier)
        worker = asyncio.create_task(notifier._process_queue())
        try:
            result = await asyncio.wait_for(
                notifier.send_with_result([SimpleNamespace(id=1), SimpleNamespace(id=2)]),
                timeout=1,
            )
        finally:
            worker.cancel()
            await worker

        self.assertEqual(result.delivered_ids, [1])
        self.assertEqual(result.failed_ids, [2])
        self.assertEqual(result.queued_ids, [])

    async def test_tag_review_menu_runs_configured_high_impact_batch_and_reports_remaining_count(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier._tag_review_batch_running = False
        query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())
        summary = {
            "attempted": 2, "accepted": 1, "unresolved": 1, "failed": 0,
            "human_override": 0, "usage": {"total": 23, "search_queries": 2},
        }

        runtime_config = {"tag_classifier": {"maintenance": {
            "concurrency": 3, "max_tags_per_run": 20, "min_profile_weight": 1.25,
        }}}
        with patch("database.get_tag_review_count", new=AsyncMock(return_value=1)), \
             patch("database.get_high_weight_unclassified_profile_tags", new=AsyncMock(return_value=[{"tag": "one"}, {"tag": "two"}])) as queue, \
             patch("notifier.telegram.load_config", return_value=runtime_config), \
             patch("notifier.telegram.run_scheduled_maintenance", new=AsyncMock(return_value=summary)) as run:
            await notifier._handle_menu_callback(query, "menu:tag_review:run")

        queue.assert_awaited_once_with(limit=20, min_profile_weight=1.25)
        run.assert_awaited_once_with(
            ["one", "two"], runtime_config, concurrency=3,
        )
        self.assertFalse(notifier._tag_review_batch_running)
        self.assertIn("当前待人工决定：*1*", query.edit_message_text.await_args_list[-1].args[0])

    async def test_tag_review_menu_displays_exact_pending_count(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())

        with patch("database.get_tag_review_count", new=AsyncMock(return_value=7)), \
             patch("database.get_high_weight_unclassified_profile_tags", new=AsyncMock(return_value=[])):
            await notifier._handle_menu_callback(query, "menu:tag_review")

        self.assertIn("当前待人工决定标签：*7* 个", query.edit_message_text.await_args.args[0])

    async def test_tag_review_menu_opens_bounded_semantic_mapping_review_without_writing(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=123),
            edit_message_text=AsyncMock(),
            answer=AsyncMock(),
        )
        candidate = {
            "id": 7,
            "original_tag": "白髪",
            "proposed_normalized_tag": "white_hair",
            "kind": "equivalent",
            "source": "legacy_ai_tag_cache",
            "original_classification": "feature",
            "target_classification": "feature",
            "original_translation": "白发",
            "target_translation": "白发",
            "ai_relation": "equivalent",
            "ai_confidence": 0.98,
            "ai_rationale": "同一视觉特征",
            "ai_canonical_tag": "white_hair",
            "ai_risk_flags": "[]",
            "ai_is_current": True,
            "duplicate_count": 2,
        }

        with patch(
            "database.count_tag_mapping_candidate_groups",
            new=AsyncMock(return_value=3),
        ) as count_groups, patch(
            "database.get_tag_mapping_candidates",
            new=AsyncMock(return_value=[candidate]),
        ) as get_candidates, patch(
            "database.review_tag_mapping_candidate",
            new=AsyncMock(),
        ) as review:
            await notifier._handle_menu_callback(query, "menu:tag_review:mapping")

        count_groups.assert_awaited_once_with()
        get_candidates.assert_awaited_once_with(limit=1, offset=0)
        review.assert_not_awaited()
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("语义映射审核", text)
        self.assertIn("白髪", text)
        self.assertIn("white\\_hair", text)
        self.assertIn("AI Relationship Recommendation", text)
        self.assertIn("Equivalent", text)
        self.assertIn("98%", text)
        self.assertIn("建议 canonical：white\\_hair", text)
        self.assertIn("1 / 3", text)
        labels = [
            button.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("✅ Tag Alias", labels)
        self.assertIn("🔎 Search Alias", labels)
        self.assertIn("❌ 拒绝", labels)
        self.assertIn("⏭ 跳过", labels)

    async def test_semantic_mapping_decision_requires_preview_before_any_write(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=123),
            edit_message_text=AsyncMock(),
            answer=AsyncMock(),
        )
        candidate = {
            "id": 7,
            "original_tag": "白髪",
            "proposed_normalized_tag": "white_hair",
            "kind": "equivalent",
            "source": "manual_candidate",
            "ai_relation": "equivalent",
            "ai_confidence": 0.98,
            "ai_is_current": True,
        }

        with patch(
            "database.count_tag_mapping_candidate_groups",
            new=AsyncMock(return_value=1),
        ), patch(
            "database.get_tag_mapping_candidates",
            new=AsyncMock(return_value=[candidate]),
        ), patch(
            "database.review_tag_mapping_candidate",
            new=AsyncMock(),
        ) as review:
            await notifier._handle_menu_callback(query, "menu:tag_review:mapping")
            await notifier._handle_menu_callback(
                query, "menu:tag_review:mapping:preview_e:7"
            )

        review.assert_not_awaited()
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("确认建立 Tag Alias", text)
        self.assertIn("白髪", text)
        self.assertIn("white\\_hair", text)
        labels = [
            button.text
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("✅ 确认", labels)
        self.assertIn("取消", labels)

    async def test_confirmed_semantic_mapping_decision_uses_authoritative_review(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=123),
            edit_message_text=AsyncMock(),
            answer=AsyncMock(),
        )
        candidate = {
            "id": 7,
            "original_tag": "白髪",
            "proposed_normalized_tag": "white_hair",
            "kind": "equivalent",
            "source": "manual_candidate",
            "updated_at": "2026-07-21 08:00:00",
            "ai_recommendation_id": 9,
            "ai_evidence_hash": "current-hash",
            "ai_is_current": True,
        }
        result = {
            "id": 7,
            "status": "accepted",
            "original_tag": "白髪",
            "normalized_tag": "white_hair",
            "kind": "equivalent",
            "duplicate_candidates_resolved": 1,
        }
        next_candidate = {
            "id": 8,
            "original_tag": "眼鏡",
            "proposed_normalized_tag": "glasses",
            "kind": "equivalent",
        }

        with patch(
            "database.count_tag_mapping_candidate_groups",
            new=AsyncMock(side_effect=[2, 1]),
        ), patch(
            "database.get_tag_mapping_candidates",
            new=AsyncMock(side_effect=[[candidate], [next_candidate]]),
        ), patch(
            "database.get_tag_mapping_candidate_group",
            new=AsyncMock(return_value=candidate),
            create=True,
        ) as get_current, patch(
            "database.review_tag_mapping_candidate",
            new=AsyncMock(return_value=result),
        ) as review:
            await notifier._handle_menu_callback(query, "menu:tag_review:mapping")
            await notifier._handle_menu_callback(
                query, "menu:tag_review:mapping:preview_e:7"
            )
            await notifier._handle_menu_callback(
                query, "menu:tag_review:mapping:confirm_e:7"
            )

        get_current.assert_awaited_once_with(7)
        review.assert_awaited_once_with(
            7,
            "accept",
            kind="equivalent",
            expected_candidate_ids=(7,),
        )
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("已接受为 Tag Alias", text)
        self.assertIn("同时关闭 1 条重复候选", text)
        self.assertIn("眼鏡", text)
        self.assertIn("1 / 1", text)

    async def test_repeated_semantic_mapping_confirmation_returns_resolved_status(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=123),
            edit_message_text=AsyncMock(),
            answer=AsyncMock(),
        )
        candidate = {
            "id": 7,
            "original_tag": "白髪",
            "proposed_normalized_tag": "white_hair",
            "kind": "equivalent",
            "updated_at": "2026-07-21 08:00:00",
            "ai_is_current": False,
        }
        next_candidate = {
            "id": 8,
            "original_tag": "眼鏡",
            "proposed_normalized_tag": "glasses",
            "kind": "equivalent",
        }

        with patch(
            "database.count_tag_mapping_candidate_groups",
            new=AsyncMock(side_effect=[2, 1]),
        ), patch(
            "database.get_tag_mapping_candidates",
            new=AsyncMock(side_effect=[[candidate], [next_candidate]]),
        ), patch(
            "database.get_tag_mapping_candidate_group",
            new=AsyncMock(return_value=candidate),
        ), patch(
            "database.get_tag_mapping_candidate_status",
            new=AsyncMock(return_value={"status": "accepted", "kind": "equivalent"}),
            create=True,
        ) as get_status, patch(
            "database.review_tag_mapping_candidate",
            new=AsyncMock(return_value={"duplicate_candidates_resolved": 0}),
        ) as review:
            await notifier._handle_menu_callback(query, "menu:tag_review:mapping")
            await notifier._handle_menu_callback(
                query, "menu:tag_review:mapping:preview_e:7"
            )
            await notifier._handle_menu_callback(
                query, "menu:tag_review:mapping:confirm_e:7"
            )
            await notifier._handle_menu_callback(
                query, "menu:tag_review:mapping:confirm_e:7"
            )

        review.assert_awaited_once()
        get_status.assert_awaited_once_with(7)
        self.assertIn("已经接受", query.edit_message_text.await_args.args[0])

    async def test_semantic_mapping_database_failure_returns_clear_unknown_state(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=123),
            edit_message_text=AsyncMock(),
            answer=AsyncMock(),
        )
        candidate = {
            "id": 7,
            "original_tag": "白髪",
            "proposed_normalized_tag": "white_hair",
            "kind": "equivalent",
            "updated_at": "2026-07-21 08:00:00",
            "ai_recommendation_id": 9,
            "ai_evidence_hash": "current-hash",
            "ai_is_current": True,
        }

        with patch(
            "database.count_tag_mapping_candidate_groups",
            new=AsyncMock(return_value=1),
        ), patch(
            "database.get_tag_mapping_candidates",
            new=AsyncMock(return_value=[candidate]),
        ), patch(
            "database.get_tag_mapping_candidate_group",
            new=AsyncMock(return_value=candidate),
            create=True,
        ), patch(
            "database.review_tag_mapping_candidate",
            new=AsyncMock(side_effect=RuntimeError("database locked")),
        ):
            await notifier._handle_menu_callback(query, "menu:tag_review:mapping")
            await notifier._handle_menu_callback(
                query, "menu:tag_review:mapping:preview_e:7"
            )
            await notifier._handle_menu_callback(
                query, "menu:tag_review:mapping:confirm_e:7"
            )

        text = query.edit_message_text.await_args.args[0]
        self.assertIn("审核状态未知", text)
        self.assertIn("Web", text)

    async def test_changed_semantic_mapping_candidate_is_not_written(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=123),
            edit_message_text=AsyncMock(),
            answer=AsyncMock(),
        )
        candidate = {
            "id": 7,
            "original_tag": "白髪",
            "proposed_normalized_tag": "white_hair",
            "kind": "equivalent",
            "updated_at": "2026-07-21 08:00:00",
            "ai_recommendation_id": 9,
            "ai_evidence_hash": "old-hash",
            "ai_is_current": True,
            "duplicate_candidate_ids": [7, 8],
            "duplicate_sources": ["first", "reverse"],
            "duplicate_kinds": ["equivalent"],
        }
        changed = {
            **candidate,
            "duplicate_candidate_ids": [7, 8, 9],
            "duplicate_sources": ["first", "new", "reverse"],
        }

        with patch(
            "database.count_tag_mapping_candidate_groups",
            new=AsyncMock(return_value=1),
        ), patch(
            "database.get_tag_mapping_candidates",
            new=AsyncMock(return_value=[candidate]),
        ), patch(
            "database.get_tag_mapping_candidate_group",
            new=AsyncMock(return_value=changed),
            create=True,
        ), patch(
            "database.review_tag_mapping_candidate",
            new=AsyncMock(),
        ) as review:
            await notifier._handle_menu_callback(query, "menu:tag_review:mapping")
            await notifier._handle_menu_callback(
                query, "menu:tag_review:mapping:preview_e:7"
            )
            await notifier._handle_menu_callback(
                query, "menu:tag_review:mapping:confirm_e:7"
            )

        review.assert_not_awaited()
        self.assertIn("已变化", query.edit_message_text.await_args.args[0])

    async def test_semantic_mapping_search_alias_and_reject_are_explicit_decisions(self):
        candidate = {
            "id": 7,
            "original_tag": "白髪",
            "proposed_normalized_tag": "white_hair",
            "kind": "equivalent",
            "updated_at": "2026-07-21 08:00:00",
            "ai_is_current": False,
        }
        cases = [
            ("s", "accept", "search"),
            ("r", "reject", "equivalent"),
        ]

        for action_code, decision, kind in cases:
            with self.subTest(action_code=action_code):
                notifier = TelegramNotifier.__new__(TelegramNotifier)
                query = SimpleNamespace(
                    message=SimpleNamespace(chat_id=123),
                    edit_message_text=AsyncMock(),
                    answer=AsyncMock(),
                )
                with patch(
                    "database.count_tag_mapping_candidate_groups",
                    new=AsyncMock(return_value=1),
                ), patch(
                    "database.get_tag_mapping_candidates",
                    new=AsyncMock(return_value=[candidate]),
                ), patch(
                    "database.get_tag_mapping_candidate_group",
                    new=AsyncMock(return_value=candidate),
                ), patch(
                    "database.review_tag_mapping_candidate",
                    new=AsyncMock(return_value={"duplicate_candidates_resolved": 0}),
                ) as review:
                    await notifier._handle_menu_callback(query, "menu:tag_review:mapping")
                    await notifier._handle_menu_callback(
                        query, f"menu:tag_review:mapping:preview_{action_code}:7"
                    )
                    await notifier._handle_menu_callback(
                        query, f"menu:tag_review:mapping:confirm_{action_code}:7"
                    )

                review.assert_awaited_once_with(
                    7,
                    decision,
                    kind=kind,
                    expected_candidate_ids=(7,),
                )

    async def test_semantic_mapping_skip_loads_only_the_next_review_group(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=123),
            edit_message_text=AsyncMock(),
            answer=AsyncMock(),
        )
        first = {
            "id": 7,
            "original_tag": "first",
            "proposed_normalized_tag": "target_one",
        }
        second = {
            "id": 8,
            "original_tag": "second",
            "proposed_normalized_tag": "target_two",
        }

        with patch(
            "database.count_tag_mapping_candidate_groups",
            new=AsyncMock(return_value=2),
        ), patch(
            "database.get_tag_mapping_candidates",
            new=AsyncMock(side_effect=[[first], [second]]),
        ) as get_candidates:
            await notifier._handle_menu_callback(query, "menu:tag_review:mapping")
            await notifier._handle_menu_callback(
                query, "menu:tag_review:mapping:next:1"
            )

        self.assertEqual(
            get_candidates.await_args_list,
            [call(limit=1, offset=0), call(limit=1, offset=1)],
        )
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("second", text)
        self.assertIn("2 / 2", text)

    async def test_tag_review_menu_lists_common_tags_alongside_gemini_actions(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())
        sections = {
            "feature": [("white_hair", 4.0), ("glasses", 2.0)],
            "identity": [("blue_archive", 3.0)],
        }

        with patch(
            "database.get_xp_profile_display_sections",
            new=AsyncMock(return_value=sections),
        ) as get_sections:
            await notifier._handle_menu_callback(query, "menu:tag_review:common")

        get_sections.assert_awaited_once()
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("常用 Tag", text)
        self.assertIn("white\\_hair", text)
        self.assertIn("blue\\_archive", text)
        labels = [button.text for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("🤖 判定高影响批次", labels)
        self.assertIn("⭐ 查看常用 Tag", labels)

    async def test_tag_review_menu_requires_candidate_preview_before_high_weight_classification(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier._tag_review_batch_running = False
        notifier._high_weight_tag_review_snapshots = {}
        query = SimpleNamespace(
            message=SimpleNamespace(chat_id=123),
            edit_message_text=AsyncMock(),
            answer=AsyncMock(),
        )
        candidates = [
            {"tag": "high_weight", "profile_weight": 4.0, "classification": None},
            {"tag": "unresolved", "profile_weight": 3.0, "classification": "unresolved"},
        ]
        summary = {"accepted": 1, "unresolved": 1, "failed": 0}

        with patch("database.get_high_weight_unclassified_profile_tags", new=AsyncMock(side_effect=[candidates, candidates])) as select, \
             patch("notifier.telegram.load_config", return_value={"tag_classifier": {"maintenance": {"concurrency": 3, "max_tags_per_run": 25, "min_profile_weight": 1.5}}}), \
             patch("notifier.telegram.run_scheduled_maintenance", new=AsyncMock(return_value=summary)) as run:
            await notifier._handle_menu_callback(query, "menu:tag_review:high_weight")
            await notifier._handle_menu_callback(query, "menu:tag_review:high_weight:confirm")

        self.assertIn("高权重未分类候选", query.edit_message_text.await_args_list[0].args[0])
        select.assert_has_awaits([
            call(limit=25, min_profile_weight=1.5),
            call(limit=25, min_profile_weight=1.5),
        ])
        run.assert_awaited_once_with(
            ["high_weight", "unresolved"], {"tag_classifier": {"maintenance": {"concurrency": 3, "max_tags_per_run": 25, "min_profile_weight": 1.5}}}, concurrency=3,
        )


if __name__ == "__main__":
    unittest.main()

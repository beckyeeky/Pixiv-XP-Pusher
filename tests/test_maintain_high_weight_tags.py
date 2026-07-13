import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, AsyncMock, patch


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "maintain_high_weight_tags.py"
SPEC = importlib.util.spec_from_file_location("maintain_high_weight_tags", SCRIPT_PATH)
maintain_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintain_script)


class HighWeightTagMaintenanceScriptTests(unittest.TestCase):
    def test_inspection_does_not_run_grounded_judge(self):
        candidates = [{"tag": "high_weight", "profile_weight": 4.0, "classification": None}]
        with patch.object(maintain_script, "select_candidates", new=AsyncMock(return_value=candidates)), \
             patch.object(maintain_script, "apply_candidates", new=AsyncMock()) as apply:
            result = asyncio.run(maintain_script.run(maintain_script.parse_args([])))

        self.assertEqual(result["candidates"], candidates)
        apply.assert_not_awaited()

    def test_apply_requires_reviewed_inspection_file(self):
        with patch.object(maintain_script, "select_candidates", new=AsyncMock(return_value=[])):
            with self.assertRaisesRegex(ValueError, "--reviewed-tags"):
                asyncio.run(maintain_script.run(maintain_script.parse_args(["--apply"])))

    def test_apply_uses_only_tags_in_reviewed_file(self):
        candidates = [
            {"tag": "first", "profile_weight": 4.0, "classification": None},
            {"tag": "second", "profile_weight": 3.0, "classification": "unresolved"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            reviewed = Path(tmpdir) / "reviewed.json"
            reviewed.write_text(
                '{"selection": {"limit": 100, "min_weight": 1.0}, "candidates": [{"tag": "second"}]}',
                encoding="utf-8",
            )
            args = maintain_script.parse_args(["--apply", "--reviewed-tags", str(reviewed)])
            with patch.object(maintain_script, "select_candidates", new=AsyncMock(return_value=candidates)) as select, \
                 patch.object(maintain_script, "apply_candidates", new=AsyncMock(return_value={"accepted": 1})) as apply:
                result = asyncio.run(maintain_script.run(args))

        self.assertEqual(result["maintenance"], {"accepted": 1})
        select.assert_awaited_once_with(100, 1.0)
        apply.assert_awaited_once_with([candidates[1]], ANY)

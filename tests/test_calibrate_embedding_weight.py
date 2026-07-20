import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "calibrate_embedding_weight.py"
SPEC = importlib.util.spec_from_file_location("calibrate_embedding_weight", SCRIPT_PATH)
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


class CalibrateEmbeddingWeightScriptTests(unittest.TestCase):
    def test_insufficient_report_has_actionable_exit_and_json(self):
        report = {
            "sufficient": False,
            "reasons": ["dislike 样本 0 少于最低要求 5"],
            "current_weight": 0.3,
            "recommended_weight": None,
            "sample_counts": {"feedback": 2, "eligible": 1, "like": 1, "dislike": 0, "missing": 1},
            "coverage": 0.5,
            "missing_reasons": {"missing_work_embedding": 1},
            "evaluations": [],
            "dataset": {"read_only": True},
        }
        output = io.StringIO()
        with patch.object(script, "_payload", return_value=report), patch("sys.stdout", output):
            exit_code = script.main(["--json"])

        self.assertEqual(exit_code, 2)
        self.assertIn('"sufficient": false', output.getvalue())
        self.assertIn('"read_only": true', output.getvalue())

    def test_parser_accepts_explicit_read_only_inputs(self):
        args = script.build_parser().parse_args([
            "--config", "/tmp/config.yaml", "--database", "/tmp/data.db",
            "--weights", "0,0.25,0.5", "--user-id", "7",
        ])

        self.assertEqual(args.weights, [0.0, 0.25, 0.5])
        self.assertEqual(args.user_id, 7)

    def test_default_database_matches_the_runtime_database_name(self):
        args = script.build_parser().parse_args([])

        self.assertEqual(args.database.name, "pixiv_xp.db")


if __name__ == "__main__":
    unittest.main()

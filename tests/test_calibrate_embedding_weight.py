import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
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

    def test_human_output_shows_follow_separately_and_feedback_time_range(self):
        report = {
            "sufficient": False,
            "reasons": ["可用样本 0 少于最低要求 20"],
            "current_weight": 0.3,
            "recommended_weight": None,
            "sample_counts": {"feedback": 3, "eligible": 0, "like": 0, "dislike": 0, "missing": 3},
            "coverage": 0.0,
            "missing_reasons": {"stale_user_embedding": 3},
            "evaluations": [],
            "dataset": {"embedding_model": "embed-v1", "user_id": 7, "read_only": True},
            "feedback": {
                "like": 2,
                "dislike": 1,
                "follow": 4,
                "first_at": "2026-07-01 10:00:00",
                "latest_at": "2026-07-20 10:00:00",
            },
        }
        output = io.StringIO()
        with patch("sys.stdout", output):
            script.print_human(report)

        text = output.getvalue()
        self.assertIn("like=2", text)
        self.assertIn("dislike=1", text)
        self.assertIn("follow=4", text)
        self.assertIn("不参与校准", text)
        self.assertIn("2026-07-01 10:00:00", text)
        self.assertIn("2026-07-20 10:00:00", text)

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

    def test_real_read_only_command_reports_balanced_feedback_without_mutation(self):
        from embedder import profile_embedding_hash

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            database_path = root / "pixiv_xp.db"
            config_path.write_text(
                "ai:\n  embedding:\n    model: embed-v1\n    semantic_weight: 0.3\n",
                encoding="utf-8",
            )
            profile = {"white_hair": 2.0}
            with sqlite3.connect(database_path) as connection:
                connection.executescript("""
                    CREATE TABLE xp_profile(tag TEXT PRIMARY KEY, weight REAL);
                    CREATE TABLE tag_aliases(
                        original_tag TEXT PRIMARY KEY, normalized_tag TEXT, kind TEXT
                    );
                    CREATE TABLE tag_classification_cache(
                        normalized_tag TEXT PRIMARY KEY, classification TEXT,
                        source TEXT, updated_at TEXT
                    );
                    CREATE TABLE feedback(
                        illust_id INTEGER PRIMARY KEY, action TEXT, created_at TEXT
                    );
                    CREATE TABLE illust_cache(
                        illust_id INTEGER PRIMARY KEY, tags TEXT, source TEXT
                    );
                    CREATE TABLE illust_embeddings(
                        illust_id INTEGER PRIMARY KEY, embedding TEXT,
                        model TEXT, created_at TEXT
                    );
                    CREATE TABLE user_embedding(
                        user_id INTEGER PRIMARY KEY, embedding TEXT, model TEXT,
                        profile_hash TEXT, updated_at TEXT
                    );
                """)
                connection.execute("INSERT INTO xp_profile VALUES ('white_hair', 2.0)")
                connection.execute(
                    "INSERT INTO tag_classification_cache VALUES "
                    "('white_hair', 'feature', 'manual', CURRENT_TIMESTAMP)"
                )
                connection.executemany(
                    "INSERT INTO feedback VALUES (?, ?, ?)",
                    [
                        (1, "like", "2026-07-01 10:00:00"),
                        (2, "dislike", "2026-07-02 10:00:00"),
                        (3, "follow", "2026-07-03 10:00:00"),
                    ],
                )
                connection.executemany(
                    "INSERT INTO illust_cache VALUES (?, ?, 'xp_search')",
                    [(1, '[\"white_hair\"]'), (2, '[\"white_hair\"]')],
                )
                connection.executemany(
                    "INSERT INTO illust_embeddings VALUES (?, ?, 'embed-v1', CURRENT_TIMESTAMP)",
                    [(1, '[1.0, 0.0]'), (2, '[0.0, 1.0]')],
                )
                connection.execute(
                    "INSERT INTO user_embedding VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (7, '[1.0, 0.0]', 'embed-v1', profile_embedding_hash(profile)),
                )

            before_config = config_path.read_bytes()
            before_database = database_path.read_bytes()
            output = io.StringIO()
            with patch("sys.stdout", output):
                exit_code = script.main([
                    "--config", str(config_path),
                    "--database", str(database_path),
                    "--user-id", "7",
                    "--min-samples", "2",
                    "--min-per-class", "1",
                    "--json",
                ])

            report = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(report["sufficient"])
            self.assertEqual(report["feedback"]["like"], 1)
            self.assertEqual(report["feedback"]["dislike"], 1)
            self.assertEqual(report["feedback"]["follow"], 1)
            self.assertEqual(config_path.read_bytes(), before_config)
            self.assertEqual(database_path.read_bytes(), before_database)


if __name__ == "__main__":
    unittest.main()

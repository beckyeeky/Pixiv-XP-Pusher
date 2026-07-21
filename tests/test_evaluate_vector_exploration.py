import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "evaluate_vector_exploration.py"
SPEC = importlib.util.spec_from_file_location("evaluate_vector_exploration", SCRIPT_PATH)
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


class EvaluateVectorExplorationScriptTests(unittest.TestCase):
    def prepare(self, path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.executescript("""
                CREATE TABLE exploration_vector_runs(
                    run_id TEXT PRIMARY KEY, user_id INTEGER, source TEXT, model TEXT,
                    profile_hash TEXT, pool_limit INTEGER, pool_size INTEGER,
                    candidate_limit INTEGER, similarity_threshold REAL,
                    duplicate_threshold REAL, status TEXT,
                    profile_concentration REAL, slate_profile_concentration REAL,
                    duplicate_semantic_rate REAL, created_at TIMESTAMP, completed_at TIMESTAMP
                );
                CREATE TABLE exploration_vector_candidates(
                    run_id TEXT, illust_id INTEGER, source TEXT, similarity REAL,
                    model TEXT, retrieval_rank INTEGER, final_rank INTEGER,
                    selected INTEGER, tags TEXT,
                    PRIMARY KEY (run_id, illust_id)
                );
                CREATE TABLE feedback(illust_id INTEGER PRIMARY KEY, action TEXT, created_at TEXT);
            """)
            connection.execute(
                """
                INSERT INTO exploration_vector_runs VALUES (
                    'run-1', 7, 'semantic_vector_exploration', 'embed-v1', 'hash',
                    1000, 100, 40, 0.6, 0.9, 'completed', 0.2, 0.25, 0.1,
                    '2026-07-20 10:00:00', '2026-07-20 10:01:00'
                )
                """
            )
            connection.executemany(
                "INSERT INTO exploration_vector_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("run-1", 1, "semantic_vector_exploration", 0.8, "embed-v1", 1, 3, 1, "[]"),
                    ("run-1", 2, "semantic_vector_exploration", 0.7, "embed-v1", 3, 2, 1, "[]"),
                ],
            )
            connection.executemany(
                "INSERT INTO feedback VALUES (?, ?, ?)",
                [(1, "like", "2026-07-20 12:00:00"), (2, "skip", "2026-07-20 12:00:00")],
            )

    def test_human_output_shows_signed_rank_movement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "exploration.db"
            self.prepare(path)
            output = io.StringIO()
            with patch("sys.stdout", output):
                exit_code = script.main(["--db", str(path)])

        self.assertEqual(exit_code, 0)
        text = output.getvalue()
        self.assertIn("带符号", text)
        self.assertIn("0.5000", text)
        self.assertIn("skip=1", text)

    def test_json_output_stays_machine_readable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "exploration.db"
            self.prepare(path)
            output = io.StringIO()
            with patch("sys.stdout", output):
                exit_code = script.main(["--db", str(path), "--json"])

        self.assertEqual(exit_code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["mean_signed_rank_movement"], 0.5)
        self.assertEqual(report["skips"], 1)


if __name__ == "__main__":
    unittest.main()

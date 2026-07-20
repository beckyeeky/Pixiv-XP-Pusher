import json
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
from exploration_vector_evaluation import load_vector_exploration_evaluation


class ExplorationVectorStoreTests(unittest.TestCase):
    def test_pool_is_model_bounded_and_excludes_pushed_and_current_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "test.db"
            with patch.object(database, "DB_PATH", path):
                database._init_db_sync()
                connection = sqlite3.connect(path)
                connection.executemany(
                    "INSERT INTO illust_embeddings (illust_id, embedding, model) VALUES (?, ?, ?)",
                    [(1, json.dumps([1, 0]), "v1"), (2, json.dumps([0, 1]), "v1"),
                     (3, json.dumps([1, 1]), "v2"), (4, json.dumps([0.5, 0.5]), "v1")],
                )
                connection.execute("INSERT INTO push_history (illust_id) VALUES (2)")
                connection.commit()
                connection.close()
                pool = asyncio.run(
                    database.get_vector_exploration_pool("v1", 10, exclude_ids={4})
                )
        self.assertEqual(pool, [(1, [1, 0])])

    def test_candidate_audit_records_final_rank_and_selection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "test.db"
            with patch.object(database, "DB_PATH", path):
                database._init_db_sync()
                asyncio.run(database.start_vector_exploration_run(
                    run_id="run-1", user_id=7, model="v1", profile_hash="hash",
                    pool_limit=10, pool_size=3, candidate_limit=2,
                    similarity_threshold=0.6, duplicate_threshold=0.9,
                    profile_concentration=0.3,
                ))
                asyncio.run(database.record_vector_exploration_candidates("run-1", [{
                    "illust_id": 1, "source": "semantic_vector_exploration",
                    "similarity": 0.8, "model": "v1", "retrieval_rank": 1,
                    "tags": ["a"],
                }]))
                asyncio.run(database.complete_vector_exploration_run(
                    "run-1", ranked_ids=[9, 1], selected_ids={1},
                    slate_profile_concentration=0.4, duplicate_semantic_rate=0.2,
                ))
                connection = sqlite3.connect(path)
                candidate = connection.execute(
                    "SELECT final_rank, selected FROM exploration_vector_candidates"
                ).fetchone()
                run = connection.execute(
                    "SELECT status, profile_concentration, slate_profile_concentration, "
                    "duplicate_semantic_rate "
                    "FROM exploration_vector_runs"
                ).fetchone()
                connection.execute(
                    "INSERT INTO feedback (illust_id, action) VALUES (1, 'like')"
                )
                connection.commit()
                connection.close()
                report = load_vector_exploration_evaluation(path)
        self.assertEqual(candidate, (2, 1))
        self.assertEqual(run, ("completed", 0.3, 0.4, 0.2))
        self.assertEqual((report.feedback_count, report.likes), (1, 1))

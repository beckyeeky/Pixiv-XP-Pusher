import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from embedder import profile_embedding_hash
from embedding_calibration_store import load_calibration_dataset


class EmbeddingCalibrationStoreTests(unittest.TestCase):
    def prepare(self, path: Path, *, stale_profile: bool = False) -> None:
        with sqlite3.connect(path) as connection:
            connection.executescript("""
                CREATE TABLE xp_profile(tag TEXT PRIMARY KEY, weight REAL);
                CREATE TABLE negative_profile(tag TEXT PRIMARY KEY, weight REAL);
                CREATE TABLE tag_aliases(
                    original_tag TEXT PRIMARY KEY, normalized_tag TEXT,
                    kind TEXT, source TEXT
                );
                CREATE TABLE tag_classification_cache(
                    normalized_tag TEXT PRIMARY KEY, classification TEXT, source TEXT, updated_at TEXT
                );
                CREATE TABLE feedback(illust_id INTEGER PRIMARY KEY, action TEXT);
                CREATE TABLE illust_cache(illust_id INTEGER PRIMARY KEY, tags TEXT, source TEXT);
                CREATE TABLE illust_embeddings(
                    illust_id INTEGER PRIMARY KEY, embedding TEXT, model TEXT, created_at TEXT
                );
                CREATE TABLE user_embedding(
                    user_id INTEGER PRIMARY KEY, embedding TEXT, model TEXT,
                    profile_hash TEXT, updated_at TEXT
                );
            """)
            profile = {"white_hair": 2.0}
            connection.execute("INSERT INTO xp_profile VALUES ('white_hair', 2.0)")
            connection.execute(
                "INSERT INTO tag_aliases VALUES ('白髪', 'white_hair', 'equivalent', 'manual')"
            )
            connection.execute(
                "INSERT INTO tag_aliases VALUES ('白发', 'white_hair', 'search', 'manual')"
            )
            connection.execute(
                "INSERT INTO tag_classification_cache VALUES ('white_hair', 'feature', 'manual', CURRENT_TIMESTAMP)"
            )
            connection.executemany(
                "INSERT INTO feedback VALUES (?, ?)", [(1, "like"), (2, "dislike"), (3, "like")]
            )
            connection.executemany(
                "INSERT INTO illust_cache VALUES (?, ?, 'xp_search')",
                [(1, json.dumps(["白髪"])), (2, json.dumps(["白发"])), (3, json.dumps(["white_hair"]))],
            )
            connection.executemany(
                "INSERT INTO illust_embeddings VALUES (?, ?, 'embed-v1', CURRENT_TIMESTAMP)",
                [(1, json.dumps([1.0, 0.0])), (2, json.dumps([-1.0, 0.0]))],
            )
            cache_hash = "stale" if stale_profile else profile_embedding_hash(profile)
            connection.execute(
                "INSERT INTO user_embedding VALUES (7, ?, 'embed-v1', ?, CURRENT_TIMESTAMP)",
                (json.dumps([1.0, 0.0]), cache_hash),
            )

    def test_loads_only_model_compatible_feedback_and_applies_normalized_mapping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "calibration.db"
            self.prepare(path)
            dataset = load_calibration_dataset(path, embedding_model="embed-v1", user_id=7)

        self.assertEqual(len(dataset.samples), 2)
        self.assertEqual(dataset.total_feedback, 3)
        self.assertEqual(dataset.missing, {"missing_work_embedding": 1})
        self.assertGreater(dataset.samples[0].tag_score, 0.0)
        self.assertEqual(dataset.samples[0].semantic_score, 1.0)
        self.assertEqual(dataset.samples[1].tag_score, 0.0)

    def test_stale_user_embedding_is_not_used(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "calibration.db"
            self.prepare(path, stale_profile=True)
            dataset = load_calibration_dataset(path, embedding_model="embed-v1", user_id=7)

        self.assertEqual(dataset.samples, ())
        self.assertEqual(dataset.missing, {"stale_user_embedding": 3})

    def test_wrong_sqlite_file_has_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wrong.db"
            sqlite3.connect(path).close()
            with self.assertRaisesRegex(ValueError, "缺少表"):
                load_calibration_dataset(path, embedding_model="embed-v1", user_id=7)


if __name__ == "__main__":
    unittest.main()

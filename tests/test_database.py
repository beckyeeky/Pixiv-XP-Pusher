import asyncio
import gc
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import database


class DatabaseInitTests(unittest.TestCase):
    def test_init_db_backfills_provenance_for_legacy_tag_evidence(self):
        async def _run(db_path):
            with patch.object(database, "DB_PATH", db_path):
                stale_time = datetime.now() - timedelta(days=61)
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(
                        """
                        CREATE TABLE tag_classification_evidence (
                            normalized_tag TEXT NOT NULL,
                            source TEXT NOT NULL,
                            classification TEXT NOT NULL,
                            confidence REAL NOT NULL DEFAULT 1.0,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (normalized_tag, source)
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO tag_classification_evidence
                            (normalized_tag, source, classification, confidence, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("tag", "danbooru", "character", 1.0, stale_time),
                    )
                    conn.commit()
                finally:
                    conn.close()
                await database.init_db()
                conn = sqlite3.connect(db_path)
                try:
                    row = conn.execute(
                        "SELECT observed_at, verified_at FROM tag_classification_evidence WHERE normalized_tag = ?",
                        ("tag",),
                    ).fetchone()
                finally:
                    conn.close()
                return row

        with tempfile.TemporaryDirectory() as tmpdir:
            provenance = asyncio.run(_run(Path(tmpdir) / "pixiv_xp.db"))

        self.assertEqual(database._parse_evidence_timestamp(provenance[0]).date(), (datetime.now() - timedelta(days=61)).date())
        self.assertEqual(database._parse_evidence_timestamp(provenance[1]).date(), (datetime.now() - timedelta(days=61)).date())

    def test_review_queue_prioritizes_profile_impact_and_accepts_manual_review(self):
        async def _run(db_path):
            with patch.object(database, "DB_PATH", db_path):
                await database.init_db()
                await database.update_xp_profile({"low_impact": 0.2, "high_impact": 3.0})
                await database.save_tag_classifications([
                    ("low_impact", "unresolved", "evidence_unresolved"),
                    ("high_impact", "unresolved", "evidence_unresolved"),
                ])
                await database.save_tag_evidence([
                    ("low_impact", "danbooru", "character", 1.0),
                    ("high_impact", "danbooru", "character", 1.0),
                ])
                queue = await database.get_tag_review_queue()
                await database.review_tag_classification("high_impact", "copyright")
                return queue, await database.get_tag_review_queue(), await database.get_tag_evidence(["high_impact"])

        with tempfile.TemporaryDirectory() as tmpdir:
            queue, remaining, evidence = asyncio.run(_run(Path(tmpdir) / "pixiv_xp.db"))

        self.assertEqual([item["tag"] for item in queue], ["high_impact", "low_impact"])
        self.assertEqual([item["tag"] for item in remaining], ["low_impact"])
        self.assertIn({"source": "manual", "classification": "copyright", "confidence": 1.0}, evidence["high_impact"])

    def test_review_normalizes_human_tag_input(self):
        async def _run(db_path):
            with patch.object(database, "DB_PATH", db_path):
                await database.init_db()
                await database.save_tag_classifications([("blue_archive", "unresolved", "ai")])
                await database.review_tag_classification("Blue Archive", "non_preference")
                return await database.get_tag_classifications(["blue_archive"])

        with tempfile.TemporaryDirectory() as tmpdir:
            result = asyncio.run(_run(Path(tmpdir) / "pixiv_xp.db"))
        self.assertEqual(result["blue_archive"]["source"], "manual")

    def test_init_db_creates_core_tables(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "pixiv_xp.db"
            with patch.object(database, "DB_PATH", db_path):
                asyncio.run(database.init_db())

            self.assertTrue(db_path.exists())

            conn = sqlite3.connect(db_path)
            cursor = None
            try:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                tables = {
                    row[0]
                    for row in cursor
                }
            finally:
                if cursor is not None:
                    cursor.close()
                conn.close()
                del cursor
                del conn
                gc.collect()

            self.assertIn("push_history", tables)
            self.assertIn("xp_profile", tables)
            self.assertIn("strategy_stats", tables)
            self.assertIn("tag_classification_cache", tables)


if __name__ == "__main__":
    unittest.main()

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
from tag_classification_migration import (
    audit_fallback_classifications,
    backup_database,
    migrate_fallback_classifications,
)


class TagClassificationMigrationTests(unittest.TestCase):
    def _database(self, root: str) -> Path:
        db_path = Path(root) / "pixiv_xp.db"
        with patch.object(database, "DB_PATH", db_path):
            database._init_db_sync()
        with sqlite3.connect(db_path) as conn:
            conn.executemany(
                """INSERT INTO tag_classification_cache
                   (normalized_tag, classification, source)
                   VALUES (?, ?, ?)""",
                [
                    ("legacy_trait", "feature", "fallback"),
                    ("manual_trait", "feature", "manual"),
                    ("unknown_trait", "unresolved", "ai"),
                ],
            )
            conn.execute(
                """INSERT INTO tag_mapping_candidates
                   (original_tag, proposed_normalized_tag, source)
                   VALUES ('legacy_trait', 'known_trait', 'test')"""
            )
        return db_path

    def test_dry_run_reports_but_does_not_mutate_fallback_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._database(tmpdir)
            report = migrate_fallback_classifications(db_path, apply=False)
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT classification, source FROM tag_classification_cache "
                    "WHERE normalized_tag = 'legacy_trait'"
                ).fetchone()

        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["before"]["fallback_feature_count"], 1)
        self.assertEqual(report["before"]["pending_mapping_candidates"], 1)
        self.assertEqual(report["before"]["integrity_check"], "ok")
        self.assertEqual(row, ("feature", "fallback"))

    def test_apply_only_quarantines_legacy_fallback_and_preserves_manual_truth(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._database(tmpdir)
            report = migrate_fallback_classifications(db_path, apply=True)
            audit = audit_fallback_classifications(db_path)
            with sqlite3.connect(db_path) as conn:
                rows = dict(conn.execute(
                    "SELECT normalized_tag, classification FROM tag_classification_cache"
                ))

        self.assertEqual(report["updated_rows"], 1)
        self.assertEqual(audit["fallback_feature_count"], 0)
        self.assertEqual(rows["legacy_trait"], "unresolved")
        self.assertEqual(rows["manual_trait"], "feature")

    def test_backup_is_a_consistent_recoverable_sqlite_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._database(tmpdir)
            backup_path = backup_database(db_path, Path(tmpdir) / "before.db")
            with sqlite3.connect(backup_path) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                count = conn.execute(
                    "SELECT COUNT(*) FROM tag_classification_cache"
                ).fetchone()[0]

        self.assertEqual(integrity, "ok")
        self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()

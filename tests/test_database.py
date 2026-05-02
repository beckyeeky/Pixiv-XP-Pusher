import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database


class DatabaseInitTests(unittest.TestCase):
    def test_init_db_creates_core_tables(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "pixiv_xp.db"
            with patch.object(database, "DB_PATH", db_path):
                asyncio.run(database.init_db())

            self.assertTrue(db_path.exists())

            conn = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                conn.close()

            self.assertIn("push_history", tables)
            self.assertIn("xp_profile", tables)
            self.assertIn("strategy_stats", tables)
            self.assertIn("tag_classification_cache", tables)


if __name__ == "__main__":
    unittest.main()

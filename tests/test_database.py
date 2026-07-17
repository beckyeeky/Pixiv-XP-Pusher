import asyncio
import gc
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import database
from tag_relationship_judge import relationship_evidence


class DatabaseInitTests(unittest.TestCase):
    def test_v6_migrates_existing_mapping_candidates_for_embedding_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "pixiv_xp.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE tag_mapping_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_tag TEXT NOT NULL,
                    proposed_normalized_tag TEXT,
                    kind TEXT NOT NULL DEFAULT 'equivalent',
                    source TEXT NOT NULL,
                    explanation TEXT NOT NULL DEFAULT '',
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
            conn.close()
            with patch.object(database, "DB_PATH", db_path):
                database._init_db_sync()
            conn = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(tag_mapping_candidates)"
                    )
                }
            finally:
                conn.close()
        self.assertIn("embedding_similarity", columns)

    def test_ai_recommendation_is_audited_and_staged_without_creating_alias(self):
        async def _run(db_path):
            with patch.object(database, "DB_PATH", db_path):
                await database.init_db()
                await database.update_xp_profile({"白髪": 2.0, "white_hair": 3.0})
                await database.save_tag_classifications([
                    ("白髪", "feature", "ai"),
                    ("white_hair", "feature", "ai"),
                ])
                await database.save_tag_mapping_candidates([{
                    "original_tag": "白髪",
                    "proposed_normalized_tag": "white_hair",
                    "source": "test",
                    "explanation": "same identity",
                    "embedding_similarity": 0.94,
                }])
                candidate = (await database.get_tag_mapping_candidates(limit=10))[0]
                recommendation_id = await database.save_tag_mapping_ai_recommendation(
                    candidate["id"],
                    {
                        "relation": "equivalent", "confidence": 0.98,
                        "rationale": "same", "canonical_tag": "white_hair",
                        "risk_flags": [], "principle_checks": {
                            "same_identity": True, "broader_narrower": False,
                            "entity_franchise": False, "modifier_variant": False,
                        },
                    },
                    model="deepseek:model",
                    principles_version="tag-alias-review-v1",
                    evidence=relationship_evidence(candidate),
                )
                enriched = (await database.get_tag_mapping_candidates(limit=10))[0]
                staged = await database.stage_tag_mapping_ai_recommendations([{
                    "candidate_id": candidate["id"],
                    "recommendation_id": recommendation_id,
                    "decision": "accept_equivalent",
                }])
                after = (await database.get_tag_mapping_candidates(limit=10))[0]
                aliases = await database.get_accepted_tag_aliases()
                return enriched, staged, after, aliases

        with tempfile.TemporaryDirectory() as tmpdir:
            enriched, staged, after, aliases = asyncio.run(
                _run(Path(tmpdir) / "pixiv_xp.db")
            )
        self.assertEqual(enriched["ai_relation"], "equivalent")
        self.assertEqual(enriched["original_classification"], "feature")
        self.assertEqual(enriched["embedding_similarity"], 0.94)
        self.assertTrue(enriched["ai_is_current"])
        self.assertEqual(staged, 1)
        self.assertEqual(after["ai_staged_decision"], "accept_equivalent")
        self.assertEqual(after["status"], "pending")
        self.assertEqual(aliases, {})

    def test_init_quarantines_legacy_automatic_mappings_without_activating_them(self):
        async def _run(db_path):
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE ai_tag_cache (
                        original_tag TEXT PRIMARY KEY,
                        cleaned_tag TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE tag_mapping_stats (
                        normalized_tag TEXT,
                        original_tag TEXT,
                        frequency INTEGER DEFAULT 0,
                        PRIMARY KEY (normalized_tag, original_tag)
                    );
                    INSERT INTO ai_tag_cache (original_tag, cleaned_tag) VALUES
                        ('パンツ', 'panties'),
                        ('platform_noise', NULL),
                        ('same', 'same');
                    INSERT INTO tag_mapping_stats (normalized_tag, original_tag, frequency)
                        VALUES ('panties', 'パンツ', 4);
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(database, "DB_PATH", db_path):
                await database.init_db()
                candidates = await database.get_tag_mapping_candidates(limit=20)
                aliases = await database.get_accepted_tag_aliases()
                await database.init_db()
                candidates_after_second_init = await database.get_tag_mapping_candidates(limit=20)
            return candidates, aliases, candidates_after_second_init

        with tempfile.TemporaryDirectory() as tmpdir:
            candidates, aliases, repeated = asyncio.run(_run(Path(tmpdir) / "pixiv_xp.db"))

        self.assertEqual(len(candidates), 3)
        self.assertEqual(aliases, {})
        self.assertEqual(len(repeated), 3)
        self.assertEqual(
            {item["source"] for item in candidates},
            {"legacy_ai_tag_cache", "legacy_tag_mapping_stats"},
        )

    def test_only_human_acceptance_creates_a_runtime_tag_alias(self):
        async def _run(db_path):
            with patch.object(database, "DB_PATH", db_path):
                await database.init_db()
                await database.save_tag_mapping_candidates([
                    {
                        "original_tag": "ブルアカ",
                        "proposed_normalized_tag": "blue_archive",
                        "kind": "equivalent",
                        "source": "test_generator",
                        "explanation": "Known Japanese abbreviation.",
                    },
                    {
                        "original_tag": "着物ビキニ",
                        "proposed_normalized_tag": "kimono",
                        "kind": "equivalent",
                        "source": "test_generator",
                    },
                ])
                pending = await database.get_tag_mapping_candidates(limit=10)
                by_original = {item["original_tag"]: item for item in pending}
                before = await database.get_accepted_tag_aliases()
                accepted = await database.review_tag_mapping_candidate(
                    by_original["ブルアカ"]["id"], "accept"
                )
                rejected = await database.review_tag_mapping_candidate(
                    by_original["着物ビキニ"]["id"], "reject"
                )
                after = await database.get_accepted_tag_aliases()
                search_term = await database.get_best_search_tag("blue_archive")
                return before, accepted, rejected, after, search_term

        with tempfile.TemporaryDirectory() as tmpdir:
            before, accepted, rejected, after, search_term = asyncio.run(
                _run(Path(tmpdir) / "pixiv_xp.db")
            )

        self.assertEqual(before, {})
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(after, {"ブルアカ": "blue_archive"})
        self.assertEqual(search_term, "ブルアカ")

    def test_high_weight_unclassified_profile_tags_include_missing_and_unresolved(self):
        async def _run(db_path):
            with patch.object(database, "DB_PATH", db_path):
                await database.init_db()
                await database.update_xp_profile({
                    "missing_high": 8.0,
                    "unresolved_mid": 4.0,
                    "resolved_high": 7.0,
                    "missing_low": 0.2,
                })
                await database.save_tag_classifications([
                    ("unresolved_mid", "unresolved", "ai"),
                    ("resolved_high", "feature", "manual"),
                ])
                return await database.get_high_weight_unclassified_profile_tags(
                    limit=10, min_profile_weight=1.0,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            tags = asyncio.run(_run(Path(tmpdir) / "pixiv_xp.db"))

        self.assertEqual(
            tags,
            [
                {"tag": "missing_high", "profile_weight": 8.0, "classification": None},
                {"tag": "unresolved_mid", "profile_weight": 4.0, "classification": "unresolved"},
            ],
        )

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
        self.assertEqual(queue[0]["evidence"][0]["source"], "danbooru")
        self.assertTrue(queue[0]["evidence"][0]["is_fresh"])
        self.assertIsNotNone(queue[0]["evidence"][0]["observed_at"])
        self.assertIsNotNone(queue[0]["evidence"][0]["verified_at"])
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

    def test_manual_review_keeps_permanent_evidence_provenance(self):
        async def _run(db_path):
            with patch.object(database, "DB_PATH", db_path):
                await database.init_db()
                await database.review_tag_classification("blue_archive", "feature")
                return await database.get_tag_evidence(
                    ["blue_archive"], include_provenance=True,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            evidence = asyncio.run(_run(Path(tmpdir) / "pixiv_xp.db"))

        manual = evidence["blue_archive"][0]
        self.assertEqual(manual["source"], "manual")
        self.assertIsNotNone(manual["observed_at"])
        self.assertIsNotNone(manual["verified_at"])

    def test_batch_manual_reviews_are_atomic_and_reject_stale_tags(self):
        async def _run(db_path):
            with patch.object(database, "DB_PATH", db_path):
                await database.init_db()
                await database.save_tag_classifications([
                    ("first", "unresolved", "ai"),
                    ("second", "unresolved", "ai"),
                ])
                stale = await database.review_tag_classifications_batch([
                    ("first", "feature"), ("missing", "character"),
                ])
                queue_after_stale = await database.get_tag_review_queue()
                applied = await database.review_tag_classifications_batch([
                    ("first", "feature"), ("second", "copyright"),
                ])
                return stale, queue_after_stale, applied, await database.get_tag_review_queue(), await database.get_tag_evidence(["first", "second"])

        with tempfile.TemporaryDirectory() as tmpdir:
            stale, queue_after_stale, applied, remaining, evidence = asyncio.run(_run(Path(tmpdir) / "pixiv_xp.db"))

        self.assertEqual(stale, ["missing"])
        self.assertEqual({item["tag"] for item in queue_after_stale}, {"first", "second"})
        self.assertEqual(applied, [])
        self.assertEqual(remaining, [])
        self.assertEqual(evidence["first"][0]["classification"], "feature")
        self.assertEqual(evidence["second"][0]["classification"], "copyright")

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
            self.assertIn("tag_aliases", tables)
            self.assertIn("tag_mapping_candidates", tables)


if __name__ == "__main__":
    unittest.main()

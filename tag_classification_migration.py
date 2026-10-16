"""Auditable migration for legacy ``feature/fallback`` tag classifications."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def audit_fallback_classifications(
    db_path: str | Path,
    *,
    sample_limit: int = 20,
) -> dict:
    """Return the pre/post migration evidence without changing the database."""
    with _connect(db_path) as connection:
        required = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = {
            "tag_classification_cache", "tag_mapping_candidates", "system_state",
        } - required
        if missing:
            raise RuntimeError(f"database schema is incomplete: {sorted(missing)}")

        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        schema_row = connection.execute(
            "SELECT value FROM system_state WHERE key = 'schema_version'"
        ).fetchone()
        fallback_count = connection.execute(
            """SELECT COUNT(*) FROM tag_classification_cache
               WHERE classification = 'feature' AND source = 'fallback'"""
        ).fetchone()[0]
        samples = [
            row[0]
            for row in connection.execute(
                """SELECT normalized_tag FROM tag_classification_cache
                   WHERE classification = 'feature' AND source = 'fallback'
                   ORDER BY normalized_tag LIMIT ?""",
                (max(0, int(sample_limit)),),
            )
        ]
        classifications = {
            f"{row['classification']}/{row['source']}": int(row["count"])
            for row in connection.execute(
                """SELECT classification, source, COUNT(*) AS count
                   FROM tag_classification_cache
                   GROUP BY classification, source
                   ORDER BY classification, source"""
            )
        }
        candidate_counts = {
            row["status"]: int(row["count"])
            for row in connection.execute(
                """SELECT status, COUNT(*) AS count
                   FROM tag_mapping_candidates GROUP BY status"""
            )
        }
        bookmark_count = connection.execute(
            "SELECT COUNT(*) FROM xp_bookmarks"
        ).fetchone()[0]
        profile_count = connection.execute(
            "SELECT COUNT(*) FROM xp_profile"
        ).fetchone()[0]

    return {
        "database": str(Path(db_path).resolve()),
        "schema_version": int(schema_row[0]) if schema_row else None,
        "integrity_check": integrity,
        "fallback_feature_count": int(fallback_count),
        "fallback_feature_samples": samples,
        "classification_counts": classifications,
        "pending_mapping_candidates": candidate_counts.get("pending", 0),
        "mapping_candidate_counts": candidate_counts,
        "cached_bookmarks": int(bookmark_count),
        "profile_tags": int(profile_count),
    }


def backup_database(
    db_path: str | Path,
    destination: str | Path | None = None,
) -> Path:
    """Create a consistent SQLite backup suitable for rollback."""
    source_path = Path(db_path)
    if destination is None:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        destination_path = source_path.with_name(f"{source_path.name}.bak-{stamp}")
    else:
        destination_path = Path(destination)
    if destination_path.exists():
        raise FileExistsError(f"backup already exists: {destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(source_path) as source, sqlite3.connect(destination_path) as target:
        source.backup(target)
        target.commit()
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"backup integrity check failed: {integrity}")
    return destination_path


def migrate_fallback_classifications(
    db_path: str | Path,
    *,
    apply: bool = False,
    sample_limit: int = 20,
) -> dict:
    """Quarantine legacy fallback truth; dry-run unless ``apply=True``."""
    before = audit_fallback_classifications(db_path, sample_limit=sample_limit)
    if not apply:
        return {"mode": "dry-run", "updated_rows": 0, "before": before}

    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(
                """UPDATE tag_classification_cache
                   SET classification = 'unresolved',
                       source = 'migration_unresolved',
                       updated_at = CURRENT_TIMESTAMP
                   WHERE classification = 'feature' AND source = 'fallback'"""
            )
            updated_rows = int(cursor.rowcount)
            if updated_rows != before["fallback_feature_count"]:
                raise RuntimeError(
                    "fallback classification count changed during migration"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    after = audit_fallback_classifications(db_path, sample_limit=sample_limit)
    if after["integrity_check"] != "ok" or after["fallback_feature_count"] != 0:
        raise RuntimeError("post-migration integrity verification failed")
    return {
        "mode": "apply",
        "updated_rows": updated_rows,
        "before": before,
        "after": after,
    }

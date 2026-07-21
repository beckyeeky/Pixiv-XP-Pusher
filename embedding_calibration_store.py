"""Read-only SQLite adapter for work-level Embedding calibration."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3

from embedder import profile_embedding_hash
from embedding_calibration import CalibrationSample, normalized_cosine
from filter import calculate_tag_match_score
from tag_mapping import TagIdentityResolver


@dataclass(frozen=True)
class CalibrationDataset:
    samples: tuple[CalibrationSample, ...]
    total_feedback: int
    missing: dict[str, int]
    user_id: int | None
    embedding_model: str
    profile_hash: str
    cached_profile_hash: str | None
    stored_like: int
    stored_dislike: int
    stored_follow: int
    first_feedback_at: str | None
    latest_feedback_at: str | None


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _read_map(connection: sqlite3.Connection, query: str) -> dict:
    return {row[0]: row[1] for row in connection.execute(query).fetchall()}


def load_calibration_dataset(
    database_path: Path,
    *,
    embedding_model: str,
    user_id: int | None = None,
    classification_ttl_days: int = 30,
) -> CalibrationDataset:
    """Load current scoring inputs without opening SQLite for writes."""

    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"数据库不存在: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        required_tables = {
            "xp_profile", "tag_classification_cache", "feedback", "illust_cache",
            "illust_embeddings", "tag_aliases", "user_embedding",
        }
        missing_tables = sorted(name for name in required_tables if not _table_exists(connection, name))
        if missing_tables:
            raise ValueError(
                f"不是可校准的 Pixiv-XP-Pusher 数据库，缺少表: {', '.join(missing_tables)}"
            )
        xp_profile = _read_map(connection, "SELECT tag, weight FROM xp_profile")
        if not xp_profile:
            raise ValueError("XP Profile 为空，无法校准作品级 Embedding")
        negative_profile = (
            _read_map(connection, "SELECT tag, weight FROM negative_profile")
            if _table_exists(connection, "negative_profile") else {}
        )
        tag_mappings = _read_map(
            connection,
            "SELECT original_tag, normalized_tag FROM tag_aliases WHERE kind = 'equivalent'",
        )
        tag_resolver = TagIdentityResolver(tag_mappings)
        cutoff = (datetime.now() - timedelta(days=classification_ttl_days)).strftime("%Y-%m-%d %H:%M:%S")
        tag_classifications = {
            row[0]: row[1]
            for row in connection.execute(
                """
                SELECT normalized_tag, classification
                FROM tag_classification_cache
                WHERE source = 'manual' OR updated_at >= ?
                """,
                (cutoff,),
            ).fetchall()
        }
        current_profile_hash = profile_embedding_hash(xp_profile)

        feedback_rows = connection.execute(
            """
            SELECT f.illust_id, f.action, c.tags, c.source,
                   e.embedding, e.model
            FROM feedback AS f
            LEFT JOIN illust_cache AS c ON c.illust_id = f.illust_id
            LEFT JOIN illust_embeddings AS e ON e.illust_id = f.illust_id
            WHERE f.action IN ('like', 'dislike')
            ORDER BY f.illust_id
            """
        ).fetchall()
        total_feedback = len(feedback_rows)

        action_counts = _read_map(
            connection, "SELECT action, COUNT(*) FROM feedback GROUP BY action"
        )
        time_row = connection.execute(
            """
            SELECT MIN(created_at), MAX(created_at)
            FROM feedback
            WHERE action IN ('like', 'dislike', 'follow')
            """
        ).fetchone()
        feedback_meta = {
            "stored_like": int(action_counts.get("like") or 0),
            "stored_dislike": int(action_counts.get("dislike") or 0),
            "stored_follow": int(action_counts.get("follow") or 0),
            "first_feedback_at": time_row[0],
            "latest_feedback_at": time_row[1],
        }

        if user_id is None:
            user_row = connection.execute(
                """
                SELECT user_id, embedding, model, profile_hash
                FROM user_embedding
                WHERE model = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (embedding_model,),
            ).fetchone()
        else:
            user_row = connection.execute(
                """
                SELECT user_id, embedding, model, profile_hash
                FROM user_embedding
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()

        missing = Counter()
        if user_row is None:
            missing["missing_user_embedding"] = total_feedback
            return CalibrationDataset(
                samples=(), total_feedback=total_feedback, missing=dict(missing), **feedback_meta,
                user_id=user_id, embedding_model=embedding_model,
                profile_hash=current_profile_hash, cached_profile_hash=None,
            )
        resolved_user_id = int(user_row["user_id"])
        cached_profile_hash = str(user_row["profile_hash"] or "")
        if user_row["model"] != embedding_model:
            missing["user_embedding_model_mismatch"] = total_feedback
            return CalibrationDataset(
                samples=(), total_feedback=total_feedback, missing=dict(missing), **feedback_meta,
                user_id=resolved_user_id, embedding_model=embedding_model,
                profile_hash=current_profile_hash, cached_profile_hash=cached_profile_hash,
            )
        if cached_profile_hash != current_profile_hash:
            missing["stale_user_embedding"] = total_feedback
            return CalibrationDataset(
                samples=(), total_feedback=total_feedback, missing=dict(missing), **feedback_meta,
                user_id=resolved_user_id, embedding_model=embedding_model,
                profile_hash=current_profile_hash, cached_profile_hash=cached_profile_hash,
            )
        try:
            user_embedding = json.loads(user_row["embedding"])
            if not isinstance(user_embedding, list):
                raise ValueError
        except (TypeError, json.JSONDecodeError, ValueError):
            missing["invalid_user_embedding"] = total_feedback
            return CalibrationDataset(
                samples=(), total_feedback=total_feedback, missing=dict(missing), **feedback_meta,
                user_id=resolved_user_id, embedding_model=embedding_model,
                profile_hash=current_profile_hash, cached_profile_hash=cached_profile_hash,
            )

        samples: list[CalibrationSample] = []
        for row in feedback_rows:
            if not row["tags"]:
                missing["missing_cached_tags"] += 1
                continue
            if not row["embedding"]:
                missing["missing_work_embedding"] += 1
                continue
            if row["model"] != embedding_model:
                missing["work_embedding_model_mismatch"] += 1
                continue
            try:
                tags = json.loads(row["tags"])
                work_embedding = json.loads(row["embedding"])
                if not isinstance(tags, list) or not isinstance(work_embedding, list):
                    raise ValueError
                semantic_score = normalized_cosine(user_embedding, work_embedding)
            except (TypeError, json.JSONDecodeError, ValueError):
                missing["invalid_cached_data"] += 1
                continue
            tag_score = calculate_tag_match_score(
                [str(tag) for tag in tags],
                xp_profile,
                negative_profile,
                tag_classifications=tag_classifications,
                tag_resolver=tag_resolver,
            )
            samples.append(CalibrationSample(
                illust_id=int(row["illust_id"]),
                action=str(row["action"]),
                tag_score=tag_score,
                semantic_score=semantic_score,
            ))

        return CalibrationDataset(
            samples=tuple(samples), total_feedback=total_feedback, missing=dict(missing), **feedback_meta,
            user_id=resolved_user_id, embedding_model=embedding_model,
            profile_hash=current_profile_hash, cached_profile_hash=cached_profile_hash,
        )
    finally:
        connection.close()

"""
SQLite 数据层
"""
import json
import aiosqlite
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

from tag_categories import (
    IDENTITY_TAG_CATEGORIES,
    TAG_CATEGORY_FEATURE,
    TAG_CATEGORY_UNRESOLVED,
    normalize_tag_category,
)
from tag_mapping import would_create_alias_cycle
from tag_relationship_judge import (
    MERGE_PRINCIPLES_VERSION,
    hash_relationship_evidence,
    relationship_evidence_hash,
)
from utils import normalize_tag

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "pixiv_xp.db"
SCHEMA_VERSION = 6
TAG_EVIDENCE_FRESHNESS_DAYS = 60


async def init_db():
    """初始化数据库表结构"""
    _init_db_sync()


def _init_db_sync():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as db:
        # ============ 简易迁移逻辑 ============
        # 检查 xp_bookmarks 表是否包含 user_id 列 (旧版没有)
        try:
             db.execute("SELECT user_id FROM xp_bookmarks LIMIT 0")
        except Exception:
             db.execute("DROP TABLE IF EXISTS xp_bookmarks")
             db.commit()
             db.execute("DROP TABLE IF EXISTS xp_profile")
             db.execute("DROP TABLE IF EXISTS xp_tag_pairs")
             db.commit()
        
        # 检查 illust_cache 表是否包含 user_id 列 (v2 新增)
        try:
             db.execute("SELECT user_id FROM illust_cache LIMIT 0")
        except Exception:
             # 旧表只有 tags，删除重建
             db.execute("DROP TABLE IF EXISTS illust_cache")
             db.commit()
        
        db.executescript("""
            -- 推送历史
            CREATE TABLE IF NOT EXISTS push_history (
                illust_id INTEGER PRIMARY KEY,
                pushed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                source TEXT  -- 'search' | 'subscription'
            );
            
            -- XP画像
            CREATE TABLE IF NOT EXISTS xp_profile (
                tag TEXT PRIMARY KEY,
                weight REAL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- XP Tag组合 (新)
            CREATE TABLE IF NOT EXISTS xp_tag_pairs (
                tag1 TEXT,
                tag2 TEXT,
                weight REAL,
                PRIMARY KEY (tag1, tag2)
            );
            
            -- 用户反馈
            CREATE TABLE IF NOT EXISTS feedback (
                illust_id INTEGER PRIMARY KEY,
                action TEXT,  -- 'like' | 'dislike' | 'skip'
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- 收藏同步记录
            CREATE TABLE IF NOT EXISTS bookmarks (
                illust_id INTEGER PRIMARY KEY,
                scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- 临时黑名单(由反馈生成)
            CREATE TABLE IF NOT EXISTS tag_blacklist (
                tag TEXT PRIMARY KEY,
                dislike_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- 作品缓存(用于反馈处理) - v2: 增加画师信息
            CREATE TABLE IF NOT EXISTS illust_cache (
                illust_id INTEGER PRIMARY KEY,
                tags TEXT,  -- JSON数组
                user_id INTEGER,      -- 画师ID
                user_name TEXT,       -- 画师名
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- AI 处理错误日志
            CREATE TABLE IF NOT EXISTS ai_error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tags_content TEXT,  -- JSON数组，原始Tags
                error_msg TEXT,
                status TEXT DEFAULT 'pending',  -- pending, resolved, ignored
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            -- 用户XP分析用的收藏数据缓存
            CREATE TABLE IF NOT EXISTS xp_bookmarks (
                illust_id INTEGER PRIMARY KEY,
                user_id INTEGER,       -- 收藏者的ID
                tags TEXT,             -- JSON encoded tags
                illust_create_date TIMESTAMP, -- 作品创建时间
                scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- 系统状态表 (用于记录同步状态等)
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            -- 隔离的旧标签映射统计（仅迁入候选，不再供运行时读取）
            CREATE TABLE IF NOT EXISTS tag_mapping_stats (
                normalized_tag TEXT,
                original_tag TEXT,
                frequency INTEGER DEFAULT 0,
                PRIMARY KEY (normalized_tag, original_tag)
            );
            
            -- 隔离的旧 AI 处理缓存（仅迁入候选，不再供运行时读取）
            CREATE TABLE IF NOT EXISTS ai_tag_cache (
                original_tag TEXT PRIMARY KEY,
                cleaned_tag TEXT,  -- NULL 表示被过滤(meaningless)
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 只有人工接受的别名可以参与 Normalized Tag 聚合或搜索反查。
            CREATE TABLE IF NOT EXISTS tag_aliases (
                original_tag TEXT PRIMARY KEY,
                normalized_tag TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('equivalent', 'search')),
                source TEXT NOT NULL DEFAULT 'manual',
                priority INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 自动模型和旧系统只能写候选；候选在人工接受前没有运行时效力。
            CREATE TABLE IF NOT EXISTS tag_mapping_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_tag TEXT NOT NULL,
                proposed_normalized_tag TEXT,
                kind TEXT NOT NULL DEFAULT 'equivalent'
                    CHECK(kind IN ('equivalent', 'search')),
                source TEXT NOT NULL,
                explanation TEXT NOT NULL DEFAULT '',
                embedding_similarity REAL,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'accepted', 'rejected')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_tag_mapping_candidates_review
                ON tag_mapping_candidates(status, occurrence_count DESC, id);
            CREATE INDEX IF NOT EXISTS idx_tag_aliases_normalized
                ON tag_aliases(normalized_tag, kind, priority DESC);

            -- AI Relationship Recommendations are advisory evidence only.
            CREATE TABLE IF NOT EXISTS tag_mapping_ai_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL,
                relation TEXT NOT NULL,
                confidence REAL NOT NULL,
                rationale TEXT NOT NULL,
                canonical_tag TEXT,
                risk_flags TEXT NOT NULL,
                principle_checks TEXT NOT NULL,
                model TEXT NOT NULL,
                principles_version TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                evidence_payload TEXT NOT NULL,
                recommendation_payload TEXT NOT NULL,
                staged_decision TEXT
                    CHECK(staged_decision IN ('accept_equivalent', 'reject')),
                staged_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(candidate_id) REFERENCES tag_mapping_candidates(id)
            );
            CREATE INDEX IF NOT EXISTS idx_tag_mapping_ai_candidate
                ON tag_mapping_ai_recommendations(candidate_id, id DESC);

            -- Tag 分类缓存 (Tag Category; legacy ip is interpreted as copyright)
            CREATE TABLE IF NOT EXISTS tag_classification_cache (
                normalized_tag TEXT PRIMARY KEY,
                classification TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tag_classification_evidence (
                normalized_tag TEXT NOT NULL,
                source TEXT NOT NULL,
                classification TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (normalized_tag, source)
            );

            CREATE TABLE IF NOT EXISTS ai_tag_classification_records (
                tag TEXT PRIMARY KEY,
                classification TEXT NOT NULL,
                explanation TEXT NOT NULL,
                languages TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- MAB 策略统计表
            CREATE TABLE IF NOT EXISTS strategy_stats (
                strategy TEXT PRIMARY KEY,
                success_count INTEGER DEFAULT 0,
                total_count INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- Bot 快速屏蔽标签 (持久化)
            CREATE TABLE IF NOT EXISTS blocked_tags (
                tag TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Bot 临时静音标签 (/mute) - 到期自动失效
            CREATE TABLE IF NOT EXISTS muted_tags (
                tag TEXT PRIMARY KEY,
                until_ts TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- Bot 快速屏蔽画师 (持久化)
            CREATE TABLE IF NOT EXISTS blocked_artists (
                artist_id INTEGER PRIMARY KEY,
                artist_name TEXT,  -- 可选，用于显示
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Tag 翻译表 (Pixiv 原生翻译)
            CREATE TABLE IF NOT EXISTS tag_translations (
                name TEXT PRIMARY KEY,
                translated_name TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 画师权重档案 (用于 Related Works 策略)
            CREATE TABLE IF NOT EXISTS artist_profile (
                artist_id INTEGER PRIMARY KEY,
                score FLOAT DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- 负向画像 (用于记录负反馈，主动排斥相似作品)
            CREATE TABLE IF NOT EXISTS negative_profile (
                tag TEXT PRIMARY KEY,
                weight REAL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- 批量消息与作品映射 (用于 Telegraph 批量模式)
            CREATE TABLE IF NOT EXISTS batch_message_map (
                message_id INTEGER,
                chat_id TEXT,
                illust_index INTEGER,  -- 作品在批次中的编号 (1-based)
                illust_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, chat_id, illust_index)
            );
            
            -- 作品 Embedding 缓存 (用于语义匹配)
            CREATE TABLE IF NOT EXISTS illust_embeddings (
                illust_id INTEGER PRIMARY KEY,
                embedding TEXT,  -- JSON 序列化的向量
                model TEXT,      -- 使用的模型名
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- 用户画像 Embedding (低频更新)
            CREATE TABLE IF NOT EXISTS user_embedding (
                user_id INTEGER PRIMARY KEY,
                embedding TEXT,  -- JSON 序列化的向量
                model TEXT,
                profile_hash TEXT,  -- XP Profile 的哈希，用于判断是否需要更新
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        db.commit()

        candidate_columns = {
            row[1] for row in db.execute("PRAGMA table_info(tag_mapping_candidates)")
        }
        if "embedding_similarity" not in candidate_columns:
            db.execute(
                "ALTER TABLE tag_mapping_candidates ADD COLUMN embedding_similarity REAL"
            )
            db.commit()

        # Quarantine legacy automatic mappings as review candidates exactly once.
        # The source tables remain untouched for rollback/audit and are no longer
        # consumed by runtime identity resolution.
        legacy_imported = db.execute(
            "SELECT value FROM system_state WHERE key = 'legacy_tag_mapping_candidates_imported_v1'"
        ).fetchone()
        if not legacy_imported:
            db.execute(
                """
                INSERT INTO tag_mapping_candidates (
                    original_tag, proposed_normalized_tag, kind, source,
                    explanation, occurrence_count, status
                )
                SELECT original_tag, cleaned_tag, 'equivalent', 'legacy_ai_tag_cache',
                       CASE
                           WHEN cleaned_tag IS NULL THEN
                               'Legacy AITagProcessor marked this tag meaningless; requires review.'
                           ELSE
                               'Legacy AITagProcessor proposed this automatic mapping; requires review.'
                       END,
                       1, 'pending'
                FROM ai_tag_cache
                WHERE cleaned_tag IS NULL OR cleaned_tag <> original_tag
                """
            )
            db.execute(
                """
                INSERT INTO tag_mapping_candidates (
                    original_tag, proposed_normalized_tag, kind, source,
                    explanation, occurrence_count, status
                )
                SELECT original_tag, normalized_tag, 'search', 'legacy_tag_mapping_stats',
                       'Legacy reverse-search observation; requires review before reuse.',
                       MAX(frequency, 1), 'pending'
                FROM tag_mapping_stats
                """
            )
            db.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES ('legacy_tag_mapping_candidates_imported_v1', 'true', CURRENT_TIMESTAMP)
                """
            )
            db.commit()
        
        # === 迁移：为 illust_cache 添加 source 和 chain 列 ===
        try:
            db.execute("ALTER TABLE illust_cache ADD COLUMN source TEXT DEFAULT 'xp_search'")
            db.commit()
            logger.info("迁移：illust_cache 添加 source 列")
        except:
            pass  # 列已存在
        
        try:
            db.execute("ALTER TABLE illust_cache ADD COLUMN chain_depth INTEGER DEFAULT 0")
            db.execute("ALTER TABLE illust_cache ADD COLUMN chain_parent_id INTEGER")
            db.execute("ALTER TABLE illust_cache ADD COLUMN chain_msg_id INTEGER")
            db.commit()
            logger.info("迁移：illust_cache 添加 chain 列")
        except:
            pass  # 列已存在

        # Machine evidence keeps its own provenance.  Existing rows predate
        # these columns, so their former update time is the best available
        # observation and verification time.
        evidence_columns = {
            row[1] for row in db.execute("PRAGMA table_info(tag_classification_evidence)")
        }
        for column in ("observed_at", "verified_at"):
            if column not in evidence_columns:
                db.execute(f"ALTER TABLE tag_classification_evidence ADD COLUMN {column} TIMESTAMP")
        db.execute(
            """
            UPDATE tag_classification_evidence
            SET observed_at = COALESCE(observed_at, updated_at, CURRENT_TIMESTAMP),
                verified_at = COALESCE(verified_at, updated_at, CURRENT_TIMESTAMP)
            """
        )
        db.commit()
        
        # === 初始化 schema 版本元数据（兼容旧实例，不做破坏性升级） ===
        db.execute(
            "INSERT OR IGNORE INTO system_state (key, value, updated_at) VALUES (?, ?, ?)",
            ("schema_version", str(SCHEMA_VERSION), datetime.now())
        )
        db.execute(
            "UPDATE system_state SET value = ?, updated_at = ? WHERE key = ?",
            (str(SCHEMA_VERSION), datetime.now(), "schema_version")
        )

        # === 初始化 MAB 策略统计 (确保所有策略都有记录) ===
        default_strategies = ['xp_search', 'subscription', 'ranking', 'related', 'related_chain']
        for strategy in default_strategies:
            db.execute(
                "INSERT OR IGNORE INTO strategy_stats (strategy, success_count, total_count) VALUES (?, 0, 0)",
                (strategy,)
            )
        db.commit()


async def cleanup_old_records(days: int = 180):
    """
    清理过期数据，防止数据库无限增长
    
    Args:
        days: 保留最近多少天的记录 (默认 180 天)
    """
    import logging
    logger = logging.getLogger(__name__)
    
    cutoff_date = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff_date.strftime("%Y-%m-%d %H:%M:%S")
    
    async with aiosqlite.connect(DB_PATH) as db:
        # 清理推送历史
        cursor = await db.execute(
            "DELETE FROM push_history WHERE pushed_at < ?", (cutoff_str,)
        )
        push_deleted = cursor.rowcount
        
        # 清理作品缓存
        cursor = await db.execute(
            "DELETE FROM illust_cache WHERE created_at < ?", (cutoff_str,)
        )
        cache_deleted = cursor.rowcount
        
        # 清理收藏同步记录
        cursor = await db.execute(
            "DELETE FROM xp_bookmarks WHERE scanned_at < ?", (cutoff_str,)
        )
        bookmarks_deleted = cursor.rowcount
        
        await db.commit()
        
        # Vacuum 数据库释放空间
        await db.execute("VACUUM")
        
        logger.info(
            f"🧹 数据库清理完成: 删除 {push_deleted} 条推送历史, "
            f"{cache_deleted} 条缓存, {bookmarks_deleted} 条收藏记录 "
            f"(保留最近 {days} 天)"
        )

async def get_accepted_tag_aliases(kind: str = "equivalent") -> dict[str, str]:
    """Return the only mappings allowed to affect Normalized Tag identity."""
    if kind not in {"equivalent", "search"}:
        raise ValueError(f"不支持的 Tag Alias 类型: {kind}")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT original_tag, normalized_tag FROM tag_aliases WHERE kind = ?",
            (kind,),
        )
        return {row[0]: row[1] for row in await cursor.fetchall()}


async def list_tag_aliases(limit: int = 500) -> list[dict]:
    """List active aliases for review and correction."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT original_tag, normalized_tag, kind, source, priority, updated_at
            FROM tag_aliases
            ORDER BY updated_at DESC, original_tag ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def delete_tag_alias(original_tag: str) -> bool:
    """Remove one active alias without deleting its review history."""
    original = normalize_tag(original_tag)
    if not original:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM tag_aliases WHERE original_tag = ?",
            (original,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def save_tag_mapping_candidates(candidates) -> int:
    """Persist untrusted proposals without activating any alias."""
    rows = []
    for candidate in candidates:
        read = candidate.get if isinstance(candidate, dict) else lambda key, default=None: getattr(candidate, key, default)
        original = normalize_tag(str(read("original_tag") or ""))
        target = normalize_tag(str(read("proposed_normalized_tag") or ""))
        kind = str(read("kind", "equivalent") or "equivalent")
        source = str(read("source", "ai_candidate") or "ai_candidate")
        explanation = str(read("explanation", "") or "")
        similarity = read("embedding_similarity")
        if similarity is not None:
            try:
                similarity = float(similarity)
            except (TypeError, ValueError) as exc:
                raise ValueError("embedding_similarity 必须是 -1 到 1 的数字") from exc
            if not -1.0 <= similarity <= 1.0:
                raise ValueError("embedding_similarity 必须是 -1 到 1 的数字")
        if not original or not target or original == target or kind not in {"equivalent", "search"}:
            continue
        rows.append((original, target, kind, source, explanation, similarity))
    if not rows:
        return 0

    inserted = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for row in rows:
            cursor = await db.execute(
                """
                SELECT id FROM tag_mapping_candidates
                WHERE original_tag = ? AND proposed_normalized_tag = ?
                  AND kind = ? AND source = ?
                LIMIT 1
                """,
                row[:4],
            )
            existing = await cursor.fetchone()
            if existing:
                if row[5] is not None:
                    await db.execute(
                        """
                        UPDATE tag_mapping_candidates
                        SET embedding_similarity = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (row[5], existing[0]),
                    )
                continue
            await db.execute(
                """
                INSERT INTO tag_mapping_candidates (
                    original_tag, proposed_normalized_tag, kind, source, explanation,
                    embedding_similarity
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            inserted += 1
        await db.commit()
    return inserted


async def get_tag_mapping_candidates(limit: int = 100, status: str = "pending") -> list[dict]:
    """Return mapping proposals in human review order."""
    if status not in {"pending", "accepted", "rejected"}:
        raise ValueError(f"不支持的候选状态: {status}")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT c.*,
                   COALESCE(po.weight, 0) AS original_weight,
                   COALESCE(pt.weight, 0) AS target_weight,
                   MAX(COALESCE(po.weight, 0), COALESCE(pt.weight, 0)) AS profile_weight,
                   co.classification AS original_classification,
                   ct.classification AS target_classification,
                   ro.explanation AS original_explanation,
                   ro.languages AS original_language,
                   rt.explanation AS target_explanation,
                   rt.languages AS target_language,
                   tro.translated_name AS original_translation,
                   trt.translated_name AS target_translation,
                   recommendation.id AS ai_recommendation_id,
                   recommendation.relation AS ai_relation,
                   recommendation.confidence AS ai_confidence,
                   recommendation.rationale AS ai_rationale,
                   recommendation.canonical_tag AS ai_canonical_tag,
                   recommendation.risk_flags AS ai_risk_flags,
                   recommendation.principle_checks AS ai_principle_checks,
                   recommendation.model AS ai_model,
                   recommendation.principles_version AS ai_principles_version,
                   recommendation.evidence_hash AS ai_evidence_hash,
                   recommendation.staged_decision AS ai_staged_decision,
                   recommendation.staged_at AS ai_staged_at
            FROM tag_mapping_candidates c
            LEFT JOIN xp_profile po ON po.tag = c.original_tag
            LEFT JOIN xp_profile pt ON pt.tag = c.proposed_normalized_tag
            LEFT JOIN tag_classification_cache co ON co.normalized_tag = c.original_tag
            LEFT JOIN tag_classification_cache ct ON ct.normalized_tag = c.proposed_normalized_tag
            LEFT JOIN ai_tag_classification_records ro ON ro.tag = c.original_tag
            LEFT JOIN ai_tag_classification_records rt ON rt.tag = c.proposed_normalized_tag
            LEFT JOIN tag_translations tro ON tro.name = c.original_tag
            LEFT JOIN tag_translations trt ON trt.name = c.proposed_normalized_tag
            LEFT JOIN tag_mapping_ai_recommendations recommendation
                ON recommendation.id = (
                    SELECT latest.id
                    FROM tag_mapping_ai_recommendations latest
                    WHERE latest.candidate_id = c.id
                    ORDER BY latest.id DESC
                    LIMIT 1
                )
            WHERE c.status = ?
            ORDER BY profile_weight DESC, c.occurrence_count DESC, c.id ASC
            LIMIT ?
            """,
            (status, limit),
        )
        items = [dict(row) for row in await cursor.fetchall()]
        for item in items:
            item["ai_is_current"] = bool(
                item.get("ai_recommendation_id")
                and item.get("ai_principles_version") == MERGE_PRINCIPLES_VERSION
                and item.get("ai_evidence_hash") == relationship_evidence_hash(item)
            )
        return items


async def save_tag_mapping_ai_recommendation(
    candidate_id: int,
    recommendation,
    *,
    model: str,
    principles_version: str,
    evidence,
) -> int:
    """Append advisory AI evidence without changing candidate or alias state."""

    payload = dict(recommendation)
    evidence_payload = dict(evidence)
    evidence_hash = hash_relationship_evidence(evidence_payload)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM tag_mapping_candidates WHERE id = ? AND status = 'pending'",
            (int(candidate_id),),
        )
        if not await cursor.fetchone():
            raise ValueError("Tag Mapping Candidate 不存在或已经审核")
        cursor = await db.execute(
            """
            INSERT INTO tag_mapping_ai_recommendations (
                candidate_id, relation, confidence, rationale, canonical_tag,
                risk_flags, principle_checks, model, principles_version,
                evidence_hash, evidence_payload, recommendation_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(candidate_id), payload["relation"], float(payload["confidence"]),
                payload["rationale"], payload.get("canonical_tag"),
                json.dumps(payload.get("risk_flags") or [], ensure_ascii=False),
                json.dumps(payload.get("principle_checks") or {}, ensure_ascii=False, sort_keys=True),
                model, principles_version, evidence_hash,
                json.dumps(evidence_payload, ensure_ascii=False, sort_keys=True),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def stage_tag_mapping_ai_recommendations(decisions) -> int:
    """Shortlist current recommendations without reviewing candidates or creating aliases."""

    allowed = {"accept_equivalent", "reject"}
    rows = []
    for decision in decisions:
        read = decision.get if isinstance(decision, dict) else lambda key: getattr(decision, key)
        staged_decision = str(read("decision"))
        if staged_decision not in allowed:
            raise ValueError("AI Recommendation 暂存决定无效")
        rows.append((int(read("candidate_id")), int(read("recommendation_id")), staged_decision))
    if not rows:
        return 0

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            for candidate_id, recommendation_id, staged_decision in rows:
                cursor = await db.execute(
                    """
                    UPDATE tag_mapping_ai_recommendations AS recommendation
                    SET staged_decision = ?, staged_at = CURRENT_TIMESTAMP
                    WHERE recommendation.id = ?
                      AND recommendation.candidate_id = ?
                      AND recommendation.id = (
                          SELECT MAX(latest.id)
                          FROM tag_mapping_ai_recommendations latest
                          WHERE latest.candidate_id = recommendation.candidate_id
                      )
                      AND EXISTS (
                          SELECT 1 FROM tag_mapping_candidates candidate
                          WHERE candidate.id = recommendation.candidate_id
                            AND candidate.status = 'pending'
                      )
                    """,
                    (staged_decision, recommendation_id, candidate_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("AI Recommendation 已过期或候选已经审核")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return len(rows)


async def review_tag_mapping_candidate(candidate_id: int, decision: str, kind: str | None = None) -> dict:
    """Apply one human decision; only acceptance creates a runtime Tag Alias."""
    if decision not in {"accept", "reject"}:
        raise ValueError("decision 必须是 accept 或 reject")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT * FROM tag_mapping_candidates WHERE id = ? AND status = 'pending'",
            (candidate_id,),
        )
        row = await cursor.fetchone()
        if not row:
            await db.rollback()
            raise ValueError("映射候选不存在或已经审核")

        applied_kind = kind or row["kind"]
        if applied_kind not in {"equivalent", "search"}:
            await db.rollback()
            raise ValueError("Tag Alias 类型必须是 equivalent 或 search")
        original = normalize_tag(row["original_tag"])
        target = normalize_tag(row["proposed_normalized_tag"] or "")

        if decision == "accept":
            if not original or not target or original == target:
                await db.rollback()
                raise ValueError("该候选没有可接受的目标 Normalized Tag")
            if applied_kind == "equivalent":
                aliases_cursor = await db.execute(
                    "SELECT original_tag, normalized_tag FROM tag_aliases WHERE kind = 'equivalent'"
                )
                aliases = {item[0]: item[1] for item in await aliases_cursor.fetchall()}
                if would_create_alias_cycle(aliases, original, target):
                    await db.rollback()
                    raise ValueError("Tag Alias 不能形成循环")
            await db.execute(
                """
                INSERT INTO tag_aliases (
                    original_tag, normalized_tag, kind, source, priority, updated_at
                ) VALUES (?, ?, ?, 'manual', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(original_tag) DO UPDATE SET
                    normalized_tag = excluded.normalized_tag,
                    kind = excluded.kind,
                    source = 'manual',
                    priority = excluded.priority,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (original, target, applied_kind, int(row["occurrence_count"] or 1)),
            )
            new_status = "accepted"
        else:
            new_status = "rejected"

        await db.execute(
            "UPDATE tag_mapping_candidates SET status = ?, kind = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_status, applied_kind, candidate_id),
        )
        await db.commit()
        return {
            "id": candidate_id,
            "status": new_status,
            "original_tag": original,
            "normalized_tag": target or None,
            "kind": applied_kind,
        }


async def get_tag_mapping_candidate_inputs(limit: int = 100) -> list[str]:
    """Select impactful tags which have neither an alias nor a pending proposal."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT p.tag
            FROM xp_profile p
            LEFT JOIN tag_aliases a ON a.original_tag = p.tag
            WHERE a.original_tag IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM tag_mapping_candidates c
                  WHERE c.original_tag = p.tag AND c.status = 'pending'
              )
            ORDER BY p.weight DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [row[0] for row in await cursor.fetchall()]


async def get_tag_classifications(
    normalized_tags: list[str],
    ttl_days: int = 30,
) -> dict[str, dict[str, str]]:
    """批量获取未过期的 Tag 分类缓存。"""
    if not normalized_tags:
        return {}

    unique_tags = list(dict.fromkeys(normalized_tags))
    cutoff = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" * len(unique_tags))
        cursor = await db.execute(
            f"""
            SELECT normalized_tag, classification, source
            FROM tag_classification_cache
            WHERE normalized_tag IN ({placeholders})
              AND (source = 'manual' OR updated_at >= ?)
            """,
            [*unique_tags, cutoff],
        )
        rows = await cursor.fetchall()
        return {
            row[0]: {"classification": normalize_tag_category(row[1]), "source": row[2]}
            for row in rows
        }


async def save_tag_classifications(items: list[tuple[str, str, str]]):
    """批量写入 Tag 分类缓存。"""
    if not items:
        return

    normalized_items = [
        (tag, normalize_tag_category(classification), source)
        for tag, classification, source in items
    ]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """
            INSERT INTO tag_classification_cache (
                normalized_tag, classification, source, updated_at
            ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(normalized_tag) DO UPDATE SET
                classification = excluded.classification,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            normalized_items,
        )
        await db.commit()


def _parse_evidence_timestamp(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def is_tag_evidence_fresh(item: dict, now: datetime | None = None) -> bool:
    """Manual evidence is permanent; machine evidence is fresh for sixty days."""
    if item.get("source") == "manual":
        return True
    verified_at = _parse_evidence_timestamp(item.get("verified_at"))
    return bool(verified_at and verified_at >= (now or datetime.now()) - timedelta(days=TAG_EVIDENCE_FRESHNESS_DAYS))


def _tag_evidence_item(
    source: str, classification: str, confidence: float, *,
    observed_at=None, verified_at=None, include_provenance: bool = False,
) -> dict:
    item = {
        "source": source,
        "classification": normalize_tag_category(classification),
        "confidence": float(confidence),
    }
    if include_provenance:
        item["observed_at"] = _parse_evidence_timestamp(observed_at)
        item["verified_at"] = _parse_evidence_timestamp(verified_at)
    return item


async def get_tag_evidence(
    normalized_tags: list[str], *, include_provenance: bool = False
) -> dict[str, list[dict]]:
    """Return evidence grouped by tag for maintenance and the future review UI."""
    if not normalized_tags:
        return {}
    unique_tags = list(dict.fromkeys(normalized_tags))
    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" * len(unique_tags))
        columns = "normalized_tag, source, classification, confidence"
        if include_provenance:
            columns += ", observed_at, verified_at"
        cursor = await db.execute(
            f"SELECT {columns} FROM tag_classification_evidence WHERE normalized_tag IN ({placeholders})",
            unique_tags,
        )
        result: dict[str, list[dict]] = {}
        for row in await cursor.fetchall():
            tag, source, classification, confidence = row[:4]
            item = _tag_evidence_item(
                source, classification, confidence,
                observed_at=row[4] if include_provenance else None,
                verified_at=row[5] if include_provenance else None,
                include_provenance=include_provenance,
            )
            result.setdefault(tag, []).append(item)
        return result


async def save_tag_evidence(items: list[tuple[str, str, str, float]]):
    """Upsert one current vote per tag/source without discarding other sources."""
    if not items:
        return
    rows = [(tag, source, normalize_tag_category(category), float(confidence)) for tag, source, category, confidence in items]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """
            INSERT INTO tag_classification_evidence
                (normalized_tag, source, classification, confidence, observed_at, verified_at, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(normalized_tag, source) DO UPDATE SET
                classification = excluded.classification, confidence = excluded.confidence,
                verified_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            """,
            rows,
        )
        await db.commit()


async def get_tag_review_queue(limit: int = 100) -> list[dict]:
    """List unresolved profile tags, highest preference impact first."""
    limit = max(1, int(limit))
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            WITH unresolved AS (
                SELECT cache.normalized_tag, cache.source, cache.updated_at,
                       COALESCE(profile.weight, 0) AS profile_weight
                FROM tag_classification_cache AS cache
                LEFT JOIN xp_profile AS profile ON profile.tag = cache.normalized_tag
                WHERE cache.classification = ?
                ORDER BY ABS(COALESCE(profile.weight, 0)) DESC, cache.updated_at ASC
                LIMIT ?
            )
            SELECT unresolved.normalized_tag, unresolved.source, unresolved.updated_at,
                   unresolved.profile_weight, evidence.source, evidence.classification,
                   evidence.confidence, evidence.observed_at, evidence.verified_at
            FROM unresolved
            LEFT JOIN tag_classification_evidence AS evidence
                ON evidence.normalized_tag = unresolved.normalized_tag
            ORDER BY ABS(unresolved.profile_weight) DESC, unresolved.updated_at ASC, evidence.source ASC
            """,
            (TAG_CATEGORY_UNRESOLVED, limit),
        )
        queue: dict[str, dict] = {}
        for (
            tag, source, updated_at, weight, evidence_source, evidence_category,
            confidence, observed_at, verified_at,
        ) in await cursor.fetchall():
            item = queue.setdefault(tag, {
                "tag": tag,
                "profile_weight": float(weight),
                "classification_source": source,
                "updated_at": updated_at,
                "evidence": [],
            })
            if evidence_source:
                evidence_item = _tag_evidence_item(
                    evidence_source, evidence_category, confidence,
                    observed_at=observed_at, verified_at=verified_at, include_provenance=True,
                )
                evidence_item["is_fresh"] = is_tag_evidence_fresh(evidence_item)
                item["evidence"].append(evidence_item)
        return list(queue.values())


async def get_high_weight_unclassified_profile_tags(
    limit: int = 40,
    min_profile_weight: float = 0.0,
) -> list[dict]:
    """List high-impact profile tags that still need Grounded Judge classification."""
    limit = max(1, int(limit))
    minimum = abs(float(min_profile_weight))
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT xp.tag, xp.weight, classification.classification
            FROM xp_profile AS xp
            LEFT JOIN tag_classification_cache AS classification
                ON classification.normalized_tag = xp.tag
            WHERE ABS(xp.weight) >= ?
              AND (classification.normalized_tag IS NULL OR classification.classification = ?)
            ORDER BY ABS(xp.weight) DESC, xp.tag ASC
            LIMIT ?
            """,
            (minimum, TAG_CATEGORY_UNRESOLVED, limit),
        )
        return [
            {"tag": tag, "profile_weight": float(weight), "classification": classification}
            for tag, weight, classification in await cursor.fetchall()
        ]


async def get_tag_review_count() -> int:
    """Return the exact number of tags still awaiting a human decision."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM tag_classification_cache WHERE classification = ?",
            (TAG_CATEGORY_UNRESOLVED,),
        )
        row = await cursor.fetchone()
    return int(row[0] if row else 0)


async def review_tag_classification(normalized_tag: str, classification: str) -> None:
    """Accept a human Tag Category decision and remove the tag from review."""
    normalized_tag = normalize_tag(normalized_tag)
    if not normalized_tag:
        raise ValueError("人工审核必须提供有效标签")
    category = normalize_tag_category(classification)
    if category == TAG_CATEGORY_UNRESOLVED:
        raise ValueError("人工审核必须选择一个已解析的 Tag Category")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO tag_classification_evidence (normalized_tag, source, classification, confidence, updated_at)
            VALUES (?, 'manual', ?, 1.0, CURRENT_TIMESTAMP)
            ON CONFLICT(normalized_tag, source) DO UPDATE SET
                classification = excluded.classification, confidence = excluded.confidence,
                updated_at = CURRENT_TIMESTAMP
            """,
            (normalized_tag, category),
        )
        await db.execute(
            """
            INSERT INTO tag_classification_cache (normalized_tag, classification, source, updated_at)
            VALUES (?, ?, 'manual', CURRENT_TIMESTAMP)
            ON CONFLICT(normalized_tag) DO UPDATE SET
                classification = excluded.classification, source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            (normalized_tag, category),
        )
        await db.commit()


async def activate_ai_tag_classification(tag: str, classification: str, explanation: str, languages: str) -> bool:
    """Persist a complete AI Classification Record unless a human owns the tag."""
    tag = normalize_tag(tag)
    category = normalize_tag_category(classification)
    if not tag or category == TAG_CATEGORY_UNRESOLVED or not explanation or not languages:
        raise ValueError("AI Classification Record 无效")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT classification, source FROM tag_classification_cache WHERE normalized_tag = ?", (tag,)
        )
        current = await cursor.fetchone()
        if current and current[1] == "manual":
            return False
        if not current:
            await db.execute(
                "INSERT INTO tag_classification_cache (normalized_tag, classification, source, updated_at) VALUES (?, ?, 'ai', CURRENT_TIMESTAMP)",
                (tag, TAG_CATEGORY_UNRESOLVED),
            )
        await db.execute(
            """INSERT INTO ai_tag_classification_records (tag, classification, explanation, languages, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(tag) DO UPDATE SET classification=excluded.classification,
               explanation=excluded.explanation, languages=excluded.languages, updated_at=CURRENT_TIMESTAMP""",
            (tag, category, explanation, languages),
        )
        await db.execute(
            "UPDATE tag_classification_cache SET classification = ?, source = 'ai', updated_at = CURRENT_TIMESTAMP WHERE normalized_tag = ?",
            (category, tag),
        )
        await db.commit()
        return True


async def mark_ai_tag_unresolved(tag: str) -> bool:
    """Keep a failed Grounded Judge result in the human review queue."""
    tag = normalize_tag(tag)
    if not tag:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT source FROM tag_classification_cache WHERE normalized_tag = ?", (tag,)
        )
        current = await cursor.fetchone()
        if current and current[0] == "manual":
            return False
        await db.execute(
            """INSERT INTO tag_classification_cache (normalized_tag, classification, source, updated_at)
               VALUES (?, ?, 'ai', CURRENT_TIMESTAMP)
               ON CONFLICT(normalized_tag) DO UPDATE SET classification=excluded.classification,
               source=excluded.source, updated_at=CURRENT_TIMESTAMP""",
            (tag, TAG_CATEGORY_UNRESOLVED),
        )
        await db.commit()
    return True


async def get_ai_tag_classification_record(tag: str) -> dict | None:
    tag = normalize_tag(tag)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT tag, classification, explanation, languages FROM ai_tag_classification_records WHERE tag = ?", (tag,)
        )
        row = await cursor.fetchone()
    return dict(zip(("tag", "classification", "explanation", "languages"), row)) if row else None


async def review_tag_classifications_batch(items: list[tuple[str, str]]) -> list[str]:
    """Apply manual decisions atomically, returning unresolved tags that became stale.

    Callers must validate the CSV format before calling this function.  A stale
    tag is one that is no longer in the unresolved review queue; in that case
    the whole batch is left untouched so an exported spreadsheet cannot
    silently overwrite a newer decision.
    """
    if not items:
        return []

    normalized_items: list[tuple[str, str]] = []
    for tag, classification in items:
        normalized_tag = normalize_tag(tag)
        category = normalize_tag_category(classification)
        if not normalized_tag:
            raise ValueError("人工审核必须提供有效标签")
        if category == TAG_CATEGORY_UNRESOLVED:
            raise ValueError("人工审核必须选择一个已解析的 Tag Category")
        normalized_items.append((normalized_tag, category))

    tags = [tag for tag, _ in normalized_items]
    placeholders = ",".join("?" * len(tags))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            f"""
            SELECT normalized_tag FROM tag_classification_cache
            WHERE normalized_tag IN ({placeholders}) AND classification = ?
            """,
            [*tags, TAG_CATEGORY_UNRESOLVED],
        )
        unresolved_tags = {row[0] for row in await cursor.fetchall()}
        await cursor.close()
        stale_tags = [tag for tag in tags if tag not in unresolved_tags]
        if stale_tags:
            await db.rollback()
            return stale_tags

        await db.executemany(
            """
            INSERT INTO tag_classification_evidence (normalized_tag, source, classification, confidence, updated_at)
            VALUES (?, 'manual', ?, 1.0, CURRENT_TIMESTAMP)
            ON CONFLICT(normalized_tag, source) DO UPDATE SET
                classification = excluded.classification, confidence = excluded.confidence,
                updated_at = CURRENT_TIMESTAMP
            """,
            normalized_items,
        )
        await db.executemany(
            """
            INSERT INTO tag_classification_cache (normalized_tag, classification, source, updated_at)
            VALUES (?, ?, 'manual', CURRENT_TIMESTAMP)
            ON CONFLICT(normalized_tag) DO UPDATE SET
                classification = excluded.classification, source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            normalized_items,
        )
        await db.commit()
    return []


async def get_best_search_tag(normalized_tag: str) -> str:
    """
    获取某 Normalized Tag 对应的已审核搜索词。
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT original_tag FROM tag_aliases
            WHERE normalized_tag = ?
            ORDER BY CASE kind WHEN 'search' THEN 0 ELSE 1 END, priority DESC, updated_at DESC
            LIMIT 1
        """, (normalized_tag,))
        row = await cursor.fetchone()
        if row:
            return row[0]
        return normalized_tag

async def get_db():
    """获取数据库连接"""
    return await aiosqlite.connect(DB_PATH)


# ============ 推送历史 ============
async def is_pushed(illust_id: int) -> bool:
    """检查作品是否已推送"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM push_history WHERE illust_id = ?", (illust_id,)
        )
        return await cursor.fetchone() is not None


async def get_pushed_ids_batch(illust_ids: list[int]) -> set[int]:
    """
    批量查询已推送的作品 ID 集合 (性能优化)
    
    将 O(n) 次数据库查询优化为 O(1) 次查询
    """
    if not illust_ids:
        return set()
    
    async with aiosqlite.connect(DB_PATH) as db:
        # 使用 IN 查询批量获取
        # 安全说明: placeholders 只包含 "?" 字符，不包含用户输入
        # illust_ids 通过参数化查询传递，无 SQL 注入风险
        if not illust_ids:
            return set()
        # 限制批量查询数量，防止内存问题
        if len(illust_ids) > 10000:
            illust_ids = illust_ids[:10000]
        placeholders = ",".join("?" * len(illust_ids))
        cursor = await db.execute(
            f"SELECT illust_id FROM push_history WHERE illust_id IN ({placeholders})",
            illust_ids
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def mark_pushed(illust_id: int, source: str):
    """记录推送"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO push_history (illust_id, source) VALUES (?, ?)",
            (illust_id, source)
        )
        await db.commit()


async def get_last_push_at() -> Optional[datetime]:
    """获取最近一次成功推送时间。"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT MAX(pushed_at) FROM push_history")
        row = await cursor.fetchone()

    value = row[0] if row else None
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning("无法解析最近推送时间: %s", value)
        return None

async def get_push_source(illust_id: int) -> Optional[str]:
    """获取推送来源"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT source FROM push_history WHERE illust_id = ?", (illust_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def get_push_history_paginated(
    limit: int = 25,
    offset: int = 0,
    favorites_only: bool = False
) -> tuple[list[dict], int]:
    """
    获取分页的推送历史
    
    Returns:
        (items, total): items 是包含 illust_id 和 pushed_at 的字典列表，total 是总数
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        if favorites_only:
            count_sql = """
                SELECT COUNT(*)
                FROM push_history ph
                INNER JOIN feedback fb ON fb.illust_id = ph.illust_id
                WHERE fb.action = 'like'
            """
            data_sql = """
                SELECT ph.illust_id, ph.pushed_at, ph.source
                FROM push_history ph
                INNER JOIN feedback fb ON fb.illust_id = ph.illust_id
                WHERE fb.action = 'like'
                ORDER BY ph.pushed_at DESC
                LIMIT ? OFFSET ?
            """
        else:
            count_sql = "SELECT COUNT(*) FROM push_history"
            data_sql = """
                SELECT illust_id, pushed_at, source
                FROM push_history
                ORDER BY pushed_at DESC
                LIMIT ? OFFSET ?
            """

        # 获取总数
        cursor = await db.execute(count_sql)
        total = (await cursor.fetchone())[0]

        # 获取分页数据
        cursor = await db.execute(data_sql, (limit, offset))
        rows = await cursor.fetchall()
        
        items = [{"illust_id": row["illust_id"], "pushed_at": row["pushed_at"], "source": row["source"]} for row in rows]
        
        return items, total


# ============ XP画像 ============
async def get_xp_profile() -> dict[str, float]:
    """获取XP画像"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT tag, weight FROM xp_profile ORDER BY weight DESC")
        rows = await cursor.fetchall()
        return {tag: weight for tag, weight in rows}


async def update_xp_profile(profile: dict[str, float]):
    """更新XP画像"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM xp_profile")
        await db.executemany(
            "INSERT INTO xp_profile (tag, weight, updated_at) VALUES (?, ?, ?)",
            [(tag, weight, datetime.now()) for tag, weight in profile.items()]
        )
        await db.commit()


async def adjust_tag_weight(tag: str, delta: float):
    """调整Tag权重"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO xp_profile (tag, weight, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(tag) DO UPDATE SET 
                weight = weight + excluded.weight,
                updated_at = excluded.updated_at
        """, (tag, delta, datetime.now()))
        await db.commit()


async def update_xp_tag_pairs(pairs: list[tuple[str, str, float]]):
    """更新Tag组合权重"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM xp_tag_pairs")
        await db.executemany(
            "INSERT INTO xp_tag_pairs (tag1, tag2, weight) VALUES (?, ?, ?)",
            pairs
        )
        await db.commit()


async def get_top_tag_pairs(limit: int = 20) -> list[tuple[str, str, float]]:
    """获取热门Tag组合"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT tag1, tag2, weight FROM xp_tag_pairs ORDER BY weight DESC LIMIT ?",
            (limit,)
        )
        return await cursor.fetchall()


# ============ 反馈 ============
async def record_feedback(illust_id: int, action: str):
    """记录反馈"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO feedback (illust_id, action, created_at) VALUES (?, ?, ?)",
            (illust_id, action, datetime.now())
        )
        await db.commit()


async def get_recent_liked_tags(limit: int = 10) -> list[str]:
    """
    获取近期喜欢的作品的标签 (用于 AI 评分)
    
    从 feedback 关联 illust_cache 获取标签
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT c.tags FROM feedback f
            JOIN illust_cache c ON f.illust_id = c.illust_id
            WHERE f.action = 'like'
            ORDER BY f.created_at DESC
            LIMIT ?
        """, (limit,))
        rows = await cursor.fetchall()
        
        # 收集所有标签
        all_tags = []
        for row in rows:
            try:
                tags = json.loads(row[0])
                all_tags.extend(tags[:5])  # 每个作品取前 5 个标签
            except:
                pass
        return all_tags[:limit * 3]  # 返回适量标签


async def get_recent_disliked_tags(limit: int = 10) -> list[str]:
    """
    获取近期不喜欢的作品的标签 (用于 AI 评分)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT c.tags FROM feedback f
            JOIN illust_cache c ON f.illust_id = c.illust_id
            WHERE f.action = 'dislike'
            ORDER BY f.created_at DESC
            LIMIT ?
        """, (limit,))
        rows = await cursor.fetchall()
        
        all_tags = []
        for row in rows:
            try:
                tags = json.loads(row[0])
                all_tags.extend(tags[:5])
            except:
                pass
        return all_tags[:limit * 3]


async def get_liked_illusts() -> set[int]:
    """获取所有被点赞的作品ID"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT illust_id FROM feedback WHERE action = 'like'"
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def get_recent_liked_illusts_for_tag(tag: str, limit: int = 3) -> list[int]:
    """Return the most recently liked cached works containing a profile tag.

    Tags are stored as JSON, so matching is deliberately performed after loading
    a bounded recent result set rather than relying on SQLite JSON extensions.
    """
    normalized_tag = normalize_tag(tag)
    if not normalized_tag or limit < 1:
        return []

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT f.illust_id, c.tags
            FROM feedback AS f
            INNER JOIN illust_cache AS c ON c.illust_id = f.illust_id
            WHERE f.action = 'like'
            ORDER BY f.created_at DESC
            """
        )
        result: list[int] = []
        while rows := await cursor.fetchmany(200):
            for illust_id, tags_json in rows:
                try:
                    tags = json.loads(tags_json) if tags_json else []
                except (TypeError, json.JSONDecodeError):
                    continue
                if any(normalize_tag(str(item)) == normalized_tag for item in tags):
                    result.append(illust_id)
                    if len(result) >= limit:
                        return result
        return result


async def get_recent_liked_illusts_for_artist(artist_id: int, limit: int = 3) -> list[int]:
    """Return the most recently liked cached works by one artist."""
    if artist_id < 1 or limit < 1:
        return []

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT f.illust_id
            FROM feedback AS f
            INNER JOIN illust_cache AS c ON c.illust_id = f.illust_id
            WHERE f.action = 'like' AND c.user_id = ?
            ORDER BY f.created_at DESC
            LIMIT ?
            """,
            (artist_id, limit),
        )
        return [row[0] for row in await cursor.fetchall()]


async def increment_tag_dislike(tag: str) -> int:
    """增加Tag否认计数，返回当前计数"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO tag_blacklist (tag, dislike_count) VALUES (?, 1)
            ON CONFLICT(tag) DO UPDATE SET dislike_count = dislike_count + 1
        """, (tag,))
        await db.commit()
        cursor = await db.execute(
            "SELECT dislike_count FROM tag_blacklist WHERE tag = ?", (tag,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_blacklisted_tags() -> set[str]:
    """获取所有黑名单Tag"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT tag FROM tag_blacklist WHERE dislike_count >= 1"
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


# ============ 收藏同步 ============
async def get_scanned_bookmarks() -> set[int]:
    """获取已扫描的收藏ID"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT illust_id FROM bookmarks")
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def mark_bookmark_scanned(illust_id: int):
    """标记收藏已扫描"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO bookmarks (illust_id) VALUES (?)", (illust_id,)
        )
        await db.commit()


# ============ 作品缓存 ============

async def cache_illust(
    illust_id: int, 
    tags: list[str], 
    user_id: int = 0, 
    user_name: str = "",
    source: str = "xp_search",  # 新增：作品来源策略
    chain_depth: int = 0,
    chain_parent_id: int = None,
    chain_msg_id: int = None
):
    """缓存作品信息 (v4: 包含来源归因 + 连锁元数据)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO illust_cache 
               (illust_id, tags, user_id, user_name, source, chain_depth, chain_parent_id, chain_msg_id, created_at) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (illust_id, json.dumps(tags), user_id, user_name, source, chain_depth, chain_parent_id, chain_msg_id, datetime.now())
        )
        await db.commit()


async def get_push_source_from_cache(illust_id: int) -> str | None:
    """从缓存获取作品的推送来源策略 (fallback 用)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT source FROM illust_cache WHERE illust_id = ?",
            (illust_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_cached_illust_tags(illust_id: int) -> list[str] | None:
    """获取缓存的作品tags (兼容旧接口)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT tags FROM illust_cache WHERE illust_id = ?", (illust_id,)
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return None


        return None


async def get_cached_illust(illust_id: int) -> dict | None:
    """获取缓存的完整作品信息 (用于反馈处理, v3 含连锁信息)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT illust_id, tags, user_id, user_name, 
                      chain_depth, chain_parent_id, chain_msg_id 
               FROM illust_cache WHERE illust_id = ?""", 
            (illust_id,)
        )
        row = await cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "tags": json.loads(row[1]) if row[1] else [],
                "user_id": row[2] or 0,
                "user_name": row[3] or "",
                "chain_depth": row[4] or 0,
                "chain_parent_id": row[5],
                "chain_msg_id": row[6]
            }
        return None


async def set_chain_meta(illust_id: int, chain_depth: int, chain_parent_id: int = None, chain_msg_id: int = None):
    """设置作品的连锁元数据 (用于已缓存的作品)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE illust_cache 
               SET chain_depth = ?, chain_parent_id = ?, chain_msg_id = ?
               WHERE illust_id = ?""",
            (chain_depth, chain_parent_id, chain_msg_id, illust_id)
        )
        await db.commit()


async def get_chain_meta(illust_id: int) -> tuple[int, int | None, int | None]:
    """获取作品的连锁元数据
    Returns: (chain_depth, chain_parent_id, chain_msg_id)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT chain_depth, chain_parent_id, chain_msg_id FROM illust_cache WHERE illust_id = ?",
            (illust_id,)
        )
        row = await cursor.fetchone()
        if row:
            return (row[0] or 0, row[1], row[2])
        return (0, None, None)


async def delete_cached_illust(illust_id: int):
    """从缓存中删除作品信息"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM illust_cache WHERE illust_id = ?", (illust_id,)
        )
        await db.commit()


async def cleanup_old_illust_cache(days: int = 30) -> int:
    """清理 N 天前的旧缓存记录"""
    cutoff = datetime.now() - timedelta(days=days)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM illust_cache WHERE created_at < ?", (cutoff,)
        )
        await db.commit()
        return cursor.rowcount


# ============ AI 错误处理 ============
async def add_ai_error(tags: list[str], error: str) -> int:
    """记录 AI 错误"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO ai_error_logs (tags_content, error_msg) VALUES (?, ?)",
            (json.dumps(tags), str(error))
        )
        await db.commit()
        return cursor.lastrowid


async def get_ai_error(error_id: int) -> dict | None:
    """获取单条错误记录"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM ai_error_logs WHERE id = ?", (error_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_ai_error_status(error_id: int, status: str):
    """更新错误状态"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ai_error_logs SET status = ? WHERE id = ?",
            (status, error_id)
        )
        await db.commit()


# ============ XP 收藏缓存 ============
async def get_xp_bookmarks(user_id: int) -> list[dict]:
    """获取缓存的XP收藏数据"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM xp_bookmarks WHERE user_id = ?", (user_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

async def save_xp_bookmarks(user_id: int, bookmarks: list):
    """保存收藏数据用于分析"""
    # bookmarks: list of Illust objects or dicts
    data = []
    for b in bookmarks:
        # 兼容 Illust 对象和 dict
        if hasattr(b, 'id'):
             iid = b.id
             tags = json.dumps(b.tags)
             cdate = b.create_date
        else:
             iid = b['id']
             tags = json.dumps(b['tags'])
             cdate = b['create_date']
             
        data.append((iid, user_id, tags, cdate))
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT OR REPLACE INTO xp_bookmarks 
               (illust_id, user_id, tags, illust_create_date) 
               VALUES (?, ?, ?, ?)""",
            data
        )
        await db.commit()


# ============ 系统状态 ============
async def get_state(key: str) -> str | None:
    """获取系统状态值"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM system_state WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None

async def set_state(key: str, value: str):
    """设置系统状态值"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, datetime.now())
        )
        await db.commit()


# ============ 推送统计 ============


async def get_schema_version() -> int:
    """获取当前数据库 schema 版本。"""
    value = await get_state("schema_version")
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


async def get_db_overview() -> dict:
    """返回数据库概览，供维护工具与 Web 使用。"""
    table_names = [
        "push_history", "xp_profile", "xp_bookmarks", "illust_cache", "feedback",
        "strategy_stats", "tag_aliases", "tag_mapping_candidates",
        "tag_mapping_stats", "ai_tag_cache", "system_state"
    ]
    async with aiosqlite.connect(DB_PATH) as db:
        table_counts = {}
        for table in table_names:
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cursor.fetchone()
            table_counts[table] = row[0] if row else 0

    return {
        "path": str(DB_PATH),
        "exists": DB_PATH.exists(),
        "size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        "schema_version": await get_schema_version(),
        "table_counts": table_counts,
    }


async def get_push_stats(days: int = 7) -> dict:
    """
    获取推送统计信息
    
    Args:
        days: 统计天数
    
    Returns:
        {
            "total_pushed": 总推送数,
            "total_feedback": 反馈数,
            "likes": 喜欢数,
            "dislikes": 不喜欢数,
            "top_artists": [(artist_id, count), ...],
            "top_tags": [(tag, count), ...]
        }
    """
    since = datetime.now() - timedelta(days=days)
    
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        # 推送总数
        cursor = await db.execute(
            "SELECT COUNT(*) FROM push_history WHERE pushed_at > ?",
            (since,)
        )
        row = await cursor.fetchone()
        total_pushed = row[0] if row else 0
        
        # 反馈统计
        cursor = await db.execute(
            "SELECT action, COUNT(*) as cnt FROM feedback WHERE created_at > ? GROUP BY action",
            (since,)
        )
        feedback_rows = await cursor.fetchall()
        likes = 0
        dislikes = 0
        for r in feedback_rows:
            if r['action'] == 'like':
                likes = r['cnt']
            elif r['action'] == 'dislike':
                dislikes = r['cnt']
        
        # Top 画师（从缓存表查）
        cursor = await db.execute("""
            SELECT ic.user_id, COUNT(*) as cnt 
            FROM push_history ph
            JOIN illust_cache ic ON ph.illust_id = ic.illust_id
            WHERE ph.pushed_at > ?
            GROUP BY ic.user_id
            ORDER BY cnt DESC
            LIMIT 5
        """, (since,))
        top_artists = [(row['user_id'], row['cnt']) for row in await cursor.fetchall()]
        
        # Top 标签（从缓存表查）
        cursor = await db.execute("""
            SELECT ic.tags FROM push_history ph
            JOIN illust_cache ic ON ph.illust_id = ic.illust_id
            WHERE ph.pushed_at > ?
        """, (since,))
        rows = await cursor.fetchall()
        
        tag_count = {}
        for row in rows:
            try:
                tags = json.loads(row['tags']) if row['tags'] else []
                for tag in tags[:10]:  # 统计前10个标签
                    tag_count[tag] = tag_count.get(tag, 0) + 1
            except:
                pass
        
        top_tags = sorted(tag_count.items(), key=lambda x: x[1], reverse=True)[:10]
        
        return {
            "total_pushed": total_pushed,
            "total_feedback": likes + dislikes,
            "likes": likes,
            "dislikes": dislikes,
            "top_artists": top_artists,
            "top_tags": top_tags
        }


async def format_stats_report(days: int = 7) -> str:
    """生成格式化的统计报告"""
    stats = await get_push_stats(days)
    
    period = "本周" if days == 7 else f"近{days}天"
    
    # 格式化 Top 画师
    artists_str = ""
    if stats["top_artists"]:
        artists_str = "\n".join(f"  - ID {a[0]}: {a[1]}张" for a in stats["top_artists"][:3])
    else:
        artists_str = "  暂无数据"
    
    # 格式化 Top 标签
    tags_str = ""
    if stats["top_tags"]:
        tags_str = ", ".join(f"#{t[0]}({t[1]})" for t in stats["top_tags"][:5])
    else:
        tags_str = "暂无数据"
    
    return f"""📊 {period}推送统计

📤 推送: {stats['total_pushed']} 张作品
👍 喜欢: {stats['likes']} | 👎 不喜欢: {stats['dislikes']}

🎨 Top 画师:
{artists_str}

🏷️ Top 标签: {tags_str}"""

# ============ 数据清理 ============
async def reset_xp_data():
    """
    重置所有 XP 分析数据（适用于Prompt变更后需要重新清洗的情况）
    将会清除：
    1. XP画像 (xp_profile, xp_tag_pairs)
    2. 运行策略统计
    
    保留：
    1. 推送历史 (push_history)
    2. 用户反馈 (feedback)
    3. 黑名单 (tag_blacklist)
    4. 已审核 Tag Alias、映射候选和隔离的旧映射数据
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # 清除画像数据
        await db.execute("DELETE FROM xp_profile")
        await db.execute("DELETE FROM xp_tag_pairs")
        
        # 清除 AI 错误日志
        await db.execute("DELETE FROM ai_error_logs")
        
        # 清除 MAB 策略统计
        await db.execute("DELETE FROM strategy_stats")
        
        # 注意：不清除 system_state 中的同步进度
        # 这样 Profiler 会跳过 Pixiv API 抓取，直接从 xp_bookmarks 读取缓存进行重分析
        
        await db.commit()


# ============ MAB 策略统计 ============
async def update_strategy_stats(strategy: str, is_success: bool):
    """
    更新策略统计
    success_count += 1 (if success)
    total_count += 1
    """
    success_inc = 1 if is_success else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO strategy_stats (strategy, success_count, total_count)
            VALUES (?, ?, 1)
            ON CONFLICT(strategy) DO UPDATE SET
                success_count = success_count + excluded.success_count,
                total_count = total_count + 1,
                updated_at = CURRENT_TIMESTAMP
        """, (strategy, success_inc))
        await db.commit()

async def get_strategy_stats(strategy: str) -> tuple[int, int]:
    """
    获取策略统计
    Returns: (success_count, total_count)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT success_count, total_count FROM strategy_stats WHERE strategy = ?",
            (strategy,)
        )
        row = await cursor.fetchone()
        if row:
            return row[0], row[1]
        return 0, 0


# ============ 快速屏蔽 (Bot /block) ============
async def block_tag(tag: str):
    """添加标签到屏蔽列表"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO blocked_tags (tag) VALUES (?)",
            (tag.lower().strip(),)
        )
        await db.commit()


async def unblock_tag(tag: str) -> bool:
    """从屏蔽列表移除标签，并重置其厌恶计数"""
    tag = tag.lower().strip()
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. 移除手动屏蔽
        cursor = await db.execute(
            "DELETE FROM blocked_tags WHERE tag = ?",
            (tag,)
        )
        manual_deleted = cursor.rowcount > 0
        
        # 2. 重置厌恶计数 (针对自动屏蔽)
        cursor = await db.execute(
            "UPDATE tag_blacklist SET dislike_count = 0 WHERE tag = ?",
            (tag,)
        )
        stats_updated = cursor.rowcount > 0
        
        await db.commit()
        return manual_deleted or stats_updated


async def get_blocked_tags() -> list[str]:
    """获取所有屏蔽的标签 (手动 + 自动)"""
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. 手动屏蔽
        cursor = await db.execute("SELECT tag FROM blocked_tags")
        rows = await cursor.fetchall()
        manual = {row[0] for row in rows}
        
        # 2. 自动屏蔽 (dislike >= 3)
        # 注意：这里硬编码了 3，最好从 config 传参，但 database 层通常不读 config
        # 或者我们只利用这个函数返回 manual，profiler 自己处理 auto
        # 但为了 /unblock 能查到，我们需要在这里聚合
        # 实际上用户更关心的是"生效的屏蔽"
        # 让我们把阈值作为参数，默认为 3
        return list(manual)

async def get_all_blocked_tags(dislike_threshold: int = 3) -> list[str]:
    """获取所有生效的屏蔽标签 (包括手动和高厌恶)"""
    async with aiosqlite.connect(DB_PATH) as db:
        # 手动
        cursor = await db.execute("SELECT tag FROM blocked_tags")
        manual = {row[0] for row in (await cursor.fetchall())}
        
        # 自动
        cursor = await db.execute(
            "SELECT tag FROM tag_blacklist WHERE dislike_count >= ?",
            (dislike_threshold,)
        )
        auto = {row[0] for row in (await cursor.fetchall())}
        
        return list(manual | auto)


async def is_tag_blocked(tag: str) -> bool:
    """检查标签是否被屏蔽 (仅手动 block)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM blocked_tags WHERE tag = ?",
            (tag.lower().strip(),)
        )
        return await cursor.fetchone() is not None


# ============ 临时静音标签 (/mute) ==========
async def mute_tag(tag: str, hours: int = 24) -> str:
    """静音某个 tag 一段时间，返回到期时间字符串"""
    tag = tag.lower().strip()
    until_dt = datetime.now() + timedelta(hours=hours)
    until_str = until_dt.strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO muted_tags (tag, until_ts) VALUES (?, ?) "
            "ON CONFLICT(tag) DO UPDATE SET until_ts=excluded.until_ts",
            (tag, until_str)
        )
        await db.commit()
    return until_str


async def unmute_tag(tag: str) -> bool:
    """提前撤销静音"""
    tag = tag.lower().strip()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM muted_tags WHERE tag = ?", (tag,))
        await db.commit()
        return cursor.rowcount > 0


async def get_muted_tags(active_only: bool = True) -> list[tuple[str, str]]:
    """获取静音 tag 列表: [(tag, until_ts), ...]"""
    async with aiosqlite.connect(DB_PATH) as db:
        if active_only:
            cursor = await db.execute(
                "SELECT tag, until_ts FROM muted_tags WHERE until_ts > CURRENT_TIMESTAMP ORDER BY until_ts DESC"
            )
        else:
            cursor = await db.execute(
                "SELECT tag, until_ts FROM muted_tags ORDER BY until_ts DESC"
            )
        rows = await cursor.fetchall()
        return [(r[0], r[1]) for r in rows]


async def cleanup_expired_mutes() -> int:
    """清理已过期的静音 tag，返回清理条数"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM muted_tags WHERE until_ts <= CURRENT_TIMESTAMP")
        await db.commit()
        return cursor.rowcount


async def is_tag_muted(tag: str) -> bool:
    """检查 tag 是否处于静音期"""
    tag = tag.lower().strip()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM muted_tags WHERE tag = ? AND until_ts > CURRENT_TIMESTAMP",
            (tag,)
        )
        return await cursor.fetchone() is not None


# ============ 画师屏蔽 (/block_artist) ============
async def block_artist(artist_id: int, artist_name: str = None):
    """添加画师到屏蔽列表"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO blocked_artists (artist_id, artist_name) VALUES (?, ?)",
            (artist_id, artist_name)
        )
        await db.commit()


async def unblock_artist(artist_id: int) -> bool:
    """从屏蔽列表移除画师，返回是否成功移除"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM blocked_artists WHERE artist_id = ?",
            (artist_id,)
        )
        await db.commit()
        return cursor.rowcount > 0

async def update_artist_score(artist_id: int, delta: float):
    """更新画师权重分数 (增量)"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Upsert logic: insert or update
        await db.execute("""
            INSERT INTO artist_profile (artist_id, score, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(artist_id) DO UPDATE SET
                score = score + ?,
                updated_at = CURRENT_TIMESTAMP
        """, (artist_id, delta, delta))
        await db.commit()

async def get_artist_score(artist_id: int) -> float:
    """获取画师权重分数"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT score FROM artist_profile WHERE artist_id = ?", (artist_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0.0


async def get_blocked_artists() -> list[tuple[int, str]]:
    """获取所有屏蔽的画师，返回 [(artist_id, artist_name), ...]"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT artist_id, artist_name FROM blocked_artists")
        rows = await cursor.fetchall()
        return [(row[0], row[1] or str(row[0])) for row in rows]


async def is_artist_blocked(artist_id: int) -> bool:
    """检查画师是否被屏蔽"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM blocked_artists WHERE artist_id = ?",
            (artist_id,)
        )
        return await cursor.fetchone() is not None


# ============ XP 画像查询 (/xp) ============
async def get_top_xp_tags(
    limit: int = 15,
    categories: tuple[str, ...] | None = None,
) -> list[tuple[str, float]]:
    """
    获取权重最高的 Top N 标签；可按标签分类筛选。
    Returns: [(tag, weight), ...]
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if categories is None:
            cursor = await db.execute(
                "SELECT tag, weight FROM xp_profile ORDER BY weight DESC LIMIT ?",
                (limit,),
            )
        else:
            normalized_categories = tuple(
                dict.fromkeys(normalize_tag_category(category) for category in categories)
            )
            if not normalized_categories:
                return []
            placeholders = ",".join("?" * len(normalized_categories))
            cursor = await db.execute(
                f"""
                SELECT xp.tag, xp.weight
                FROM xp_profile AS xp
                INNER JOIN tag_classification_cache AS classification
                    ON classification.normalized_tag = xp.tag
                WHERE classification.classification IN ({placeholders})
                ORDER BY xp.weight DESC
                LIMIT ?
                """,
                (*normalized_categories, limit),
            )
        rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]


async def get_xp_profile_display_sections(
    feature_limit: int = 15,
    identity_limit: int = 5,
) -> dict[str, list[tuple[str, float]]]:
    """Return separately ranked transferable features and identity preferences."""
    feature_tags = await get_top_xp_tags(feature_limit, (TAG_CATEGORY_FEATURE,))
    identity_tags = await get_top_xp_tags(identity_limit, tuple(IDENTITY_TAG_CATEGORIES))
    return {"feature": feature_tags, "identity": identity_tags}


# ============ 互动画师发现 (策略E) ============
async def get_top_engaged_artists(limit: int = 10) -> list[tuple[int, str, int]]:
    """
    获取用户互动最多的画师列表 (用于 Engagement-Based Discovery 策略)
    
    通过 feedback + illust_cache 联表查询，统计各画师被点赞的次数。
    
    Returns: [(artist_id, artist_name, like_count), ...]
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT ic.user_id, ic.user_name, COUNT(*) as like_count
            FROM feedback f
            JOIN illust_cache ic ON f.illust_id = ic.illust_id
            WHERE f.action = 'like' AND ic.user_id IS NOT NULL AND ic.user_id > 0
            GROUP BY ic.user_id
            ORDER BY like_count DESC
            LIMIT ?
        """, (limit,))
        rows = await cursor.fetchall()
        return [(row[0], row[1] or "", row[2]) for row in rows]


async def get_recent_engagement_sequence(limit: int = 50) -> list[tuple[int, str, str]]:
    """
    获取最近的用户互动序列 (用于历史序列建模)
    
    Returns: [(illust_id, action, timestamp), ...]
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT illust_id, action, created_at
            FROM feedback
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))
        rows = await cursor.fetchall()
        return [(row[0], row[1], row[2]) for row in rows]


# ============ Embedding 缓存 ============
async def get_illust_embedding(illust_id: int) -> Optional[list[float]]:
    """获取作品的缓存 Embedding"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT embedding FROM illust_embeddings WHERE illust_id = ?",
            (illust_id,)
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return None


async def save_illust_embedding(illust_id: int, embedding: list[float], model: str):
    """保存作品的 Embedding"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO illust_embeddings (illust_id, embedding, model, created_at)
            VALUES (?, ?, ?, ?)
        """, (illust_id, json.dumps(embedding), model, datetime.now()))
        await db.commit()


async def get_illust_embeddings_batch(illust_ids: list[int]) -> dict[int, list[float]]:
    """批量获取作品的缓存 Embedding"""
    if not illust_ids:
        return {}
    
    async with aiosqlite.connect(DB_PATH) as db:
        # 安全说明: placeholders 只包含 "?" 字符，不包含用户输入
        # illust_ids 通过参数化查询传递，无 SQL 注入风险
        if len(illust_ids) > 10000:
            illust_ids = illust_ids[:10000]
        placeholders = ",".join("?" * len(illust_ids))
        cursor = await db.execute(
            f"SELECT illust_id, embedding FROM illust_embeddings WHERE illust_id IN ({placeholders})",
            illust_ids
        )
        rows = await cursor.fetchall()
        return {row[0]: json.loads(row[1]) for row in rows if row[1]}


async def save_illust_embeddings_batch(items: list[tuple[int, list[float], str]]):
    """
    批量保存作品 Embedding
    
    Args:
        items: [(illust_id, embedding, model), ...]
    """
    if not items:
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now()
        data = [(iid, json.dumps(emb), model, now) for iid, emb, model in items]
        await db.executemany("""
            INSERT OR REPLACE INTO illust_embeddings (illust_id, embedding, model, created_at)
            VALUES (?, ?, ?, ?)
        """, data)
        await db.commit()


async def get_user_embedding(user_id: int) -> Optional[tuple[list[float], str]]:
    """
    获取用户画像 Embedding
    
    Returns: (embedding, profile_hash) or None
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT embedding, profile_hash FROM user_embedding WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return (json.loads(row[0]), row[1])
        return None


async def save_user_embedding(user_id: int, embedding: list[float], model: str, profile_hash: str):
    """保存用户画像 Embedding"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO user_embedding (user_id, embedding, model, profile_hash, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, json.dumps(embedding), model, profile_hash, datetime.now()))
        await db.commit()


async def cleanup_old_embeddings(days: int = 60) -> int:
    """清理过期的作品 Embedding 缓存"""
    cutoff = datetime.now() - timedelta(days=days)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM illust_embeddings WHERE created_at < ?",
            (cutoff,)
        )
        await db.commit()
        return cursor.rowcount


# ============ MAB 策略统计汇总 (/stats) ============
async def get_all_strategy_stats() -> dict[str, dict]:
    """
    获取所有策略的统计数据
    Returns: {strategy: {"success": int, "total": int, "rate": float}, ...}
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT strategy, success_count, total_count FROM strategy_stats"
        )
        rows = await cursor.fetchall()
        result = {}
        for strategy, success, total in rows:
            success = int(success or 0)
            total = int(total or 0)
            rate = success / total if total > 0 else 0.0
            result[strategy] = {"success": success, "total": total, "rate": rate}
        return result


# ============ 每日维护辅助函数 ============
async def sync_blocked_tags_to_xp() -> int:
    """将屏蔽的标签从 XP 画像中移除，返回移除数量"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            DELETE FROM xp_profile 
            WHERE tag IN (SELECT tag FROM blocked_tags)
        """)
        await db.commit()
        return cursor.rowcount


async def cleanup_old_sent_history(days: int = 30) -> int:
    """清理 N 天前的推送历史记录，返回删除数量"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            DELETE FROM push_history 
            WHERE pushed_at < datetime('now', ?)
        """, (f'-{days} days',))
        await db.commit()
        return cursor.rowcount


# ============ 负向画像 (负反馈记录) ============
async def get_negative_profile() -> dict[str, float]:
    """获取负向画像"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT tag, weight FROM negative_profile ORDER BY weight DESC")
        rows = await cursor.fetchall()
        return {tag: weight for tag, weight in rows}


async def adjust_negative_weight(tag: str, delta: float):
    """调整负向画像权重"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO negative_profile (tag, weight, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(tag) DO UPDATE SET 
                weight = weight + excluded.weight,
                updated_at = excluded.updated_at
        """, (tag, delta, datetime.now()))
        await db.commit()


async def get_top_negative_tags(limit: int = 20) -> list[tuple[str, float]]:
    """获取权重最高的负向 Tag"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT tag, weight FROM negative_profile ORDER BY weight DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]


# ============ 冷启动支持 ============
async def get_popular_tags(limit: int = 20) -> list[tuple[str, float]]:
    """
    获取热门 Tag（基于收藏频率）
    用于冷启动时注入先验权重
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # 从 xp_bookmarks 统计标签出现频率
        cursor = await db.execute("""
            SELECT tag, COUNT(*) as freq
            FROM (
                SELECT json_each.value as tag 
                FROM xp_bookmarks, json_each(xp_bookmarks.tags)
            )
            GROUP BY tag
            ORDER BY freq DESC
            LIMIT ?
        """, (limit,))
        rows = await cursor.fetchall()
        if rows:
            return [(row[0], row[1]) for row in rows]
        
        # Fallback: 如果 xp_bookmarks 为空，从现有画像中取 top tags
        cursor = await db.execute(
            "SELECT tag, weight FROM xp_profile ORDER BY weight DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]


async def get_bookmark_count(user_id: int = None) -> int:
    """获取收藏数量（用于检测冷启动）"""
    async with aiosqlite.connect(DB_PATH) as db:
        if user_id:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM xp_bookmarks WHERE user_id = ?",
                (user_id,)
            )
        else:
            cursor = await db.execute("SELECT COUNT(*) FROM xp_bookmarks")
        row = await cursor.fetchone()
        return row[0] if row else 0


# ============ 批量消息映射 (Telegraph 模式) ============
async def save_batch_mapping(message_id: int, chat_id: str, illusts: list):
    """
    保存批量消息与作品的映射关系
    
    Args:
        message_id: Telegram 消息 ID
        chat_id: 聊天 ID
        illusts: 作品列表 (需要有 .id 属性)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        data = [(message_id, str(chat_id), i + 1, illust.id) 
                for i, illust in enumerate(illusts)]
        await db.executemany(
            """INSERT OR REPLACE INTO batch_message_map 
               (message_id, chat_id, illust_index, illust_id) VALUES (?, ?, ?, ?)""",
            data
        )
        await db.commit()


async def get_batch_illust_id(message_id: int, chat_id: str, index: int) -> int | None:
    """
    根据消息 ID 和编号获取作品 ID
    
    Args:
        message_id: Telegram 消息 ID
        chat_id: 聊天 ID
        index: 作品编号 (1-based)
    
    Returns:
        作品 ID，不存在时返回 None
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT illust_id FROM batch_message_map 
               WHERE message_id = ? AND chat_id = ? AND illust_index = ?""",
            (message_id, str(chat_id), index)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_batch_all_illust_ids(message_id: int, chat_id: str) -> list[int]:
    """获取批量消息中所有作品 ID"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT illust_id FROM batch_message_map 
               WHERE message_id = ? AND chat_id = ? 
               ORDER BY illust_index""",
            (message_id, str(chat_id))
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


async def cleanup_old_batch_mappings(days: int = 7) -> int:
    """清理旧的批量消息映射"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """DELETE FROM batch_message_map 
               WHERE created_at < datetime('now', ?)""",
            (f'-{days} days',)
        )
        await db.commit()
        return cursor.rowcount


# ============ Tag 翻译操作 (新增) ============
async def save_tag_translations(tags: list[tuple[str, str]]):
    """
    保存 Tag 的翻译 (name, translated_name)
    """
    if not tags:
        return
    
    # 过滤掉空的 translated_name
    valid_tags = [(n, t) for n, t in tags if t]
    if not valid_tags:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany("""
            INSERT OR REPLACE INTO tag_translations (name, translated_name, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, valid_tags)
        await db.commit()

async def get_translated_tag(name: str) -> Optional[str]:
    """获取 Tag 的翻译"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT translated_name FROM tag_translations WHERE name = ?", (name,))
        row = await cursor.fetchone()
        return row[0] if row else None

async def get_original_tag(translated_name: str) -> Optional[str]:
    """反查 Tag (通过翻译名查原名)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT name FROM tag_translations WHERE translated_name = ? COLLATE NOCASE", (translated_name,))
        row = await cursor.fetchone()
        return row[0] if row else None

async def search_tags_with_translation(keyword: str, limit: int = 10) -> list[tuple[str, str]]:
    """
    搜索标签 (同时匹配 name 和 translated_name)
    返回 [(name, translated_name)]
    """
    # 安全说明: 使用参数化查询传递 keyword，防止 SQL 注入
    # f-string 仅用于构建 LIKE 通配符模式，实际值通过参数传递
    keyword = f"%{keyword}%"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT name, translated_name 
            FROM tag_translations 
            WHERE name LIKE ? OR translated_name LIKE ?
            LIMIT ?
        """, (keyword, keyword, limit))
        return await cursor.fetchall()


async def get_best_search_tags(normalized_tags: list[str]) -> dict[str, str]:
    """
    批量获取 Normalized Tag 对应的已审核搜索词。
    """
    if not normalized_tags:
        return {}
        
    result = {tag: tag for tag in normalized_tags}
    
    async with aiosqlite.connect(DB_PATH) as db:
        # aiosqlite does not support executemany for SELECT, we use IN clause or loop
        # Since tags are small, we can loop or use a simple query
        for tag in normalized_tags:
            cursor = await db.execute("""
                SELECT original_tag FROM tag_aliases
                WHERE normalized_tag = ?
                ORDER BY CASE kind WHEN 'search' THEN 0 ELSE 1 END, priority DESC, updated_at DESC
                LIMIT 1
            """, (tag,))
            row = await cursor.fetchone()
            if row:
                result[tag] = row[0]
                
    return result

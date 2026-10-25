"""
SQLite 数据层
"""
import asyncio
import json
import hashlib
import time
import uuid
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
from push_schedule import DEFAULT_PUSH_SCHEDULE
from tag_relationship_judge import (
    MERGE_PRINCIPLES_VERSION,
    hash_relationship_evidence,
    relationship_evidence_hash,
)
from utils import normalize_tag

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "pixiv_xp.db"
SCHEMA_VERSION = 9
TAG_EVIDENCE_FRESHNESS_DAYS = 60
TAG_MAPPING_EVIDENCE_HASH_STATE = "tag_mapping_evidence_hash_semantic_v2"
TAG_MAPPING_PROPOSAL_VERSION = "tag-mapping-proposal-v1"



# Compact lifetime retention, independent of the preference ledger.
_EXPOSURE_BACKFILL = (
    "INSERT OR IGNORE INTO exposure_ids SELECT illust_id, pushed_at FROM push_history",
    "INSERT OR IGNORE INTO exposure_ids SELECT illust_id, created_at FROM feedback",
    """INSERT INTO feedback_metadata (illust_id,tags,user_id,user_name,source)
       SELECT c.illust_id,c.tags,c.user_id,c.user_name,c.source FROM illust_cache c
       WHERE EXISTS (SELECT 1 FROM feedback f WHERE f.illust_id=c.illust_id)
       ON CONFLICT(illust_id) DO UPDATE SET
       tags=excluded.tags,
       user_id=CASE WHEN excluded.user_id>0 THEN excluded.user_id ELSE feedback_metadata.user_id END,
       user_name=CASE WHEN excluded.user_name!='' THEN excluded.user_name ELSE feedback_metadata.user_name END,
       source=excluded.source""",
)


def _backup_exposure_upgrade(path):
    if not path.exists() or not path.stat().st_size:
        return
    with sqlite3.connect(path) as source:
        exists = source.execute("SELECT 1 FROM sqlite_master WHERE name='retention_migrations'").fetchone()
        if exists and source.execute('SELECT 1 FROM retention_migrations WHERE version=1').fetchone():
            return
        directory = path.parent / 'backups'
        directory.mkdir(exist_ok=True)
        target = directory / (path.name + '.retention-v1.' + datetime.now().strftime('%Y%m%dT%H%M%S%f') + '.sqlite3')
        with sqlite3.connect(target) as destination:
            source.backup(destination)


def _migrate_exposure(connection):
    connection.execute('BEGIN IMMEDIATE')
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS retention_migrations (version INTEGER PRIMARY KEY)')
        if connection.execute('SELECT 1 FROM retention_migrations WHERE version=1').fetchone():
            connection.commit()
            return
        connection.execute('CREATE TABLE IF NOT EXISTS exposure_ids (illust_id INTEGER PRIMARY KEY, first_seen_at TIMESTAMP NOT NULL)')
        connection.execute("CREATE TABLE IF NOT EXISTS feedback_metadata (illust_id INTEGER PRIMARY KEY, tags TEXT, user_id INTEGER DEFAULT 0, user_name TEXT DEFAULT '', source TEXT)")
        for statement in _EXPOSURE_BACKFILL:
            connection.execute(statement)
        # Also cover direct ledger feedback writes without coupling to that module.
        for table in ('feedback', 'illust_cache'):
            for event in ('INSERT', 'UPDATE'):
                if table == 'feedback':
                    exposure_sql = "INSERT OR IGNORE INTO exposure_ids VALUES (NEW.illust_id, CURRENT_TIMESTAMP);"
                else:
                    exposure_sql = ""
                connection.execute(f"""CREATE TRIGGER IF NOT EXISTS retain_{table}_{event.lower()}
                    AFTER {event} ON {table} BEGIN
                    {exposure_sql}
                    INSERT INTO feedback_metadata (illust_id,tags,user_id,user_name,source)
                    SELECT f.illust_id,c.tags,COALESCE(c.user_id,0),COALESCE(c.user_name,''),c.source
                    FROM feedback f JOIN illust_cache c ON c.illust_id=f.illust_id
                    WHERE f.illust_id=NEW.illust_id
                    ON CONFLICT(illust_id) DO UPDATE SET
                    tags=COALESCE(excluded.tags,feedback_metadata.tags),
                    user_id=CASE WHEN excluded.user_id>0 THEN excluded.user_id ELSE feedback_metadata.user_id END,
                    user_name=CASE WHEN excluded.user_name!='' THEN excluded.user_name ELSE feedback_metadata.user_name END,
                    source=COALESCE(excluded.source,feedback_metadata.source);
                    END""")
        connection.execute('INSERT OR IGNORE INTO retention_migrations VALUES (1)')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


async def _retain_before_cleanup(connection):
    # Acquire the writer lock before snapshot and delete; independent cleanup
    # entrypoints cannot remove cache/history ahead of their durable snapshot.
    await connection.execute('BEGIN IMMEDIATE')
    for statement in _EXPOSURE_BACKFILL:
        await connection.execute(statement)


async def init_db():
    """初始化数据库表结构"""
    _init_db_sync()


def _init_db_sync():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    from persistent_profile import backup_before_upgrade, migrate
    _backup_exposure_upgrade(DB_PATH)
    backup_before_upgrade(DB_PATH)

    with sqlite3.connect(DB_PATH) as db:
        # Preserve old schemas and data; missing owner IDs remain unknown (0).
        for table, column, definition in (
            ('xp_bookmarks', 'user_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('illust_cache', 'user_id', 'INTEGER NOT NULL DEFAULT 0'),
        ):
            columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            if columns and column not in columns:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
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
                input_evidence_hash TEXT NOT NULL DEFAULT '',
                proposal_fingerprint TEXT NOT NULL DEFAULT '',
                proposal_version TEXT NOT NULL DEFAULT 'tag-mapping-proposal-v1',
                supersedes_candidate_id INTEGER,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'accepted', 'rejected')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(supersedes_candidate_id) REFERENCES tag_mapping_candidates(id)
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
                grounding_provenance TEXT NOT NULL DEFAULT '{}',
                invalidated_at TIMESTAMP,
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

            -- Semantic vector Exploration retrieval audit.  Similarity is
            -- retrieval evidence only; these rows never activate Tag Aliases.
            CREATE TABLE IF NOT EXISTS exploration_vector_runs (
                run_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'semantic_vector_exploration',
                model TEXT NOT NULL,
                profile_hash TEXT NOT NULL,
                pool_limit INTEGER NOT NULL,
                pool_size INTEGER NOT NULL DEFAULT 0,
                candidate_limit INTEGER NOT NULL,
                similarity_threshold REAL NOT NULL,
                duplicate_threshold REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'started',
                profile_concentration REAL,
                slate_profile_concentration REAL,
                duplicate_semantic_rate REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS exploration_vector_candidates (
                run_id TEXT NOT NULL,
                illust_id INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'semantic_vector_exploration',
                similarity REAL NOT NULL,
                model TEXT NOT NULL,
                retrieval_rank INTEGER NOT NULL,
                final_rank INTEGER,
                selected INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (run_id, illust_id),
                FOREIGN KEY (run_id) REFERENCES exploration_vector_runs(run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_exploration_vector_candidates_illust
                ON exploration_vector_candidates(illust_id);

            -- 历史种子轮换的独立状态表（纯增量，不重建或改写已有业务表）。
            -- 时间均为 UTC epoch 秒；claim_token 是唯一租约标识。
            CREATE TABLE IF NOT EXISTS related_seed_state (
                illust_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'claimed',
                cooldown_until REAL NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_attempt_at REAL NOT NULL DEFAULT 0,
                claim_token TEXT,
                claim_until REAL,
                updated_at REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_related_seed_state_claim
                ON related_seed_state(claim_until);
        """)
        db.execute(
            """
            INSERT OR IGNORE INTO system_state (key, value, updated_at)
            VALUES ('schedule_cron', ?, CURRENT_TIMESTAMP)
            """,
            (DEFAULT_PUSH_SCHEDULE,),
        )
        db.commit()

        ai_record_columns = {
            row[1] for row in db.execute("PRAGMA table_info(ai_tag_classification_records)")
        }
        if "grounding_provenance" not in ai_record_columns:
            db.execute(
                "ALTER TABLE ai_tag_classification_records "
                "ADD COLUMN grounding_provenance TEXT NOT NULL DEFAULT '{}'"
            )
            db.commit()
            logger.info("迁移：AI Classification Record 添加 Grounding provenance")
        if "invalidated_at" not in ai_record_columns:
            db.execute(
                "ALTER TABLE ai_tag_classification_records ADD COLUMN invalidated_at TIMESTAMP"
            )
            db.commit()
            logger.info("迁移：AI Classification Record 添加失效标记")

        candidate_columns = {
            row[1] for row in db.execute("PRAGMA table_info(tag_mapping_candidates)")
        }
        if "embedding_similarity" not in candidate_columns:
            db.execute(
                "ALTER TABLE tag_mapping_candidates ADD COLUMN embedding_similarity REAL"
            )
            db.commit()
        for column_name, column_sql in (
            ("input_evidence_hash", "TEXT NOT NULL DEFAULT ''"),
            ("proposal_fingerprint", "TEXT NOT NULL DEFAULT ''"),
            ("proposal_version", "TEXT NOT NULL DEFAULT 'tag-mapping-proposal-v1'"),
            ("supersedes_candidate_id", "INTEGER"),
        ):
            if column_name not in candidate_columns:
                db.execute(
                    f"ALTER TABLE tag_mapping_candidates ADD COLUMN {column_name} {column_sql}"
                )
        db.commit()

        # v2 hashes only stable semantic context.  Re-hash stored payloads once
        # so a profile-weight refresh does not force otherwise valid Judge
        # recommendations to be regenerated.
        evidence_hash_migrated = db.execute(
            "SELECT value FROM system_state WHERE key = ?",
            (TAG_MAPPING_EVIDENCE_HASH_STATE,),
        ).fetchone()
        if not evidence_hash_migrated:
            for recommendation_id, raw_evidence in db.execute(
                "SELECT id, evidence_payload FROM tag_mapping_ai_recommendations"
            ).fetchall():
                try:
                    evidence_payload = json.loads(raw_evidence)
                except (TypeError, json.JSONDecodeError):
                    continue
                db.execute(
                    "UPDATE tag_mapping_ai_recommendations SET evidence_hash = ? WHERE id = ?",
                    (hash_relationship_evidence(evidence_payload), recommendation_id),
                )
            db.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (?, 'true', CURRENT_TIMESTAMP)
                """,
                (TAG_MAPPING_EVIDENCE_HASH_STATE,),
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
                WHERE cleaned_tag IS NULL OR LOWER(cleaned_tag) <> LOWER(original_tag)
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
                FROM tag_mapping_stats stats
                WHERE LOWER(normalized_tag) <> LOWER(original_tag)
                  AND NOT EXISTS (
                      SELECT 1 FROM ai_tag_cache cache
                      WHERE cache.original_tag = stats.original_tag
                        AND cache.cleaned_tag = stats.normalized_tag
                  )
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
        migrate(db)
        _migrate_exposure(db)


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
        await _retain_before_cleanup(db)
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


def get_tag_aliases_sync() -> dict[str, str]:
    """Return every active alias for standalone CLI conflict preflight."""

    with sqlite3.connect(DB_PATH, timeout=10) as db:
        return {
            row[0]: row[1] for row in db.execute(
                "SELECT original_tag, normalized_tag FROM tag_aliases"
            ).fetchall()
        }


def _tag_alias_search_clause(query: str | None) -> tuple[str, list[str]]:
    value = str(query or "").strip()
    if not value:
        return "", []
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return (
        "WHERE (original_tag LIKE ? ESCAPE '\\' COLLATE NOCASE "
        "OR normalized_tag LIKE ? ESCAPE '\\' COLLATE NOCASE)",
        [pattern, pattern],
    )


async def list_tag_aliases(
    limit: int = 20,
    offset: int = 0,
    query: str | None = None,
) -> list[dict]:
    """List one searchable page of active aliases for review and correction."""
    where_sql, search_params = _tag_alias_search_clause(query)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT original_tag, normalized_tag, kind, source, priority, updated_at
            FROM tag_aliases
            {where_sql}
            ORDER BY updated_at DESC, original_tag ASC
            LIMIT ? OFFSET ?
            """,
            (*search_params, int(limit), int(offset)),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def count_tag_aliases(query: str | None = None) -> int:
    """Count active aliases matching the same search used by list_tag_aliases."""
    where_sql, search_params = _tag_alias_search_clause(query)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM tag_aliases {where_sql}",
            search_params,
        )
        row = await cursor.fetchone()
        return int(row[0] if row else 0)


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


def _stable_json_hash(payload: dict) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def _tag_mapping_input_evidence_hash(db, tag: str) -> str:
    """Hash semantic evidence that can legitimately reopen a rejected proposal."""
    cache_cursor = await db.execute(
        "SELECT classification, source FROM tag_classification_cache "
        "WHERE normalized_tag = ?",
        (tag,),
    )
    cache_row = await cache_cursor.fetchone()
    record_cursor = await db.execute(
        """SELECT classification, explanation, languages, grounding_provenance
           FROM ai_tag_classification_records
           WHERE tag = ? AND invalidated_at IS NULL""",
        (tag,),
    )
    record_row = await record_cursor.fetchone()
    translation_cursor = await db.execute(
        "SELECT translated_name FROM tag_translations WHERE name = ?",
        (tag,),
    )
    translation_row = await translation_cursor.fetchone()
    return _stable_json_hash({
        "tag": tag,
        "classification": list(cache_row) if cache_row else None,
        "ai_record": list(record_row) if record_row else None,
        "translation": translation_row[0] if translation_row else None,
        "proposal_version": TAG_MAPPING_PROPOSAL_VERSION,
    })


async def _tag_mapping_proposal_fingerprint(db, row, input_evidence_hash: str) -> str:
    """Use identical source/target evidence for queue selection and persistence."""
    return _stable_json_hash({
        "original_tag": row[0],
        "proposed_normalized_tag": row[1],
        "kind": row[2],
        "source": row[3],
        "explanation": row[4],
        "embedding_similarity": row[5],
        "proposal_version": row[6],
        "input_evidence_hash": input_evidence_hash,
        "target_evidence_hash": await _tag_mapping_input_evidence_hash(db, row[1]),
    })


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
        proposal_version = str(
            read("proposal_version", TAG_MAPPING_PROPOSAL_VERSION)
            or TAG_MAPPING_PROPOSAL_VERSION
        )
        rows.append((
            original, target, kind, source, explanation, similarity, proposal_version,
        ))
    if not rows:
        return 0

    inserted = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for row in rows:
            input_evidence_hash = await _tag_mapping_input_evidence_hash(db, row[0])
            proposal_fingerprint = await _tag_mapping_proposal_fingerprint(
                db, row, input_evidence_hash,
            )
            cursor = await db.execute(
                """
                SELECT id, status, input_evidence_hash, proposal_fingerprint
                FROM tag_mapping_candidates
                WHERE (
                    original_tag = ? AND proposed_normalized_tag = ?
                ) OR (
                    original_tag = ? AND proposed_normalized_tag = ?
                )
                ORDER BY id DESC
                LIMIT 1
                """,
                (row[0], row[1], row[1], row[0]),
            )
            existing = await cursor.fetchone()
            if existing:
                if existing[1] in {"pending", "accepted"}:
                    continue
                if (
                    str(existing[2] or "") == input_evidence_hash
                    and str(existing[3] or "") == proposal_fingerprint
                ):
                    continue
            await db.execute(
                """
                INSERT INTO tag_mapping_candidates (
                    original_tag, proposed_normalized_tag, kind, source, explanation,
                    embedding_similarity, proposal_version, input_evidence_hash,
                    proposal_fingerprint, supersedes_candidate_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*row, input_evidence_hash, proposal_fingerprint, existing[0] if existing else None),
            )
            inserted += 1
        await db.commit()
    return inserted


_TAG_MAPPING_CANDIDATES_SELECT_SQL = """
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
    LEFT JOIN ai_tag_classification_records ro
        ON ro.tag = c.original_tag AND ro.invalidated_at IS NULL
    LEFT JOIN ai_tag_classification_records rt
        ON rt.tag = c.proposed_normalized_tag AND rt.invalidated_at IS NULL
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
"""


_TAG_MAPPING_CANDIDATES_SQL = _TAG_MAPPING_CANDIDATES_SELECT_SQL + """
    WHERE c.status = ?
    ORDER BY profile_weight DESC, c.occurrence_count DESC, c.id ASC
"""


def _enrich_tag_mapping_candidates(rows) -> list[dict]:
    items = [dict(row) for row in rows]
    for item in items:
        item["ai_is_current"] = bool(
            item.get("ai_recommendation_id")
            and item.get("ai_principles_version") == MERGE_PRINCIPLES_VERSION
            and item.get("ai_evidence_hash") == relationship_evidence_hash(item)
        )
    return items


def _validate_tag_mapping_candidate_status(status: str) -> None:
    if status not in {"pending", "accepted", "rejected"}:
        raise ValueError(f"不支持的候选状态: {status}")


def collapse_tag_mapping_candidate_groups(items: list[dict]) -> list[dict]:
    """Choose one review item per unordered tag pair without discarding audit rows."""

    grouped: dict[tuple[str, str] | tuple[str, int], list[dict]] = {}
    for item in items:
        original = str(item.get("original_tag") or "")
        target = str(item.get("proposed_normalized_tag") or "")
        if original and target and original.casefold() == target.casefold():
            # normalize_tag already folds case, so this cannot create a useful
            # alias and must not consume a human or AI review.
            continue
        if not original or not target:
            key: tuple[str, str] | tuple[str, int] = ("id", int(item["id"]))
        else:
            key = tuple(sorted((original.casefold(), target.casefold())))
        grouped.setdefault(key, []).append(dict(item))

    collapsed = []
    for group in grouped.values():
        primary = min(
            group,
            key=lambda item: (
                # A current recommendation belongs to the unordered tag pair,
                # even when an older source row owns its audit record.  Keep
                # that row as the group primary so Judge and staging reuse it.
                0 if item.get("ai_is_current") else 1,
                0 if item.get("kind") == "equivalent" else 1,
                0 if item.get("source") == "legacy_ai_tag_cache" else 1,
                int(item["id"]),
            ),
        )
        primary["duplicate_candidate_ids"] = sorted(int(item["id"]) for item in group)
        primary["duplicate_count"] = len(group)
        primary["duplicate_sources"] = sorted({str(item.get("source") or "") for item in group})
        primary["duplicate_kinds"] = sorted({str(item.get("kind") or "") for item in group})
        collapsed.append(primary)
    return collapsed


def paginate_tag_mapping_candidate_groups(
    items: list[dict],
    *,
    limit: int,
    offset: int = 0,
) -> list[dict]:
    """Collapse the complete row set before applying a review-group page."""

    limit = max(1, int(limit))
    offset = max(0, int(offset))
    groups = collapse_tag_mapping_candidate_groups(items)
    return groups[offset:offset + limit]


def get_tag_mapping_candidates_sync(limit: int = 100, status: str = "pending") -> list[dict]:
    """Synchronous candidate read for standalone CLI processes."""

    _validate_tag_mapping_candidate_status(status)
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(_TAG_MAPPING_CANDIDATES_SQL, (status,)).fetchall()
    return paginate_tag_mapping_candidate_groups(
        _enrich_tag_mapping_candidates(rows), limit=limit,
    )


async def _load_tag_mapping_candidate_rows(status: str) -> list[dict]:
    _validate_tag_mapping_candidate_status(status)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(_TAG_MAPPING_CANDIDATES_SQL, (status,))
        rows = await cursor.fetchall()
    return _enrich_tag_mapping_candidates(rows)


async def get_tag_mapping_candidates(
    limit: int = 100,
    status: str = "pending",
    offset: int = 0,
) -> list[dict]:
    """Return mapping proposals in human review order for async Web/API callers."""

    rows = await _load_tag_mapping_candidate_rows(status)
    return paginate_tag_mapping_candidate_groups(
        rows, limit=limit, offset=offset,
    )


async def get_tag_mapping_candidate_group(
    candidate_id: int,
    status: str = "pending",
) -> dict | None:
    """Return the complete collapsed group containing one candidate row."""

    groups = collapse_tag_mapping_candidate_groups(
        await _load_tag_mapping_candidate_rows(status)
    )
    candidate_id = int(candidate_id)
    return next(
        (
            group for group in groups
            if candidate_id in (group.get("duplicate_candidate_ids") or [group["id"]])
        ),
        None,
    )


async def get_tag_mapping_candidate_status(candidate_id: int) -> dict | None:
    """Return current review status for an idempotent repeated confirmation."""

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT status, kind, original_tag, proposed_normalized_tag
            FROM tag_mapping_candidates
            WHERE id = ?
            """,
            (int(candidate_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "status": row[0],
        "kind": row[1],
        "original_tag": row[2],
        "proposed_normalized_tag": row[3],
    }


async def count_tag_mapping_candidate_groups(status: str = "pending") -> int:
    """Count collapsed review groups (unordered tag pairs) for queue summaries."""

    _validate_tag_mapping_candidate_status(status)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, original_tag, proposed_normalized_tag, kind, source
            FROM tag_mapping_candidates
            WHERE status = ?
            """,
            (status,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    return len(collapse_tag_mapping_candidate_groups(rows))


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


def _tag_mapping_ai_stage_rows(decisions) -> list[tuple[int, int, str]]:
    allowed = {"accept_equivalent", "reject"}
    rows = []
    for decision in decisions:
        read = decision.get if isinstance(decision, dict) else lambda key: getattr(decision, key)
        staged_decision = str(read("decision"))
        if staged_decision not in allowed:
            raise ValueError("AI Recommendation 暂存决定无效")
        rows.append((int(read("candidate_id")), int(read("recommendation_id")), staged_decision))
    return rows


_STAGE_TAG_MAPPING_AI_SQL = """
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
"""


def stage_tag_mapping_ai_recommendations_sync(decisions) -> int:
    """Synchronous shortlist write for the standalone CLI."""

    rows = _tag_mapping_ai_stage_rows(decisions)
    if not rows:
        return 0
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        db.execute("BEGIN IMMEDIATE")
        for candidate_id, recommendation_id, staged_decision in rows:
            cursor = db.execute(
                _STAGE_TAG_MAPPING_AI_SQL,
                (staged_decision, recommendation_id, candidate_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("AI Recommendation 已过期或候选已经审核")
    return len(rows)


def apply_tag_mapping_ai_batch_sync(decisions) -> dict:
    """Atomically apply one explicitly human-confirmed safe AI batch."""

    rows = _tag_mapping_ai_stage_rows(decisions)
    result = {
        "accepted_equivalent": 0,
        "rejected": 0,
        "aliases_created": 0,
        "aliases_already_active": 0,
        "aliases_reversed_to_ai_canonical": 0,
        "duplicate_candidates_resolved": 0,
    }
    if not rows:
        return result
    with sqlite3.connect(DB_PATH, timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        aliases = {
            row[0]: row[1] for row in db.execute(
                "SELECT original_tag, normalized_tag FROM tag_aliases WHERE kind = 'equivalent'"
            ).fetchall()
        }
        for candidate_id, recommendation_id, staged_decision in rows:
            candidate = db.execute(
                "SELECT * FROM tag_mapping_candidates WHERE id = ? AND status = 'pending'",
                (candidate_id,),
            ).fetchone()
            recommendation = db.execute(
                """
                SELECT id, relation, canonical_tag FROM tag_mapping_ai_recommendations
                WHERE id = ? AND candidate_id = ?
                  AND id = (
                      SELECT MAX(id) FROM tag_mapping_ai_recommendations
                      WHERE candidate_id = ?
                  )
                """,
                (recommendation_id, candidate_id, candidate_id),
            ).fetchone()
            if not candidate or not recommendation:
                raise ValueError("AI Recommendation 已过期或候选已经审核")
            db.execute(
                """
                UPDATE tag_mapping_ai_recommendations
                SET staged_decision = ?, staged_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (staged_decision, recommendation_id),
            )

            candidate_original = normalize_tag(candidate["original_tag"])
            candidate_target = normalize_tag(candidate["proposed_normalized_tag"] or "")
            if staged_decision == "accept_equivalent":
                if recommendation["relation"] != "equivalent":
                    raise ValueError("只有 equivalent Recommendation 可以建立 Tag Alias")
                canonical = normalize_tag(recommendation["canonical_tag"] or "")
                if (
                    not candidate_original or not candidate_target
                    or candidate_original == candidate_target
                    or canonical not in {candidate_original, candidate_target}
                ):
                    raise ValueError("该候选没有可接受的目标 Normalized Tag")
                if canonical == candidate_original:
                    original, target = candidate_target, candidate_original
                    result["aliases_reversed_to_ai_canonical"] += 1
                else:
                    original, target = candidate_original, candidate_target
                active_row = db.execute(
                    "SELECT normalized_tag, kind FROM tag_aliases WHERE original_tag = ?",
                    (original,),
                ).fetchone()
                if active_row and (
                    str(active_row[0]) != target or str(active_row[1]) != 'equivalent'
                ):
                    raise ValueError(
                        "alias_conflict: 已存在不同的 Tag Alias；请先显式撤销现有别名"
                    )
                existing_target = aliases.get(original)
                reverse_target = aliases.get(target)
                if existing_target and existing_target != target:
                    raise ValueError(f"{original} 已映射到另一个 Normalized Tag")
                if existing_target == target or reverse_target == original:
                    result["aliases_already_active"] += 1
                else:
                    if would_create_alias_cycle(aliases, original, target):
                        raise ValueError("Tag Alias 不能形成循环")
                    db.execute(
                        """
                        INSERT INTO tag_aliases (
                            original_tag, normalized_tag, kind, source, priority, updated_at
                        ) VALUES (?, ?, 'equivalent', 'manual', ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(original_tag) DO NOTHING
                        """,
                        (original, target, int(candidate["occurrence_count"] or 1)),
                    )
                    aliases[original] = target
                    result["aliases_created"] += 1
                status = "accepted"
                applied_kind = "equivalent"
                result["accepted_equivalent"] += 1
            else:
                status = "rejected"
                applied_kind = candidate["kind"]
                result["rejected"] += 1

            duplicate_ids = [
                int(row[0]) for row in db.execute(
                    """
                    SELECT id FROM tag_mapping_candidates
                    WHERE status = 'pending' AND id != ? AND (
                        (original_tag = ? AND proposed_normalized_tag = ?)
                        OR (original_tag = ? AND proposed_normalized_tag = ?)
                    )
                    """,
                    (
                        candidate_id,
                        candidate["original_tag"], candidate["proposed_normalized_tag"],
                        candidate["proposed_normalized_tag"], candidate["original_tag"],
                    ),
                ).fetchall()
            ]
            reviewed_ids = [candidate_id, *duplicate_ids]
            placeholders = ", ".join("?" for _ in reviewed_ids)
            db.execute(
                f"""
                UPDATE tag_mapping_candidates
                SET status = ?, kind = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                (status, applied_kind, *reviewed_ids),
            )
            result["duplicate_candidates_resolved"] += len(duplicate_ids)
        db.commit()
    return result


async def stage_tag_mapping_ai_recommendations(decisions) -> int:
    """Shortlist current recommendations without reviewing candidates or creating aliases."""

    rows = _tag_mapping_ai_stage_rows(decisions)
    if not rows:
        return 0

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            for candidate_id, recommendation_id, staged_decision in rows:
                cursor = await db.execute(
                    _STAGE_TAG_MAPPING_AI_SQL,
                    (staged_decision, recommendation_id, candidate_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("AI Recommendation 已过期或候选已经审核")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return len(rows)


async def review_tag_mapping_candidate(
    candidate_id: int,
    decision: str,
    kind: str | None = None,
    *,
    expected_candidate_ids: tuple[int, ...] | list[int] | None = None,
) -> dict:
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

        original_group_tag = str(row["original_tag"] or "")
        target_group_tag = str(row["proposed_normalized_tag"] or "")
        if original_group_tag and target_group_tag:
            pair_key = tuple(sorted((
                original_group_tag.casefold(), target_group_tag.casefold(),
            )))
            group_cursor = await db.execute(
                """
                SELECT id, original_tag, proposed_normalized_tag
                FROM tag_mapping_candidates
                WHERE status = 'pending'
                """
            )
            pending_rows = await group_cursor.fetchall()
            reviewed_ids = sorted(
                int(item["id"])
                for item in pending_rows
                if item["original_tag"] and item["proposed_normalized_tag"]
                and tuple(sorted((
                    str(item["original_tag"]).casefold(),
                    str(item["proposed_normalized_tag"]).casefold(),
                ))) == pair_key
            )
        else:
            reviewed_ids = [int(candidate_id)]

        if expected_candidate_ids is not None and reviewed_ids != sorted(
            int(item) for item in expected_candidate_ids
        ):
            await db.rollback()
            raise ValueError("映射候选组已变化，请重新审核")

        applied_kind = kind or row["kind"]
        if applied_kind not in {"equivalent", "search"}:
            await db.rollback()
            raise ValueError("Tag Alias 类型必须是 equivalent 或 search")
        original = normalize_tag(row["original_tag"])
        target = normalize_tag(row["proposed_normalized_tag"] or "")
        alias_already_active = False
        active_alias_original = None
        active_normalized_tag = None

        if decision == "accept":
            if not original or not target or original == target:
                await db.rollback()
                raise ValueError("该候选没有可接受的目标 Normalized Tag")
            if applied_kind == "equivalent":
                aliases_cursor = await db.execute(
                    "SELECT original_tag, normalized_tag FROM tag_aliases WHERE kind = 'equivalent'"
                )
                aliases = {item[0]: item[1] for item in await aliases_cursor.fetchall()}
                if aliases.get(original) == target:
                    alias_already_active = True
                    active_alias_original = original
                    active_normalized_tag = target
                elif aliases.get(target) == original:
                    alias_already_active = True
                    active_alias_original = target
                    active_normalized_tag = original
                elif would_create_alias_cycle(aliases, original, target):
                    await db.rollback()
                    raise ValueError("Tag Alias 不能形成循环")
            active_cursor = await db.execute(
                "SELECT normalized_tag, kind FROM tag_aliases WHERE original_tag = ?",
                (original,),
            )
            active_row = await active_cursor.fetchone()
            if active_row and (
                str(active_row[0]) != target or str(active_row[1]) != applied_kind
            ):
                await db.rollback()
                raise ValueError(
                    "alias_conflict: 已存在不同的 Tag Alias；请先显式撤销现有别名"
                )
            if not alias_already_active:
                await db.execute(
                    """
                    INSERT INTO tag_aliases (
                        original_tag, normalized_tag, kind, source, priority, updated_at
                    ) VALUES (?, ?, ?, 'manual', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(original_tag) DO NOTHING
                    """,
                    (original, target, applied_kind, int(row["occurrence_count"] or 1)),
                )
            new_status = "accepted"
        else:
            new_status = "rejected"

        duplicate_ids = [item for item in reviewed_ids if item != int(candidate_id)]
        placeholders = ", ".join("?" for _ in reviewed_ids)
        await db.execute(
            f"UPDATE tag_mapping_candidates SET status = ?, kind = ?, updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            (new_status, applied_kind, *reviewed_ids),
        )
        await db.commit()
        return {
            "id": candidate_id,
            "status": new_status,
            "original_tag": original,
            "normalized_tag": target or None,
            "kind": applied_kind,
            "alias_already_active": alias_already_active,
            "active_alias_original": active_alias_original,
            "active_normalized_tag": active_normalized_tag,
            "duplicate_candidates_resolved": len(duplicate_ids),
        }


async def reject_legacy_filtered_tag_mapping_candidates() -> int:
    """Close legacy ``meaningless`` rows that have no mapping target.

    These rows came from the retired AITagProcessor's filter decision.  They
    cannot be accepted as an alias, and rejecting them preserves the imported
    audit record without giving the old automatic decision any runtime effect.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE tag_mapping_candidates
            SET status = 'rejected', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'pending'
              AND source = 'legacy_ai_tag_cache'
              AND (proposed_normalized_tag IS NULL OR TRIM(proposed_normalized_tag) = '')
            """
        )
        await db.commit()
        return cursor.rowcount


async def get_tag_mapping_candidate_inputs(limit: int = 100) -> list[str]:
    """Select tags without an alias, pending proposal, or unchanged rejection."""
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
            """,
        )
        selected = []
        for (tag,) in await cursor.fetchall():
            evidence_hash = await _tag_mapping_input_evidence_hash(db, tag)
            rejected_cursor = await db.execute(
                """SELECT original_tag, proposed_normalized_tag, kind, source,
                          explanation, embedding_similarity, proposal_version,
                          input_evidence_hash, proposal_fingerprint
                   FROM tag_mapping_candidates
                   WHERE original_tag = ? AND status = 'rejected'
                   ORDER BY id DESC LIMIT 1""",
                (tag,),
            )
            rejected = await rejected_cursor.fetchone()
            if rejected and (
                str(rejected[7] or "") == evidence_hash
                and str(rejected[6] or "") == TAG_MAPPING_PROPOSAL_VERSION
                and str(rejected[8] or "") == await _tag_mapping_proposal_fingerprint(
                    db, rejected[:7], evidence_hash,
                )
            ):
                continue
            selected.append(tag)
            if len(selected) >= limit:
                break
        return selected


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
              AND (source = 'manual' OR (source = 'ai' AND classification != 'unresolved') OR updated_at >= ?)
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
    *,
    offset: int = 0,
) -> list[dict]:
    """List high-impact profile tags that still need Grounded Judge classification.

    ``offset`` advances one page of the same deterministic
    ``ABS(weight) DESC, tag ASC`` order, so a caller that must skip paused or
    cooling rows can keep reading further pages until it has a full batch or
    reaches the end of the profile.  Nothing else about the ordering changes.
    """
    limit = max(1, int(limit))
    minimum = abs(float(min_profile_weight))
    offset = max(0, int(offset))
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
            LIMIT ? OFFSET ?
            """,
            (minimum, TAG_CATEGORY_UNRESOLVED, limit, offset),
        )
        return [
            {"tag": tag, "profile_weight": float(weight), "classification": classification}
            for tag, weight, classification in await cursor.fetchall()
        ]


class DatabaseNotInitializedError(sqlite3.OperationalError):
    """The database file is absent, so a read-only preview cannot run.

    Subclassing :class:`sqlite3.OperationalError` lets a read-only caller
    report the missing file with the same exception family SQLite itself uses
    for an unreadable/uninitialized database ("no such table"), while callers
    that want a precise, non-creating message keep catching this type.
    """


class DatabaseSchemaMismatchError(RuntimeError):
    """The database file exists but does not carry the expected schema."""


#: Minimum tables/columns a read-only preview needs.  Anything else is reported
#: as a schema mismatch instead of being repaired (a preview never migrates).
PREVIEW_REQUIRED_SCHEMA = {
    "xp_profile": ("tag", "weight"),
    "tag_classification_cache": ("normalized_tag", "classification"),
}


def _connect_readonly(path: Path) -> sqlite3.Connection:
    from urllib.parse import quote

    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _validate_preview_schema(connection: sqlite3.Connection) -> None:
    """Reject a database the read-only preview cannot safely read."""
    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    for table, columns in PREVIEW_REQUIRED_SCHEMA.items():
        if table not in tables:
            raise DatabaseSchemaMismatchError(
                f"数据库 schema 不匹配，只读预览中止：缺少表 {table}"
            )
        present = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        missing = [column for column in columns if column not in present]
        if missing:
            raise DatabaseSchemaMismatchError(
                f"数据库 schema 不匹配，只读预览中止：表 {table} 缺少列 {', '.join(missing)}"
            )


def _open_preview_readonly(path: Path) -> sqlite3.Connection:
    """Open the preview database read-only; never creates and never migrates it."""
    path = Path(path)
    if not path.exists():
        raise DatabaseNotInitializedError(
            f"数据库文件不存在，只读读取中止（no such table: xp_profile）：{path}"
        )
    try:
        connection = _connect_readonly(path)
    except sqlite3.Error as exc:
        raise DatabaseSchemaMismatchError(f"数据库无法以只读方式打开：{path}") from exc
    try:
        _validate_preview_schema(connection)
    except DatabaseSchemaMismatchError:
        connection.close()
        raise
    return connection


def profile_tags_exist_readonly(tags) -> dict[str, bool]:
    """Report which tags exist in the profile, through a read-only connection.

    A missing database or an incompatible schema aborts instead of creating or
    migrating a file, so a read-only command can never leave an empty database
    behind -- not even when the caller supplied an explicit tag list.
    """
    wanted = [str(tag) for tag in dict.fromkeys(tags or ()) if str(tag).strip()]
    if not wanted:
        return {}
    path = Path(DB_PATH)
    connection = _open_preview_readonly(path)
    found: set[str] = set()
    try:
        for start in range(0, len(wanted), 400):
            chunk = wanted[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            try:
                rows = connection.execute(
                    f"SELECT tag FROM xp_profile WHERE tag IN ({placeholders})", chunk,
                ).fetchall()
            except sqlite3.DatabaseError as exc:
                raise DatabaseSchemaMismatchError(
                    f"数据库 schema 不匹配，只读预览中止：{path}"
                ) from exc
            found.update(str(row[0]) for row in rows)
    finally:
        connection.close()
    return {tag: tag in found for tag in wanted}


def get_high_weight_unclassified_profile_tags_readonly(
    limit: int = 40,
    min_profile_weight: float = 0.0,
    *,
    offset: int = 0,
) -> list[dict]:
    """Read one page of the review queue through a read-only SQLite connection.

    The connection is opened with ``mode=ro``: a missing file or an
    incompatible schema aborts the call instead of creating or migrating a
    database, so a preview can never touch production data.  ``offset`` walks
    the same ``ABS(weight) DESC, tag ASC`` order as the maintenance path, so
    both read-only previews select identical batches page by page.
    """
    limit = max(1, int(limit))
    minimum = abs(float(min_profile_weight))
    offset = max(0, int(offset))
    path = Path(DB_PATH)
    connection = _open_preview_readonly(path)
    try:
        rows = connection.execute(
            """
            SELECT xp.tag, xp.weight, classification.classification
            FROM xp_profile AS xp
            LEFT JOIN tag_classification_cache AS classification
                ON classification.normalized_tag = xp.tag
            WHERE ABS(xp.weight) >= ?
              AND (classification.normalized_tag IS NULL OR classification.classification = ?)
            ORDER BY ABS(xp.weight) DESC, xp.tag ASC
            LIMIT ? OFFSET ?
            """,
            (minimum, TAG_CATEGORY_UNRESOLVED, limit, offset),
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise DatabaseSchemaMismatchError(
            f"数据库 schema 不匹配，只读预览中止：{path}"
        ) from exc
    finally:
        connection.close()
    return [
        {"tag": str(tag), "profile_weight": float(weight), "classification": classification}
        for tag, weight, classification in rows
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


async def activate_ai_tag_classification(
    tag: str,
    classification: str,
    explanation: str,
    languages: str,
    *,
    grounding_provenance: dict | None = None,
    preserve_resolved: bool = False,
) -> bool:
    """Persist a complete AI Classification Record unless a human owns the tag.

    ``preserve_resolved`` is the Native Jev option: inside one real SQLite
    transaction it re-reads the cache row, so an explicit AI or human decision
    that landed while this operation was waiting for search/model results is
    never overwritten.  Legacy backends keep the previous last-writer-wins
    semantics by leaving the flag at its default.
    """
    tag = normalize_tag(tag)
    category = normalize_tag_category(classification)
    if not tag or category == TAG_CATEGORY_UNRESOLVED or not explanation or not languages:
        raise ValueError("AI Classification Record 无效")
    if grounding_provenance is not None and not isinstance(grounding_provenance, dict):
        raise ValueError("Grounding provenance 必须是对象")
    provenance_json = json.dumps(
        grounding_provenance or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT classification, source FROM tag_classification_cache WHERE normalized_tag = ?", (tag,)
            )
            current = await cursor.fetchone()
            if current:
                current_category = normalize_tag_category(current[0])
                if current[1] == "manual":
                    await db.rollback()
                    return False
                if preserve_resolved and current_category != TAG_CATEGORY_UNRESOLVED:
                    await db.rollback()
                    return False
            else:
                await db.execute(
                    "INSERT INTO tag_classification_cache (normalized_tag, classification, source, updated_at) VALUES (?, ?, 'ai', CURRENT_TIMESTAMP)",
                    (tag, TAG_CATEGORY_UNRESOLVED),
                )
            await db.execute(
                """INSERT INTO ai_tag_classification_records
                   (tag, classification, explanation, languages, grounding_provenance,
                    invalidated_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, NULL, CURRENT_TIMESTAMP)
                   ON CONFLICT(tag) DO UPDATE SET classification=excluded.classification,
                   explanation=excluded.explanation, languages=excluded.languages,
                   grounding_provenance=excluded.grounding_provenance,
                   invalidated_at=NULL, updated_at=CURRENT_TIMESTAMP""",
                (tag, category, explanation, languages, provenance_json),
            )
            await db.execute(
                "UPDATE tag_classification_cache SET classification = ?, source = 'ai', updated_at = CURRENT_TIMESTAMP WHERE normalized_tag = ?",
                (category, tag),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        return True


async def mark_ai_tag_unresolved(tag: str, *, preserve_resolved: bool = False) -> bool:
    """Keep a failed Grounded Judge result in the human review queue.

    With ``preserve_resolved`` (Native Jev), a human decision or an explicit AI
    classification written while this operation was in flight stays untouched:
    the status is only downgraded when the current row is still unresolved.
    """
    tag = normalize_tag(tag)
    if not tag:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT classification, source FROM tag_classification_cache WHERE normalized_tag = ?", (tag,)
            )
            current = await cursor.fetchone()
            if current:
                if current[1] == "manual":
                    await db.rollback()
                    return False
                if preserve_resolved and normalize_tag_category(current[0]) != TAG_CATEGORY_UNRESOLVED:
                    await db.rollback()
                    return False
            await db.execute(
                """INSERT INTO tag_classification_cache (normalized_tag, classification, source, updated_at)
                   VALUES (?, ?, 'ai', CURRENT_TIMESTAMP)
                   ON CONFLICT(normalized_tag) DO UPDATE SET classification=excluded.classification,
                   source=excluded.source, updated_at=CURRENT_TIMESTAMP""",
                (tag, TAG_CATEGORY_UNRESOLVED),
            )
            await db.execute(
                "UPDATE ai_tag_classification_records "
                "SET invalidated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE tag = ? AND invalidated_at IS NULL",
                (tag,),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return True


async def get_ai_tag_classification_record(tag: str) -> dict | None:
    tag = normalize_tag(tag)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT tag, classification, explanation, languages, grounding_provenance "
            "FROM ai_tag_classification_records "
            "WHERE tag = ? AND invalidated_at IS NULL", (tag,)
        )
        row = await cursor.fetchone()
    if not row:
        return None
    record = dict(zip(
        ("tag", "classification", "explanation", "languages", "grounding_provenance"), row
    ))
    try:
        record["grounding_provenance"] = json.loads(record["grounding_provenance"] or "{}")
    except (json.JSONDecodeError, TypeError):
        record["grounding_provenance"] = {}
    return record


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
            "SELECT 1 FROM (SELECT illust_id FROM exposure_ids UNION SELECT illust_id FROM push_history) WHERE illust_id = ?", (illust_id,)
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
            f"SELECT illust_id FROM (SELECT illust_id FROM exposure_ids UNION SELECT illust_id FROM push_history) WHERE illust_id IN ({placeholders})",
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
        await db.execute(
            "INSERT OR IGNORE INTO exposure_ids (illust_id, first_seen_at) VALUES (?, CURRENT_TIMESTAMP)",
            (illust_id,),
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
    from persistent_profile import replace_projection
    await asyncio.to_thread(replace_projection, DB_PATH, profile)


async def adjust_tag_weight(tag: str, delta: float):
    """Compatibility write: fixed-unit correction recorded in the ledger."""
    from persistent_profile import replace_projection
    await asyncio.to_thread(replace_projection, DB_PATH, tag=tag, delta=delta)


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


def _parse_feedback_tags(raw) -> list[str]:
    """Parse retained tag JSON into a de-duplicated list of non-empty strings."""
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            continue
        tag = value.strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


async def get_feedback_metadata(illust_id: int) -> dict | None:
    """Read retained feedback metadata without touching cache or the ledger.

    Returns None when the work has no retained row or no usable tag, so callers
    cannot mistake an empty tag list for a valid feedback target.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT illust_id, tags, user_id, user_name FROM feedback_metadata WHERE illust_id = ?",
            (illust_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    tags = _parse_feedback_tags(row[1])
    if not tags:
        return None
    return {
        "illust_id": row[0],
        "tags": tags,
        "user_id": row[2] or 0,
        "user_name": row[3] or "",
    }


async def get_recent_liked_tags(limit: int = 10) -> list[str]:
    """
    获取近期喜欢的作品的标签 (用于 AI 评分)
    
    从 feedback 关联 illust_cache 获取标签
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT c.tags FROM feedback f
            JOIN feedback_metadata c ON f.illust_id = c.illust_id
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
            JOIN feedback_metadata c ON f.illust_id = c.illust_id
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
            INNER JOIN feedback_metadata AS c ON c.illust_id = f.illust_id
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
            INNER JOIN feedback_metadata AS c ON c.illust_id = f.illust_id
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
        await _retain_before_cleanup(db)
        await db.execute(
            "DELETE FROM illust_cache WHERE illust_id = ?", (illust_id,)
        )
        await db.commit()


async def cleanup_old_illust_cache(days: int = 30) -> int:
    """清理 N 天前的旧缓存记录"""
    cutoff = datetime.now() - timedelta(days=days)
    async with aiosqlite.connect(DB_PATH) as db:
        await _retain_before_cleanup(db)
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
    from persistent_profile import reset_path
    await asyncio.to_thread(reset_path, DB_PATH)


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


# ============ 历史种子轮换 ============
# Epoch seconds, UTC. The seed pool is long-lived bookmark/feedback evidence;
# this state table only records rotation leases and cooldowns.
RELATED_SEED_MAX_PER_ROUND = 2
RELATED_SEED_CLAIM_LEASE_SECONDS = 600
RELATED_SEED_SUCCESS_COOLDOWN_SECONDS = 7 * 24 * 3600
RELATED_SEED_FAILURE_BASE_SECONDS = 6 * 3600
RELATED_SEED_FAILURE_CAP_SECONDS = 7 * 24 * 3600
RELATED_SEED_THROTTLE_COOLDOWN_SECONDS = 3600
RELATED_SEED_OUTCOMES = frozenset({"success", "empty", "error", "throttled", "unused"})

_RELATED_SEED_SELECT_SQL = """
WITH pool AS (
    SELECT illust_id FROM preference_bookmarks WHERE illust_id > 0
    UNION SELECT illust_id FROM xp_bookmarks WHERE illust_id > 0
    UNION SELECT illust_id FROM feedback WHERE action = 'like' AND illust_id > 0
    UNION SELECT illust_id FROM preference_feedback WHERE action = 'like' AND illust_id > 0
),
disliked AS (
    SELECT illust_id FROM feedback WHERE action = 'dislike' AND illust_id > 0
    UNION SELECT illust_id FROM preference_feedback WHERE action = 'dislike' AND illust_id > 0
),
known_authors AS (
    SELECT illust_id, user_id FROM feedback_metadata WHERE user_id > 0
    UNION SELECT illust_id, user_id FROM illust_cache WHERE user_id > 0
    UNION SELECT illust_id, artist_id FROM preference_feedback
          WHERE artist_id IS NOT NULL AND artist_id > 0
)
SELECT p.illust_id
FROM pool AS p
LEFT JOIN related_seed_state AS s ON s.illust_id = p.illust_id
WHERE p.illust_id NOT IN (SELECT illust_id FROM disliked)
  AND NOT EXISTS (
      SELECT 1 FROM known_authors AS a
      JOIN blocked_artists AS b ON b.artist_id = a.user_id
      WHERE a.illust_id = p.illust_id
  )
  AND (
      s.illust_id IS NULL
      OR (
          COALESCE(s.cooldown_until, 0) <= :now
          AND (s.claim_token IS NULL OR COALESCE(s.claim_until, 0) <= :now)
      )
  )
ORDER BY COALESCE(s.last_attempt_at, 0) ASC, p.illust_id ASC
LIMIT :max_seeds
"""


async def claim_related_seeds(
    max_seeds: int = RELATED_SEED_MAX_PER_ROUND,
    now: float | None = None,
    lease_seconds: int = RELATED_SEED_CLAIM_LEASE_SECONDS,
) -> list[dict]:
    """Claim up to ``max_seeds`` eligible seeds in one BEGIN IMMEDIATE transaction.

    Returns ``[{"illust_id": int, "claim_token": str}, ...]``. Every claim gets a
    unique lease token so a late ``finish_related_seed`` call cannot overwrite a
    newer lease. Invalid budgets (<=0 or above 2) are clamped, never expanded.
    """
    try:
        budget = min(int(max_seeds), RELATED_SEED_MAX_PER_ROUND)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0:
        return []

    current = time.time() if now is None else float(now)
    lease = max(1, int(lease_seconds))
    claims: list[dict] = []

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                _RELATED_SEED_SELECT_SQL,
                {"now": current, "max_seeds": budget},
            )
            rows = await cursor.fetchall()
            for row in rows:
                illust_id = int(row[0])
                token = uuid.uuid4().hex
                await db.execute(
                    """
                    INSERT INTO related_seed_state (
                        illust_id, state, cooldown_until, failure_count,
                        last_attempt_at, claim_token, claim_until, updated_at
                    ) VALUES (?, 'claimed', 0, 0, 0, ?, ?, ?)
                    ON CONFLICT(illust_id) DO UPDATE SET
                        state = 'claimed',
                        claim_token = excluded.claim_token,
                        claim_until = excluded.claim_until,
                        updated_at = excluded.updated_at
                    """,
                    (illust_id, token, current + lease, current),
                )
                claims.append({"illust_id": illust_id, "claim_token": token})
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    return claims


async def finish_related_seed(
    illust_id: int,
    claim_token: str,
    outcome: str,
    now: float | None = None,
) -> bool:
    """Release a seed lease and record its cooldown.

    The UPDATE is guarded by ``claim_token``; a stale worker whose lease was
    already replaced gets ``False`` and changes nothing. ``unused`` releases the
    lease without treating the seed as attempted.
    """
    if outcome not in RELATED_SEED_OUTCOMES:
        raise ValueError(f"unknown related seed outcome: {outcome!r}")
    if not isinstance(illust_id, int) or isinstance(illust_id, bool) or illust_id <= 0:
        return False
    if not isinstance(claim_token, str) or not claim_token:
        return False

    current = time.time() if now is None else float(now)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if outcome == "unused":
                cursor = await db.execute(
                    """
                    UPDATE related_seed_state
                    SET claim_token = NULL, claim_until = NULL, updated_at = ?
                    WHERE illust_id = ? AND claim_token = ?
                    """,
                    (current, illust_id, claim_token),
                )
                await db.commit()
                return cursor.rowcount > 0

            cursor = await db.execute(
                "SELECT failure_count FROM related_seed_state WHERE illust_id = ? AND claim_token = ?",
                (illust_id, claim_token),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return False

            failure_count = int(row[0] or 0)
            if outcome == "success":
                failure_count = 0
                cooldown_until = current + RELATED_SEED_SUCCESS_COOLDOWN_SECONDS
            elif outcome == "throttled":
                cooldown_until = current + RELATED_SEED_THROTTLE_COOLDOWN_SECONDS
            else:
                failure_count += 1
                exponent = min(failure_count - 1, 16)
                delay = min(
                    RELATED_SEED_FAILURE_BASE_SECONDS * (2 ** exponent),
                    RELATED_SEED_FAILURE_CAP_SECONDS,
                )
                cooldown_until = current + delay

            cursor = await db.execute(
                """
                UPDATE related_seed_state
                SET state = ?, cooldown_until = ?, failure_count = ?,
                    last_attempt_at = ?, claim_token = NULL, claim_until = NULL,
                    updated_at = ?
                WHERE illust_id = ? AND claim_token = ?
                """,
                (
                    outcome,
                    cooldown_until,
                    failure_count,
                    current,
                    current,
                    illust_id,
                    claim_token,
                ),
            )
            await db.commit()
            return cursor.rowcount > 0
        except BaseException:
            await db.rollback()
            raise


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
            JOIN feedback_metadata ic ON f.illust_id = ic.illust_id
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


async def get_current_user_embedding(
    user_id: int,
    model: str,
    profile_hash: str,
) -> Optional[list[float]]:
    """Return a model-compatible, current cached Preference Profile vector."""
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            """
            SELECT embedding
            FROM user_embedding
            WHERE user_id = ? AND model = ? AND profile_hash = ?
            """,
            (user_id, model, profile_hash),
        ).fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                logger.warning("忽略损坏的用户画像 Embedding 缓存: user_id=%s", user_id)
        return None


async def get_vector_exploration_pool(
    model: str,
    limit: int,
    exclude_ids: Optional[set[int]] = None,
) -> list[tuple[int, list[float]]]:
    """Load a bounded recent cache slice; similarity is computed in-process."""
    bounded_limit = max(0, min(int(limit), 10000))
    if bounded_limit == 0:
        return []
    excluded = set(exclude_ids or ())
    # Fetch a small bounded surplus so current-run duplicates do not consume
    # the configured pool. Pushed works are never eligible for retrieval.
    query_limit = min(10000, bounded_limit + len(excluded))
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute(
            """
            SELECT ie.illust_id, ie.embedding
            FROM illust_embeddings AS ie
            WHERE ie.model = ?
              AND NOT EXISTS (
                  SELECT 1 FROM (SELECT illust_id FROM exposure_ids UNION SELECT illust_id FROM push_history) AS ph
                  WHERE ph.illust_id = ie.illust_id
              )
            ORDER BY ie.created_at DESC, ie.illust_id DESC
            LIMIT ?
            """,
            (model, query_limit),
        ).fetchall()
    result = []
    for illust_id, raw_embedding in rows:
        if illust_id in excluded or not raw_embedding:
            continue
        try:
            result.append((illust_id, json.loads(raw_embedding)))
        except (TypeError, json.JSONDecodeError):
            logger.warning("忽略损坏的作品 Embedding 缓存: illust_id=%s", illust_id)
        if len(result) >= bounded_limit:
            break
    return result


async def start_vector_exploration_run(
    *,
    run_id: str,
    user_id: int,
    model: str,
    profile_hash: str,
    pool_limit: int,
    pool_size: int,
    candidate_limit: int,
    similarity_threshold: float,
    duplicate_threshold: float,
    profile_concentration: float,
) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO exploration_vector_runs (
                run_id, user_id, model, profile_hash, pool_limit, pool_size,
                candidate_limit, similarity_threshold, duplicate_threshold,
                profile_concentration
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, user_id, model, profile_hash, pool_limit, pool_size,
                candidate_limit, similarity_threshold, duplicate_threshold,
                profile_concentration,
            ),
        )
        db.commit()


async def record_vector_exploration_candidates(
    run_id: str,
    candidates: list[dict],
) -> None:
    if not candidates:
        return
    with sqlite3.connect(DB_PATH) as db:
        db.executemany(
            """
            INSERT INTO exploration_vector_candidates (
                run_id, illust_id, source, similarity, model, retrieval_rank, tags
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    item["illust_id"],
                    item["source"],
                    item["similarity"],
                    item["model"],
                    item["retrieval_rank"],
                    json.dumps(item.get("tags") or [], ensure_ascii=False),
                )
                for item in candidates
            ],
        )
        db.commit()


async def complete_vector_exploration_run(
    run_id: str,
    *,
    ranked_ids: list[int],
    selected_ids: set[int],
    slate_profile_concentration: float,
    duplicate_semantic_rate: float,
) -> None:
    final_ranks = {illust_id: rank for rank, illust_id in enumerate(ranked_ids, 1)}
    with sqlite3.connect(DB_PATH) as db:
        db.executemany(
            """
            UPDATE exploration_vector_candidates
            SET final_rank = ?, selected = ?
            WHERE run_id = ? AND illust_id = ?
            """,
            [
                (final_ranks.get(illust_id), int(illust_id in selected_ids), run_id, illust_id)
                for illust_id in final_ranks
            ],
        )
        db.execute(
            """
            UPDATE exploration_vector_runs
            SET status = 'completed', slate_profile_concentration = ?,
                duplicate_semantic_rate = ?, completed_at = CURRENT_TIMESTAMP
            WHERE run_id = ?
            """,
            (slate_profile_concentration, duplicate_semantic_rate, run_id),
        )
        db.commit()


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


async def delete_illust_embedding(illust_id: int) -> None:
    """Remove a work vector that can no longer be hydrated from Pixiv."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM illust_embeddings WHERE illust_id = ?", (illust_id,)
        )
        await db.commit()


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
    """只移除兼容投影，保留持久观察，解除屏蔽后可重新投影。"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('BEGIN IMMEDIATE')
        await db.execute("""
            DELETE FROM preference_projection
            WHERE tag IN (SELECT tag FROM blocked_tags)
        """)
        cursor = await db.execute("""
            DELETE FROM xp_profile 
            WHERE tag IN (SELECT tag FROM blocked_tags)
        """)
        await db.commit()
        return cursor.rowcount


async def cleanup_old_sent_history(days: int = 30) -> int:
    """清理 N 天前的推送历史记录，返回删除数量"""
    async with aiosqlite.connect(DB_PATH) as db:
        await _retain_before_cleanup(db)
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
        from tag_mapping import TagIdentityResolver
        aliases = await db.execute("SELECT original_tag,normalized_tag FROM tag_aliases WHERE kind='equivalent'")
        resolver = TagIdentityResolver(dict(await aliases.fetchall()))
        result = {}
        for tag, weight in rows:
            canonical = resolver.resolve(tag)
            result[canonical] = result.get(canonical, 0) + weight
        return result


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
    profile = await get_negative_profile()
    return sorted(profile.items(), key=lambda item: item[1], reverse=True)[:limit]


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

"""
SQLite 数据层
"""
import json
import aiosqlite
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "pixiv_xp.db"


async def init_db():
    """初始化数据库表结构"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    async with aiosqlite.connect(DB_PATH) as db:
        # ============ 简易迁移逻辑 ============
        # 检查 xp_bookmarks 表是否包含 user_id 列 (旧版没有)
        try:
             await db.execute("SELECT user_id FROM xp_bookmarks LIMIT 0")
        except Exception:
             await db.execute("DROP TABLE IF EXISTS xp_bookmarks")
             await db.commit()
             await db.execute("DROP TABLE IF EXISTS xp_profile")
             await db.execute("DROP TABLE IF EXISTS xp_tag_pairs")
             await db.commit()
        
        # 检查 illust_cache 表是否包含 user_id 列 (v2 新增)
        try:
             await db.execute("SELECT user_id FROM illust_cache LIMIT 0")
        except Exception:
             # 旧表只有 tags，删除重建
             await db.execute("DROP TABLE IF EXISTS illust_cache")
             await db.commit()
        
        await db.executescript("""
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
            -- 标签映射统计表 (用于反查最佳搜索词)
            CREATE TABLE IF NOT EXISTS tag_mapping_stats (
                normalized_tag TEXT,
                original_tag TEXT,
                frequency INTEGER DEFAULT 0,
                PRIMARY KEY (normalized_tag, original_tag)
            );
            
            -- AI 处理结果缓存 (Tag -> CleanedTag/NULL)
            CREATE TABLE IF NOT EXISTS ai_tag_cache (
                original_tag TEXT PRIMARY KEY,
                cleaned_tag TEXT,  -- NULL 表示被过滤(meaningless)
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
        """)
        
        await db.commit()


# ============ Tag 翻译操作 ============
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
    keyword = f"%{keyword}%"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT name, translated_name 
            FROM tag_translations 
            WHERE name LIKE ? OR translated_name LIKE ?
            LIMIT ?
        """, (keyword, keyword, limit))
        return await cursor.fetchall()


# ============ 基础操作 ============

async def is_illust_pushed(illust_id: int) -> bool:
    """检查是否已推送过"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM push_history WHERE illust_id = ?",
            (illust_id,)
        )
        return await cursor.fetchone() is not None


async def mark_pushed(illust_id: int, source: str = "search"):
    """标记为已推送"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO push_history (illust_id, source) VALUES (?, ?)",
            (illust_id, source)
        )
        await db.commit()


async def get_feedback(illust_id: int) -> Optional[str]:
    """获取反馈状态"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT action FROM feedback WHERE illust_id = ?",
            (illust_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def record_feedback(illust_id: int, action: str):
    """记录反馈"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO feedback (illust_id, action) VALUES (?, ?)",
            (illust_id, action)
        )
        await db.commit()


async def cache_illust_info(illust_id: int, tags: list[str], user_id: int = None, user_name: str = None):
    """缓存作品信息 (v2: 增加画师信息)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO illust_cache (illust_id, tags, user_id, user_name) VALUES (?, ?, ?, ?)",
            (illust_id, json.dumps(tags), user_id, user_name)
        )
        await db.commit()


async def get_illust_info(illust_id: int):
    """获取缓存的作品信息"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT tags, user_id, user_name FROM illust_cache WHERE illust_id = ?",
            (illust_id,)
        )
        row = await cursor.fetchone()
        if row:
            try:
                tags = json.loads(row['tags'])
            except:
                tags = []
            return {
                "tags": tags,
                "user_id": row['user_id'],
                "user_name": row['user_name']
            }
        return None

# ============ 标签黑名单操作 ============

async def block_tag(tag: str):
    """添加标签到黑名单"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO tag_blacklist (tag, dislike_count) VALUES (?, 1)",
            (tag,)
        )
        await db.execute(
            "UPDATE tag_blacklist SET dislike_count = dislike_count + 1 WHERE tag = ?",
            (tag,)
        )
        
        # 同时添加到快速屏蔽表
        await db.execute(
            "INSERT OR IGNORE INTO blocked_tags (tag) VALUES (?)",
            (tag,)
        )
        
        await db.commit()

async def mute_tag(tag: str, hours: int = 24) -> str:
    """暂时屏蔽标签 (Mute)"""
    until_ts = datetime.now() + timedelta(hours=hours)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO muted_tags (tag, until_ts) VALUES (?, ?)",
            (tag, until_ts)
        )
        await db.commit()
    return until_ts.strftime("%Y-%m-%d %H:%M")

async def unmute_tag(tag: str):
    """取消暂时屏蔽"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM muted_tags WHERE tag = ?", (tag,))
        await db.commit()

async def get_muted_tags() -> list[str]:
    """获取当前生效的静音标签"""
    now = datetime.now()
    async with aiosqlite.connect(DB_PATH) as db:
        # 清理过期
        await db.execute("DELETE FROM muted_tags WHERE until_ts < ?", (now,))
        await db.commit()
        
        cursor = await db.execute("SELECT tag FROM muted_tags")
        return [row[0] for row in await cursor.fetchall()]

async def get_blocked_tags() -> set[str]:
    """获取所有屏蔽标签 (包括自动和手动的)"""
    async with aiosqlite.connect(DB_PATH) as db:
        # 自动黑名单 (dislike > 2)
        cursor = await db.execute("SELECT tag FROM tag_blacklist WHERE dislike_count >= 3")
        auto_blocked = {row[0] for row in await cursor.fetchall()}
        
        # 手动黑名单
        cursor = await db.execute("SELECT tag FROM blocked_tags")
        manual_blocked = {row[0] for row in await cursor.fetchall()}
        
        return auto_blocked | manual_blocked

async def remove_blocked_tag(tag: str):
    """移除屏蔽标签"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tag_blacklist WHERE tag = ?", (tag,))
        await db.execute("DELETE FROM blocked_tags WHERE tag = ?", (tag,))
        await db.commit()

# ============ 画师黑名单操作 ============

async def block_artist(artist_id: int, artist_name: str = None):
    """屏蔽画师"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO blocked_artists (artist_id, artist_name) VALUES (?, ?)",
            (artist_id, artist_name)
        )
        await db.commit()

async def unblock_artist(artist_id: int):
    """取消屏蔽画师"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM blocked_artists WHERE artist_id = ?", (artist_id,))
        await db.commit()

async def get_blocked_artists() -> set[int]:
    """获取所有屏蔽的画师ID"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT artist_id FROM blocked_artists")
        return {row[0] for row in await cursor.fetchall()}

# ============ XP画像操作 ============

async def update_xp_profile(tag: str, weight_delta: float):
    """更新 XP 权重 (原子操作)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO xp_profile (tag, weight) VALUES (?, ?)
            ON CONFLICT(tag) DO UPDATE SET 
                weight = weight + ?,
                updated_at = CURRENT_TIMESTAMP
        """, (tag, weight_delta, weight_delta))
        await db.commit()


async def get_xp_profile(limit: int = 20) -> list[tuple[str, float]]:
    """获取 Top XP 标签"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT tag, weight FROM xp_profile ORDER BY weight DESC LIMIT ?",
            (limit,)
        )
        return await cursor.fetchall()


# ============ 组合 XP 操作 (Tag Pairs) ============

async def update_tag_pair_weight(tag1: str, tag2: str, weight_delta: float):
    """更新 Tag 组合权重 (确保 tag1 < tag2)"""
    if tag1 > tag2:
        tag1, tag2 = tag2, tag1
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO xp_tag_pairs (tag1, tag2, weight) VALUES (?, ?, ?)
            ON CONFLICT(tag1, tag2) DO UPDATE SET 
                weight = weight + ?
        """, (tag1, tag2, weight_delta, weight_delta))
        await db.commit()

async def get_top_tag_pairs(limit: int = 10) -> list[tuple[str, str, float]]:
    """获取 Top Tag 组合"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT tag1, tag2, weight FROM xp_tag_pairs ORDER BY weight DESC LIMIT ?",
            (limit,)
        )
        return await cursor.fetchall()


# ============ 收藏同步操作 ============

async def save_bookmarks_snapshot(illusts: list[dict]):
    """
    保存收藏快照 (全量更新模式，但保留历史)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        for illust in illusts:
            await db.execute(
                "INSERT OR REPLACE INTO bookmarks (illust_id) VALUES (?)",
                (illust['id'],)
            )
        await db.commit()


async def get_uncached_bookmarks() -> list[int]:
    """获取未在本地缓存的收藏ID"""
    async with aiosqlite.connect(DB_PATH) as db:
        # 找出在 bookmarks 表但不在 illust_cache 表的 ID
        cursor = await db.execute("""
            SELECT b.illust_id FROM bookmarks b
            LEFT JOIN illust_cache c ON b.illust_id = c.illust_id
            WHERE c.illust_id IS NULL
        """)
        return [row[0] for row in await cursor.fetchall()]

# ============ 用户 XP 分析 (XP Bookmarks) ============
async def save_xp_bookmarks(bookmarks: list[dict]):
    """保存用于XP分析的收藏数据"""
    async with aiosqlite.connect(DB_PATH) as db:
        for b in bookmarks:
            tags_json = json.dumps([t['name'] for t in b.get('tags', [])])
            # parse create date
            try:
                create_date = datetime.strptime(b['create_date'], "%Y-%m-%dT%H:%M:%S%z")
            except:
                create_date = datetime.now()
                
            await db.execute("""
                INSERT OR REPLACE INTO xp_bookmarks 
                (illust_id, user_id, tags, illust_create_date)
                VALUES (?, ?, ?, ?)
            """, (b['id'], b.get('user', {}).get('id'), tags_json, create_date))
        await db.commit()

async def get_all_xp_bookmarks():
    """获取所有用于分析的收藏数据"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT illust_id, tags, illust_create_date FROM xp_bookmarks")
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            try:
                tags = json.loads(row['tags'])
            except:
                tags = []
            result.append({
                "id": row['illust_id'],
                "tags": tags,
                "create_date": row['illust_create_date']
            })
        return result

# ============ 系统状态管理 ============
async def set_system_state(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value)
        )
        await db.commit()

async def get_system_state(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM system_state WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None


# ============ 统计报表 ============
async def get_push_stats(days: int = 7) -> dict:
    """获取推送统计数据"""
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
    """重置所有XP数据 (慎用)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM xp_profile")
        await db.execute("DELETE FROM xp_tag_pairs")
        await db.execute("DELETE FROM feedback")
        await db.execute("DELETE FROM push_history")
        await db.commit()

async def get_top_xp_tags(limit: int = 15) -> list[tuple[str, float]]:
    """获取 Top XP 标签"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT tag, weight FROM xp_profile ORDER BY weight DESC LIMIT ?",
            (limit,)
        )
        return await cursor.fetchall()

async def get_all_strategy_stats() -> dict:
    """获取所有策略的统计数据"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM strategy_stats")
        rows = await cursor.fetchall()
        
        stats = {}
        for row in rows:
            success = row['success_count']
            total = row['total_count']
            rate = success / total if total > 0 else 0.0
            stats[row['strategy']] = {
                "success": success,
                "total": total,
                "rate": rate
            }
        return stats

async def update_strategy_stats(strategy: str, success: bool):
    """更新策略统计"""
    async with aiosqlite.connect(DB_PATH) as db:
        # 先尝试插入
        await db.execute(
            "INSERT OR IGNORE INTO strategy_stats (strategy) VALUES (?)",
            (strategy,)
        )
        # 更新
        if success:
            await db.execute(
                "UPDATE strategy_stats SET success_count = success_count + 1, total_count = total_count + 1, updated_at = CURRENT_TIMESTAMP WHERE strategy = ?",
                (strategy,)
            )
        else:
            await db.execute(
                "UPDATE strategy_stats SET total_count = total_count + 1, updated_at = CURRENT_TIMESTAMP WHERE strategy = ?",
                (strategy,)
            )
        await db.commit()

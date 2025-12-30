"""
SQLite 数据层
"""
import json
import aiosqlite
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path(__file__).parent / "data" / "pixiv_xp.db"


async def init_db():
    """初始化数据库表结构"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    async with aiosqlite.connect(DB_PATH) as db:
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
            
            -- 作品缓存(用于反馈处理)
            CREATE TABLE IF NOT EXISTS illust_cache (
                illust_id INTEGER PRIMARY KEY,
                tags TEXT,  -- JSON数组
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
        """)
        await db.commit()

async def get_ai_cache_map() -> dict[str, str | None]:
    """获取所有 AI 处理缓存"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT original_tag, cleaned_tag FROM ai_tag_cache")
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

async def update_ai_cache(cache_data: dict[str, str | None]):
    """批量更新 AI 处理缓存"""
    if not cache_data:
        return
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR REPLACE INTO ai_tag_cache (original_tag, cleaned_tag) VALUES (?, ?)",
            [(k, v) for k, v in cache_data.items()]
        )
        await db.commit()

async def update_tag_mapping_stats(mappings: dict[str, str]):
    """
    更新标签映射统计
    mappings: {original_tag: normalized_tag}
    """
    async with aiosqlite.connect(DB_PATH) as db:
        for original, normalized in mappings.items():
            await db.execute("""
                INSERT INTO tag_mapping_stats (normalized_tag, original_tag, frequency)
                VALUES (?, ?, 1)
                ON CONFLICT(normalized_tag, original_tag) 
                DO UPDATE SET frequency = frequency + 1
            """, (normalized, original))
        await db.commit()

async def get_best_search_tag(normalized_tag: str) -> str:
    """
    获取某标准化标签对应的最高频原始标签
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT original_tag FROM tag_mapping_stats
            WHERE normalized_tag = ?
            ORDER BY frequency DESC
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


async def mark_pushed(illust_id: int, source: str):
    """记录推送"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO push_history (illust_id, source) VALUES (?, ?)",
            (illust_id, source)
        )
        await db.commit()


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


async def get_liked_illusts() -> set[int]:
    """获取所有被点赞的作品ID"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT illust_id FROM feedback WHERE action = 'like'"
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


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
import json

async def cache_illust(illust_id: int, tags: list[str]):
    """缓存作品信息"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO illust_cache (illust_id, tags, created_at) VALUES (?, ?, ?)",
            (illust_id, json.dumps(tags), datetime.now())
        )
        await db.commit()


async def get_cached_illust_tags(illust_id: int) -> list[str] | None:
    """获取缓存的作品tags"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT tags FROM illust_cache WHERE illust_id = ?", (illust_id,)
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return None


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
            SELECT ic.artist_id, COUNT(*) as cnt 
            FROM push_history ph
            JOIN illust_cache ic ON ph.illust_id = ic.illust_id
            WHERE ph.pushed_at > ?
            GROUP BY ic.artist_id
            ORDER BY cnt DESC
            LIMIT 5
        """, (since,))
        top_artists = [(row['artist_id'], row['cnt']) for row in await cursor.fetchall()]
        
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
                for tag in tags[:5]:  # 只统计前5个标签
                    tag_count[tag] = tag_count.get(tag, 0) + 1
            except:
                pass
        
        top_tags = sorted(tag_count.items(), key=lambda x: x[1], reverse=True)[:5]
        
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
    2. 标签映射统计 (tag_mapping_stats)
    3. 系统状态中的处理进度 (system_state)
    
    保留：
    1. 推送历史 (push_history)
    2. 用户反馈 (feedback)
    3. 黑名单 (tag_blacklist)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # 清除画像数据
        await db.execute("DELETE FROM xp_profile")
        await db.execute("DELETE FROM xp_tag_pairs")
        
        # 清除 AI 映射统计
        await db.execute("DELETE FROM tag_mapping_stats")
        
        # 顺便清除 AI 错误日志
        await db.execute("DELETE FROM ai_error_logs")
        
        # 注意：不清除 system_state 中的同步进度
        # 这样 Profiler 会跳过 Pixiv API 抓取，直接从 xp_bookmarks 读取缓存进行重分析
        
        await db.commit()


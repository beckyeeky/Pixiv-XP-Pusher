"""
工具函数模块
- 日志配置
- 异步限流器
- 重试装饰器
- 图片URL处理
- 标签自动扩展
"""
import asyncio
import logging
import random
import time
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

import aiohttp

TAG_TRANSLATIONS = {
    # Visual Traits
    "white_hair": "白髪 OR 銀髪 OR white_hair",
    "silver_hair": "銀髪 OR 白髪",
    "grey_hair": "灰髪",
    "black_hair": "黒髪",
    "blonde_hair": "金髪",
    "red_hair": "赤髪",
    "blue_hair": "青髪",
    "pink_hair": "ピンク髪",
    "green_hair": "緑髪",
    "purple_hair": "紫髪",
    "brown_hair": "茶髪",
    "long_hair": "ロングヘア OR 長髪",
    "short_hair": "ショートヘア OR 短髪",
    "twintails": "ツインテール",
    "ponytail": "ポニーテール",
    
    # Body & Cloth
    "large_breasts": "巨乳",
    "flat_chest": "貧乳",
    "maid": "メイド",
    "swimsuit": "水着",
    "school_uniform": "セーラー服 OR 制服 OR ブレザー",
    "pantyhose": "パンスト OR ストッキング",
    "thighhighs": "ニーソ OR ニーソックス",
    "glasses": "眼鏡 OR メガネ",
    "kimono": "着物 OR 浴衣",
    "bunny_suit": "バニー OR バニーガール",
    "cat_ears": "猫耳 OR ネコミミ",
    
    # Popular IP
    "genshin_impact": "原神 OR GenshinImpact",
    "原神": "原神 OR GenshinImpact",
    
    "blue_archive": "ブルーアーカイブ OR BlueArchive OR 碧蓝档案",
    "ブルーアーカイブ": "ブルーアーカイブ OR BlueArchive OR 碧蓝档案",
    "ブルアカ": "ブルーアーカイブ OR BlueArchive OR 碧蓝档案",
    
    "arknights": "アークナイツ OR Arknights OR 明日方舟",
    "アークナイツ": "アークナイツ OR Arknights OR 明日方舟",
    "明日方舟": "アークナイツ OR Arknights OR 明日方舟",
    
    "fate_grand_order": "FGO OR Fate/GrandOrder",
    "azur_lane": "アズールレーン",
    "hololive": "ホロライブ",
    
    # Elements
    "scenery": "風景",
    "cyberpunk": "サイバーパンク",
    "steampunk": "スチームパンク",
    "fantasy": "ファンタジー",
}

def expand_search_query(tag: str) -> str:
    """
    将标准化的英文标签扩展为适合 Pixiv 搜索的查询字符串 (包含日文别名)
    并且自动加上括号以防逻辑错误: (A OR B) AND C
    """
    expanded = TAG_TRANSLATIONS.get(tag, tag)
    if " OR " in expanded:
        return f"({expanded})"
    return expanded


def setup_logging(log_dir: Path = Path("logs")):
    """配置日志（分级、文件轮转）"""
    log_dir.mkdir(exist_ok=True)
    
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # 文件Handler（轮转，最大5MB，保留3份）
    file_handler = RotatingFileHandler(
        log_dir / "pixiv_xp.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    
    # 控制台Handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    
    # 根Logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    # 屏蔽第三方库的废话
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    
    return root_logger


class AsyncRateLimiter:
    """
    令牌桶限流器
    """
    
    def __init__(self, requests_per_minute: int = 60, random_delay: tuple[float, float] = (1.0, 3.0)):
        self.rate = requests_per_minute / 60.0  # 每秒令牌数
        self.max_tokens = requests_per_minute
        self.tokens = self.max_tokens
        self.last_update = time.monotonic()
        self.lock = asyncio.Lock()
        self.random_delay = random_delay
    
    async def acquire(self):
        """获取令牌"""
        async with self.lock:
            now = time.monotonic()
            # 补充令牌
            elapsed = now - self.last_update
            self.tokens = min(self.max_tokens, self.tokens + elapsed * self.rate)
            self.last_update = now
            
            if self.tokens < 1:
                # 等待令牌恢复
                wait_time = (1 - self.tokens) / self.rate
                await asyncio.sleep(wait_time)
                self.tokens = 0
            else:
                self.tokens -= 1
            
            # 随机抖动
            if self.random_delay:
                delay = random.uniform(*self.random_delay)
                await asyncio.sleep(delay)
    
    async def __aenter__(self):
        await self.acquire()
        return self
    
    async def __aexit__(self, *args):
        pass


def retry_async(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """
    异步重试装饰器
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries:
                        logging.warning(
                            f"{func.__name__} 失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}"
                        )
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff
            
            raise last_exception
        return wrapper
    return decorator


def get_pixiv_cat_url(illust_id: int, page: int = 0) -> str:
    """
    获取 pixiv.cat 反代图片URL
    """
    if page == 0:
        return f"https://pixiv.cat/{illust_id}.jpg"
    else:
        return f"https://pixiv.cat/{illust_id}-{page + 1}.jpg"


async def download_image_with_referer(
    session: aiohttp.ClientSession,
    url: str,
    semaphore: asyncio.Semaphore | None = None
) -> bytes:
    """
    带Referer下载Pixiv图片
    """
    headers = {
        "Referer": "https://www.pixiv.net/",
        "User-Agent": "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)"
    }
    
    async def _download():
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.read()
    
    if semaphore:
        async with semaphore:
            return await _download()
    return await _download()

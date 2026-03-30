#!/usr/bin/env python3
"""
Sync Danbooru Copyright Tags (IP/Game/Anime titles)
Run manually or via cron: python scripts/sync_ip_tags.py
"""

import requests
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))

from config import load_config

# === Configuration ===
MIN_POST_COUNT = 1000      # Minimum posts to include
LIMIT = 2000               # Max tags to fetch
OUTPUT_FILE = PROJECT_ROOT / "data" / "ip_tags.json"


def load_danbooru_credentials() -> tuple[str, str]:
    """环境变量优先，其次回退到 config.yaml 中的 profiler 配置。"""
    env_login = os.getenv("DANBOORU_LOGIN", "").strip()
    env_api_key = os.getenv("DANBOORU_API_KEY", "").strip()
    if env_login and env_api_key:
        return env_login, env_api_key

    config = load_config(PROJECT_ROOT / "config.yaml")
    profiler_cfg = config.get("profiler", {}) if isinstance(config, dict) else {}
    cfg_login = str(profiler_cfg.get("danbooru_login", "") or "").strip()
    cfg_api_key = str(profiler_cfg.get("danbooru_api_key", "") or "").strip()

    return env_login or cfg_login, env_api_key or cfg_api_key


def fetch_copyright_tags(login: str, api_key: str):
    """Fetch category=3 (copyright) tags from Danbooru API"""
    url = "https://danbooru.donmai.us/tags.json"
    all_tags = []
    page = 1
    
    while len(all_tags) < LIMIT:
        params = {
            "search[category]": "3",
            "search[post_count]": f">{MIN_POST_COUNT}",
            "search[order]": "count",
            "limit": 200,
            "page": page,
            "login": login,
            "api_key": api_key,
        }
        
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            if not data:
                break
            
            for tag in data:
                # Danbooru 使用 : 作为命名空间分隔符，直接去掉以匹配 Pixiv 格式
                # 同时处理可能产生的连续下划线
                # 例如: honkai:_star_rail -> honkai_star_rail
                #       series:_name -> series_name (处理连续下划线)
                normalized_name = tag["name"].replace(":", "").replace("__", "_")
                all_tags.append(normalized_name)
            
            print(f"[Page {page}] Fetched {len(data)} tags")
            page += 1
            
        except Exception as e:
            print(f"[Error] Page {page}: {e}")
            break
    
    return all_tags[:LIMIT]


def main():
    print("=" * 50)
    print("Danbooru Copyright Tags Sync")
    print("=" * 50)

    danbooru_login, danbooru_api_key = load_danbooru_credentials()
    if not danbooru_login or not danbooru_api_key:
        print("\n[Warning] Please set DANBOORU_LOGIN and DANBOORU_API_KEY")
        print("Options:")
        print("  1. Set env vars: export DANBOORU_LOGIN=xxx")
        print("  2. Or fill profiler.danbooru_login / profiler.danbooru_api_key in config.yaml")
        sys.exit(1)

    tags = fetch_copyright_tags(danbooru_login, danbooru_api_key)
    if not tags:
        print("\n[Error] No tags fetched, aborting without overwriting existing file")
        sys.exit(1)
    
    # Ensure output directory exists
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    # Save to JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(tags, f, indent=2, ensure_ascii=False)
    
    print(f"\n[Done] Saved {len(tags)} tags to {OUTPUT_FILE}")
    print(f"\nTop 10 tags:")
    for tag in tags[:10]:
        print(f"  - {tag}")


if __name__ == "__main__":
    main()

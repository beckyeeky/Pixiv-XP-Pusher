#!/usr/bin/env python3
"""
Sync Danbooru Copyright Tags (IP/Game/Anime titles)
Run manually or via cron: python scripts/sync_ip_tags.py
"""

import requests
import json
import os
from pathlib import Path

# === Configuration ===
DANBOORU_LOGIN = os.getenv("DANBOORU_LOGIN", "your_username")
DANBOORU_API_KEY = os.getenv("DANBOORU_API_KEY", "your_api_key")
MIN_POST_COUNT = 1000      # Minimum posts to include
LIMIT = 2000               # Max tags to fetch
OUTPUT_FILE = "data/ip_tags.json"


def fetch_copyright_tags():
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
            "login": DANBOORU_LOGIN,
            "api_key": DANBOORU_API_KEY,
        }
        
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            if not data:
                break
            
            for tag in data:
                all_tags.append(tag["name"])
            
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
    
    if DANBOORU_LOGIN == "your_username":
        print("\n[Warning] Please set DANBOORU_LOGIN and DANBOORU_API_KEY")
        print("Options:")
        print("  1. Edit this script")
        print("  2. Set env vars: export DANBOORU_LOGIN=xxx")
        return
    
    tags = fetch_copyright_tags()
    
    # Ensure output directory exists
    Path(OUTPUT_FILE).parent.mkdir(parents=True, exist_ok=True)
    
    # Save to JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(tags, f, indent=2, ensure_ascii=False)
    
    print(f"\n[Done] Saved {len(tags)} tags to {OUTPUT_FILE}")
    print(f"\nTop 10 tags:")
    for tag in tags[:10]:
        print(f"  - {tag}")


if __name__ == "__main__":
    main()

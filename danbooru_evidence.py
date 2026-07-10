"""Small, on-demand Danbooru tag lookup for profile classification maintenance."""

import aiohttp


_CATEGORY_MAP = {0: "feature", 1: "artist", 3: "copyright", 4: "character", 5: "non_preference"}


class DanbooruEvidenceLookup:
    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.login = cfg.get("login", "")
        self.api_key = cfg.get("api_key", "")
        self.base_url = cfg.get("base_url", "https://danbooru.donmai.us")

    async def lookup(self, tags: list[str]) -> dict[str, list[tuple[str, str, float]]]:
        if not self.enabled or not tags:
            return {}
        params = {"search[name_matches]": ",".join(tags), "limit": len(tags)}
        if self.login and self.api_key:
            params.update({"login": self.login, "api_key": self.api_key})
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{self.base_url.rstrip('/')}/tags.json", params=params) as response:
                response.raise_for_status()
                rows = await response.json()
        result = {}
        for row in rows:
            category = _CATEGORY_MAP.get(row.get("category"))
            name = row.get("name")
            if category and name:
                result.setdefault(name, []).append(("danbooru", category, 1.0))
        return result

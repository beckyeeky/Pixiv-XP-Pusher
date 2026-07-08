"""Lightweight helpers for proxy configuration normalization."""


def normalize_proxy_url(url: str | None) -> str | None:
    """规范化代理地址，兼容空值、字符串 None 和缺少 scheme 的旧配置。"""
    if url is None:
        return None
    if not isinstance(url, str):
        url = str(url)

    normalized = url.strip()
    if not normalized or normalized.lower() == "none":
        return None
    if not normalized.startswith(("http://", "https://", "socks5://", "socks5h://")):
        normalized = f"http://{normalized}"
    return normalized

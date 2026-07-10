"""
Web UI - FastAPI 后端
深色护眼主题，模板化设计
"""
import hashlib
import logging
import secrets
import subprocess
import os
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request, HTTPException, Depends, Form, Query, Response, Body
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import aiohttp

import yaml

import database as db
from config import load_config as shared_load_config
from proxy_utils import normalize_proxy_url
from web.settings_editor import (
    apply_settings_payload,
    build_settings_snapshot,
    redact_sensitive_config,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Pixiv-XP-Pusher")

# 配置路径
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

# 确保目录存在
STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)

# 初始化模板引擎
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# 会话存储（简易实现）
sessions: dict[str, datetime] = {}
login_attempts: dict[str, list[datetime]] = {}
SESSION_EXPIRE_HOURS = 24 * 30
LOGIN_ATTEMPT_WINDOW_MINUTES = 15
MAX_LOGIN_ATTEMPTS = 10


def load_config() -> dict:
    """复用共享配置加载逻辑，避免 Web 与主流程出现兼容性分叉。"""
    return shared_load_config(CONFIG_PATH)


def save_config(config: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def render_template(request: Request, name: str, context: dict):
    """Render templates across Starlette TemplateResponse signature versions."""
    context = {"request": request, **context}
    try:
        return templates.TemplateResponse(request=request, name=name, context=context)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc) and "positional" not in str(exc):
            raise
        return templates.TemplateResponse(name, context)


def _get_web_security_config() -> dict:
    config = load_config()
    return config.get("web", {}) if isinstance(config, dict) else {}


def _redact_sensitive_config(data: Any) -> Any:
    """兼容旧调用：脱敏逻辑由 settings_editor 统一维护。"""
    return redact_sensitive_config(data)


def _get_client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _prune_login_attempts(client_key: str):
    cutoff = datetime.now() - timedelta(minutes=LOGIN_ATTEMPT_WINDOW_MINUTES)
    attempts = [ts for ts in login_attempts.get(client_key, []) if ts >= cutoff]
    if attempts:
        login_attempts[client_key] = attempts
    elif client_key in login_attempts:
        del login_attempts[client_key]


def is_password_auth_enabled(web_cfg: Optional[dict] = None) -> bool:
    web_cfg = web_cfg or _get_web_security_config()
    if "require_login_password" in web_cfg:
        return bool(web_cfg.get("require_login_password"))
    return bool(web_cfg.get("password"))


def needs_initial_security_setup(web_cfg: Optional[dict] = None) -> bool:
    web_cfg = web_cfg or _get_web_security_config()
    return "require_login_password" not in web_cfg and not web_cfg.get("password")


def verify_session(request: Request) -> bool:
    web_cfg = _get_web_security_config()
    if not needs_initial_security_setup(web_cfg) and not is_password_auth_enabled(web_cfg):
        return True

    session_id = request.cookies.get("session_id")
    if not session_id or session_id not in sessions:
        return False
    if (datetime.now() - sessions[session_id]).total_seconds() > SESSION_EXPIRE_HOURS * 3600:
        del sessions[session_id]
        return False
    return True


async def require_auth(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401, detail="未登录")


# ============ 页面路由 ============

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """主页/登录页"""
    logger.info("访问 / 路由")
    try:
        config = load_config()
        logger.info(f"加载的 config: {config}")
        web_cfg = config.get("web", {})
        logger.info(f"web_cfg: {web_cfg}, password: {web_cfg.get('password')}")
        
        if needs_initial_security_setup(web_cfg):
            logger.info("尚未完成安全设置，重定向到 /setup")
            return RedirectResponse("/setup")

        if not is_password_auth_enabled(web_cfg):
            return RedirectResponse("/dashboard")
        
        if verify_session(request):
            return RedirectResponse("/dashboard")
        
        logger.info("渲染 login.html")
        return render_template(request, "login.html", {"active_page": ""})
    except Exception as e:
        logger.error(f"访问首页出错: {e}")
        raise HTTPException(500, f"服务器错误: {e}")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """首次设置密码页"""
    try:
        config = load_config()
        if not needs_initial_security_setup(config.get("web", {})):
            return RedirectResponse("/")
        
        return render_template(request, "setup.html", {"active_page": ""})
    except Exception as e:
        logger.error(f"访问设置页出错: {e}")
        raise HTTPException(500, f"服务器错误: {e}")


@app.post("/setup")
async def do_setup(
    auth_mode: str = Form("password"),
    password: str = Form(""),
    confirm: str = Form(""),
):
    """首次设置访问验证方式"""
    config = load_config()
    if "web" not in config:
        config["web"] = {}

    if auth_mode == "password":
        if password != confirm:
            raise HTTPException(400, "密码不一致")
        if len(password) < 6:
            raise HTTPException(400, "密码至少6位")
        config["web"]["require_login_password"] = True
        config["web"]["password"] = hash_password(password)
    elif auth_mode == "none":
        config["web"]["require_login_password"] = False
        config["web"]["password"] = ""
    else:
        raise HTTPException(400, "无效的验证方式")

    save_config(config)
    return RedirectResponse("/", status_code=303)


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    """登录 - 密码错误时返回页面内提示"""
    config = load_config()
    web_cfg = config.get("web", {})
    if not is_password_auth_enabled(web_cfg):
        return RedirectResponse("/dashboard", status_code=303)

    stored_hash = web_cfg.get("password", "")
    client_key = _get_client_key(request)
    _prune_login_attempts(client_key)
    if len(login_attempts.get(client_key, [])) >= MAX_LOGIN_ATTEMPTS:
        raise HTTPException(429, f"登录失败次数过多，请 {LOGIN_ATTEMPT_WINDOW_MINUTES} 分钟后重试")
    
    if hash_password(password) != stored_hash:
        login_attempts.setdefault(client_key, []).append(datetime.now())
        # 密码错误，返回登录页面并显示错误信息
        return render_template(request, "login.html", {
            "active_page": "",
            "error": "密码错误，请重试"
        })
    
    login_attempts.pop(client_key, None)
    session_id = secrets.token_hex(32)
    sessions[session_id] = datetime.now()
    web_cfg = _get_web_security_config()
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        "session_id",
        session_id,
        httponly=True,
        secure=bool(web_cfg.get("secure_cookies", False)),
        samesite=web_cfg.get("cookie_samesite", "lax"),
        max_age=SESSION_EXPIRE_HOURS * 3600,
    )
    return response


@app.get("/logout")
async def logout(request: Request):
    """登出"""
    session_id = request.cookies.get("session_id")
    if session_id and session_id in sessions:
        del sessions[session_id]
    
    response = RedirectResponse("/")
    response.delete_cookie("session_id")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """仪表盘"""
    # 检查登录状态，未登录重定向到登录页
    if not verify_session(request):
        return RedirectResponse("/", status_code=302)
    
    # 获取 XP 画像
    xp_profile = await db.get_xp_profile()
    top_tags = sorted(xp_profile.items(), key=lambda x: x[1], reverse=True)[:20]
    
    # 获取推送统计
    stats = await db.get_push_stats(days=7)
    
    # 计算点赞率
    if stats["total_pushed"] > 0:
        like_rate = f"{stats['likes'] / stats['total_pushed'] * 100:.1f}%"
    else:
        like_rate = "0%"
    
    return render_template(request, "dashboard.html", {
        "active_page": "dashboard",
        "top_tags": top_tags,
        "stats": stats,
        "like_rate": like_rate
    })


# 配置路径常量（添加到文件顶部已有的常量后面）
PROJECT_ROOT = Path(__file__).parent.parent
IP_TAGS_FILE = PROJECT_ROOT / "data" / "ip_tags.json"
SYNC_SCRIPT = PROJECT_ROOT / "scripts" / "sync_ip_tags.py"

# 确保 data 目录存在
IP_TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)

@app.get("/gallery", response_class=HTMLResponse)
async def gallery(
    request: Request,
    page: int = Query(1, ge=1),
    favorites_only: bool = Query(False)
):
    """推送历史画廊"""
    # 检查登录状态，未登录重定向到登录页
    if not verify_session(request):
        return RedirectResponse("/", status_code=302)
    
    limit = 25
    offset = (page - 1) * limit
    
    # 获取推送历史
    items, total = await db.get_push_history_paginated(
        limit=limit,
        offset=offset,
        favorites_only=favorites_only
    )
    
    return render_template(request, "gallery.html", {
        "active_page": "gallery",
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "favorites_only": favorites_only
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """设置页面（V2 分区布局）"""
    # 检查登录状态，未登录重定向到登录页
    if not verify_session(request):
        return RedirectResponse("/", status_code=302)

    config = build_settings_view_config(load_config())
    return render_template(request, "settings_v2.html", {
        "active_page": "settings",
        "config": config
    })


@app.get("/tags", response_class=HTMLResponse)
async def tags_page(request: Request):
    """标签管理页面"""
    # 检查登录状态，未登录重定向到登录页
    if not verify_session(request):
        return RedirectResponse("/", status_code=302)
    
    config = load_config()
    return render_template(request, "tags.html", {
        "active_page": "tags",
        "config": config
    })


# ============ API 路由 ============

class FeedbackRequest(BaseModel):
    illust_id: int
    action: str  # 'like' | 'dislike'


class TagReviewRequest(BaseModel):
    tag: str
    classification: str

class SettingsRequest(BaseModel):
    require_login_password: Optional[bool] = True
    web_password: Optional[str] = ""
    web_password_confirm: Optional[str] = ""
    user_id: int
    cron: str
    ip_weight_discount: float
    danbooru_login: Optional[str] = ""
    danbooru_api_key: Optional[str] = ""
    strategies: List[str]
    r18_mode: str
    proxy_url: Optional[str] = ""
    # 新增字段
    search_limit: Optional[int] = 200
    date_range_days: Optional[int] = 60
    bookmark_threshold_search: Optional[int] = 500
    bookmark_threshold_subscription: Optional[int] = 0
    bookmark_threshold_related: Optional[int] = 100
    daily_limit: Optional[int] = 50
    max_per_artist: Optional[int] = 3
    exclude_ai: Optional[bool] = True
    skip_ugoira: Optional[bool] = True
    batch_mode: Optional[str] = "album"
    image_quality: Optional[int] = 85
    max_image_size: Optional[int] = 2000
    rich_message_enabled: Optional[bool] = False
    rich_message_fallback_to_photo: Optional[bool] = True
    rich_message_image_mode: Optional[str] = "photo"
    max_concurrency: Optional[int] = 5
    requests_per_minute: Optional[int] = 60


def merge_config_replace_lists(base: Any, override: Any) -> Any:
    """兼容旧调用：合并逻辑由 settings_editor 统一维护。"""
    from web.settings_editor import merge_config_replace_lists as merge_settings_config

    return merge_settings_config(base, override)


def build_settings_view_config(raw_config: Any) -> dict:
    """兼容旧调用：设置页快照由 settings_editor 统一维护。"""
    return build_settings_snapshot(raw_config)


def _legacy_settings_payload(req: SettingsRequest) -> dict:
    """Map the old narrow settings request onto the full settings payload."""
    image_mode = req.rich_message_image_mode if req.rich_message_image_mode in {"photo", "rich_card", "hybrid"} else "photo"
    proxy_url = req.proxy_url.strip() if req.proxy_url and req.proxy_url.strip().lower() != "none" else None
    return {
        "pixiv": {"user_id": req.user_id},
        "profiler": {
            "ip_weight_discount": req.ip_weight_discount,
            "danbooru_login": req.danbooru_login,
            "danbooru_api_key": req.danbooru_api_key,
        },
        "strategies": req.strategies,
        "scheduler": {"cron": req.cron},
        "filter": {
            "r18_mode": req.r18_mode,
            "daily_limit": req.daily_limit,
            "max_per_artist": req.max_per_artist,
            "exclude_ai": req.exclude_ai,
            "skip_ugoira": req.skip_ugoira,
        },
        "fetcher": {
            "search_limit": req.search_limit,
            "date_range_days": req.date_range_days,
            "bookmark_threshold": {
                "search": req.bookmark_threshold_search,
                "subscription": req.bookmark_threshold_subscription,
                "related": req.bookmark_threshold_related,
            },
        },
        "notifier": {
            "telegram": {
                "batch_mode": req.batch_mode,
                "image_quality": req.image_quality,
                "max_image_size": req.max_image_size,
                "proxy_url": proxy_url,
                "rich_message": {
                    "enabled": bool(req.rich_message_enabled),
                    "fallback_to_photo": bool(req.rich_message_fallback_to_photo),
                    "image_mode": image_mode,
                },
            },
        },
        "web": {"require_login_password": bool(req.require_login_password)},
        "web_password": req.web_password or "",
        "web_password_confirm": req.web_password_confirm or "",
        "network": {
            "max_concurrency": req.max_concurrency,
            "requests_per_minute": req.requests_per_minute,
        },
    }

@app.post("/api/settings")
async def save_settings(req: SettingsRequest, _=Depends(require_auth)):
    """保存配置"""
    try:
        current = load_config()
        merged = apply_settings_payload(current, _legacy_settings_payload(req), hash_password)
        save_config(merged)
        return {"success": True}
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/config/full")
async def save_full_config(payload: Dict[str, Any] = Body(...), _=Depends(require_auth)):
    """
    全量配置保存接口（V2 设置页使用）：
    - 仅更新前端提交的字段，未提交字段保留原值
    - 兼容 Web 密码更新逻辑，避免误清空
    """
    try:
        current = load_config()
        merged = apply_settings_payload(current, payload, hash_password)
        save_config(merged)
        return {"success": True}
    except Exception as e:
        logger.error(f"全量保存配置失败: {e}")
        return {"success": False, "error": str(e)}

@app.get("/api/sync-status")
async def get_sync_status(_=Depends(require_auth)):
    """检查 IP 列表状态"""
    if IP_TAGS_FILE.exists():
        try:
            mtime = datetime.fromtimestamp(IP_TAGS_FILE.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            with open(IP_TAGS_FILE, "r") as f:
                data = json.load(f)
                return {"exists": True, "count": len(data), "mtime": mtime}
        except:
            pass
    return {"exists": False}

class SyncRequest(BaseModel):
    danbooru_login: str
    danbooru_api_key: str

@app.post("/api/sync-ip")
async def sync_ip_list(req: SyncRequest, _=Depends(require_auth)):
    """执行 IP 同步"""
    if not SYNC_SCRIPT.exists():
        return {"success": False, "output": "脚本文件未找到: scripts/sync_ip_tags.py"}
    
    env = os.environ.copy()
    if req.danbooru_login: env["DANBOORU_LOGIN"] = req.danbooru_login
    if req.danbooru_api_key: env["DANBOORU_API_KEY"] = req.danbooru_api_key
    
    try:
        # 确保 data 目录存在
        IP_TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        
        result = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120
        )
        
        return {
            "success": result.returncode == 0,
            "output": result.stdout + "\n" + result.stderr
        }
    except Exception as e:
        return {"success": False, "output": f"执行出错: {e}"}

@app.get("/api/config")
async def get_config_section(section: str = Query(None), _=Depends(require_auth)):
    """获取配置的特定部分"""
    config = _redact_sensitive_config(load_config())
    if section:
        return {section: config.get(section, {})}
    return config

class BoostTagRequest(BaseModel):
    tag: str
    multiplier: float = 1.5

@app.post("/api/config/boost-tag")
async def add_boost_tag(req: BoostTagRequest, _=Depends(require_auth)):
    """添加或更新 Boost Tag"""
    try:
        config = load_config()
        
        # 确保 profiler 部分存在
        if "profiler" not in config:
            config["profiler"] = {}
        
        # 确保 boost_tags 存在
        if "boost_tags" not in config["profiler"]:
            config["profiler"]["boost_tags"] = {}
        
        # 添加或更新
        config["profiler"]["boost_tags"][req.tag] = req.multiplier
        
        save_config(config)
        return {"success": True}
    except Exception as e:
        logger.error(f"添加 Boost Tag 失败: {e}")
        return {"success": False, "error": str(e)}

@app.delete("/api/config/boost-tag")
async def remove_boost_tag(req: BoostTagRequest, _=Depends(require_auth)):
    """删除 Boost Tag"""
    try:
        config = load_config()
        
        if ("profiler" in config and 
            "boost_tags" in config["profiler"] and 
            req.tag in config["profiler"]["boost_tags"]):
            
            del config["profiler"]["boost_tags"][req.tag]
            save_config(config)
            return {"success": True}
        else:
            return {"success": False, "error": "Tag 不存在"}
    except Exception as e:
        logger.error(f"删除 Boost Tag 失败: {e}")
        return {"success": False, "error": str(e)}

class BlacklistTagRequest(BaseModel):
    tag: str

@app.post("/api/config/blacklist-tag")
async def add_blacklist_tag(req: BlacklistTagRequest, _=Depends(require_auth)):
    """添加黑名单标签"""
    try:
        config = load_config()
        
        # 确保 filter 部分存在
        if "filter" not in config:
            config["filter"] = {}
        
        # 确保 blacklist_tags 存在
        if "blacklist_tags" not in config["filter"]:
            config["filter"]["blacklist_tags"] = []
        
        # 添加（如果不存在）
        if req.tag not in config["filter"]["blacklist_tags"]:
            config["filter"]["blacklist_tags"].append(req.tag)
            save_config(config)
            return {"success": True}
        else:
            return {"success": False, "error": "标签已在黑名单中"}
    except Exception as e:
        logger.error(f"添加黑名单标签失败: {e}")
        return {"success": False, "error": str(e)}

@app.delete("/api/config/blacklist-tag")
async def remove_blacklist_tag(req: BlacklistTagRequest, _=Depends(require_auth)):
    """删除黑名单标签"""
    try:
        config = load_config()
        
        if ("filter" in config and 
            "blacklist_tags" in config["filter"] and 
            req.tag in config["filter"]["blacklist_tags"]):
            
            config["filter"]["blacklist_tags"].remove(req.tag)
            save_config(config)
            return {"success": True}
        else:
            return {"success": False, "error": "标签不在黑名单中"}
    except Exception as e:
        logger.error(f"删除黑名单标签失败: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/search-tag")
async def search_tag(q: str = Query(..., min_length=1), _=Depends(require_auth)):
    """模糊搜索 XP 画像中的标签（支持多语言）"""
    try:
        conn = await db.get_db()
        results = []
        seen_tags = set()  # 避免重复
        
        # 1. 先搜索 xp_profile 表 (XP 画像) - 英文标签
        try:
            cursor = await conn.execute(
                "SELECT tag, weight FROM xp_profile WHERE tag LIKE ? ORDER BY weight DESC LIMIT 20",
                (f"%{q}%",)
            )
            profile_rows = await cursor.fetchall()
            for row in profile_rows:
                tag = row[0]
                if tag not in seen_tags:
                    seen_tags.add(tag)
                    # 获取这个标准化标签对应的原始标签
                    original_tags = await get_original_tags_for_normalized(conn, tag)
                    results.append({
                        "tag": tag, 
                        "weight": row[1], 
                        "source": "xp_profile",
                        "type": "normalized",
                        "original_tags": original_tags
                    })
        except Exception as e:
            logger.warning(f"搜索 xp_profile 表失败（可能表不存在）: {e}")
        
        # 2. 搜索 tag_mapping_stats 表 - 通过原始标签找标准化标签
        try:
            cursor = await conn.execute(
                """SELECT DISTINCT tms.normalized_tag, xp.weight, tms.original_tag
                   FROM tag_mapping_stats tms 
                   LEFT JOIN xp_profile xp ON tms.normalized_tag = xp.tag
                   WHERE tms.original_tag LIKE ? 
                   ORDER BY COALESCE(xp.weight, 0) DESC 
                   LIMIT 20""",
                (f"%{q}%",)
            )
            mapping_rows = await cursor.fetchall()
            for row in mapping_rows:
                tag = row[0]
                original_tag = row[2]
                if tag not in seen_tags:
                    seen_tags.add(tag)
                    # 获取所有原始标签（包括当前匹配的）
                    original_tags = await get_original_tags_for_normalized(conn, tag)
                    results.append({
                        "tag": tag, 
                        "weight": row[1] or 0.0, 
                        "source": "tag_mapping",
                        "type": "normalized",
                        "original_match": True,  # 标记是通过原始标签匹配的
                        "matched_original": original_tag,  # 匹配到的原始标签
                        "original_tags": original_tags  # 所有原始标签
                    })
        except Exception as e:
            logger.warning(f"搜索 tag_mapping_stats 表失败: {e}")
        
        # 3. 搜索 ai_tag_cache 表 - 原始标签到清洗后标签的映射
        try:
            cursor = await conn.execute(
                """SELECT DISTINCT atc.cleaned_tag, xp.weight, atc.original_tag
                   FROM ai_tag_cache atc 
                   LEFT JOIN xp_profile xp ON atc.cleaned_tag = xp.tag
                   WHERE atc.original_tag LIKE ? AND atc.cleaned_tag IS NOT NULL
                   ORDER BY COALESCE(xp.weight, 0) DESC 
                   LIMIT 20""",
                (f"%{q}%",)
            )
            cache_rows = await cursor.fetchall()
            for row in cache_rows:
                tag = row[0]
                original_tag = row[2]
                if tag and tag not in seen_tags:
                    seen_tags.add(tag)
                    # 获取所有原始标签
                    original_tags = await get_original_tags_for_normalized(conn, tag)
                    results.append({
                        "tag": tag, 
                        "weight": row[1] or 0.0, 
                        "source": "ai_cache",
                        "type": "normalized",
                        "original_match": True,
                        "matched_original": original_tag,
                        "original_tags": original_tags
                    })
        except Exception as e:
            logger.warning(f"搜索 ai_tag_cache 表失败: {e}")
        
        # 4. 如果结果太少，搜索 xp_bookmarks 表 (收藏数据)
        if len(results) < 5:
            try:
                cursor = await conn.execute(
                    "SELECT DISTINCT tag FROM xp_bookmarks WHERE tag LIKE ? LIMIT 20",
                    (f"%{q}%",)
                )
                bookmark_rows = await cursor.fetchall()
                for row in bookmark_rows:
                    tag = row[0]
                    if tag not in seen_tags:
                        seen_tags.add(tag)
                        results.append({
                            "tag": tag, 
                            "weight": 0.0, 
                            "source": "xp_bookmarks",
                            "type": "raw"
                        })
            except Exception as e:
                logger.warning(f"搜索 xp_bookmarks 表失败: {e}")
        
        await conn.close()
        
        # 按权重排序
        results.sort(key=lambda x: x["weight"], reverse=True)
        
        return {"success": True, "results": results}
    except Exception as e:
        logger.error(f"搜索标签失败: {e}")
        return {"success": False, "error": str(e)}

async def get_original_tags_for_normalized(conn, normalized_tag: str) -> list:
    """获取标准化标签对应的所有原始标签"""
    try:
        # 从 tag_mapping_stats 表获取
        cursor = await conn.execute(
            "SELECT original_tag FROM tag_mapping_stats WHERE normalized_tag = ? ORDER BY frequency DESC LIMIT 5",
            (normalized_tag,)
        )
        mapping_rows = await cursor.fetchall()
        
        # 从 ai_tag_cache 表获取
        cursor = await conn.execute(
            "SELECT original_tag FROM ai_tag_cache WHERE cleaned_tag = ? LIMIT 5",
            (normalized_tag,)
        )
        cache_rows = await cursor.fetchall()
        
        # 合并并去重
        original_tags = set()
        for row in mapping_rows:
            if row[0]:
                original_tags.add(row[0])
        for row in cache_rows:
            if row[0]:
                original_tags.add(row[0])
        
        return list(original_tags)
    except Exception as e:
        logger.warning(f"获取原始标签失败: {e}")
        return []

@app.post("/api/feedback")
async def api_feedback(req: FeedbackRequest, request: Request, _=Depends(require_auth)):
    """统一反馈接口"""
    if req.action not in ("like", "dislike"):
        raise HTTPException(400, "无效的action")
    
    await db.record_feedback(req.illust_id, req.action)
    return {"success": True, "message": f"已记录对作品 {req.illust_id} 的 {req.action}"}


@app.get("/api/xp-profile")
async def api_xp_profile(request: Request, _=Depends(require_auth)):
    """获取XP画像"""
    profile = await db.get_xp_profile()
    return {"profile": profile}


@app.get("/api/tag-reviews")
async def api_tag_reviews(limit: int = Query(100, ge=1, le=500), _=Depends(require_auth)):
    """Return unresolved tags in the order a human should review them."""
    return {"items": await db.get_tag_review_queue(limit)}


@app.post("/api/tag-reviews")
async def submit_tag_review(req: TagReviewRequest, _=Depends(require_auth)):
    """Persist a human Tag Category decision and remove the tag from the queue."""
    try:
        await db.review_tag_classification(req.tag, req.classification)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "tag": req.tag, "classification": req.classification}


@app.get("/health")
async def health():
    """健康检查端点 (无需认证)"""
    runtime = await db.get_state("runtime.last_run_summary")
    schema_version = await db.get_schema_version()
    return {"status": "ok", "timestamp": datetime.now().isoformat(), "schema_version": schema_version, "last_run_summary": runtime}


@app.get("/api/runtime-status")
async def api_runtime_status(request: Request, _=Depends(require_auth)):
    """获取运行时状态与数据库概览"""
    return {
        "last_run_summary": await db.get_state("runtime.last_run_summary"),
        "last_run_started_at": await db.get_state("runtime.last_run_started_at"),
        "db": await db.get_db_overview(),
    }


@app.get("/api/stats")
async def api_stats(request: Request, days: int = 7, _=Depends(require_auth)):
    """获取推送效果统计"""
    stats = await db.get_push_stats(days)
    
    # 计算点赞率
    if stats["total_pushed"] > 0:
        like_rate = stats["likes"] / stats["total_pushed"] * 100
    else:
        like_rate = 0
        
    return {
        "days": days,
        "total_pushed": stats["total_pushed"],
        "likes": stats["likes"],
        "dislikes": stats["dislikes"],
        "like_rate": f"{like_rate:.1f}%",
        "top_artists": stats.get("top_artists", []),
        "top_tags": stats.get("top_tags", [])
    }


@app.get("/api/gallery")
async def api_gallery(
    request: Request,
    page: int = 1,
    limit: int = 25,
    favorites_only: bool = False,
    _=Depends(require_auth)
):
    """获取推送历史 (API)"""
    offset = (page - 1) * limit
    items, total = await db.get_push_history_paginated(
        limit=limit,
        offset=offset,
        favorites_only=favorites_only
    )
    total_pages = (total // limit) + (1 if total % limit else 0)
    has_more = page < total_pages

    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "favorites_only": favorites_only,
        "pages": total_pages,
        "has_more": has_more,
        "next_page": page + 1 if has_more else None,
    }


@app.get("/import-export", response_class=HTMLResponse)
async def import_export_page(request: Request):
    """导入/导出页面"""
    # 检查登录状态，未登录重定向到登录页
    if not verify_session(request):
        return RedirectResponse("/", status_code=302)
    
    config = load_config()
    return render_template(request, "import_export.html", {
        "active_page": "import_export",
        "config": config
    })


@app.get("/api/config/export")
async def export_config(_=Depends(require_auth)):
    """导出配置为带注释的 YAML"""
    try:
        config = load_config()
        
        # 添加注释的 YAML 输出
        yaml_content = generate_commented_yaml(config)
        
        return {
            "success": True,
            "yaml": yaml_content,
            "filename": f"pixiv-xp-pusher-config-{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
        }
    except Exception as e:
        logger.error(f"导出配置失败: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/export")
async def export_config_legacy(_=Depends(require_auth)):
    """兼容旧前端导出入口。"""
    return await export_config(_)


class ImportConfigRequest(BaseModel):
    yaml_content: str
    merge: bool = False  # True = 合并, False = 覆盖


@app.post("/api/config/import")
async def import_config(req: ImportConfigRequest, _=Depends(require_auth)):
    """导入 YAML 配置"""
    try:
        # 解析导入的 YAML
        imported = yaml.safe_load(req.yaml_content)
        if imported is None:
            return {"success": False, "error": "YAML 内容为空或格式无效"}
        if not isinstance(imported, dict):
            return {"success": False, "error": "YAML 根节点必须是对象 (key-value)"}
        
        if req.merge:
            # 合并模式：保留现有配置，导入的覆盖
            current = load_config()
            merged = deep_merge(current, imported)
            save_config(merged)
        else:
            # 覆盖模式：完全替换
            save_config(imported)
        
        return {"success": True, "message": "配置导入成功"}
    except yaml.YAMLError as e:
        return {"success": False, "error": f"YAML 解析错误: {e}"}
    except Exception as e:
        logger.error(f"导入配置失败: {e}")
        return {"success": False, "error": str(e)}


def generate_commented_yaml(config: dict) -> str:
    """生成带注释的 YAML"""
    comments = {
        "pixiv": {
            "_desc": "Pixiv 配置",
            "user_id": "你的 Pixiv 用户 ID",
            "refresh_token": "Pixiv Refresh Token（可选，用于获取收藏数据）"
        },
        "scheduler": {
            "_desc": "调度器配置",
            "cron": "Cron 表达式，例如 '0 */6 * * *' 每6小时执行一次",
            "coalesce": "是否合并错过任务",
            "daily_report_cron": "每日维护任务 Cron（日报+清理）"
        },
        "notifier": {
            "_desc": "通知渠道配置",
            "types": "启用的推送通道，例如 [telegram]",
            "max_pages": "多图作品最大打包页数",
            "multi_page_mode": "多图发送模式: cover_link / media_group",
            "telegram": {
                "_desc": "Telegram 通知",
                "bot_token": "Bot Token",
                "chat_ids": "频道/群组 ID 列表",
                "allowed_users": "允许互动的用户 ID 列表",
                "thread_id": "默认 Topic ID（可选）",
                "proxy_url": "代理地址（可选）",
                "batch_mode": "批量推送模式: single / telegraph"
            }
        },
        "filter": {
            "_desc": "内容过滤配置",
            "r18_mode": "R18 过滤模式: mixed / r18_only / safe",
            "blacklist_tags": "黑名单标签列表",
            "content_type": "内容类型: all / illust / manga",
            "min_create_days": "过滤 N 天前作品",
            "author_diversity": "画师多样性衰减配置",
            "source_boost": "来源加成权重"
        },
        "profiler": {
            "_desc": "XP 画像配置",
            "ip_weight_discount": "IP 标签权重折扣 (0-1)",
            "boost_tags": "加权标签字典 {tag: multiplier}",
            "scan_limit": "扫描收藏数量上限",
            "discovery_rate": "探索新 Tag 概率"
        },
        "ai": {
            "_desc": "AI 增强配置",
            "embedding": "Embedding 语义匹配配置",
            "scorer": "LLM 二次评分配置"
        },
        "strategies": {
            "_desc": "推送策略列表",
            "_items": [
                "xp_search: XP 画像搜索",
                "related: 关联连锁",
                "ranking: 排行榜",
                "subscription: 关注更新"
            ]
        },
        "web": {
            "_desc": "Web UI 配置",
            "enabled": "是否启用 Web UI",
            "port": "Web 服务端口",
            "require_login_password": "是否启用 Web 登录密码验证",
            "password": "登录密码（SHA256 哈希）"
        }
    }
    
    lines = ["# Pixiv-XP-Pusher 配置文件", f"# 导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
    
    def add_comments(obj, schema, indent=0):
        prefix = "  " * indent
        if isinstance(obj, dict):
            for key, value in obj.items():
                # 获取注释
                key_schema = schema.get(key, {}) if isinstance(schema, dict) else {}
                desc = key_schema.get("_desc", "") if isinstance(key_schema, dict) else ""
                
                if desc and indent == 0:
                    lines.append("")
                    lines.append(f"{prefix}# {desc}")
                
                if isinstance(value, dict):
                    lines.append(f"{prefix}{key}:")
                    add_comments(value, key_schema, indent + 1)
                elif isinstance(value, list):
                    lines.append(f"{prefix}{key}:")
                    for item in value:
                        if isinstance(item, str):
                            lines.append(f"{prefix}  - {item}")
                        else:
                            lines.append(f"{prefix}  - {item}")
                else:
                    # 处理字符串值，需要引号
                    if isinstance(value, str):
                        # 转义特殊字符
                        escaped = value.replace('"', '\\"').replace("'", "''")
                        lines.append(f'{prefix}{key}: "{escaped}"')
                    else:
                        lines.append(f"{prefix}{key}: {value}")
        
    add_comments(config, comments)
    return "\n".join(lines)


def deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典（列表以 override 为准，避免策略/ID 被意外拼接）。"""
    merged = merge_config_replace_lists(base, override)
    return merged if isinstance(merged, dict) else {}


@app.get("/api/proxy/image/{illust_id}")
async def proxy_image(illust_id: int):
    """
    服务端图片代理
    解决前端无法直接访问外网图床的问题
    """
    config = load_config()
    # 复用 Telegram 配置的代理
    proxy = normalize_proxy_url(config.get("notifier", {}).get("telegram", {}).get("proxy_url"))
        
    urls = [
        f"https://pixiv.cat/{illust_id}.jpg",
        f"https://c.pixiv.re/img-master/img/{illust_id}.jpg",
        f"https://c.pixiv.re/img-master/img/{illust_id}_p0.jpg"
    ]
    
    async with aiohttp.ClientSession() as session:
        for url in urls:
            try:
                async with session.get(url, proxy=proxy, timeout=10, ssl=False) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        return Response(content, media_type="image/jpeg")
            except Exception as e:
                logger.warning(f"代理请求 {url} 失败 (proxy={proxy}): {e}")
                continue
                
    # 失败时返回占位图
    return RedirectResponse("https://via.placeholder.com/300?text=Load+Failed")

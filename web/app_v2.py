<<<<<<< HEAD
"""
Web UI - FastAPI 后端 (增强版)
完整配置管理 + 导入导出功能
"""
import hashlib
import logging
import secrets
import subprocess
import os
import sys
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request, HTTPException, Depends, Form, Query, Response, Body, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import aiohttp

import yaml

import database as db

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
SESSION_EXPIRE_HOURS = 24

# 配置注释映射（用于导出时生成带注释的YAML）
CONFIG_COMMENTS = {
    "pixiv": {
        "_desc": "Pixiv 认证配置",
        "refresh_token": "Pixiv 刷新令牌 (通过 get_token.py 获取)",
        "sync_token": "同步专用令牌 (降低主号风险, 可选)",
        "user_id": "Pixiv 用户ID"
    },
    "web": {
        "_desc": "Web 管理界面配置",
        "enabled": "是否启用 Web UI",
        "port": "服务端口",
        "password": "登录密码 (SHA256哈希)"
    },
    "strategies": {
        "_desc": "推送策略 (xp_search | related | ranking | subscription)"
    },
    "scheduler": {
        "_desc": "定时任务配置",
        "cron": "主任务 Cron 表达式",
        "coalesce": "合并错过的任务",
        "daily_report_cron": "日报任务 Cron"
    },
    "network": {
        "_desc": "网络配置",
        "max_concurrency": "最大并发请求数",
        "random_delay": "随机延迟范围 [最小, 最大] (秒)",
        "requests_per_minute": "每分钟请求上限"
    },
    "feedback": {
        "_desc": "反馈机制配置",
        "like_boost": "点赞时权重增加量",
        "dislike_penalty": "点踩时权重减少量",
        "dislike_threshold": "累计点踩几次提示屏蔽",
        "max_chain_depth": "连锁反应深度上限",
        "related_push_limit": "每次反馈触发关联推送数"
    }
}


def load_config() -> dict:
    """加载配置文件"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
            if config is None:
                logger.warning("config.yaml 为空或格式无效，返回默认配置")
                return {}
            return config
    except Exception as e:
        logger.error(f"加载 config.yaml 失败: {e}")
        return {}


def save_config(config: dict):
    """保存配置文件（保持原有格式）"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def export_config_with_comments(config: dict) -> str:
    """导出带注释的 YAML 配置"""
    lines = []
    lines.append("# =============================================================================")
    lines.append("# Pixiv XP Pusher 配置文件")
    lines.append("# 导出时间: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("# =============================================================================")
    lines.append("")
    
    section_order = [
        ("pixiv", "核心认证 (Pixiv)"),
        ("web", "Web 管理界面"),
        ("strategies", "推送策略"),
        ("notifier", "推送通道"),
        ("profiler", "画像分析"),
        ("ai", "AI 增强"),
        ("filter", "内容过滤"),
        ("fetcher", "内容获取"),
        ("feedback", "反馈机制"),
        ("scheduler", "定时任务"),
        ("network", "网络配置")
    ]
    
    for key, title in section_order:
        if key not in config:
            continue
        
        lines.append("# =============================================================================")
        lines.append(f"# {title}")
        lines.append("# =============================================================================")
        lines.append("")
        
        section_yaml = yaml.dump({key: config[key]}, allow_unicode=True, default_flow_style=False, sort_keys=False)
        lines.append(section_yaml.rstrip())
        lines.append("")
    
    return "\n".join(lines)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_session(request: Request) -> bool:
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
    try:
        config = load_config()
        web_cfg = config.get("web", {})
        
        if not web_cfg.get("password"):
            return RedirectResponse("/setup")
        
        if verify_session(request):
            return RedirectResponse("/dashboard")
        
        return templates.TemplateResponse("login.html", {"request": request, "active_page": ""})
    except Exception as e:
        logger.error(f"访问首页出错: {e}")
        raise HTTPException(500, f"服务器错误: {e}")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """首次设置密码页"""
    try:
        config = load_config()
        if config.get("web", {}).get("password"):
            return RedirectResponse("/")
        
        return templates.TemplateResponse("setup.html", {"request": request, "active_page": ""})
    except Exception as e:
        logger.error(f"访问设置页出错: {e}")
        raise HTTPException(500, f"服务器错误: {e}")


@app.post("/setup")
async def do_setup(password: str = Form(...), confirm: str = Form(...)):
    """设置密码"""
    if password != confirm:
        raise HTTPException(400, "密码不一致")
    if len(password) < 6:
        raise HTTPException(400, "密码至少6位")
    
    config = load_config()
    if "web" not in config:
        config["web"] = {}
    config["web"]["password"] = hash_password(password)
    config["web"]["enabled"] = True
    save_config(config)
    
    return RedirectResponse("/", status_code=303)


@app.post("/login")
async def login(password: str = Form(...)):
    """登录"""
    config = load_config()
    stored_hash = config.get("web", {}).get("password", "")
    
    if hash_password(password) != stored_hash:
        raise HTTPException(401, "密码错误")
    
    session_id = secrets.token_hex(32)
    sessions[session_id] = datetime.now()
    
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie("session_id", session_id, httponly=True)
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
async def dashboard(request: Request, _=Depends(require_auth)):
    """仪表盘"""
    xp_profile = await db.get_xp_profile()
    top_tags = sorted(xp_profile.items(), key=lambda x: x[1], reverse=True)[:20]
    
    stats = await db.get_push_stats(days=7)
    
    if stats["total_pushed"] > 0:
        like_rate = f"{stats['likes'] / stats['total_pushed'] * 100:.1f}%"
    else:
        like_rate = "0%"
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "active_page": "dashboard",
        "top_tags": top_tags,
        "stats": stats,
        "like_rate": like_rate
    })


@app.get("/gallery", response_class=HTMLResponse)
async def gallery(request: Request, page: int = Query(1, ge=1), _=Depends(require_auth)):
    """推送历史画廊"""
    limit = 24
    offset = (page - 1) * limit
    
    items, total = await db.get_push_history_paginated(limit=limit, offset=offset)
    
    return templates.TemplateResponse("gallery.html", {
        "request": request,
        "active_page": "gallery",
        "items": items,
        "total": total,
        "page": page,
        "limit": limit
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, _=Depends(require_auth)):
    """增强版设置页面 - 支持完整配置"""
    config = load_config()
    return templates.TemplateResponse("settings_v2.html", {
        "request": request,
        "active_page": "settings",
        "config": config
    })


@app.get("/tags", response_class=HTMLResponse)
async def tags_page(request: Request, _=Depends(require_auth)):
    """标签管理页面"""
    config = load_config()
    return templates.TemplateResponse("tags.html", {
        "request": request,
        "active_page": "tags",
        "config": config
    })


@app.get("/import-export", response_class=HTMLResponse)
async def import_export_page(request: Request, _=Depends(require_auth)):
    """导入导出页面"""
    return templates.TemplateResponse("import_export.html", {
        "request": request,
        "active_page": "import_export"
    })


# ============ API 路由 - 配置管理 ============

@app.get("/api/config")
async def get_config(_=Depends(require_auth)):
    """获取完整配置"""
    return load_config()


@app.get("/api/config/{section}")
async def get_config_section(section: str, _=Depends(require_auth)):
    """获取配置特定部分"""
    config = load_config()
    return {section: config.get(section, {})}


@app.post("/api/config/full")
async def save_full_config(request: Request, _=Depends(require_auth)):
    """保存完整配置"""
    try:
        data = await request.json()
        save_config(data)
        return {"success": True}
    except Exception as e:
        logger.error(f"保存完整配置失败: {e}")
        return {"success": False, "error": str(e)}


# ============ API 路由 - 导入导出 ============

@app.get("/api/export")
async def export_config(_=Depends(require_auth)):
    """导出配置 (带注释)"""
    config = load_config()
    content = export_config_with_comments(config)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"pixiv_xp_pusher_config_{timestamp}.yaml"
    
    return PlainTextResponse(
        content=content,
        media_type="text/yaml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.post("/api/import")
async def import_config(file: UploadFile = File(...), _=Depends(require_auth)):
    """导入配置"""
    try:
        content = await file.read()
        content_str = content.decode('utf-8')
        
        # 解析 YAML
        new_config = yaml.safe_load(content_str)
        
        if not isinstance(new_config, dict):
            return {"success": False, "error": "无效的配置文件格式"}
        
        # 备份当前配置
        config = load_config()
        backup_path = CONFIG_PATH.parent / f"config_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
        with open(backup_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        
        # 合并配置（保留部分关键字段如 web.password）
        if "web" in config and "password" in config["web"]:
            if "web" not in new_config:
                new_config["web"] = {}
            new_config["web"]["password"] = config["web"]["password"]
        
        save_config(new_config)
        
        return {"success": True, "backup": str(backup_path)}
    except Exception as e:
        logger.error(f"导入配置失败: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/import/validate")
async def validate_import(file: UploadFile = File(...), _=Depends(require_auth)):
    """验证导入的配置文件（不保存）"""
    try:
        content = await file.read()
        content_str = content.decode('utf-8')
        
        new_config = yaml.safe_load(content_str)
        
        if not isinstance(new_config, dict):
            return {"valid": False, "error": "无效的配置文件格式"}
        
        # 检查必需的顶层键
        required_keys = ["pixiv", "strategies", "filter"]
        missing = [k for k in required_keys if k not in new_config]
        
        if missing:
            return {"valid": False, "error": f"缺少必需的配置项: {', '.join(missing)}"}
        
        return {"valid": True, "sections": list(new_config.keys())}
    except Exception as e:
        return {"valid": False, "error": str(e)}


# ============ API 路由 - 配置更新（分模块） ============

class PixivConfigRequest(BaseModel):
    refresh_token: str
    sync_token: Optional[str] = ""
    user_id: int

@app.post("/api/config/pixiv")
async def save_pixiv_config(req: PixivConfigRequest, _=Depends(require_auth)):
    """保存 Pixiv 配置"""
    try:
        config = load_config()
        config["pixiv"] = {
            "refresh_token": req.refresh_token,
            "sync_token": req.sync_token or "",
            "user_id": req.user_id
        }
        save_config(config)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


class NotifierConfigRequest(BaseModel):
    types: List[str]
    telegram: Dict[str, Any]
    onebot: Optional[Dict[str, Any]] = None
    astrbot: Optional[Dict[str, Any]] = None
    max_pages: int = 10
    multi_page_mode: str = "cover_link"

@app.post("/api/config/notifier")
async def save_notifier_config(req: NotifierConfigRequest, _=Depends(require_auth)):
    """保存推送通道配置"""
    try:
        config = load_config()
        config["notifier"] = {
            "types": req.types,
            "telegram": req.telegram,
            "max_pages": req.max_pages,
            "multi_page_mode": req.multi_page_mode
        }
        if req.onebot:
            config["notifier"]["onebot"] = req.onebot
        if req.astrbot:
            config["notifier"]["astrbot"] = req.astrbot
        save_config(config)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


class AIConfigRequest(BaseModel):
    embedding: Dict[str, Any]
    scorer: Dict[str, Any]

@app.post("/api/config/ai")
async def save_ai_config(req: AIConfigRequest, _=Depends(require_auth)):
    """保存 AI 增强配置"""
    try:
        config = load_config()
        config["ai"] = {
            "embedding": req.embedding,
            "scorer": req.scorer
        }
        save_config(config)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


class FilterConfigRequest(BaseModel):
    daily_limit: int
    exclude_ai: bool
    skip_ugoira: bool
    content_type: str
    r18_mode: str
    max_per_artist: int
    min_create_days: int
    match_score: Dict[str, float]
    blacklist_tags: List[str]

@app.post("/api/config/filter")
async def save_filter_config(req: FilterConfigRequest, _=Depends(require_auth)):
    """保存过滤配置"""
    try:
        config = load_config()
        if "filter" not in config:
            config["filter"] = {}
        
        config["filter"].update({
            "daily_limit": req.daily_limit,
            "exclude_ai": req.exclude_ai,
            "skip_ugoira": req.skip_ugoira,
            "content_type": req.content_type,
            "r18_mode": req.r18_mode,
            "max_per_artist": req.max_per_artist,
            "min_create_days": req.min_create_days,
            "match_score": req.match_score,
            "blacklist_tags": req.blacklist_tags
        })
        save_config(config)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


class FetcherConfigRequest(BaseModel):
    bookmark_threshold: Dict[str, int]
    date_range_days: int
    search_limit: int
    ranking: Dict[str, Any]
    subscribed_artists: List[int]

@app.post("/api/config/fetcher")
async def save_fetcher_config(req: FetcherConfigRequest, _=Depends(require_auth)):
    """保存获取器配置"""
    try:
        config = load_config()
        config["fetcher"] = {
            "bookmark_threshold": req.bookmark_threshold,
            "date_range_days": req.date_range_days,
            "search_limit": req.search_limit,
            "ranking": req.ranking,
            "subscribed_artists": req.subscribed_artists
        }
        save_config(config)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============ API 路由 - 原有功能 ============

class SettingsRequest(BaseModel):
    user_id: int
    cron: str
    ip_weight_discount: float
    danbooru_login: Optional[str] = ""
    danbooru_api_key: Optional[str] = ""
    strategies: List[str]
    r18_mode: str
    proxy_url: Optional[str] = ""

@app.post("/api/settings")
async def save_settings(req: SettingsRequest, _=Depends(require_auth)):
    """保存基础配置（兼容旧版）"""
    try:
        config = load_config()
        
        if "profiler" not in config:
            config["profiler"] = {}
        config["profiler"]["ip_weight_discount"] = req.ip_weight_discount
        config["profiler"]["danbooru_login"] = req.danbooru_login
        config["profiler"]["danbooru_api_key"] = req.danbooru_api_key
        
        config["strategies"] = req.strategies
        
        if "pixiv" not in config:
            config["pixiv"] = {}
        config["pixiv"]["user_id"] = req.user_id
        
        if "scheduler" not in config:
            config["scheduler"] = {}
        config["scheduler"]["cron"] = req.cron
        
        if "filter" not in config:
            config["filter"] = {}
        config["filter"]["r18_mode"] = req.r18_mode
        
        if "notifier" not in config:
            config["notifier"] = {}
        if "telegram" not in config["notifier"]:
            config["notifier"]["telegram"] = {}
        
        if req.proxy_url and req.proxy_url.strip() and req.proxy_url.strip().lower() != "none":
            config["notifier"]["telegram"]["proxy_url"] = req.proxy_url.strip()
        else:
            config["notifier"]["telegram"]["proxy_url"] = None
        
        save_config(config)
        return {"success": True}
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
        return {"success": False, "error": str(e)}


# ============ 标签管理 API ============

class BoostTagRequest(BaseModel):
    tag: str
    multiplier: float = 1.5

@app.post("/api/config/boost-tag")
async def add_boost_tag(req: BoostTagRequest, _=Depends(require_auth)):
    """添加或更新 Boost Tag"""
    try:
        config = load_config()
        if "profiler" not in config:
            config["profiler"] = {}
        if "boost_tags" not in config["profiler"]:
            config["profiler"]["boost_tags"] = {}
        
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
        if "filter" not in config:
            config["filter"] = {}
        if "blacklist_tags" not in config["filter"]:
            config["filter"]["blacklist_tags"] = []
        
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
    """模糊搜索 XP 画像中的标签"""
    try:
        conn = await db.get_db()
        results = []
        seen_tags = set()
        
        # 搜索 xp_profile 表
        try:
            cursor = await conn.execute(
                "SELECT tag, weight FROM xp_profile WHERE tag LIKE ? ORDER BY weight DESC LIMIT 20",
                (f"%{q}%",)
            )
            for row in await cursor.fetchall():
                tag = row[0]
                if tag not in seen_tags:
                    seen_tags.add(tag)
                    results.append({
                        "tag": tag, 
                        "weight": row[1], 
                        "source": "xp_profile"
                    })
        except Exception as e:
            logger.warning(f"搜索 xp_profile 失败: {e}")
        
        await conn.close()
        results.sort(key=lambda x: x["weight"], reverse=True)
        return {"success": True, "results": results}
    except Exception as e:
        logger.error(f"搜索标签失败: {e}")
        return {"success": False, "error": str(e)}


# ============ 其他原有 API ============

@app.get("/api/sync-status")
async def get_sync_status(_=Depends(require_auth)):
    """检查 IP 列表状态"""
    PROJECT_ROOT = Path(__file__).parent.parent
    IP_TAGS_FILE = PROJECT_ROOT / "data" / "ip_tags.json"
    
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
    PROJECT_ROOT = Path(__file__).parent.parent
    SYNC_SCRIPT = PROJECT_ROOT / "scripts" / "sync_ip_tags.py"
    IP_TAGS_FILE = PROJECT_ROOT / "data" / "ip_tags.json"
    
    if not SYNC_SCRIPT.exists():
        return {"success": False, "output": "脚本文件未找到"}
    
    env = os.environ.copy()
    if req.danbooru_login:
        env["DANBOORU_LOGIN"] = req.danbooru_login
    if req.danbooru_api_key:
        env["DANBOORU_API_KEY"] = req.danbooru_api_key
    
    try:
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


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/api/stats")
async def api_stats(days: int = 7, _=Depends(require_auth)):
    """获取推送统计"""
    stats = await db.get_push_stats(days)
    
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
async def api_gallery(page: int = 1, limit: int = 24, _=Depends(require_auth)):
    """获取推送历史"""
    offset = (page - 1) * limit
    items, total = await db.get_push_history_paginated(limit=limit, offset=offset)
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total // limit) + (1 if total % limit else 0)
    }


@app.get("/api/proxy/image/{illust_id}")
async def proxy_image(illust_id: int):
    """服务端图片代理"""
    config = load_config()
    proxy = config.get("notifier", {}).get("telegram", {}).get("proxy_url")
    
    if not proxy or proxy.strip() == "" or proxy.strip().lower() == "none":
        proxy = None
    elif proxy and not proxy.startswith("http"):
        proxy = f"http://{proxy}"
    
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
                logger.warning(f"代理请求失败: {e}")
                continue
    
    return RedirectResponse("https://via.placeholder.com/300?text=Load+Failed")
=======
"""Compatibility shim for the legacy `web.app_v2` entrypoint.

`web.app` is the canonical FastAPI implementation. This module re-exports the
same objects so existing deployments that still start `uvicorn web.app_v2:app`
continue to work without code duplication.
"""

from web.app import *  # noqa: F401,F403
>>>>>>> 7b0f146118bc27dc90e98577c1745288ebb202c5

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
from datetime import datetime
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


def load_config() -> dict:
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
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)


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
    logger.info("访问 / 路由")
    try:
        config = load_config()
        logger.info(f"加载的 config: {config}")
        web_cfg = config.get("web", {})
        logger.info(f"web_cfg: {web_cfg}, password: {web_cfg.get('password')}")
        
        # 检查是否已设置密码
        if not web_cfg.get("password"):
            logger.info("密码为空，重定向到 /setup")
            return RedirectResponse("/setup")
        
        if verify_session(request):
            return RedirectResponse("/dashboard")
        
        logger.info("渲染 login.html")
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
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
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
async def gallery(request: Request, page: int = Query(1, ge=1), _=Depends(require_auth)):
    """推送历史画廊"""
    limit = 24
    offset = (page - 1) * limit
    
    # 获取推送历史
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
    """设置页面"""
    config = load_config()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "active_page": "settings",
        "config": config
    })


# ============ API 路由 ============

class FeedbackRequest(BaseModel):
    illust_id: int
    action: str  # 'like' | 'dislike'

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
    """保存配置"""
    try:
        config = load_config()
        
        # 1. IP / Profiler
        if "profiler" not in config: config["profiler"] = {}
        config["profiler"]["ip_weight_discount"] = req.ip_weight_discount
        # 保存 Danbooru 凭证到 config (虽然之前脚本只读 env，但为了回显我们要存)
        config["profiler"]["danbooru_login"] = req.danbooru_login
        config["profiler"]["danbooru_api_key"] = req.danbooru_api_key
        
        # 2. Strategies
        config["strategies"] = req.strategies
        
        # 3. Pixiv User ID
        if "pixiv" not in config: config["pixiv"] = {}
        config["pixiv"]["user_id"] = req.user_id
        
        # 4. Scheduler
        if "scheduler" not in config: config["scheduler"] = {}
        config["scheduler"]["cron"] = req.cron
        
        # 5. Filter / R18
        if "filter" not in config: config["filter"] = {}
        config["filter"]["r18_mode"] = req.r18_mode
        
        # 6. Proxy
        # 确保 notifier.telegram 结构存在
        if "notifier" not in config:
            config["notifier"] = {}
        if "telegram" not in config["notifier"]:
            config["notifier"]["telegram"] = {}
        
        # 处理代理 URL：如果是空字符串或 "None"，则设为 None
        if req.proxy_url and req.proxy_url.strip() and req.proxy_url.strip().lower() != "none":
            config["notifier"]["telegram"]["proxy_url"] = req.proxy_url.strip()
        else:
            # 清除代理设置
            config["notifier"]["telegram"]["proxy_url"] = None
                
        save_config(config)
        return {"success": True}
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
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

@app.get("/api/search-tag")
async def search_tag(q: str = Query(..., min_length=1), _=Depends(require_auth)):
    """模糊搜索 XP 画像中的标签"""
    try:
        conn = await db.get_db()
        results = []
        
        # 1. 先搜索 xp_profile 表 (XP 画像)
        try:
            cursor = await conn.execute(
                "SELECT tag, weight FROM xp_profile WHERE tag LIKE ? ORDER BY weight DESC LIMIT 20",
                (f"%{q}%",)
            )
            profile_rows = await cursor.fetchall()
            for row in profile_rows:
                results.append({"tag": row[0], "weight": row[1], "source": "xp_profile"})
        except Exception as e:
            logger.warning(f"搜索 xp_profile 表失败（可能表不存在）: {e}")
        
        # 2. 如果结果太少，搜索 xp_bookmarks 表 (收藏数据)
        if len(results) < 5:
            try:
                cursor = await conn.execute(
                    "SELECT DISTINCT tag FROM xp_bookmarks WHERE tag LIKE ? LIMIT 20",
                    (f"%{q}%",)
                )
                bookmark_rows = await cursor.fetchall()
                for row in bookmark_rows:
                    # 避免重复
                    if not any(r["tag"] == row[0] for r in results):
                        results.append({"tag": row[0], "weight": 0.0, "source": "xp_bookmarks"})
            except Exception as e:
                logger.warning(f"搜索 xp_bookmarks 表失败: {e}")
        
        await conn.close()
        
        # 3. 如果还是没有结果，返回空列表
        return {"success": True, "results": results}
    except Exception as e:
        logger.error(f"搜索标签失败: {e}")
        return {"success": False, "error": str(e)}

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


@app.get("/health")
async def health():
    """健康检查端点 (无需认证)"""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


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
async def api_gallery(request: Request, page: int = 1, limit: int = 24, _=Depends(require_auth)):
    """获取推送历史 (API)"""
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
    """
    服务端图片代理
    解决前端无法直接访问外网图床的问题
    """
    config = load_config()
    # 复用 Telegram 配置的代理
    proxy = config.get("notifier", {}).get("telegram", {}).get("proxy_url")
    
    # 处理代理 URL：如果是 None、空字符串或字符串 "None"，则设为 None
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
                logger.warning(f"代理请求 {url} 失败 (proxy={proxy}): {e}")
                continue
                
    # 失败时返回占位图
    return RedirectResponse("https://via.placeholder.com/300?text=Load+Failed")

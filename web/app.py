"""
Web UI - FastAPI 后端
素色设计，支持开关
"""
import hashlib
import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

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

# 会话存储（简易实现）
sessions: dict[str, datetime] = {}
SESSION_EXPIRE_HOURS = 24


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


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
    config = load_config()
    web_cfg = config.get("web", {})
    
    # 检查是否已设置密码
    if not web_cfg.get("password"):
        return RedirectResponse("/setup")
    
    if verify_session(request):
        return RedirectResponse("/dashboard")
    
    return get_login_page()


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    """首次设置密码页"""
    config = load_config()
    if config.get("web", {}).get("password"):
        return RedirectResponse("/")
    
    return get_setup_page()


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
    xp_profile = await db.get_xp_profile()
    top_tags = sorted(xp_profile.items(), key=lambda x: x[1], reverse=True)[:20]
    
    return get_dashboard_page(top_tags)


# ============ API 路由 ============

class FeedbackRequest(BaseModel):
    illust_id: int
    action: str  # 'like' | 'dislike'


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


# ============ HTML 模板 ============

def get_base_styles() -> str:
    """素色UI样式"""
    return """
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            color: #333;
            line-height: 1.6;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            padding: 2rem;
        }
        .card {
            background: #fff;
            border-radius: 8px;
            padding: 2rem;
            margin-bottom: 1rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        h1, h2 { color: #222; margin-bottom: 1rem; }
        input, button {
            padding: 0.75rem 1rem;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 1rem;
            width: 100%;
            margin-bottom: 1rem;
        }
        button {
            background: #333;
            color: #fff;
            cursor: pointer;
            border: none;
        }
        button:hover { background: #555; }
        .tag-list { display: flex; flex-wrap: wrap; gap: 0.5rem; }
        .tag {
            background: #eee;
            padding: 0.5rem 1rem;
            border-radius: 20px;
            font-size: 0.9rem;
        }
        .tag-weight {
            color: #666;
            font-size: 0.8rem;
            margin-left: 0.5rem;
        }
        nav {
            background: #333;
            padding: 1rem 2rem;
            margin-bottom: 2rem;
        }
        nav a {
            color: #fff;
            text-decoration: none;
            margin-right: 1.5rem;
        }
        nav a:hover { text-decoration: underline; }
    </style>
    """


def get_login_page() -> str:
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>登录 - Pixiv-XP-Pusher</title>
        {get_base_styles()}
    </head>
    <body>
        <div class="container">
            <div class="card" style="max-width: 400px; margin: 5rem auto;">
                <h1>Pixiv-XP-Pusher</h1>
                <p style="color: #666; margin-bottom: 2rem;">请输入密码登录</p>
                <form method="post" action="/login">
                    <input type="password" name="password" placeholder="密码" required>
                    <button type="submit">登录</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """


def get_setup_page() -> str:
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>初始设置 - Pixiv-XP-Pusher</title>
        {get_base_styles()}
    </head>
    <body>
        <div class="container">
            <div class="card" style="max-width: 400px; margin: 5rem auto;">
                <h1>首次设置</h1>
                <p style="color: #666; margin-bottom: 2rem;">请设置访问密码</p>
                <form method="post" action="/setup">
                    <input type="password" name="password" placeholder="设置密码 (至少6位)" required minlength="6">
                    <input type="password" name="confirm" placeholder="确认密码" required>
                    <button type="submit">确认</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """


def get_dashboard_page(top_tags: list) -> str:
    tags_html = ""
    for tag, weight in top_tags:
        tags_html += f'<span class="tag">{tag}<span class="tag-weight">{weight:.2f}</span></span>'
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Dashboard - Pixiv-XP-Pusher</title>
        {get_base_styles()}
    </head>
    <body>
        <nav>
            <a href="/dashboard">Dashboard</a>
            <a href="/logout">登出</a>
        </nav>
        <div class="container">
            <div class="card">
                <h2>🎯 XP 画像 Top 20</h2>
                <div class="tag-list">
                    {tags_html if tags_html else '<p style="color:#666;">暂无数据，请先运行一次推送任务</p>'}
                </div>
            </div>
            
            <div class="card">
                <h2>⚡ 快速操作</h2>
                <p style="color: #666;">使用命令行执行: <code>python main.py --once</code></p>
            </div>
        </div>
    </body>
    </html>
    """

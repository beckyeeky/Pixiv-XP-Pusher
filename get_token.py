#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pixiv OAuth Flow Script
Based on: https://gist.github.com/upbit/6edda27cb1644e94183291109b8a5fde
Adapted for Pixiv-XP-Pusher with auto-config saving.
"""
import time
import json
import re
import sys
import yaml
from pathlib import Path
from base64 import urlsafe_b64encode
from hashlib import sha256
from secrets import token_urlsafe
from urllib.parse import urlencode
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from webdriver_manager.chrome import ChromeDriverManager

# Constants
USER_AGENT = "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
AUTH_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"

def s256(data):
    """S256 transformation method."""
    return urlsafe_b64encode(sha256(data).digest()).rstrip(b"=").decode("ascii")

def oauth_pkce(transform):
    """Proof Key for Code Exchange by OAuth Public Clients (RFC7636)."""
    code_verifier = token_urlsafe(32)
    code_challenge = transform(code_verifier.encode("ascii"))
    return code_verifier, code_challenge

def save_to_config(access_token, refresh_token, user_id, expires_in, target_key="refresh_token"):
    """Save the obtained tokens to config.yaml."""
    print(f"\n[INFO] Saving tokens to config.yaml...")
    try:
        config_path = Path(__file__).parent / "config.yaml"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        else:
            config = {}
        
        providers = config.setdefault("providers", {})
        legacy_pixiv = config.get("pixiv") if isinstance(config.get("pixiv"), dict) else {}
        pixiv_entries = [provider for provider in providers.values()
                         if isinstance(provider, dict) and provider.get("type") == "pixiv"]
        if len(pixiv_entries) > 1:
            raise ValueError("config.yaml 配置了多个 Pixiv Provider")
        pixiv_provider = pixiv_entries[0] if pixiv_entries else providers.setdefault("pixiv", {
            "type": "pixiv",
            "refresh_token": legacy_pixiv.get("refresh_token", ""),
            "sync_token": legacy_pixiv.get("sync_token", ""),
            "user_id": legacy_pixiv.get("user_id", 0),
        })

        if not pixiv_provider.get("user_id"):
            pixiv_provider["user_id"] = int(user_id) if user_id else 0
            print(f"[INFO] Auto-set user_id to {user_id}")
        else:
            print(f"[INFO] Preserving existing user_id in config: {pixiv_provider['user_id']} (Login User: {user_id})")

        pixiv_provider[target_key] = refresh_token
        # Optional: save access token if needed (only for main token usually, but simpler to skip)
        # config["pixiv"]["access_token"] = access_token 
        
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        
        print("✅ Config saved successfully!")
    except Exception as e:
        print(f"❌ Failed to save config: {e}")

def login():
    print("[INFO] Starting Chrome with Performance Logging enabled...")
    
    caps = DesiredCapabilities.CHROME.copy()
    caps["goog:loggingPrefs"] = {"performance": "ALL"}  # enable performance logs
    
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-infobars")
    # Merge capabilities into options
    for key, value in caps.items():
        options.set_capability(key, value)
    
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        print(f"❌ Failed to launch Chrome: {e}")
        return

    code_verifier, code_challenge = oauth_pkce(s256)
    login_params = {
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "client": "pixiv-android",
    }
    
    url = f"{LOGIN_URL}?{urlencode(login_params)}"
    print(f"[INFO] Opening Login URL...")
    driver.get(url)

    print("\n" + "="*50)
    print("👉 Please login in the browser window.")
    print("The script is waiting for redirect to 'https://accounts.pixiv.net/post-redirect'...")
    print("="*50 + "\n")

    while True:
        # wait for login redirect
        if driver.current_url.startswith("https://accounts.pixiv.net/post-redirect"):
            break
        time.sleep(1)

    print("[INFO] Detected redirect! Scanning logs for code...")

    # filter code url from performance logs
    code = None
    try:
        logs = driver.get_log('performance')
        for row in logs:
            data = json.loads(row.get("message", "{}"))
            message = data.get("message", {})
            if message.get("method") == "Network.requestWillBeSent":
                req_url = message.get("params", {}).get("documentURL")
                if req_url and req_url.startswith("pixiv://"):
                    match = re.search(r'code=([^&]*)', req_url)
                    if match:
                        code = match.groups()[0]
                        print(f"[INFO] Captured Code from Network Logs: {code[:10]}...")
                        break
    except Exception as e:
        print(f"❌ Error reading logs: {e}")

    try:
        driver.quit()
    except:
        pass

    if not code:
        print("❌ Failed to capture code. logic error or browser closed too early?")
        return

    print("[INFO] Exchanging code for tokens...")
    
    try:
        response = requests.post(
            AUTH_TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "include_policy": "true",
                "redirect_uri": REDIRECT_URI,
            },
            headers={
                "user-agent": USER_AGENT,
                "app-os-version": "14.6",
                "app-os": "ios",
            },
            timeout=30
        )
        
        if response.status_code != 200:
            print(f"❌ Token exchange failed: {response.status_code}")
            print(response.text)
            return

        data = response.json()
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        user_id = data.get("user", {}).get("id")
        expires_in = data.get("expires_in")
        
        print("\n🎉 Token Get!")
        print(f"User ID: {user_id}")
        print(f"Refresh Token: {refresh_token}")
        
        print(f"Refresh Token: {refresh_token}")
        
        if refresh_token:
            print("\n" + "="*50)
            print("💾 Token 保存选项")
            print("1. 保存为主 Token (refresh_token) - 用于搜索/推荐 [覆盖]")
            print("2. 保存为同步 Token (sync_token) - 仅用于收藏/关注")
            print("3. 仅显示，不保存")
            
            save_choice = input("\n请选择保存位置 (1/2/3) [默认1]: ").strip()
            
            target_key = "refresh_token"
            if save_choice == "2":
                target_key = "sync_token"
            elif save_choice == "3":
                print("已跳过保存。请手动复制上面的 Refresh Token。")
                return

            save_to_config(access_token, refresh_token, user_id, expires_in, target_key)

    except Exception as e:
        print(f"❌ Error during token request: {e}")

def manual_input():
    """Allow manual token input for headless servers."""
    print("\n" + "="*50)
    print("📝 手动输入模式 (Headless Server)")
    print("="*50)
    print("\n如果您在服务器上无法打开浏览器，请：")
    print("1. 在本地 Windows/Mac 电脑上运行 python get_token.py")
    print("2. 获取 refresh_token 后复制到此处\n")
    
    token = input("请粘贴 refresh_token (留空取消): ").strip()
    if not token:
        print("已取消")
        return
    
    user_id = input("请输入 Pixiv User ID (留空自动获取): ").strip()
    
    print("\n保存位置:")
    print("1. refresh_token (主)")
    print("2. sync_token (同步)")
    pk_choice = input("选择 (1/2) [默认1]: ").strip()
    target_key = "sync_token" if pk_choice == "2" else "refresh_token"

    save_to_config(None, token, user_id or None, None, target_key)
    print("✅ Token 已保存!")

if __name__ == "__main__":
    print("\n请选择获取 Token 的方式:")
    print("  1. 浏览器登录 (需要 GUI 环境)")
    print("  2. 手动粘贴 Token (服务器环境)")
    
    choice = input("\n请选择 (1/2): ").strip()
    
    if choice == "2":
        manual_input()
    else:
        login()
    
    input("\nPress Enter to exit...")

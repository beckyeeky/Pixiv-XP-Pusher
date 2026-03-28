import os
import sys
import subprocess
import time
import shutil
import yaml

# 设置编码
sys.stdout.reconfigure(encoding='utf-8')

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def print_header(title):
    print("\n" + "=" * 40)
    print(f"   {title}")
    print("=" * 40 + "\n")

def run_command(cmd, shell=True, ignore_errors=False):
    try:
        if ignore_errors:
            subprocess.run(cmd, shell=shell, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(cmd, shell=shell, check=True)
        return True
    except subprocess.CalledProcessError:
        return False

def to_int_or_raw(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value

def check_env():
    print_header("[1/8] 环境检查")
    
    # Check Conda
    if shutil.which("conda"):
        print("   * 检测到 Conda")
        # Check if env exists
        result = subprocess.run("conda env list", shell=True, capture_output=True, text=True)
        if "pixiv-xp" not in result.stdout:
            print("   * 正在创建环境 pixiv-xp (可能需要几分钟)...")
            run_command("conda create -n pixiv-xp python=3.11 -y", ignore_errors=True)
        print("   * 注意：请确保在 Conda 环境中运行此脚本")
    else:
        print("   * 未检测到 Conda，使用系统 Python")
    
    print(f"   * Python 版本: {sys.version.split()[0]}")
    time.sleep(1)

def install_deps():
    print_header("[2/8] 安装依赖")
    print("   正在后台安装，请稍候...")
    run_command("pip install -r requirements.txt -q")
    print("   依赖安装完成")
    time.sleep(1)

def init_db():
    print_header("[3/8] 初始化数据库")
    run_command(f'"{sys.executable}" -c "import asyncio; from database import init_db; asyncio.run(init_db())"')
    print("   数据库已就绪")
    time.sleep(1)

def setup_token():
    print_header("[4/8] 获取 Pixiv Token")
    print("   Token 用于访问 Pixiv API 完整功能")
    print("   没有 Token 将以访客模式运行(功能受限)\n")
    
    choice = input("   是否获取 Token? (y/n): ").strip().lower()
    if choice == 'y':
        run_command(f'"{sys.executable}" get_token.py')

def setup_user_id():
    print_header("[5/8] 配置收藏分析目标")
    print("   系统会分析指定用户的公开收藏来构建 XP 画像")
    print("   输入 User ID (可在个人主页 URL 找到)\n")
    
    user_id = input("   请输入 User ID (直接回车跳过): ").strip()
    if user_id:
        update_config_value(["pixiv", "user_id"], to_int_or_raw(user_id))
        print(f"   已保存 User ID: {user_id}")

def setup_schedule():
    print_header("[6/8] 定时任务设置")
    print("   设定每天自动运行的时间 (24小时制)")
    print("   例如: 12:30, 08:00, 23:59\n")
    
    t_input = input("   请输入每天推送时间 (默认为 12:00): ").strip()
    if t_input:
        try:
            t_input = t_input.replace("：", ":")
            h, m = map(int, t_input.split(":"))
            if 0 <= h < 24 and 0 <= m < 60:
                cron = f"{m} {h} * * *"
                update_config_value(["scheduler", "cron"], cron)
                print(f"   已更新: 每天 {h:02d}:{m:02d} 执行")
            else:
                print("   ⚠️ 时间超出范围，未修改")
        except ValueError:
            print("   ⚠️ 格式错误，未修改")

def setup_ai():
    print_header("[7/8] AI 标签优化 (可选)")
    print("   使用 AI 过滤无意义标签、归类同义标签")
    print("   支持 OpenAI 及兼容 API (如 DeepSeek)\n")
    
    choice = input("   是否配置 AI? (y/n): ").strip().lower()
    if choice == 'y':
        api_key = input("   API Key: ").strip()
        base_url = input("   API Base URL (留空使用 OpenAI): ").strip()
        model = input("   模型名称 (默认 gpt-4o-mini): ").strip() or "gpt-4o-mini"
        
        update_config_value(["profiler", "ai", "enabled"], True)
        update_config_value(["profiler", "ai", "api_key"], api_key)
        if base_url:
            update_config_value(["profiler", "ai", "base_url"], base_url)
        update_config_value(["profiler", "ai", "model"], model)
        print("   AI 已配置")
    else:
        print("   已跳过 AI 配置")

def setup_notifier():
    print_header("[8/8] 配置推送方式")
    print("   1. Telegram Bot")
    print("   2. OneBot / QQ")
    print("   3. 跳过\n")
    
    choice = input("   请选择 (1/2/3): ").strip()
    
    if choice == '1':
        token = input("   Bot Token: ").strip()
        chat_id = input("   Chat ID (支持负数群组ID): ").strip()
        notifier_types = load_config_value(["notifier", "types"], default=[])
        if "telegram" not in notifier_types:
            notifier_types.append("telegram")
        update_config_value(["notifier", "types"], notifier_types)
        update_config_value(["notifier", "telegram", "bot_token"], token)
        update_config_value(["notifier", "telegram", "chat_ids"], [to_int_or_raw(chat_id)])
        print("   Telegram 已配置")
        
    elif choice == '2':
        url = input("   WebSocket URL: ").strip()
        tid = input("   目标 QQ/群号: ").strip()
        type_choice = input("   类型 (1=私聊, 2=群聊): ").strip()
        notifier_types = load_config_value(["notifier", "types"], default=[])
        if "onebot" not in notifier_types:
            notifier_types.append("onebot")
        update_config_value(["notifier", "types"], notifier_types)
        update_config_value(["notifier", "onebot", "ws_url"], url)
        if type_choice == '2':
            update_config_value(["notifier", "onebot", "group_id"], to_int_or_raw(tid))
            update_config_value(["notifier", "onebot", "push_to_group"], True)
            update_config_value(["notifier", "onebot", "push_to_private"], False)
        else:
            update_config_value(["notifier", "onebot", "private_id"], to_int_or_raw(tid))
            update_config_value(["notifier", "onebot", "push_to_private"], True)
            update_config_value(["notifier", "onebot", "push_to_group"], False)
        print("   OneBot 已配置")

def load_config_file():
    if not os.path.exists("config.yaml"):
        return {}
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"   读取配置失败: {e}")
        return {}

def save_config_file(config):
    try:
        with open("config.yaml", "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
    except Exception as e:
        print(f"   保存配置失败: {e}")

def update_config_value(path, value):
    config = load_config_file()
    current = config
    for key in path[:-1]:
        if not isinstance(current.get(key), dict):
            current[key] = {}
        current = current[key]
    current[path[-1]] = value
    save_config_file(config)

def load_config_value(path, default=None):
    config = load_config_file()
    current = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current

def main_menu():
    while True:
        clear_screen()
        print_header("Pixiv-XP-Pusher 主菜单")
        print("   1. 立即启动并常驻 (推荐)")
        print("   2. 仅启动定时任务")
        print("   3. 单次运行 (调试用)")
        print("   4. 启动网页管理")
        print("   5. 同时启动 Web + 推送服务")
        print("   6. 获取 Token")
        print("   7. 重新运行配置")
        print("   0. 退出\n")
        
        choice = input("   请选择: ").strip()
        
        if choice == '1':
            print("\n   🚀 正在立即启动任务，并在完成后转为后台常驻...")
            run_command(f'"{sys.executable}" main.py --now')
            input("\n   按回车键继续...")
            
        elif choice == '2':
            print("\n   ⏰ 启动定时调度器 (Ctrl+C 停止)")
            run_command(f'"{sys.executable}" main.py')
            input("\n   按回车键继续...")

        elif choice == '3':
            print("\n   🔧 执行单次推送调试...")
            run_command(f'"{sys.executable}" main.py --once')
            input("\n   按回车键继续...")
            
        elif choice == '4':
            print("\n   启动网页管理 (http://localhost:8000)")
            # Fix: Wrap executable path in quotes to handle spaces in path (e.g. "C:\Program Files\...")
            run_command(f'"{sys.executable}" -m uvicorn web.app:app --host 0.0.0.0 --port 8000')
            input("\n   按回车键继续...")
            
        elif choice == '5':
            print("\n   🌐 同时启动 Web 管理 + 推送服务")
            print("   Web: http://localhost:8000")
            print("   日志输出到终端，按 Ctrl+C 停止所有服务\n")
            
            # Windows 和 Linux/macOS 不同的后台启动方式
            if os.name == 'nt':  # Windows
                # Windows 使用 start 命令启动新窗口
                import threading
                import time
                
                def start_web():
                    os.system(f'"{sys.executable}" -m uvicorn web.app:app --host 0.0.0.0 --port 8000')
                
                # 在新线程中启动 Web 服务器
                web_thread = threading.Thread(target=start_web, daemon=True)
                web_thread.start()
                time.sleep(2)  # 给 Web 服务器一点启动时间
                
                # 前台运行推送服务
                print("   Web 服务器已启动，现在启动推送服务...")
                run_command(f'"{sys.executable}" main.py --now')
                
            else:  # Linux/macOS
                # 使用 & 后台运行 Web 服务器
                print("   启动 Web 服务器到后台...")
                os.system(f'"{sys.executable}" -m uvicorn web.app:app --host 0.0.0.0 --port 8000 > web.log 2>&1 &')
                time.sleep(2)
                
                # 前台运行推送服务
                print("   Web 服务器已启动，现在启动推送服务...")
                run_command(f'"{sys.executable}" main.py --now')
            
        elif choice == '6':
            run_command(f'"{sys.executable}" get_token.py')
            input("\n   按回车键继续...")
            
        elif choice == '7':
            if os.path.exists(".initialized"):
                os.remove(".initialized")
            return  # restart wizard
            
        elif choice == '0':
            sys.exit(0)

def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    
    if os.path.exists(".initialized"):
        main_menu()
    
    # Wizard
    clear_screen()
    print_header("首次运行向导")
    print("   欢迎使用 Pixiv-XP-Pusher")
    input("\n   按回车键开始配置...")
    
    check_env()
    install_deps()
    init_db()
    setup_token()
    setup_user_id()
    setup_schedule()
    setup_ai()
    setup_notifier()
    
    with open(".initialized", "w") as f:
        f.write("done")
        
    print("\n   配置完成！")
    time.sleep(1)
    main_menu()

if __name__ == "__main__":
    main()

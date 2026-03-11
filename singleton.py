"""
单实例锁模块 - 防止重复启动多个进程
"""
import os
import sys
import atexit
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
PID_FILE = Path(__file__).parent / "bot.pid"


def check_single_instance():
    """
    检查是否已有实例在运行
    如果存在存活的进程，则退出当前程序
    """
    if PID_FILE.exists():
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            
            # 检查进程是否存在
            os.kill(old_pid, 0)
            
            # 如果到这里，说明进程存在
            logger.error(f"❌ 已有实例在运行 (PID: {old_pid})，拒绝重复启动")
            print(f"❌ 错误：Pixiv-XP-Pusher 已有实例在运行 (PID: {old_pid})")
            print("   如需重启，请先停止现有进程或删除 bot.pid 文件")
            sys.exit(1)
            
        except (ValueError, ProcessLookupError, FileNotFoundError, PermissionError):
            # PID 文件内容无效 / 进程不存在 / 文件被删除 / 无权限检查
            # 继续启动，覆盖旧的 PID 文件
            logger.info(f"发现遗留的 PID 文件 (PID: {old_pid if 'old_pid' in dir() else 'unknown'})，进程不存在，继续启动")
            pass
    
    # 写入当前 PID
    try:
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
        logger.info(f"✅ 单实例锁已创建 (PID: {os.getpid()})")
    except Exception as e:
        logger.warning(f"⚠️ 无法写入 PID 文件: {e}")
    
    # 注册退出时清理
    atexit.register(remove_pid_file)


def remove_pid_file():
    """
    清理 PID 文件（在程序退出时自动调用）
    """
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
            logger.info("✅ 单实例锁已清理")
    except Exception as e:
        logger.debug(f"清理 PID 文件时出错: {e}")


def force_unlock():
    """
    强制删除 PID 文件（用于手动解锁）
    """
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
            logger.info("✅ 已强制删除 PID 文件")
            return True
        return False
    except Exception as e:
        logger.error(f"删除 PID 文件失败: {e}")
        return False

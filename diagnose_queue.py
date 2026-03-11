#!/usr/bin/env python3
"""
诊断脚本：检查 _queue_limit 状态
"""
import asyncio
import sys
sys.path.insert(0, '/opt/Pixiv-XP-Pusher')

# 记录导入时的状态
print("=== 模块导入阶段 ===")
import main
print(f"_queue_limit 对象: {main._queue_limit}")
print(f"_queue_limit._value: {main._queue_limit._value}")

# 模拟 main_task 的队列检查逻辑
async def diagnose():
    print("\n=== 事件循环阶段 ===")
    print(f"_queue_limit._value: {main._queue_limit._value}")
    
    # 尝试多次获取 semaphore
    for i in range(5):
        try:
            await asyncio.wait_for(main._queue_limit.acquire(), timeout=0)
            print(f"第 {i+1} 次获取: 成功")
        except asyncio.TimeoutError:
            print(f"第 {i+1} 次获取: 失败 (队列已满)")
            break
    
    print(f"\n最终 _value: {main._queue_limit._value}")

if __name__ == "__main__":
    asyncio.run(diagnose())

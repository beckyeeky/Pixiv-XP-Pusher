# Telegram 命令与交互说明

以下为当前实现中的主要命令（以 `notifier/telegram.py` 为准）：

- `/start`, `/menu`：控制面板
- `/push`：推送向导（含指定作品/画师等）
- `/search`：交互式搜索
- `/schedule`：查看/修改计划时间
- `/xp`：查看 XP 画像
- `/stats`：查看策略成功率
- `/status`：查看系统状态
- `/block`, `/unblock`：标签屏蔽
- `/mute`, `/unmute`：标签静音
- `/block_artist`, `/unblock_artist`：画师屏蔽
- `/batch`：批量模式设置
- `/rich`：Rich Message 模式设置（inline 按钮，或 `on` / `off` / `fallback on|off` / `mode photo|rich_card|hybrid` / `test`）
- `/help`：帮助
- `/restart`：重启服务（需部署环境支持）

> 说明：命令交互与按钮文案可能随版本调整，建议升级后以 `/help` 与菜单实际显示为准。

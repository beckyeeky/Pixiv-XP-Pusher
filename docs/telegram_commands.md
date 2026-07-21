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
- `/tags`：打开 `🏷️ 标签管理与审核`；查看常用 Tag（Preference Profile 的已分类 Top Tag）、待人工决定数与高权重未分类候选，并可预览后确认 Gemini 分类或批量判定当前待人工队列
  - `🔗 语义映射审核`：每次显示一个 Tag Mapping Candidate review group。浏览和跳过不写入；接受为 Tag Alias、接受为 Search Alias 或拒绝都需要单独预览并确认。确认时会重新读取候选，AI Relationship Recommendation 只作为建议，不能自动建立 Alias。
- `/menu` → `🏷️ 标签审核`：与 `/tags` 打开同一标签管理与审核菜单
- `/rich`：Rich Message 模式设置（inline 按钮，或 `on` / `off` / `fallback on|off` / `mode photo|rich_card|hybrid` / `test`）
- `/help`：帮助
- `/restart`：通过 systemd 同时重启 `pixiv-pusher.service` 与 `pixiv-web.service`（需部署环境支持）

> 说明：命令交互与按钮文案可能随版本调整，建议升级后以 `/help` 与菜单实际显示为准。

# Pixiv-XP-Pusher (Enhanced Fork)

> 基于 Pixiv 收藏构建 XP（偏好）画像，自动抓取候选作品并推送到 Telegram / OneBot / AstrBot。

README 只保留**项目介绍 + 最小可用上手**。更细的配置、运维、命令与开发文档已拆分到 `docs/`。

---

## 项目简介

Pixiv-XP-Pusher 是一个“抓取 + 过滤 + 推送”流水线：
- 从 Pixiv 收藏构建标签偏好画像（XP）
- 通过多策略抓取候选作品（搜索/关联/排行/订阅）
- 过滤、打分、去重后推送到通知渠道
- 提供 Web 控制台管理配置与标签

---

## 核心特性

- **多策略推荐**：`xp_search` / `related` / `ranking` / `subscription`
- **多通知器**：Telegram、OneBot、AstrBot
- **可视化管理**：Web 配置页、标签页、历史画廊、导入导出
- **可运维**：内置数据库维护与推荐效果评估脚本

---

## 快速开始（最小路径）

```bash
git clone https://github.com/beckyeeky/Pixiv-XP-Pusher.git
cd Pixiv-XP-Pusher
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

至少填写：
- `pixiv.refresh_token`
- `notifier.types`
- 对应通知器的必填字段（如 Telegram 的 `bot_token`、`chat_ids`）

首次建议先单次验证：

```bash
python main.py --once
```

验证通过后再常驻调度：

```bash
python main.py --now
```

---

## 常用入口

- 主程序：`python main.py --help`
- Web 控制台：`uvicorn web.app:app --host 0.0.0.0 --port 8000`
- Docker Compose：`docker-compose up -d`

---

## 文档导航（详细内容）

- [快速上手（详细版）](docs/quickstart.md)
- [配置说明](docs/configuration.md)
- [Telegram 命令与交互说明](docs/telegram_commands.md)
- [运维与诊断脚本](docs/operations.md)
- [测试与开发说明](docs/development.md)
- [功能矩阵](docs/feature_matrix.md)
- [Notifier 能力矩阵](docs/notifier_capabilities.md)

---

## License

沿用原项目 License（如仓库新增 LICENSE，请以该文件为准）。

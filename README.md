# Pixiv-XP-Pusher

> 基于 Pixiv 收藏自动构建 XP / 兴趣画像，搜索候选作品，过滤去重，并推送到 Telegram、OneBot 或 AstrBot。

这是 [bwwq/Pixiv-XP-Pusher](https://github.com/bwwq/Pixiv-XP-Pusher) 的 fork / 增强版。原项目已经提供了清晰的基础推荐、推送和部署流程；本仓库在此基础上继续增强了 Web 管理、标签分类、推荐多样性、数据库维护、推荐效果评估和 Telegram Rich Message 等能力。

本项目使用 MIT License 开源，详见 [LICENSE](LICENSE)。

---

## 这个项目做什么

Pixiv-XP-Pusher 会读取你的 Pixiv 收藏与配置，生成一套标签偏好画像，然后按多种策略抓取候选作品：

- `xp_search`：根据 XP 画像组合标签搜索作品
- `related`：基于已推送或已喜欢作品继续发现关联作品
- `ranking`：从日榜、周榜、月榜中筛选候选
- `subscription`：追踪关注画师或手动配置画师的新作品

候选作品会经过过滤、去重、评分和排序，再通过通知器推送。你也可以用 Web 控制台管理配置、查看推送历史，或通过 Telegram Bot 指令即时操作。

---

## 主要特性

- XP 画像分析：从收藏中统计标签权重，支持时间衰减、停用词和探索率
- 多策略推荐：搜索、排行榜、关联作品、关注画师更新
- 标签分类增强：可区分 `feature` 与 `ip` 标签，让白发、兽耳、黑丝等视觉特征优先参与匹配与展示
- 多样性控制：支持同画师衰减、同 IP 衰减，减少连续刷屏
- AI 增强：可选 LLM 标签清洗、同义词合并、Embedding 语义匹配和 AI 精排
- 智能过滤：收藏数阈值、R-18 模式、AI 作品过滤、动图过滤、内容类型过滤、黑名单标签
- 多渠道推送：Telegram、OneBot、AstrBot
- Telegram 交互：菜单、手动推送、搜索、查看画像、查看统计、屏蔽标签/画师、调整计划任务
- Web 控制台：仪表盘、配置管理、标签页、历史画廊、导入导出
- 运维工具：数据库维护、推荐效果评估、Danbooru IP 标签同步、systemd 示例

---

## 快速开始

### 方式一：Docker Compose

适合部署到 VPS 或长期运行的机器。

```bash
git clone https://github.com/beckyeeky/Pixiv-XP-Pusher.git
cd Pixiv-XP-Pusher
cp config.example.yaml config.yaml
```

先编辑 `config.yaml`，至少填写：

- `pixiv.refresh_token`
- `notifier.types`
- 对应通知器配置，例如 Telegram 的 `bot_token`、`chat_ids`、`allowed_users`

启动服务：

```bash
docker-compose up -d --build
```

查看日志：

```bash
docker-compose logs -f pusher
docker-compose logs -f web
```

Web 控制台默认地址：

```text
http://127.0.0.1:8000
```

也可以使用部署脚本：

```bash
chmod +x deploy.sh
./deploy.sh start
./deploy.sh logs
```

### 方式二：本地直接运行

适合开发、调试或 Windows 本机使用。

```bash
git clone https://github.com/beckyeeky/Pixiv-XP-Pusher.git
cd Pixiv-XP-Pusher
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

获取 Pixiv refresh token：

```bash
python get_token.py
```

执行一次推送验证：

```bash
python main.py --once
```

启动调度模式，并在启动时立即执行一次：

```bash
python main.py --now
```

启动 Web 控制台：

```bash
python -m uvicorn web.app:app --host 0.0.0.0 --port 8000
```

Windows 也可以直接运行：

```bat
start.bat
```

或使用交互式引导：

```bash
python launcher.py
```

---

## 配置文件

配置文件为 `config.yaml`，建议从 `config.example.yaml` 复制后修改。

最小配置示例：

```yaml
pixiv:
  refresh_token: "YOUR_PIXIV_REFRESH_TOKEN"
  sync_token: ""
  user_id: 0

strategies:
  - xp_search
  - related
  - ranking
  - subscription

notifier:
  types: [telegram]
  telegram:
    bot_token: "YOUR_TELEGRAM_BOT_TOKEN"
    chat_ids:
      - 123456789
    allowed_users:
      - 123456789

web:
  enabled: true
  require_login_password: true
  password: "YOUR_WEB_PASSWORD"
  port: 8000
```

常用推荐质量配置：

```yaml
tag_classifier:
  enabled: true

filter:
  daily_limit: 20
  r18_mode: "mixed"      # mixed / r18_only / safe
  exclude_ai: false
  skip_ugoira: true
  display_tags:
    max_ip_count: 2
  author_diversity:
    enabled: true
  ip_diversity:
    enabled: true
```

AI 功能是可选项。没有 API Key 时可以关闭，系统仍可用纯统计模式运行。

完整配置请看：

- [config.example.yaml](config.example.yaml)
- [docs/configuration.md](docs/configuration.md)

---

## 常用命令

```bash
# 查看命令帮助
python main.py --help

# 立即执行一次并退出
python main.py --once

# 启动时立即执行一次，然后保持调度运行
python main.py --now

# 重置 XP 画像缓存
python main.py --reset-xp

# 快速测试模式
python main.py --test

# 使用指定配置文件
python main.py --config ./config.yaml
```

Docker Compose：

```bash
docker-compose up -d --build
docker-compose logs -f pusher
docker-compose logs -f web
docker-compose restart
docker-compose down
```

部署脚本：

```bash
./deploy.sh start
./deploy.sh stop
./deploy.sh restart
./deploy.sh logs
./deploy.sh once
./deploy.sh reset-xp
```

---

## Telegram 指令

Telegram Bot 支持的主要命令：

| 指令 | 功能 |
|---|---|
| `/start`, `/menu` | 打开控制面板 |
| `/push` | 推送向导 |
| `/push <ID>` | 手动推送指定作品 |
| `/search` | 交互式搜索 |
| `/schedule` | 查看或修改计划时间 |
| `/xp` | 查看 XP 画像 |
| `/stats` | 查看策略成功率 |
| `/status` | 查看系统状态 |
| `/block`, `/unblock` | 屏蔽或取消屏蔽标签 |
| `/mute`, `/unmute` | 静音或取消静音标签 |
| `/block_artist`, `/unblock_artist` | 屏蔽或取消屏蔽画师 |
| `/batch` | 批量模式设置 |
| `/rich` | Rich Message 模式设置 |
| `/restart` | 通过 systemd 重启服务 |

命令细节见 [docs/telegram_commands.md](docs/telegram_commands.md)。实际菜单和按钮文案以当前 Bot 的 `/help` 为准。

---

## Web 控制台

启动后访问：

```text
http://127.0.0.1:8000
```

Web 控制台包含：

- Dashboard：查看状态、统计和任务概览
- Settings：编辑 Pixiv、推荐、过滤、通知器、AI、Web 等配置
- Tags：管理画像标签和标签权重
- Gallery：浏览推送历史
- Import / Export：导入导出配置

首次公开部署时，建议开启 `web.require_login_password` 并设置强密码。

---

## 运维与维护

数据库维护：

```bash
python scripts/db_maintenance.py overview
python scripts/db_maintenance.py backup --output ./backup/pixiv_xp.db
python scripts/db_maintenance.py cleanup --days 180
```

推荐效果评估：

```bash
python scripts/evaluate_recommendation.py
python scripts/evaluate_recommendation.py --json
```

Danbooru IP 标签同步：

```bash
export DANBOORU_LOGIN=your_login
export DANBOORU_API_KEY=your_api_key
python scripts/sync_ip_tags.py
```

systemd 示例和更多诊断命令见 [docs/operations.md](docs/operations.md)。

---

## 项目结构

```text
Pixiv-XP-Pusher/
├── main.py                 # 程序入口：一次执行、调度、重置画像
├── launcher.py             # 交互式引导与管理菜单
├── get_token.py            # Pixiv refresh token 获取工具
├── config.example.yaml     # 配置模板
├── docker-compose.yml      # pusher + web 两个服务
├── web/                    # FastAPI Web 控制台
├── notifier/               # Telegram / OneBot / AstrBot 通知器
├── scripts/                # 维护、评估、同步脚本
├── ops/                    # 运维脚本
├── docs/                   # 详细文档
└── tests/                  # 自动化测试
```

---

## 常见问题

### 如何获取 Pixiv Refresh Token？

运行：

```bash
python get_token.py
```

脚本会引导你登录 Pixiv 并保存 refresh token。服务器无法打开浏览器时，可以先在本地电脑获取 token，再复制到服务器的 `config.yaml`。

### Telegram 连不上怎么办？

国内网络通常需要代理。请在 Telegram 配置中填写代理地址：

```yaml
notifier:
  telegram:
    proxy_url: "http://127.0.0.1:7890"
```

### 点击按钮提示无权限怎么办？

把你的 Telegram User ID 加入：

```yaml
notifier:
  telegram:
    allowed_users:
      - 123456789
```

### 不想使用 AI 可以吗？

可以。关闭 `profiler.ai.enabled`、`ai.embedding.enabled`、`ai.scorer.enabled` 和 `tag_classifier.enabled` 后，项目仍会使用规则与统计方式运行。

### 可以公开部署 Web 控制台吗？

可以，但请至少开启登录密码。更推荐放在内网、反向代理认证后面，或只通过 SSH 隧道访问。

---

## 与原项目的关系

本仓库基于 [bwwq/Pixiv-XP-Pusher](https://github.com/bwwq/Pixiv-XP-Pusher) 继续开发。感谢原项目提供的基础架构、推荐流程和部署思路。

本 fork 主要补充：

- 更细的配置和文档拆分
- Web 设置、导入导出和历史画廊增强
- feature / IP 标签分类与展示排序
- IP 多样性、画师多样性等推荐去重策略
- Telegram Rich Message 实验性支持
- 数据库维护、推荐效果评估和运维脚本
- 更完整的测试覆盖

---

## License

MIT License. See [LICENSE](LICENSE).

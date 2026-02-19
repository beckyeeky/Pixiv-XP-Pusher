# Pixiv-XP-Pusher (beckyeeky Fork)

> 🎨 **智能 XP 捕获与推送系统 - 增强版**
>
> 基于用户收藏自动分析 XP（性癖/兴趣偏好）画像，全网搜索并智能推送最懂你的 Pixiv 插画。支持 Telegram / QQ (OneBot) 多渠道推送。
>
> **✨ Fork 增强特性：**
> - **IP 标签降权** - 自动识别并降低 IP/游戏/动画标题标签权重，专注视觉元素
> - **手动标签加权** - 可手动提升特定标签的推荐权重
> - **Web 配置编辑器** - 图形化界面管理所有设置，告别 YAML 编辑
> - **Danbooru IP 同步** - 自动同步最新 IP 标签列表
> - **标签调试器** - 查询标签在系统中的标准名称
> - **代理修复** - 改进的代理 URL 处理逻辑

---

## ✨ 核心特性

- 🧠 **XP 画像构建** - 深度分析你的收藏夹，提取核心 Tag 权重，比你更懂你的口味
- 🤖 **AI 增强引擎** [New!]
  - **语义匹配 (Embedding)**: 理解标签背后的深层含义，发现画风相似但标签不同的宝藏
  - **智能清洗**: 集成 LLM (OpenAI/DeepSeek) 过滤无意义标签、归并同义词
  - **多样性控制**: 智能抑制刷屏画师，确保推荐内容丰富多元
- 🔍 **混合搜索策略** -
  - **XP 搜索**: 基于画像权重的多标签组合搜索
  - **互动发现**: [New!] 深度挖掘常互动画师的社交圈，发现同好圈子
  - **关联推荐**: 自动发掘高分作品的"相似作品"，发现潜在 XP
  - **画师订阅**: 自动追踪关注画师的最新作品
  - **排行榜**: 每日/每周排行榜筛选
- 🧬 **反馈闭环** - [New!]
  - **连锁反应**: 点击 Like ❤️ 立即推送关联作品，自动回复形成消息链 (单图深度可控)，好图停不下来
  - **画师权重**: 反馈直接影响画师评分，喜欢的画师会更常出现
  - **智能屏蔽**: 厌恶达到阈值仅提示确认，尊重用户选择
- 🎭 **智能过滤** -
  - **R-18 混合模式**: 支持 纯R18 / 净网 / 混合模式 三档调节
  - 多维度去重（ID/图片指纹）
  - AI 作画检测与过滤
  - 动态阈值（热门 Tag 需高收藏，冷门 Tag 宽容度高）
- 📱 **多渠道推送** -
  - **Telegram**:
    - 支持 MediaGroup 图集、直传图片（防盗链/被墙）
    - **交互式菜单** [New!]: `/menu` 打开控制面板，按钮操作无需记指令
    - **Telegraph 批量模式** [New!]: 多图合并为 Telegraph 页面，界面简洁
    - **交互式指令**:
      - `/menu` - 📋 打开控制面板 (推荐)
      - `/push` - 立即触发推送
      - `/push <ID>` - 手动推送指定作品 ID
      - `/xp` - 查看您的 XP 画像 (Top Tags)
      - `/stats` - 查看各策略 (XP搜索/订阅/榜单) 的成功率
      - `/schedule` - 查看或修改定时任务时间
      - `/block` - 快速屏蔽讨厌的标签
      - `/block_artist` - 快速屏蔽画师 ID
  - **OneBot (QQ)**: 支持 Go-CQHTTP/Lagrange，链接卡片或图文消息，多图并发下载
  - **AstrBot** [实验性]: 通过 HTTP API 接入 AstrBot 多平台框架，支持 QQ/微信/Telegram 等

## 🚀 Fork 增强特性

### 1. IP 标签降权系统
**问题**: 系统会过度推荐 IP/游戏/动画标题标签（如 `blue_archive`, `honkai_star_rail`），而忽略视觉元素标签（如 `large_breasts`, `thighs`）。

**解决方案**:
- **自动识别**: 从 Danbooru 同步版权标签列表
- **权重折扣**: 可配置的降权系数（默认 1.0 = 不降权，0.1 = 降权 90%）
- **向后兼容**: 默认启用，不影响现有配置

**配置示例**:
```yaml
profiler:
  ip_tags: []  # 自动从 Danbooru 同步，或手动指定
  ip_weight_discount: 0.1  # IP 标签权重打 1 折
```

**同步 IP 列表**:
```bash
# 设置环境变量
export DANBOORU_LOGIN=your_username
export DANBOORU_API_KEY=your_api_key

# 运行同步脚本
python scripts/sync_ip_tags.py
```

### 2. 手动标签加权
**功能**: 可手动提升特定标签的推荐权重，突破算法限制。

**配置示例**:
```yaml
profiler:
  boost_tags:
    white_hair: 1.5      # 提升 50% 权重
    perfect_body: 2.0    # 提升 100% 权重（双倍）
    garter_straps: 1.2   # 提升 20% 权重
```

**工作原理**:
- **乘法加成**: 尊重原始权重，热门标签获得更大绝对提升
- **新标签注入**: 可强行注入从未收藏过的标签
- **实时生效**: 下次构建 XP 画像时立即应用

### 3. Web 图形化配置编辑器
**功能**: 完全替代手动编辑 `config.yaml`，提供直观的 Web 界面。

**访问方式**: `http://localhost:8000/settings`

**包含功能**:
- **IP 降权设置**: 滑块调节降权系数
- **Danbooru 凭证**: 配置 API 密钥用于同步
- **IP 同步按钮**: 一键同步最新 IP 标签列表
- **策略开关**: 勾选启用的推荐策略
- **Cron 设置**: 可视化定时任务配置
- **标签调试器**: 查询标签在系统中的标准名称

### 4. 标签调试器
**问题**: 不知道某个标签在系统中被标准化为什么名称？

**解决方案**: 在 Web 设置页面的 "XP 字典查询" 中输入关键词，系统会显示匹配的标签及其权重。

**示例**:
- 输入 `女体` → 显示 `perfect_body: 2.5`, `voluptuous: 1.8`
- 输入 `blue` → 显示 `blue_hair: 3.2`, `blue_eyes: 2.1`

### 5. 代理 URL 智能处理
**修复**: 正确处理 `None`、空字符串和字符串 `"None"` 代理配置，避免 `http://None:80` 连接错误。

### 6. 增强的 AI 屏蔽系统
**问题**: 原系统只依赖 Pixiv 官方的 `ai_type` 标记，效果有限。

**增强方案**:
1. **Pixiv 官方标记**: 过滤 `ai_type == 2`（纯 AI）
2. **标签关键词检测**: 新增智能检测，识别包含 AI 相关关键词的标签
   - 支持多语言: 英文、中文、日文
   - 常见关键词: `AI`, `Stable Diffusion`, `Midjourney`, `AI生成`, `AIイラスト` 等

**配置建议**:
```yaml
filter:
  exclude_ai: true  # 启用增强 AI 过滤
  blacklist_tags:
    - "R-18G"
    # 可选的 AI 相关黑名单（如果不想看到任何 AI 相关内容）
    - "AI"
    - "AI生成"
    - "AI art"
    - "AIイラスト"
```
- ⚙️ **完全自动化**
  - 智能调度器，支持多时间点运行
  - **每日日报**: 每天生成 XP 变化报告与策略统计
  - **健康检查**: 每 30 分钟自动检测 Telegram 连接，断线自动重连 [New!]
  - **Web API**: `/health` 端点，易于接入外部监控
- 🛠️ **懒人配置** - 提供交互式引导脚本 `launcher.py`，一键完成环境与参数配置

---

## 🚀 快速开始

### 方式一：Docker 部署 (推荐)

最简单、最稳定的运行方式。

```bash
# 1. 下载项目
git clone https://github.com/bwwq/Pixiv-XP-Pusher.git
cd Pixiv-XP-Pusher

# 2. 启动服务 (自动构建并运行)
chmod +x deploy.sh
./deploy.sh start

# 3. 查看日志
./deploy.sh logs
```

- **初次启动**：
  - 容器会自动启动 **Web UI** 和 **调度程序**
  - 访问 `http://VPS_IP:8000` 设置管理密码并完成初始化配置
  - 首次执行一次 "Run Once" 任务，然后进入定时调度模式
- **管理命令**：
  - `./deploy.sh stop` - 停止服务
  - `./deploy.sh once` - 手动触发一次推送
  - `./deploy.sh reset-xp` - 清空 XP 缓存（保留收藏数据）

### 方式二：本地直接运行 (Windows/Linux)

#### 1. 环境准备

确保已安装 Python 3.10+。

```bash
# 安装依赖
pip install -r requirements.txt
```

#### 2. 交互式配置 (小白推荐)

运行引导脚本，跟随提示完成 Token 获取、账号设置和推送配置。

```bash
# Windows
start.bat

# Linux/macOS
python launcher.py
```

#### 3. 手动运行

```bash
# 获取 Pixiv Token (需要浏览器交互)
python get_token.py

# 立即执行一次推送
python main.py --once

# 启动定时调度模式 (守护进程)
python main.py --now
```

---

## ⚙️ 配置文件 (config.yaml)

如果跳过引导脚本手动配置，请参考以下结构。

> **注意**：推荐使用 `launcher.py` 自动生成配置。

```yaml
pixiv:
  user_id: 12345678 # 你的 Pixiv 用户 ID（用于分析 XP）
  refresh_token: "..." # 必填，用于搜索/排行榜等操作

  # [New!] 可选：同步专用 Token (仅用于获取收藏和关注动态)
  # 使用独立 Token 可降低主号因搜索等操作被封禁的风险
  sync_token: "" # 留空则使用主 Token

profiler:
  ai:
    enabled: true
    provider: "openai" # 支持 openai 格式接口
    api_key: "sk-..."
    base_url: "https://api.openai.com/v1"
    model: "gpt-4o-mini"

    # [New!] 语义搜索配置
    embedding:
      model: "text-embedding-3-small"
      dimensions: 1536

  scan_limit: 1000 # 每次分析收藏的数量
  discovery_rate: 0.1 # 探索新 Tag 的概率
  
  # [Fork!] IP 标签降权配置
  ip_tags: []  # IP/版权标签列表，可自动从 Danbooru 同步
  ip_weight_discount: 1.0  # IP 标签权重折扣 (0.0-1.0)，1.0=不降权，0.1=降权90%
  
  # [Fork!] 手动标签加权
  boost_tags: {}  # {标签: 倍率}，如 {"white_hair": 1.5, "perfect_body": 2.0}
  
  # [Fork!] Danbooru API 凭证 (用于同步 IP 标签)
  danbooru_login: ""  # Danbooru 用户名
  danbooru_api_key: ""  # Danbooru API Key

fetcher:
  # MAB 策略配额限制 (防止某一策略独占)
  mab_limits:
    min_quota: 0.2
    max_quota: 0.6

  bookmark_threshold:
    search: 1000 # 搜索结果的最低收藏数要求
  subscribed_artists: [] # 额外关注的画师 ID 列表

filter:
  daily_limit: 20 # 每日推送上限
  exclude_ai: true # 过滤 AI 生成作品
  r18_mode: false # 是否允许 R18 (需 Token 权限)

scheduler:
  # 定时任务配置 (Cron 表达式: 分 时 日 月 周)
  # 支持多个时间点，用逗号分隔
  cron: "0 12 * * *, 0 21 * * *"
  # 每日维护任务 (发送日报 + 清理数据)
  daily_report_cron: "0 0 * * *"

notifier:
  # 启用的推送通道列表
  types: [telegram]

  telegram:
    bot_token: "123456:ABC..."
    # 你的 Telegram User ID (必须配置，否则无法使用指令)
    allowed_users:
      - "123456789"
    chat_ids: [123456789]

  onebot:
    ws_url: "ws://127.0.0.1:3001"
    private_id: 12345678

# 进阶配置 (可选)
web:
  password: "" # 留空表示首次访问时设置
  # 启动后访问 http://localhost:8000 查看管理面板
```

### 进阶配置项 [New!]

```yaml
notifier:
  multi_page_mode: "media_group" # cover_link | media_group
  max_pages: 10 # 多图模式最大页数 (1-10)

  telegram:
    image_quality: 85 # JPEG 压缩质量 (50-95)，越低越快
    max_image_size: 2000 # 最大边长 (px)，越小越快

    # Topic 智能分流 [New!]
    topic_rules:
      r18: 12345 # R18 作品自动发到此 Topic
      wallpaper: 67890 # "wallpaper" 标签发到此 Topic
    topic_tag_mapping:
      wallpaper: ["風景", "背景", "scenery"]
```

## 🎨 Web 管理后台 [New!]

启动服务后，访问 `http://localhost:8000` (默认端口) 即可进入管理后台。

- **Dashboard**: 查看 XP 画像词云、近期推送统计。
- **Gallery**: 浏览推送历史，提供无限滚动画廊。
  - **画廊代理**: 内置本地反代服务，**无需梯子**即可在画廊中浏览 Pixiv 图片（需配置 `proxy_url`）。
- **设置**: 首次访问需设置管理密码，之后凭密码登录。
- **Settings (Fork!)**: 图形化配置编辑器，包含：
  - IP 降权系数调节
  - Danbooru 凭证配置
  - IP 标签一键同步
  - 推荐策略开关
  - 定时任务配置
  - 标签调试器

---

## 📂 项目结构

```
pixiv-xp/
├── config.yaml          # 配置文件
├── main.py              # 程序主入口 (调度/执行)
├── launcher.py          # 交互式引导/管理菜单
├── start.bat            # Windows 启动脚本
├── deploy.sh            # Docker 管理脚本
├── get_token.py         # Pixiv Token 获取工具
├── requirements.txt     # Python 依赖
├── docker-compose.yml   # Docker 编排
├── pixiv_client.py      # Pixiv API 封装
├── profiler.py          # XP 画像分析核心 (包含 IP 降权/Boost 逻辑)
├── fetcher.py           # 内容搜索与抓取
├── filter.py            # 过滤与去重逻辑
├── database.py          # SQLite 数据存储
├── scripts/             # [Fork!] 工具脚本
│   └── sync_ip_tags.py  # Danbooru IP 标签同步工具
├── web/                 # Web 管理界面
│   ├── app.py           # FastAPI 后端 (包含设置页面 API)
│   ├── templates/       # HTML 模板
│   │   ├── settings.html # [Fork!] 图形化配置编辑器
│   │   └── ...
│   └── static/          # 静态资源
└── notifier/            # 推送适配器 (Telegram, OneBot)
```

---

## 📖 新手完整教程 (Step-by-Step)

> 从零开始，一步步教你跑起来！

### Step 1: 环境准备

**必需软件：**

- **Python 3.10+**: [下载地址](https://www.python.org/downloads/)
- **Git** (可选): 用于克隆项目
- **代理软件** (国内必需): v2rayN / Clash 等，用于连接 Telegram

**安装依赖：**

```bash
# 克隆项目（或直接下载 ZIP）
git clone https://github.com/bwwq/Pixiv-XP-Pusher.git
cd Pixiv-XP-Pusher

# 安装 Python 依赖
pip install -r requirements.txt
```

---

### Step 2: 获取 Pixiv Token

> ⚠️ **这是最关键的一步！** Token 是访问 Pixiv API 的钥匙。

```bash
python get_token.py
```

脚本会自动打开浏览器窗口，请登录您的 Pixiv 账号。登录成功后，Token 会自动保存到 `config.yaml`。

**无法打开浏览器？** （服务器环境）

1. 在本地电脑运行 `get_token.py` 获取 Token
2. 将获取到的 `refresh_token` 手动复制到服务器的 `config.yaml` 中

---

### Step 3: 配置 Telegram Bot

#### 3.1 创建 Bot

1. 在 Telegram 中搜索 `@BotFather`
2. 发送 `/newbot` 创建机器人
3. 按提示设置名称，获得 `bot_token`（形如 `123456789:ABCdefGHIjklMNO...`）

#### 3.2 获取 Chat ID

1. 将你的 Bot 拉入目标群组（或直接私聊）
2. 访问 `https://api.telegram.org/bot<你的token>/getUpdates`
3. 发送任意消息给 Bot，刷新页面，找到 `"chat": {"id": -100xxxxx}` 就是 Chat ID

#### 3.3 获取 User ID

1. 在 Telegram 中搜索 `@userinfobot`
2. 发送 `/start`，它会回复你的 User ID

#### 3.4 填写配置

打开 `config.yaml`，填入获取到的信息：

```yaml
notifier:
  types: [telegram]

  telegram:
    bot_token: "123456789:ABCdefGHIjklMNO..." # BotFather 给你的
    chat_ids:
      - "-1001234567890" # 你的群组 ID（或个人 Chat ID）
    allowed_users:
      - "987654321" # 你的 User ID（用于权限控制）
    proxy_url: "http://127.0.0.1:7890" # 代理地址（国内必填！）
```

> **🔴 国内用户必看：** `proxy_url` 必须填写你的代理软件地址，否则 Bot 无法连接 Telegram！

#### 3.5 (可选) 配置 OneBot (QQ)

如果您使用 QQ 而非 Telegram，请配置 OneBot：

1. 安装 [Lagrange.OneBot](https://github.com/LagrangeDev/Lagrange.Core) 或 [Go-CQHTTP](https://github.com/Mrs4s/go-cqhttp)
2. 启动后获取 WebSocket 地址（默认 `ws://127.0.0.1:3001`）
3. 填写配置：

```yaml
notifier:
  types: [onebot]

  onebot:
    ws_url: "ws://127.0.0.1:3001"
    private_id: 12345678 # 你的 QQ 号（推送目标）
    master_id: 12345678 # 主人 QQ（只有主人能使用指令）
    push_to_private: true
```

OneBot 支持与 Telegram 相同的指令：`/push`, `/xp`, `/stats`, `/block`, `/unblock`, `/schedule`, `/help`

#### 3.6 (可选) 配置 AstrBot [实验性]

> ⚠️ **实验性功能**：AstrBot 渠道目前为实验性支持，API 接口可能随 AstrBot 版本更新而变化。

如果您使用 [AstrBot](https://github.com/Soulter/AstrBot) 多平台机器人框架：

**前置条件：**

1. 已安装并运行 AstrBot
2. 在 AstrBot 管理面板安装 `astrbot_plugin_http_adapter` 插件
3. 获取目标会话的 `unified_msg_origin`（从 AstrBot 日志或管理面板获取）

```yaml
notifier:
  types: [astrbot]

  astrbot:
    http_url: "http://127.0.0.1:6185" # HTTP API 地址
    unified_msg_origin: "QQOfficial:group:123456" # 目标会话标识
    api_key: "" # API 密钥（如需认证）
```

> **提示：** `unified_msg_origin` 格式通常为 `平台:类型:ID`，如 `QQOfficial:group:123456` 或 `Telegram:private:789`

---

### Step 4: 配置 AI 清洗 (可选但推荐)

AI 功能可以智能合并同义词（如"白发"="White Hair"），提升画像精准度。

```yaml
profiler:
  ai:
    enabled: true
    provider: "openai"
    api_key: "sk-..." # 你的 OpenAI API Key
    base_url: "https://api.openai.com/v1" # 或中转地址
    model: "gpt-4o-mini" # 推荐，便宜又好用
```

**没有 API？** 设置 `enabled: false`，系统会用纯统计模式运行。

---

### Step 5: 启动程序

```bash
# 方式A: 立即执行一次推送（测试用）
python main.py --once

# 方式B: 启动定时任务 + 立即执行一次
python main.py --now

# 方式C: 仅启动定时任务（后台守护）
python main.py

# 方式D: 同时启动 Web 管理 + 推送服务
# Windows: 运行 start.bat 选择选项 5
# Linux/macOS:
python -m uvicorn web.app:app --host 0.0.0.0 --port 8000 > web.log 2>&1 &
python main.py --now
```

**Windows 用户：** 直接双击 `start.bat` 启动交互菜单，选择选项 **5** 同时启动 Web 和推送服务。

---

### Step 6: 使用 Bot 指令

Bot 启动后，在 Telegram 聊天框输入 `/` 可看到所有指令：

| 指令             | 功能                            |
| ---------------- | ------------------------------- |
| `/push`          | 🚀 立即触发一次推送             |
| `/push <ID>`     | 📌 手动推送指定作品             |
| `/xp`            | 🎯 查看你的 XP 画像（Top 标签） |
| `/stats`         | 📈 查看各策略的成功率           |
| `/schedule`      | ⏰ 查看/修改定时时间            |
| `/block <tag>`   | 🚫 屏蔽讨厌的标签               |
| `/unblock <tag>` | ✅ 取消屏蔽标签                 |
| `/help`          | ℹ️ 显示帮助                     |

**修改定时时间示例：**

```
/schedule 9:30,21:00   # 每天 9:30 和 21:00 推送
```

---

### Step 7: 日常维护

- **重置 XP 画像：** `python main.py --reset-xp`
- **查看日志：** 日志保存在 `logs/` 目录
- **更新项目：** `git pull && pip install -r requirements.txt`

---

## 🙋 常见问题

<details>
<summary><b>Q: telegram.error.NetworkError / httpx.ConnectError</b></summary>

**原因：** 无法连接 Telegram 服务器（国内被墙）

**解决：** 在 `config.yaml` 中配置代理：

```yaml
telegram:
  proxy_url: "http://127.0.0.1:7890" # 改成你的代理地址
```

</details>

<details>
<summary><b>Q: 点击 喜欢/不喜欢 按钮提示"无权限"</b></summary>

**原因：** 你的 User ID 没有加入 `allowed_users` 列表

**解决：**

1. 通过 `@userinfobot` 获取你的 User ID
2. 将 ID 添加到 `config.yaml` 的 `allowed_users` 列表中
</details>

<details>
<summary><b>Q: 如何获取 Pixiv Refresh Token?</b></summary>
运行 <code>python get_token.py</code>，脚本会启动一个 Selenium 浏览器窗口引导登录，登录成功后会自动捕获并保存 Token。
</details>

<details>
<summary><b>Q: 部署在服务器，无法打开浏览器获取 Token 怎么办?</b></summary>
请在本地电脑运行 <code>python get_token.py</code> 获取 Token 后，将其复制到服务器的 <code>config.yaml</code> 中。
</details>

<details>
<summary><b>Q: XP 画像是如何生成的?</b></summary>
系统会拉取你的 Pixiv 收藏夹（公开+私密），统计所有作品的标签频率。结合 AI (如果启用) 清洗同义词（如 "白发" = "White Hair"），计算出这一标签的权重。推送时会根据这些权重组合搜索关键词。
</details>

<details>
<summary><b>Q: 为什么 AI 模式报错 401/502?</b></summary>
请检查 `config.yaml` 中 LLM 的 `api_key` 和 `base_url` 是否正确。如果 API 不稳定，可以在配置中关闭 AI 功能 (`enabled: false`)，系统将回退到纯统计模式运行。
</details>

<details>
<summary><b>Q: [Fork] 如何配置 IP 标签降权?</b></summary>

1. **自动同步** (推荐):
   ```bash
   # 设置 Danbooru 环境变量
   export DANBOORU_LOGIN=your_username
   export DANBOORU_API_KEY=your_api_key
   
   # 运行同步脚本
   python scripts/sync_ip_tags.py
   ```

2. **手动配置**:
   ```yaml
   profiler:
     ip_tags: ["blue_archive", "honkai_star_rail", "nikke"]
     ip_weight_discount: 0.1  # 降权 90%
   ```

3. **Web UI 配置**:
   访问 `http://localhost:8000/settings`，在 "IP 降权" 部分设置折扣系数并同步 IP 列表。
</details>

<details>
<summary><b>Q: [Fork] 如何手动提升某个标签的权重?</b></summary>

1. **查找标签标准名称**:
   访问 `http://localhost:8000/settings`，在 "XP 字典查询" 中输入关键词，找到系统使用的标准名称。

2. **配置 boost_tags**:
   ```yaml
   profiler:
     boost_tags:
       white_hair: 1.5      # 提升 50%
       perfect_body: 2.0    # 提升 100% (双倍)
   ```

3. **Web UI 配置**:
   未来版本将在 Web 设置页面添加 Boost 标签配置界面。
</details>

<details>
<summary><b>Q: [Fork] Web 设置页面打不开怎么办?</b></summary>

1. **检查服务是否运行**:
   ```bash
   # 查看进程
   ps aux | grep uvicorn
   
   # 检查端口
   netstat -tlnp | grep 8000
   ```

2. **检查 config.yaml**:
   ```yaml
   web:
     enabled: true
     password: ""  # 留空以显示设置页面
     port: 8000
   ```

3. **查看日志**:
   ```bash
   # 查看 Web 服务日志
   tail -f logs/web.log
   ```

4. **常见问题**:
   - 端口被占用: 修改 `web.port`
   - 密码已设置: 删除 `web.password` 或设为空字符串
   - 路由问题: 确保已拉取最新代码并重启服务
</details>

<details>
<summary><b>Q: [Fork] 图片代理报错 "Cannot connect to host none:80"</b></summary>

**原因**: 代理 URL 被设置为 `None` 或字符串 `"None"`。

**解决**:

1. **检查配置**:
   ```yaml
   notifier:
     telegram:
       proxy_url: "http://127.0.0.1:7890"  # 正确的代理地址
       # 或留空/删除此行以禁用代理
   ```

2. **Web UI 修复**:
   访问 `http://localhost:8000/settings`，在 "基础设置" 中:
   - 设置正确的代理 URL
   - 或留空并保存以清除代理设置

3. **手动修复**:
   ```bash
   # 编辑 config.yaml，确保 proxy_url 是有效 URL 或完全删除
   sed -i '/proxy_url:/d' config.yaml  # 删除 proxy_url 行
   ```
</details>

<details>
<summary><b>Q: 如何同时运行 Web 管理界面和后台推送服务？</b></summary>

**方法 1: 使用交互菜单 (推荐)**
```bash
# 运行启动脚本
start.bat  # Windows
# 或
python launcher.py  # Linux/macOS

# 选择选项 5: "同时启动 Web + 推送服务"
```

**方法 2: 手动启动两个进程**
```bash
# 终端 1: 启动 Web 服务器
python -m uvicorn web.app:app --host 0.0.0.0 --port 8000

# 终端 2: 启动推送服务
python main.py --now
```

**方法 3: Linux/macOS 后台运行**
```bash
# 后台启动 Web 服务器
python -m uvicorn web.app:app --host 0.0.0.0 --port 8000 > web.log 2>&1 &

# 前台启动推送服务
python main.py --now
```

**方法 4: Docker 部署 (最稳定)**
```bash
# 一键启动所有服务
docker-compose up -d
```
</details>

---

## 📜 许可证

MIT License

---

## 📝 更新日志 / Changelog

### 2026-02-19 (beckyeeky Fork)

**✨ 新增功能 / New Features**

- **IP 标签降权系统**: 自动识别并降低 IP/游戏/动画标题标签权重，专注视觉元素标签
- **手动标签加权**: 支持手动提升特定标签的推荐权重 (`boost_tags`)
- **Web 图形化配置编辑器**: 完全替代手动编辑 `config.yaml`，提供直观的 Web 界面
- **Danbooru IP 同步**: 自动从 Danbooru 同步最新版权标签列表
- **标签调试器**: Web UI 内查询标签标准名称
- **代理 URL 智能处理**: 修复 `http://None:80` 连接错误

**🔧 配置更新 / Configuration Updates**

```yaml
profiler:
  # IP 标签降权
  ip_tags: []  # 自动同步或手动指定
  ip_weight_discount: 1.0  # 降权系数 (0.0-1.0)
  
  # 手动标签加权
  boost_tags: {}  # {标签: 倍率}
  
  # Danbooru 凭证
  danbooru_login: ""
  danbooru_api_key: ""
```

**🐛 修复 / Bug Fixes**

- **Web UI 路由修复**: 修复重复导入导致的 `/` 和 `/setup` 404 错误
- **图片代理修复**: 正确处理 `None`/空字符串代理 URL
- **配置传递修复**: 修复 `ip_tags` 参数传递错误

**🚀 使用指南 / Usage Guide**

1. **访问 Web 设置页面**: `http://localhost:8000/settings`
2. **配置 IP 降权**: 设置折扣系数并同步 IP 列表
3. **使用标签调试器**: 查询标签标准名称
4. **配置手动加权**: 编辑 `config.yaml` 中的 `boost_tags`

### 2026-01-26 (原项目)

**🐛 修复 / Bug Fixes**

- **Docker 部署修复 / Docker Deployment Fix**
  - 🇨🇳 修复 Docker 容器只启动主程序，未启动 Web UI 的问题。现在容器启动后会同时运行 Web UI (`uvicorn`) 和调度程序 (`main.py --now`)
  - 🇺🇸 Fixed Docker container only starting the main program without the Web UI. Now the container runs both Web UI (`uvicorn`) and scheduler (`main.py --now`) simultaneously
  - 🇨🇳 修复 `config.yaml` 挂载为只读导致首次设置密码失败的问题。已移除 `:ro` 只读限制
  - 🇺🇸 Fixed `config.yaml` being mounted as read-only, causing password setup to fail. Removed `:ro` read-only restriction
  - 🇨🇳 添加 `8000:8000` 端口映射，使 Web UI 可从外部访问
  - 🇺🇸 Added `8000:8000` port mapping to make Web UI accessible externally

**📖 文档更新 / Documentation**

- 🇨🇳 更新 Docker 部署说明，明确首次启动需访问 `http://VPS_IP:8000` 设置密码
- 🇺🇸 Updated Docker deployment instructions, clarifying that users need to visit `http://VPS_IP:8000` to set password on first launch

**🔄 升级指南 / Upgrade Guide**

```bash
# 拉取更新 / Pull updates
git pull

# 重新构建并启动 / Rebuild and start
docker-compose up -d --build

# 访问 Web UI / Access Web UI
# http://VPS_IP:8000
```

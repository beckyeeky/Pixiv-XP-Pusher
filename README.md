# Pixiv-XP-Pusher (Enhanced Fork)

> 🎨 **全自动智能 XP 捕获与多渠道推送系统**
>
> 基于用户收藏自动分析 XP（性癖/偏好）画像，全网搜索并智能推送最懂你的 Pixiv 插画。支持 Telegram / QQ (OneBot) 推送。
>
> **本 Fork 版本针对内存占用、高并发稳定性及用户交互体验 (UX) 进行了深度改造，极其适合在资源受限的 VPS 上进行长期稳定的守护进程部署。**

---

## 📑 导航
- [🌟 本 Fork 版本核心演进 (相比原版)](#-本-fork-版本核心演进-相比原版)
- [✨ 基础功能特性](#-基础功能特性)
- [🚀 部署指南 (最佳实践)](#-部署指南-最佳实践)
- [⚙️ 配置文件核心说明](#-配置文件核心说明)
- [💬 常见问题 (FAQ)](#-常见问题-faq)

---

## 🌟 本 Fork 版本核心演进 (相比原版)

> 🙏 **致敬原项目作者**：本 Fork 完整保留并沿用原项目在"XP 画像计算""混合发现策略"等方面的优秀设计，以下所有改进均建立在对原作的尊重与延展之上。
>
> 💡 **重要说明**：本 Fork 的全部增强、重构与优化，均通过 **"Vibe Coding"** 完成--即开发者与其 AI 助手通过自然语言结对编程驱动的项目演进。

在此基础上，本分支聚焦稳定性、资源效率与 UX 体验，进行了以下专业化演进：

### 1. 🧱 架构与并发稳定性提升
- **任务编排与并发修复**：重构了 `main_task` 的执行链路，加入了更明确的阶段划分与并发约束，避免高峰时的资源争抢与死锁风险。
- **图集下载并发控制**：为多图集下载/发送引入了全局信号量（Semaphore），确保批量请求在高并发下依旧可控、稳定，避免触发外部 API 的风控。

### 2. 🧠 内存与资源优化
- **Ugoira (动图) 跳过机制**：新增 `skip_ugoira: true` 配置，可在过滤阶段主动跳过极其消耗内存的动图 ZIP 下载与转码流程，将常驻内存峰值显著压制在数百 MB 以内。
- **资源占用均衡化**：在关键环节优化了对象生命周期与请求负载，提升了长期作为守护进程运行时的韧性。

### 3. 🖥️ Web UI 体验升级与精细化配置管控
- **独立的 Tags 管理页**：将标签管理独立为专用页面，并提供字典级的实时搜索。交互上支持标签语义展示（中/日文字典映射）。
- **细粒度的三态权重调控**：提供清晰、直观的三态操作——
  - 🟢 **Boost (加权)**：为极度偏好的标签提升权重（如 1.5x）。
  - 🟠 **Downweight (降权)**：引入轻量级的 0.1x 降权机制，温和抑制容易"霸榜"的大型版权/游戏名标签，让算法把注意力回归到核心"视觉元素"。
  - 🔴 **Block (屏蔽)**：将不喜欢的标签纳入黑名单，并增加防误触的二次确认。
- **配置导入/导出**：新增独立的导入/导出页面，支持带注释的 YAML 格式配置备份与恢复，方便迁移与版本管理。
- **增强型设置面板**：全面重构设置页面，支持全量参数的可视化配置——
  - **搜索策略参数**：搜索深度、时间范围、各策略收藏数阈值
  - **过滤规则**：每日上限、单画师上限、AI 过滤、动图跳过
  - **推送设置**：批量模式、图片质量、尺寸限制
  - **网络调优**：并发数、每分钟请求速率限制
- **Danbooru 词库同步**：支持从 Danbooru 同步最新版权标签，并兼容外部别名映射字典 (`ip_tag_aliases.json`)，优雅处理复杂的日文简写。
- **会话持久化**：Web 登录会话有效期延长至 30 天，减少频繁登录烦恼。

### 4. 🖼️ Web 画廊与网络链路优化
- **Gallery 直连优先**：优化了 Web 画廊 (`Gallery`) 的图片渲染逻辑，浏览器优先直接拉取 Pixiv 外网反代图床（如 `https://pixiv.cat/...`），大幅降低了 VPS 本身的带宽与内存压力。
- **自动回退兜底保障**：当客户端网络受限触发 `onerror` 时，自动平滑回退到 VPS 本地的代理接口 (`/api/proxy/image/`)，兼顾了国内直连和访问稳定性。

### 5. 🤖 强化的 AI 过滤与模型适配
- **多语言标签检测拦截**：扩充了 AI 画作的拦截策略，通过引入中、日、英多语言的关键词检测，进一步净化推荐信息流。
- **深度适配 DeepSeek**：全面推荐并适配了高性价比的 DeepSeek API 进行高效标签清洗，其对二次元语境的优秀理解力及宽松的风控策略使其成为最佳选择。

### 6. 💬 Telegram Bot 交互式体验
- **交互式菜单 (`/menu`)**：控制面板式操作，无需记忆复杂命令。
- **交互式推送向导 (`/push`)**：选择今日精选、画师作品集或指定作品 ID 推送。
- **交互式搜索 (`/search`)**：引导式时间范围与关键词选择，支持标签翻译智能关联。
- **设置面板 (`/settings`)**：AI 过滤、R18 模式、每日上限、推送时间可视化配置。
- **Streaming UX**：所有交互式命令完成后自动删除引导消息与用户输入，保持聊天整洁。

### 7. 🔒 R18 内容智能遮罩
- **Spoiler Mask**：自动检测标题、标签、画师名中的 R-18/R-18G/🔞 关键词，推送时自动添加 Telegram 原生模糊遮罩。

### 8. 📚 标签翻译与智能关联
- **自动保存翻译**：收集 Pixiv 标签的中日翻译并持久化存储。
- **搜索扩展**：搜索关键词时自动关联原始标签，提升召回率。

### 9. ⚡ 推送队列与限流
- **异步队列**：Telegram 通知使用 `asyncio.Queue` 队列化处理，避免阻塞。
- **深度限制**：队列最多保留 30 个触发，防止积压。
- **指数退避**：HTTP 429 限流时自动退避重试。

---

## ✨ 基础功能特性

- 🤖 **XP 画像构建**: 提取历史收藏计算 TF-IDF 权重，AI 归一化同义词（如将 `白发`、`silver hair` 统为 `white_hair`）。
- 🔍 **混合发现策略 (MAB)**:
  - XP 匹配搜索
  - 订阅画师追踪
  - 排行榜筛选
  - 盲盒探索 (从落选池中随机捞取潜力股，比例可调)
  - 关联连锁推荐 (点赞好图自动追溯相似作品)
- 📱 **多渠道覆盖**: 支持 Telegram (MediaGroup 图集) 和 OneBot 协议 (QQ)。

---

### 🤖 Telegram Bot 命令列表

| 命令 | 说明 |
| :--- | :--- |
| `/start`, `/menu` | 打开交互式控制面板 |
| `/push` | 推送菜单（今日精选/画师作品集/指定ID）|
| `/search` | 交互式搜索向导（时间范围+关键词）|
| `/settings` | 设置菜单（AI过滤/R18模式/每日上限/推送时间）|
| `/block` | 标签屏蔽管理（交互式/直接参数）|
| `/unblock` | 解除标签屏蔽 |
| `/mute` | 临时静音标签24小时 |
| `/unmute` | 解除标签静音 |
| `/block_artist` | 画师屏蔽管理 |
| `/unblock_artist` | 解除画师屏蔽 |
| `/batch` | 批量模式设置（Telegraph开关）|
| `/xp` | 查看 XP 画像（Top Tags）|
| `/stats` | 查看策略统计（MAB成功率）|
| `/schedule` | 查看/修改推送时间 |
| `/restart` | 通过 systemctl 重启服务 |
| `/help` | 显示帮助信息 |

---

## 🚀 部署指南 (最佳实践)

为了达到真正的**"无感常驻运行"**，强烈建议在 Linux 环境下，将「自动推送脚本」与「Web 控制台」解耦，交由 **Systemd** 进行双进程守护。

### 1. 基础准备
```bash
# 1. 克隆项目到常规应用目录 (推荐 /opt/)
sudo git clone https://github.com/beckyeeky/Pixiv-XP-Pusher.git /opt/Pixiv-XP-Pusher
cd /opt/Pixiv-XP-Pusher

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. 运行交互脚本获取 Pixiv Refresh Token
python get_token.py
```

### 2. Systemd 双进程守护
分别创建两个服务文件：

**推送守护进程:** `sudo nano /etc/systemd/system/pixiv-pusher.service`
```ini
[Unit]
Description=Pixiv XP Pusher Daemon
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/Pixiv-XP-Pusher
# 启动前清理残留锁文件，防止重启后启动失败
ExecStartPre=/bin/rm -f /tmp/pixiv_xp_pusher.lock
ExecStart=/usr/bin/python3 main.py --now
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Web 控制台进程:** `sudo nano /etc/systemd/system/pixiv-web.service`
```ini
[Unit]
Description=Pixiv XP Pusher Web UI
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/Pixiv-XP-Pusher
# 强绑定本地 127.0.0.1 防爆破，外部通过 SSH 隧道安全访问
ExecStart=/usr/local/bin/uvicorn web.app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**激活并启动:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pixiv-pusher
sudo systemctl enable --now pixiv-web
```

### 3. 本地安全连入控制台
由于 Web 服务绑定在 `127.0.0.1`，我们需要利用 SSH 隧道建立安全连线：
- **命令行方案**: `ssh -L 8000:127.0.0.1:8000 root@你的VPS_IP`
- **PuTTY 方案**: 在 `Connection` -> `SSH` -> `Tunnels` 中，Source port 填 `8000`，Destination 填 `127.0.0.1:8000`，点击 Add。

连上 SSH 后，打开本地浏览器访问：**`http://127.0.0.1:8000`** 即可管理你的所有标签与配置。

---

## 🐳 Docker Compose 部署 (推荐)

如果你更习惯容器化部署，本项目也提供了开箱即用的 Docker 支持。这种方式同样实现了"双进程解耦"，且环境隔离更干净。

### 1. 准备配置
```bash
# 克隆项目
git clone https://github.com/beckyeeky/Pixiv-XP-Pusher.git
cd Pixiv-XP-Pusher

# 创建配置文件 (填入你的 Token 和配置)
cp config.example.yaml config.yaml
nano config.yaml
```

### 2. 启动服务
```bash
# 构建并后台启动
docker-compose up -d

# 查看日志
docker-compose logs -f
```

### 3. 访问 Web 控制台
Web UI 默认运行在容器的 `8000` 端口。
- 访问地址: `http://你的VPS_IP:8000`
- 注意：请确保你的 VPS 防火墙放行了 8000 端口。

---

## ⚙️ 配置文件核心说明

编辑项目根目录的 `config.yaml`（如果是初次运行，可复制 `config.example.yaml`）：

```yaml
pixiv:
  user_id: 12345678      # 你的 Pixiv 用户 ID
  refresh_token: "..."   # 获取到的 Token

filter:
  daily_limit: 200       # 每次运行时最多推多少张图
  max_per_artist: 3      # 单画师每次推送上限，防止单一画师霸屏
  exclude_ai: true       # 启用增强型多语言 AI 画作拦截
  skip_ugoira: true      # [极度推荐] 丢弃动图以节省服务器内存
  r18_mode: mixed        # 支持 r18_only (纯车), safe (净网), mixed (混合)

fetcher:
  search_limit: 200              # 搜索策略最大结果数
  date_range_days: 60            # 搜索时间范围（天）
  bookmark_threshold:            # 各策略收藏数阈值过滤
    search: 500                  # 搜索策略：只推送收藏数 ≥500 的作品
    subscription: 0              # 订阅策略：0 表示不限制
    related: 100                 # 关联推荐：只推送收藏数 ≥100 的作品

network:
  max_concurrency: 5             # 并发下载数
  requests_per_minute: 60        # Pixiv API 速率限制

notifier:
  telegram:
    bot_token: "..."
    chat_ids: [你的TG_ID]
    proxy_url: "http://127.0.0.1:7890"  # [必填] 国内 VPS 无法直连，需配置本机代理端口
    batch_mode: "album"                 # 推送模式: album (相册) / telegraph (长图)
    image_quality: 85                   # 图片压缩质量 (1-100)
    max_image_size: 2000                # 图片最大边长 (px)

scheduler:
  cron: "0 12 * * *"     # 默认每天中午 12 点推送，支持多时间点如 "0 12 * * *, 0 21 * * *"
```

---

## 📋 日志查看

> ⚠️ 日志文件在**首次成功运行后**才会生成（`logs/` 目录由 app 自动创建，相对 WorkingDirectory）。
> systemd 部署时 WorkingDirectory=`/opt/Pixiv-XP-Pusher`，所以日志在 `/opt/Pixiv-XP-Pusher/logs/`。

### 主日志（推荐）
```bash
# 先确认日志目录存在
ls /opt/Pixiv-XP-Pusher/logs/

# 实时跟踪
tail -f /opt/Pixiv-XP-Pusher/logs/pixiv_xp.log

# 最近 50 行
tail -50 /opt/Pixiv-XP-Pusher/logs/pixiv_xp.log

# 搜索关键词
grep "ERROR\|WARNING\|推送" /opt/Pixiv-XP-Pusher/logs/pixiv_xp.log | tail -20

# 查看 Telegram 冲突记录
grep -i "Conflict\|getUpdates" /opt/Pixiv-XP-Pusher/logs/pixiv_xp.log

# 统计推送数量
grep "推送完成" /opt/Pixiv-XP-Pusher/logs/pixiv_xp.log | wc -l
```

### Systemd 服务日志
```bash
# 实时跟踪推送日志
journalctl -u pixiv-pusher -f

# 最近 100 行
journalctl -u pixiv-pusher -n 100

# 今天的日志
journalctl -u pixiv-pusher --since today

# 指定时间范围
journalctl -u pixiv-pusher --since "2026-03-19 23:00" --until "2026-03-20 02:00"
```

### Web 服务日志
```bash
journalctl -u pixiv-web -n 50
```

### 日志清理
主日志自动轮转（最大 5MB，保留 3 份），无需手动清理。

手动清理旧日志：
```bash
# 删除旧版 service.log（如从 launcher.py 遗留）
rm -f /opt/Pixiv-XP-Pusher/service.log

# 删除旧的调试日志
rm -f /opt/Pixiv-XP-Pusher/logs/debug_push_*.log
```

---

## 💬 常见问题 (FAQ)

**Q: 启动后终端报错 `NetworkError` 或 `ConnectError`？**
A: 如果你的 VPS 在国内，是连不上 Telegram 和 Pixiv 的。请确保在 `config.yaml` 的 `telegram.proxy_url` 中正确配置了科学代理地址（如 `http://127.0.0.1:7890`）。

**Q: 点击 TG 上的"喜欢"按钮，提示无权限？**
A: 请通过 `@userinfobot` 获取你的 Telegram User ID，并确保它填在了 `config.yaml` 的 `allowed_users` 列表里。

**Q: 为什么日志里提示 AI 洗标签一直失败，或者返回 400 Bad Request？**
A: 检查 `profiler.ai.api_key` 和 `base_url` 是否有效。强烈推荐使用 **DeepSeek**（`deepseek-chat`）模型，不仅性价比极高，且不会因为处理合法二次元标签而触发严格的道德风控审查。

**Q: systemd 服务反复重启或启动失败？**
A: 检查是否有手动启动的进程占用锁文件，执行 `pkill -f "main.py"` 后重启服务。服务配置已包含 `ExecStartPre=/bin/rm -f /tmp/pixiv_xp_pusher.lock` 自动清理锁文件。

**Q: R18 内容如何自动遮罩？**
A: 系统会自动检测标题、标签、画师名中的 R-18/R-18G/🔞 关键词，推送时自动添加 Telegram 原生 Spoiler 模糊遮罩。用户需点击才能查看原图。

**Q: 如何调整各策略的收藏数阈值？**
A: 在 Web UI 的「设置」页面，可以为搜索、订阅、关联推荐分别设置收藏数阈值。例如将搜索策略设为 500，可有效过滤低质量作品，只推送高人气内容。

**Q: 如何备份和迁移配置？**
A: 使用 Web UI 的「导入/导出」页面，可将当前完整配置（含注释）导出为 YAML 文件备份。迁移时只需在新环境导入该文件即可还原所有设置。
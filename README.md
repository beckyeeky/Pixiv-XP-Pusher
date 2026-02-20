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

> 🙏 **致敬原项目作者**：本 Fork 完整保留并沿用原项目在“XP 画像计算”“混合发现策略”等方面的优秀设计，以下所有改进均建立在对原作的尊重与延展之上。
>
> 💡 **重要说明**：本 Fork 的全部增强、重构与优化，均通过 **“Vibe Coding”** 完成——即开发者与其 AI 助手通过自然语言结对编程驱动的项目演进。

在此基础上，本分支聚焦稳定性、资源效率与 UX 体验，进行了以下专业化演进：

### 1. 🧱 架构与并发稳定性提升
- **任务编排与并发修复**：重构了 `main_task` 的执行链路，加入了更明确的阶段划分与并发约束，避免高峰时的资源争抢与死锁风险。
- **图集下载并发控制**：为多图集下载/发送引入了全局信号量（Semaphore），确保批量请求在高并发下依旧可控、稳定，避免触发外部 API 的风控。

### 2. 🧠 内存与资源优化
- **Ugoira (动图) 跳过机制**：新增 `skip_ugoira: true` 配置，可在过滤阶段主动跳过极其消耗内存的动图 ZIP 下载与转码流程，将常驻内存峰值显著压制在数百 MB 以内。
- **资源占用均衡化**：在关键环节优化了对象生命周期与请求负载，提升了长期作为守护进程运行时的韧性。

### 3. 🖥️ Web UI 体验升级与精细化标签管控
- **独立的 Tags 管理页**：将标签管理独立为专用页面，并提供字典级的实时搜索。交互上支持标签语义展示（中/日文字典映射）。
- **细粒度的三态权重调控**：提供清晰、直观的三态操作——
  - 🟢 **Boost (加权)**：为极度偏好的标签提升权重（如 1.5x）。
  - 🟠 **Downweight (降权)**：引入轻量级的 0.1x 降权机制，温和抑制容易“霸榜”的大型版权/游戏名标签，让算法把注意力回归到核心“视觉元素”。
  - 🔴 **Block (屏蔽)**：将不喜欢的标签纳入黑名单，并增加防误触的二次确认。
- **Danbooru 词库同步**：支持从 Danbooru 同步最新版权标签，并兼容外部别名映射字典 (`ip_tag_aliases.json`)，优雅处理复杂的日文简写。

### 4. 🖼️ Web 画廊与网络链路优化
- **Gallery 直连优先**：优化了 Web 画廊 (`Gallery`) 的图片渲染逻辑，浏览器优先直接拉取 Pixiv 外网反代图床（如 `https://pixiv.cat/...`），大幅降低了 VPS 本身的带宽与内存压力。
- **自动回退兜底保障**：当客户端网络受限触发 `onerror` 时，自动平滑回退到 VPS 本地的代理接口 (`/api/proxy/image/`)，兼顾了国内直连和访问稳定性。

### 5. 🤖 强化的 AI 过滤与模型适配
- **多语言标签检测拦截**：扩充了 AI 画作的拦截策略，通过引入中、日、英多语言的关键词检测，进一步净化推荐信息流。
- **深度适配 DeepSeek**：全面推荐并适配了高性价比的 DeepSeek API 进行高效标签清洗，其对二次元语境的优秀理解力及宽松的风控策略使其成为最佳选择。

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

## 🚀 部署指南 (最佳实践)

为了达到真正的**“无感常驻运行”**，强烈建议在 Linux 环境下，将「自动推送脚本」与「Web 控制台」解耦，交由 **Systemd** 进行双进程守护。

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

## ⚙️ 配置文件核心说明

编辑项目根目录的 `config.yaml`（如果是初次运行，可复制 `config.example.yaml`）：

```yaml
pixiv:
  user_id: 12345678      # 你的 Pixiv 用户 ID
  refresh_token: "..."   # 获取到的 Token

filter:
  daily_limit: 200       # 每次运行时最多推多少张图
  exclude_ai: true       # 启用增强型多语言 AI 画作拦截
  skip_ugoira: true      # [极度推荐] 丢弃动图以节省服务器内存
  r18_mode: mixed        # 支持 r18_only (纯车), safe (净网), mixed (混合)

notifier:
  telegram:
    bot_token: "..."
    chat_ids: [你的TG_ID]
    proxy_url: "http://127.0.0.1:7890"  # [必填] 国内 VPS 无法直连，需配置本机代理端口

scheduler:
  cron: "0 12 * * *"     # 默认每天中午 12 点推送，支持多时间点如 "0 12 * * *, 0 21 * * *"
```

---

## 💬 常见问题 (FAQ)

**Q: 启动后终端报错 `NetworkError` 或 `ConnectError`？**
A: 如果你的 VPS 在国内，是连不上 Telegram 和 Pixiv 的。请确保在 `config.yaml` 的 `telegram.proxy_url` 中正确配置了科学代理地址（如 `http://127.0.0.1:7890`）。

**Q: 点击 TG 上的“喜欢”按钮，提示无权限？**
A: 请通过 `@userinfobot` 获取你的 Telegram User ID，并确保它填在了 `config.yaml` 的 `allowed_users` 列表里。

**Q: 为什么日志里提示 AI 洗标签一直失败，或者返回 400 Bad Request？**
A: 检查 `profiler.ai.api_key` 和 `base_url` 是否有效。强烈推荐使用 **DeepSeek**（`deepseek-chat`）模型，不仅性价比极高，且不会因为处理合法二次元标签而触发严格的道德风控审查。
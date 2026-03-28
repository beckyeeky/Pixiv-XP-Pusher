# 运维与诊断

## 更新部署（推荐流程）

```bash
git pull
# 然后重启所有相关服务（如 pusher/web）
```

## Docker Compose

```bash
docker-compose up -d
docker-compose logs -f pusher
docker-compose logs -f web
```

更新后建议：

```bash
docker-compose up -d --force-recreate
```

## 守护进程（systemd）配置

如使用 systemd 管理，可拆分为两个服务（推送主任务 + Web）：

`/etc/systemd/system/pixiv-xp-pusher.service`

```ini
[Unit]
Description=Pixiv XP Pusher Service
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/Pixiv-XP-Pusher
ExecStart=/path/to/Pixiv-XP-Pusher/.venv/bin/python main.py --now
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/pixiv-xp-web.service`

```ini
[Unit]
Description=Pixiv XP Pusher Web Console
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/Pixiv-XP-Pusher
ExecStart=/path/to/Pixiv-XP-Pusher/.venv/bin/python -m uvicorn web.app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

常用命令：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pixiv-xp-pusher pixiv-xp-web
sudo systemctl restart pixiv-xp-pusher pixiv-xp-web
sudo systemctl status pixiv-xp-pusher pixiv-xp-web
```

## 数据库维护

```bash
python scripts/db_maintenance.py overview
python scripts/db_maintenance.py backup --output ./backup/pixiv_xp.db
python scripts/db_maintenance.py cleanup --days 180
```

## 推荐效果评估

```bash
python scripts/evaluate_recommendation.py
python scripts/evaluate_recommendation.py --json
```

## Danbooru IP 标签同步

```bash
export DANBOORU_LOGIN=your_login
export DANBOORU_API_KEY=your_api_key
python scripts/sync_ip_tags.py
```

## 其他诊断脚本
- `diagnose_queue.py`
- `debug_push.py`
- `check_count.py`
- `cleanup_unknown.py`

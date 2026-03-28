# 运维与诊断

## Docker Compose

```bash
docker-compose up -d
docker-compose logs -f pusher
docker-compose logs -f web
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

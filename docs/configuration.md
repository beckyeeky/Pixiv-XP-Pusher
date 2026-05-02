# 配置说明

完整配置请以 `config.example.yaml` 为准。以下是常用字段分组。

## pixiv
- `refresh_token`：主账号 token
- `sync_token`：可选，独立同步 token
- `user_id`：用于画像分析的用户 ID

## strategies
可选策略：`xp_search` / `related` / `ranking` / `subscription`

## scheduler
- `cron`：主任务 cron
- `daily_report_cron`：日报/维护 cron

## filter
- `daily_limit`、`max_per_artist`
- `exclude_ai`、`skip_ugoira`
- `content_type`：`all` / `illust` / `manga`
- `r18_mode`：`mixed` / `r18_only` / `safe`
- `display_tags.max_ip_count`：推送展示时最多保留多少个 IP 标签；feature 标签优先展示
- `author_diversity`：同画师连续作品衰减，默认衰减更激进（`decay_factor: 0.5`）
- `ip_diversity`：同 IP 连续出现时对后续作品降权，默认 `decay_factor: 0.6`、`floor: 0.1`

## tag_classifier
- `enabled`：启用后将标签区分为 `feature` 与 `ip`
- `api_key` / `base_url` / `model`：兼容 OpenAI 接口；未配置时会回退到手动 IP 列表
- `ttl_days` / `batch_size` / `concurrency`：控制缓存与批处理并发

启用后会影响三处行为：
- 匹配度计算里 `feature` 标签按 `1.3x` 加权，`ip` 标签不额外加成
- 推送消息中的 `display_tags` 按 feature-first 排序
- `filter.ip_diversity` 使用分类结果识别“同坑连续出现”的作品并做衰减

## fetcher
- `search_limit`、`date_range_days`
- `bookmark_threshold.search` / `subscription` / `related`

## notifier
- `types`：可启用多个通知器
- `telegram` / `onebot` / `astrbot` 各自独立配置

## web
- `enabled`
- `require_login_password`
- `password`
- `port`

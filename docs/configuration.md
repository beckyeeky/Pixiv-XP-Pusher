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

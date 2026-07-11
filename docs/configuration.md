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
- `enabled`：启用后将标签区分为 Feature、Character、Copyright、Artist、Non-preference 与 Unresolved
- `api_key` / `base_url` / `model`：兼容 OpenAI 接口；未配置 `judges` 时作为单 Judge 的兼容回退
- `judges`：多个独立 Judge Model；相同 `provider + base_url + model` 只计一票
- `maintenance.max_tags_per_run`：每次只刷新高影响画像标签，Unresolved 可优先
- `danbooru`：仅查询被选中的画像标签；证据会缓存，超时或错误时继续使用缓存和 Judge 投票。若 login/api_key 为空，会继承 `profiler.danbooru_login` / `profiler.danbooru_api_key`
- 机器 Tag Evidence 按 source 独立保鲜 60 天；缓存读取不会刷新时效，只有该 source 成功复核才会更新。人工审核永不过期。
- `--once` 在成功推送 Daily Slate 后最多等待 90 秒完成有界 Classification Maintenance；维护失败或超时会独立记录，不会撤销推送结果。调度模式始终后台执行且不会重复启动活动维护。

人工审核既可通过受鉴权保护的 `GET/POST /api/tag-reviews` 完成，也可使用维护命令：

```bash
python scripts/review_tag_queue.py list
python scripts/review_tag_queue.py review tag_name character
```

启用后会影响三处行为：
- 匹配度计算里 `feature` 标签按 `1.3x` 加权，`ip` 标签不额外加成
- 推送消息中的 `display_tags` 按 feature-first 排序
- `filter.ip_diversity` 使用分类结果识别“同坑连续出现”的作品并做衰减
- `filter.daily_slate` 依据 Feature/Character/Copyright 的 strongest Preference Contribution 决定动机；Feature 缺额时先将 Exploration 扩展到 40%，再考虑扩大身份动机份额

## fetcher
- `search_limit`、`date_range_days`
- `bookmark_threshold.search` / `subscription` / `related`

## notifier
- `types`：可启用多个通知器
- `telegram` / `onebot` / `astrbot` 各自独立配置
- `telegram.rich_message.enabled`：实验性 Telegram Bot API 10.1 Rich Message 推送，默认关闭
- `telegram.rich_message.fallback_to_photo`：Rich Message 失败时回退普通图片消息，默认开启
- `telegram.rich_message.image_mode`：图片展示模式，`photo` 保持可点击放大，`rich_card` 使用 Rich Message 卡片，`hybrid` 同时发送 Photo 和 Rich 卡片，默认 `photo`

## web
- `enabled`
- `require_login_password`
- `password`
- `port`


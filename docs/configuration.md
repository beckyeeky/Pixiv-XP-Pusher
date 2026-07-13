# 配置说明

完整配置请以 `config.example.yaml` 为准。以下是常用字段分组。

配置模板升级后，可将现有值自动填入新版模板：

```bash
python scripts/refresh_config.py config.yaml --output config.new.yaml
```

新文件保留 `config.example.yaml` 中的注释、字段顺序与新增默认项，并覆盖同名的现有配置。因此生成文件本身就是新版字段的带说明写法。默认会移除模板中没有的旧字段，并在生成后以 YAML 输出这些字段及其原始值；如需原样保留它们，可加 `--keep-unknown`。

旧版 `tag_classifier` 直接填写的 `provider`、`api_key`、`base_url`、`model` 会自动迁移到新版 `providers` 与第一个 `models` 条目，并在终端显示迁移路径。

`profiler.boost_tags` 当前没有接入主任务的画像构建流程，因此不在模板中保留；脚本会将旧值报告为未匹配字段，避免生成看似有效但实际不生效的配置。

## providers
- `type: pixiv`：唯一的 Pixiv Provider，包含 `refresh_token`、可选 `sync_token` 与 `user_id`。
- `type: danbooru`：唯一的 Danbooru Provider，包含可选 `login`、`api_key` 和 `base_url`；不会配置 Model。
- LLM Provider 可配置多个；Model 只可引用 LLM Provider。
- `models.<name>.capabilities` 声明可用功能：`llm` 用于 Scorer/Judge/Profiler，`embedding` 用于语义 Embedding，也可同时填写两者。
- WebUI 按能力分别暴露 LLM / Embedding 已知模型目录；自定义模型名仍可直接填写。
- `profiler.ai.model`、`ai.scorer.model` 只能选择具备 `llm` 能力的 Model；`ai.embedding.model` 只能选择具备 `embedding` 能力的 Model。凭据、地址和 Provider 类型统一从所选 Model 解析。
- 旧版根级 `pixiv`、`profiler.danbooru_*` 与 `tag_classifier.danbooru` 凭据会在读取时迁移到 typed Provider，兼容字段仅供旧脚本读取。

旧版 `profiler.ai` / `ai.embedding` / `ai.scorer` 中的 `provider`、`api_key`、`base_url` 和实际模型名会在读取时分别迁移为 `profiler_default` / `embedding_default` / `scorer_default` Model；运行时仍保留旧配置回退路径。

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
- `providers`：顶层 typed Provider 配置；Pixiv 与 Danbooru 各只能有一个，LLM Provider 可配置多个。自定义 OpenAI-compatible Provider 必须提供 `base_url`。
- `models`：顶层 Model 配置，每项只引用一个 Provider，并填写该 Provider 可用的模型名称。
- `judges`：引用 `models` 的名称列表；每个不同 `Provider + Model` 身份只计一票。内嵌 Judge 对象不再支持。
- `maintenance.max_tags_per_run`：每次只刷新高影响画像标签，Unresolved 可优先
- `maintenance.concurrency`：后台 Gemini Grounded Judge 的最大并发请求数，默认 `10`
- `grounded_judge`：Gemini Grounded Judge 的请求配置。默认总超时 `45` 秒、输出上限 `512` token、温度 `1`；超时、连接错误、限流（429）或服务端错误会按指数退避自动重试 2 次。
- `danbooru`：仅查询被选中的画像标签；证据会缓存，超时或错误时继续使用缓存和 Judge 投票。连接凭据与地址由 `type: danbooru` Provider 提供。
- 机器 Tag Evidence 按 source 独立保鲜 60 天；缓存读取不会刷新时效，只有该 source 成功复核才会更新。人工审核永不过期。
- `--once` 在成功推送 Daily Slate 后最多等待 90 秒完成有界 Classification Maintenance；维护失败或超时会独立记录，不会撤销推送结果。调度模式始终后台执行且不会重复启动活动维护。

人工审核既可通过受鉴权保护的 `GET/POST /api/tag-reviews` 完成，也可使用维护命令：

```bash
python scripts/review_tag_queue.py list
python scripts/review_tag_queue.py review tag_name character
```

高权重但尚未分类的 Preference Profile Tag 应先导出、审核，再显式执行 Grounded Judge；不要直接对整个画像批量分类：

```bash
python scripts/maintain_high_weight_tags.py --limit 40 --min-weight 1.0 --output /tmp/reviewed-tags.json
# 审核 /tmp/reviewed-tags.json 后：
python scripts/maintain_high_weight_tags.py --apply --reviewed-tags /tmp/reviewed-tags.json
```

Telegram 的 `🏷️ 标签审核` 菜单提供同一流程：先点 `📋 查看高权重候选`，确认列表后才会出现并执行分类按钮。候选快照会在确认前重新校验，避免分类已不再符合当前条件的 Tag。

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

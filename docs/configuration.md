# 配置说明

完整配置请以 `config.example.yaml` 为准。以下是常用字段分组。

`tag_mapping` 既负责生成待人工审核候选，也可使用同一 LLM Model 对现有候选生成 AI Relationship Recommendation。候选复核、证据失效规则、CLI 预选和最终 Web 人工确认流程见 [AI 辅助 Tag Mapping 审核](./tag_mapping_ai_review.md)。

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
- `models.<name>.capabilities` 声明可用功能：`llm` 用于 Scorer/Judge/Tag Mapping Candidate，`embedding` 用于语义 Embedding，也可同时填写两者。
- WebUI 按能力分别暴露 LLM / Embedding 已知模型目录；自定义模型名仍可直接填写。
- `tag_mapping.model`、`ai.scorer.model` 只能选择具备 `llm` 能力的 Model；`ai.embedding.model` 只能选择具备 `embedding` 能力的 Model。凭据、地址和 Provider 类型统一从所选 Model 解析。
- 旧版根级 `pixiv`、`profiler.danbooru_*` 与 `tag_classifier.danbooru` 凭据会在读取时迁移到 typed Provider，兼容字段仅供旧脚本读取。

旧版 `profiler.ai` 会迁移为只生成待审候选的 `tag_mapping`；它不再过滤标签、合并画像或写入正式映射。旧版内联 Provider 配置会迁移到共享 Model，`ai.embedding` / `ai.scorer` 的兼容迁移保持不变。

标签映射候选必须在“标签管理 → 标签映射候选”中人工接受后才会成为 `Tag Alias` 或 `Search Alias`。旧 `ai_tag_cache` 与 `tag_mapping_stats` 数据会保留并一次性导入候选队列，但不再被运行时读取。

## strategies
可选策略：`xp_search` / `related` / `ranking` / `subscription`

## scheduler
- 主推送计划保存在数据库 `schedule_cron`。新数据库默认为每日 09:30 和 21:00。请使用 Telegram `/schedule` 修改。
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
- `judges`：旧版 Gemini / 多 Judge 证据路径。Search-first 模式会忽略并清空此列表，不需要配置。
- `maintenance.max_tags_per_run`：每次只刷新高影响画像标签，Unresolved 可优先
- `maintenance.concurrency`：后台 Grounded Judge 的最大并发请求数，默认 `10`
- `grounded_judge`：Search-first 搜索判定配置。分类温度固定为 `0`，不需要 Gemini Model 或 Gemini API Key。
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

### Search-first Grounded Judge

生产标签判定固定使用以下流程：

1. Brave 搜索一次并提供证据。
2. 只有 Brave 没有有效证据时，才使用 Tavily Advanced 搜索。
3. DeepSeek Flash 只根据搜索证据输出分类，温度固定为 `0`。
4. 搜索失败或模型不确定时保留为 Unresolved，不调用 Gemini 兜底。

自动维护只处理达到 Maintenance Eligibility 的画像标签；新 tag 不会在出现时立即调用搜索。推荐起点是 `maintenance.min_profile_weight: 1.0`、`max_tags_per_run: 40`。每个 Brave/Tavily 账号应作为一个独立 Provider，列表顺序即额度耗尽顺序：

```yaml
providers:
  brave_search_1: {type: brave_search, api_key: "..."}
  tavily_search_1: {type: tavily_search, api_key: "..."}
  deepseek: {type: deepseek, api_key: "...", base_url: "https://api.deepseek.com/v1"}
models:
  deepseek_flash: {provider: deepseek, model: deepseek-v4-flash, capabilities: [llm]}
tag_classifier:
  enabled: true
  judges: []
  maintenance: {max_tags_per_run: 40, concurrency: 3, min_profile_weight: 1.0}
  grounded_judge:
    backend: search_first
    search_classifier_model: deepseek_flash
    brave_providers: [brave_search_1]
    tavily_providers: [tavily_search_1]
    brave_request_limit: 1000
    tavily_request_limit: 500
```

WebUI 配置顺序：

1. 在 Provider 页面分别创建 Brave Search、Tavily Search 和 DeepSeek/OpenAI-compatible Provider，并填写各自凭据。
2. 在 Model 页面创建指向 DeepSeek Provider 的 `deepseek_flash` LLM Model。
3. 在“AI 功能 → 搜索标签判定”中选择分类模型、Brave 账号池和 Tavily 账号池。
4. 保存后即可通过 WebUI 或 Telegram 运行高影响维护批次。

Gemini Provider/Model 即使仍保留在通用 Provider/Model 列表中，也不会被标签判定调用；确认没有被其他功能引用后可自行删除。

### Search-first Shadow Evaluation

上线前后都可使用 Brave LLM Context、Tavily Advanced fallback 和 DeepSeek Flash 对 JSONL 样本做只读对照。每行至少包含 `tag`，可选 `translation` 与 `expected_classification`；脚本不会读取或修改 `config.yaml`，也不会写入任何 Tag Classification。

```bash
export BRAVE_SEARCH_KEY_1='...'
export TAVILY_SEARCH_KEY_1='...'
export DEEPSEEK_API_KEY='...'
python3 scripts/run_search_judge_shadow.py \
  --input /tmp/tag-shadow.jsonl \
  --report /tmp/tag-shadow-report.json \
  --brave-key-env BRAVE_SEARCH_KEY_1 \
  --tavily-key-env TAVILY_SEARCH_KEY_1
```

可重复传入 `--brave-key-env` / `--tavily-key-env` 以使用独立 Quota Pool。默认每个 Brave Pool 最多 1000 次搜索；Tavily Advanced 默认最多 500 次搜索（每次 2 credits）。请按实际套餐使用 `--brave-free-search-limit` 或 `--tavily-free-search-limit` 调整。每次已提交且未被拒绝的搜索（即使没有 snippets）都会计入额度；默认在 `data/search_judge_quota_usage.json` 保存当月匿名 pool 用量，以便下次运行继续使用同一额度。可用 `--quota-state-path` 改为其他本地路径。报告只含匿名 pool ID、统计和来源 URL，不会输出 Key。

要优先验收对推荐影响最大的标签，可只读导出人工分类记录；默认将 `abs(xp_profile.weight)` 最高的 50 条标为 `priority`（包含强负反馈），而报告会单列 `priority_metrics`。这不会修改数据库或生产分类：

```bash
python3 scripts/export_tag_shadow_manual.py \
  --output /tmp/tag-shadow-priority.jsonl \
  --priority-limit 50

python3 scripts/run_search_judge_shadow.py \
  --input /tmp/tag-shadow-priority.jsonl \
  --report /tmp/tag-shadow-priority-report.json \
  --brave-key-env BRAVE_SEARCH_KEY_1 \
  --tavily-key-env TAVILY_SEARCH_KEY_1
```

Telegram 的 `🏷️ 标签审核` 菜单提供同一流程：先点 `📋 查看高权重候选`，确认列表后才会出现并执行分类按钮。候选快照会在确认前重新校验，避免分类已不再符合当前条件的 Tag。

作品级 `semantic_weight` 不应凭感觉调整。运行 `python3 scripts/calibrate_embedding_weight.py` 可用历史 like/dislike 和当前缓存向量生成只读离线对照；样本不足时不会给出建议，也不会自动修改配置。指标与解释见 [作品级 Embedding 权重校准](./embedding_weight_calibration.md)。

启用后会影响三处行为：
- 匹配度计算里 `feature` 标签按 `1.3x` 加权，`ip` 标签不额外加成
- 推送消息中的 `display_tags` 按 feature-first 排序
- `filter.ip_diversity` 使用分类结果识别“同坑连续出现”的作品并做衰减
- `filter.daily_slate` 依据 Feature/Character/Copyright 的 strongest Preference Contribution 决定动机；Feature 缺额时先将 Exploration 扩展到 40%，再考虑扩大身份动机份额

## fetcher
- `search_limit`、`date_range_days`
- `bookmark_threshold.search` / `subscription` / `related`
- `semantic_vector_exploration`：默认关闭的独立 Exploration 候选来源，启用时要求 `filter.daily_slate.enabled=true`。它只读取与当前 Model 匹配的画像/作品 Embedding 缓存，在 `pool_limit` 内做进程内余弦相似度比较，再将最多 `candidate_limit` 个详情完整的作品送入统一 Filter。候选只能参与 Daily Slate 的 Exploration lane，仍受过滤规则与 Identity Cap 限制；相似度不会创建 Tag Alias 或修改 Normalized Tag。

启用后，每次检索会在 schema v7 的 `exploration_vector_runs` 与 `exploration_vector_candidates` 中记录来源、相似度、Model、检索排名、最终排名和是否入选。只读验收报告：

```bash
python3 scripts/evaluate_vector_exploration.py
python3 scripts/evaluate_vector_exploration.py --json
```

报告聚合入选候选的反馈覆盖/like rate、检索到最终排序的排名移动、运行时 Preference Profile 权重分布的 HHI 集中度，以及入选 Daily Slate 作品缓存向量的 pairwise 重复语义率；另附 Slate 对画像标签支持的 HHI 作为诊断项。不同配置或上线窗口应使用 `--since` / `--model` 分开比较，避免与 `semantic_weight` 校准混为同一次发布。

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

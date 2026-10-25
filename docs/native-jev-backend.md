# 原生 Jev 标签分类后端（候选实现，未部署）

状态：`/var/minis/workspace/pixiv-jev-implementation` 中的**未提交候选**。基线生产 HEAD
`bc5f801c4ef121e33875fc72f132b92129eaf4de`。本目录无 `.git`，未提交、未推送、未部署、未写入生产。
默认配置仍为 `search_first`；原生后端只有在 `tag_classifier.grounded_judge.backend: native_jev`
**且** `native_jev.enabled: true` 时才构建。

## 1. 目标与边界

- 新增**可选**后端：Exa + Tavily 优先池（按额度利用率负载均衡）→ DeepSeek Search 仅第三顺位
  fallback → 原生 System One `jev-1.13` 对每个搜索来源独立给出 `choice` 分类。
- 复用既有维护入口（`classification_maintenance.ClassificationMaintenance` /
  `classify_and_activate_tag` / `run_scheduled_maintenance`）、既有 Provider 映射、既有月度额度账本
  （`MonthlyQuotaUsageLedger`）和既有 `activate_ai_tag_classification` 写入路径。
- 不改写人工结果、已有明确分类、旧 IP 标签与 alias 映射；不新建自动任务；默认不启用自动外搜。
- **费用上限无法严格实现**：本实现只用本地计数、滚动窗口预算与冷却降低风险，不能阻止上游计费
  超出预算；不提供、也不声称任何硬性费用保证。

## 2. 模块与文件

| 文件 | 变更 |
| --- | --- |
| `native_jev_judge.py` | 新增：凭据解析、Exa/DeepSeek Search 客户端、System One 客户端、优先级路由、聚合门控、状态存储、预算、审计、生产适配器与构建器 |
| `grounded_judge.py` | 新增 `ExistingClassificationPreserved`；`classify_single_tag` 增加 `native_jev` 分支（未启用时转为 `GroundedJudgeDeferredError`） |
| `classification_maintenance.py` | `preserved` 状态与计数；provenance 记录动态 `backend`、`engine` 轨迹与有界 `activation` |
| `config.py` | 接受 `native_jev` 后端与 `exa_search` Provider；归一化 `exa_providers` 与 `native_jev` 配置段（缺省不生成该段） |
| `provider_model_graph.py` | 注册 `exa_search` Provider 类型；`native_jev` 校验至少一个 Exa/Tavily 池 + OpenAI-compatible OpenRouter Provider；`search_first` 校验仍只要求 Brave+Tavily |
| `tag_classifier.py` | `native_jev` 与 `search_first` 一样视为“维护专用后端”，投递阶段不初始化旧 Judge |
| `scripts/preview_native_jev.py` | 新增单批预览 CLI（默认只读） |
| `config.example.yaml` | 新增注释掉的 `exa_search_1` 与 `native_jev` 示例；生效配置未改动 |
| `tests/test_native_jev_judge.py` | 新增 65 个回归测试（全部 mock 网络） |

## 3. 检索层

优先级固定为：

1. **Exa**（`exa_providers`，`type: exa_search`）
2. **Tavily**（`tavily_providers`，`type: tavily_search`；`native_jev.tavily_providers` 为空时沿用
   `grounded_judge.tavily_providers`）
3. **DeepSeek Search**（`deepseek_search_provider`，`type: deepseek`/`openai_compatible`）

Exa 与 Tavily 属于同一优先层：两个引擎都会被尝试；单引擎内部多账号通过
`SearchCredentialPool` 按“已用/额度上限”利用率选择，额度耗尽时在同进程内切换账号。
DeepSeek Search 仅在优先层无法提供足够的相关来源（默认 2 个，等于 `min_agreeing_sources`）时调用，
不参与常规轮转。

请求形状：

- Exa：`POST {endpoint|https://api.exa.ai/search}`，头 `x-api-key`，体
  `{query, type: "auto", numResults, contents:{text:{maxCharacters}}}`。
- Tavily：复用 `search_grounded_judge.TavilySearchClient`（`search_depth: advanced`）。
- DeepSeek Search：`POST {base_url}/tools/search`，`Authorization: Bearer`，体
  `{query, count, need_content: true}`。
- 402/429/432 → `PoolQuotaExhausted`（触发引擎冷却 + 账号额度耗尽）；401/403 → `PoolUnavailable`；
  5xx/408/409 → 瞬时错误冷却。

## 4. 决策层（System One）

`POST {jev_endpoint|https://openrouter.ai/api/v1/systemone}`，`Authorization: Bearer <OPENROUTER key>`：

```json
{
  "model": "jev-1.13",
  "state": {"tag": "<原始归一化标签>", "evidence": [{"url": "", "title": "", "text": ""}]},
  "questions": {"category": {"type": "choice", "instructions": "...", "criteria": {
    "feature": "...", "character": "...", "copyright": "...",
    "artist": "...", "non_preference": "...", "unresolved": "..."}}}
}
```

- 每个搜索来源**独立一次**调用（不共享上下文），与 2026-10-24 十标签实验方法一致。
- 解析 `answers.category.{choice,confidence,probabilities}` 与 `usage.{input_tokens,output_tokens,cost}`，
  记录响应 `model`（例如 `typesafe/jev-1.13-20260917`），不把请求模型名当作已核实响应模型。
- `choice` 必须是六个类别之一、`confidence` 必须落在 `[0,1]`，否则判为协议错误（不写库）。

## 5. 自动激活门控

写入条件（全部满足）：

1. 至少 `min_agreeing_sources`（默认 2）个**不同搜索引擎**给出**同一非 unresolved** 类别；
2. 这些引擎的 `confidence` 均**严格大于** `min_confidence`（默认 0.9，等于不算通过）；
3. 没有任何引擎给出 `confidence > min_confidence` 的**相反**已解析类别；
4. 证据中至少提及标签本体（`require_tag_mention: true` 时），否则判为低相关，不调用模型；
5. 该标签当前没有人工分类，也没有已解析的 AI 分类（命中即 `skipped_existing` → `preserved`）。

不写库的情形分两类，边界固定：

- `hold`：只有**真正完成的检索**（该引擎返回了结果，含 0 条结果）才能进入 `hold`——`no_evidence` /
  `low_relevance` / `model_unresolved` / `insufficient_agreement` / `conflicting_classification`。
  `hold` 记录未解决冷却，保持人工复核队列（与原 `mark_ai_tag_unresolved` 语义一致）。
- `deferred`：运营性条件——`budget_exhausted`、`engine_cooldown`、`engines_not_configured`、
  `insufficient_engines_configured`、`search_unavailable`、`jev_rate_limited`、`jev_unavailable`、
  `task_lock_timeout`。`deferred` 既不写数据库，也**不建立标签级长冷却**（避免一次额度耗尽让标签
  24 小时不可用，并被误记为 unresolved）。

### 第三顺位补查（DeepSeek Search）的边界

- 只有在主层检索完成、分类完成、且门控未通过（`insufficient_agreement` / `model_unresolved`）
  时，才向**尚未尝试**的第三顺位引擎补发 **一次** 检索 + 分类，然后重新聚合。
- 两个主引擎已给出 `confidence > min_confidence` 的**相反**明确类别时，直接 `hold`
  （`conflicting_classification` 或 `insufficient_agreement`），**不**用第三票做多数覆盖，
  以节省一次请求。
- 第三票只有在满足“两个不同引擎一致且都 > 阈值”时才会参与激活；它不能单独激活，也不能推翻对立类别。

### URL 重合与“独立证据”

`url_report` 会记录每引擎 URL 集合、共享 URL 数、去重 URL 总数与 `independence`：

- `shared_pages`：各引擎 URL 集合完全相同 → `corroboration_basis = "same_pages"`；
- `partial_overlap`：部分共享；
- `independent_pages`：无共享 URL；
- `single_source`：仅一个引擎。

`activation` 中固定写入
`engine_agreement_is_not_independent_web_evidence: true`：**引擎数不等于独立网页证据**，
入库 provenance 同时保存共享 URL 与独立性标记，供人工复核。

## 6. 状态、冷却与预算

| 机制 | 位置 | 行为 |
| --- | --- | --- |
| 决策缓存 | `state_path`（默认 `data/native_jev_state.json`） | 已激活决策按 `decision_cache_ttl_seconds`（默认 7 天）复用，命中时不再请求任何 Provider，状态 `cached`；条目绑定 `policy_fingerprint`（model/阈值/最少来源/提及要求），阈值或策略变低的旧条目**不再复用**，并按当前策略重新校验 |
| 未解决冷却 | 同上 `cooldowns["tag:<tag>"]` | `hold` 后默认 24 小时不再检索/调用模型 |
| 自动暂停（候选 `jev-queue-pause`） | 同上 `pauses[<tag>]` | `hold` 时写入的**持久**记录（无到期时间）：自动批量入口在选择前排除全部暂停/冷却标签并向后补足；只有单标签显式手动重试才清除。详见 [jev-queue-pause.md](jev-queue-pause.md) |
| 限流冷却 | 同上 `cooldowns["engine:<engine>"]` | 402/429/432/401/403 后默认 900 秒跳过该引擎；5xx/超时默认 60 秒 |
| 模型冷却 | 同上 `cooldowns["engine:jev"]` | System One 429 后默认 900 秒内所有标签 deferred |
| 并发预算 | 进程内 `ConcurrencyBudget` | 同时进行的检索 + 模型请求不超过 `max_concurrent_requests`（默认 4） |
| 请求预算 | 进程内 `RequestBudget` | 滚动窗口 `budget_window_seconds`（默认 300s）内最多 `per_run_request_budget`（默认 24）次请求，超限 deferred |
| 月度额度账本 | `quota_state_path`（沿用既有账本） | 池 ID 前缀 `native_jev:<provider>`，避免覆盖 Brave/Tavily Search-first 计数；预览与维护共用同一账本（用量按凭据计） |
| 单任务锁 | `lock_path`（默认 `<state_path>.lock`） | 跨进程 `flock` 互斥：整个 native 批次 / CLI 请求 / 账本更新序列化；等待 `lock_timeout_seconds`（默认 5s）超时即 `deferred(task_lock_timeout)`，避免两个进程用旧快照互相覆盖 |

状态文件与账本都**不包含任何凭据**；测试断言写入内容不含密钥。

## 7. 凭据来源（不改现有配置语义）

解析顺序（每种引擎独立）：

1. Provider 的 `api_key`（复用现有 `providers` 映射，含已有的 `openrouter`/`deepseek`/`tavily_search_N`）；
2. Provider 的 `api_key_env` 指定的环境变量；
3. 标准环境变量：`EXA_API_KEY`、`TAVILY_API_KEY`、`DEEPSEEK_API_KEY`、`OPENROUTER_API_KEY`；
4. `native_jev.env_file`（或 `grounded_judge.env_file`）指向的 `KEY=VALUE` 文件，只读解析、不记录内容。

主代理已核实：VPS 上 `/etc/pixiv-xp-pusher/exa.env`（root:root 0600）保存了 `EXA_API_KEY`，
未配置 systemd `EnvironmentFile`、未重启、未改 `config.yaml`；生产加载在发布阶段决定。
本候选实现**未读取**该文件，也不要求重复配置 OpenRouter（复用现有 Provider）。

缺少必需凭据时构建器抛出不含密钥值的 `ValueError`，指出引擎与变量名；缺少标签覆盖的
“Exa/Tavily 至少一个”同样报错。`enabled` 未显式为 `true` 时抛
`NativeJevDisabledError`（`ValueError` 子类），经 `classify_single_tag` 转为
`GroundedJudgeDeferredError`，不会把标签标成 unresolved。

## 8. 单批预览 CLI

```bash
python3 scripts/preview_native_jev.py --tags "魅惑の顔,キス" --output report.json
python3 scripts/preview_native_jev.py --limit 5 --min-weight 1.0
```

- 默认**只读**：直接调用 `NativeJevJudge.evaluate`，不写数据库。
- 默认载入的候选队列只用**只读 SQLite 连接**（`mode=ro`）读取，不执行 `init_db`、不做迁移：
  数据库缺失或 schema 不符时**中止并报错**，绝不创建或修补数据库。
- 预览的决策缓存/冷却写入独立路径 `<state_path>.preview`，不会污染生产决策；月度用量账本仍是
  同一 `quota_state_path`（按凭据计）；整个 CLI 请求持有与维护批次相同的单任务锁。
- 报告含 `policy`、`quota_estimates`（`estimated: true`、`upstream_verified: false`、
  `basis: "local_monthly_counter"` 及“未查询上游余额”的 caveat）、每个标签的
  `sources`/`url_report`/`search_trace`/`usage`。
- `--audit-log` / `--state-path` 仅覆盖本次运行，不修改磁盘配置。
- `--apply` 需要同时给出 `--confirm-apply`，才会调用既有
  `ClassificationMaintenance.run_tags`（当前候选任务不执行该路径）。

审计（`native_jev.audit_log_path` 非空时）为 JSONL，事件类型：

- `search_request`：引擎、端点、查询词；
- `search_response`：provider、池 ID、返回 URL、前两条证据摘录（≤500 字符）、usage；
- `search_error` / `jev_error`：`error_type` 与有界错误文本（不含 traceback）；
- `jev_request`：System One 完整请求体（model/state/questions，证据文本已按上限截断）；
- `jev_response`：类别、confidence、probabilities、响应 model、usage；
- `decision`：标签、状态、原因、搜索轨迹、来源、用量。

**不写**请求头、`Authorization`、API key、Cookie、原始异常栈或**上游错误原始体**：

- `search_error` / `jev_error` 只记录 `error_type`、受控 `error_code`（`quota_exhausted` /
  `credential_unavailable` / `search_error` / `timeout` / `network_error` / `jev_*` /
  `unexpected_error`）与 `http_status`，不记录异常字符串——供应商可能在任意字段、任意格式回声
  被拒凭据（不限 `sk-` 前缀）；
- `search_trace` 同样只带 `error_code` / `http_status`，不带文本；
- 进程实际解析出的凭据会注册进 `AuditLog`，任何被审计字符串一旦包含该值即被替换为 `[redacted]`；
  即使未注册，原始异常体也从不进入日志；
- 键名命中 `headers/authorization/api_key/x-api-key/cookie` 的字段直接丢弃，`sk-…`/`Bearer …`
  做模式脱敏，所有字符串统一截断。

## 9. 成本与额度限制（明确的不保证项）

- 所有“剩余额度”均为**本地月度计数器估算**，未查询 Exa/Tavily/DeepSeek/OpenRouter 的上游余额；
  预算或额度可能与供应商后台不一致。
- Tavily Advanced Search 每次请求消耗 2 credits，默认估算上限 500（即约 250 次请求），
  与既有 Search-first 默认一致。
- DeepSeek Search 的默认月度上限 200 是保守占位值，**没有**公开账单依据，可在
  `quota_estimates` 中改写。
- Jev 按 token 计费（响应含 `usage.cost`，以 OpenRouter 结算为准）；搜索费用另计。
- 无法在代码层强制费用上限：预算窗口、并发上限与冷却只能降低失控风险；上游价格变化、
  重试与多账号并行都可能超出预期花费。
- 只配置单一搜索引擎时，门控永远无法满足（需要两个不同引擎）：后端直接 `deferred`
  （`insufficient_engines_configured`），不发起请求、不建标签冷却、不写 unresolved。
- 预算与额度都是**本地软限额**：滚动窗口计数在进程内，多个进程各自持有；只有同一凭据的月度用量
  共享既有账本。因此在多进程并发时，合计请求量可能略高于单进程预算；未查询上游剩余额度。

## 10. 已知未验证边界

- **Exa REST 请求/响应形状未对真实 API 验证**：2026-10-24 实验使用的是 Exa MCP
  （`mcp.exa.ai/mcp` 的 `web_search_exa`），本实现按 Exa 公开 REST 形状实现；端点可通过
  `native_jev` 覆盖，实网验证需主代理在发布前完成。
- DeepSeek Search 形状取自本机 `web-search-router` 技能脚本（`/v1/tools/search`、
  `{query,count,need_content}`），本任务未实网调用。
- System One 请求/响应形状取自归档文档（`/var/minis/shared/pixiv-jev-demo/20261024/openrouter-sdk.json`）
  与 2026-10-24 成功请求（`ten-tags/*-request-body.json`、`response-raw.json`）；真实 `confidence`
  不等于正确率。
- 并发、时延、重试放大与跨月份账本滚动只在 mock 中验证。
- `languages` 由标签脚本推断（CJK→`ja`，Hangul→`ko`，Cyrillic→`ru`，其余取
  `default_language`，默认 `ja`），provenance 以 `languages_basis` 标注，不是模型输出。
- 未做真实网络、真实计费、真实数据库写入的端到端测试。

## 11. 发布前建议（供主代理裁决）

1. 先在候选环境用 `scripts/preview_native_jev.py --tags <固定样本>` 做真实网络试跑，核对 Exa
   REST、DeepSeek Search 与 System One 实际形状与费用。
2. 决定 Exa 凭据加载方式：systemd `EnvironmentFile=/etc/pixiv-xp-pusher/exa.env` 或
   `native_jev.env_file`，并确认服务账号读取权限（当前 0600 root）。
3. 明确 `quota_estimates` 的真实额度、`audit_log_path` 的磁盘轮转策略。
4. 决定是否允许 `allow_automatic_external_search: true`（默认保持 false）。
5. 灰度：先 `--tags` 固定小样本（不 `--apply`），再受限 `run_reviewed`，最后才考虑定时维护。

## 12. 回滚

- 将 `grounded_judge.backend` 改回 `search_first`（或 `gemini`）即可；`native_jev` 段可留空。
  注意：`apply_settings_payload` 只接受**双动作**离开——同一次保存里既要提交
  `grounded_judge.backend: search_first`（或 `gemini`），又要提交
  `native_jev.enabled: false`；只改 `backend` 的保存（包括旧页面自动提交的
  `search_first`）一律保留 `backend: native_jev` 与整棵 `native_jev` 子树。
  其中 `native_jev.enabled: false` 表示“停用该后端”，`native_jev` 段本身仍留在配置里，
  下次要恢复只需把 `enabled` 改回 `true`。
- 新代码路径只在 `backend == "native_jev"` 时进入；`exa_search` Provider 与未使用的
  `native_jev` 段对旧行为无影响。
- 生成的 `data/native_jev_state.json`、审计 JSONL 与账本中的 `native_jev:` 前缀条目可删除，
  不影响既有 `search_judge_quota_usage.json` 的 Brave/Tavily 计数。

## 11. 第二轮独立审查修复（2026-10-25）

主代理已复核的独审反例逐项修复，反例脚本保留在
`/var/minis/workspace/jev-independent-review/`（只读，不修改）：

| 编号 | 问题 | 修复 | 证据 |
| --- | --- | --- | --- |
| B1 | 设置页保存无关字段会把 `backend` 改回 `search_first`，并在 `normalize_config` 中丢掉 `native_jev` 子树 | 页面不再硬编码后端；`apply_settings_payload` 只在显式 `native_jev.enabled: false` 时才允许离开该后端，否则保留后端与整棵子树 | `SettingsPreservationTests` |
| B2 | 等待期间新增的明确 AI/人工分类被覆盖（激活与 hold 回退） | `activate_ai_tag_classification` / `mark_ai_tag_unresolved` 新增 `preserve_resolved`，在 `BEGIN IMMEDIATE` 事务内重读并保护已解析行；旧后端默认语义不变 | `ActivationConcurrencyTests` |
| B3 | 预算耗尽 / 引擎冷却 / 未配置组合被当作 `hold`，建 24h 标签冷却并写 unresolved | 只有完成的检索才 `hold`；运营性条件一律 `deferred`，不建冷却、不写库 | `DeferralTests` |
| B4 | 默认预览走 `init_db`，会写 schema / 创建数据库 | 只读连接读取队列；缺库或 schema 不符即中止，绝不建库 | `ReadOnlyPreviewTests` |
| C1 | `min_confidence` 可被配置成 0.5，低阈值旧缓存继续生效 | 阈值下限 0.9（严格大于），缓存条目绑定策略指纹并按当前策略重新校验 | `ThresholdAndCacheTests` |
| C2/C4 | 多实例各自持有旧快照，互相覆盖冷却与账本 | 跨进程单任务锁串行化整个批次/CLI 请求；超时 `deferred` | `TaskLockTests` |
| C3 | 审计把上游错误体（可能回声任意格式密钥）写盘 | 审计与 trace 只保留受控 `error_code`/`http_status`，凭据值精确脱敏，异常文本不落盘 | `ReflectionLeakTests` |
| D1 | 一高一低证据不再补查，可救回的证据被丢弃 | 分类后再补一次 DeepSeek 第三顺位；已有明确对立类别时直接 hold，不花第三票 | `ThirdSourceFallbackTests` |
| D2 | 首查顺序固定 Exa 先 | 按剩余额度/冷却/近期负载排序（同分保持配置顺序）；两家健康时仍各请求一次 | `EngineOrderTests` |

新增回归：`tests/test_native_jev_review_fixes.py`（31 项，已加入 `tests/run_offline.py` 默认模块）。
旧基线在这 31 项中失败 22 项，修复后全部通过。

### 仍未覆盖 / 需主代理裁决

- 单任务锁只保护**已接入的入口**（维护批次、预览 CLI）。`NativeJevStateStore` 的
  `cool()`/`put_decision()` 本身仍是“读-改-写”，直接并发使用两个 store 实例仍可能丢一次更新；
  任何新入口必须复用 `native_task_lock_for_config`。
- 锁是单机 `flock`，不跨主机；多主机部署需要另一次裁决（本候选不引入分布式调度）。
- 真实 Exa / Tavily / DeepSeek / System One 的网络形状与本轮修复无关，仍需真实联调。

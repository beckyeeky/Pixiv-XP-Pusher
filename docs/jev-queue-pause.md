# 候选：批量队列占位修复与未解决自动暂停（jev-queue-pause）

状态：**候选实现，未部署、未 push、未发布**。基线为生产 `52e1e759`，本候选只交付工作区 diff 与证据。

第二轮收敛（子任务 `jev-queue-fixes`，独立审查 `final-independent.log` 的 B1/B2 反例）只改队列/暂停契约本身，见 §3–§6 与 §7.1；未改 schema、画像、评分与检索门控。

## 1. 要解决的问题（主代理已核实的根因）

1. 手动批量入口 `ClassificationMaintenance.preview()` 先按权重 `LIMIT` 取候选，之后才在 `native_jev_judge.evaluate()` 里检查 `tag:` 冷却。冷却中的 `hold` 标签因此占满名额，批次实际处理数远低于 `limit`（饥饿）。
2. `hold` 只写 24h `tag:` 冷却，冷却到期后同一个标签会再次被自动选中、再次消耗完整检索 + 模型请求，形成“每 24 小时重新判定一次且仍无结论”的循环；没有任何“已经尝试过、暂时别自动碰”的状态。

**不改**的部分（本轮范围外）：画像评分/权重、`non_preference` 语义、旧 IP/alias/gallery、搜索路由双源门控（`min_confidence > 0.9`）。`database.py` 在第二轮只增加了队列分页参数（`offset`）与只读访问器的错误类型，**无 schema 变更、无迁移、不写画像**。

## 2. pause 与 cooldown 的语义（两者同时存在，含义不同）

| 名称 | 位置 | 何时写入 | 何时消失 | 作用 |
| --- | --- | --- | --- | --- |
| `cooldown` | `state["cooldowns"]["tag:<tag>"]`（既有） | 每次 `hold`，`unresolved_cooldown_seconds`（默认 86400s） | 自动到期（且 `_prune_unlocked` 会删除过期条目） | 短退避：短时间内不要重复花费同一标签的请求 |
| `pause` | `state["pauses"][<tag>]`（新增，无 `until`） | **仅** `NativeJevJudge.hold()` 判定成立时 | 只有单标签显式手动重试 / 人工决定 / 迁移脚本不会清除 | 持久记录“该标签已完成一次完整分类尝试但没有结论”，自动批量从此跳过 |

pause 条目形状（唯一允许持久化的字段）：

```json
{
  "reason": "insufficient_agreement",
  "source": "native_hold",
  "since": 1761400000.0,
  "last_attempt_at": 1761400000.0,
  "attempts": 1
}
```

- `reason` ∈ `HOLD_REASONS` = `low_relevance`、`no_evidence`、`model_unresolved`、`insufficient_agreement`、`conflicting_classification`；其它 reason（`budget_exhausted`、`jev_rate_limited`、`search_unavailable`、`engine_cooldown`、`task_lock_timeout`、`internal_error`、`insufficient_engines_configured` 等）写入被拒绝，`pause()` 返回 `None` 且不落盘。
- `source`：`native_hold`（正常判定）或 `legacy_cooldown_import`（迁移，见 §5）。
- 不含凭据、不含上游错误文本、不含 URL。

## 3. 队列行为

- `_select_available()`：先读一次磁盘状态得到 `paused ∪ cooling`，把它们在整页里过滤掉再计数，不足时**继续翻页**（`_QUEUE_PAGE_SIZE = 200` 一页，`LIMIT/OFFSET` 走数据库自身的 `ABS(weight) DESC, tag ASC`）直到补足 `limit` 或**画像库读完**为止。**没有扫描上限**：过去 `_SCAN_CAP = 1000` 会在暂停条目超过 1000 条时永久饥饿（审查反例：1000 个高权重暂停条目挡住其下 5 条可用），该常数已删除；每页仍然有限，循环一定会推进。
- 分页接口：`database.get_high_weight_unclassified_profile_tags(..., offset=)`（异步、写入路径）与 `database.get_high_weight_unclassified_profile_tags_readonly(..., offset=)`（`mode=ro`、只读路径）共享同一 ORDER BY，因此两条入口逐页选出同一批。
- 入口统一：`preview()`、`run_eligible()`、`run_profile()`（`_select_profile_tags_detailed`）、`scripts/maintain_high_weight_tags.py` 的候选清单、`scripts/preview_native_jev.py` 的队列候选、Web `POST /api/tag-reviews/ai-process-all`、Telegram 批量流程都走同一套排除。`preview_native_jev._select_tags()` 不再自己写扫描循环，直接调用 `ClassificationMaintenance.available_candidates()`，两个只读预览不可能各自扫描、各自分页。
- 数量不足时只返回可用项；返回体带 `skipped_paused` / `skipped_cooling` 计数（仍只统计本次调用实际检查过的行），Web 与 Telegram 只在非零时提示。
- 队列选择**只读**：`read_native_state_store()` 每次新建实例读取，不创建状态文件、不写 pause、不 resume（`test_preview_never_resumes_or_writes_pause_state`、`test_preview_does_not_create_a_missing_state_file`）。
- **不建库**：`preview()` 与 `available_candidates()`（维护脚本/预览 CLI）都走 `mode=ro`。文件缺失时抛 `DatabaseNotInitializedError`（`sqlite3.OperationalError` 子类，消息含 `no such table: xp_profile`），schema 不匹配抛 `DatabaseSchemaMismatchError`；两者都**在创建任何文件之前中止**。维护脚本的候选清单不再 `init_db`，缺库时输出空清单并在 stderr 说明。`--apply`（唯一写入路径）仍在真正写入前 `init_db`。
- **执行边界复核**：队列只是快照。`NativeJevJudge.evaluate()` 在持有单一 task 锁并刷新快照之后**再次**检查 `pauses` 与 `tag:` 冷却；命中就返回 `paused`/`cooldown` 且不发任何请求。因此 `run_tags()`、`run_reviewed()`（外层 preview 与执行之间的竞态）以及预览 CLI 的默认模式都不会为已暂停标签花钱。

## 4. 手动重试（唯一恢复路径）

- 审核列表 `GET /api/tag-reviews` 对暂停行附加 `automatic_pause`（reason/source/attempts），模板显示“⏸ 自动暂停：… 自动批量跳过；点「🤖 重新尝试」只重试这一个标签”。列表本身不隐藏暂停标签——人工仍可看到并直接人工判定。
- `POST /api/tag-reviews/{tag}/classify` → `ClassificationMaintenance.classify_tag()`：在已审 task 锁内先 `refresh_native_runtime_snapshot()`，读一次该标签的 pause；若存在，则签发一个**一次性 bypass token**（`native_jev_judge.PauseRetryToken`，放在 contextvar 里，只对该 task/该标签有效、用掉即废）并执行**一次**分类。
  - 不再“先 `resume()` 再分类”。旧写法一旦分类遇到 503/异常/取消，pause 已被永久删除，标签会被下一次自动批量重新花钱；这是审查 B1 反例。
  - 只有 `status == "accepted"` 时才在锁内 `resume(tag)` + `clear_cooldown("tag:<tag>")`（仅该标签），响应带 `resumed_pause`。
  - `GroundedJudgeDeferredError`（额度/冷却/503/锁）、`ValueError`（再次 hold）、`asyncio.CancelledError` 与其它异常都**保留原 pause 的 `since`/`last_attempt_at`/`attempts`**；再次 hold 时 `attempts` 加一，`since` 不变；网络失败（defer）不增加 `attempts`。
- token 只允许一次执行：同一个 token 第二次进入 `evaluate()` 时已被消费，重新回到 `paused`。因此单标签重试不会变成批量解冻。
- 批量入口不会静默恢复：`run_reviewed()` 对 preview 时已暂停的标签抛 `no longer eligible`；若 pause 是在 preview 之后、批次拿锁之前才写入的（真实 TOCTOU），执行边界复核会把该标签记为 `deferred` 且不发请求。Telegram 快照确认、`maintain_high_weight_tags --apply` 同样按 `preview/select_candidates` 复核。
- 批量后如果再次 `hold`，会重新写入 pause（`attempts` 递增，`since` 保留）。

## 5. 既有暂停项迁移（保守、只导可证实的 hold）

`NativeJevStateStore.import_legacy_holds(dry_run=True)`：

- 只接受 `cooldowns` 中 **key 为 `tag:<name>`** 且 **reason ∈ `HOLD_REASONS`** 的条目——这是旧版本唯一留下的“已完成尝试但无结论”的证据；`engine:*` 冷却、`jev_rate_limited` 等一律不导入。
- 冷却是否已过期不影响导入（hold 本身与时间无关）；已存在 pause 的标签不覆盖、不递增。
- **不会**把任何 `tag_classification_cache.classification = 'unresolved'` 的历史行当作“已尝试”——数千条历史未分类继续按原样留在候选与审核队列。
- CLI：

```bash
python3 scripts/maintain_high_weight_tags.py --import-legacy-pauses              # 只列清单
python3 scripts/maintain_high_weight_tags.py --import-legacy-pauses --confirm-import   # 写入
```

写入前取同一条 native task 锁并 `reload()`，与批次共享同一互斥；没有运行中的 native runtime 时用文件实例（CLI 进程内本就无缓存快照）。

**部署顺序（强制）**：升级到本候选后，必须在**第一次普通写入之前**执行一次
`python3 scripts/maintain_high_weight_tags.py --import-legacy-pauses --confirm-import`。
原因：旧证据只存在于 `cooldowns["tag:<x>"]` 里，而任何一次普通写入都会触达 `_prune_unlocked()`。
本候选已让 `_prune_unlocked()` **保留**“reason ∈ HOLD_REASONS 且该标签尚无 pause”的过期 `tag:` 冷却条目，
所以普通写入不再抹掉可证实的证据；一旦该标签有了 pause，对应的冷却条目才会被丢弃（证据已落地）。
这条保留规则只影响“证据是否还在”，不影响行为：`is_cooling()`/`cooling_tags()` 仍按 `until` 报告过期。

**已知无法补救的证据**：如果旧版本已经把这些 `tag:` 冷却条目 prune 掉（例如升级前的最后 24h 之后又跑过写入），
磁盘上就不存在任何“尝试过但没结论”的痕迹。这类历史 `unresolved` 行**不会被推断成 pause**，也没有脚本会
凭空补造 attempt 证据；它们继续按普通候选参与队列。导入只在显式 `--import-legacy-pauses --confirm-import`
动作里发生，`preview`/`preview_native_jev` 等普通只读入口从不写入。

## 6. 与 task 锁 / 快照的关系（不新增竞态）

- pause 的写入方是 `NativeJevJudge.hold()`，它写的就是 runtime 自己的 `NativeJevStateStore`，与既有的 `cool()` 同一临界区（`threading.Lock` + 原子 `replace`），因此不会被 runtime 的整份快照回写覆盖。
- 所有写入口（`classify_tag`、`import_legacy_holds`）都在 `native_task_lock_for_config()` 已持有后先 `refresh_native_runtime_snapshot()` / `reload()`，沿用已审的“持锁 + 刷新”顺序。
- 队列选择只读磁盘、持锁外执行，不写任何文件。
- 一次性 bypass token 只在 contextvar 里存在，且只在单标签入口**持有 task 锁期间**有效：并发批次拿不到锁（会 `deferred`），因此不可能继承 token；`run_tags()` 本身从不签发 token。

## 7. 验证证据（本次实际执行）

| 证据 | 位置 | 结果 |
| --- | --- | --- |
| 新增 state+SQLite 入口测试（先红） | `artifacts/jev-queue-pause/red-baseline.log` | 基线 23 failed / 2 passed（2 条为“必须保持不变”的守护用例） |
| 同上（后绿） | `artifacts/jev-queue-pause/green-local.log` | 35 passed |
| 界面/CLI 入口测试（先红） | `artifacts/jev-queue-pause/red-surfaces.log` | 基线 10 failed |
| 全量本地套件（venv 含 fastapi） | `/tmp/before.txt` vs `/tmp/after.txt` | 基线 15 failed / 665 passed；候选 15 failed / 700 passed，失败集合逐条相同（既有失败，与本候选无关） |
| 独立主机隔离回归（`/opt/pixiv-jev-regression/queue-pause`，临时 DB + mock 网络，`nice` 单进程，python 3.11） | `artifacts/jev-queue-pause/green-remote.log` | 21 项全 PASS |
| 同一脚本对基线源码 | `artifacts/jev-queue-pause/red-remote.log` | `AttributeError: 'NativeJevStateStore' object has no attribute 'pause'`（基线无此能力） |
| 候选 diff | `artifacts/jev-queue-pause/candidate.diff` | 9 个文件，`database.py` 无改动 |

覆盖点：高权重前 10 条暂停后取到第 11–15 条；不足时只返回可用项；跨重启仍暂停；冷却/决策缓存 TTL 到期仍跳过；网络失败/额度失败/lock 不 pause；单标签手动可试且只恢复该标签；批量 preview 不恢复 pause 且不改状态文件；从未处理过的历史 unresolved 仍可选；既有 manual/accepted 行不被改写；迁移只导入可证实 hold；暂停状态不含凭据。

### 7.1 第二轮收敛（jev-queue-fixes）

| 证据 | 位置 | 结果 |
| --- | --- | --- |
| 审查方独立脚本（只读副本，`final-independent.log` 的 10 failed 用例）对**改动前**候选 | `jev-queue-fixes/red-reviewer.log` | 10 failed / 4 passed |
| 同上，对**改动后**候选 | `jev-queue-fixes/green-reviewer.log` | 14 passed |
| 新增回归 `tests/test_queue_pause_hardening.py`（先红） | `jev-queue-fixes/red-candidate-tests.log` | 14 failed / 3 passed |
| 同上（后绿） | 本地 `pytest tests/test_queue_pause_hardening.py` | 17 passed |
| 相关旧套件（queue_pause + surfaces + classification_maintenance + maintain） | `jev-queue-fixes/green-queue-subset.log` | 70 passed（改动前同集 53 passed） |
| 全量 `pytest tests`（本机无 fastapi，跳过 3 个 web 模块） | `jev-queue-fixes/green-full-suite.log` | 662 passed / 4 skipped / 11 failed（11 条与 `artifacts/native-jev/baseline-env-failures.log` 的既有环境失败逐条相同） |
| `tests/run_offline.py`（无 pytest 的离线 runner） | 本地执行 | 146 tests OK（6 skipped） |
| 改动前重建树校验（生产基线 + `candidate.diff`） | `jev-queue-fixes/pre-tree` | 复现审查脚本 10 failed、新测试 14 failed，证明红基线就是改动前代码 |

覆盖点（按审查 10 failed 分类）：B1 冷暖 runtime 下 503 保留原 pause；取消保留原 pause；`run_tags` 绕 pause（冷却到期后仍不发请求）；`run_reviewed` 的 preview→执行 TOCTOU 被锁内复核拦下；`attempts` 累计且 `since` 保留、网络失败不累计；1000 条暂停后 5 条可用、200 条暂停时两个只读入口逐条一致；近期 `unresolved` 在自动 profile 中仍可选而明确分类仍被排除；两个预览缺库都不建文件；一次性 token 用尽后再次批量不发请求；成功分类只清该标签的 pause。

## 8. 未覆盖边界（供主代理裁决）

1. **pause 不自动恢复**是刻意设计：如果某个标签的证据环境改善，只能靠人工单标签重试或人工判定解除。是否需要“一键恢复全部暂停/按原因批量恢复”目前**未实现**（本轮也未扩大 resume 到整批）。
2. 队列扫描**没有上限**，但代价随“暂停条目下方的可用行距离”线性增长：最坏情况会把画像库读到尾（每页 200 行）。真实生产库规模下的耗时未测量。
3. 分页用 `LIMIT/OFFSET` + 固定 `ORDER BY`。扫描期间若有并发写入改变 `xp_profile` 权重或分类，同一批的两页之间可能漏读/重读个别行（`preview` 期间并发导入画像属此类）。改为 keyset 分页可以消除该窗口，但会改动 SQL 形状，本轮未做。
4. 排除仍在 Python 侧做（而不是 SQL `NOT IN`），因为 `skipped_paused`/`skipped_cooling` 的语义是“本次实际检查过多少行被跳过”，SQL 侧排除会让计数恒为 0。代价是暂停条目本身也要被读出来（只读、无网络）。
5. `preview_native_jev --include-paused` 是**付费**诊断：它会为被暂停的标签真的发起检索+模型请求（`test_preview_include_paused_is_paid_isolated_diagnostic` 用合成 transport 断言 4 次请求）。它只读隔离的 `.preview` 状态文件，不写生产状态、不 resume，也不是“批量恢复”入口。
6. 没有引入新的配置开关（没有 `pause_on_hold: false` 之类的退路）；如需灰度关闭需追加设计。
7. Telegram 的候选快照确认流程仍以 `run_reviewed()` 复核资格，未单独新增“查看暂停原因”的独立菜单（暂停原因目前只在 Web 审核列表展示）。
8. 迁移脚本按文档只做 dry-run/显式确认两步，尚未在生产状态文件上实际执行；生产 `52e1e759` 的 `data/native_jev_state.json` 未读取。**部署顺序要求见 §5**：首次普通写入前必须先 `--confirm-import`。
9. 三处旧断言与本轮契约冲突、已按新契约改写（不是删除）：`tests/test_native_jev_judge.py::test_no_evidence_holds_the_tag_and_starts_a_cooldown`（第二次调用的状态由 `cooldown` 变为 `paused`）、`...::test_cooldown_expires_and_the_tag_is_retried`（改名为 `test_cooldown_expiry_alone_never_retries_a_paused_tag`，并补上“只有显式 token 才重试一次”）、`tests/test_phase2_safety.py::test_permanent_confirmed_and_unresolved_cooldown`（过期 `unresolved` 行改为可选）。三处都在测试内写明契约变更原因。
10. 三个 web/Telegram 表面测试（`test_ui_fixes_round2`、`test_surface_backend_parity`、`test_telegram_delivery`）打桩的是旧的异步队列查询；`preview()` 改走 `mode=ro` 后同步补了只读访问器的 stub（断言参数形状不变）。
11. 远端隔离回归（`/opt/pixiv-jev-regression/queue-fixes`）本轮为可选动作，实际是否执行与结果见 §7.1 与主代理报告；本机未安装任何新包。

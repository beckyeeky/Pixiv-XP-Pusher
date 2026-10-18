# 历史种子轮换

状态：本地候选实现（未提交、未部署）。基线 `d93d67a`，分支 `feature/historical-seed-rotation`。
范围：仅替换定时 `related` 策略的种子选择；连锁反馈 `push_chain`、XP/画师打分、阈值和全局策略份额不变。

## 1. 目标

旧实现每轮从 `feedback like` 中随机取一个种子，无冷却、无租约、无持久预算，清理缓存后可能失去长期收藏种子。本实现改为从长期历史正反馈池中按冷却轮换，并用数据库租约约束并发。

## 2. 种子池与排除

候选池为以下来源的并集，去重且仅接受正整数作品 ID：

- `preference_bookmarks`：包括 `imported=1` 的历史收藏。
- `xp_bookmarks`：收藏分析缓存。
- `feedback`：当前账本中 `action='like'`。
- `preference_feedback`：持久账本中 `action='like'`。

排除规则：

1. 任一账本存在明确 `dislike` 的作品优先排除，即使同时存在 like。
2. 已知作者被屏蔽时排除。作者仅取 `feedback_metadata.user_id`、`illust_cache.user_id`、`preference_feedback.artist_id`；`bookmarks` / `xp_bookmarks.user_id` 是收藏者，绝不当作画师。
3. 无任何作者元数据视为“未知”，不推断为屏蔽，仍可作为种子。
4. 当前处于成功/失败冷却，或租约仍有效的种子不参与本轮。

历史收藏被清理、`bookmarks` 扫描表为空并不影响池；池只剩持久账本记录时仍可轮换。

## 3. 每轮预算与调用

- 每轮最多 2 个种子，每个种子最多 1 次 `get_related_illusts`。
- 定时种子显式调用 `get_related_illusts(..., single_page=True, no_retry=True)`：
  - `single_page=True` 固定只读第一页，即使响应带 `next_url` 也不再请求；不再受 `limit // 30 + 1` 分页深度影响。
  - `no_retry=True` 走单次传输预算：既绕过项目层 `retry_async(max_retries=3)`，也绕过 `pixivpy_async` 1.2.14 在 `Net.fetch` 上叠加的 `retry(..., retries=10)`。实现方式是对 SDK API 做一次浅拷贝，把该拷贝的 `fetch` 绑定到 `functools.wraps` 暴露的未装饰协程（`Net.fetch.__wrapped__`），再调用 SDK 自己的 `illust_related`：URL/查询参数、App 头与 Bearer 认证头、`additional_headers`、SDK session、`conn_opt`（proxy/env/bypass）与 `requests_kwargs`（timeout）仍由 SDK 构造，只跳过重试。
  - 共享的 API 实例不被修改，也不做全局 monkeypatch；并发的默认连锁调用仍保留 SDK 与项目两层重试。
  - 因而每个种子只产生 **1 次物理 HTTP GET**（`session.get`），与 `limit` 大小无关；一次读超时也只对应 1 次已发出的请求。
- `login()` 的 token 刷新是独立调用：仅当 token 超过 30 分钟时才发生，不计入上述“每 seed 1 次 related 请求”预算；它可能额外产生认证网络请求（`POST /auth/token`）。
- 默认调用（不传新参数）严格保持基线行为：`retry_async(max_retries=3)`、原分页公式；连锁 `push_chain` 仍调用 `get_related_illusts(seed.id, limit=20)`。遇到 `{"error": ...}` 错误体时只停止继续分页并返回已获取的部分/空列表，不抛出 `PixivAPIError`，因此不会触发重试放大。
- 只有 `no_retry=True` 的严格路径把 `{"error": ...}` 抛为 `PixivAPIError`，供策略区分 `throttled` 与 `empty`。`PixivAPIError.status` 从错误体尽力提取；`pixivpy_async` 不保留 HTTP 状态，实际 429 识别还依赖错误体中的 `status` 字段或 `message`（如 `Rate Limit`）。
- 目标 `limit <= 0` 直接返回 `[]`，不领取种子、不请求。
- 第一个种子的可用候选已达到 `limit` 时立即停止，不领取/不调用第二个种子。
- 未使用的第二个租约以 `unused` 释放，不产生冷却，也不算一次尝试。
- 所有种子的候选跨轮去重，并排除本轮全部种子自身（包括第一个种子的关联结果中出现的第二个种子）。
- 候选仍按原 XP 权重 + `artist_profile` 分数排序，保留 `bookmark_threshold.related` 阈值。
- 全局策略配额仍由调用方（fetcher/MAB）决定，本模块不改变份额。

## 4. 租约与冷却

状态表 `related_seed_state` 为独立新增表，时间全部为 UTC epoch 秒：

| 字段 | 含义 |
| --- | --- |
| `illust_id` | 种子作品 ID（主键） |
| `state` | 最近一次结算结果：`success` / `empty` / `error` / `throttled` |
| `cooldown_until` | 冷却截止时间；到期前不可领取 |
| `failure_count` | 连续 empty/error 次数，成功时归零 |
| `last_attempt_at` | 最近结算时间，用于公平轮换排序 |
| `claim_token` | 唯一租约标识（UUID hex） |
| `claim_until` | 租约过期时间 |
| `updated_at` | 状态写入时间 |

领取使用 `BEGIN IMMEDIATE`，在同一事务内完成“筛选 + 写入租约”。租约默认 600 秒；过期后其他进程可重新领取并生成新 token。

`finish_related_seed` 的 `UPDATE` 带有 `illust_id + claim_token` 条件：

- token 不匹配（租约已被新请求接管）时返回 `False`，不写冷却、不覆盖新租约。
- 成功结算前若发现租约已失效，当前工作器丢弃本轮候选，避免重复处理。
- `success`：候选获取成功（不是投递成功），冷却 7 天，失败计数归零。
- `empty` / `error`：从 6 小时起指数退避（6h、12h、24h、48h、96h、168h），封顶 7 天。
- `throttled`（HTTP 429 / Too Many Requests）：短冷却 1 小时，并立即停止当轮，不再尝试第二个种子。
- `unused`：仅释放租约，不写冷却。
- 数据库领取失败（锁冲突、缺表等）：记录告警并返回 `[]`；不回退旧的随机选种路径，因为回退会绕过冷却。

同一假设不重试：任何异常在本轮最多结算一次；失败租约由冷却或过期回收，不在循环内立即重领。

## 5. 迁移与兼容

- 表通过 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` 在 `init_db` 中增量创建。
- 不重建、不清空、不改写既有业务表；重复初始化保持原数据。
- `SCHEMA_VERSION` 保持 9。该变更只新增独立表，不改变既有表结构或语义；不通过单纯改版本号跳过迁移。
- 旧库无此表时，首次 `init_db` 补建；运行中若尚未初始化，领取会安全失败并告警。
- 未新增配置项。预算和冷却目前是 `database.py` 内部常量；即使内部调用传入负数或超大值，领取预算也会被夹紧到 0..2。

## 6. 隐私与日志

- 新日志只记录数量、结果类别和错误类别，不输出画师名、偏好标签或收藏者信息。
- 不记录候选的完整偏好明细。
- 本文档不包含真实 IP、账号、作品 ID 或生产数据。

## 7. 测试

命令（TZ=UTC，临时 SQLite，无排除）：

```
TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q tests/test_historical_seed_rotation.py
TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q tests/test_related_recommender.py
TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q tests/test_related_request_budget.py
```

新增测试覆盖：

- 清理扫描缓存后持久收藏/XP 收藏仍可选。
- 两个账本中的 dislike 均优先排除。
- 已知屏蔽作者排除；未知作者保留；收藏者 ID 不被当作画师。
- 成功冷却 7 天且池耗尽返回空，不软化冷却。
- 并发领取得到不相交租约。
- 过期 finish 不覆盖新租约。
- empty/error 6h 起指数退避并封顶 7 天；成功重置计数。
- 429 只调用一次并进入 1 小时短冷却。
- 每轮最多 2 次调用；首种子满额早停；无额外 client 方法。
- 真实 pixivpy_async 1.2.14 + 假 session（仅替换 `session.get/post` 与 `ClientManager`，走真实 `Net.fetch`/`auth` 与真实 `illust_related`）：
  - 首请求读超时只发 1 次 `session.get`，种子记为 `error`（非 `throttled`），代理与 `requests_kwargs` 仍来自 SDK。
  - 429 错误体（`status=429`）只发 1 次请求、种子 `throttled`、第二个种子不请求且无冷却。
  - 响应带 `next_url` 时定时路径只读 1 页。
  - 默认调用遇错误体只发 1 次并返回 `[]`；分页中途错误时保留第一页部分结果、不抛异常。
  - 默认调用遇传输异常仍会重试（证明未全局禁用重试）。
  - token 刷新产生独立的 1 次认证 `POST`，与 1 次 related `GET` 分开计数，且新 Bearer 头被复用。
- FakeAPI 级测试保留为接口契约测试（分页早停、429 停止第二个种子、默认连锁参数 `(seed_id, limit=20)`）。
- 跨 seed 去重并排除本轮全部种子。
- 零/负 limit 不发请求。
- 领取数据库失败返回空且不回退随机路径。
- 旧库无新表时初始化补建且不改变既有数据；重复初始化幂等。
- 原 XP + 画师打分与阈值排序保持。

## 8. 未验证边界

- 未连接真实 Pixiv、Telegram、生产数据库或业务网络；429 使用离线错误体模拟。
- 定时种子调用 `single_page=True, no_retry=True`，在 `session.get` 层实测只产生 1 次物理请求；通用 `retry_async` 装饰器与 SDK `Net.fetch` 装饰器均未修改，默认调用仍重试。
- 单次传输依赖 `pixivpy_async` 1.2.14 的 `Net.fetch.__wrapped__`。若升级后该属性消失，严格路径会抛出 `RuntimeError`（种子记为 `error`）而不是静默退回重试；需先复核 SDK 的 `fetch` 装饰结构再放行升级。
- 测试替身（无 SDK 传输面的 fake API）会退回 `api.illust_related`，因此不能证明其单次传输；单次传输只由真实 SDK + 假 session 的测试覆盖。
- `pixivpy_async` 的 `Net.fetch` 只返回 JSON 体并丢弃 HTTP 状态码；`PixivAPIError.status` 只能从错误体尽力提取。离线测试覆盖错误体 `status=429` 与 `message="Rate Limit"` 两种形态，未验证真实 Pixiv 429 响应体。若真实 429 体既无状态字段也无 Rate Limit 文本，轮换会按普通 `error` 退避而不是 `throttled` 短冷却。
- token 刷新（`login()`）可能产生独立认证请求，未计入“每 seed 1 次 related 请求”预算；认证请求本身仍走 SDK 的 10 次重试，未纳入单次预算。
- 未验证真实长耗时请求超过 600 秒租约时的外部行为；代码按“丢弃旧候选、由新租约接管”处理。
- 未提交、未部署；独立审查与发布由主控执行。

## 最终验收边界

主代理完整回归：495 passed、38 subtests passed，无失败、无排除。独立复核确认单次传输及默认连锁兼容修复通过；旧候选反例的预期失败不代表当前候选失败。

依赖限定为 `pixivpy-async==1.2.14`，因为单次传输复用了此版本 SDK 的未装饰 fetch；升级必须重新验证该接口。认证刷新不计入作品相关请求预算。真实网络限流及服务器运行环境尚未验证；陌生错误体如丢失状态，可能按普通失败退避，不能承诺识别所有429。

当前仅候选验收，不代表提交、发布或生产部署已完成。

# 画像复核修复记录：B1、B2

## 范围与基线

仅修改 `persistent_profile.py`、`tests/test_persistent_profile.py`，新增本文档。不修改 database/config/task_manager/分类渠道，不提交、不推送，不使用默认数据库或外部业务网络。

首次执行 `git diff -- persistent_profile.py tests/test_persistent_profile.py` 无输出；后续 `git status --short` 确认两文件原本属于未跟踪工作文件，因此不能将空 diff 解释为文件无既有实现。读取后以局部替换保留原有实现与测试。

## B1：新兴趣平均值

`persistent_profile.py:_project` 在扣除旧 base 之前，从未过滤掉的观察权重计算 average。旧 base 的倍率仍由原有 preference_anchor 比率机制单独处理，不重复 boost。

真实 SQLite 反例：迁移 existing=8，先 build，再以 new_interest=2 build，修复前得到 new_interest=0，修复后为 8。另验证 existing 倍率改为 3 时 existing=24，而新兴趣仍按原始观察平均值产生 8。

## B2：原始观察与精确撤销分离

新增事务内惰性创建的 `preference_feedback_observations(illust_id, observations)`，保存新反馈实际观察到的 raw 标签及正向投影增量。`preference_feedback.contributions` 保留精确撤销账项，不再被当作新反馈的唯一原始观察来源。

投影按当前 equivalent resolver，对同一作品的原始观察去重；删除 alias 后可恢复 raw 身份。反馈写入时按当前规范身份分组，每组只记一次负向罚分与 blacklist 计数，但精确账项键采用该组实际观察到的第一个 raw 标签；撤销始终使用记录的原键和数值。对外 disliked_tags 仍返回 canonical 标签。

受控 reset 同时清空新观察表，防止重放已重置的反馈。新表创建及写入与反馈共用 SQLite 事务，失败完整回滚。

旧 v1 反馈未保存 raw 标签，无法可靠反推；没有新观察行时仍读取旧 contributions，不伪造历史身份。

## 验证命令与结果

均在 `/var/minis/workspace/xp-pusher-review-20261016` 执行，数据库由 pytest tmp_path 创建。

1. 红灯：`/tmp/xp-profile-venv/bin/python -m pytest -q tests/test_persistent_profile.py -k review`
   - `/tmp/xp-profile-review-red.log`：3 failed, 2 passed, 18 deselected。
   - 失败分别确认新兴趣 0≠8、删除 alias 后 canonical 未恢复 raw、负向身份仍 canonical。
2. 曾误用 `tests/test_profile_feedback.py`：pytest 返回 file or directory not found，无测试执行；日志随后由正式运行覆盖。实际文件为 `tests/test_profiler_feedback.py`。
3. 首轮正式回归：26 passed, 1 failed。并行 database 变更产生 retention-v1 与 preference-v1 两份备份，原测试将所有 sqlite3 备份统统计数。
   - 仅将画像备份数量与幂等断言限定到 `*.preference-v1.*.sqlite3`，仍验证画像备份恰好一份、内容与 integrity_check；没有删除或跳过断言。
4. 最终：`/tmp/xp-profile-venv/bin/python -m pytest -q tests/test_persistent_profile.py tests/test_profiler_feedback.py`
   - `/tmp/xp-profile-review-green.log`：29 passed, 55 warnings in 18.25s。
   - warnings 为 Python 3.12 SQLite datetime adapter 弃用警告。

新增覆盖：B1、like 后删除 alias、like→dislike→like 跨 alias 精确撤销与 artist/blacklist 归零、同作品 raw/canonical 同义词去重、既有 raw 观察动态合并再拆分、观察表 DDL 与反馈失败共同回滚。

## 限制与待主代理裁决

- R1 首次 IP 文件与 R2 模式闸门已在下节补齐；其余历史限制仍适用。
- 不追溯修复缺失 raw 元数据的旧反馈。
- 负向账项在反馈写入时按当时等价关系去重；未扩展到重算既存负向账项或动态改变历史罚分。
- 别名合并后，同作品不同 raw 观察若保存了不同罚分，正向投影使用原始观察顺序中首个身份的保存增量；不根据当前权重重算历史罚分。
- 工作树有其他代理并行修改；未运行全仓库测试，也未验证生产库或宿主端行为。主代理需裁决并行合并后的全量回归及备份测试口径。
- 未跟踪文件不出现在普通 git diff 中，不能用本次空 diff/check 输出证明完整补丁边界。


## R1：首次倍率初始化输入闸门

`profiler.py:XPProfiler.__init__` 记录文件缺失、JSON 解析错误和非字符串列表格式错误，不再只输出日志而丢弃读取状态。`_profile_preflight` 在 build 的黑名单加载、收藏同步、游标写入之前执行；反馈也在写入之前执行。首次持久操作且 `ip_weight_discount != 1` 时，文件错误阻止继续，并明确提示修复文件（允许有效空列表 `[]`）或显式设置 `ip_weight_discount=1`。

兼容政策（最终审查修订）：未指定文件时继续使用原有内置列表；显式指定默认路径 `data/ip_tags.json` 而文件缺失，适用相同闸门及逃生提示，不要求仓库必须自带该文件。所有 persistent build/feedback（含已使用或 reset 后）在文件错误且折扣不等于 1 时拒绝；仅 legacy 保留原有文件错误降级行为。显式关闭折扣或提供有效空列表文件可继续运行。文件读取故障不是一次配置更改。

## R2：持久模式使用后禁止退回 legacy 写路径

`persistent_profile.py:persistent_used/preflight` 使用 SQLite 只读连接检查，不因初始化空画像而锁定。build/feedback 事务通过 `_mark_used` 原子创建并写入 `preference_usage`，失败随事务回滚。reset 不删除该标记，因此空库画像不能伪装成从未使用持久模式。

兼容当前已构建 v1：`preference_anchor` 或 `preference_feedback_observations` 表存在（含空表）即视为曾使用；无表时，非 imported 收藏账项或 reversible 反馈也是使用证据。迁移标记、导入的 base/历史反馈本身不锁定 legacy。无标记的旧 reset 与真实 build 可能无法区分，已有 anchor/observations 表时保守拒绝，不冒险猜测。拒绝信息要求恢复升级前备份再运行 legacy，明确不是无损往返承诺。未使用持久模式的新库、真实迁移旧库仍可 legacy build/feedback。

## R1/R2 验证与边界

在本工作树、分支 `refactor/persistent-profile-low-quota` 执行：

- 先红：`/tmp/xp-profile-venv/bin/python -m pytest -q tests/test_persistent_profile.py -k 'r1 or r2'`，日志 `/tmp/xp-profile-r12-red.log`：8 failed, 25 deselected。实际 SQLite 反例覆盖坏文件及 build/feedback/reset 后旧模式写入。
- 后绿：`/tmp/xp-profile-venv/bin/python -m pytest -q tests/test_persistent_profile.py tests/test_profiler_feedback.py`，日志 `/tmp/xp-profile-r12-green.log`：48 passed, 103 warnings in 7.76s。原有 29 项保留，新增 19 项；未删断言或跳过失败。
- `git diff --check -- profiler.py persistent_profile.py tests/test_persistent_profile.py docs/profile-review-fixes.md` 无输出。
- 新增验证包含：有效空列表、关闭折扣、缺失默认路径可操作提示、非首次/legacy 兼容、init 不锁定、真实旧库迁移不锁定、旧 v1 空表证据、reset 后拒绝及事务失败标记回滚。被拒绝入口检查 client 无调用。网络替身保留 get_bookmarks 异步返回列表接口，不验证真实 Pixiv/宿主行为。

本轮仅写入指定四个文件，保留 B1/B2；未操作默认数据库，未业务联网、提交或推送。warnings 为 Python 3.12 SQLite datetime adapter 弃用警告。未进行全仓库、生产库或宿主端回归。预检与之后的旧模式操作不共用一个数据库事务，不承诺跨进程同时切换模式的串行化；部署应避免同时运行两种模式，若需覆盖该并发场景需主代理裁决扩展锁定/API 范围。直接调用数据库手工修正 API 仍按原有 correction 语义，不属于 profile_mode=legacy 入口；本修复不撤销已经发生的历史污染，也不恢复丢失的原始元数据。


## 最终审查：IP 故障、完整手工替换与测试数据库隔离

证据来源：`/tmp/xp-independent-review-20261016-154748/reviewer_migrated_probe.py`、`/tmp/xp-independent-migrated-probe.log`。旧实现确认迁移 series=8 在文件丢失后变为 16，以及首次 build 前 manual=7 被 boost seed 加到 8。

### 修复语义

- `persistent_profile.preflight` 不再以 `not used` 限制 IP 输入闸门。persistent 首次、重复、反馈及 reset 后的操作统一写前拒绝；legacy 降级不变。`XPProfiler._profile_preflight` 每次重新读取显式文件，覆盖同一实例长期复用及文件恢复；失败保留上次有效集合，不以空列表替代。
- `preference_boost_seed(tag,factor,weight,frozen)` 在投影事务内惰性创建，保存合成种子基线。完整手工替换冻结已存在的种子基线，使手工 correction 对种子的抵消不因下轮平均值变化而失效；首次投影遇到已有 manual 身份时，种子基线从零开始。冻结基线即便移除 boost 配置仍存在于 correction 核算中，避免删键后产生负数残影。
- 合成种子状态和 manual 键按当前 equivalent resolver 处理；手工完整替换输入也规范化并合并别名。真实新反馈仍作为独立观察计入，显式改变 boost 倍率解除该倍率下的冻结；新增 boost 标签仍按原 B1 平均值生成兴趣。reset 同事务清空种子状态。
- `tests/test_profiler_feedback.py` 为每个 unittest 创建临时目录中的真实 SQLite schema，并 patch `database.DB_PATH`。安全闸门仍真实执行，不 Mock preflight，不访问默认路径。

### 本轮命令与结果

工作目录 `/var/minis/workspace/xp-pusher-review-20261016`，使用系统 `python`，TZ=Asia/Shanghai。

1. 先红：`python -m pytest -q tests/test_persistent_profile.py -k 'final or compatible_paths'`，日志 `/tmp/xp-final-red.log`：7 failed、3 passed、40 deselected。失败包含更新后的 already_used 政策断言、迁移及 reset 后 build/feedback 四项、首次手工替换及旧 seed 删除两项。
2. 首轮绿：`python -m pytest -q tests/test_persistent_profile.py tests/test_profiler_feedback.py`：54 passed。补充同一 profiler 实例文件丢失/恢复，以及删除 boost 后真实新贡献边界后，再运行相同命令，日志 `/tmp/xp-final-green.log`：57 passed、134 warnings。
3. 无 data 干净快照：将根目录 Python 源码与单个反馈测试复制到 `/tmp/xp-final-clean-23cnw0h2`，运行前断言无 `data` 目录；在快照内执行 `TZ=Asia/Shanghai python -m pytest -q tests/test_profiler_feedback.py`，日志 `/tmp/xp-final-clean.log`：4 passed、8 warnings，退出码 0，运行后仍无 `data` 目录。数据库仅在 TemporaryDirectory 中创建并自动清理。
4. `git diff --check -- persistent_profile.py profiler.py tests/test_persistent_profile.py tests/test_profiler_feedback.py docs/profile-review-fixes.md`：无输出。

### 未验证边界及限制

- 本轮没有重跑全仓库。主代理提供的基线为 TZ=Asia/Shanghai 下 343 passed、2 个既有 DB fail，singleton/web 文件缺依赖被排除；不能称全仓全过。
- 只读预检和后续同步/写入不是单一事务；预检后文件再次变化的跨进程竞态不在本轮保证范围内。
- 没有既往种子状态的旧 v1 库，无法恢复历史 synthetic seed 的配置与数值；不伪造已有历史元数据。本轮状态对后续投影开始记录。
- 显式 boost 变化按当前倍率与已存倍率比较；移除配置再以相同倍率恢复，不被识别为一次新的加权指令。若必须将此操作也视为显式新 boost，需主代理另行裁决配置版本/事件接口。
- 没有验证生产 DB、真实 Pixiv、iOS 宿主或跨进程并发；网络替身只覆盖既有异步客户端接口约束。警告来自 Python 3.12 SQLite datetime adapter 弃用。
- 本轮仅修改授权五个文件；无 commit、push、默认 DB 或业务网络操作。干净快照和日志保存在 `/tmp` 供复核。


## 受限验收修复：冻结种子的 raw 身份与可见性

本轮仅写 `persistent_profile.py`、`tests/test_persistent_profile.py`、本文档。基线副本 `/tmp/xp-frozen-before.py`、`/tmp/xp-frozen-tests-before.py`；实现差异 `/tmp/xp-frozen-implementation.diff`。未改其他代理文件、提交、推送、访问默认数据库或业务网络。

### 修复

- `_project` 分离 raw boost 配置和 resolver 后的倍率查询；新 seed 使用原始配置键，已有 seed 不再 DELETE 后写入 canonical 键。
- 冻结账项按 raw 行保存。alias 合并/拆分、block/unblock、stop_words 与移除 boost 仅影响投影，不修改冻结数值或原键；同倍率恢复不会增加新行。当前倍率显式改变仍解除相应冻结，保持原有配置变化语义。
- 首次 seed 就在 alias 下创建时，完整手工替换还须将新冻结种子的抵消项从 canonical 单位重平衡到 seed 的 raw 单位。此过程仅作用于新冻结行，并与 replacement 同事务，重复 replacement 不重放。否则仅保存 raw seed 仍会在 unalias 后留下 canonical manual 残影。
- 继续使用原 `preference_boost_seed` 与 `preference_manual`，不增加表、版本或每轮状态。

### 红绿与实际命令

工作目录为当前仓库，所有测试使用 `TZ=Asia/Shanghai` 与 `/tmp/xp-profile-venv/bin/python`。

1. 将 `/tmp/xp-final-review-fOODCp/tests/test_reviewer_final.py` 的参数化冻结往返函数原样复制到当前授权测试文件，再运行 `-m pytest -q tests/test_persistent_profile.py -k independent_frozen`。`/tmp/xp-frozen-red.log`：2 failed、53 deselected。alias 后得到 `canon=1,old=-1`；block 后得到 `old=-1`，与 public-api-probe.log 相符。
2. 初步实现后原有画像回归为 59 passed。随后新增公开 API 回归发现“首次构建时已存在 alias”的 manual cancellation 身份也需修复；保留断言并修正实现，未放宽测试。
3. 最终 `-m pytest -q tests/test_persistent_profile.py tests/test_profiler_feedback.py`，`/tmp/xp-frozen-green.log`：61 passed、142 warnings。包含 B1、manual=7、真实新贡献、配置改变及 rollback 等既有断言。
4. 新公开 API 测试使用 `save_tag_mapping_candidates` → `review_tag_mapping_candidate(...,'accept')`、`delete_tag_alias`、`block_tag`、`unblock_tag`；真实 SQLite。覆盖首次有/无 alias、三轮 block/stop/移除恢复配置、unalias、seed 行身份数值不变、新反馈和倍率改变。
5. 直接从外部目录运行独立测试时，pytest 路径引入了其旧快照，实现仍复现原失败：`/tmp/xp-frozen-independent-green.log` 为 2 failed、4 passed，不能作为当前实现结果。改为先从当前仓库 import `persistent_profile,database,profiler` 再调用 `pytest.main(['-q','--import-mode=importlib','/tmp/xp-final-review-fOODCp/tests/test_reviewer_final.py'])`，日志打印实际当前实现路径；`/tmp/xp-frozen-independent-current.log` 为 6 passed、14 warnings，包含事务注入回滚、manual7、新贡献和同实例 IP 故障拒绝。
6. `git diff --check -- persistent_profile.py tests/test_persistent_profile.py docs/profile-review-fixes.md` 无输出；未跟踪文件的完整本轮实现差异以 `/tmp/xp-frozen-implementation.diff` 复核，不以空 git diff 证明没有改动。

### 边界

未恢复在旧实现中已被 DELETE/重写而丢失的 raw 元数据；无法从 canonical 可靠反推，不伪造修复。显式倍率改变会按既有语义更新对应 seed，不承诺其旧基线永不改变。未运行全仓、生产库、宿主或并发写入测试；warnings 是 SQLite datetime adapter 弃用提示。主代理需负责并行集成后的全仓验收，无需为本轮新增状态架构作裁决。

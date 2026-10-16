# main 现网候选验证记录

## 范围与结论

对象为 `review/server-main-sync`，HEAD `8a062ee`，基线是已应用现网补丁和额外文件的未提交工作树，不是重构分支。本轮仅新增测试隔离、标签页稀疏配置兼容和回归断言；未提交、推送、SSH、修改生产或运行生产迁移。

新增有效运行时修复位于 `web/templates/tags.html`：先取得可缺省的 `tag_classifier.maintenance` 父配置，再读取批量上限和画像权重阈值。缺失或 null 父配置不再导致页面 500；显式配置的上限和阈值保持不变。

`tests/conftest.py` 为每个测试初始化真实临时 SQLite 数据库，并阻止 IPv4/IPv6 socket connect/connect_ex；不访问默认数据库。原反馈测试保留真实 alias 查询，没有用空 alias Mock 掩盖错误。数据库初始化、alias 加载或状态查询失败仍向上传播，没有新增运行时异常吞噬。已有审核修订、alias 覆盖保护、IP 折扣弃用和多样性逻辑未修改。

## 红绿证据

- 原始全量红灯：`/tmp/xp-server-main-red.log`，7 failed、332 passed、6 subtests passed。四项反馈失败为未初始化默认库缺少 `tag_aliases`，两项设置页失败为缺少 `system_state`；标签页为缺失父配置。已有控件标题断言无需改写，修复父配置后通过。
- 原模板回放：`/tmp/xp_live_template_red.py` 通过 Jinja loader 注入保存的现网原模板，不改候选文件。命令：`PYTHONPATH=/tmp TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q -p xp_live_template_red tests/test_web_entrypoints.py::WebEntrypointTests::test_tags_page_renders_with_sparse_maintenance_config tests/test_web_entrypoints.py::WebEntrypointTests::test_tags_page_preserves_configured_maintenance_bounds`。日志 `/tmp/xp-server-template-red.log`：三项稀疏配置子用例触发 UndefinedError；显式上限用例通过。
- 聚焦绿灯：`TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q tests/test_profiler_feedback.py tests/test_web_entrypoints.py`，42 passed、4 subtests passed。日志 `/tmp/xp-server-focused-green.log`。
- 全量绿灯：`TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q`，341 passed、10 subtests passed、768 warnings，33.42 秒。日志 `/tmp/xp-server-full-green.log`。`git diff --check` 通过。
- 宿主默认时区 `LCL-8` 不能被当前 tzlocal/zoneinfo 解析；隔离 DB 后最初暴露两个设置页时区错误，使用有效 `TZ=UTC` 后通过。未将其伪装成已修复的生产问题。

## 迁移脚本静态风险

`scripts/migrate_fallback_tag_classifications.py` 默认不 apply，调用只读审计；库函数 `migrate_fallback_classifications(..., apply=False)` 也默认 dry-run。本轮未执行迁移 CLI。已有迁移单元测试仅在临时库运行。

`--apply` 是显式写入开关，但默认数据库路径仍为项目 `data/pixiv_xp.db`，没有额外确认提示或生产环境识别。不能声称具备二次生产确认保护。CLI 会先备份，再调用 `_init_db_sync()`、迁移和缓存画像重建；旧表结构可能触发初始化中的 DROP，且这些步骤不是一个整体事务。重建失败可能留下已迁移数据和未重建画像。直接调用库函数 `apply=True` 不自动备份。执行授权、显式目标路径、备份与回滚、并发停写和重建失败处理须由主代理裁决；不建议将脚本随同步自动执行。

## 未验证边界与交接

网络隔离覆盖 Python IPv4/IPv6 socket 连接，不是操作系统沙箱，不能覆盖外部子进程网络。未验证真实 Pixiv/模型服务、生产数据库迁移、服务器启动、浏览器交互和生产时区。高数量 warnings 主要来自 Python 3.12 SQLite datetime 适配弃用，另有依赖与 TestClient cookie 弃用提示，未删除告警或放宽断言。

增量补丁：`/tmp/xp-server-main-followup.patch`，仅相对本轮开始的现网快照新增修改。现网原补丁仍在 `/var/minis/workspace/xp-server-review/server-working.patch`；没有混入重构提交。下一步是独立实质审查，是否提交、同步 main 或部署由主代理决定。

## 受限补丁：候选目标证据与批量 alias 安全边界

本轮只修改 `database.py`、`tests/test_database.py` 和本记录，基于派工时未提交工作树增量修改；没有清理已有改动，没有修改重构工作树、迁移执行门槛，也没有 commit/push/SSH、业务网络访问或默认 DB 操作。

### 实现与真实 SQLite 证据

- 提取 `_tag_mapping_proposal_fingerprint`，保存与输入队列复用同一源端/目标端证据指纹。源端、版本和指纹均未变化时仍排除已拒绝候选；目标端语义证据变化时重新入队，保存新增 revision 并保留 `supersedes_candidate_id`。回归逐字段比较原 rejected 行，确认旧记录不变。
- `apply_tag_mapping_ai_batch_sync` 在原有 `BEGIN IMMEDIATE` 事务内，针对实际 AI canonical 方向查询所有 kind 的既有 alias；不同目标或 kind 均报 `alias_conflict`，不覆盖 search。INSERT 使用 `ON CONFLICT DO NOTHING`。相同 equivalent alias 保持原行不变；冲突时 recommendation 暂存与候选状态一起回滚。
- 新增三项测试：目标端变化与 rejected 行不可变；不同目标 search、同目标 search、不同目标 equivalent 冲突及审核回滚；两个真实线程并发审核同一候选且 alias 已相同，只有一个审核成功、另一个因已审核拒绝，alias 行完全不变。后两项不模拟 SQLite 锁或事务。

### 命令与结果

工作目录 `/var/minis/workspace/xp-server-main-candidate`，解释器 `/tmp/xp-profile-venv/bin/python`，全部测试 `TZ=UTC`。

1. 修复前：`TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q tests/test_database.py -k 'rejected_target_evidence or batch_preserves_existing'`，日志 `/tmp/xp-evidence-red.log`。目标端变更后 inputs 仍为空；两个 search 分支未抛错且发生覆盖；不同目标 equivalent 已拒绝但缺少统一错误标识。结果 4 failed（含三个 subtest）、1 passed、29 deselected。
2. 修复后聚焦：`TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q tests/test_database.py tests/test_review_tag_mapping_ai.py`，日志 `/tmp/xp-evidence-focused-green.log`。42 passed、3 subtests passed（新增并发测试前）。
3. 含新增并发测试全量：`TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q`，日志 `/tmp/xp-evidence-full-green.log`。344 passed、13 subtests passed、781 warnings，34.59 秒。`git diff --check` 通过。告警未隐藏。

### 阻断交接：规划/应用一致尚需范围裁决（后续授权与修复见下一节）

允许修改范围不包括 `scripts/review_tag_mapping_ai.py`。该脚本 `_build_recommendation_plan` 在 `follow_ai` 路径只读 equivalent alias，默认路径不执行该 alias 冲突过滤；因此数据库应用层已安全拒绝，但规划仍可能显示 `eligible=1`，不能声称规划与应用一致，也不应据全量测试绿灯直接同步。

最小真实 SQLite 复现 `/tmp/xp-evidence-plan-boundary.py`，命令 `PYTHONPATH=. TZ=UTC /tmp/xp-profile-venv/bin/python /tmp/xp-evidence-plan-boundary.py`，日志 `/tmp/xp-evidence-plan-boundary.log`：已有 `source -> other` manual search，dry-run 仍返回 `eligible: 1, blocked: {}`；确认应用抛 `alias_conflict`，原 alias 保留。复现仅调用本地函数和临时库，没有运行迁移 CLI 或调用模型。

需要主代理授权扩展写入 `scripts/review_tag_mapping_ai.py`（及其对应测试），将默认/follow_ai 的全类型冲突判定统一至与应用相同的实际方向，并保留应用事务内再次校验以防规划后并发变化。在现有范围下未通过改变通用 alias API、隐藏候选或伪造 Mock 来规避缺口。真实模型调用、宿主/生产 SQLite 并发和生产数据库均未验证；本次证据未变测试直接验证队列为空与 save=0，没有运行真实模型服务。


## 受限收尾：批量规划与事务应用的 alias 冲突一致性

主代理明确授权扩展脚本规划修复。本轮仅修改 `scripts/review_tag_mapping_ai.py`、`tests/test_review_tag_mapping_ai.py` 和本记录；保留派工时工作树改动，未修改 `database.py` 的事务保护。基线为既有 344 passed、13 subtests passed。

### 实现与验收证据

- `_load_alias_state` 从同一 SQLite 查询结果分别建立全 kind 的源键占用状态和仅 `equivalent` 的身份边。默认及 `follow_ai` 规划均执行冲突过滤；`search` 不参与等价解析、反向幂等判定或循环检测。
- `_build_recommendation_plan` 使用与 `database.apply_tag_mapping_ai_batch_sync` 相同的归一化 canonical 方向：canonical 为候选源时反转，否则使用候选源指向目标。实际源键已有不同 target 的任何 kind，或同 target 的 search，均计入 `blocked.alias_conflict`；同 target equivalent 可幂等。批次内新建 alias 同时更新占用状态和等价边。
- 新增 5 项真实临时 SQLite 测试：默认、follow_ai 正向、follow_ai 反向的三类冲突共 9 个子用例；三个方向的同 target equivalent 幂等；search 反向边及混合路径不冒充 equivalent；三个方向规划后新增同 target search 时，直接应用旧 plan 仍在事务内拒绝，alias、candidate、recommendation 全行快照不变。最后一项是可重复的规划后变更复现，不声称为真实线程竞争测试。
- 既有两个纯规划单测改为替换新的 `_load_alias_state` 接口，保留双状态返回约束；新增验收不 mock 数据库连接、写入或事务。

### 实际命令与结果

工作目录 `/var/minis/workspace/xp-server-main-candidate`；全部测试使用 `TZ=UTC` 和 `/tmp/xp-profile-venv/bin/python`，由既有 autouse fixture 隔离临时 DB 并阻断 Python 业务网络。

1. 修复前运行 `TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q tests/test_review_tag_mapping_ai.py -k Sqlite`，日志 `/tmp/xp-plan-red.log`：5 failed、4 passed、7 subtests passed、11 deselected。五个失败均为 eligible 未过滤：默认模式的三类冲突，以及 follow_ai 正反向的同 target search。
2. 修复后运行 `TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q tests/test_review_tag_mapping_ai.py tests/test_database.py`，日志 `/tmp/xp-plan-focused-green.log`：48 passed、18 subtests passed、166 warnings，8.61 秒。
3. 运行 `TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q`，日志 `/tmp/xp-plan-full-green.log`：349 passed、28 subtests passed、791 warnings，36.00 秒。没有删除断言、跳过失败或隐藏告警。`git diff --check` 通过。

局部补丁 `/tmp/xp-plan-consistency.patch` 仅包含相对本轮文件快照 `/tmp/xp-plan-baseline/` 的三文件增量，不包含数据库事务补丁或其他既有工作树修改。

### 边界与裁决

规划仍是建议性快照，不承诺规划后状态不变；事务内重检保留且已用规划后写入验证回滚。未验证生产 DB、宿主多进程竞争、真实模型或服务网络；未访问默认 DB，未执行迁移、commit、push、SSH 或发布。该范围内无新增阻断项；提交、同步和部署仍由主代理裁决。

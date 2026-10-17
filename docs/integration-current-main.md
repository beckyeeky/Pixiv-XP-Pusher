# 当前 main 整合验收记录

公开报告中的服务器身份、内部路径与作品/画师标识使用占位符；原私有验收材料另行保存且未修改。

日期：2026-10-17。状态：完整可测试的未提交候选，等待主代理关键 diff 与独立审查；不是生产升级放行。

## 范围与基线

- 唯一工作树：`/var/minis/workspace/xp-integration-20261017`。
- 分支：`integration/persistent-profile-current-main`。
- HEAD：`db6663087eadc559a85b4a813b936b667e0b7793`。
- MERGE_HEAD：`43ec3ad22d249778c0f0961453b78fdf6439f07d`。
- 接收时已执行 `git merge --no-commit --no-ff main`。本次仅解决隔离候选，未 commit/push/SSH，未访问业务网络或生产数据库。
- 三个冲突文件已标记解决，其余本次修改保持未暂存，便于主代理区分合并内容与补修。

## 合并决定与实现

1. `tests/test_profiler_feedback.py` 同时保留重构的隔离数据库 setup 和 main 的 alias 加载失败测试。
2. `tests/test_web_entrypoints.py` 保留实际 Provider/Model 控件的严格断言，保留有效 AI Provider 标题；不恢复已删除的 profiler_ai_model 控件断言。
3. `web/templates/tags.html` 采用 main 对空 tag_classifier/maintenance 的防御性读取。
4. 自动合并导致 `task_manager.setup_services` 的 `boost_tags` 关键字重复，造成 SyntaxError；删去重复项，仅传一次有效配置。
5. `XPProfiler._profile_preflight` 保留 persistent/legacy 安全门，但不再拿已弃用 IP 文件错误拒绝写入；应用 build 和 feedback 给账本固定传入 IP 倍率1。旧配置字段、加载诊断和弃用警告保留；底层 `persistent_profile` 兼容参数及其独立测试保留，不代表应用启用折扣。
6. `cache_only=True` 在新入口复用账本 build，不走旧 TF-IDF 覆盖路径；fallback 分类迁移脚本使用该入口，无 Pixiv 请求、不推进同步状态。CLI 与升级说明明确它不能恢复已删除历史。
7. 原 boost 按首次有效配置一次锚定；raw seed alias/block 冻结修复不改动。默认 persistent、默认关闭自动外搜、永久曝光ID/反馈metadata、仅 Telegram+Web 保留。

## main 安全修复核对

对照 main 及整合 diff，保留以下实现和原断言：

- `database.py` 候选 revision、两端 semantic evidence、不可变旧审核记录、所有 kind 的既有 alias 覆盖保护、批量审核预规划与事务一致性。
- `filter.py` 与 `profiler.py` alias/屏蔽加载错误向上传播，不退回未审核身份；`tag_classifier.py` 未接受分类保持 unresolved。
- `daily_slate.py` 每次添加都受总 limit 约束，身份 cap 通过 accepted resolver 计数，分类贡献 fail-closed。
- 原两个数据库失败 `test_ai_recommendation_is_audited_and_staged_without_creating_alias`、`test_review_rejects_a_group_that_changed_after_confirmation` 均已通过，不保留失败豁免。

## schema9 升级证据

`tests/test_integration_current_main.py::test_real_current_main_schema9_migrates_idempotently` 用 `git show` 读取固定 main 提交的真实 database 初始化器，在临时 SQLite 建立 main9，而不是只写一个版本号模拟。

- 确认迁移前 schema_version=9，且无 preference_migrations。
- 保留旧权重和 search alias；将旧 push_history 回填到永久曝光表。
- 初始化后逐个验证账本/留存关键表存在，旧画像只导入一次。
- 再次初始化，对除 system_state 时间刷新外的所有业务表逐表比较完整行数据，严格相等；验证单个迁移标记和备份存在。
- `database._init_db_sync` 不因 schema_version 相等跳过 DDL：实际列检查和独立 preference/retention 标记负责迁移。

该测试需要本地 Git 对象中存在固定 main 提交，不需网络；不能代替真实生产旧库副本演练。

## 红灯、修复与绿灯

实际命令均在上述工作树运行，解释器 `/tmp/xp-profile-venv/bin/python`，时区 `TZ=UTC`；测试 autouse fixture 使用临时 DB 并禁止 INET/INET6 socket 连接。

1. `TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q`，日志 `/tmp/xp-integration-red.log`：收集失败2项，均为重复 boost_tags SyntaxError。
2. 修复重复参数后同命令，日志 `/tmp/xp-integration-red2.log`：431 passed、38 subtests passed、3 failed。三项 main 画像测试原先只 mock async DB，默认 persistent 已改为同步 SQLite 账本，mock 数据未进入账本。
3. 新增真实SQLite弃用政策测试先执行，日志 `/tmp/xp-integration-policy-red.log`：4 failed，证实 IP0.3 仍错误降权、缺失/损坏文件仍错误阻断。修复应用传参/预检后绿灯。
4. 原 main 三项 mock 画像测试显式设为 legacy，保留全部原业务断言；默认 persistent 的对应语义由新增真实SQLite测试覆盖，不 mock 账本返回值。原重构中要求弃用IP文件阻断的测试按主裁决改为忽略文件、离线不访问客户端、旧权重稳定和真实反馈结果断言；不再保留相反政策断言。
5. 新联合测试 `/tmp/xp-integration-joint.log`：10 passed。覆盖 main9 迁移幂等、0.3忽略、旧权重/boost锚定、反馈重复/切换、all-kind alias审核保护与raw账本、fallback投影不覆盖、清理后曝光去重和贡献幂等。
6. 最终完整命令（无排除）：`TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q`，日志 `/tmp/xp-integration-final-full.log`：**444 passed、38 subtests passed、0 failed**，109.62秒。全部原测试及新增测试均收集执行。
7. `git diff --check`、`git diff --cached --check` 均通过；三个文本冲突已解决，无 unmerged 文件。

1234个 warning 主要为 Python3.12 SQLite 默认 datetime adapter 弃用、Starlette cookie 接口弃用及 requests 字符检测可选依赖提示，不作为失败豁免。未通过删断言、跳过测试或放宽 Mock 制造通过。

## 未验证边界与待裁决

- 未测试真实 Pixiv、Telegram 图片/回调/菜单或本次整合的宿主 Web server；此前基线的连通性记录不能充作本次验证。
- 未取得真实旧生产库副本；未完整验证跨进程竞争、磁盘耗尽、强杀等宿主故障。
- 两个版本不允许并发写同库，正式迁移必须停写并保存一致性备份；已用持久库回滚需旧程序与升级前备份，不能 legacy 无损往返。
- 已删除历史不能恢复；不新增推荐功能、视觉模型或每日外搜预算。
- 主代理需审阅关键 diff 和独立审查后决定是否提交/发布；本次没有提交、推送或部署授权。

## 受限旧锚点保护补充（2026-10-17）

- `db6663087eadc559a85b4a813b936b667e0b7793` 的真实 initializer + profiler build 在临时库生成旧 IP0.3 / boost2 锚点。应用 build（非 cache-only）和 feedback 均须拒绝；拒绝前后完整 `iterdump()` 相等且客户端 `mock_calls == []`。
- 新增 `preference_application_policy(policy='fixed-ip-v1')`，仅应用接口启用；同步前只读检查，build/feedback 事务内再次检查并与账本同事务写标记。低级 ledger 默认兼容语义不变。非1来源未知的旧boost也拒绝，不重写旧锚点。
- 真实固定 main9 初始化器无账本首次迁移、boost2首次锚定、重启boost4得到16；注入投影异常时完整dump不变（包括政策标记DDL回滚）。
- 最初红测 `/tmp/xp-anchor-red.log` 因旧build不支持 cache_only 而失败，不作为功能红灯证据。修正为真实旧接口后，禁用新增 `_check_application_policy` 的诊断性对照运行得到4项预期拒绝失败：`/tmp/xp-anchor-red-guard-disabled.log`；此对照不属于通过测试，不替代真实接口测试。正常保护开启后整合测试15项全绿，日志 `/tmp/xp-anchor-green.log`；后续增加事务回滚测试1项。
- 最终完整命令：`TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q`，`/tmp/xp-anchor-final-full.log`：**450 passed、38 subtests passed、0 failed**，98.33秒；原444项保持通过，新增6项。`git diff --check` 通过。
- 主代理已SSH只读确认真实备份 `<rehearsal-root>/<backup-id>/baseline.sqlite3` 无 preference migration/usage/anchor 与 retention 账本；本子任务未SSH、未访问业务网络或默认数据库。该识别不替代真实副本演练，后者仍待做。未知旧账本仍需备份及专门兼容处理，不声称自动恢复或完整兼容。

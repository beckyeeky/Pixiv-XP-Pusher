# 第二阶段候选核验（2026-10-16）

## 范围与结论

工作树基线 `8a062ee`。保留全部已有未提交候选；本轮仅修改 `database.py`、`task_manager.py`、`tests/test_phase2_safety.py`、`tests/test_task_manager.py` 和本报告/phase2 测试日志。未修改画像代理负责文件，未提交、推送、调用业务网络或打开默认业务数据库。测试使用 `/tmp/xp-profile-venv/bin/pytest` 和临时 SQLite。

核心缓存误曝光缺陷已修复；第二阶段仍不具备无条件验收条件：发现范围外关联推荐调用会把未确认发送作品标记已推送，需主代理裁决；没有日硬预算；没有真实生产旧库验证。

## 已完成与证据

- `database.py::_EXPOSURE_BACKFILL` 删除从全部 `illust_cache` 回填曝光的 SQL；仅从 `push_history` 与明确 `feedback` 回填。普通候选不会因初始化、清缓存、历史维护进入永久曝光。
- `feedback_metadata` 回填仅针对有反馈且缓存确实存在的数据。反馈触发器由 LEFT JOIN 改为 JOIN：无缓存时保留反馈/曝光 ID，但不制造空元数据；之后缓存写入触发器可补存真实数据。
- `mark_pushed` 在同一 SQLite 事务内写历史及曝光；触发器 AFTER INSERT/UPDATE 与反馈写入共享事务，留存失败会回滚原写入。测试分别注入曝光写入失败，验证历史/反馈未残留。
- `_migrate_exposure` 使用 BEGIN IMMEDIATE，提交前完成表、回填、触发器及迁移标记；`_backup_exposure_upgrade` 使用 SQLite backup，迁移前生成 retention-v1 备份，标记存在后重复初始化不再备份。测试以临时数据库模拟旧 schema，验证备份含旧历史且无新曝光表、迁移保留已推送/反馈 ID、不包含仅缓存 ID。
- `_retain_before_cleanup` 在 BEGIN IMMEDIATE 内先回填再清理；已覆盖总维护、独立历史清理、缓存定期清理、单作品缓存删除。清理后仍可查曝光以及反馈标签/作者信息。
- `classification_maintenance.py::run_profile` 自动外搜默认关闭，只有配置布尔 true 开启；测试验证默认不触达分类器。既有分类缓存测试验证 manual/已确认不随普通 TTL 失效，unresolved 冷却后可重试；手工分类仍可外搜。
- 渠道候选运行装配仅构造 Telegram；废弃字段/渠道不自动转 Telegram。`task_manager.py::setup_notifiers` 本轮修复无通知器返回 None 的问题，改为空列表，避免仅 Web 模式日报等遍历 None。新增测试覆盖 Web-only、Telegram 未配置、OneBot/AstrBot/email 废弃字段均不构造 Telegram。
- `launcher.py` 保留 Telegram/仅 Web 选择；Web settings 候选去除旧渠道字段。`requirements.txt` 仅删除 aiocqhttp，aiohttp 等公共依赖仍在。

## 红绿测试与实际命令

1. 将迁移测试预期从 `{901,902,903}` 修正为真实曝光 `{901,902}` 后运行：
   `/tmp/xp-profile-venv/bin/pytest -q tests/test_phase2_safety.py`
   修复前结果 `1 failed, 5 passed`：903 仅缓存，却被加入曝光；错误为 `Extra items in the left set: 903`。
2. 修复并新增测试后同命令：`10 passed`。完整日志 `docs/validation/phase2-safety.log`。
3. 第一轮受影响测试未覆盖时区：`4 failed, 100 passed, 6 subtests passed`。其中两项 scheduler 测试受宿主 `TZ=LCL-8` 影响，tzlocal 报 `ZoneInfoNotFoundError`。日志 `docs/validation/phase2-environment.log`。后续明确使用 `TZ=Asia/Shanghai`，不修改断言或 Mock 掩盖错误。
4. 最终命令：
   `TZ=Asia/Shanghai /tmp/xp-profile-venv/bin/pytest -q tests/test_phase2_safety.py tests/test_database.py tests/test_classification_maintenance.py tests/test_config.py tests/test_task_manager.py tests/test_settings_editor.py tests/test_refresh_config.py tests/test_delivery_reconciliation.py tests/test_related_recommender.py`
   结果 `2 failed, 115 passed, 10 subtests passed`；日志 `docs/validation/phase2-regression.log`。另有 datetime adapter 弃用警告。
5. 基线复核：`git archive 8a062ee | tar -x -C /tmp/xp-phase2-baseline-8a062ee`，在独立临时副本使用相同时区/解释器运行上列测试（不含新 phase2 文件）。结果 `2 failed, 99 passed`；日志 `docs/validation/phase2-baseline.log`。基线和候选均失败于：
   - `DatabaseInitTests::test_ai_recommendation_is_audited_and_staged_without_creating_alias`
   - `DatabaseInitTests::test_review_rejects_a_group_that_changed_after_confirmation`
   两者均出现候选列表为空而索引越界，不属于本轮引入的失败，未改断言。
6. `git diff --check` 通过。

## 未完成、边界及主代理裁决

- **范围外确认缺陷**：`related_recommender.py::_push_chain_results` 对全部 `top_results` 无条件 `mark_pushed`，即使 `sent_map.get(ill.id)` 为 None。缓存误曝光修复不能阻止该调用制造曝光。该文件不在授权修改范围，本轮仅只读核查；需授权修复并增加部分/失败发送不曝光测试。对照 `delivery_reconciliation.py::DatabaseDeliveryPersistence.commit` 接收 ConfirmedDelivery，正常主链使用已确认发送边界。
- **已运行过错误候选的 v1 库**：迁移标记 version=1 存在时直接返回，不能区分其中缓存误回填 ID 与已清历史的真实曝光。不能安全自动删这些 ID；需主代理确认候选是否触碰过真实库，并使用迁移前备份/真实发送证据制定修复方案。本轮不猜测、不删除已有曝光。
- **真实旧库**：仅模拟旧表结构和数据，未接触生产数据库；不能声称已验证真实生产旧库升级。迁移备份与后续持久画像迁移由现有集成顺序调用，画像迁移细节归另一代理。
- **预算**：手工 classify_tag/run_reviewed 仍可外搜；现有每次运行数量限制不等于日硬预算，没有实现日总额、跨入口原子预留或持久计数，不能标记 quota 完成。
- **渠道边界**：未真实启动 Web 服务或向 Telegram 发信；测试只核实装配/配置兼容与不自动发送。Web 是独立 UI，不是新增 notifier。旧渠道模块文件仍保留但不由公共导出/调度装配加载。
- 本轮不处理基线两个标签映射测试失败、不发布，也不接管画像修复。

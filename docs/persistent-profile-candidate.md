# 持久画像第一阶段候选升级说明

状态：未提交、未发布；仅覆盖持久画像纵向候选，不代表 A—D 全部完成。
基线：`8a062eef9fb054d8aa3f2a7512ac2ba30ef6194d`。

## 已实现的路径

- `database._init_db_sync` 在旧库升级前通过 SQLite backup API 备份到数据库同目录 `backups/`，包含已提交 WAL 数据；备份失败则不继续初始化。
- 移除旧初始化中删除 `xp_bookmarks`、`xp_profile`、`xp_tag_pairs`、`illust_cache` 的分支；缺少 `user_id` 时添加列并保留未知归属为 0，不猜测真实用户。
- `persistent_profile.migrate` 使用独立 `preference_migrations` 版本号。账本建表、旧画像权重导入、旧反馈标记、版本写入在一个 `BEGIN IMMEDIATE` 事务中完成。注意：既有其他 schema 初始化仍含多个事务；尚不能宣称整个历史升级链原子化。
- 旧 `xp_profile` 数值原样导入 `preference_base`。已有画像时，迁移前的收藏标记为已计入，不重复增加；历史权重不反推为虚构的逐项观察。
- 新收藏按作品、用户幂等登记，每个去重标签增加 1.0；不按发表时间衰减，不使用高频饱和度剔除。该增量规则是本阶段新规则，不是旧 TF-IDF 的数学等价替换。
- `XPProfiler` 默认使用 `profile_mode="persistent"`；`config.normalize_config` 为旧配置补默认值、规范化大小写和空白，拒绝其他值；`load_config` 不吞掉无效模式异常。`task_manager.setup_services` 在 DB 初始化及登录前复核模式，并装配 `profile_mode`、`boost_tags`。`legacy` 保留旧算法。
- 新反馈记录精确贡献值；重复反馈不再次增加标签、画师、负向权重或 dislike 计数；反向反馈先撤销旧记录再应用新值。上述写入及兼容 `feedback`、`xp_profile` 投影在同一事务中提交。
- 后续 build 从不可重复导入的基线与观察账本投影，不覆盖反馈贡献。boost、标签黑名单、已接受 alias 的变化重新投影，不在上次投影结果上重复乘权。每日屏蔽同步只更新投影，不删除观察。

## 明确暂停点与兼容边界

1. 导入旧反馈缺少原始 metadata：相同 action 为 no-op；首次切换保留未知历史影响，应用一个新的、可精确撤销的迁移后贡献。返回 `migration_policy` 解释该策略；后续切换只撤销已记录的新贡献。不能宣称撤销了未知历史贡献。重置后反馈也按此政策处理。
2. `reset_xp_data` 已接入受控持久重置，所有清理在同一 `BEGIN IMMEDIATE` 中提交或回滚：清空正向基线、投影、手工校正、收藏观察，保留收藏缓存供下一轮只导入一次；保留反馈、负向画像、画师历史、黑名单，并将旧反馈标记为不可逆历史。直接 `update_xp_profile` / `adjust_tag_weight` 在事务中记录固定数值单位的手工校正，并同步投影；下次 build 不再因接口写入而暂停。手工校正不是原始观察，不额外接受 boost，配置变化后维持固定单位；完整替换只保证当前配置下下一轮稳定，不冻结未来真实新贡献。外部 SQL 绕过接口仍会被偏离检测拒绝。
3. 导入基线在首次持久 build/feedback 的事务中，按每个原始标签记录当次生效倍率锚点（boost 优先于 IP discount）；首次和后续同配置原值不变，后续使用 `新倍率 / 锚点倍率` 调整旧基线，新贡献使用新倍率一次。此为受控相对锚定，不声称知道迁移前真实配置或能重建未知贡献。首次调用使用的配置须由运维确认；锚点倍率为零无法反推，保留不透明旧权重，不推测恢复。标签屏蔽仍可隐藏投影，alias 仍可合并同义标签。
4. 反馈撤销不会自动解除已自动屏蔽的画师，避免误解除人工屏蔽。负向读取和 Top-N 按当前已审核 alias 聚合，保留负向原始键以精确撤销已记贡献及支持 alias 取消；未改黑名单计数的全部展示/查询路径。
5. 未重算持久模式的共现组合、未改排序和策略比例、未自动合并 alias、未增加图像或向量聚类。
6. 曝光 ID 与可删缓存解耦、旧反馈 metadata 恢复、分类维护与低配额配置、Telegram/Web 装配均不在本候选已完成范围。

## 本地验证

隔离依赖环境：`/tmp/xp-profile-venv`，安装了 APScheduler、pixivpy-async、pytest-asyncio；未调用 Pixiv、Telegram 或分类外部服务。

```sh
/tmp/xp-profile-venv/bin/python -m pytest -q tests/test_persistent_profile.py tests/test_profiler_feedback.py
# 16 passed, 29 warnings in 2.21s
/tmp/xp-profile-venv/bin/python -m pytest -q tests/test_database.py
# 2 failed, 24 passed, 57 warnings in 2.77s
```

数据库的两个失败已在独立 `git archive` 基线目录 `/tmp/xp-profile-baseline` 复现：`2 failed, 24 passed`。失败项为 `test_ai_recommendation_is_audited_and_staged_without_creating_alias` 和 `test_review_rejects_a_group_that_changed_after_confirmation`；本候选未删除断言或跳过它们。

新增测试覆盖 WAL 备份和恢复、历史权重不变、旧收藏避免重复导入、新收藏缓存删除后重入不重复计权、迁移重复执行和失败回滚、反馈重复和双向切换、并发重复反馈、反馈失败回滚、boost/alias/屏蔽重投影、每日屏蔽维护和真实 `XPProfiler.build_profile` 调用。

日志保留于 `docs/validation/persistent-profile/`：`targeted.log`、`database-candidate.log`、`database-baseline.log`；`.log` 被项目忽略规则排除，尚未暂存。`git diff --check` 通过。未运行全套测试、未进行线上数据库或宿主交互验收。

首次回归发现旧反馈单测漏 Mock `get_accepted_tag_aliases`，曾连接默认 `data/pixiv_xp.db` 并因缺少 `tag_aliases` 表失败；随后改为隔离该查询。该文件元数据为 0 字节，未读取任何业务行、未执行迁移或业务写入，无法仅凭现有证据判定其此前已存在还是由连接创建；未删除该文件。

## 兼容性阻断修复复核（2026-10-16）

本轮只修改许可的画像、数据库、配置、装配及对应测试文档；未改曝光、分类、渠道实现。未 commit/push，未调用 Pixiv/Telegram/分类业务服务，测试 DB 均为 tmp_path（旧反馈单测为既有 Mock 隔离）。注意：为解除 task_manager 收集失败，本轮执行了 `pip install 'python-telegram-bot>=20.7'`，访问包索引下载测试依赖；这不是业务 API，但若“禁止外网服务”包含依赖下载，则属于超出限制，须主代理记录并裁决，不应隐瞒。

实际命令和结果：

```sh
# 改前：包含 task_manager 的 targeted 因缺 telegram 无法收集
/tmp/xp-profile-venv/bin/python -m pytest tests/test_persistent_profile.py tests/test_profiler_feedback.py tests/test_config.py tests/test_task_manager.py tests/test_database.py -q
# ERROR collecting tests/test_task_manager.py: No module named 'telegram'
# 改前：排除无法收集的模块
/tmp/xp-profile-venv/bin/python -m pytest tests/test_persistent_profile.py tests/test_profiler_feedback.py tests/test_config.py tests/test_database.py -q
# 2 failed, 58 passed；两个数据库失败与已有基线一致

# 红灯：在单独进程替换为改前亲读的原始 _project，其他代码不变
/tmp/xp-profile-venv/bin/python docs/validation/persistent-profile/compatibility/xp-profile-original-project-red.py
# 1 failed, 1 passed, 16 deselected；boost=2, IP=.3 复现原候选二次加权
# 该复现是原始函数的隔离重放，不是整份旧候选 checkout 的端到端结果。

# 绿灯：所有持久画像、旧反馈、配置测试与新装配契约测试
/tmp/xp-profile-venv/bin/python -m pytest tests/test_persistent_profile.py tests/test_profiler_feedback.py tests/test_config.py tests/test_task_manager.py::ProfileAssemblyTests -q
# 45 passed, 6 subtests passed

/tmp/xp-profile-venv/bin/python -m pytest tests/test_database.py -q
# 2 failed, 24 passed；不属于本轮修改的两个既有数据库断言失败

TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest tests/test_persistent_profile.py tests/test_profiler_feedback.py tests/test_config.py tests/test_task_manager.py tests/test_database.py -q
# 4 failed, 78 passed, 6 subtests passed
# 除上述两个数据库失败外，两个调度失败是运行环境没有 zoneinfo 数据。
cd /tmp/xp-profile-baseline
TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest tests/test_task_manager.py -q
# 同样的 2 failed, 9 passed；确认不是本轮新增回归。
```

证据留于 `docs/validation/persistent-profile/compatibility/`（含 before/core、红灯、绿灯、全 targeted、数据库最终、调度基线、原函数重放脚本和中间失败日志）。首次新增重置回滚测试曾误用 `strategy_name` 列，已按真实 schema 改成 `strategy`，未改产品 schema 或放宽断言。原候选新增的“旧反馈切换应暂停”单测按本轮明确的新政策替换为更完整的切换、重复、再切回数值断言；基线旧测试断言未改。

未验证边界：未运行全套测试；未验证 Telegram/Web 宿主交互；未验证黑名单计数全部 alias 查询路径；未改自动解除画师屏蔽、曝光、分类或渠道模块。手工写与 reset 的 SQLite 故障回滚、锚点倍率变化、重复 build、缓存重入、负向 alias 合并及撤销均由真实 SQLite 测试覆盖，不能替代宿主验收。主代理需裁决首次配置锚定、固定单位手工校正、reset 后保留不透明负向/画师历史是否满足产品预期，并独立审查。

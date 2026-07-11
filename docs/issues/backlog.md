# Backlog

> 原则：优先处理**不破坏现有运行实例**的修复；所有变更默认要求向后兼容、可回滚、可观察。

## P0

- [x] ISSUE-001：修复 `config.example.yaml` 的 `filter.daily_limit` 示例格式，并在运行时增加非破坏性的配置规范化，避免错误类型导致实例异常。
- [x] ISSUE-002：统一 `web/app.py`、`web/app_v2.py` 与 `web/app.py.bak`，收敛为单一 Web 入口；保留 `web.app_v2` 兼容入口，并移除陈旧备份文件。
- [x] ISSUE-003：建立最小测试框架，先覆盖配置加载、数据库初始化与 Web 入口兼容性；后续再补登录与过滤逻辑。

## P1

- [x] ISSUE-004：为数据库引入 schema 版本元数据与数据库概览接口，先建立版本化迁移基线；后续再逐步替换旧的删表式升级逻辑。
- [x] ISSUE-005：增强 Web Session 的安全属性，加入登录限速与可配置 Cookie 安全参数；会话持久化后续再做数据库化。
- [x] ISSUE-006：增加运行摘要状态写入、健康检查扩展与运行时状态 API，先提供最小可观测性。
- [x] ISSUE-007：新增功能对齐矩阵文档，标明默认入口、兼容入口、自动化覆盖与缺口。

## P2

- [x] ISSUE-008：为 notifier 增加 capability 声明，并补文档说明能力边界。
- [x] ISSUE-009：新增数据库维护脚本，支持 overview / backup / cleanup。
- [x] ISSUE-010：新增推荐质量基线脚本，先基于 `strategy_stats` 导出可读统计。

## Active GitHub slices

> 下面跟 GitHub Issues 对齐；详细说明见 `docs/issues/`。

### Done (parent tracks closed)

- [x] [#31](https://github.com/beckyeeky/Pixiv-XP-Pusher/issues/31) Classification Maintenance 父 issue
- [x] [#33](https://github.com/beckyeeky/Pixiv-XP-Pusher/issues/33) / [#40](https://github.com/beckyeeky/Pixiv-XP-Pusher/issues/40) Unresolved Tag Review Queue
- [x] [#34](https://github.com/beckyeeky/Pixiv-XP-Pusher/issues/34) 多 Judge 投票 + Danbooru evidence
  本地文档：[`docs/issues/0034-multi-judge-danbooru-evidence.md`](./0034-multi-judge-danbooru-evidence.md)
- [x] [#38](https://github.com/beckyeeky/Pixiv-XP-Pusher/issues/38) Provider / Model / Credential / Tag-review 管理面（由 #39–#43 交付）

### Residuals from #38

- [x] [#44](https://github.com/beckyeeky/Pixiv-XP-Pusher/issues/44) Settings 暴露 Classification Maintenance 运维字段
- [x] [#45](https://github.com/beckyeeky/Pixiv-XP-Pusher/issues/45) Provider / Model 删除控件 + 引用检查

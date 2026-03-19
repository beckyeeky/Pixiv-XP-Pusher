# Backlog

> 原则：优先处理**不破坏现有运行实例**的修复；所有变更默认要求向后兼容、可回滚、可观察。

## P0

- [x] ISSUE-001：修复 `config.example.yaml` 的 `filter.daily_limit` 示例格式，并在运行时增加非破坏性的配置规范化，避免错误类型导致实例异常。
- [x] ISSUE-002：统一 `web/app.py`、`web/app_v2.py` 与 `web/app.py.bak`，收敛为单一 Web 入口；保留 `web.app_v2` 兼容入口，并移除陈旧备份文件。
- [ ] ISSUE-003：建立最小测试框架，覆盖配置加载、数据库初始化、Web 登录与基础过滤逻辑。

## P1

- [ ] ISSUE-004：为数据库引入版本化迁移机制，替代探测失败后直接删表的做法。
- [ ] ISSUE-005：增强 Web Session 的持久化与安全属性，在不影响当前登录体验的前提下减少重启丢会话问题。
- [ ] ISSUE-006：增加调度、队列、重试、推送结果的运行指标与日志可观测性。
- [ ] ISSUE-007：整理 README 与默认运行入口的功能对齐矩阵，避免文档描述与实际行为偏离。

## P2

- [ ] ISSUE-008：抽象 notifier capability，统一 Telegram / OneBot / AstrBot 的能力边界与降级行为。
- [ ] ISSUE-009：补充数据库维护工具（备份、清理、表统计）。
- [ ] ISSUE-010：建立推荐质量评估闭环，为后续 embedding / AI scorer 调优提供基线。

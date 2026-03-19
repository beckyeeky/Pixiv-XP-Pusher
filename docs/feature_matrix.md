# Feature Matrix

| 功能 | README 已描述 | 默认入口 `web.app` | 兼容入口 `web.app_v2` | 自动化覆盖 | 备注 |
|---|---|---|---|---|---|
| Web 登录/仪表盘 | 是 | 是 | 是（兼容层） | 是（入口兼容） | `web.app_v2` 复用同一对象 |
| 配置加载规范化 | 是（配置示例） | 是 | 是 | 是 | `config.load_config()` 负责规范化 |
| 数据库初始化 | 间接 | 是 | 是 | 是 | 通过 `init_db()` 建表与记录 schema 版本 |
| 导入/导出配置 | 是 | 是 | 是 | 否 | 仍需补 Web API 测试 |
| 数据库维护工具 | 否 | N/A | N/A | 否 | 新增 `scripts/db_maintenance.py` |
| Notifier 能力声明 | 否 | N/A | N/A | 否 | 通过 `BaseNotifier.CAPABILITIES` 暴露 |
| 推荐效果基线评估 | 否 | N/A | N/A | 否 | 新增 CLI 汇总策略统计 |

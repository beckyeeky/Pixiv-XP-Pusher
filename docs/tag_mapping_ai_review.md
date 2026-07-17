# AI 辅助 Tag Mapping 审核

Relationship Judge 使用现有 `tag_mapping.model`，读取待审候选两侧的分类、翻译、Grounded Judge explanation、画像权重、可用的 Embedding similarity、候选来源和旧建议说明，生成可审计的 AI Relationship Recommendation。非 Embedding 来源的候选会把相似度明确记录为 `null`，不会伪造分数。

## 配置

```yaml
tag_mapping:
  enabled: true
  model: deepseek_flash
  batch_size: 50
  review_concurrency: 3
  review_temperature: 0.0
  review_max_output_tokens: 1024
```

## 推荐操作顺序

```bash
# 只复核前 20 条；会把展示的语义资料发送给配置的外部 LLM Provider
python3 scripts/review_tag_mapping_ai.py judge --limit 20

# 查看候选及最新建议
python3 scripts/review_tag_mapping_ai.py list --limit 40

# 预览高置信安全预选，不写任何状态
python3 scripts/review_tag_mapping_ai.py stage --min-confidence 0.95 --dry-run

# 只标记预选结果，不接受/拒绝候选，也不创建 Tag Alias
python3 scripts/review_tag_mapping_ai.py stage --min-confidence 0.95 --confirm
```

最后打开 Web 的“标签映射候选”区域逐条确认。`accept_equivalent` 只是建议接受为等价别名，`reject` 只是建议拒绝；只有 Web 中明确点击接受才会写入 `tag_aliases`。

批量预选有不可降低的 `0.90` 置信度下限。Equivalent 还必须满足：两侧分类一致且已解决、没有风险标记、原则检查明确证明同一身份且排除上下位词/角色与作品/修饰变体、canonical 方向与当前候选一致。证据或原则版本变化后旧建议自动失效。

`judge` 是唯一会调用外部 Relationship Judge 的命令；`list`、`stage` 和 Web 查看不会发送数据。

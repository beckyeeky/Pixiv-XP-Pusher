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

# 预览 100% 置信建议如何直接处理 WebUI 待审核候选
python3 scripts/review_tag_mapping_ai.py apply --min-confidence 1.0 --follow-ai --dry-run

# 明确确认后原子执行，跳过逐条 WebUI 审核
python3 scripts/review_tag_mapping_ai.py apply --min-confidence 1.0 --follow-ai --confirm
```

默认流程可在 Web 的“标签映射候选”区域逐条确认，或使用 Web 的预览与批量确认。`apply --confirm` 是显式的人工作用门：它会原子接受/拒绝预览中的候选并写入 Tag Alias；`--dry-run` 永远不写数据库。

批量预选有不可降低的 `0.90` 置信度下限。Equivalent 还必须满足：两侧分类一致且已解决、没有风险标记、原则检查明确证明同一身份且排除上下位词/角色与作品/修饰变体、canonical 方向与当前候选一致。证据或原则版本变化后旧建议自动失效。

`apply --follow-ai` 是更主动的显式信任模式。达到门槛的 `equivalent` 在无风险且原则检查完整时按照 AI canonical 方向创建别名，即使一侧分类尚未解决；方向相反时会反向创建别名。达到门槛的 `related` 和 `distinct` 会作为“不应等价合并”而拒绝候选。`uncertain`、带风险标记、证据过期或 canonical 不在候选对中的建议仍会保留待审。预览还会读取当前 Tag Alias；任何会改写已有 canonical 或形成循环的候选分别计为 `alias_conflict` 或 `alias_cycle` 并跳过，不阻止其余计划执行。无人逐条审核时建议使用 `1.0`。

`judge` 是唯一会调用外部 Relationship Judge 的命令；`list`、`stage`、`apply` 和 Web 查看不会发送数据。

# 作品级 Embedding 权重校准

这个工具回答一个具体问题：当前 `semantic_weight` 是否真的让被你点赞的作品排在被你点踩的作品前面？

它只读取当前 Preference Profile、Human-reviewed Normalized Tag 映射、`like` / `dislike` 反馈、用户画像向量和作品向量缓存。它不会调用 Embedding Provider 或 Grounded Judge，也不会修改 `config.yaml`、数据库或推荐结果。

## 运行

在已部署、包含 `config.yaml` 和数据库的项目目录中：

```bash
python3 scripts/calibrate_embedding_weight.py
```

隔离功能工作树可以安全读取正式目录中的配置和数据库；SQLite 会以只读模式打开：

```bash
cd /path/to/Pixiv-XP-Pusher-worktree
python3 scripts/calibrate_embedding_weight.py \
  --config /opt/Pixiv-XP-Pusher/config.yaml \
  --database /opt/Pixiv-XP-Pusher/data/pixiv_xp.db
```

需要保存机器可读报告时：

```bash
python3 scripts/calibrate_embedding_weight.py --json > /tmp/embedding-calibration.json
```

也可以调整候选值和最低样本要求：

```bash
python3 scripts/calibrate_embedding_weight.py \
  --weights 0,0.1,0.2,0.3,0.4,0.5 \
  --min-samples 20 \
  --min-per-class 5
```

## 怎么看报告

- `覆盖率`：同时拥有缓存 Tag、当前模型作品向量和当前画像用户向量的反馈比例。
- `AUC`：随机选择一条 like 和一条 dislike 时，like 得分更高的概率；`0.5` 接近随机，越接近 `1` 越好。
- `分离度`：like 平均分减去 dislike 平均分；正数越大越好。
- `平均排名移动`：相对纯 Tag 分数（weight `0`）的平均名次变化，用来判断语义权重会不会大幅扰动现有排序。
- 行首 `*`：当前配置值。

工具优先比较 AUC，再比较分离度；完全相同时保留最接近当前配置的值，避免没有证据的调整。

## 为什么可能拒绝给建议

以下情况会输出“暂不建议修改”并以退出码 `2` 结束：

- 可用反馈少于 20 条。
- like 或 dislike 任一类少于 5 条。
- 当前画像哈希与缓存用户向量不一致。
- 作品向量缺失，或向量来自另一个 Embedding Model。

画像或模型变化后，先让正常推荐流程运行一次以生成新的用户画像向量，并逐步积累真实反馈。不要为了得到一个建议而降低到只有一两条正负样本。

退出码 `0` 表示样本条件满足并生成候选建议，`2` 表示数据不足，`1` 表示配置或数据库错误。即使退出码为 `0`，工具也只输出建议；修改 `semantic_weight` 仍需人工确认。

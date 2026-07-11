# #34 Complete multi-Judge voting and Danbooru evidence for classification maintenance

- Tracker: https://github.com/beckyeeky/Pixiv-XP-Pusher/issues/34
- Status: OPEN
- Label: `ready-for-agent`
- Parent / related: #31
- Unblocks: #33 (useful Unresolved evidence), better inputs for #32

## Goal

把 #31 里还没验收通过的 Classification Maintenance 缺口补齐：多 Judge 独立投票 + Danbooru 远程证据 + 有界维护选择 + 明确降级。

不是另起炉灶。现有骨架已经有：

- `tag_evidence.py` 共识规则（同一 category 的独立 source >= 2 才接受）
- `danbooru_evidence.py` 按需查询（默认关）
- `TagClassifier.maintain_profile_tags()` 异步维护入口
- `tag_classification_evidence` 持久化

## Why now

本地验证过当前实现体感几乎不变，主因是：

1. 只能配 **1 个** LLM judge，不是 3 个 AI 投票
2. Danbooru evidence 默认关；`profiler.danbooru_*` 不会自动喂给 `tag_classifier.danbooru`
3. 维护几乎没落库：`tag_classification_evidence` 曾观察到为 0
4. 分类缓存大量仍是旧 `feature` / `ip`，Character/Copyright 拆分发挥不出来

## In scope

1. `tag_classifier.judges[]` 多模型配置，每个 Judge identity 一票
2. 有界高影响 profile tag 选择（预算、未决优先、权重）
3. Danbooru 仅查选中标签；缓存与不可用降级
4. 从 `profiler.danbooru_*` 继承凭证
5. 单模型旧配置回退为 1 个 Judge
6. 文档与测试

## Out of scope

- Review Queue UI/CLI 产品化（#33）
- Daily Slate 动机配比补完（#32）
- `profiler.ai` 洗 tag / 同义词合并
- 全量镜像 Danbooru taxonomy

## Proposed config

```yaml
tag_classifier:
  enabled: true
  api_key: ""
  base_url: "https://api.deepseek.com/v1"
  model: "deepseek-v4-flash"
  ttl_days: 30
  batch_size: 50
  concurrency: 5
  maintenance:
    max_tags_per_run: 40
    min_profile_weight: 0.0
    prefer_unresolved_first: true
  judges:
    - name: judge_a
      provider: openai
      api_key: ""
      base_url: "https://api.deepseek.com/v1"
      model: "deepseek-v4-flash"
    - name: judge_b
      provider: openai
      api_key: ""
      base_url: "https://api.openai.com/v1"
      model: "gpt-4o-mini"
    - name: judge_c
      provider: openai
      api_key: ""
      base_url: "https://api.chatai.best/v1"
      model: "gpt-4o-mini"
  danbooru:
    enabled: true
    login: ""
    api_key: ""
    base_url: "https://danbooru.donmai.us"
    timeout_seconds: 15
```

Compatibility:

- `judges` 为空/缺失时，回退现有 `api_key`/`base_url`/`model` 为单 Judge
- `tag_classifier.danbooru.login`/`api_key` 为空时，继承 `profiler.danbooru_login`/`profiler.danbooru_api_key`
- 相同 provider+base_url+model 只算一个 Judge Model

## Acceptance criteria

- [ ] 可配置多个 Judge Models；每个 identity 每 tag 最多一票
- [ ] Maintenance 只刷新有界高影响集合，而不是整组画像标签
- [ ] Danbooru 只服务选中 observed tags，并写入/复用 Tag Evidence
- [ ] Danbooru 关闭、超时、报错时仍可用缓存 + Judge 投票，并有测试覆盖
- [ ] 共识只接受唯一 Tag Category；分歧或仅单 source 机器证据保持 Unresolved
- [ ] 人工 `manual` 决定继续覆盖机器结果
- [ ] 推送 delivery 不阻塞 maintenance，继续读最新 accepted classifications
- [ ] 旧单模型配置仍可作为 one-judge fallback
- [ ] `config.example.yaml` 与 `docs/configuration.md` 写明 multi-judge 与 Danbooru 继承/降级
- [ ] 测试覆盖：多 judge 一致、多 judge 分歧、Danbooru+一 judge 一致、Danbooru 不可用、预算选择、旧配置回退、非阻塞 delivery

## Suggested implementation order

1. `config.py` 规范化 `judges[]` + Danbooru 凭证继承
2. 有界 tag 选择
3. 并行 per-judge 投票，写入稳定 source id
4. Danbooru 失败降级 + cache reuse
5. delivery 继续只消费 accepted cache
6. 测试与示例配置
7. 完成验收后在 #31 评论并视情况关闭 #31

## Related files

- `tag_classifier.py`
- `tag_evidence.py`
- `danbooru_evidence.py`
- `config.py` / `config.example.yaml`
- `push_run.py`
- `tests/test_tag_classifier.py`
- `docs/configuration.md`
- ADR 0002

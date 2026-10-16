# 重构交接状态

更新日期：2026-10-16。继续任务前必须同时核对 git status、当前分支及实际测试日志；本文不代替代码证据。

## 用户目标与授权

- 在现有 beckyeeky/Pixiv-XP-Pusher 新分支重构；不创建第二个仓库，main 保持不变。
- 分支：refactor/persistent-profile-low-quota；基线：8a062eef9fb054d8aa3f2a7512ac2ba30ef6194d。
- 仅保留 Telegram 和 Web 交互；旧 OneBot/AstrBot/email 字段兼容忽略，不自动改为 Telegram 发送。
- 旧配置兼容、旧数据库安全迁移；迁移前 SQLite backup，重复迁移幂等。不得删除无法还原的历史或捏造缺失元数据。
- 用户最新要求：第一步完成后直接推进第二步，不再反复请求“继续”。第二步为提交并推送已验收的重构分支，不是发布 main 或部署生产。
- 不新增视觉模型、共现自动合并、双轨复杂画像、历史种子策略重排等功能。先交付可运行兼容版本。

## 四阶段顺序

1. 本地候选验收：最终补丁复核、完整测试、隔离 Web 启动、统一升级/恢复说明。
2. 提交并推送固定重构分支；main 不变，无正式发布、无生产部署。
3. 在用户旧数据库的一致性副本上演练迁移。尚未取得真实旧库。
4. 演练通过后，另行安排停写、最终备份、迁移、小批真实投递及定时任务切换。

## 当前证据

- 工作树包含未提交、未跟踪候选，不可 reset/clean，不可因普通 git diff 未显示新文件而漏审。
- 主代理最终完整 pytest（无文件排除）：403 passed、16 subtests passed、2 failed；日志 /tmp/xp-step1-final-full.log。两个失败均此前在原始基线复现：
  - test_ai_recommendation_is_audited_and_staged_without_creating_alias
  - test_review_rejects_a_group_that_changed_after_confirmation
- 固定 TZ=Asia/Shanghai；宿主默认 TZ=LCL-8 会使 tzlocal 出错，部署说明需要求有效 IANA 时区。
- Web 已使用临时配置、临时 SQLite、loopback-only uvicorn 启动。登录、dashboard/settings/tags、配置/审核 API 正常；未登录配置 API=401。已停止服务。详见 docs/acceptance-web.md。
- Web 单测35 passed；原 profiler_ai_model 控件/旧标题断言已由主代理对照实际 Provider/Model 界面更新为三种有效模型控件断言，不是添加空标记骗过测试。
- 最新冻结 seed 修复：原始身份与数值不被 alias/block 投影改写。画像测试61 passed；原独立反例6 passed。主已读 /tmp/xp-frozen-implementation.diff。最新修复尚需最终有限复核，不重新扩展设计。
- Telegram getMe、向用户确认的私人测试会话发送单条纯文本成功；用户已确认收到。仅连通性，不代表项目图片/菜单/回调全链验收。
- 尚未真实验证 Pixiv 登录、项目图片投递、生产旧库迁移。不得宣称已完成。

## 凭据与隔离

- 仅记录变量名：PIXIV_XP_TEST_TELEGRAM_BOT_TOKEN、PIXIV_REFRESH_TOKEN。两者已检测存在；不输出值，不写入仓库/日志/文档。
- 测试 chat_id 不写仓库，以会话确认信息或本地临时配置使用；发送新增内容需明确授权范围。
- 测试解释器 /tmp/xp-profile-venv/bin/python。所有测试 DB 必须临时目录，不能依赖默认 data/pixiv_xp.db。
- 初期旧单测曾连接默认路径，当前0字节，来源无法确定；不可称从未触及默认路径。已修测试隔离，勿删除此文件作掩盖。

## 功能与限制

- 持久画像默认不按作品发表日期衰减；旧画像作为不透明基线保留，不声称恢复原始贡献。
- 新收藏/反馈幂等，raw观察与撤销账项分离；曝光ID和反馈元数据独立于可清缓存。
- 自动分类外搜默认关闭，已确认缓存复用、未知冷却；手工外搜每日全局硬预算未实现，不宣称配额功能全部完成。
- 持久模式明确配置IP文件读取失败且折扣非1时，build/feedback写前拒绝；可修复文件、提供真实有效空列表或显式关闭折扣。
- 已使用持久模式的库不允许legacy继续写入；回退需旧程序+升级前备份，不支持无损模式往返。禁止两种模式并发写同库。
- 备份与迁移之间需停写；不声称整个历史schema升级链单事务。
- 关联推送仅确认送达才记曝光；仍需最终复核非法message_id是否采用保守策略。

## 下一动作

1. 有限复核最终 seed 修复与关联发送确认语义；整理统一升级/恢复说明，明确两个基线失败影响。
2. 保存完整验收结果和源码清单；通过后锁定提交SHA，按用户要求推送同名新分支，核验远端SHA。
3. 未通过则仅修复可复现阻断，不再连续增加画像状态机制。未审候选不得发布。

## 第一步验收完成与第二步执行（2026-10-16）

- 最终独立复核允许作为已披露限制的试运行分支提交，非生产升级放行。seed旧独立反例当前6 passed，旧实现4 passed/2 failed；相关回归71 passed。
- 主代理进一步收紧关联投递：非法/缺失/布尔消息号不确认送达；严格断言先1 failed，修复后包含在完整回归中通过。
- 发布前最后完整测试：403 passed、16 subtests passed、2个相同基线失败；/tmp/xp-step1-release-final.log。无文件排除，diff check通过。
- 第一阶段的本地试运行验收完成。真实Pixiv、项目图片/菜单/反馈全链与真实旧库仍为后续验证，不据此宣称生产可用。
- 按用户授权执行提交及推送 refactor/persistent-profile-low-quota；不触碰main或生产部署。统一说明 docs/upgrade-persistent-profile.md。

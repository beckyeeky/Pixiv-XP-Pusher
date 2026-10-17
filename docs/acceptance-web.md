# Web 第一步验收记录

公开报告中的服务器身份、内部路径与作品/画师标识使用占位符；原私有验收材料另行保存且未修改。

## 范围与环境

工作树：`/var/minis/workspace/xp-pusher-review-20261016`；独立基线：`/tmp/xp-profile-baseline`（8a062ee）；解释器：`/tmp/xp-profile-venv/bin/python`。
依赖：FastAPI 0.143.2、Starlette 1.8.0、httpx 0.28.1、Jinja2 3.1.6、tzlocal 5.4.4、APScheduler 3.11.3。
本次仅修改 `web/templates/tags.html`、`tests/test_web_entrypoints.py`，新增本文。既有其他候选修改未清理、未覆盖；未提交或推送。

## 命令与结果

1. `cd /tmp/xp-profile-baseline && /tmp/xp-profile-venv/bin/python -m pytest tests/test_web_entrypoints.py -q > /tmp/xp-web-baseline.log 2>&1`
   结果：4 failed, 31 passed。四个测试名称与主代理完整日志一致，故这四项初始失败均不是候选独有回归。
2. 候选修复后同命令：`/tmp/xp-web-fixed.log`，2 failed, 33 passed。
3. `cd /var/minis/workspace/xp-pusher-review-20261016 && TZ=Asia/Shanghai /tmp/xp-profile-venv/bin/python -m pytest tests/test_web_entrypoints.py -q > /tmp/xp-web-fixed-tz.log 2>&1`
   结果：1 failed, 34 passed。没有删除、跳过或放宽失败断言。

## 四项初始失败归因

- `test_authenticated_single_tag_grounded_judge_uses_translation_and_activates_valid_result`：AsyncMock 的同步 lambda side_effect 返回 coroutine；等待 Mock 后获得的仍是 coroutine，引发 `TypeError: 'coroutine' object is not subscriptable`。改为真实 async side_effect，保持 `classify_tag(tag)` 接口及原断言；通过。此测试替身不覆盖真实分类供应商。
- `test_authenticated_tags_page_contains_the_review_queue_flow`：空配置下模板链式访问 `config.tag_classifier.maintenance`，Jinja2 抛 UndefinedError；末级 default 无法保护缺失的父级。改为逐级 dict.get 并保留原默认值；通过。
- `test_settings_page_does_not_embed_a_provider_secret`：测试未初始化 DB，settings 读取数据库所有的推送时间时抛 `no such table: system_state`。安全测试使用临时真实 DB 并 init_db 后通过（在有效 IANA 时区下）。
- `test_settings_page_renders_with_sparse_config`：同样先被未初始化 DB 阻断；修复夹具及设置时区后暴露原有失配断言 `assertIn("profiler_ai_model", response.text)`。基线模板也没有该控件，候选模板对该控件未作删除。原断言仍保留，当前此项失败，交主代理裁决历史测试契约与实际功能的取舍，不添加无功能标记伪造通过。

没有证据表明四项初始失败源于新 FastAPI/Starlette TemplateResponse API；现有适配器在实际请求中工作。新环境另有 `TZ=LCL-8`：tzlocal 不支持此非 IANA 名称，修复 DB 夹具后 settings 触发 ZoneInfoNotFoundError。以真实 `TZ=Asia/Shanghai` 运行消除此环境阻塞，没有 Mock 调度器或改共享 push_schedule。默认环境下仍需部署方设置有效时区，不能宣称原环境已无条件通过。

## 真实 HTTP 启动

脚本：`/tmp/xp-web-acceptance-run.py`；服务日志：`/tmp/xp-web-uvicorn.log`。
启动：`cd /tmp && /tmp/xp-profile-venv/bin/python /tmp/xp-web-acceptance-run.py > /tmp/xp-web-uvicorn.log 2>&1 &`。
脚本先限制 socket.connect 仅允许 loopback，再导入数据库及 Web；任何 DB 操作之前将 database.DB_PATH 指向 `/tmp/xp-web-acceptance/isolated.sqlite3`，任何 Web 请求之前将 CONFIG_PATH 指向同目录临时 config.yaml；调用真实 init_db。Web 模块导入仅建立 FastAPI、模板和静态目录，不初始化默认 DB、不调用 load_config。没有导入 main/launcher；脚本断言 main/task_manager/notifier 不在 sys.modules。Web 无 startup/lifespan 后台任务，因此不启动 scheduler、notifier 或业务抓取；没有以 Mock 假装启动这些组件。

使用 curl（均为 `http://127.0.0.1:18763`）：
- `GET /` 未登录：200，HTML 含密码输入。
- `POST /login`，临时验收密码：303，Location /dashboard；真实 session cookie。
- 带 cookie `GET /dashboard`：200。
- 带 cookie `GET /settings`：200，含 Pixiv Provider。
- 带 cookie `GET /tags`：200，含标签审核队列。
- 带 cookie `GET /api/config`、`/api/tag-reviews`、`/api/classification-maintenance-status`：均 200。
- 无 cookie `GET /api/config`：401，detail 未登录。
- JSON 检查确认 API 未返回临时密码哈希原值。

curl 使用 `-sS -D /tmp/xp-web-<name>.headers -o /tmp/xp-web-<name>.body` 保留头和正文，首页正文为 `/tmp/xp-web-home.html`；登录 cookie 位于 `/tmp/xp-web-cookies`，仅隔离验收会话，不应当作业务凭证外发。服务已用 kill 停止（PID 已隐去）。未发送 Telegram/Pixiv/分类供应商请求。

## 边界与待裁决

仅验收本机真实 HTTP、模板和临时真实 SQLite；未运行浏览器交互、业务网络、TG 通知、生产配置启动、main 全链后台任务。本次未重新跑完整 pytest；原完整日志的两项 DB 失败不在授权修改范围。基线历史测试自己的默认数据库可能因其原夹具不足而在 `/tmp/xp-profile-baseline/data/` 建立；不是用户业务默认 DB。候选 Web 测试现已在两类夹具中全部隔离 config/DB。

待裁决：是否以当前 AI Provider/Model 实际接口修订历史 profiler_ai_model 测试契约，或确有需求恢复画像 AI 控件；本次不擅自恢复业务功能，也不删除断言。验收结论：Web 在隔离配置、初始化数据库和有效 IANA 时区下可启动并完成上述 HTTP 路径；Web 测试尚非全绿。

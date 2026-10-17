# 隔离副本验收：反馈账本 / 清理留存 / 备份恢复 / Web 本地服务

公开报告中的服务器身份、内部路径与作品/画师标识使用占位符；原私有验收材料另行保存且未修改。

日期：2026-10-17（服务器时间当日下午）。执行模式：**A 只读提取与验证**（未修改、未提交、未推送候选源码；仅在授权 rehearsal 目录写入数据库副本、测试脚本与日志）。**不发布，不称阶段 2 完成。**

## 1. 范围与冻结基线

| 项 | 值 |
|---|---|
| 服务器 | 测试服务器（身份已隐去；经既有授权路径访问，未输出凭据） |
| 副本根 | `<rehearsal-root>/<backup-id>/`（唯一写入范围） |
| 候选代码 | `integration-candidate/`（本地 `integration/persistent-profile-current-main` 未提交候选，含 `application_policy`；非提交、非发布物） |
| 正式安装 | `/opt/Pixiv-XP-Pusher` main `43ec3ad`（未运行、未改、未停重启，未连接生产 DB） |
| 正式服务只读检查 | `pixiv-pusher.service`=active、`pixiv-web.service`=active（验收前、验收后各一次，均 active） |
| 源库哈希 | `baseline.sqlite3` `d435ee0c…466`；`behavior-copy.sqlite3` `22266b42…f1d`；运行前后**哈希不变**（`inputs_unchanged: true`） |

候选源码 `sha256`（服务器副本与本机 `/var/minis/workspace/xp-integration-20261017` 逐项一致，验收前后两次复核未变）：

| 文件 | sha256 |
|---|---|
| database.py | `0eb39206cd0c2b945f98ff545c6303702f2ba83eefad9555b2744300d4adf574` |
| persistent_profile.py | `57084391d03d449517967fa5fcd0933c6c62d82f7d2e29d48ae4dd33794072d0` |
| profiler.py | `f82373f56cb9a2bbf5c8422edfe5a3061e1fbd115f55475b2c1391a58a6f7906` |
| web/app.py | `279d9f0e51d1f594e00ad9d2363065d606085da1a002ed716c9113ff848f4fc2` |
| web/templates/tags.html | `f2ab340b874221d5c53a41b09135b68744f2cc0acf094995579e7e41663976d1` |

结论：服务器候选与本地候选**同源同版本**，不是"最新已提交 main"；本次未产生任何提交/分支/发布物。

## 2. 反馈账本（`feedback-test.sqlite3`）

建库方式：`sqlite3 backup` API 从 `behavior-copy.sqlite3` 另建（非文件拷贝），`integrity_check=ok`，sha256 前缀 `4859c29c4c135034`。全程 `TZ=UTC`，`cache_only=True`，Pixiv 客户端为 `AsyncMock`，从未 await。

测试事件使用**唯一合成 ID**（`990000000001`），标签/画师元数据取自真实 `illust_cache`（`[缓存作品A]`，`[画师A]`，9 标签）。报告中不把该事件表述为真实用户反馈。

| # | 检查 | 结果 | 证据 |
|---|---|---|---|
| 1 | 新 like | PASS | 写入 1 条 `preference_feedback`，`contributions` 按规范身份各 +0.5，observations 落库 |
| 2 | 重复 like = no-op | PASS | 两次调用后 15 张账本表逐行相同（含时间戳），行数仍为 1，返回值无迁移提示 |
| 3 | like→dislike→like 精确撤销 | PASS（附残留） | 数值态完全相等：`xp_profile`/`preference_projection` 权重逐项相等、`preference_feedback` 回到 like、`artist_profile`/`preference_base` 相等；`negative_profile`/`tag_blacklist` 残留 6+6 行**权重为 0.0** 的行（撤销用 `weight=weight-delta`、`MAX(0,…)`，不删行）。差分为 0 值残留，非权重漂移 |
| 4 | 连续 build 不覆盖反馈 | PASS | 连续 3 次 `build_profile(cache_only=True)` 权重完全相同（2626 tag；2625 基线 + 反馈新增[标签A] 0.5），与反馈后即时态一致 |
| 5 | 历史反馈同 action 幂等 | PASS | 取历史反馈作品 `[历史反馈作品B]`（历史 like，`reversible=0`），再 like：15 表逐行相同，无 `migration_policy` |
| 6 | 历史反馈切换政策（独立副本 `feedback-switch-test`，已销毁） | PASS | like→dislike：返回 `Historical contribution retained; new action applies only a post-migration contribution.`；`preference_base` 逐行不变（旧不透明贡献保留）；`artist_profile` 增量 **-1.0**（只含新动作，未反算旧 +1.0）；行变为 dislike/`reversible=1`。**未断言任何历史可撤回** |

未做：真实用户反馈、跨进程并发点击、Web 写入账本（见 §5）。

## 3. 清理留存（`cleanup-test.sqlite3`）

建库方式同上（backup API，`integrity ok`，sha256 前缀 `ffe7ee60bf62fb09`）。调用**现有** `database.cleanup_old_records(days=-1)`，仅对副本，未对正式库执行任何迁移/清理 CLI。

| 检查 | 清理前 → 后 | 结果 |
|---|---|---|
| 曝光 ID 去重集合 | 3847 → 3847（集合相等） | PASS |
| 已曝光作品查询 | 对[缓存作品A]调用 `is_pushed`，清理后仍返回 True | PASS |
| feedback 元数据可读 | 224 → 224 行；222 行含标签；`tags/user_id/user_name` 逐行不变（作者身份保留） | PASS |
| 作品缓存 | `illust_cache` 1752 → 0（元数据仍可读，不依赖缓存表） | PASS |
| 旧收藏不重复计入 | `xp_bookmarks` 3757 → 0，但 `preference_bookmarks` 3757 → 3757，`preference_feedback` 3048 → 3048；重建权重与清理前**逐项相等**（2625 tag） | PASS |

说明：`feedback_metadata` 只覆盖迁移时仍在 `illust_cache` 的 224 个反馈作品（3048 条历史反馈中其余无标签可留存），这是既有设计边界，不是本次回归。

## 4. 备份恢复与重启稳定

| 检查 | 结果 |
|---|---|
| `baseline.sqlite3` → 全新副本（backup API） | PASS：`integrity_check=ok`，30 张表**逐行完全相等**，`xp_profile` 2625 项权重相等；恢复文件 sha256 与源**逐字节相同**（`d435ee0c…`） |
| `behavior-copy.sqlite3` → 全新副本 | PASS：`integrity_check=ok`，44 张表**逐行完全相等**（含 `exposure_ids 3847`、`feedback 3048`、`illust_cache 1752`、`xp_bookmarks 3757`、`preference_feedback 3048`、`preference_bookmarks 3757`、`preference_anchor 2625`）；字节 sha 因页布局不同为 `a0e53183…`，逻辑内容一致 |
| 另起进程重启后 cache_only 稳定 | PASS：独立 `python3.11` 子进程（新解释器/新连接）连续 2 次 `build_profile(cache_only=True)`，与前一进程记录的 2626 项权重**完全相等**，两次 build 亦相等（rc=0） |

## 5. Web 隔离副本（实际本地 HTTP 服务）

部署：`web-rehearsal/`（配置 0600，结构取自 `source-config-snapshot.tgz` 内 `config.yaml`，**未输出任何凭据**）；DB 为 `behavior-copy` 的 backup 副本。启动前 patch `database.DB_PATH`、`web.app.CONFIG_PATH`；socket 层仅允许 `127.0.0.1/::1`（运行期 `blocked_sockets=[]`，即无外联尝试）；未启动 `main`/`task_manager`/`notifier`/`scheduler`，uvicorn 单进程。临时密码仅存 0600 文件并已删除，未打印密码或 Cookie。配置改动：业务凭据置空、`notifier.types=[]`、`tag_mapping.enabled=false`、`tag_classifier.enabled=false`（含 grounded_judge 的 tavily/brave 列表清空）、`web.password`=临时口令哈希、`web.port=18764`。

| 请求 | 结果 |
|---|---|
| `GET /health` | 200 |
| `GET /`（未登录） | 200，登录表单存在 |
| `GET /dashboard` 无 session | 302 → `/` |
| `GET /api/stats`、`POST /api/feedback` 无 session | 401、401 |
| `POST /login` 错误口令 | 200，页面显示"密码错误" |
| `POST /login` 正确口令 | 303 → `/dashboard`，下发 `session_id`（未输出） |
| `GET /dashboard` / `/settings` / `/tags` | 200 / 200（120381 B）/ 200（70549 B），均含 `<title>` |
| `GET /api/stats` / `/api/xp-profile` / `/api/runtime-status` / `/api/config` | 200（JSON）/ 200（101964 B）/ 200 / 200 |
| `/api/config` 凭据脱敏 | PASS：对真实凭据值逐项比对，命中 0 项，口令哈希亦未出现 |
| `GET /logout` 后 `GET /api/stats` | 307，随后 401 |

服务结束后按自身进程 `TERM` 终止；`ss` 确认 `127.0.0.1:18764` 已关闭，进程消失；`server.log` 无 password/token/api_key 字样（40 条仅为旧渠道配置忽略告警）。

### 已确认的语义缺口（不修代码，仅报告）

`POST /api/feedback` 仅调用 `database.record_feedback`（写 `feedback` 表 + 触发器补 `exposure_ids`/`feedback_metadata`），**不调用** `XPProfiler.apply_feedback`/`ledger.feedback`。隔离副本实测两次（真实缓存作品 `[缓存作品A]`、`[作品C]`，均无历史反馈）：

- 返回 `200 {"success":true,...}`；`feedback` 行 3048→3049→3050；`feedback_metadata` +1/+1；`exposure_ids` +0/+1（前者已在推送历史中）。
- `preference_feedback` 恒为 3048 行，`xp_profile` 哈希不变，`preference_projection` 行数不变。

即：**Web 反馈不会进入偏好账本、不改变画像权重，也不会触发 like/dislike 的撤销/切换语义**。该行为与旧 main 相同（非本次整合引入），但意味着 Web 侧"反馈画像"链路是不完整的；本报告不宣称 Web 反馈画像通过，仅确认接口返回、鉴权、页面与账本"未变"这一事实。

## 6. 发现与风险

1. **Web 反馈语义缺口**（上文）：产品语义问题，需主代理裁决是否属已知设计；修复需单独任务与授权，本任务未改源码。
2. **撤销后 0 值残留行**：`negative_profile`/`tag_blacklist` 在精确撤销后保留 6 行 0 权重/0 计数记录。数值正确、投影正确，但"逐行等于撤销前"不成立；若后续以行数/存在性判断黑名单会误判，建议记录为已知残留语义（未修改）。
3. **元数据覆盖有限**：`feedback_metadata` 224/3048，仅覆盖迁移时仍在缓存中的反馈作品；清理后其余历史反馈无标签可读。
4. **日志潜在泄露**：`web/app.py:163` 在 INFO 级打印 `web.password`（哈希）。本次以 WARNING 级运行未落盘，但默认 INFO 部署会写入口令哈希，建议后续单独评审（未修改）。
5. 服务器磁盘资源足够，本次新增约 0.9G 副本/恢复文件；后续演练仍需留意磁盘。
6. 正式服务在验收前后均 active，未重启；未连接生产 DB、未发 Telegram、未访问 Pixiv/业务网络。

## 7. 未验证边界

- 真实 Pixiv/Telegram/宿主生产流量、真实用户点击时序、跨进程并发写同一库。
- 正式库迁移 CLI（`scripts/db_maintenance.py cleanup` 等）**未执行**，按授权只在副本调用库函数。
- 历史已删除/未知来源权重不可恢复，未做任何"撤回历史"断言。
- Web 仅验证登录/dashboard/settings/tags 与列出的只读 API；未点击设置保存、未触发出站同步、未跑 18764 之外的端口。
- 稳定性只覆盖 cache_only 构建与单进程重启，不含强杀、磁盘耗尽、断电。

## 8. 证据位置（均在副本根，服务器）

```
rehearsal_feedback_cleanup.py        验收脚本（13 项，2 次修正后 13/13 PASS）
results-feedback-cleanup.json        逐项结果/哈希/服务状态/输入未变证明
feedback-test.sqlite3                反馈账本副本（4859c29c…）
cleanup-test.sqlite3                 清理副本（ffe7ee60…）
baseline-restored.sqlite3            基线恢复副本（与源逐字节相同）
behavior-copy-restored.sqlite3       迁移副本恢复（逐行相同）
web-rehearsal/{run.py,probe.py,config.yaml,web-copy.db,web-probe.json,state.json,server.log}
diag.py                              残留行最小复现（差异诊断）
```

本机报告：`/var/minis/workspace/xp-integration-20261017/docs/rehearsal-results.md`。

## 9. 待主代理裁决

1. Web `/api/feedback` 不写账本：按已知缺口记录，还是立项修复（需新授权与新回归）。
2. 撤销后 0 值残留行是否需要清理（会改动账本契约，须独立评审）。
3. `web/app.py` INFO 日志打印口令哈希是否纳入候选修复。
4. 是否用 `behavior-copy-restored.sqlite3` 作为下一阶段（如清理/长稳）演练起点；本轮未提交、未发布、不称阶段 2 完成。

## 10. 两项已确认缺口的受限补丁回归（2026-10-17，未提交）

授权范围仅 `database.py`、`web/app.py`、新增 `tests/test_web_feedback.py` 与本文件追加；未修改画像权重机制、预算、推荐功能，未提交/推送/SSH/访问业务网络或默认 DB。

### 10.1 代码变更

- `web/app.py`：删除 `/` 路由中打印完整 `config` 与 `web_cfg/password` 的两条 INFO 日志（原第 161、163 行），不降级为 DEBUG 继续泄漏。
- `database.py`：新增只读 `get_feedback_metadata(illust_id)` 与 `_parse_feedback_tags`。从 `feedback_metadata.tags` 解析非空字符串列表并去重；缺失、非列表、JSON 损坏或无有效标签均返回 `None`，不写库。
- `web/app.py`：`POST /api/feedback` 不再先调用 `db.record_feedback`（消除账本之外的独立写）。仅从 `illust_cache` 或 `feedback_metadata` 构造本地 `Illust`，缓存 JSON 损坏时以元数据兜底；两处都无有效标签返回 422 且不写任何表。作者缺失保持 `user_id=0/user_name=""`，不虚构。
- 复用 `XPProfiler(client=None).apply_feedback`，不调用 `setup_services`/`login`/Pixiv/notifier，无网络。配置规范化与 `task_manager.setup_services` 对齐：默认 `persistent`、拒绝未知 `profile_mode`、传入 `stop_words/boost_tags` 及已弃用 IP 参数（预检固定按 1.0 处理）。
- 持久库在 `legacy` 模式下被现有 preflight 拒绝时返回 409 与恢复备份提示，不返回堆栈；其他写失败返回 500，不伪造 success。legacy 在未使用持久账本的库上仍走原有 `XPProfiler` 政策。
- 成功响应保持 `{"success": true, "message": "已记录对作品 <id> 的 <action>"}`；鉴权 401、无效 action 400 均先于任何写。

### 10.2 红灯与绿灯

- 红灯（修复前，真实 SQLite/TestClient）：`TZ=UTC /tmp/xp-profile-venv/bin/python -m pytest -q tests/test_web_feedback.py`，`/tmp/xp-web-feedback-red.log`：11 failed、2 passed。失败原因与两个缺口一致：反馈未入账本（200 而非预期账本变化/422/409）、INFO 泄漏虚构 secret 与口令哈希；401/400 已符合。
- 绿灯（修复后）：同一命令，`/tmp/xp-web-feedback-green4.log`：14 passed。新增用例覆盖：首次 like 真实 `xp_profile`/`preference_feedback` 变化；重复 like 全业务表逐表不变；like→dislike→like 按账本实际贡献精确回到首次 like 数值（期望值由记录贡献计算，未硬编码惩罚）；metadata-only 及缓存清理后元数据幂等；缓存 JSON 损坏时元数据兜底；无有效标签 422 且全表不变；未登录 401、无效 action 400 均无写；历史 `reversible=0` 反馈切换保留不透明基线且只计新动作；无效模式拒绝；legacy 持久库 409、干净库 legacy 仍可用；`/` INFO 日志不含虚构 secret/口令哈希。

### 10.3 未验证边界

- 未在真实生产库副本或宿主 Web 服务上运行；server 端行为仍以本地隔离库/TestClient 为限。
- 按授权把“已有 raw 账项”解释为 `feedback_metadata` 留存记录；缓存与元数据都无有效标签时一律 422，没有从 `preference_feedback_observations` 反推标签的第三来源。
- legacy 写入仍为原有多步非事务路径，本次只保证其既有政策与错误映射，不做无损切换。

## 11. 修复后最终 HTTP 验收（2026-10-17，仅副本）

本地 manifest `/tmp/xp-source-manifest-<timestamp>.txt`（sha256 `c11dccd1…`，139 文件）；`database.py` `f226edfa…`、`web/app.py` `71a65988…`。服务器 `http-final/` 由 `integration-candidate` 复制，`diff -rq` 仅这两个文件不同，hash 与本地逐字节一致。

`behavior-copy.sqlite3` 原 sha256 `22266b42…` 未变；`web-final.sqlite3` 经 backup API 新建，初始 `a0e53183…`，`integrity_check=ok`。`web-final/` 配置 0600，socket 仅 `127.0.0.1:18764`，`client=None`，日志 WARNING；临时口令文件已删。

实际命令：`python3.11 web-final/probe_final.py` → **22/22 PASS**（`web-final/probe-final.json`，sha256 `3060da94…`）。未登录 `/api/stats`、`/api/feedback` 均 401；错密 200；正确 303→`/dashboard`；dashboard/settings/tags 均 200；stats/xp-profile/runtime-status/config 均 200；`/api/config` 命中 0 个真实凭据、无口令哈希；logout 307 后 401。

反馈以 cache 样本复制到合成 `990000000001`：like 200，`preference_feedback` like/`reversible=1`、6 条 observations、`artist_delta=+1`；重复 like 全库快照逐表相同（sha `acdc1b81…`）、账本/观察各 1 行；dislike 200，observations 全负、negative 权重和 +1.4899、blacklist +6、`artist_delta=-1`；再 like 200，`xp_profile` 等 5 张权重表 max diff **0.0**，contributions/observations 与首次 like 相同（残留 0 值行 6+6）。仅 metadata（`990000000002`，无 cache）200 且入账；无 cache/无 metadata（`990000000003`）422，全库快照不变、无该 ID 任何行。

对照旧 13 项：旧 #3 重复 no-op、#4 like→dislike→like 精确撤销（此前 Web 端绕过账本）本次 HTTP 已满足，权重 max diff 0.0；#6/#7 共用账本未改、未重跑；#8–#13 按指示未重跑，不宣称。

隔离：未发 TG、未请求 Pixiv、未启 scheduler；`server.log` 41 行全 WARNING，password/token/secret 命中 0、non-loopback 0；自身进程已 `TERM` 终止，18764 关闭；`pixiv-pusher`/`pixiv-web` 均 active 且启动时间早于本轮、未重启。

未验证：真实用户流量、并发点击、生产库。待裁决：撤销后 0 值残留行是否清理。

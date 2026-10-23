# Web 设置精简：persistent 画像隐藏不生效旧参数（本地受限补丁）

- 日期：2026-10-23
- 工作副本：`/var/minis/workspace/xp-web-cleanup`（生产 `2332940098e8ef3042080e5cf279c18941b6cdc6` 的 `git archive` 副本，无 `.git`）
- 模式：B 受限补丁（未提交、未推送、未部署、未联网）
- 测试解释器：`/tmp/xp-profile-venv/bin/python`（Python 3.12.15），`TZ=Asia/Shanghai`
- 证据目录：`/var/minis/workspace/xp-web-cleanup-evidence/`
  - `web-settings-cleanup.patch`（模板与测试的完整 unified diff）
  - `baseline-tests.log`、`after-tests.log`、`render_check.py`

## 1. 结论

1. `profiler.profile_mode` 默认 `persistent` 时，设置页「画像分析」不再展示「时间衰减半衰期(天)」「标签饱和阈值」两个旧算法参数；两者保留在 DOM 中并带 `hidden`，保存时 JS 完全不读取，因此历史值不会被重置。
2. 「探索率」经核查由 Fetcher 消费、与画像模式无关，**保留可见**，并在控件下补充一行说明。
3. `legacy` 模式完全兼容：两个参数照常可见、可编辑、可保存。
4. 历史记录瀑布流 Gallery 未改动：`gallery.html`、`base.html`、`web/app.py`、`web/settings_editor.py` 哈希与基线一致；新增一条 Gallery 渲染回归测试。
5. 审计确认 `web/templates/settings.html` 与 `web/app.py.bak` 为死文件后删除（原文件可从副本内 `source.tar` 恢复，SHA-256 已记录）。

## 2. 参数生效性核查（代码证据）

| 参数 | 实际消费点 | persistent 下是否生效 | 处理 |
| --- | --- | --- | --- |
| `profiler.profile_mode` | `config.py:159-162`（默认 `persistent`）、`profiler.py:60-64` | 决定分支 | 仅用于模板/JS 判定 |
| `profiler.time_decay_days` | `profiler.py:543`（`math.exp(-days_ago / self.time_decay_days)`） | **否**：persistent 分支在 `profiler.py:315-320` 直接 `persistent_profile.build(...)`，不进入旧权重计算 | 隐藏 |
| `profiler.saturation_threshold` | `profiler.py:391`（饱和度停用词）、`profiler.py:534`（`df_ratio` 归零） | **否**：同上，均在 legacy 权重路径 | 隐藏 |
| `profiler.discovery_rate` | `task_manager.py:423` / `push_run.py:203` 传入 `PixivFetcher`，`fetcher.py:30,44,211,304` 消费 | **是**（两种模式均生效） | 保留可见 + 说明 |
| `profiler.scan_limit` / `include_private` | `profiler.py:197-208,275,291,305`（抓取阶段） | 是 | 未改动 |
| `profiler.top_n` | `push_run.py:163,210` → `XPProfiler.get_top_tags` | 是 | 未改动 |

补充：`web/app.py:854-867` 仍会把 `time_decay_days`/`saturation_threshold` 传入 `XPProfiler` 构造器；persistent 分支不读取它们，属于既有兼容行为，本次不改公共契约。

## 3. 当前持久画像与 like 语义（按代码事实）

- 基础：`persistent_profile.py:79-86` 从 `preference_bookmarks`（`imported=0`）按 canonical 标签去重，每作品每标签 +1.0；旧库 `preference_base` 权重作为不透明基线并入，不反推历史贡献。
- like：`persistent_profile.py:292` 对作品的有效标签各记 `feedback.like_boost`（默认 0.5，`web/settings_editor.py` 默认值），`_project`（`:89-102`）按 canonical 合并，同一 canonical 只计一次。
- 重复 like：`persistent_profile.py:259-263` 若该作品已有相同 action 直接返回，不新增贡献——**重复 like 不叠加画像**。
- 画像权重不使用作品发表日期衰减（模块 docstring `persistent_profile.py:1-3`；`_project` 无日期项）。
- 以上只是权重口径说明，**不承诺任何推荐比例或命中率**。

## 4. 变更清单

### 4.1 `web/templates/settings_v2.html`（+27/−11 行）

- 在 `#section-profiler` 内新增 `{% set profiler_mode = (config.profiler.profile_mode | default('persistent')) | lower %}`。
- 新增模式说明 `#profilerModeHint`：persistent 说明「有效标签基础 + like_boost、不随发表年龄衰减、重复 like 不叠加」；legacy 说明两参数参与旧算法。
- 将两个旧参数包进 `<div id="profiler_legacy_params"{% if profiler_mode != 'legacy' %} hidden{% endif %}>`（输入控件本身保留，值仍随 `initialConfig` 回传）。
- 探索率下方新增说明文案：参与 XP 搜索组合、与画像模式无关、始终生效。
- JS：`collectConfig()` 新增 `const profilerMode = String(profiler.profile_mode || 'persistent').toLowerCase();`；删除无条件的 `time_decay_days`/`saturation_threshold` 读取，改为仅在 `if (profilerMode === 'legacy')` 内读取，并对控件存在性做判空保护。

关键 diff：

```diff
@@ -596,6 +596,12 @@
             <h3 class="section-title">👤 画像分析</h3>
+            {% set profiler_mode = (config.profiler.profile_mode | default('persistent')) | lower %}
+            {% if profiler_mode == 'legacy' %}
+            <p class="help-text" id="profilerModeHint">当前为 legacy 画像模式：时间衰减与饱和阈值参与旧算法权重计算。</p>
+            {% else %}
+            <p class="help-text" id="profilerModeHint">当前为持久画像：权重来自有效标签基础与 like 反馈的 like_boost 加成，不随作品发表年龄衰减；对同一作品重复 like 不会重复叠加画像权重。</p>
+            {% endif %}
@@
-                    <div class="form-group">
-                        <label class="form-label">时间衰减半衰期(天)</label>
-                        <input type="number" class="form-control" id="profiler_time_decay_days"
-                               value="{{ config.profiler.time_decay_days | default(180) }}">
+                    <div id="profiler_legacy_params"{% if profiler_mode != 'legacy' %} hidden{% endif %}>
+                        <div class="form-group">
+                            <label class="form-label">时间衰减半衰期(天)</label>
+                            <input type="number" class="form-control" id="profiler_time_decay_days"
+                                   value="{{ config.profiler.time_decay_days | default(180) }}">
+                        </div>
+                        <div class="form-group">
+                            <label class="form-label">标签饱和阈值</label>
+                            <input type="number" step="0.01" class="form-control" id="profiler_saturation_threshold"
+                                   value="{{ config.profiler.saturation_threshold | default(0.5) }}">
+                        </div>
                     </div>
@@
     const profiler = ensureObject(cfg, 'profiler');
+    const profilerMode = String(profiler.profile_mode || 'persistent').toLowerCase();
@@
     profiler.scan_limit = asInt(document.getElementById('profiler_scan_limit').value, profiler.scan_limit || 1000);
     profiler.discovery_rate = asFloat(document.getElementById('profiler_discovery_rate').value, profiler.discovery_rate || 0.1);
-    profiler.time_decay_days = asInt(document.getElementById('profiler_time_decay_days').value, profiler.time_decay_days || 180);
-    profiler.saturation_threshold = asFloat(document.getElementById('profiler_saturation_threshold').value, profiler.saturation_threshold || 0.5);
     profiler.top_n = asInt(document.getElementById('profiler_top_n').value, profiler.top_n || 20);
     profiler.include_private = document.getElementById('profiler_include_private').checked;
+    // Legacy-only weights are hidden in persistent mode; keep their stored
+    // values untouched and never read controls that are not rendered.
+    if (profilerMode === 'legacy') {
+        const decayField = document.getElementById('profiler_time_decay_days');
+        const saturationField = document.getElementById('profiler_saturation_threshold');
+        if (decayField) profiler.time_decay_days = asInt(decayField.value, profiler.time_decay_days || 180);
+        if (saturationField) profiler.saturation_threshold = asFloat(saturationField.value, profiler.saturation_threshold || 0.5);
+    }
```

### 4.2 测试

- `tests/test_web_entrypoints.py`（+4 用例）
  - `test_persistent_profile_hides_legacy_weight_parameters`：持久模式下容器带 `hidden`、两个旧控件仍在、探索率仍在、模式文案存在。
  - `test_legacy_profile_keeps_legacy_weight_parameters_visible`：legacy 模式无 `hidden`。
  - `test_settings_save_only_reads_legacy_parameters_in_legacy_mode`：两个字段的 `getElementById` 各仅出现一次且都在 `if (profilerMode === 'legacy')` 块内，不存在直接 `.value` 读取。
  - `test_gallery_page_keeps_waterfall_and_pagination`：`/gallery` 渲染含 `galleryGrid`、条目标识、瀑布流切换按钮、分页、自动加载哨兵与布局存储键。
- `tests/test_settings_editor.py`（+1 用例）
  - `test_persistent_profile_save_keeps_hidden_legacy_weights`：保存载荷省略两个旧字段时，服务端 merge 保留 `90`/`0.42` 原值（服务端“不重置隐藏旧值”保证）。

### 4.3 本报告

- `docs/web-settings-cleanup-report.md`（仅文档，不参与运行）。

### 4.4 删除（审计后确认无引用）

| 文件 | 原 SHA-256 | 审计结果 |
| --- | --- | --- |
| `web/templates/settings.html` | `38ba2cb629ccb26722718e0dc9992f7b759ebd983264eda8139982c7ca8c3454` | 全仓库仅被 `web/app.py.bak:228` 引用；无路由/测试引用 |
| `web/app.py.bak` | `1c4596929ab8214ff49764c5d32d2185f45251a5b560b3a4a2382af25be78dbe` | 无 import/入口/测试引用；`docs/issues/backlog.md:8` 已将“移除陈旧备份文件”标记为完成 |

删除前检索命令：`rg -n "settings\.html|app\.py\.bak" -uuu --glob '!source.tar' .`，仅上述两处（`source.tar` 内为归档原件）。两文件可从副本内 `source.tar` 原样恢复。

## 5. 测试与证据

### 5.1 基线（未改动源码）

```text
TZ=Asia/Shanghai /tmp/xp-profile-venv/bin/python -m pytest -q
=> 6 failed, 489 passed, 38 subtests passed（/tmp/xp-web-cleanup-baseline.log）
```

6 个失败全部在 `tests/test_integration_current_main.py`，原因为副本无 `.git`：

```text
subprocess.CalledProcessError: Command '['git', 'show', '43ec3ad22d249778c0f0961453b78fdf6439f07d:database.py']' returned non-zero exit status 128.
fatal: not a git repository (or any of the parent directories): .git
```

属环境缺 `.git` 对象，非本次改动；未修改或跳过这些测试。

### 5.2 红（原始基线 + 新测试）

将新测试文件复制到 `source.tar` 解包副本 `/tmp/xp-web-red`：

```text
pytest -q tests/test_web_entrypoints.py -k "persistent_profile or legacy_profile or settings_save_only or gallery_page_keeps"
=> 3 failed, 1 passed（persistent / legacy / JS 三个新用例失败，gallery 回归用例通过）
```

失败原因分别为：基线无 `profiler_legacy_params` 容器、无模式文案、JS 仍无条件读取旧字段；Gallery 用例在基线与新版本均通过，证明未改变 Gallery 行为。

### 5.3 绿（修改后）

```text
pytest -q tests/test_web_entrypoints.py -k "persistent_profile or legacy_profile or settings_save_only or gallery_page_keeps"
=> 4 passed

pytest -q tests/test_settings_editor.py
=> 18 passed

pytest -q ...::test_webui_javascript_is_syntax_valid_when_node_is_available ...::test_active_templates_do_not_use_native_alert_or_confirm
=> 2 passed（node --check 通过）
```

最终全量：

```text
TZ=Asia/Shanghai /tmp/xp-profile-venv/bin/python -m pytest -q
=> 6 failed, 494 passed, 38 subtests passed（/tmp/xp-web-cleanup-after-final.log）
```

与基线逐项对比：失败集合完全相同（6 个 `.git` 环境失败），新增 5 个用例全部通过，**零新增失败**。

### 5.4 未改动文件的哈希核对

| 文件 | 与基线 |
| --- | --- |
| `web/templates/gallery.html` | 一致 `c2964d43…61718f0` |
| `web/templates/base.html` | 一致 `7dbd67db…22e0ec9` |
| `web/app.py` | 一致 `71a65988…7af37f5e4` |
| `web/settings_editor.py` | 一致 `38e17967…6f4333dc` |
| `web/templates/settings_v2.html` | 变更后 `ca60d791…a77a6a9c` |
| `tests/test_web_entrypoints.py` | 变更后 `e11a438e…c834c31bf` |
| `tests/test_settings_editor.py` | 变更后 `1e3819e1…c837126` |

### 5.5 渲染抽查（实际 Jinja 输出，非仅字符串断言）

```text
persistent: hidden_attr=True
  <p class="help-text" id="profilerModeHint">当前为持久画像：…不随作品发表年龄衰减；…重复 like 不会重复叠加画像权重。</p>
  <input type="range" id="profiler_discovery_rate" ...>
  <div id="profiler_legacy_params" hidden>
  <input type="number" class="form-control" id="profiler_time_decay_days" ...>
legacy: hidden_attr=False
  <div id="profiler_legacy_params">
```

`#section-profiler` 渲染后 `<div>` 开闭均为 16，结构平衡。

## 6. 已保留功能

- Gallery 历史记录瀑布流：`gallery.html` 未改；`/gallery` 分页、瀑布流切换、收藏筛选、自动加载哨兵与 `gallery-layout-mode` 本地存储均保留（新增回归用例覆盖首屏与关键节点）。
- `base.html` 导航、主题、确认弹窗、公共 JS 未改。
- 设置页其余分区（Provider/Model/AI/通知/筛选/抓取等）未改。
- legacy 画像模式的两个旧参数行为、保存路径完全保留。
- 服务端配置合并语义未改；`web/settings_editor.py`、`web/app.py` 未修改。
- 数据库、生产配置、凭据、SSH、部署脚本未触碰；未联网 GitHub。

## 7. 未验证边界与限制

1. 未在真实浏览器中执行 `collectConfig()`（无 DOM 的端到端 JS 测试）；对“保存不读取隐藏控件”的证据为：结构性断言（两字段读取仅存在于 legacy 分支）+ `node --check` 语法校验 + 服务端 merge 单测。评分不能等同于真实浏览器行为验证。
2. 未启动 uvicorn 做登录态 HTTP 验收；渲染抽查使用 FastAPI `TestClient` 并桩掉 `PushScheduleModule`。
3. 未执行真实 `POST /api/settings` 全链路（未提交真实配置文件写入）；服务端保留语义由 `apply_settings_payload` 单测覆盖。
4. 基线 6 个 `.git` 依赖测试仍失败，未修复、未跳过；需在含 `.git` 的完整仓库复核。
5. 未验证真实旧库迁移、真实 Pixiv/Telegram 投递；本次不涉及。
6. 删除的两个文件在副本内无 Git 历史可回退，仅可经 `source.tar` 恢复（哈希已记录）。
7. 本次为渲染抽查临时编写的脚本未隔离数据库路径，于 12:08 在副本 `data/pixiv_xp.db` 生成了一个 **0 字节、无任何表** 的默认库文件（位于 `data/`，不在生产环境）。按交接文档要求未删除该文件，特此披露；正式测试均使用临时 DB。

## 8. 待主代理裁决

1. 是否接受删除 `web/templates/settings.html`、`web/app.py.bak`（本报告 4.4 已给出审计依据与恢复方式）。
2. 是否需要在 persistent 模式下同样标注 `scan_limit`/`top_n`/`include_private` 的适用范围（本次按任务边界未动，避免误删生效参数）。
3. 模式说明文案是否需再精简或调整措辞（当前明确不承诺推荐比例）。
4. 本补丁保持未提交状态；是否进入提交/发布由主代理决定，本代理未执行任何提交、推送或部署。

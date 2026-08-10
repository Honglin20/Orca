# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前：nas-supernet-v2 新 workflow 实现——实现 + 自验完成，code-reviewer 后台运行中

**任务**：SPEC `docs/specs/2026-08-11-nas-supernet-v2.md`（REVIEWED-PASS，21 issue 闭环）——
新建 `nas-supernet-v2` workflow（8 agent + 0 terminate），根治 v1 四类问题（terminate 在 in-session
崩 / DDP 脚本坏 / 伪 agent 浪费 / 生成节点过重）。

**状态**：**实现 + 自验完成，等待 code-reviewer 后台审查结果**：C1-C6 全部实现，
`tars validate` 0/0，v1 零改动，bash -n 全过，Python 语法全过，dev residue 清零。
- C1: ns2_flatten (Step 0-3) + ns2_expand_supernet (Step 4-7) 拆分
- C2: select 合并进 ns2_run_search + 失败安全网（emit falsy JSON 禁 node_failed）
- C3: 3 子代理并行生成 + search_record_schema.json 共享 schema
- C4: 单设备默认（plain python3 / AMP=false / 条件 DDP / guarded sync_random_seed）
- C5: 7 个 check_*.sh 固化脚本 + .user_pkg marker
- C6: ns2_report 单一终端 reporter（零跨节点 output 引用 + 磁盘判终态 + workflow outputs 全读 ns2_report）

**必读**：
- SPEC `docs/specs/2026-08-11-nas-supernet-v2.md`（§3 C1-C6 + §10 闭环日志）。
- release note `docs/releases/2026-08-11-nas-supernet-v2.md`。
- yaml `workflows/nas-supernet-v2.yaml`（8 节点拓扑 + 路由）。

**待办**：
- [ ] code-reviewer 后台审查完成后修全部反馈
- [ ] test-agent headless 双项目 E2E（下一阶段）

---

## 历史：详情页返回主页导航——已完成（SPA navigate + Playwright 真机闭环）

**任务**：详情页 TopBar 返回按钮 `window.location.href`（整页刷新，"没法后退"体感）→ SPA navigate。
**状态**：**完成**：`TopBar.tsx` 改 `useNavigate` + `navigate("/")`（react-router v6.28，多处已用，
安全）。附带修 test fixture：tape topology 缺 `routes` → 详情页 `AgentsRail` 的 `selectAgentGroups`
迭代 `def.routes`（`selectors.ts:171`）崩白屏 `routes is not iterable`；真实 run topology 有 routes
（workflow 定义），非产品 bug——`test_playwright_runlist` 只验进详情页 URL 不验渲染故漏网。新增
`test_back_navigation.py`（真机断言返回主页，topology 补 `routes: []`）。Playwright 真机 13 passed
（WSL 无 sudo 装 libnspr4 等系统库，见 `wsl-playwright-no-sudo-deps` memory + home-list-lazy-index release）。
**必读**：release note `docs/releases/2026-08-10-detail-back-nav.md`；`TopBar.tsx`（navigate）。

---

## 历史：CAC 权限审批/yolo 生效——已完成（permission hook 补 CAC PID 回溯 + code-reviewer 闭环）

**任务**：CAC（CC 换皮 `codeagentcli`）PermissionRequest hook 已随 cc+cac 家族装好，但 yolo / web
审批卡从不生效。根因：CAC 不注入 `CLAUDE_CODE_SESSION_ID`（sessionId 存内存变量），hook
`_resolve_session_id` 三路（env > env > stdin）皆空 → broker `_resolve_active_run` miss → `ask` →
`if self._yolo`（resolver-hit 门后）永不执行。

**状态**：**完成 + code-reviewer 一轮闭环（0 MUST-FIX / 3 SHOULD-FIX 全修）**：hook 内嵌
`_cac_session_id_from_pid`（行为等价 `_hostenv`/`cc_nudge.sh` 同款，纯 stdlib 无新 import），
`_resolve_session_id` 加 `CAC PID 回溯 > stdin` 一级（对齐 `host_session_from_env`，取值 == tape
`data.host_session` → broker 双键命中）+ best-effort `try/except` 包裹（防 `UnicodeDecodeError` 崩 hook）。
SPEC §3.1 + yolo R2 同步。验证：hook+install 30 passed；更广 in_session+approval_broker 621 passed /
8 failed（全 pre-existing，grep 证零命中本改动符号）。

**必读**：
- release note `docs/releases/2026-08-10-cac-permission-yolo.md`。
- `orca/iface/in_session/templates/orca-permission-hook.py:_cac_session_id_from_pid` +
  `_resolve_session_id`（CAC PID 回溯 + 优先级 + fail-soft 包裹）。
- DRY 漂移闸门 `tests/iface/in_session/test_orca_permission_hook.py:test_cac_pid_walk_drift_gate_against_canonical`
  （守 `_hostenv` ↔ hook 守恒常量同步，防本 bug 复发）。

**遗留**（环境限制，如实）：无 CAC 真机——`/proc` 回溯体执行覆盖率为零，取值与 tape 一致靠 inspection
逐字段比对 + 漂移闸门守恒常量（`sessionId` ASCII 故编码差不产生分歧）。§9 #2 spike 降级为"真机确认"
（根因已代码层堵死）。另两块未做：opencode `--auto` 固化 / opencode↔nga 自动探测。

---

## 历史：卡片事件数对齐 log + 图表数字段——已完成（code-reviewer + test-agent e2e 真机闭环）

**任务**：SPEC `docs/specs/2026-08-10-card-event-log-align.md` v3（两轮 spec-review FAIL→PASS-ready）
——主页卡片"事件数"在别处服务器显示 0（in-memory 分支硬编码 0）+ 语义错（全量而非 log 行数）+
缺图表数字段。

**状态**：**实现 + 12 单测（AC4-AC7 + F3 fallback + in-memory 异常路径）+ code-reviewer 一轮
闭环（0 MUST-FIX，2 SHOULD-FIX 全修：AC4 fixture 精确匹配 SPEC + in-memory 异常路径补测）**。
逐字实现 SPEC v3 五块契约：
- §3.1 双分支 log_event_count（fast-path + full-parse 都查白名单；NEW-1 placement：fast-path
  type check 放 `if m:` 块内 `count += 1` 之后）。新增 `_LOG_EVENT_TYPES`（26 类型，U1 ↔ 前端
  `classifyLogLevel`）+ `_META_TYPE_RE` regex。
- §3.2 chart_count 去重（isinstance 守卫匹配前端 `typeof === "string"`，F4 空 chart_type edge case；
  NEW-2 Option A：charts list 与 chart_count 共用同款守卫，DRY）。
- §3.3 RunSummary.event_count 语义改全量→log 行数 + 新增 chart_count；meta event_count 保持
  全量（huge 判定依赖，双语义对照表 docstring）。
- §3.4 in-memory 分支直调 `_scan_meta_overview`（不经 cache，ISSUE-B 避免 per-poll writeback）。
- §3.5 cache version v2→v3 五处（default/gate/writeback stamp/两个 docstring）。
- 前端 RunRow.tsx + BoardCard.tsx 加 chart_count metric；TS 类型同步。
验证：tests/iface/web 非 Playwright 299 passed（297 旧 + 12 新）+ 前端 tsc 干净 + 535 vitest passed。
**test-agent 真机 HTTP 闭环（独立 oracle 重实现白名单对账，全 PASS）**：AC1 event_count==log 行数零偏差
（rich 6/minimal 4/toolheavy 3；F1 双分支真机守门 retry_started fast-path 计入 + tool_call/route_taken 排除）/
AC2 in-memory 分支非 0（orphan attach→6，§1.1 根因）/ AC3 chart_count==5（同 title 去重 + 无/空 title +
空 chart_type edge case，F4 isinstance）。

**必读**：
- SPEC `docs/specs/2026-08-10-card-event-log-align.md`（§3 五块契约 + §7 U1 同步）。
- release note `docs/releases/2026-08-10-card-event-log-align.md`。
- `orca/iface/web/run_manager.py:_scan_meta_overview`（双分支计数 + chart 去重）+ `_LOG_EVENT_TYPES`
  （U1 白名单）+ `_summary_from_overview`（F3 fallback）+ `discover_runs`（§3.4 in-memory 直调）。

**遗留**（环境限制，如实）：纯 InProcessRunHandle（live CC in-session）未真机起（AttachedRunHandle 同代码
路径覆盖 + 单测兜底）；前端浏览器渲染未验（WSL 缺 libnspr4，间接证据：后端字段==独立 oracle + 前端逐字渲染同 JSON）。

---

## 当前：nas-supernet 监控改造+图表修复+洁净——实现+单测+code-reviewer+test-agent E2E 完成；全链 in-flow 验证受阻于 2 个上游缺陷（SPEC 外）

**任务**：SPEC `docs/specs/2026-08-10-nas-supernet-chart-cleanliness-monitoring.md`（两轮 spec-review pass）
A/B/C/D 四段改动——图表正确性 6 项 + prompt 洁净 5 项 + train/retrain CRON→有界轮询+无上限自愈 +
search 无上限自愈。

**状态**：**实现（commit `4f829b1`）+ 单测 + code-reviewer 两轮闭环 + test-agent 真机 E2E 完成**：
- A/B/C/D 全部实现，`tars validate` 0/0，bash -n 全过，ruff 干净，grep 门全过（cron/3次/ATTEMPT_BUDGET 清零）。
- tests/workflows 559 passed + 92 新测试（A1四场景/A2drain/A4sentinel/A3caption/A6stderr/C1结构/C2静态门/镜像同步）。
- **test-agent 真机 E2E（WSL headless + tars run + playground/mnist_kd 清旧脚本）**：
  - **监控改造（SPEC C/D）交付物：真机 PASS**（三层）——monitor_until_done.sh 真机 8 场景全过
    （TRAIN_COMPLETE/INCOMPLETE/STUCK/STILL_RUNNING/GATE_SKIP/status-ambiguous/artifacts-unreachable，
    cheap 活性进程活时 status.sh delta=0 不 torch.load；镜像仅 4 行路径前缀差异；RETRAIN_* 经 *COMPLETE* 通配匹配）；
    静态门全过；agent.md C-loop/HEAL-LOOP/C-end 真连线 + 铁律 3 无上限 + FOREIGN guard + ns_run_search D3 RESUME_SEARCH guard + D1 glob；
    真实 run tape `grep -ic cron`=0（去 cron 生效）。
  - **全链 in-flow 到 ns_run_train：未达成**——受阻于 2 个**上游、SPEC 外**真机缺陷（见待办）。

**必读**：
- release note `docs/releases/2026-08-10-nas-supernet-chart-cleanliness-monitoring.md`。
- SPEC `docs/specs/2026-08-10-nas-supernet-chart-cleanliness-monitoring.md`。
- `workflows/agents/ns_run_train/scripts/monitor_until_done.sh`（有界轮询+cheap 活性+发散检测）。
- test-agent 真机证据：`tests/iface/web/_e2e_artifacts/monitor_real_test.sh`（8 场景驱动）+ run2 tape `runs/nas-supernet-20260810-213024-0f132e.jsonl`。

**待办**（E2E 暴露的 2 个上游缺陷，**非本 SPEC 引入**，各立 case）：
- [ ] **DEFECT-1（headless hang，exec 层）**：`tars run --background` ns_expand_supernet memory-verifier 读 project_root（cwd 外）触发 opencode `external_directory` permission ask → headless 无人审批 → opencode 挂死。根因：opencode profile 用 `--dangerously-skip-permissions` 但 `external_directory` 需单独 `--auto`。**已 user-scope 绕过**（`~/.orca/config.json` 加 `--auto`，run2 expand→train_script 推进）。固化方案：把 `--auto` 加进 opencode profile default flags（`orca/exec` exec 层小改）。
- [ ] **DEFECT-2（ns_train_script DDP smoke-test 杀宿主，架构层）**：run2 train_script 成功生成 train_supernet.py/run_train_supernet.sh/data_utils.py，但其 smoke-test `nohup torchrun`（DDP）把 SIGTERM 传给 opencode 宿主（exit -15）→ node_failed + workflow_failed。根因：torchrun DDP 信号/进程组传播命中宿主；ns_train_script 无 kill_train_group.sh 归属门（那是 ns_run_train/ns_retrain 才有）。**需设计**：smoke-test 进程隔离（setsid 独立会话 + 进程组感知 kill，仿 kill_train_group）或去 DDP 单进程冒烟。
- [ ] 修 DEFECT-1 + DEFECT-2 后重跑 E2E，验 ns_run_train in-flow 调 monitor_until_done.sh + HEAL-LOOP 自愈。
- [ ] stale kd-nas opencode 孤儿进程（WSL PID 982146，5 天前）清理。

---

## 历史：launch.sh 防跨 run 误杀——已完成（训练进程 kill 全收敛到带归属门的 kill_train_group.sh）

**任务**：同项目并发 run 共享 artifacts 目录（project-scoped），训练进程清理按 cmdline
**名字级**匹配，3 处 kill 点（launch.sh 残留清理 + agent Step 2 假死 + self-heal）会
`kill -- -PID` 整组杀掉隔壁 run 的活训练。

**状态**：**完成 + review agent 一轮闭环（1 MUST-FIX 已修 + 3 minor 全修）**：新增
`scripts/kill_train_group.sh` × 2（ns_run_train / ns_retrain 镜像）——`/proc/<pid>/environ`
的 `ORCA_RUN_ID` 与当前比对，本 run 才整组杀；别的 run → `FOREIGN_RUN_ALIVE` 不杀 +
exit 1；env 缺失 legacy 旧行为。launch.sh 残留清理与 agent.md（Step 2 假死 / self-heal）
全部改调 helper（review 发现 Step 2 误认 ALIVE 杀隔壁是主路径，已堵）；launch.sh FOREIGN
abort 在尝试预算计数前（不烧预算）。验证：4 场景 smoke（FOREIGN 不杀不烧预算 / 本 run
stale 整组杀 / 无关进程不误杀 / legacy 旧行为）train+retrain 双份全过 + `tars validate`
0 error + tests/workflows 532 passed。

**必读**：
- release note `docs/releases/2026-08-10-launch-ownership-guard.md`。
- `workflows/agents/ns_run_train/scripts/kill_train_group.sh`（归属门契约）+ 同款 ns_retrain。

**遗留**（另行决策）：共享目录的 run 间 liveness 判定仍互相可见（status.sh 误认他人训练
为自己的 → 误报 ALIVE，但 kill 已被归属门拦住，安全）；彻底隔离需 run 级 artifacts 或
引擎级并发守卫。kd-nas train-teacher 旧模型的裸 kill 未迁移（独立目录，无共享）。

---

## 历史：in-session elapsed 真相修复——已完成 + review agent 闭环（web Log 0s / workflow 0.077s）

**任务**：web Log 面板 in-session run 显示假 elapsed：node 全 `(0s)`（in-session
`node_completed.data` 无 elapsed，LogStream 读 `d.elapsed` → 0）+ `workflow completed
(0.077s)`（CLI `orca next` per-call `start_ts` 测成最后一次调用耗时）。

**状态**：**完成 + review agent 一轮闭环（0 MUST-FIX，2 MINOR 全修）**：
- `events/replay.py` 新增 `_replay_fold`（单遍历 fold + 捕获 `workflow_started_ts` 首条 /
  `node_started_ts` 每 node 最后一条；O1a 单遍历不破）；`_replay_state_and_inputs` 零调用方删除（E5）。
- `advance_step` 移除 `elapsed` 参数，node/workflow elapsed 从 tape 时间戳差算（M5 不撒谎）；
  daemon 删 `_start_ts`；cli 去 per-call 计时。
- 前端 `summarizeEvent` 增 `nodeElapsed` resolver（store D5 差补值回退；两路都无省略括号不显示 0s）。
- review 2 MINOR 修复：resolver 注释与实际一致（未知耗时省略而非 0s 假值）+ 死代码 wrapper 删除。
- 验证：events+in_session+web（非 Playwright）1072 passed（8 失败 stash 隔离证为改动前已有）；
  前端 tsc 干净 + 35 passed。

**必读**：
- release note `docs/releases/2026-08-10-in-session-elapsed-truth.md`。
- `orca/events/replay.py:_replay_fold`（elapsed 锚点捕获契约）。
- `orca/run/step.py:advance_step`（elapsed 派生 + M5 注释）。

---

## 历史：主页 run 列表懒加载——概要索引化 discovery 已完成（1354 tape 12s→18ms）

**任务**：`GET /api/runs?scope=all` 在 1354 tape 时首屏 12s+（主页卡死）。根因
`_summary_from_tape` 每 tape 做 3 遍扫描（`_scan_meta_overview` + 两个无缓存的
`_topology_workflow_name_from_tape` / `_scan_tape_timebounds` 重扫同样行）。

**状态**：**完成 + code-reviewer 两轮闭环（0 MUST-FIX）**：SPEC
`2026-08-10-home-list-lazy-index.md` 逐字实现——① `_scan_meta_overview` 单遍 capture
`workflow_name`/`started_ts`/`ended_ts`（带 isinstance 守卫，BLOCKER I-1/I-2 防非数值
timestamp 炸 overview）；② 抽 `_summary_from_overview` 公共构造器 + 删两个死 helper（DRY）；
③ persistent cache `v1→v2` version gate + 批量写回（defer 期间只更 in-memory + 标 dirty，
尾部 per-runs_dir 单次 `os.replace`）；④ `discover_runs` attached 分支改 `os.scandir` 一次
枚举 + 缓存命中直构（零 fold）+ 两层 fail-soft（目录级 scandir OSError→glob 降级 / per-entry
stat OSError→skip+warn 不降级整目录）；⑤ 前端 `POLL_INTERVAL_MS` 4s→8s。验证：14 新测 + 493
回归全绿（iface/web + events，非 Playwright）；实测 AC1 命中 17ms / AC2 冷启 91ms（1354 tape）/
AC7 13540 tape 温路径 365ms + R²=0.96。

**必读**：
- SPEC `docs/specs/2026-08-10-home-list-lazy-index.md`（§3.1-§3.5 契约 + §4 fail-soft）。
- `orca/iface/web/run_manager.py:_scan_meta_overview`（单遍 capture + 守卫）+ `_summary_from_overview`
  （公共构造器）+ `discover_runs`（scandir 直构 + defer/flush）。

---

## 历史：in-session runs 解析——已完成

**任务**：子代理 CWD 落子目录时 `orca status` 扫不到活跃 run（marker 在项目根 `runs/`，
子代理 `cwd/runs` 空）。根因：`_write_orca_env` 没写项目根锚点 → tape/rundir 解析全走
CWD 相对。

**状态**：**完成**：`runtime/_project.py` 新增 `resolve_runs_dir()`（两级 `ORCA_PROJECT_ROOT`
env > CWD 相对，刻意不调 `detect_project_root` 避免回溯祖先重开上一轮 visibility bug）+
cli.py 三入口（`_default_tape_path` / `_write_orca_env` 加 `ORCA_PROJECT_ROOT` / status 空
markers 新增可选 `hint` 字段）全经它同源；`bg_runner` 零影响。code-reviewer 0 MUST-FIX /
3 SHOULD-FIX 全采纳（hint 文案去未验证断言、warning 反映 runs 解析退化、异常链保真）+
1 MINOR 测试补全（registry 坏 fail-soft）。17 新测 + 284+53 回归全绿。Commit `658d1cd`。
详见 `docs/releases/2026-08-07-in-session-runs-resolution.md`。

**必读**：
- release note `docs/releases/2026-08-07-in-session-runs-resolution.md`。
- `orca/runtime/_project.py:resolve_runs_dir`（两级解析契约 + 不回溯祖先约束）。

---

## 历史：launch hygiene 契约 + 残留进程清理——已完成（封死历史 run 训练启动失败事故类别）

**任务**：他机弱模型实跑 nas-supernet 历史 run 复盘，训练反复死于确定性启动卫生问题：
`num_workers=4` fork 崩 CUDA / `pin_memory=True` 报 cannot be pinned / torchrun 默认 29500
端口 rendezvous EADDRINUSE / 残留 wrapper 叠加二次训练。判定：契约层该固化，非运行期
LLM self-heal 范畴。

**状态**：**完成**：生成契约新增 §4 DataLoader Launch Hygiene（`num_workers=0` +
`pin_memory=False` 写死）+ launcher `NUM_WORKERS=0` + `MASTER_PORT` 随机（`$((20000 +
RANDOM % 20000))`）+ torchrun 自身 flag 排除 cross-check；checklist 新增 [C]36/37（追加
编号不重排 1-35）；`ns_retrain/agent.md` 加同款卫生铁律；`launch.sh` ×2 detach 前清残留
wrapper（`/proc/<pid>/cmdline` 匹配 `run_train_supernet`/`run_retrain` 才组杀，防 PID 复用
误杀）。验证：bash -n + 实测两场景（无关进程不误杀 / 残留整组杀）+ compile 212 passed +
tests/workflows 506 passed（4 预存失败 stash 隔离验证与本改动无关，`_common.py:251`
NameError 为用户 WIP）+ tars validate 过。

**必读**：
- release note `docs/releases/2026-08-10-launch-hygiene-contract.md`。
- 生成契约 `workflows/agents/ns_train_script/references/workflows/train_supernet_script_generation.md` §4 + Run Launcher。
- `workflows/agents/ns_run_train/scripts/launch.sh`（残留清理段）。

**后续待定**（用户另行决策，未做）：单进程单卡改造（放弃，改动面大）；引擎轮询长任务
替代 in-session CRON（ns_run_train/ns_retrain 仍走 CRON 模型）。

---

## 历史：nas-supernet 可视化分散化——已完成（删 ns_visualize，边训练边推前端）

**任务**：用户要求可视化不放最后（原 ns_visualize 是全链跑完才出），不要单独 agent，
固化进前面节点确定性脚本，**边训练边推送到前端**。

**状态**：**完成**：`live_loss_watcher.py`（ns_run_train/ns_retrain 镜像）由 launch.sh
wrapper 内伴生启动，解析生成契约进度行 `epoch N/T loss V` 每新点推全量 loss 曲线（同
title 前端实时刷新）；done-marker 驱动退出 + stale 防误杀 + fail-soft 绝不碰训练。其余
5 图分散：搜索 3 图（pareto/search_table/latency_dist）→ ns_run_search Step 2.7；
对比 2 图（metrics_bar/compare_table）→ ns_retrain Step 3.5。yaml 8→7 agent（retrain
executed → $end）+ 删 ns_visualize 目录。chart daemon TTL 6h→72h。验证：`tars validate`
全 workflow 0/0 + compile/workflows 717 passed + in-session chart 84 passed。测试隔离
修复：新测试 pop sys.modules 同名 `_common` + monkeypatch.chdir。详见
`docs/releases/2026-08-06-nas-supernet-live-viz.md`。

**待办**（留用户定夺）：
- [ ] 真机 E2E：跑一次 nas-supernet（或 mock 训练），验证训练中前端实时刷新 loss 曲线 +
      搜索完 3 图 + retrain 完成 2 图全链路。
- [ ] kd-nas train-teacher 迁移到 in-session CRON 模型（仍走旧 deferred-training-cron）。
- [ ] 可选：>72h 训练需调大 chart daemon TTL（`--ttl` 参数已存在，bootstrap 未暴露）。

**必读**：
- release note `docs/releases/2026-08-06-nas-supernet-live-viz.md`。
- `workflows/agents/ns_run_train/scripts/live_loss_watcher.py`（watcher 契约 + fail-soft）。
- 参照：`workflows/agents/ns_run_train/scripts/launch.sh`（wrapper 内 watcher 启动）。

---

## 历史：ns_retrain 迁移到 in-session CRON 模型（同 ns_run_train），留 train-teacher 迁移 + 真机验证

**任务**：把 `ns_retrain` 从 deferred-training-cron（detached + 外部 cron + headless 重跑）迁移到
ns_run_train 的 in-session CRON 模型——节点常驻到重训**真正完成**（rc=0 + 进程退出 + ckpt 有效才认完成，
ckpt 存在 ≠ 完成），in-session CRON 1~2h 定时自检，未完成更新 `retrain_status.md` + 重注册，完成才产出 JSON。

**状态**：**迁移完成 + code-reviewer 独立洁净审查闭环（0 MUST-FIX + 2 SHOULD-FIX + 6 MINOR 全修）+
37 项 smoke test 全过 + `tars validate` 0/0 + 残留扫描全清**。改动：agent.md 686→365 行（决策树 +
尝试预算 + CRON 生命周期 + 生成阶段保留 [3a 生成 retrain.py/finetune.py/run_retrain.sh 只做一次 +
3b fidelity 复查] + 生成契约新增 [progress line `epoch N/T loss V` + `--epochs` 暴露 + final ckpt
固定路径]）+ 确定性逻辑固化到 `scripts/` 7 脚本（镜像 ns_run_train 契约）+ yaml（status 去 detached /
删 terminate_retrain_pending / 4 terminate → 3 全 fail-loud）。审查修的关键项：launch.sh 不再清
fidelity flag（3b 先于 launch 时序导致成功路径审计失真）、生成落点声明 + 双文件 gate、
`max_retries_hit` 从 `.retrain_attempt` 推导（≥3 才 true）。
详见 release note `docs/releases/2026-08-06-ns-retrain-cron-selfcheck.md`。

**待办**（留用户定夺）：
- [ ] **kd-nas train-teacher 迁移到本模型**——仍走旧 deferred-training-cron（detached + 外部
      cron + headless 重跑），与本模型不一致。
- [ ] 真机验证：CRON 工具唤醒 → 检查 → 完成 → `orca next` 提交 → 下游继续的完整闭环
      （ns_run_train / ns_retrain 同一待办）。

**必读**：
- release note `docs/releases/2026-08-06-ns-retrain-cron-selfcheck.md`。
- 新 agent.md `workflows/agents/ns_retrain/agent.md`（决策树 + 生成契约 + CRON 生命周期）。
- 参照模型 `workflows/agents/ns_run_train/agent.md`（本次迁移的对齐基准）。

---

## 历史：project-scoped artifacts——实现 + code-reviewer 闭环完成，留集成测试补全给用户

**任务**：SPEC [`project-scoped-artifacts-design-draft.md`](../specs/project-scoped-artifacts-design-draft.md)
（spec-review 14 issue 全闭 → 实现）—— in-session 引擎面 project-scoped `$ORCA_ARTIFACTS_DIR`
解析 + nas-supernet input 改名 + 6 个昂贵节点 Step 0 软跳过 + kd-nas 撤销拍平 + 4 个 kd-nas
节点 Step 0。

**状态**：**实现 + code-reviewer 一轮闭环完成**（0 must-fix / 4 should-fix / 3 optional；
surgical should-fix 全修 + 1 docstring，留 1 测试补全项给用户）。
- `797a6c8`：核心三块（引擎面 + nas-supernet 改名/carve-out/Step 0 + kd-nas 撤销拍平）。
- `1cb377f`：code-reviewer 闭环（bootstrap raise 结构化 + Step 0 dead code 清理 + docstring Rule 7）。
- `77013e4`：CHANGELOG + CURRENT.md 索引。

**待办**（留用户定夺）：
- [ ] **bootstrap 集成测试补全**（code-reviewer should-fix #3）：补 `CliRunner` 驱动
      `orca <wf> --inputs '{"project_root":"/abs"}'` 断言 `<proj>/artifacts/<wf>/` 真 mkdir +
      `$ORCA_ARTIFACTS_DIR` 注入 env；`project_root="rel"` → 非 0 退出 + 结构化错误信封。
      属测试补全非生产代码改动，单独立 case 更合适。

**必读**：
- release note `docs/releases/2026-08-06-project-scoped-artifacts.md`（含偏离 SPEC 记录 +
  code-reviewer 闭环明细）。
- SPEC `docs/specs/project-scoped-artifacts-design-draft.md`（§2 契约 / §5 非目标）。

---

## 历史：kd-nas LLM 语义 fidelity 审计——实现完成（B1+B2+D3），待独立洁净审查 + 真机 E2E

**任务**：SPEC [`2026-08-05-kd-nas-fidelity-audit-spec.md`](../specs/2026-08-05-kd-nas-fidelity-audit-spec.md)
（REVIEWED + 用户拍板，逐字实现）—— 给 kd-nas 训练脚本生成节点加 LLM 语义 fidelity 审计
（B1）+ ID 化 Resumed Re-Check 收敛环（B2），抓 L3 确定性层的实证盲区
（helper 体外 look-alike / transform 内容 / optim kwargs / 控制流重排）。

**状态**：**实现完成 + `tars validate` 0 error/0 warning + code-reviewer 一轮闭环**
（0 must-fix / 3 should-fix 已采纳）。已 commit。
- 新建 `workflows/subagents/kd-nas/project-fidelity-verifier-kd.md`（KD 化独立副本，
  sentinel=KDPFV01，Out-of-scope=KD 引擎 + student 变体；STATUS 契约机械可解析）。
- `kd-train-script/agent.md` + `SKILL.md` Step 4 在 L3 与 L4-mechanical 间插
  **L4-semantic 收敛环**（MAX_TURNS=3，first-run + resume 模板经 `{{ subagents_root }}`
  point-to-file，ID 范围防御，reaffirm/Unresolved→ask-user，apply fixes 后重跑 L1+L3）。
- `train-script-verify/agent.md` 加 step 3.5（report-only 一次性 spawn）+ step 4 也
  report-only 并传 Accepted IDs（D1+N8）。
- `kd-nas.yaml` 两节点注释同步（output_schema 不变）。
- 新建 `examples/mnist_kd_adversarial/`（D3 fixture，`optim.py::build_optimizer`
  weight_decay 偏差，L3-blind / B1-caught）。
- SPEC §3.1 line 54/106 prose typo（与 frontmatter `-kd` 冲突 validator 铁律）→ 文件
  统一命名 `project-fidelity-verifier-kd.md`。

**待办**：
- [x] 实现 + `tars validate` + code-reviewer 一轮闭环。
- [ ] **独立最终洁净审查**（用户另行派 agent，不属本次范围）。
- [ ] **真机 E2E 三条路径**（SPEC §4 A7/A8/A9：收敛环 / Unresolved→ask-user /
      reaffirm 防呆）用 D3 fixture 跑生成节点验证。

**必读**：
- 本任务 release note `docs/releases/2026-08-05-kd-nas-fidelity-audit.md`
- SPEC `docs/specs/2026-08-05-kd-nas-fidelity-audit-spec.md`（§3 契约 / §4 验收 / §6 洁净）

---

## 当前：in-session 权限审批 Web 桥——已 commit + test-agent e2e 闭环，仅剩 §9 #2 真机 spike

**任务**：SPEC [`in-session-permission-hook.md`](../specs/in-session-permission-hook.md) v3.2
（3 轮 spec-review conditional-pass）—— in-session workflow 运行期，宿主 CC 的 PermissionRequest →
web 审批卡 → 用户 allow/deny；超时默认 allow（可配）；前端 yolo 开关。

**状态**：**已 commit + 单测全绿 + test-agent 真机 e2e 闭环**（real uvicorn + real hook 子进程 + real WS）。
- 7 组件：stdlib-only hook + ApprovalBroker（不碰 tape/gate/handler/exec/events.bus）+ 路由 + ws 第二 pump +
  前端独立 approval store + install 扩展 `_install_cc_nudge` + doctor 心跳。
- test-agent e2e 抓到 2 真 bug 并已修 + 防回归测试：**BUG A**（`_disconnect_poller` 缺 `await` → timeout
  路径全断，已修）、**BUG B**（redact regex 空白截断 → Bearer token 泄露，已修）。64 单测 + 回归全绿。
- 失败语义（SPEC §7）：broker 不可达→ask / HTTP 4xx-5xx→deny+stderr / 非 JSON→deny+stderr /
  timeout→policy（默认 allow）/ disconnect→aborted。

**待办**：
- [x] 实现 + 单测 + code-reviewer + test-agent e2e + 2 bug 修复 + commit。
- [ ] **§9 #2 spike（唯一剩余，真机用户侧）**：交互式 CC + Task 子 agent 下 PermissionRequest 是否
      自然触发 + stdin 字段名实证（自动化 `claude -p` 证不了，task 4 Q3 已证非交互不触发）。
      失败 → SPEC §9 #6 fallback 切 PreToolUse（`block` 枚举 + tool-classification 扩 `readonly_tools`）。

**必读**：
- release note `docs/releases/2026-08-05-in-session-permission-hook.md`（r2 含 bug 修复）
- SPEC `docs/specs/in-session-permission-hook.md`（§3.1 hook / §3.2 broker / §9 spike）

---

## 历史：subagent point-to-file 协议——实现完成 + 单测全绿，待 headless e2e

详见 [release note](../releases/2026-08-05-subagent-point-to-file.md)。**待 headless nas-supernet e2e**。

---

## 历史：PostToolUse 事后告警守卫——coder-agent 完成，待 test-agent 四前端真机 e2e

详见 [release note](../releases/2026-08-05-posttooluse-rogue-guard.md)。**待四前端真机 e2e**。

---

## 历史：in-session YOLO 兜底路由（active-run fallback）——已完成 + code-reviewer 两轮闭环

**任务**：真实用户反馈 CC 终端跑 in-session workflow、web 已开 yolo，但工具权限审批仍照常弹出。
根因：in-session 宿主 CC/子代理 session 从不注册 registry（注册 id 是 executor 入口 uuid，非 CC
会话 id）→ `resolve_session_context` 恒 miss → yolo 分支不可达。
**状态**：**完成**：新增 `orca/iface/web/active_runs.py`（扫 marker → 终态第二守卫 → tape 双键
匹配 host_session/顶层 session_id → 多 run 取 marker mtime 最新 + 平局字典序；per-run 缓存键含
marker 存在性；坏数据 per-item fail-soft）+ `ApprovalBroker` 注入 `active_run_resolver`（registry
miss 命中走与注册命中完全相同的 yolo/web 审批，未命中/异常 → ask）+ `create_app` 装配（工厂零
IO，调用期枚举全项目）。code-reviewer 两轮闭环 0 MUST-FIX（缓存键显式化 / UnicodeDecodeError
per-item / 静默 continue 补 warn / broker 守门升级 AST+orca.run / 测试走公开构造器 / deny 分支）。
63 pinned + iface/web 全量（除 Playwright）272 passed。Commit `984d55b`。详见
`docs/releases/2026-08-07-in-session-yolo-active-run-fallback.md`。
**待办**（真机验证，SPEC §7 遗留）：CC 终端跑一次 in-session workflow，验证 yolo on 自动放行 /
yolo off 出 web 审批卡。
**必读**：release note `docs/releases/2026-08-07-in-session-yolo-active-run-fallback.md`；
`orca/iface/web/active_runs.py`；SPEC `docs/specs/2026-08-07-in-session-yolo-active-run-fallback.md`。

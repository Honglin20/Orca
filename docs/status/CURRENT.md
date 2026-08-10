# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

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

## 当前（严重/设计错误，待修）：主页 run 列表随 tape 数量线性变慢——缺懒加载

**现象**：`runs/` 有 1354 个 tape（120M，多为测试产物）时，`GET /api/runs?scope=all`
首屏 **12s+** 才返回（discovery 全量扫描 + 逐 tape fold 概览），主页一直转圈卡死。
诊断复现：`/api/runs` 其他端点均 <1s，唯 `scope=all` 慢；二层派生缓存（in-memory +
`.orca-meta-cache.json`）**每次请求仍要对全部 1354 tape 逐个 stat + glob + 过滤**，
且 legacy 分支对每个 meta 重扫 tape 路径，O(n) 全量开销不可豁免（缓存只省 fold 不省扫描）。

**设计判定**：这是**架构问题不是 bug**——违背「一条读路径」精神的懒加载契约：
主页只需总览（run_id / workflow / status / progress），却在请求期做全量 tape 派生，
把磁盘 IO 成本押在首屏关键路径上。修复 ≥3 文件且需改数据流，不允许 surgical 打补丁。

**修复方向（待写设计稿）**：
- 列表端点应基于**轻量总览层**（如 `.orca-meta-cache.json` 作为 index 直接读，mtime 增量
  失效），不逐 tape stat/parse；或分页/增量返回 + 前端滚动加载。
- 关键路径与全量派生解耦：总览快、详情才 fold。
- 附带：清理历史测试产物（本次已手动清 1354 tape + 1456 个注册表测试项目，`projects.json`
  仅剩 Orca；`/home/mozzie/tars-serve*.log` 为本次排查日志）。

**必读**：
- `orca/iface/web/run_manager.py:discover_runs`（1373）+ `_scan_meta_overview_cached`（1560）。
- `orca/iface/web/routes/runs.py:list_runs`（40，`scope=all` 全量路径）。
- 复现数据：`/api/runs?scope=all` 12.3s（1354 tape）/ 1.2s（0 tape，本次清理后）。

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

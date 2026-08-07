# CHANGELOG —— 任务索引

> 每个任务完成后，在**顶部**加一条索引（1-2 句话 + commit SHA + release note 链接）。
> 最近的在上面。**不积累、不延后**——完成即记。

---

## [2026-08-07] feat(in-session): YOLO active-run 兜底路由——session 未注册时扫活跃 run tape 双键匹配

真实用户反馈：CC 终端跑 in-session workflow、web 已开 yolo，但工具权限审批仍照常弹出。根因：
in-session 宿主 CC/子代理 session 从不注册进 `SessionContextRegistry`（注册 id 是 executor 入口
uuid，非 CC 会话 id），`resolve_session_context` 恒 miss → 每次 PermissionRequest 都走
`native-fallback ask`，yolo 分支不可达。**改动**：新增 `orca/iface/web/active_runs.py`（扫 marker
→ 终态第二守卫 → tape 双键匹配：首行 `data.host_session` / 全量顶层 `session_id`；多 run 取 marker
mtime 最新 + 平局字典序；per-run 缓存键含 marker 存在性；坏数据 per-item fail-soft）；
`ApprovalBroker` 注入 `active_run_resolver`（DI，零 run/tape 依赖），registry miss 时命中走与注册
命中完全相同的 yolo/web 审批流程，未命中/异常 → `native-fallback ask`；`create_app` 装配工厂
（零 IO，调用期枚举 `resolve_runs_dir` + `list_registered` 全项目）。code-reviewer 两轮闭环
0 MUST-FIX。63 pinned + iface/web 全量（除 Playwright）272 passed。Commit: `984d55b`。详见
[release note](../releases/2026-08-07-in-session-yolo-active-run-fallback.md)。

## [2026-08-07] fix(in-session): runs 目录解析鲁棒化——env 自描述 + 两级解析器，修子代理子目录迷路

子代理 CWD 落子目录（如 `artifacts/.../runs/train/`）时 `orca status` 扫不到活跃 run：marker 在项目根 `runs/`，子代理 `cwd/runs` 空。根因：`_write_orca_env` 只写 7 个变量，无项目根锚点 → tape/rundir 解析全走 CWD 相对。**改动**：`runtime/_project.py` 新增中立层 `resolve_runs_dir()`（两级：`ORCA_PROJECT_ROOT` env > CWD 相对 `Path("runs")`；刻意不调 `detect_project_root` 避免回溯祖先重开上一轮 visibility bug）；`cli.py` 三入口（`_default_tape_path` / `_write_orca_env` 加 `ORCA_PROJECT_ROOT` per-run 常量 / status 空 markers 新增可选 `hint` 字段）全经它同源；`bg_runner` 零影响（锁定测试绿）。code-reviewer 0 MUST-FIX / 3 SHOULD-FIX 全采纳 + 1 MINOR 测试补全；17 新测 + 284+53 回归全绿。Commit: `658d1cd`。详见 [release note](../releases/2026-08-07-in-session-runs-resolution.md)。

## [2026-08-06] feat(nas-supernet): 可视化分散化——边训练边推送到前端（删 ns_visualize 单独节点）

用户要求「不要单独可视化 agent，可视化固化进前面节点，边训练边推送」。机制核实：render_chart 是 stdlib 轻客户端（4 env 即可推图），同 label+title 重复推送 = 前端替换（实时更新语义）；chart daemon per-run、run 活跃期间一直活着 → 训练伴生进程可直接推图，零引擎改动。**改动**：(1) 新增 `live_loss_watcher.py`（ns_run_train + ns_retrain 镜像）——launch.sh wrapper 内训练前后台启动（同进程组组杀一并清），解析生成契约进度行 `epoch N/T loss V` / `step N/T loss V` 每新点推全量曲线（同 title 前端刷新）；done-marker（.train_rc/.retrain_rc mtime 晚于启动）驱动退出 + stale marker 防误杀续训 + 首点前不启用 idle（首 epoch 慢不误杀）+ 全路径 fail-soft exit 0 绝不碰训练 rc；(2) 其余 5 图按产数据节点分散：pareto/search_table/latency_dist → ns_run_search Step 2.7（搜索完立刻可见），metrics_bar/compare_table → ns_retrain Step 3.5（完成时，selected 坐标 Jinja 注入）；(3) yaml 8→7 agent（ns_retrain executed → $end，outputs 去 visualization）+ 删 ns_visualize 目录（loss_curve/generate_charts/report 随 watcher 取代）；(4) chart daemon TTL 6h→72h（覆盖天级训练，保留防泄漏兜底；用户拍板非无限制）。**验证**：tars validate 全 10 workflow 0/0；compile+workflows 717 passed / 3 skipped（新 watcher 单测 11 + chart scripts 28）；in-session chart 相关 84 passed。**测试隔离修复**：新测试 `from _common import` 把 chart 版 `_common` 注册进 sys.modules 截胡 `_quant_scripts/_common`（同名跨目录模块）→ import 后 pop 三模块名 + 移除 sys.path 插入 + watcher 测试 cwd 泄漏改 monkeypatch.chdir。预存失败 8 项（用户 WIP 相关，stash 验证与本次无关）。详见 [release note](../releases/2026-08-06-nas-supernet-live-viz.md)。

## [2026-08-06] feat(nas-supernet): ns_retrain 迁移到 in-session CRON 定时自检（与 ns_run_train 同模型）

按 ns_run_train 的 in-session CRON 模型迁移 ns_retrain，替代 `658f85c` 的 deferred-training-cron（detached + 外部 at/crontab + headless 重跑）：节点常驻到重训**真正完成**（rc=0 + 进程退出 + ckpt 有效才认完成，残留 ckpt 续训），agent 自调 in-session CRON 1~2h 自检，未完成更新 `retrain_status.md` + 重注册，完成才输出 JSON。**生成阶段保留**（本节点特有）：3a 按 AGENTS.md scaffold 生成 retrain.py / finetune.py / run_retrain.sh（只做一次）+ 3b fidelity 复查 + **生成契约新增**（machine-parseable progress line `epoch N/T loss V` / `step N/T loss V` + `--epochs N` 暴露 + final ckpt 固定 `runs/retrain/retrain_best.pth`——scripts 解析前提治本）。尝试预算 N=1..3 跨唤醒统一；CRON 生命周期（续期 + 完成后停止）。yaml：status 枚举去 detached、删 terminate_retrain_pending 节点与路由（4 terminate → 3 全 fail-loud）、description 同步。确定性逻辑固化到 `scripts/` 7 脚本（status/health/launch/warmup_poll/eta/update_status_md/emit_result，镜像 ns_run_train 契约 + retrain 路径）；agent.md 686→365 行。`tars validate` 0 error / 0 warning；**34 项 smoke test 全过**（含 eta argparse add_argument 形态新支持、ckpt marker 绝对路径修复）；残留扫描全清。**code-reviewer 独立洁净审查闭环**（0 MUST-FIX + 2 SHOULD-FIX + 6 MINOR 全修）：S1 launch.sh 清 marker 抹掉 3b 首启 fidelity flag → 成功路径 `fidelity_retriggered=false` 失真 → rm 列表去 flag（覆盖写无 stale，语义改"对当前脚本已跑过 fidelity"）；S2 生成落点未声明 + gate 单文件 → 3a 补 artifacts 根 + 双文件 gate + 3f 允许 write 重建自产文件；M3 emit_result `max_retries_hit` 改从 `.retrain_attempt` 推导（≥3 才 true，前置失败不误报）；smoke 扩到 **37 项全过**。**待办**：kd-nas train-teacher 仍走旧模型（用户定夺）。详见 [release note](../releases/2026-08-06-ns-retrain-cron-selfcheck.md)。

## [2026-08-06] feat(nas-supernet): ns_run_train 重写——in-session CRON 定时自检（节点常驻到训练真正完成，替代 deferred-training-cron）

用户对齐确认的新模型，替代 `e8f7700` 的 deferred-cron 原型：**节点不 park / 不挂起 / 不提前输出**——训练未完成前节点保持执行中、run 活跃、引擎逻辑零改动（唯一引擎侧改动是 `_drive_protocol` 纯 prompt 文本加"产出非 JSON 含『请勿调用 orca next』→ 宿主不调 next"分支）。完成判定 = rc=0 + 进程已退出 + ckpt 有效（**ckpt 存在 ≠ 完成**，中断残留续训到真正完成）；PID/状态/epoch/ETA 落 `runs/train/train_status.md`（跨唤醒真相源）；agent 自调 in-session CRON 工具注册 1~2h 定时自检，唤醒后重读指令按 Step 1 判定，完成才输出 JSON（宿主 next 提交）。尝试预算 N=1..3 跨唤醒统一（warmup 自愈/假死重启/中断续训/rc==0 无 ckpt 共享，耗尽 → failed，杜绝无限重启）。yaml：status 枚举去 detached、删 terminate_training_pending 节点与路由、description 同步（4 terminate）。code-reviewer 一轮闭环 3 MUST-FIX + 5 SHOULD-FIX + 4 MINOR 全修（M1 驱动协议文本分支 / M2 CRON 生命周期防周期唤醒杀 run / M3 ckpt marker 纯路径 / S4 stale rc 排除活进程 / S5 3e 从 log 重算 epoch / S6-S7 共享预算 / m10 weights_only=False 等）。**重构**：确定性逻辑固化到 `scripts/` 7 个脚本（status/health/launch/warmup_poll/eta/update_status_md/emit_result，经 `$ORCA_AGENT_RESOURCES` 锚定），agent.md 601→320 行（CRON 唤醒 token 成本腰斩）。`tars validate` 0/0；22 项 smoke test 全过（真 torch ckpt → TRAIN_COMPLETE→executed）；`tars install` 部署完成。**补**：log 格式契约（ns_train_script 生成 spec 加 machine-parseable progress line：`epoch N/T loss V` / `step N/T loss V` + `--epochs`/`--max_steps`）+ 超时兜底（脚本正则兼容 epoch|step、warmup/health 加 LOG_MTIME/LOG_SIZE、agent.md 判分支"log 在增长 → 人工判健康不烧预算"、Step 2 假死判定 mtime 兜底）；smoke test 扩到 27 项全过。**二轮复查**：code-reviewer 抓 1 MUST-FIX（loss 发散检测死分支——`loss nan` 无数字被数字行过滤漏掉，发散防线静默失效）+ 7 SHOULD-FIX（计数前导零归并 / eta 解析 EPOCHS= 变量形态 / MD stuck 参数 / setsid 进程组整组 kill 防孤儿重复 detach / rc=0 快速完成分支 / stat 双平台 / 环境依赖声明）+ 8 MINOR 全修；smoke test 扩到 **35 项全过**。**待办**：ns_retrain / kd-nas train-teacher 仍走旧 deferred-cron 模型，需按本模型迁移（用户定夺）。详见 [release note](../releases/2026-08-06-ns-run-train-cron-selfcheck.md)。

## [2026-08-06] feat: 迁移 deferred-training-cron 到 ns_retrain + kd-nas train-teacher（distill 评估后不迁移）

按 SPEC §5 把 `ns_run_train` 原型（commit `e8f7700`）的 deferred-training-cron 模式迁移到两个 training agent：(1) `ns_retrain`（nas-supernet）：Step 0 三分支 + Step 4 五步 fresh-launch + Step 5 dual-signal；marker 隔离（`.retrain_*` / `.cron_registered_retrain.flag`）；MARKER `ORCA_CRON_NS_SUPERNET_RETRAIN`；yaml 加 `detached` enum + `terminate_retrain_pending` 终态。(2) kd-nas `train-teacher`：Step 0 三分支 + Step 1 五步（wrapper 串 train_pipeline + teacher_setup nohup detach）；**marker 必落 `$KD_ARTIFACTS_DIR`**（kd-nas per-run dir 不持久）；wrapper 捕获 PER_RUN 快照；yaml 加 `status` field + 双显式路由（detached/failed）+ 2 终态节点。`distill`（迭代节点）评估后**不**迁移——gen_student 跨 run 状态依赖 + decide 同步读 distill output + 无 project-scoped 单 round ckpt marker，硬上会跨 4 节点状态耦合改造，超「同模式迁移」范围，按 SPEC §5 留用户定夺。self-caught must-fix：train-teacher 加 status=failed enum 后 catch-all 会静默放行 → 加显式 `when status=='failed' → terminate_train_teacher_failed` + 移除 Step 3 python `sys.exit(2)`（路由才是 fail-loud 机制）。code-reviewer 一轮闭环：清「退 2」文档残留 + Step 2 metrics_tail log path 改稳定路径（与 wrapper 重定向一致）。`tars validate` 两 wf 0 error / 0 warning；bash + python heredoc 语法全过；status 推导 12 场景 smoke test 全过。详见 [release note](../releases/2026-08-06-deferred-training-cron.md)（含迁移段）。

## [2026-08-06] feat(nas-supernet): ns_run_train deferred training via cron（warmup 估时→cron 重跑→软跳过）

按 SPEC `deferred-training-cron-design-draft.md` 实现 `ns_run_train` 多天训练解耦原型：三分支 Step 0（reuse / resume-pending / fresh-launch）+ Step 2 fresh-launch 五步（detach → warmup 测每 epoch 耗时 → 估时 → cron 注册 → park detached）。warmup 失败走既有 self-heal（白名单 + ≤3 次）。YAML 加 `detached` status enum + route + `terminate_training_pending` 终态节点（`status: success` + reason 文案——schema 仅 success/failed 无 pending，选 success 表 deferred 语义）。code-reviewer 一轮加固：detached 信号改 `pid_alive AND cron_registered.flag` 双条件（防 self-heal 全败被静默吞掉）+ Step 2a 清旧 deferred markers；Step 2e python f-string 语法错改 heredoc；0a 写 ckpt marker + Step 3 优先读，统一 ckpt 路径解析；plus cron PATH 注入、`set -e`、T_MIN 校验、`{{ inputs | tojson }}` DRY 化等。实现决策：`tars nas-supernet` → `orca nas-supernet`（CLAUDE.md 钉死 tars 是 skill 不是 CLI）。`tars validate` 0 error / 0 warning；9 bash 块 + 3 python heredoc 语法全过；5 场景 status 推导 smoke test 全过（含 dual-signal regression）。**未**迁移到 ns_retrain / kd-nas（后续 task）。Commit: `e8f7700`。详见 [release note](../releases/2026-08-06-deferred-training-cron.md)。

## [2026-08-06] feat(artifacts): project-scoped artifacts + nas-supernet input rename + kd-nas 撤销拍平

按 SPEC `project-scoped-artifacts-design-draft.md`（spec-review 14 issue 全闭）三块实现：(1) in-session 引擎面 `cli.py` 新增 `_read_workflow_inputs` + `_resolve_artifacts_dir`（workflow 有 `project_root` 绝对 input → `<proj>/artifacts/<wf>/`，否则 per-run 回落；签名扩展为 `tuple[Path, bool]` 以支持 project-scoped fail-loud mkdir vs per-run fail-open 区分；bootstrap raise 包装 try/except ValueError → emit workflow_failed + clear_marker + JSON 错误信封，防 orphan marker）；(2) nas-supernet：input `user_project_root`→`project_root`（yaml + 8 ns_*.md Jinja + 散文）+ 6 个昂贵节点加 Step 0 Reuse-Check 软跳过（status 枚举不动，reused 命中成功分支）+ 严格编辑 agent 的禁碰清单 carve-out（project_root 下源文件只读，artifacts/ 子目录例外可写）；(3) kd-nas 撤销拍平：`KD_ARTIFACTS_DIR` 加 `kd-nas` 子目录 + 删 `migrate_flat.py`（520 行）及其测试 + model-flatten 路径同步 `${PROJECT_ROOT}/artifacts/kd-nas/models/baseline/` + `test_model_flatten.py` 6 条 pin 断言反转。`tars validate` 两 wf 0 error / 0 warning；15 新单测 + 141 in-session CLI 测试 + 57 model-flatten 测试全绿。code-reviewer 一轮闭环：0 must-fix / 4 should-fix / 3 optional，surgical should-fix 全修（bootstrap 结构化 raise + dead variable 清理 + marker rm-only 模式统一 + model-flatten Step 0 物理位置重排 + docstring Rule 7 surfacing），留 bootstrap 集成测试补全给用户。Commits: `797a6c8` (核心) + `1cb377f` (reviewer 闭环)。详见 [release note](../releases/2026-08-06-project-scoped-artifacts.md)。

## [2026-08-05] feat(kd-nas): LLM 语义 fidelity 审计 + ID 化收敛环（B1+B2）

按 SPEC `2026-08-05-kd-nas-fidelity-audit-spec.md`（REVIEWED + 用户拍板）逐字实现：新建 `workflows/subagents/kd-nas/project-fidelity-verifier-kd.md`（KD 化独立副本，删 RL/auxiliary/reward 段，加 STATUS 契约 + KD 专属 Intended behavior）；`kd-train-script/agent.md` + `SKILL.md` Step 4 在 L3 与 L4-mechanical 间插 **L4-semantic 收敛环**（MAX_TURNS=3，first-run + resume 模板经 `{{ subagents_root }}` point-to-file，ID 范围防御，reaffirm/Unresolved→ask-user）；`train-script-verify/agent.md` 加 step 3.5 一次性 report-only fidelity-verifier spawn（D1）+ step 4 同样 report-only + 传 Accepted IDs（N8）；`kd-nas.yaml` 两节点注释同步（output_schema 不变）；新建 `examples/mnist_kd_adversarial/`（D3，复制 mnist_kd，仅 `optim.py::build_optimizer` 注入 `weight_decay=1e-3` 偏差——L3 `OPT_TYPE_OK` 比 class 名 PASS、B1 比 kwargs 命中）。`tars validate` 0 error / 0 warning；code-reviewer 一轮闭环 0 must-fix / 3 should-fix 已采纳。SPEC §3.1 line 54/106 文件名 typo（与 line 60 frontmatter `-kd` 后缀冲突 validator `fm["subagent"]==md.stem` 铁律）→ 文件统一命名为 `project-fidelity-verifier-kd.md`。Commit: 见 git log。详见 [release note](../releases/2026-08-05-kd-nas-fidelity-audit.md)。

## [2026-08-05] feat(in-session): 权限审批 Web 桥——PermissionRequest → web 审批卡 + yolo 开关

SPEC `in-session-permission-hook.md` v3.2（3 轮 spec-review conditional-pass）实现：宿主 CC 的 PermissionRequest 经 stdlib-only hook（urllib/json/os/sys/time/socket）→ POST `/approval` → 进程级 `ApprovalBroker`（不碰 tape/gate/handler/exec/events.bus，grep 守门）→ web 弹审批卡；超时默认 allow（`ORCA_APPROVAL_TIMEOUT_POLICY` 可配 ask/deny）；前端 yolo 全局开关（`~/.orca/approval-yolo.json` 持久化）。task 4 实测：CC hook `timeout` 字段被严格遵守、上限 ≥86400 无硬顶 → 两层分离（CC=86400 / ORCA_APPROVAL_TIMEOUT=600）。并发模型：per-approval `asyncio.Future` + `threading.Lock` first-wins + 手写 timer task + disconnect poller + uuid4 id（SPEC §3.2 P1 偏差：`asyncio.wait_for` cancel 底层 future 触发 `InvalidStateError`，改手写 timer task 直接 await fut）。失败语义（§7）：broker 不可达→ask / HTTP 4xx-5xx→deny+stderr / 非 JSON→deny+stderr / timeout→policy / disconnect→aborted。WS 第二条 approval pump（不经 handle.bus，run-scoped 投递）；前端独立 `useApprovalStore`（不复用 `useWorkflowStore.gate`，snapshot 权威清 stale）。install 扩展 `_install_cc_nudge` 单事务合并 `hooks.PermissionRequest`（去重关键字 `orca-permission`，timeout=86400）+ 拷 hook + chmod 755 + env（ORCA_PORT/HOST/TIMEOUT/POLICY）；doctor 加 `approval_broker` 心跳。test-agent 真机 e2e（real uvicorn + real hook 子进程 + real WS）抓 2 bug 并修 + 防回归测试：BUG A（`_disconnect_poller` 缺 `await` → timeout 路径全断）/ BUG B（redact regex 空白截断 → Bearer token 泄露）。**64 单测 + 回归全绿**（broker 26 / hook 15 / install 9 / ws 集成 6 / routes 8）+ 前端 527 测试绿。**§9 #2 spike（交互式 CC 下 PermissionRequest 触发）留用户真机**，自动化证不了（task 4 Q3）。Commit: 见 git log。详见 [release note](../releases/2026-08-05-in-session-permission-hook.md)。

## [2026-08-05] feat(exec): subagent point-to-file 协议——nas-supernet 子 agent 自读 md + sentinel 回显

把 nas-supernet 子 agent 调用从 read+embed（parent cat body 内联进 Task prompt）改为 point-to-file（子 agent 自读 md body）：`{{ subagents_root }}/<name>.md` 在 render 期 inline 成绝对路径（不引新 env var，shell 无关）；parent prompt 只发短指针 + sentinel 回显约束；5 个子 agent md 加 frontmatter（subagent/version/sentinel 三键）；目录拓扑 v3 `workflows/subagents/<wf>/`（git mv 迁移）。引擎：RunContext 加 `subagents_root` 字段；6 处构造点 populate（orchestrator/step.py）；render.py 引用但空串时 fail loud；validator 加 `_check_subagents_md`（frontmatter strict regex + dev-residue 扩扫子 agent md + Read 工具前置校验）；install copytree `workflows/subagents/` → `~/.orca/workflows/subagents/`。code-reviewer 一轮闭环：0 must-fix / 3 should-fix（_recover_step_result e2e 测 + 子串探测 vs regex 注释 + SPEC 文本 footnote）+ 3 nice-to-have（foreach body 测 + 例外直接断言 + skip 可见性），2 项已采纳。**890 passed**（含 35 新测，1 pre-existing demo fail 与本次无关）。Commit: 见 git log。详见 [release note](../releases/2026-08-05-subagent-point-to-file.md)。

## [2026-08-05] feat(in-session): PostToolUse 事后告警守卫——四前端纯提示 hook（B 路径扩展）

给四前端（cc/cac + opencode/nga）加 PostToolUse 事后告警：主 session 在活跃 run 期间自己下场干活（Edit/Write/跑 train）→ 工具执行后注入**纯文本提示**（additionalContext / promptAsync），不阻止、不推进、不捕 output、不 emit decision。覆盖 §4.4 Stop/idle nudge 的 turn 中途盲区。新增 `tool-classification.json` 单一真相源（writing/bash 工具集 + readonly 前缀 word-boundary + 复合命令分隔符 + guard_reason_template）；`cc_nudge.sh` 加 hook_event_name 分支（Stop 字节级不变 / PostToolUse 新增）；`install_cmds` 合并 hooks.PostToolUse 条目（matcher 锚定 + 去重）+ 拷 classification；`orca.ts` 加 tool.execute.after 钩子（复用 listActiveRuns/nudgeAllowed；throttleFile 参数化；idle/guard 独立 mutex）。code-reviewer 两轮闭环：第一轮 4 should-fix（Stop 字节级 golden fixture / 节流顺序对称 / reason 模板入 JSON / opencode classification 候选路径补全）+ 3 nice-to-have；第二轮 fresh 拾遗 1 must-fix（PostToolUse 路径 leak exit 2 → `_scan_my_active_run_ids` 加 `strict` 参数，guard fail-open / Stop 仍 fail loud）+ 2 should-fix（idle/guard 共用 mutex 拆为 `injectingIdle`/`injectingGuard`；`>`/`>>` 重定向加入复合分隔符）+ 2 nice-to-have（Edit 直命中测、空命令注释）。**138 passed**（含 27 新测）。Commit: 见 git log。详见 [release note](../releases/2026-08-05-posttooluse-rogue-guard.md)。

## [2026-08-05] feat(compile): agent prompt dev-residue lint + 洁净契约——根治 workflow 残留开发期信息

`tars validate` 加 `_check_prompt_dev_residue`：扫 AgentNode.prompt body 的开发期残留（plan/§节号、issue breadcrumb、Orca 源码路径、内部 examples 路径），命中即 warning（不阻断既有 workflow）；跳过 inline prompt；foreach body 同扫；同类别去重；operational 串零误报。新建 `orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（受众分离原则 + 禁止/允许表 + 测试夹具防火墙 + 受众翻转通读审查法）；CLAUDE.md / SKILL.md 加短引用。执行靠契约 + 受众翻转通读，deterministic lint 兜底。单测 23 例含全覆盖矩阵 invariant（防 regex 捕获组破坏类别映射）。Commit: `2c87e72`。详见 [release note](../releases/2026-08-05-agent-prompt-dev-residue-lint.md)。

## [2026-08-05] fix(kd-nas): codegen 禁造假数据——扩标准包白名单 + 反造假硬规则 + fail-loud/ask-user

修审计 run `6c2ebe` 发现的 KD-NAS 训练零学习真根因：codegen 因 `torchvision` 不在叶子 import 白名单 → 用 `torch.rand`+`torch.randint` 冒充 "ported verbatim" MNIST（teacher acc=0.12 锁死 ln 10）。修复 = (1) 扩白名单含 torchvision/PIL/numpy/scipy/sklearn + stdlib（禁用户项目模块保留）；(2) `fidelity_check.py` 加 `_check_no_random_fabrication` AST 扫描（torch.rand/randn/randint/normal/like 变体 + numpy.random.* + stdlib random.* + in-place uniform_/normal_/...）；torch.randperm 不入造假集（仅索引非数据）；用户 train.py 自身用 random 视为 verbatim port 跳过；(3) SKILL/agent/workflow doc/checklist/leaf skel/CONTRACTS/yaml 全套加反造假硬规则 + 'faithful mover, not designer' 原则；(4) CONTRACTS 删迁移叙事行 + flag-diff 表去「相对单体」；(5) 守门 regex 加 `已移除`/`相对单体`；(6) `--user_train/eval` 缺失改 rc=2 fail-loud（原裸 traceback）。**175 passed / 2 skipped**（原 169 + 6 新测），零回归；audit-run artifact 经新 fidelity_check 复测准确 4 处造假被捕获。Commit: `f22568b`。详见 [release note](../releases/2026-08-05-kd-nas-codegen-anti-fabrication.md)。

## [2026-08-05] feat(workflow): nas-supernet——nas-agent 重构版固化 + MNIST 端到端验证

新建 `nas-supernet` workflow（8 节点：超网生成→训练脚本→搜索脚本→超网训练→NAS搜索→选架构→retrain→可视化），把 nas-agent 最新版「3 skill + 5 subagent + Pipeline Memory」完整能力固化进 Orca，根治旧 `nas-agent-pipeline` 生成脚本语义偏离（缺 fidelity-verifier 守门）导致的 acc 问题。subagent 可移植文件存 `~/.orca`（read+embed，host-agnostic）；ns_visualize 6 图含静态文件回退；MNIST fixture；时延脚本规则（用户提供→onnx 包装，否则 PyTorch 内置）。3 轮 spec-review + logic review 全闭环，`tars validate` 通过。**MNIST headless E2E workflow_completed**：640 候选搜索、选定 latency=0.384ms<全开 0.82ms（时延下降）、retrain 最终 acc=0.9866、6 图全渲染。关键发现：opencode.db 膨胀（786MB）是长会话问题根因，reset 后全链通。旧 workflow 标 DEPRECATED。详见 [release note](../releases/2026-08-05-nas-supernet-workflow-rebuild.md)。

## [2026-08-05] fix(kd-nas): finalize JSON 改 json.dumps 发射——禁手写模板（P6）

修 KD-NAS finalize 节点 e2e 末段 JSON 结构性畸形（缺根级 `}` / depth=1）。根因：finalize inline prompt 用手写 ```` ```json ```` 模板填值发射，agent 易漏逗号/括号；其它节点（distill/decide）早已用 `python3 -c json.dumps` 安全发射，finalize 没遵循。修复 = `workflows/kd-nas.yaml` finalize inline prompt 新增 Step 3 `python3 -c json.dumps({...})` 发射 + viz 解析合并进单 try/except + stderr 显式告警（消除 Step 2/3 双层兜底不对称）；output_schema / Step 1（finalize_kd.py）/ Step 2 viz_kd_stage 调用 / routes / outputs 零改动。tars validate 通过；守门测试 `test_kd_prompt_no_source_narrative.py` 绿；kd-nas 测试套件 169 passed / 2 skipped；code-reviewer 一轮闭环 0 must-fix / 2 nice-to-have 已合并。真实 e2e 终态见 CURRENT.md。Commit: `4cd2428`。详见 [release note](../releases/2026-08-05-kd-nas-finalize-json-dumps-p6.md)。

## [2026-08-05] fix(exec): opencode events result 取末条消息——中间叙述不再抢 result（P5）

修 engine 核心 result 抽取 bug：opencode events 模式下 `RunAccumulator.events_result_text` 旧逻辑把所有 `agent_message` 串接，`result_extractor._first_balanced_block` 兜底取首个平衡块——KD-NAS flatten agent 中间叙述「input `[1,1,28,28]`」比末条合法 JSON 早出现，被误抓为 result（真实失败 tape `kd-nas-20260805-005130-ac3b11.jsonl` seq 98 复现）。SDD 契约本就声明「agent 最终消息 = JSON result」（sentinel specs），故修法 = `events_result_text` 改取末条（`_last_text`），`result_extractor` 不动（blast radius 最小：只影响 events 模式；claude `result_line` 路径不变）。决定性回归门：真实失败 tape 101 events replay → 抽出 seq 98 合法 JSON object，`"[1,1,28,28]" not in result_text`。tests/exec/ 全绿（440 passed + 1 skipped）+ tests/profiles/ 全绿（89 passed）。真实 e2e `kd-nas-20260805-011253-6c2ebe` flatten 节点 PASS（P5 原失败点直接验证）。Commit: `269e288` + `f9fe02c`（code-reviewer 一轮：YAGNI 简化 `_text_parts` → `_last_text` + text=None 边界测试）。详见 [release note](../releases/2026-08-05-opencode-events-result-last-message-p5.md)。

## [2026-08-05] fix(kd-nas): validate_contract 去同形 I/O 过约束——支持分类器族（P4）

修 test-agent 真跑 `examples/mnist_kd/`（MNIST 分类器 [1,1,28,28]→[1,10]）在 flatten 节点 fail 的根因：`validate_contract.py` check 7 旧逻辑要求 `forward output shape == DUMMY_INPUT.shape`，把输入当输出契约过约束（CONTRACTS §1 此前为 receiver 自编码器族设计，I/O 同形）。KD 本质只要求 teacher/student 共享输出 shape，不要求 output==input。架构层修复（向后兼容）：check 7 拆 7a（forward 实测）/7b（determinism 自检）/7c（可选 OUTPUT_SHAPE 声明校验）；CONTRACTS §1 加可选 `OUTPUT_SHAPE` 字段；flatten agent forward 捕获真实输出 shape 写入契约；teacher-gen / gen-student 加防御性双声明一致性检查。下游审计：tune_latency / export_onnx / gpu_probe / teacher_setup / measure_latency 均只用 DUMMY_INPUT.shape 构造输入张量，无残留同形假设。新增 / 重写 10 个测试用例（分类器族 PASS / OUTPUT_SHAPE mismatch FAIL / determinism FAIL / 类型错 FAIL / teacher 双声明三件套等）；274 passed + 守门 `test_kd_prompt_no_source_narrative.py` 绿零回归。code-reviewer 一轮闭环：0 BLOCKER / 0 MAJOR / 3 MINOR（2 修 / 1 保留有理由）。Commit: `24f3df0`。详见 [release note](../releases/2026-08-05-kd-nas-validate-contract-relax-same-shape.md)。

## [2026-08-04] fix(kd-nas): ORCA_WORKFLOWS_ROOT env 注入 + agent.md 去 cwd-relative + fail-loud 空 JSON 禁止

修 test-agent 真跑 `tars run` 暴露的 P0 架构问题：`kd-setup/kd-train-script/teacher-gen/struct-curator` 的 agent.md 用 `find workflows/agents/_kd_scripts ...` 这种 cwd-relative 查找，`tars run` 从用户项目起跑时 agent CWD ≠ Orca 仓库根 → fail。修复方案（OCP-clean，单通用 env）：executor 在 spawn 时注入 `ORCA_WORKFLOWS_ROOT`（= workflow yaml 所在目录绝对路径，dev=<repo>/workflows，安装态=~/.orca/workflows），agent.md 读 `$ORCA_WORKFLOWS_ROOT/agents/_kd_scripts` + 三档 fail loud（env 缺 / dir 缺 / file 缺）。plumbing 全链（build_env_overlay / make_executor / ClaudeExecutor / ScriptExecutor / Orchestrator 3 构造路径 / OrcaApp / RunManager / run_workflow 库 API / `python -m orca.run`）透传，kwarg 默认 None → 旧调用零回归。P1 强化：kd-setup 严禁块加「step 非零退出时禁止返回空字段 JSON 占位」（覆盖 deepseek 填 schema 倾向）。单测 +6（env_overlay 注入/缺省/共存 + executor 构造/spawn config 注入/缺省）；守门 + 全 suite 对比零回归（25 failed + 13 errors 全是 pre-existing test isolation）。Commit: `a952ecc`。详见 [release note](../releases/2026-08-04-kd-nas-workflows-root-env-injection.md)。

## [2026-08-04] refactor(kd-nas): P5 任务纯净度清扫——决策标签 / 历史叙事清除 + 守门测试强化

Phase 4 之后**审查过程决策标签**（D2/D8/D10/E1-E13/M1-M8/N3-N21/Q6-Q10/B4/R1-R3/F3/A4-A8 等非括号形式）pervasive 残留 + 3 个 agent description 含历史对比叙事 + CONTRACTS/yaml 夹带迁移叙事——本次纯文本层清扫闭环：agent prompt（5 agent.md + 1 leaf skel + kd-nas.yaml 4 处 + CONTRACTS 3 处）零过程标签零历史叙事；引擎 .py（migrate_flat ~15 处 / trainer / _resume / kd_reducer / finalize_kd）删尾部过程 ID + `code-reviewer Rx` 归属，保留设计 why 注释；守门测试 `test_kd_prompt_no_source_narrative.py` deny-list 分层强化——agent prompt 层加非括号决策标签 + 历史叙事词锁，.py 仅保留括号 + 复合源叙事锁（D7 边界），E402 noqa 双重免疫。零逻辑 / 契约 / CLI / 字段改动。测试 468 passed, 3 skipped（零回归）。Commit: `e3c2c2b`。详见 [release note](../releases/2026-08-04-kd-nas-trainer-engine-phase5.md)。

## [2026-08-04] refactor(kd-nas): Phase 4 agent prompt 去 SPEC 源化——任务纯净态

把 kd-nas workflow 的所有 agent prompt（10 节点 agent.md + 3 SKILL.md + references/templates/leaves/*.py.skel + workflow-checklists/*）+ 引擎库代码（`_kd_scripts/*.py`、`kd/*.py`）+ `CONTRACTS.md` + `kd-nas.yaml` 残留的**来源叙事**（SPEC §x.y / SPEC-REVIEW NX / spec-review mX / cleanup 20XX / v5 变更 / deleted / historical / port from / Phase N / plan §x / 决策标签 Q2·E4·N21·M8·D2·E6·A8 等）全部清除，改为任务纯净态（只描述「做什么 / 契约 / 输入输出 / 执行步骤 / fail 条件 / catch 协议行为」）。D7 客观边界执行：agent.md 零源叙事硬线；引擎 .py 留设计注释删源叙事；CONTRACTS 保留 §N 章节号删 changelog。distill agent.md 补 M8 ofd 重试提示（fail-loud → 降级 mse-only 重跑 step 3+4）。新增反回归测试 `tests/workflows/test_kd_prompt_no_source_narrative.py`（deny-list grep 守门）。基线 128 命中 → 完成后 0；测试 467 passed, 3 skipped（deselect 1 个 pre-existing `nas-supernet.yaml` 解析失败，与本 Phase 无关）。Commit: `204a64d`。详见 [release note](../releases/2026-08-04-kd-nas-trainer-engine-phase4.md)。

## [2026-08-04] feat(workflow): nas-supernet workflow YAML——DAG + inputs + select 契约

新增 `workflows/nas-supernet.yaml`（298 行，纯增量）——把 7 个已实现 `ns_*` folder-agent 接成完整 DAG（expand → train-script → search-pipeline → run-train → run-search → select → retrain → $end）+ 2 fail-loud terminate（model_type 不支持 / select 无候选）。5 inputs（plan §12 三档标签：3 [ask] 必填 + 1 [advanced] + 1 [default]）。7 节点 output_schema 全 `additionalProperties:false` 逐字对齐各 agent.md `## 输出` JSON 块；run_* 节点 status enum 按实际产出收窄（ns_run_train 含 skipped；ns_run_search / ns_retrain 仅 [executed, failed]）。路由守卫：`ns_expand_supernet.model_type_supported != false` + `ns_select.selected_arch truthy AND pareto_size > 0`（plan §4.1 note：不用 `is defined`）。验证：manual rule-trace 全 9 项 `_check_*` 通过 + node + js-yaml 结构自检；`tars validate` 因 shell 无 Python 未实跑。**已知 BLOCKER（不在本 commit 范围）**：`ns_select/agent.md:69` 文档字符串 `{{ ns_select.output.selected_arch }}` 自引用 → Jinja2 parser 误判 → `_check_self_reference` error；修复=反引号内套 `{% raw %}...{% endraw %}`（contract §5），follow-up。code-reviewer 一轮（1 BLOCKER 确认 + 5 MINOR，2 applied / 3 surface disagreement with reasoning）。Commit: `e02245c`。详见 [release note](../releases/2026-08-04-nas-supernet-yaml.md)。

## [2026-08-04] feat(workflow): nas-supernet 生成节点 agent 三件——expand / train-script / search-pipeline

把 nas-agent 的 3 个源 SKILL.md（`expand-to-supernet` / `supernet-train-script` / `nas-search-pipeline`）最小适配成 Orca folder-agent（`ns_expand_supernet` / `ns_train_script` / `ns_search_pipeline`），references / assets 原样迁移（保 `workflows/` + `workflow-checklists/` 兄弟结构），严格遵循 plan v5 §9.1 八条替换 + read+embed subagent 协议（fresh-Task loop）。ns_search_pipeline 含 §10 时延分支（默认 PyTorch `measure_module_latency` / 提供 latency_script_path 时 onnx 单文件禁 .data）+ §7.2 增量生成 `select_architecture.py`（schema-aware JSON 契约供下游 ns_select Bash 调用）+ Non-Searchable Logic freeze + NPU foreach=False。3 节点 output_schema 含 `fidelity_passed` / `workflow_verifier_passed` / `error`（命名偏离 `nas-train-runner.last_error` 因 generation 节点无 retry）。**纯增量零覆盖**——不碰既有 supernet-train-script / nas-search-pipeline / nas-train-runner / nas-hp-search.yaml / nas-agent-pipeline.yaml。code-reviewer 两轮闭环（第一轮 1 must-fix + 2 should-fix + 2 minor；第二轮 1 must-fix + 3 should-fix + 2 minor，零遗留）。Commit: `83552da`。详见 [release note](../releases/2026-08-04-nas-supernet-generation-agents.md)。

## [2026-08-04] refactor(kd-nas): Trainer 引擎化 Phase 3——产物拍平 + logs 折叠 + 原子迁移

把 durable artifacts 从 `${PROJECT_ROOT}/artifacts/kd-nas/` 拍平到 `${PROJECT_ROOT}/artifacts/`（去 kd-nas 层，D6）+ 删 `logs/` 顶层目录（M1：日志走 per-run `runs/<exp>/train.log`）+ 新增 `migrate_flat.py` 5 步原子迁移（copy → rewrite `Path.relative_to` 全字段 → 行数校验 → os.replace → sentinel → rmtree）+ sentinel 幂等（数据安全契约：sentinel 在但 kd_old 内容行数变 → fail loud 拒绝 rmtree）。全字段 rewrite：`ledger.{ckpt,student_path}` + `champions.{snapshot}` + `teacher_meta.{teacher_onnx,teacher_cache,teacher_ckpt}`；per-run `teacher_model_path` 禁 rewrite。`tune_cache.json` 不迁移（R2）。`distill` 删 `DISTILL_LOG` alias + `train-teacher` 删 `meta/teacher_train.log` 兼容 cp（logs 直读 per-run）。`model-flatten` agent.md + SKILL.md OUTPUT_DIR 同步拍平（code-reviewer R1 闭环：SKILL.md 漏改已补 + 测试扩扫）。`teacher_setup.py` 修预存 mkdir subdirs bug。16 新单测 + 3 预存失败测试同步修。零回归（281 passed, 3 skipped）。code-reviewer 一轮闭环（5 must-fix 全修：SKILL.md drift / sentinel 数据安全 / silent-skip WARN / 坏 JSON 测试 / teacher_meta 缺失分支测试）。Phase 1 引擎 + Phase 2 codegen 零改动。Commit: `b2ce694`。详见 [release note](../releases/2026-08-04-kd-nas-trainer-engine-phase3.md)。

## [2026-08-04] feat(kd-nas): Trainer 引擎化 Phase 2——接口原子切换（叶子化 codegen + 5 调用点切固定引擎）

把 `kd-train-script` codegen 从产单体 `train_pipeline.py`（5 inline user_* slot）切到产 4 叶子（`user/{loss,data,eval,optim}.py`）+ `run_config.yaml` + `run.sh`。下游 5 调用点（train-teacher train+eval / distill train+eval / finalize eval）按 §3.3 矩阵原子切到固定引擎入口（`_kd_scripts/train_pipeline.py`）+ inline flag + `--artifacts_dir` per-run。**distill 移除 inline `--kd_config`（E4），唯一真相源 = run_config.yaml**（每轮 AST 决策 read→patch kd_config 字段）+ redirect stdout → `runs/r${ROUND}_student/train.log`（E13/M1）。**finalize eval champion 三字段强制 inline**（student_model_path / build_cfg / student_ckpt；矩阵第 5 行硬约束）。`fidelity_check.py` 重写为 `--leaves_dir` 模式 + AST 自包含（Q6）+ AST 签名相等（E9）+ kind 方向硬校验（D2）+ 数值等价。`kd-train-script` SKILL/agent/workflow doc/checklists 全部重写为叶子化契约 + D8 AST 检测；删旧单体模板（692 行）；任务纯净形态。`train-script-verify` 重写为 4 叶子并行 review + 引擎 smoke + workflow-verifier。CONTRACTS §3.1 flag diff 表（M6）+ 调用点 × 字段 × 数据源矩阵（N1）+ §6 叶子契约节 + E7 错位修正（「`train_teacher 调`」）。零回归：164 passed（含 Phase 1 引擎 33 测 + 新 leaf codegen 19 测 + finalize 13 测，含顺手修 2 个预存 mkdir bug + sys.path 污染）；3 个 test_struct_kd_p7 预存失败（git stash 验证无关）。code-reviewer 一轮闭环（1 must-fix + 1 决策 + 2 minor）。Commit: `7870b2a`。详见 [release note](../releases/2026-08-04-kd-nas-trainer-engine-phase2.md)。

## [2026-08-04] feat(test): nas-supernet E2E MNIST fixture——小 CNN + train/test + 离线降级

`tests/e2e_nas_supernet/fixtures/mnist/`——为 `nas-supernet` workflow（计划 `2026-08-04-nas-agent-pipeline-rebuild.md` §17）准备的 E2E 目标项目：标准 LeNet 风格两层 CNN（参数化 conv1/conv2/fc_hidden，可被 expand-to-supernet 张开）+ CrossEntropy 训练 + test 集评估。复用判定：`examples/mnist_kd/` 带 kd-nas 专属契约（KNOBS/feature_hook_names/latency_provider），故**仅复用其 CNN 架构设计**、新建干净用户项目，让 expand 节点真实发现搜索维度。落点对齐 `tests/e2e_mxint/target_project/` 先例；全程 `pathlib`；离线降级 `torch.randn`（stderr WARNING，仅冒烟）；空 loader raise（fail loud）；code-reviewer 一轮闭环（M1 空 loader raise / S1 argparse 边界 / N1 weights_only / N2 num_classes 注释），保留 `test.py` 文件名（显式 spec 优先）。不改 `workflows/`/`orca/`。Commit: `bfe857f`。详见 [release note](../releases/2026-08-04-nas-supernet-mnist-fixture.md)。

## [2026-08-04] feat(examples): 真实 MNIST 分类项目（kd-nas 真实输入）

`examples/mnist_kd/`——LeNet 风格两层 CNN，torchvision MNIST + cross-entropy，CPU 1-epoch test_acc≈0.98。契约兼容（CONTRACTS §1）：`build_model`/`DUMMY_INPUT=[1,1,28,28]`/`KNOBS`（conv1/conv2/fc，step<0）/`feature_hook_names`；`train.py` 暴露 `compute_loss`+re-iterable `build_dataloader`；`eval.py` top-1 acc；`latency_provider.py` ONNX 实测（cuda 缺失 WARN、文件缺失 fail loud）。零 orca import——genuine user project；code-reviewer 一轮闭环（空 loader raise / cuda WARN / `--epochs<1` 拒绝）。Commit: `2d9b5ff`。

## [2026-08-04] feat(kd-nas): Trainer 引擎化 Phase 1——KDTrainer 孤儿 + 叶子 skel + 33 单测（不改 DAG）

新增固定引擎层：`_kd_scripts/kd/trainer.py`（`KDTrainer` + `TrainConfig`，三 mode；distill 严格走 Q2 hot-order：prepare→kd_parameters→opt；emit 双协议 stdout keys + 日志前缀字面命中 metrics_tail._LOSS_LINE_RE；M1 只 print stdout / M3 scheduler None 守卫 / M4+B6 proxy_mse / B5+Q18 orca.chart lazy import 降级 / D3 resume hash+mode fail loud / B4 scheduler drop WARN / R1 latest.pt 无 abs path）+ `_leaves.py`（importlib 单文件 + AST 签名 + 自包含 deny-list + lazy exec，D9-c）+ `_resume.py`（原子 tmp+replace + sort_keys sha16）+ `train_pipeline.py` 孤儿入口 + 4 个 leaves skel + 33 单测（含 Q2 顺序 spy 断言 + 端到端 resume + 6 个 kind 方向参数化 + R1 无 abs path）。**严格隔离 N4**：DAG / gen_train_script emit / kd-nas.yaml / `references/templates/train_pipeline.py` / kd 库 / CONTRACTS 全未改（git diff 空）；Phase 2 才原子切 emit + 5 调用点 + 删旧模板。code-reviewer 四轮闭环（F0/F1/F2/F3/F4/F5/F6/F7/F8/F10 全修）。Commit: `6929685`。详见 [release note](../releases/2026-08-04-kd-nas-trainer-engine-phase1.md)。

## [2026-08-04] feat(web): workflow/agent 文件浏览器（只读）——/workflows 页 + 5 API + prism 高亮

新增 Web 纯只读浏览页 `/workflows`：列 workflow → 看引用 + 全量 agents → 点 agent 文件树 → 看文件内容（md 渲染 + .py prism 高亮）。后端 `routes/workflows.py`（无 manager 薄封装 compile loader）+ `_safe_resolve` 守卫（symlink 双查/null byte/越界）+ file endpoint 1MB cap & 二进制检测→422；前端 `workflow-browse-store`（plain zustand）+ `WorkflowsPage`/`WorkflowBrowsePage`(lazy)+`FileTree`+`CodeViewer`，prism CSS 进 browse chunk 不污染 `/runs/:runId`。spec-reviewer 4 blocker 闭环 + coder code-review 3 项闭环 + test-agent 真机 16 断言全过；回归后端 203 / 前端 487 零红；现有 route/页面/`main.tsx`/`FileContentView` 零改。**build 产物未含此 commit**（本机 python 环境受限），需另行 `npm run build`。Commit: `0103235`。详见 [release note](../releases/2026-08-04-web-workflow-agent-browser.md)。

## [2026-08-04] feat(in-session): 失败哨兵 + 失败历史注入——next 单一校验关口收口（根治 run 孤儿化）

子代理自报失败经 `orca_node_failed_v1` 哨兵 → 引擎 `agent_blocked` recoverable → 重 arm 并把连续失败历史（**含本次**）确定性注入 prompt。主 session 改哑管道（永远喂 next，唯一例外 ask-user 哨兵），根治「见失败截胡变 executor 致 run 孤儿化 + 前端不出第二轮」——`orca status` 期间恒显 running。SPEC v3 经两轮 spec-reviewer 对抗闭环（R-N2「含本次」收紧获认可）；57 单测/集成 + 真实 `orca` CLI E2E 全绿；`_step_io`/`cli`/`daemon`/`events`/前端 **零改**（generic 透传，AC12/AC14 守门），不侵 in-session 灵活度（引擎不 dispatch、不决定 fresh/复用）。Commit: `35fdf78`。详见 [SPEC](../specs/2026-08-04-in-session-failure-sentinel-and-injection.md) + [plan](../plans/2026-08-04-in-session-failure-sentinel-and-injection.md)。

## [2026-08-04] fix(web): 看板重设计 test-agent 真机实测闭环——KPI live-pending + blocked 架构 draft

test-agent 真机实测 18 PASS/0 FAIL，抓到 2 个只有真机才能发现的缺陷（mock 边界假绿）：① KPI 运行计数漏算 live-pending（`RunListPage` kpiCounts `{running,queued}` 与 `group-runs` 排队桶 `accept={queued,live-pending}` 不一致，4 胶囊合计 ≠ total）——已修 kpiCounts+filter 补 live-pending，SPEC §2.2/AC-B2/B3 同步，vitest 99 pass（`76073ef`）；② 待决策(blocked) 死 UI——后端 `RunStatus` 无 blocked（仅 node 级 gate/interrupt 投影），继承的架构裂缝，另开 [`run-blocked-status-design-draft.md`](../specs/run-blocked-status-design-draft.md) 跟进（方案 A 后端 fold），本次保留 UI 待激活。SPEC 升 v1.2（`b6d5034`）。详见看板 release note + draft。

## [2026-08-04] feat(web): 看板卡片网格重设计——横向列→KPI概览带+section垂直堆叠+卡片网格

Web 主页 `/` 看板从 Trello 式横向 5 列重设计为「KPI 概览带（可点过滤）+ 分组 section + 卡片网格」；去 cost、状态只画一遍、失败/待决策提级。SPEC `web-board-cardgrid-redesign.md` v1.1 逐字实现，code-reviewer 6 项反馈闭环。Commit: `ca5c07a`。详见 [release note](docs/releases/2026-08-04-web-board-cardgrid-redesign.md)。

## [2026-08-04] fix(kd-nas): gpu_probe teacher_cache 可选（device-only）+ setup step3 grep bug + flatten review R1（未 commit）

- `gpu_probe.py`：`--teacher_cache` 改可选 + 新增 `_probe_device_only`；`_main` 分 VRAM/device-only 两路。
  根因：串行版 setup 在 teacher 训练前跑 gpu_probe，旧版把 baseline `.py` 契约当 `--teacher_cache` →
  `torch.load` UnpicklingError → workflow_failed。串行版 concurrency 恒 1，只需 device。
- `kd-setup/agent.md` step3：删 `--teacher_cache`（走 device-only）+ 修 pre-existing grep bug
  （`^DEVICE:`→`^RESOLVED_DEVICE:`，旧 grep 永不命中致 device 总 fallback）。
- flatten review R1 闭环：step3 去后缀落定确定性 python 片段（`split(' (low-confidence')`+`os.path.abspath`，
  与 setup 逐字对齐，Rule 5/DRY）；测试升级断言确定性代码。
- 验证：`tests/workflows/` 421 passed / 5 预存失败 / 0 新红。
- review 第二轮闭环（7 项）：gpu_probe DRY 抽 `_resolve_backend`/`_probe_gpu_inventory` helper +
  llm_artifacts fallback 文档统一（4 处 stale 删）+ CONTRACTS §4 表整表对齐 yaml required +
  PROJECT_ROOT_IN 改 inline argv + device-only 测试 docstring 诚实标注 + setup CONCURRENCY grep
  死代码删 + device enum 注明 resolved。详见
  [gpu_probe release](docs/releases/2026-08-04-kd-nas-gpu-probe-teacher-cache-optional.md)。

## [2026-08-04] fix(kd-nas): flatten 产物落项目 artifacts 根 + 删 baseline latency bar（未 commit）

两处 drift 修复：
- flatten `<output_dir>` 从 `$ORCA_ARTIFACTS_DIR`（per-run `runs/<run_id>/`）改为
  `${PROJECT_ROOT}/artifacts/kd-nas/models/baseline/`（与 setup `kd_artifacts_dir` 同根，跨 run 持久；
  flatten 先于 setup 执行故自算 PROJECT_ROOT；PROJECT_ROOT=dirname 总非空，无 fallback）。
- 删 `viz_kd_stage` baseline stage（单柱 bar 与 setup `baseline_seed_table` 冗余）；flatten 不推图，
  `viz_status` 固定 `{env_status:skipped}`（字段保留，保 schema 统一纪律）。SPEC/yaml/CONTRACTS + 测试同步。
- 验证：tests/workflows/ 418 passed / 5 预存失败（HEAD 已存）/ 0 新红。详见
  [release note](docs/releases/2026-08-04-kd-nas-flatten-artifacts-dir-and-drop-baseline-bar.md)。

## [2026-08-04] refactor(kd-nas): 死代码清理 + review 修复（5 commits）

SPEC `2026-08-04-kd-nas-dead-code-cleanup.md` 全 4 节闭环 + code-reviewer 反馈全闭环：
- §1 review 修复（commit `323a0a4`）：`_LEDGER_STATUS` 加 FAIL_accuracy/export；teacher_setup
  `_parse_accuracy` TEACHER_ 优先于 STUDENT_；viz_kd_stage + finalize_kd hue/met_* 字面对齐
  （student 行 lowercase true/false，baseline/teacher 行 "ref"）；16bd8b5 release note 补全。
- §2 Phase 1 纯删（commit `61a6e45`）：gate_all / distill_dispatch / train_pool / viz_kd /
  kd-select / _deprecated/ 全删（脚本 + agent 目录 + ~60 测 + 11 obsolete skip）。
- §3+§4 迁移+文档清扫（commit `3bcc1d2`）：pick_variant / measure_student / setup_helpers /
  teacher_model 全删；可复用不变量（KNOBS 校验 / 绝对基线对比）迁 kd_common；
  CONTRACTS.md / SKILL.md / README.md 同步。
- docs（commit `ff65b14`）：release note + CHANGELOG 索引。
- review 闭环（commit `ad14436`）：1 FATAL（ALL_TERMINAL_STATUSES 缺 FAIL_build）+ 5 MAJOR
  （parse_accuracy 死代码删 + 4 处虚假 docstring 纠正 + train_pipeline_script_generation.md
  train_pool 误述）+ 3 MINOR + 1 NIT。详见
  [release note](docs/releases/2026-08-04-kd-nas-dead-code-cleanup.md)。

验证：pytest tests/workflows/ → 5 预存失败（HEAD 已有）/ 0 新红 / 总数 -148 死代码。

---

## [2026-08-03] feat(kd-nas): 时延单位统一 us + KD loss 强制 + teacher 评估 + 全模型总表

四项串行化改动（commit `16bd8b5`）：(1) latency ms→us 全链重命名（修 1000× 误判 FAIL_latency）；
(2) distill 模式 KD loss 强制（`compose.py` 构造期空 kd_losses → raise）；(3) teacher 评估复用
train_pipeline --mode eval（精度进 teacher_meta）；(4) `viz_kd_stage._push_all_models_table`
+ finalize_kd final_report.md 同步「All Architectures」总表（baseline + teacher + students + champions）。
309 passed / 0 回归。详见 [release note](docs/releases/2026-08-03-kd-nas-us-kd-mandatory-teacher-eval-master-table.md)。

---

## [2026-08-03] fix(kd-nas): 特征蒸馏 fail-loud 守卫 + 终态帕累托/FAIL 图 + CONTRACTS 串行化

SPEC `2026-08-03-kd-nas-fail-loud-chart-cleanup.md` §1/§3/§4/§2 收尾：`kd/compose.py`
加守卫（含 ofd/fitnets/rkd 且运行时 feats 空 → raise）；distill/gen-student agent.md 的
KD_CONFIG 改 AST 条件化（修 F2 dormant bug：旧 `grep '^def'` 漏判缩进 class method → ofd 永远剥离）；
`viz_kd_stage --stage final` 新增 pareto_front + fail_status_bar（port viz_kd 语义非 import，
为 followup 删 viz_kd 铺路）；finalize final_report.md 加 `## Search Outcome` + §4 bool/latency 一致性；
CONTRACTS.md 重写串行 v5 + 4 脚本加 DEPRECATED docstring 头。Commit: `d9fcd9c`。详见
[release note](docs/releases/2026-08-03-kd-nas-fail-loud-chart-cleanup.md)。

---

## [2026-08-03] fix(web): 双路 review 闭环——实现时序漏洞 + 看板视觉放大 + 死代码清理

分两 agent 对 RunList 重设计做 review（实现 code-reviewer + 美观真机截图）。实现侧闭环 6 MAJOR：
选择求交改用未过滤 runs（切 chip 保留选择，SPEC §3.3）/ expandAll·collapseAll 合并语义（跨 dim 不擦写，§10.8）/
单删成功 toast + 不清整个选择集 / Shift 范围选收窄到当前桶（§5.5）/ 去 project_id-as-path / + parseProgress 认后端
`"done/total"`（原 parseFloat 截成 3%→43%）等。美观侧：BoardCard padding、非状态 dim 列加默认左条（看板不再退化成
带底色列表）、列底色 /0.45、竖条 w-1、显示空加字、暗 mode 删 icon 可见。删 deletingIds 死代码链（乐观移除使
in-flight opacity-40 不可达）。补 M-1/M-2/M-4 cross-scenario 单测。vitest 465 / build OK / R3 无命中 / 后端 diff 空。
SPEC §6.5/§5.6 同步消歧。Commits: `93f931d` + `c58172a`。

---

## [2026-08-03] feat(web): RunList 加分组维度选择器（状态/项目/workflow/时间）+ 空桶自动隐藏

用户反馈「要更多分区 + 按项目分类 + 排队/待决策空列没用」的增量（在 RunListPage 重设计之上）：
**分组方式下拉**（替换旧 groupBy on/off toggle）支持 不分组/状态/项目/workflow/时间 五维度，看板列/列表段随 dim，
持久 `orca-runlist-groupby-v1` 默认 status，共享 `groupRuns` 单出口（DRY）；**空桶默认自动隐藏**（解决排队/待决策空列噪音）+
「显示空」toggle + 待决策>0 高亮；折叠持久泛化 `use-collapsed-buckets`（`Set<dim:key>` + key v2，切 dim 各自独立）。
前端唯一 / 零新依赖 / R3 不违 / 后端零改。vitest 456 / Playwright 真机 10 / 后端回归 31 全绿；E2E 抓到并修了 stale 五列用例。
SPEC：`docs/specs/web-runlist-redesign.md` §10.8-10.10。详见
[release note](../releases/2026-08-03-web-runlist-redesign.md)（增量段）。Commits: `7cd8328` + `13f60d5`。

---

## [2026-08-03] feat(web): RunListPage 重设计——看板 + 列表 toggle + 多选/排序/批量删/折叠持久/修主题

Web 主页 `/` 重设计：**默认状态列看板**（排队/运行中/待决策/已完成/失败，运行中·待决策聚焦）+ **列表 toggle**
（双行顶栏、无 border 分组容器、项目头美化、多选三级+Shift 范围选、6 字段排序、批量删除部分失败对账、
放大常显删除）。三类 localStorage 持久化（折叠/排序/视图）；主题按钮真切换；WS 断线退避重连；
refresh `inflightSeq` 防 stale 覆盖；`pendingDeletes` 防幽灵 run；`reset()` epoch 守卫；focus trap；
搜索/待决策穿透折叠；三态加载。**前端唯一 / 零新依赖 / R3 不违 / 后端零改**（AC-18 后端回归 43 passed 旁证）。
过程：D1/D2/D3 三 agent 并行审查 → SPEC → spec-reviewer 对抗闭环（2 FATAL+多 MAJOR）→ coder-agent
（自带 code-review 0 FATAL/4 MAJOR/3 MINOR 全修）→ test-agent 真机 E2E（WSL+chromium：
Playwright 9 passed + vitest 437 passed）。E2E 抓到并修了 vitest 漏掉的 AC-4 折叠持久 regression（reload 擦写 storage）。
SPEC：`docs/specs/web-runlist-redesign.md`。详见
[release note](../releases/2026-08-03-web-runlist-redesign.md) + [E2E 证据](../releases/2026-08-03-web-runlist-e2e.md)。
Commits: `d782335` + `1f8e5cd`。

---

## [2026-08-03] refactor(kd-train-script): 模板占位符 → 基于用户代码强制特化生成

kd-train-script 生成策略重写：**根除「拷贝模板 + 填 `{{...}}` 占位符」模式**（用户指出远程产物残留占位符、
loss/eval 指标与用户原逻辑不符）。对齐 NAS-AGENT-PIPLINE 的「读用户代码 → 特化生成」设计：
骨架模板（协议机械：CLI/三 mode/ckpt schema/stdout 协议/live push/fail-loud）保留，
**删除** 4 个占位符常量 / `_placeholder_*` dummy fallback / `_load_user_*` 运行时 importlib 注入 /
4 个 `--user_*` 覆盖 flag；新增 5 个固定 slot（user_compute_loss / user_build_dataloader /
user_eval_metric / build_user_optimizer / build_user_scheduler），未填 = NotImplementedError fail loud
（非静默 dummy）。**校验升级为四层**：静态无残留 AST 扫描 → 三模式 smoke（无覆盖 flag，脚本必须自带搬入逻辑才能跑）
→ 新写 `fidelity_check.py`（数值级等价性：同种子同输入下用户 loss/eval 函数 vs 搬入版 torch.allclose）→
workflow-verifier 交叉核对（C21-C24 无残留/loss 逐字/eval 逐字/fidelity 证据 + C5/C17/C20 清理）。
train-script-verify 升级（替换会 vacuous pass 的旧 substring 检查 → 5 接口 grep + fidelity 复核）。
设计草稿 `docs/specs/kd-train-script-rework-design-draft.md` 经独立 review agent 比对闭环
（PASS with conditions → 5 MAJOR + 7 MINOR 全并入）；实现经 code-review 两轮闭环（1 CRITICAL frontmatter
YAML + 3 MINOR 修复；3 个过时测试补 obsolete skip）。全量回归 **240 passed, 14 skipped, 0 failed**。
未 commit（待用户确认）。

---

## [2026-08-03] feat(kd-nas): 串行迭代重写（替代批量并发，SPEC v3）

把 `kd-nas` 从批量并发（gate_all → train_pool → select）重写为**串行迭代 KD 蒸馏**（10 节点 DAG：
flatten→setup→gen_teacher→gen_train_script→train_script_verify→train_teacher→
gen_student→distill→decide[loopback]→finalize）。每轮 1 个 student（首轮固定规则缩1层+FFN→pointwise
/ 迭代轮 KB+perf 驱动，DUMMY_INPUT 字节级校验防 LLM 写死 shape）→ KD 蒸馏（--kd_config mse+ofd+EMA
recipe 必传）→ min-latency champion ratchet（FIFO tiebreak）+ continue_loop 决策。teacher 用用户默认
lr/epochs（从 user_train_script 提取，非硬编码）。全程 metrics 推 web（每节点 viz_kd_stage sidecar）。
显式 catch 协议（业务失败 rc≠0 → FAIL_* 落账 continue；系统失败 → workflow_failed）。新 4 脚本
（kd_reducer / viz_kd_stage / metrics_tail / finalize_kd）+ 新 5 agent（train-script-verify /
train-teacher / gen-student / distill / decide）+ setup 精简（拆出 train_teacher）+ model-flatten/
teacher-gen 扩 viz_status。tars validate 0 error；43 新单测 + 73 旧 kd_redesign 通过（11 obsolete 跳过）。
SPEC v3 spec-reviewer 三轮对抗 PASS。Commits: `b3c3c91` + `aa6e5d7` + `a230ebe` + `d03e4c9`。
详见 [release note](../releases/2026-08-03-kd-nas-serial-iteration.md)。E2E 真机留 task #6（需 GPU）。

---

## [2026-08-03] chore(audit): Orca 真实代码审查 5 聚类全流程交付（A/B/C/D/E）

**干了什么**：8 维度 fan-out 真实代码审查（24→52 agent，对抗验证）→ **44 raw → 26 confirmed / 18 rejected**，26 条全带 file:line 证据。按根因聚类 A–E，每个走完整 SDD：写 spec → 多轮对抗 spec-review（A/B 各 4 轮、C 7 轮+rev8、D/E 各 2 轮）至 pass → coder 实现 + 单测 → test-agent 真机 E2E → 每改动 commit + CHANGELOG。零 follow-up。

**解决了什么问题**（业务影响）：
- **A — stop 污染 tape**：`orca stop` 无脑向**已结束**的 run 追加第二条「已取消」事件，破坏 append-only 唯一真相源、使 web 状态机与统计错乱。→ stop 先扫 tape 终态，已终态幂等短路。
- **B — resume 重跑**：崩溃恢复时重跑**已完成/已跳过**的节点（重复烧 token、重复外部副作用），破「单 tape 重放=同态」铁律。→ resume 起点改按 node 终态判，覆盖 nc/skipped↔route_taken 两窗口。
- **C — Bug2 永久空白**：前端加载失败仅 `console.error+return`，用户看到**永久空白页**零提示。→ 四处 loader 加用户可见退避重试 + 错误态 + defer-RESUME + ErrorBoundary。
- **D — auto-exit 误杀 + 守护竞态**：web 默认 auto-exit 撕掉用户在浏览器起的**并发 run B**；sidechain pidfile 竞态级联 respawn；macOS 守护误判 dead。→ auto-exit 加「无非终态 in-process run」第三条件 + pidfile 原子写 + macOS liveness。
- **E — discovery 撒谎**：legacy run 列表**硬编码「已取消」**，与 tape 真相脱钩（实际 completed 的 run 被误标 cancelled，用户误重跑浪费 token）。→ legacy 走 tape 派生。

**结果**：5 实现全落地，单测全绿（A 126 / B 81 / C 384 / D 38 / E 182）；真机 E2E **A/B/D/E PASS、C 受限于无浏览器（HTTP/store/bundle 层 + 32 vitest 全过）、零真实 bug**。证据 `docs/releases/2026-08-02-audit-e2e-evidence.md`。Commits: A `08cb7b0` / B `f627196` / C `ccb8d7a` / D `c0cdd23` / E `50a52fe`。各聚类详见下方分条 + [release notes](../releases/2026-08-02-audit-e2e-evidence.md)。

---

## [2026-08-02] fix(web): legacy discovery 走 tape 派生 + handle.status hint 文档化（真实审查聚类 E）

`run_manager.discover_runs` legacy 分支从硬编码 `RunSummary(status="cancelled",...)` 改为复用与 attached 同款的 `_summary_from_tape(tape_path, source="legacy")` 从 tape 派生 status/progress/cost/elapsed/event_count（DRY）；`seen_ids` 跨 attached/in-memory/legacy 三分支显式 dedup（写入优先级 in-memory>attached>legacy，机制故意非对称）；`_summary_from_tape` 内层 except 补 `logger.warning`（N1，AC5「含 run_id+tape path」）；run_id 不一致 warn（E5，返回取 tape_stem，覆盖三源）；`handle.status` 实时 hint 非权威文档化（方向 A，不改代码——B/C 破依赖方向/职责重叠）；in-memory 分支 status/event_count perf 豁免 docstring 标注。**E9 破坏性变更**：crashed legacy run 显示 cancelled→live-pending/running（release note 声明）。round-2 pass + coder 自我 review 0 BLOCKER，web iface 套件 182 测试绿（16 新）。Commit: `50a52fe`。详见 [release note](../releases/2026-08-02-audit-e.md)。

## [2026-08-02] fix(web/in-session): 并发守护竞态修复（真实审查聚类 D）

`orca run` web-default 的 `_wait_ws_autoexit` 加第三条件 `AND 无非终态 in-process run`（新 `RunManager.has_nonterminal_inproc_runs() -> bool` 只读 helper），防主 run 终态后 auto-exit 撕掉用户在 web UI 起的并发 run B（架构问题：auto-exit 决策权限越界到整个进程生命周期）；sidechain pidfile 改原子写（tmp+`os.replace`）+ 退出 ownership 比对再 unlink（防级联 respawn）；`_daemon_liveness` 加 macOS 分支（`os.kill(pid,0)` + `ps -o args=` 子串匹配，kill-0≠活 zombie 仍响应 cmdline mandatory，不引 psutil）；`ws_handler._on_run_changed` QueueFull 加 warn（仅帧 payload，`_cleanup` None-sentinel 除外）。**M1 行为变更**（并发 run 卡 gate 永不终态 → 进程不再 auto-exit 撕 B，需 Ctrl-C）+ 上游 `web-attach-and-default-spec.md` 第三 conjunct 勘误。round-2 pass-with-minor-caveats + R-1..R-4 裁定，coder 自我 review 0 BLOCKER，38 测试绿。Commit: `c0cdd23`。详见 [release note](../releases/2026-08-02-audit-d.md)。

## [2026-08-02] fix(web): 前端 fail-loud + defer-RESUME + Bug2 修复（真实审查聚类 C）

四处 loader（loadRun/loadRunWithMeta/loadFull/loadEarlierChunk）HTTP 非200 / parse 失败 / `!Array.isArray(json)` → 用户可见退避重试（3 次）+ `loadError` 错误态（**= Bug2 前端根因**，非 `console.error+return` 静默）；**INV-7 defer-RESUME**：initial-load loaded/error → `sendResume since=lastSeqSeen`（subscribe forward-only，只有 resume 重放 seq>since）+ per-socket `resumeSent` dedup + reconnect 保留 `sendSubscribe` 作 server-restart lazy-mount fallback（reverse A5：`_handle_resume` handle=None short-circuit，唯一 `ensure_attached` 在 `_handle_subscribe`）；listener `useEffect` 顶层注册 + one-shot 自清；ChartRenderer `partitionCharts` cast+reject + **ErrorBoundary**（LazyChartWidget 内/ChartWidget 外）+ ChartGroup `key={chart.identity}`；`seenSeqs: Set` + `enableMapSet` O(1) 去重收进 refold；`retryCount` 进 store + 退避期 loading 叠加非阻塞 retry-banner；AbortController + `moduleEpoch` 防 A→B→A 陈旧 fetch 污染。7 轮对抗 spec review PASS + coder 两轮自我 review 0 BLOCKER，384 测试绿（31 新）。Commits: `ccb8d7a` + `6aa7d5f`。详见 [release note](../releases/2026-08-02-audit-c.md)。

## [2026-08-02] fix(run): resume/重放幂等裂缝修复（真实审查聚类 B）

B2：`node_skipped` reducer 写 `context[N]=None` + live 去 `"skipped":True`（消除 live/resume 视图发散，破「单 tape 重放=同态」铁律）；B1：resume 起点改 `status∈{done,skipped}` 双判据 + 新 `replay_for_resume(tape)->(state,inputs,last_progressed)` 单 pass fallback 追踪 done/skipped（覆盖 `[node_completed/route_taken]` 与 `[node_skipped/route_taken]` 两窗口，不重跑已完成/skip 节点）；B3：删 `RunState.usage` 字段（保留 `UsageSummary` 类）+ `run_manager._extract_cost` 切 `projections.node_usage`（顺带修 `cost`→`cost_usd` 潜伏 typo）；`from_tape` 全 tape 读 4→2 真承诺；删死代码 `_inputs_from_tape`/`_find_last_done_node_name` + AST 守门。4 轮对抗 spec review 闭环 + coder 自我 review 0 BLOCKER，目标 suite 81 测试绿。Commits: `f627196` + `9fdb6b9`。详见 [release note](../releases/2026-08-02-audit-b.md)。

## [2026-08-02] fix(in-session): stop 判 run 终态 + dupe-check 活跃判据改 tape 派生（真实审查聚类 A）

stop emit `workflow_cancelled` 前先 fail-loud 扫 tape 终态，已终态幂等短路（不追加第二终态事件污染 append-only tape）；新增 `orca/iface/in_session/_tape_probe.py`（reader：JSONDecodeError→raise TapeParseError / pydantic Event 校验失败→continue+warn / 双判据 `terminal_count`+`terminal_types_seen` 多类 raise 同类重复 warn 不阻塞）；`_find_active_run_for_wf` 活跃判据 marker≡活跃→tape 终态派生，候选坏 tape warn+skip 不阻塞无关 bootstrap；stop 控制流骨架 fd 释放统一外层 finally（防双释放 hazard），Tape/EventBus 仅 emit 分支内实例化。经 4 轮对抗 spec review 闭环 + coder 自我 review 0 BLOCKER，126 测试全绿。Commit: `08cb7b0`。详见 [release note](../releases/2026-08-02-audit-a.md)。

## [2026-08-02] refactor(workflow-docs): 产物去考古化 + 写作规范固化（kd/struct/quant/nas/scripts 全面清理）

把「去考古化」写作规范固化进 create-workflow skill（新建 `reference/writing-style.md` §0-§9：A 类考古禁 `[A-Z]+-[0-9]+` 编号 / analogue of / v嵌入 / 外部 plan·SPEC §；B 类真实文件导航允许 CONTRACTS §N / workflow §N / checklist item；§9 标准正则 + 宽口径 grep 兜底），SKILL.md 集成规范节 + success_criteria，memory 跨会话兜底。按规范清理全仓库产物层 A 类考古：kd 系列（含 _kd_scripts 注释）+ struct 系列（按新规范重做，去 plan §X / SPEC § ~140 处，功能字节级等价）+ quant + nas + model optimizer，保留 B 类导航。新增 prune-channel-sweep.yaml（用新规范生成的剪枝 workflow，端到端验证规范生效）。三轮 code-reviewer 独立 review（含 git diff 核查 struct 功能等价）+ 终审 review 所有实现（抓 4 项严重：prune torch API / struct {%raw%} / 盲区 / 规范三件套不自洽，全修复）。Commits: `7465231`（规范+kd/struct/scripts/prune，45 文件）+ `e546255`（quant+nas，19 文件）+ `4fcda82`（终审返工，12 文件）。详见 [release note](../releases/2026-08-02-workflow-docs-dearchaeology.md)。

## [2026-08-02] feat(validate): tars validate 加引用合规校验门（自引用 / output_schema 字段对齐 / scripts 存在 / input 三档标签）

把 4 项引用合规校验加进现有 `tars validate`（不新设 lint 子命令），成为 create-workflow skill 生成后的检验门，catch `agent-struct-exploration.yaml` `{%raw%}` 误删类自引用崩。复用 validator 现有 AST 基建（`_ENV` + `_iter_templates`）；`_iter_templates` 重构为 5 元组新增 `self_name` 区分预渲染 vs 评估期字段。同步修 pre-existing silent-warning bug：新增 `load_workflow_with_warnings`，CLI `validate` 现在能把非阻断 warning 显示到 stderr。9 个真实 workflow 全部 0 新 error / 0 新 warning（基线保留），测试 99 → 112 passed。两路 code-reviewer 审查所有 must-fix 已修（含死代码移除 + foreach body 两侧覆盖 + 聚合测试）。Commit: `5b139ac`。详见 [release note](../releases/2026-08-02-tars-validate-reference-checks.md)。

## [2026-07-31] feat(kd-nas): model8 实际 student 变体（BatchNorm/3层/ReLU）+ 精简 workflow description

新增基于原始 model8（SignalProcessingTransformer）的实际 student 变体到 demo KB（`examples/kd-nas-demo/knowledge_base/families/receiver/`），覆盖三条轻量化路径：① BatchNorm 替 LayerNorm（验证过时延达标）② 缩到 3 层 ③ GELU→ReLU。主变体 `00_model8_bn3relu`（三路径全开）排 KB glob 第一（digit 前缀字典序最小，pick_variant 实测确认）；组合变体 `01_bn3gelu`/`02_ln3relu`/`03_bn4relu` 隔离各路径。共享积木 `_model8_student_blocks.py`（`norm_type`/`act_type` 开关，OCP）。`demo_tiny_*` 保留不动。BatchNorm 维度适配验证（reshape 后 3D `[N,C,L]`，C=embed_dim，真跑 forward+backward+ONNX）。测试 73 passed（test_smoke 36 + test_model8_students 37）+ KD 无回归（158 passed）。另：`kd-nas.yaml` description 精简为只讲作用（"通过 KD 搜索轻量化模型结构达时延目标"），去节点流程细节。Commit: `<待补>`。

## [2026-07-31] fix(kd-nas): review #6 可视化 + #5 思路 pass 修复（latency baseline 透传 / accuracy_compare baseline 行 / 进度图 0 保留 / min 方向取负显示 / is_measured_row 单测）

修 review #6（可视化 conditional-pass）4 必修 + review #5（思路 pass）1 建议，范围严格限定列出 5 项：① `train_pool.py` 加 `--baseline_latency_ms` + 透传 viz_kd（latency bar 缺 baseline 参考线）+ `kd-train/agent.md` bash 块补 `{{ setup.output.baseline_latency_ms }}`；② `viz_kd._push_accuracy_compare` 把 baseline 作为 data 行（`met_accuracy="ref"`，前端画得出，对齐 latency_bar）；③ `_push_progress` 去掉固定 status 项的 0 过滤（注释承诺「一眼全貌」）；④ **min 方向 kind（db/nmse/mse/ber）取负显示**（`acc_disp=-acc`，`_acc_display` helper，display 变换对齐 tail_metrics）使「轴上越大越好」统一——防 bar 图 -20dB 高于 -22dB 视觉误导（goal 硬要求；Rule 7 选数据层消除歧义最强），未知 kind 三图齐跳（取负需已知方向，不 auto 猜）；⑤ 新增 `is_measured_row` 7 边界直接单测。两路 code-reviewer 自检无 BLOCKER：补 latency_bar baseline 端到端值级断言（闭合 #6-1 intent）+ progress 固定显示序 / 杂项 status 正路径 / variants_total 未知 / status:null 归一测试 + agent.md baseline flag 守门 + docstring 精确化（tail_metrics display 对齐但 kind 检测相反）。测试 +23 / 更新 1；WSL `tests/workflows`+`tests/compile`+`tests/schema` 634 passed + kd-nas contract 79 passed 无回归。Commit: `<待补>`。详见 [release note](../releases/2026-07-31-kd-nas-review-fixes.md)。

## [2026-07-31] feat(kd-nas): finalize——select 节点 + 指标方向显式化 + 训练监控 + 防假 + 修 no-fabrication 误报

KD-NAS 最后一公里：① 新增 ``kd-select`` folder-agent（零 LLM ``select_and_report.py``：读 ledger → 按 ``accuracy_baseline_kind`` 显式方向挑最优 student + 帕累托前沿 + ``final_report.md``），DAG 升 7 节点 ``… → train → select → $end``；② ``accuracy_baseline_kind`` 加回 inputs + ``kd_common.accuracy_direction`` 单一真相源（measure/viz/select 三处同源，含 db；防 -20dB 误判优于 -22dB 的方向反转）；③ viz_kd 加 progress/pareto/accuracy_compare 三图 + 方向感知轴标；④ ``train_pool.classify_final_sweep`` 0-SUCCESS→FAIL 防假；⑤ 修 ``test_no_fabrication[kd-nas]`` 误报（gpu_probe docstring + teacher_model ``_smoke()`` 抽出）。两路 code-reviewer 审查发现 critical C1（FAIL_latency 哨兵 ``acc=0``+真测 lat 污染帕累托前沿）已修（``kd_common.is_measured_row`` 按 status+accuracy_kind 判真测，select/viz 同步）+ 9 条测试加固。Commit: `<待补>`。详见 [release note](../releases/2026-07-31-kd-nas-finalize.md)。

## [2026-07-31] feat(kd-nas): v4 嵌入——teacher-gen + train-script-gen 串进 DAG（4→6 节点）

把 teacher-gen + train-script-gen 两 folder-agent 从独立阶段嵌入 kd-nas workflow DAG（``flatten → teacher_gen → train_script_gen → setup → gate → train``）；setup teacher 训练改调 ``train_pipeline.py --mode teacher``（固定 ``--out_ckpt``，删 setup_helpers find-teacher-ckpt）+ teacher_setup latency 透传自 teacher-gen（不再自测）；setup 删 step6 grep-user-train（loss 适配下沉 train-script-gen）；train_pool worker 改调 ``train_pipeline.py --mode distill``；``train_adapter_template.py`` 退役到 ``_deprecated/``；input ``teacher_train_command`` 改名 ``user_train_script``。teacher-gen/kd-train-script agent.md 最小接口调整（Jinja 输入 + JSON 输出，SKILL/scripts 未动——Rule 7 偏离说明见 release note）。两路 code-reviewer 审查（0 BLOCKER / 3 MAJOR + 5 MINOR / 2🔴 + 3🟡）全修；单测 230 passed + 脚本级 E2E（teacher 训 + distill + measure）跑通。Commit: `<待补>`。详见 [release note](../releases/2026-07-31-kd-nas-embed.md)。

## [2026-07-31] fix(nas): push_describe 实测 + review 两轮收口（不臆造 / fail-soft 闭环 / 矛盾暴露）

实测 + code-reviewer 审查 4d55c2b 后三处 honesty/fail-soft 收口：① 非 head Linear（MLP fc1 / transformer embed）原用 baseline 维度冒充「超网维度(后)」——SearchSpace 只标准化 head，改显 `—`（不臆造）；② `_build_rows` 原在 `main()` 的 try 之外，SearchSpace 字段异常会绕过 `_err` ERROR 兜底表——并入 try/except 补齐 fail-soft 闭环；③ head `super_in` 原静默取末级 stage 宽度、掩盖 baseline `in_feat` 与之冲突——`in_feat` 已解析且 ≠ 末级宽度时显 `?(baseline=X,last_stage=Y)` 暴露矛盾。两份副本同步；测试 4→10（+ 非 head 臆造 / 非常量 fail-loud / `__main__` kwargs 优先级 override / transformer `stage_emb_dims` 标签 + head 冲突 / 两副本 `filecmp` 同步性 / 无 `*_flat.py` 的 `_err` 兜底表，全绿）。Commits: `22e0762`（实测）+ `54cf33a`（review）。详见 [release note](../releases/2026-07-31-nas-push-describe-enrich.md#实测后修正2026-07-31)。

## [2026-07-31] feat(kd-nas): 新增 teacher-gen folder-agent（teacher 纯调参派生：深度×3 / 宽度×2）

新建 `workflows/agents/teacher-gen/`（folder-agent：`agent.md` 强执行指令 + `SKILL.md` 4-step 派生工作流 + `scripts/validate_teacher.py` teacher 专属硬校验 + `scripts/measure_latency.py` 字节对齐副本）。基于 flatten 产出的 baseline 契约**纯调参派生** teacher 结构文件——wrapper 通过 `importlib.util.spec_from_file_location` 按绝对路径委托 `baseline.build_model`（不拷贝架构代码），深度轴 ×3 / 宽度轴 ×2（KNOBS 名字语义识别：`block/layer/stage/depth/num_layers` → 深度；`channel/embed_dim/hidden/width/feature` → 宽度），DUMMY_INPUT 与 baseline 逐字一致（KD 硬约束，硬编码字面量非引用），`__main__` 逐字照 `model-flatten/SKILL.md` Step 3 模板（正确性 + latency via `measure_contract_latency`）。**双重硬校验门**：复用 model-flatten `validate_contract.py`（跨 agent 路径调用，KD 变体契约通用）+ 新 `validate_teacher.py`（DUMMY_INPUT 一致 / 深度×3 / 宽度×2 / 其余 KNOBS 不变 / 容量严格上升）；另加 `teacher-gen-verifier` 子 agent（轴识别 / wrapper 纯度 / latency wiring）。两路 code-reviewer 审查：1 BLOCKER（wrapper 委托失败路径无测试）+ 5 MAJOR + 6 MINOR 全修，测试 35→46 全绿。**未嵌入 workflow yaml**（独立阶段；未来嵌入 setup 改读 `teacher_gen.output.teacher_model_path`，不再硬编码 `_kd_scripts/teacher_model.py`）。详见 [release note](../releases/2026-07-31-kd-nas-teacher-gen.md)。

## [2026-07-31] feat(kd-nas): flatten `__main__` 升级为「正确性 + latency」统一契约，baseline latency 下沉到 flatten

把 baseline latency 测量从 `kd-setup` step2（调 `tune_latency.py`）上移到 flatten 产出的 `<base>_flat.py` 的 `__main__`：新增自包含 helper `workflows/agents/model-flatten/scripts/measure_latency.py`（导 ONNX + ort 实测 median+std；latency_provider 空 → ONNXRT-CPU fallback + WARN；绝不伪造）；flat 文件 `__main__` 经 `$ORCA_AGENT_RESOURCES` env 调 helper，契约顶层 standalone 不变（latency 逻辑全在 `__main__` 内，validate_contract import 不受影响）；flatten output 加 `baseline_latency_ms`（实测中位数）；kd-setup step2 删 tune_latency 调用、改读 `flatten.output.baseline_latency_ms`；flatten-verifier 加第三维校验（给了 latency_provider 却走 fallback → [BLOCKER]）。两路 code-reviewer 审查，3 个 [MAJOR]（模块级 importorskip 误伤 / accepts_device 分支零覆盖 / 「绝不伪造」intent 4 取 1）全修。`tests/workflows/` 全套 358 passed 无回归。Commit: `<待补>`。详见 [release note](../releases/2026-07-31-kd-nas-flatten-latency.md)。

## [2026-07-31] feat(nas): push_describe 对比表富化——真层名/解析维度/超网维度/组件候选

用户反馈 baseline→elastic 对比表「毫无信息量」（`fc2 Linear(?->?) ElasticLinear`）。根因在 `push_describe._build_rows`：层名写死 `conv{idx}/fc{idx}`、维度遇变量名显 `?`、丢 `stage_widths`、组件选择被埋在长串。重构为 5 列 `[层名, 替换前, 替换后, 超网维度(后), 组件/深度/核候选]`：层名取 AST 赋值目标真名（Sequential 内带下标）；替换前维度走符号表消解变量名（`__main__` kwargs > `__init__` 默认 > 模块常量）；超网维度(后)取 `stage_widths[i]` / head `super_in→super_out`；组件/深度/核候选取 `stage_depth_candidates` + `stage_layer_configs`。全程 AST 静态 + `SearchSpace` asdict（不实例化、不靠 LLM），fail-soft 不变。两份副本（pytorch-model-optimizer + elastic_optimizer）+ agent.md 同步；新增 `tests/workflows/test_push_describe.py`（4 纯函数测试，全绿）。Commit: `4d55c2b`。详见 [release note](../releases/2026-07-31-nas-push-describe-enrich.md)。

## [2026-07-31] feat(kd-nas): 新增 kd-train-script agent（生成统一 train_pipeline.py，teacher + distill 两模式）

把 nas-agent-pipeline 的 `supernet-train-script` 的生成式哲学（folder-agent + 自包含搬用户逻辑 + verifier 闭环）搬到 KD-NAS，产出 `workflows/agents/kd-train-script/`（`agent.md` + `SKILL.md` + `references/workflows/train_pipeline_script_generation.md` + 2 checklist + `references/templates/train_pipeline.py` 参考实现），一个脚本两模式（`--mode teacher` 纯 task_loss / `--mode distill` task_loss+KD），模型按路径 `importlib` 加载，单卡 `--device` CLI（零 DDP/torchrun/sandwich 残留，AST 扫描守门），用现有 `kd.compose`/`kd.wrapper`/`kd.ema` 库（不碰 `nas_agent.train.distillation`），用户 train.py 边界更窄（只 `compute_loss`+`build_dataloader`，optimizer/scheduler 缺省走 Adam + 显式 fallback 标注）。新增独立测试 `tests/workflows/test_kd_train_script.py`（18 个：folder-agent 结构契约 + 静态校验 + 两模式功能 smoke + fail-loud 路径，全绿；相邻隔离测试 105 全绿无回归）。**未 commit**（待主 session 与用户确认；范围隔离：未嵌入 workflow yaml / 未退役 train_adapter_template.py / 未改隔离清单文件）。Commit: `<待补>`。详见 [release note](../releases/2026-07-31-kd-train-script-agent.md)。

## [2026-07-31] feat(kd-nas): 新增独立 flatten agent（model-flatten）+ 输入瘦身

把 nas-agent-pipeline 的 pytorch-model-optimizer Step 1（展平能力）抽成独立 folder-agent（`workflows/agents/model-flatten/`：`agent.md` + `SKILL.md` + `scripts/validate_contract.py`），让用户只给任意 PyTorch 模型入口，flatten agent 自动展平成 KD 变体契约（`build_model` + `DUMMY_INPUT` + `KNOBS`），脚本做硬校验（fail loud），LLM 做展平 + KNOBS 识别 + flatten-verifier 子 agent 复核（judgment vs deterministic 分工，rule 5）。workflow DAG 改为 4 节点 `flatten → setup → gate → train`，entry 改为 `flatten`；setup 节点 baseline 来源从 `inputs.baseline_model_path` 改为 `flatten.output.baseline_contract_path`；同时砍 5 个 advanced inputs（`seed` / `kd_artifacts_dir` / `accuracy_baseline_kind` / `latency_tune_budget` / `kd_force_rerun`——下游 CLI 本就有默认值），对应 kd-{setup,gate,train}/agent.md bash 块清理。测试新增 `tests/workflows/test_model_flatten.py`（14 个：validate_contract PASS/FAIL 全路径 + agent.md/SKILL.md 结构契约 + DAG），`test_kd_redesign.py` DAG 测试改为 4 节点 + 3 个新测试守门（flatten output_schema / inputs 不回潮 / setup 消费 flatten output）。**未 commit**（待主 session 确认 + WSL 跑 pytest）。Commit: `<待补>`。详见 [release note](../releases/2026-07-31-kd-nas-flatten-agent.md)。

## [2026-07-29] fix(visibility): in-session run 可见性根治——marker-free 自注册 tape 所在目录

SPEC v3 Bug A：任意目录（含无 `workflows/`）下 in-session 起 workflow → web 列表/详情/子 agent 推送不可见。根因是 tape 落点（cwd/runs）、注册根（detect 祖先）、discovery 扫描（注册根/runs）三处独立计算 + M-16 门槛太死。修法（逐字对齐 §4.1 A-E）：`register_project` 加 `*, require_marker: bool = True`（仅门控 M-16，M-15/P2 始终守）；bootstrap 改注册 tape 物理位置（cwd）`require_marker=False`；`rebuild_registry` 旧 entry 信任（`not in old_paths`，D-rebuild=A，不擦除 marker-free 旧 entry）；抽 `RUNS_DIRNAME` 到 runtime 中立层单源。测试 AC1-AC5c 全覆盖（C1 布尔方向守门经 mutation 验证非空泛）；runtime/web/in_session/exec 注册相关 147 passed。Commit: `b83d81d`。详见 [release note](../releases/2026-07-29-run-visibility-marker-free.md)。

## [2026-07-25] feat(kb/receiver): SOTA 调研驱动加 4 个昇腾友好 student 变体

Web 调研三轴（无线接收机 SOTA / transformer 昇腾提速特性 / 高效 transformer 结构，引 arXiv 来源）后，给 `knowledge_base/families/receiver/` 加 4 个**昇腾友好 + 未被现有 11 变体覆盖**的新 student：`spt_inception`（InceptionNeXt arXiv:2303.16900，3 分路 k∈{3,5,7} 标准 Conv1d 并行，零 MATMUL/零 DW/零 attention → IMG2COL cube）、`spt_resnext`（ResNeXt CVPR2017，grouped conv groups=4 走 CANN GroupedConv，零 1×1 bottleneck 避 MATMUL）、`spt_se`（SENet Hu2018/2020，DilatedResBlock 主体 + per-channel 门控，与 channelformer 空间注意力正交）、`spt_dualpath`（DPRNN/DPCRN Interspeech2021，F+S 双轴卷积替代双轴 attention）。**依用户硬约束排除**：FFT/MLP-MIXER（硬件不行）、可变形/MSDA（昇腾 vector-core bound，arXiv:2505.14022）、Mamba/SSM（选择性扫描顺序累加，cube 利用率低）、Linformer（短序列 48/64 低秩投影反拖慢）、复值 conv（I/O 契约实值）、新 U-Net（已覆盖且需去 MATMUL）。receiver 变体 11→15，全契约合规 + ONNX 导出 onnxruntime 对齐 max_delta=0.00；`tars validate` 0 error。Commit: `ee44b4b`。

## [2026-07-25] fix(workflow): kd-nas E2E 真 bug + reviewer findings 全闭环

E2E 真跑 `tars run workflows/kd-nas.yaml --background`（demo inputs，9m40s 完成、4/4 变体 SUCCESS）暴露的真 bug + reviewer 6 项 finding 全修复：🔴BUG-2 `bg_runner.build_child_argv` 用错 binary（`shutil.which('orca')`→`tars`，in-session CLI 无 `run` 子命令 → `No such option: -i` 崩）；🔴BUG-1 kd-{setup,gate,train}/agent.md 被 deepseek-v4-flash 当 spec 审查不执行 → 三个 agent.md 重写（开头「⚠️ 唯一职责」段 + ❌ 红线 + JSON schema 前置 + bash 块标「执行：」），确定性逻辑仍全在脚本（rule 5）；🟡BUG-3 train_pool `VARIANTS_TOTAL:0`（ORCA_KB_DIR 在 in-session next 链里被重置）→ setup output_schema 加 `receiver_dir`，gate+train 都从 setup.output 取，train_pool 三级 fallback；🟡R4 setup step5/step6 teacher_ckpt+user_train 留给 LLM grep 违反 rule 5 → 新增 `setup_helpers.py`（find-teacher-ckpt + grep-user-train AST，`_walk_with_prune` 硬剪枝 venv/.git/llm_artifacts 让 Orca repo 33k 文件下扫描从 >30s 降到 <2s）；🟡R1 gpu_probe NPU `max_memory_allocated=0` 不再沉默估算 `total_free//4`（改 fail-soft 不估算）；🟡R2 train_pool viz_kd rc!=0 不再静默吞（stderr WARN 含原 stderr 尾部 300 字）；🟡R3 gate_all 空 KB 不再沉默 N_ACCEPTED:0（stderr WARN 含 receiver_dir）。测试 +24 个（R1/R2 mock 守门 / BUG-1 agent.md 结构不变量 / setup_helpers prune 等），全 139 passed。E2E 关键判据通过：agent 真执行 bash + emit 合法 JSON（不再写验证报告），step2 偶发 copy 错时自纠正切 heredoc。Commit: `902457d`。详见 [release note](../releases/2026-07-25-kd-nas-e2e-bug-fixes.md)。

## [2026-07-25] feat(demo): kd-nas-demo E2E 测试靶子（满足 kd-nas v2 setup→gate→train 契约）

新增 `examples/kd-nas-demo/`（12 文件）作为真实 kd-nas workflow 的 E2E 靶子：`baseline_model.py`(4层契约文件) + `train_teacher.py`(训 10 层 teacher) + `train.py`(用户 loss/dataloader，消除 kd-setup 的 user_train_import 非确定性) + `test_student.py`(真算 NMSE，末行 JSON 使 measure_student 稳定检 nmse kind) + `latency_provider.py`(onnxruntime 实测) + `knowledge_base/families/receiver/`(4 混合类型变体 + `_demo_blocks.py` 原创简化积木，非复制 `_model8_blocks.py`) + `test_smoke.py`(契约 smoke 20 测试) + `README.md`(inputs 全集 + orca run 示例)。铁律：数据随机但 latency/精度真实测量（无造假）。自验全过——契约 smoke 20 passed；setup/gate/train 三节点的确定性后端（`teacher_setup`/`tune_latency`/`train_adapter[OFD+EMA]`/`gpu_probe`/`measure_student`）逐个端到端跑通；`tars validate` 0 error。code-reviewer 🔴(漏验集成脚本，已补跑) + 🟡(KNOBS min=2 / pytest / README 自验对齐 / demo train.py 消歧) 全修。Commit: `02b927b`。详见 [release note](../releases/2026-07-25-kd-nas-demo.md)。

## [2026-07-25] feat(workflow): KD-NAS v2 重构为 setup→gate→train DAG（确定性 gate + 有界并发池）

把 `kd-nas` 从 4 节点 workflow 循环（`setup→selector→distill→recorder→…`，每变体一轮 LLM 编排）收敛到 3 节点线性 DAG `setup→gate→train→$end`：setup 扩展 step 8 GPU 预检（新 `gpu_probe.py`，per-variant footprint + free VRAM → 并发公式 + 多卡 round-robin device_plan，fail-soft 无 CUDA→concurrency=1+WARN）成为**并发数唯一权威**；gate（新 `gate_all.py`）一个节点内串行遍历全部变体（校验 + tune + dispatch），FAIL_latency 当场增量落账、ACCEPTED 写 `gate_manifest.json`，路由 `n_accepted==0→$end` 否则 `→train`；train（新 `train_pool.py`，前身 `train_variants_parallel.py` 重构为只做训练阶段）吃 manifest 做 Phase B VRAM 再校验（不够降级 WARN / 连 1 都放不下 fail loud）+ `ThreadPoolExecutor` round-robin 绑卡 + `as_completed` 逐行增量 append ledger（单 worker 崩不杀整批）+ 末尾 viz_kd。删 kd-selector/kd-distill/kd-recorder agent 目录（脚本全保留被复用）；`kd_common` 加 `append_ledger_row/run_subproc/parse_key` 共享 helper（DRY）。code-reviewer 🔴（yaml outputs 在 gate→$end 路径崩，已修 + 2 回归测试）+ 🟡（CONTRACTS §5 补 cfg_hash / gpu_probe teacher_cache 损坏政策对齐 exit 2 / worker exception handler 测试）全修。测试：`test_kd_redesign`（v2 gate_all/train_pool/gpu_probe 单测 + wf.outputs 渲染回归 + worker handler）+ `test_struct_kd_p7`（3 节点）全绿；`tests/workflows/` 258 passed。Commit: `e14f775`。详见 [release note](../releases/2026-07-25-kd-nas-gate-train-dag.md) + [计划](../plans/2026-07-25-kd-nas-parallel-agent-driven.md)。

## [2026-07-24] feat(workflow): KD-NAS 重构为 Receiver KB 驱动的确定性蒸馏 sweep

把 `kd-nas` 从「搜索」workflow（Phase1 sweep + Phase2 LLM 变异 + finalize + proxy_mse）改成**确定性蒸馏 sweep**：DAG `setup→selector→distill→recorder→…→$end`（砍 5 节点）；model8 `.py` 变体放 `knowledge_base/families/receiver/`，确定性遍历；teacher(10层 t1/t2) 写死 repo 仅作 KD 软标签源；精度基线 = 用户绝对值（`accuracy_baseline`）；时延超阈→`tune_latency` 最小缩量调参（刚跨 target 即停）；完整训练（非 proxy）+ 每-epoch `render_chart` 实时图；跨 run 复用（稳定 `kd_artifacts_dir` + 哈希校验 + ledger-driven 跳过，无引擎 `--reuse`）。新脚本 `pick_variant/tune_latency/distill_dispatch/kd_common` + `teacher_model`；改 `measure_student`(绝对基线)/`teacher_setup`(瘦身+hash)/`train_adapter_template`(路径加载+实时图)/`viz_kd`(新 schema)/`export_onnx`(`build_kwargs`)；`export_onnx.build_kwargs` 是所有 latency 调优的前提。spec-review 17 blocker + HIGH/SR/MED findings 全 fold（BLK-1/2/3/4/5/6/7/8/9/10/11/12/13/14/16/17 + HI-1..15 + SR1/2/3）。`latency_provider` 必填无默认 + dummy_input 用户指定（禁硬编码 shape）+ FAIL_latency 确定性门 + 单写者锁。测试：`test_struct_kd_p7` kd 部分重写 + 新 `test_kd_redesign`（BLK-8 最小缩量 / done 谓词边沿 / HI-11 field∈schema）；compile+workflows+e2e 195 全绿。Commit: `855531c`。详见 [release note](../releases/2026-07-24-kd-nas-distill-redesign.md) + [计划](../plans/2026-07-24-kd-nas-distill-redesign.md)。

## [2026-07-24] feat(cli): `tars close` 命令（关闭本地 orca web server，与 `tars open` 对称）

补齐 `tars open` 的对称命令：`POST /api/shutdown`（loopback-only，跨平台优雅，触发 uvicorn lifespan）为主，PID 兜底（POSIX SIGTERM→SIGKILL / Windows taskkill /F）为辅（仅 404 老版无端点 server）。B1 双 wire（`run_server` + `_serve_and_run_inprocess` 两处都暴露 uvicorn 句柄）；B2 `--all` 用指纹过滤不误杀别用户 server；B3 不清 registry 靠 stale 自愈；B4 PID 兜底返 False 后必 re-probe（并发 winner 语义）。Windows PID 兜底恒 force-kill（无 SIGTERM），kill 前 stderr warn tape 可能未 flush。31 新测（web 8 + cli 23，win32-only warn 1 skip）+ 相邻回归 143 passed / 1 pre-existing fail（`run_manager.py:37` 预存 web→cli 违反，独立 issue）。code-reviewer：Implementation **pass** / Test coverage **conditional-pass → 已转 pass**（4 🟡 全修：Windows 空 parse fail loud + 平台文案 + AC5 registry 守门 + AC1 tape 直接验证；3 🟢 修 1 跳 2）。Commit: `59d73dd`。详见 [release note](../releases/2026-07-24-tars-close-command.md)。

## [2026-07-24] test(in-session): 解析 CLI 输出前去 ANSI（修复 2 个 pre-existing 测试假报）

typer/Rich 即使在 CliRunner（非 tty）下也给 `--help` 与 BadParameter 错误输出上 ANSI 色，并把 flag token 拆碎（`--run-id` 跨 span 成 `--`+`run`+`-id`），导致 `test_skill_md_flags_subset_of_cli_help`（`_help_flags` regex 全 miss → SKILL.md 真实 flag 全被判「未声明」）和 `test_cli_gc_max_age_zero_rejected`（`--max-age 必须为正` 里 flag 被高亮拆碎、子串断言失配）两个 pre-existing 假报。均为测试断言未去 ANSI、与生产无关（gc 正确 exit 2；各命令 flag 都真实存在）。修法：加 `_strip_ansi` helper，`_help_flags` 与 gc 两个 CLI 错误断言去色后再匹配。`test_gc.py + test_skill_md_flags_guard.py`：33 passed（原 31 + 2 failed）。Commit: `df5380a`。详见 [release note](../releases/2026-07-24-strip-ansi-cli-test-parsing.md)。

## [2026-07-24] fix(in-session): bootstrap 注册项目（修复 TARS run 在 web 列表/详情不可见）

§13「单端口+多 Run 监控」把 web 发现（`discover_runs`）与详情懒挂载（`resolve_run_path`）改成全依赖注册表 `~/.orca/projects.json`，但 in-session `bootstrap`（TARS 入口）漏调 `register_project` → TARS 启动的 run 项目永不在注册表 → 列表空 + 详情 404 + 远程 `projects.json` 不生成（用户 `tars project rebuild` 后恢复坐实根因）。修复：`orca/iface/in_session/cli.py` 加 `_register_current_project()` helper（`detect_project_root` → `register_project`，broad `try/except` fail-open + warn，与 daemon spawn 同降级语义），在 `bootstrap` post-lock 段（lock 释放后、SPEC §3 O2 临界区外）调用。依赖 `iface/in_session → orca/runtime` 合法向下。2 新测（真端到端 bootstrap 断言注册 + 落盘 / fail-open 不阻断）。`tests/iface/in_session/ + tests/runtime/test_project.py` **494 passed / 2 pre-existing fail**（`test_cli_gc_max_age_zero_rejected`、`test_skill_md_flags_subset_of_cli_help`，stash 验证无关）。code-reviewer 0 🔴 / 1 🟡（broad catch 注释显式化，已采纳）/ 2 🟢（保持）。Commit: `12d5279`。详见 [release note](../releases/2026-07-24-in-session-bootstrap-register-project.md)。

## [2026-07-24] fix(workflow): 可视化审计修复（P1×5 + P2×7，0 🔴 / 2 🟡 / 5 🟢 全修）

两轮审计 12 项全部闭环：P1-1 超时候选 death-penalty 保留 latency 实测（`_infeasible_result` 纯函数）/ P1-2 C5-live 轴方向动态化（`_axis_direction`）/ P1-3 bit-curve 逐层位宽图（`_load_bit_trend_layer_bits` 多形态 fail-soft）/ P1-4 cumulative-best 寻优过程图（`_cumulative_best`）/ P1-5 QAT 收敛曲线改 live 推（每 scheme 一张图）/ P2-1 struct accuracy champion trace / P2-2 kd latency candidate trace / P2-3 struct x 轴 index→round / P2-4 删除孤儿 nas-viz 目录（grep 确认零引用）/ P2-5 quant 4 脚本共享 helper 下沉到 `_common.py`（字节等价，仅 log_prefix 参数化）/ P2-6 sensitivity 业务异常 fail loud exit 3 / P2-7 candidates_evaluated 改用 len(archive_records)（字段名不变）。43 新单测（AST 切片钉真源码非手抄）+ 既有 2 测试断言更新（3→4 图）。159 workflows passed / 654 跨目录 passed。Commit: `eb78c52`。详见 [release note](../releases/2026-07-24-workflow-viz-audit-fixes.md)。

## [2026-07-24] feat(web): 单端口 + 多 Run 监控「遗留清项」（SPEC §13 v4 carry-over）

清掉 SPEC §13 v4 遗留清单全部七项：AC14 contract test（EventType 双档自动派生 + 守门）、P0 持久派生缓存 `<runs_dir>/.orca-meta-cache.json`（cache 非 index）、`tars project rebuild`（pre-rebuild 快照 + 全失败回滚）+ `tars project list`（含 stale）、P3 Stale projects 折叠区（`GET /api/projects/stale` + 前端组件）、统一 `orca open` 列表语义（`--list` flag + 无活跃 run 回落列表）、`scripts_e2e_driver.py` 归位到 `scripts/`、2 个 pre-existing fail quick fix（SKILL.md 禁词 / cc_nudge.sh 反引号）。第 3 个 pre-existing（`test_web_does_not_import_cli`，`apply_kb_requirement` web→cli 反向依赖）属架构问题**不动**，登记 release note。前端 `out/` 已重建。code-reviewer 3 🟡 全闭环 + 3 🟢 采纳（持久 cache flock 跨进程那项不采纳：cache 正确性不依赖跨进程一致，加锁引入死锁面）。27 新测 + 463 passed / 1 pre-existing fail。Commit: `449f851`。详见 [release note](../releases/2026-07-24-single-port-multi-run-cleanup.md)。

## [2026-07-24] feat(web): 单端口 + 多 Run 监控（Phase A + B' + C，SPEC §13 v4）

身份解耦 `sha1(ORCA_HOME)[:12]` → 同用户所有项目共享指纹 → 单端口复用；端口登记上移 `~/.orca/.orca-web.json` 在 exclusive flock 临界区内 spawn+bind+ready+写回（B-6）。中立层 `orca/runtime/_project.py` 注册表（path 指针非 run 数据，R1）+ `start_run(*, project_path=None)` kwarg + `POST /api/run` body 必填 project_path（B-1）。`GET /api/runs?scope=all` 跨项目 discovery + `manager.ensure_attached(run_id)` 懒挂载（触发面 /meta /events /assets WS subscribe）+ `DELETE /api/runs/<id>` 四态响应（200/404/409 M-3）+ WS 控制帧基础设施（B-4 每 WS queue+writer task，解决并发 send RuntimeError）+ AuthMiddleware no-op stub（M-1）+ 前端 RunListPage 与 workflow-store 物理隔离（R3）。注册表原子写+.bak+损坏 fail loud（P1），并发 register flock 串行化；`_write_orca_home_registry_unlocked`/`_lookup_orca_home_port_unlocked` 内部变体避免嵌套 flock 死锁。45 新单测 + 既有 web/cli 套件全绿（1 pre-existing apply_kb_requirement 非 §13 引入）。**遗留**：前端 out/ 未构建（WSL/Win 环境限制）、AC14 contract test、P0 持久层缓存、`orca project rebuild`。Commit: `1788cea`。详见 [release note](../releases/2026-07-24-single-port-multi-run-monitoring.md)。

## [2026-07-23] feat(in-session): 错误管理 recoverable/irrecoverable 分级（SPEC 2026-07-23 v2）

in-session 可恢复错误（`output_schema_mismatch`）不再判死 run —— 引擎 emit `[node_failed, node_started]` 重 arm 当前节点、回 recoverable 信封让主 session 重派，连续 3 次同节点失败才升格 `workflow_failed`。`subagent_compliance` 改双阈值（≥3 warn / ≥10 hard）。`render_error` 保持全 irrecoverable。**v1 recoverable = `output_schema_mismatch`（重 arm）+ `subagent_compliance`（warn）**。立场反转 SPEC §2.5「所有 InSessionError 一律 workflow_failed 终态」→ 分级，对齐 executor/drive_loop 早就用的 `node_failed`（非终态）模式。新增 `RecoverableInSessionError` + `consecutive_fail_count(tape, node)` 局部扫描 helper（**不进 reducer fold**，保 `events/replay.py` 零改）+ `_recover_step_result`（升格 emit 顺序钉死 `nf→ns→workflow_failed`，E8）+ `merge_recoverable_envelope`（cli/daemon 共用，DRY）。控制流 R2：advance_step 自捕 recoverable **不外抛** → cli 走正常 result 路径拼信封 / 不 clear_marker / 不 exit(1)（recoverable 不经 cli 的 `except InSessionError`）。**边界（git diff 核实零改）**：events/replay.py / drive_loop / executor / schema/compile / marker schema（仍 3 字段）。code-reviewer 两轮闭环（impl + coverage 并行）：0 🔴 + 2 🟡（daemon 升格 reply parity + 升格测试）+ 2 🔴（AC4 跨 session 续跑 + AC5 中间态重放）+ 4 🟡（AC1/AC2/AC3/AC10 补强）+ 6 🟢 全修。189 passed。Commits: `3499618` (impl) → `e7ce35f` (tests) → `8776b21` (skill+spec 修订标记)。详见 [release note](../releases/2026-07-23-in-session-error-management.md)。

## [2026-07-23] feat(chart): in-session 鲁棒出图 + 失败告知 agent（SPEC 2026-07-23）

根治「agent-struct-exploration in-session 路径下可视化静默失败」：子代理 bash 不经 ClaudeExecutor，`ORCA_*` env 不自动注入 → `render_chart` 缺 env raise 被三层吞（脚本 except → bash `|| true` → workflow done 假成功、前端零图）。方案 = 确定性自加载 env（从 `--ledger`/`--champions` anchor 向上找 `orca_env.sh` 且内容含 `^export ORCA_CHART_SOCK=` 行，仅补 4 个身份键）+ 失败可见回流（stdout 加 `viz_env_status`/`charts.reason`，agent dumb copy 写 `output.viz_status`，`_main` 兜底保证 stdout 永远有合法 JSON）+ `--mode compare` 替代 finalize step6 inline `python3 -c` 块。新增 `orca/chart/_env.py`（stdlib only，light-touch）+ 改 `viz_struct.py`（自加载 + reason 分类 env_missing/socket_unreachable/ack_failed/data_insufficient/import_failed/generic + `_main` 兜底 + `--mode compare`）+ yaml curator/finalize output_schema 加必填 `viz_status`（严化 additionalProperties）+ agent.md Step 4 dumb copy。**铁律守恒**：`_render.py` 零改动（env 只从 env 读），`chart_daemon`/`cli.py` 零改动（daemon 已就绪），依赖方向不破。code-reviewer 两轮闭环（impl + coverage 并行）：0 🔴 + R1-R4 + 5 🟡 + 6 🟢 全修或登记。245 chart/workflows/in-session + 249 compile/e2e_redesign passed（2 skipped kd-nas 与本改动无关）；`tars validate` 0 error。Commit: `003acc3`。详见 [release note](../releases/2026-07-23-in-session-chart-robustness.md)。

## [2026-07-23] fix(in-session): _encode_cwd 匹配 CC 真实编码（非字母数字全归一为 -）

CC 实测（headless `claude -p` 在含 `_`/`.`/空格 的目录跑，看其生成的 `~/.claude/projects/` 目录名）把**所有非字母数字字符**逐个归一为 `-`，大小写/数字保留。旧 `_encode_cwd` 仅 `replace("/", "-")`，含特殊字符的 cwd 下 Orca 算的 sidechain root ≠ CC 实际写入目录 → daemon discover 不到子 agent jsonl → 子 agent 消息进不了 web、doctor H3 误报 root 不存在。修：`_family._encode_cwd` 改 `re.sub(r"[^a-zA-Z0-9]", "-", cwd)` + 空 cwd fail-loud 守门 + docstring 标注 Linux/macOS 实证（Windows 盘符/Unicode 待验）。测试同步：4 处手写 `replace("/", "-")` 切真实 `_encode_cwd`（sidechain_daemon 1 + in_session_v8 3；pytest tmp_path 含下划线，旧编码下 v8 **2 红 1 假绿**）；两处 `test_encode_cwd` 扩 `_`/`.`/空格 + 连续字符不 collapse；v8 假绿用例补 `root_exists` 断言钉死。code-reviewer 两轮闭环（编码规则/fail-loud/DRY/依赖铁律）；events + in_session 全量 **641 passed**（1 pre-existing failure 与本修复无关：`test_entry_skill_md_has_no_business_logic_keywords`——SKILL.md 文案含禁词 `compile` 误伤 grep 守门）。Commit: `cd21c8a`。

## [2026-07-22] doctor --probe-push 推送链路诊断（H1-H6 全 6 跳）

`orca doctor --probe-push`：一次跑完推送链路 6 跳（family_detect / cac_pid_walk / adapter_discovery / daemon_progress / bus_flow / ws_delivery），精确指出哪一跳断（不止「daemon 活着」）+ 输出 first_break + fix_hint 指针指向 runbook。新增唯一模块 `_push_probe.py`（叶子消费方，复用 _hostenv/sidechain_daemon/events.adapters 现有真相源，零新增接口）+ runbook `docs/troubleshooting/push-chain.md` + cli.py 加 3 typer Option（零副作用：无 --probe-push 时输出与基线一致）。H6 self-spawn 走 B2 决议 degradation（RunManager.start_run + monkey-patch Orchestrator.run + bus.emit 合成事件 + WS 3s 等收）。40 测试全绿（含 SPEC §5 三组守门 + fast e2e 冒烟 happy/负向 + H2 中间态自洽双向）。Commits: `275838b` (S1) → `af97ac1` (S2) → `a3f10a1` (S3) → `284b389` (S4)。详见 [release note](../releases/2026-07-22-push-chain-diagnostic.md)。
- **+ S5（`5b68629`）**：H6 passive `--ws-url` 模式生效——连用户真实在跑的 web，subscribe `--run-id`，8s 窗口被动等收真实事件（pass/fail/unknown 三态），回答「我这个 run 的事件到没到前端」。新增 `_hop_h6_ws_delivery_passive_async`；4 passive 测试，44 push_chain 全绿。

## [2026-07-22] bootstrap 启动即把 web 链接反馈给用户

补 `9677c1e`（bootstrap 默认自动开 web）的遗漏环：detached `orca open` 子进程的 URL echo 进了日志文件、用户终端看不到 → bootstrap 自身启动当下即算出 URL（单一真相源 `resolve_web_endpoint`，新增 helper `_resolve_web_url`，lazy import 避循环 + soft-fail），分两路显式吐给用户：①JSON `reply["web_url"]`（模型驱动路径拿得到）②stderr `Orca Web UI → <url>`（直接终端可见，不污染 stdout 契约）。**不进 `prompt`**：prompt 须与 `next` idempotent 重发逐字相等（`test_f1_resume_flow` 不变量）。已知 limitation：不探活端口归属（与 soft-fail 一致，端口探活是 `orca open` probe 的职责）。code-reviewer 一轮闭环（0 🔴，2 🟡 全修）；25 passed。详见 [release note](../releases/2026-07-22-bootstrap-surface-web-url.md)。

## [2026-07-22] KB 可移植 + struct-exploration 结构优先（direction 覆盖软闸）

解决「struct-exploration 只改超参不碰结构」两个根因。**Part 1（KB 可移植）**：`orca install` 部署 `knowledge_base/` → `~/.orca/knowledge_base/`；`config.resolve_kb_dir()` 确定性解析 KB 根（env>config>~/.orca>cwd，first-existing，显式来源权威不静默回退）；`ORCA_KB_DIR` 经 `build_env_overlay` 注入（env 作 transport，exec 不 import iface）；`apply_kb_requirement` 对 `requires:[knowledge_base]` 的 workflow 在 run 启动/in_session bootstrap 预检 KB，缺失 → ConfigurationError fail-loud（含 searched 路径+指引），不进 setup agent。**Part 2（结构优先软闸）**：新增确定性 `direction_coverage.py`（KB meta.json 枚举本族 direction 目录 D0-D21 + 读 ledger tried direction_id → untried/all_exhausted/near_target）；hypothesizer 每轮跑之 → 软闸优先选未试结构方向（all_exhausted/near_target 才允许 hyperparam）+ 补读 directions 切片 + 输出 direction_id；curator 记 direction_id 进 ledger；yaml 加 requires + setup 缓存 directions + KB 根改 $ORCA_KB_DIR。kd-nas defer。commit `6e0f167` + `0be8c6d`。详见 [release note](../releases/2026-07-22-kb-portability-struct-direction-gate.md)。

## [2026-07-22] Stage 3 统一 headless TARS-SKILL E2E（禁 CLI 驱动 workflow）

建 `tests/e2e_redesign/` 统一 headless TARS harness：经 TARS skill 路径（`orca <wf> --inputs` + `orca next --run-id`，复用 spike 基建，**禁** `orca run`/手搓 next）三层验证 8 workflow。①静态契约闸（64 parametrized：inputs/无残留引用/output_schema 链/device+seed/chart 标签/造假扫描 AST 感知/prohibition 正向存在）；②headless DAG walk（schema_faker 合成最小合规 JSON 喂 next，单节点 quant×4 到 done:true、多节点 bootstrap+首跳）；③哨兵路径 E2E（ptq-sweeper spawn→哨兵→resume→真实 output→done，断言 task_id 复用+哨兵不进 --output+MAX_ASK 兜底）。契约闸就地修了 **6 处 chart label 缺失**（P1/Stage4 遗漏：qat bar x_label + 5 处 table caption 漂移）+ **1 个 P9b 真 bug**（agent-struct setup prompt 自引用 `{{ setup.output.struct_scripts_dir }}` 渲染崩 → `{% raw %}` 转义——静态链 check 抓不到的**自引用**，动态 bootstrap 抓到）。code-reviewer impl+coverage 两轮闭环：0 🔴，5 🟡（DAGStallError 异常分离/schema_faker docstring/load_parsed DRY/conftest dir 过滤/测试名达意）+ 若干 🟢 全修；新增 4 个函数边界单测模块（schema_faker/contract_logic/conftest_cleanup/walk_dag_branches，39 测试）闭合确定性逻辑的 Rule 9 盲区。**120 passed, 2 skipped**（kd-nas 受用户既有活跃 run 阻塞 skip，按硬约束不 stop 用户 run）；回归 workflows+compile+spike 246 passed。全量真模型 run 不可行（无 GPU/数据集，预期），落在结构/契约/sentinel 可行边界。Commit: `89d30ee`。详见 [release note](../releases/2026-07-22-stage3-headless-tars-e2e.md)。

## [2026-07-22] P9a quant/NAS 按 input 三档原则精简 + 切 $ORCA_ARTIFACTS_DIR

quant-ptq-sweep 12→3 / sensitivity 11→3 / qat 15→3 / bit-curve 16→6（保留 accuracy_tolerance/avg_bit_budget/max_evals 三个 Tier A KPI/预算）；nas-agent-pipeline / nas-hp-search 6→5（下沉 output_dir）。Tier C 固化脚本默认（mode/bit_width(s)/recipes/scheme/cage/bake/method/ratio/low_bits/high_bits/candidate_format_space/bit_objective/granularity）；Tier B infer-once + P4b 哨兵（project_root 从 model_path 向上走对齐 NAS P6 + calib/train/eval loader/eval_fn ref）；output_dir → P8 `$ORCA_ARTIFACTS_DIR`（env 缺则 fallback 旧路径）——NAS entry infer-once + propagate（下游 nas-train-runner/nas-select 读 `{{ model_optimizer.output.output_dir }}`）。清理 P5 遗留 dead required 参数（4 脚本 --project_root/--calib_data_ref/--eval_data_ref/--train_data_ref/--eval_fn_ref，loader 逻辑在 adapter 不消费）。裁决策：qat lr/total_steps 走 smoke 兜底不走哨兵（QAT 短训恢复超参 ≠ 用户全量 epochs，哨兵 over-ask）但脚本 stderr WARN 非静默。SPEC §5 表 nas 目标 4→5（算术笔误订正）。code-reviewer 一轮闭环：0 🔴 + 2 🟡（qat WARN + 裁决策留痕）+ 4 🟢 全修或登记；新增 `test_p9a_input_contract.py`（6 wf input key 集合契约）。`tars validate` 0 error；Jinja StrictUndefined 16 节点 OK；250 passed。Commit: `64c5c11`。详见 [release note](../releases/2026-07-22-p9a-quant-nas-input-slim.md)。

## [2026-07-22] P9b struct/kd 按 input 三档原则精简 + 切 $ORCA_ARTIFACTS_DIR + setup 哨兵化 + create-workflow-skill 编码三档

struct 11→9 / kd 17→9 inputs（6 [ask] 主 + 3 [advanced] 固化）；Tier C 下沉（struct/kd_scripts_dir → setup.output.X infer-once+propagate；iterations 完全移除，引擎兜底 100 + `--max-iter` CLI 覆盖；teacher_layers=6/short_epochs=10/full_epochs=50/eval_dataset=""/proxy_dataset_spec="" 固化进 prompt）；setup 节点切 P8 `$ORCA_ARTIFACTS_DIR`（env 优先 + llm_artifacts 回退 + 尾斜杠补齐）；**P4b 遗留收口**——struct setup（yaml 内联）+ kd-setup Step 1 的 build_fn/dummy_input 缺失走 ask-user 哨兵（原「低置信猜测」/「报错」是潜在造假）；create-workflow-skill 编码三档（SKILL.md 新增专节 + reference §6 + 新 demo `tier-discipline.yaml`）。code-reviewer impl+coverage 两 review 闭环：0 🔴，4 🟡 + 7 🟢 全修或登记；新增 `test_no_jinja_ref_to_undeclared_input`（8 wf parametrize，填补 compile validator 只 warn 不 error 的契约守门空白）。`tars validate` 0 error；compile+workflows 196 passed。Commit: `8a1e5f0`。详见 [release note](../releases/2026-07-22-p9b-struct-kd-input-slim-and-skill.md)。登记后续：三档标签 lint / tier-discipline benchmark case / 生产 agent 哨兵 E2E（批 3）/ SPEC seed Tier A vs [advanced] 张力澄清 / SPEC device↔target_hardware 命名加注。

## [2026-07-22] Stage4 viz 执行：producer 侧 ~15 张图补 x_label/y_label/caption + 方向标注

应用 [viz 优化方案](../plans/2026-07-22-workflow-viz-optimization.md) 6 批 checklist：~15 张缺标签图（quant/NAS/kd/struct 推图脚本）全补人话轴标签 + caption + metric 方向（↓lower is better）；NAS 终态帕累托 scatter→pareto 恢复前沿连线。**dedup 键 label+title 冻结**：全范围零 label=/title= 值改动 → 无重复图风险；`higher_is_better` 设 required（错默认会反向 caption，违 Rule 12）。code-reviewer 零 🔴 + 2 🟡 全修；24 viz 测试无回归。Commits: `e05ad2d`/`81facb9`/`3b01a5f`/`4559f65`/`22b2392`/`250e4a7`/`23361af`。详见 [release note](../releases/2026-07-22-viz-labels-producer-side.md)。遗留：`nas-viz/scripts/` 死代码（零 yaml 引用）待 DRY 清理。

## [2026-07-22] P8 引擎注入 `$ORCA_ARTIFACTS_DIR` + `orca gc` 命令（Phase 4-A/4-C）

单一真相源：`artifacts_dir_for_run()` 落 `orca/chart/_paths.py`（绕开 exec/↛run/ 反向 import 契约），`build_env_overlay` + executor/script mirror + bootstrap `mkdir -p` + `orca_env.sh` 注入 `ORCA_ARTIFACTS_DIR=<abs>/runs/<run_id>/artifacts/`；workflow `source orca_env.sh` 后 `os.environ` 读（替换自建 `llm_artifacts/...`，P9 迁移）。`orca gc --max-age 14d [--keep N] [--dry-run]`：4 类候选（stale-run/orphan-dir/orphan-marker/orphan-lock）+ 安全（active run 不删 / 路径逃逸拒 / advisory lock / MANIFEST run 跳过保 P9 worktree 闭环）。67 新测试 + 664 回归 passed；spike NameError 已修。Commit: `b1eaf43`。详见 [release note](../releases/2026-07-22-p8-engine-artifacts-dir-and-gc.md) + [P9 接口约定](../releases/2026-07-22-p8-engine-artifacts-dir-and-gc.md#p9-接口约定)。

## [2026-07-22] P4b agent.md 接入 ask-user 哨兵（接通 ask-user 最后一段）

P4（TARS skill 哨兵检测）已激活，本任务把 6 个含 Tier B 必填项的 agent.md（ptq-sweeper/sensitivity-analyzer/qat-trainer/bit-curve-searcher/nas-search-pipeline/kd-setup）的「缺数据 fail loud」升级成「读代码无果→返回轻量哨兵 `{"_orca_ask_user","_sentinel":"orca_ask_user_v1"}`」。与 SPEC 轻量版（2 必填键，options/context 可选）+ spike `is_sentinel` strict 识别逐键对齐；保留 fail_loud fallback；DRY（每 agent.md 加「缺失必填输入时（严禁造假）」段，inline 回指）。`tars validate` 0 error；127 compile + 61 workflow + 39 spike 测试过。Commit: `530b580`。详见 [release note](../releases/2026-07-22-p4b-agent-md-ask-user-sentinel.md)。范围外：struct setup 哨兵化（yaml 内联 prompt，留 P9/后续）。

## [2026-07-22] P7 struct/kd workflow 精简（11→6 / 13→6）+ latency-first + 图表根因 + device

struct / kd 两 workflow 大刀阔斧精简：struct `family_detect+baseline_measure`→`setup`、`analyst+viz_round`→`curator`、删 `structure_gate`（tag 改 curator 内联 ast_diff deterministic 推导）、内联 `viz_finalize`→`finalize`；kd 同款再合 `teacher_setup+profile_gate+kd_train_script_gen`→`setup`、**`kd_trainer+measure_student`→`candidate_eval` 改 latency-first**（先默认权重导 ONNX 测 latency → 不达标 FAIL_latency 不训练 → 通过才短训测 proxy_mse；measure_student 加 latency-only 模式 db_gap=-1 sentinel）。图表根因修：viz_struct Pareto 过滤 accuracy=None（修 y=0 伪点）+ 删 Round Ledger / Exploration Tree（零信息）+ Candidate Ledger 短字段拆分；viz_kd round 模式 db_gap/met_acc 移出默认列 + finalize compare caption 标 champion deferred / teacher_accuracy_known=false 警告。device/latency_provider/ONNX：`_device.py`（resolve_device + ort_providers，inline 自 NAS）struct/kd 各一份；6 脚本加 `--device/--seed`；`export_onnx --no-external-data` 默认断言；`latency_provider` 升 [advanced] input；`seed` 加两 yaml；解开 export/measure_student/teacher_setup 原 `device="cpu"` 硬编码。P2 遗留收口：7 agent.md 拼接切 setup 专用字段 + CONTRACTS.md 6-节点 I/O 表同步 + kd-hypothesizer `rationale_summary`→`rationale` + kd-curator 加 phase==2 门 + champion_db_gap 短训恒 -1（不造假）+ kd-setup 不硬编码 teacher_accuracy_known。code-reviewer 一轮闭环：R1-R4 必修（造假/静默丢点/契约漂移/round 计算）+ M1-M7 中等（LLM 兜底/deterministic path/字段派生）+ L1-L7 轻微全修；新增 24 smoke test（latency-first 顺序 / Pareto None 过滤 / 6-节点结构 / output_dir 拼接守门 / teacher_accuracy_known 传播）。`tars validate` 0 error；319 测试无回归。**Surface-conflict（Rule 7）**：plan headline 写"11→7/13→7"，bullet 算下是 6——以 bullet 为准（更具体），plan 已订正。Commit: `66f74ea`。详见 [release note](../releases/2026-07-22-p7-struct-kd-restructure.md) + [计划](../plans/2026-07-21-workflow-redesign.md) §Phase 3。

## [2026-07-22] P6 NAS 系 workflow 重设计（补 KPI inputs + sink project_root + heavy 7→5 对齐 slim）

两 NAS workflow（`nas-agent-pipeline` heavy / `nas-hp-search` slim）补 4 个 [ask] KPI input（target_hardware / latency_constraint / max_rounds / seed）；`project_root` 下沉给 setup 节点 infer-once（从 model_path 向上走）+ output_schema 向后传（抄 agent-struct family_detect 范式）；heavy 7→5 节点对齐 slim 确定性护栏（删 viz_describe / LLM evaluator / viz_finalize，viz 内联进 setup、选架构复用 slim `nas-select`）；`train_runner` 加 output_schema `search_records minimum:1` 防假执行；`latency_estimator.py` 构造函数 device 无默认（forcing function）；dataset 缺失 fail loud（不造假，暂不哨兵）。code-reviewer 一轮提 1 🔴（output_schema vs SKILL Step4 早退契约断裂）+ 3 🟡（slim 同形 / heavy doc 漂移 / best-effort vs strict JSON 边界）全闭环：两 yaml `model_type` 加 `enum: [..., unsupported]` + 条件路由短路 $end + 两 setup agent.md 加早退 JSON 分支 + docs/workflows/{nas-agent-pipeline,nas-hp-search,README}.md 同步。`tars validate` 0 error；6 个 NAS agent.md Jinja2 StrictUndefined 渲染全 OK。Commit: `42e4a06`。详见 [release note](../releases/2026-07-22-nas-workflow-redesign.md) + [计划](../plans/2026-07-21-workflow-redesign.md) §Phase 2。

## [2026-07-22] P5：quant 四 workflow 正确性修复（删造假 + device + bit-curve bake 改动生效）

修 ptq-sweep / sensitivity / qat / bit-curve 四 workflow 的契约级硬伤：①**删造假**——agent.md 模板原指示「torch.randn 兜底 / 复用 calib 当 eval / 复用 train 当 eval」全删，改 Tier B 契约（读用户代码找 loader dotted-path → 找不到 fail loud，stderr 明确 + exit 2）；脚本 grep 0 个 `torch.randn`。②**device**——新增 `_quant_scripts/_device.py` 共享模块（`resolve_device` / `is_npu_available` / `set_seed` / `move_batch_to_device` / `wrap_forward_with_device` / `add_device_seed_args` / `resolve_device_and_seed`，inline 自 nas-agent 不引跨包依赖）；4 yaml 加 `target_hardware`(Tier A [ask]) + `seed`(默认 0) input；4 脚本加 `--device`/`--seed`，`fp_model.to(device)` + `wrap_forward_with_device`（batch 搬 device 自动做）；NPU 经 `torch.npu.is_available()` 有路径。③**bit-curve bake 改动生效**——`_bake_selected` reload 落盘 state_dict + 重 eval（strict=True 键失配 fail loud），返 `(path, reeval_metric)`；`_check_bake_metric_consistency` 超 tol（相对 1e-4）exit 3；持久化顺序保证 exit(3) 时 `best_mixed_model.pt` 与 `bit_curve_summary.json` 一致；bake 失败不阻断曲线产出（N7）。④`output_dir` 默认加 `/<wf-name>/` 子目录防撞；⑤qat 示例数字修正（recovery=after−before，mse 口径负=改善）；⑥sensitivity 补 `--env_file` 对齐 PTQ env 兜底；⑦qat recovery bar / bit-curve pareto 用 P1 轴标签（`x_label`/`y_label`/`caption`），pareto 标题用 `metric_kind` 替代写死的 "Accuracy"。eval_fn_ref 空 → WARN「用 teacher-student mse，精度仅自洽性参考」（SDK 合法默认，非造假）；eval_loader 缺 → fail loud（复用 calib/train 是禁掉的造假口径，code-reviewer Rule 7 surface）。code-reviewer 两轮闭环（impl + coverage 并行）：5 🔴 + 6 🟡 + 8 🟢 全处理（既有 7 类 helper 复制 + 死 required 参数登记给 P9 input slim 同期）。37 新测试 + 110 既有测试无回归；`tars validate` 0 error。详见 [release note](../releases/2026-07-22-quant-workflow-correctness-fix.md)。

## [2026-07-22] P4 TARS skill 哨兵处理全量（子 agent 缺必填项时问用户而非造假）

TARS skill（`orca/skills/tars/SKILL.md`）驱动循环第 2 步加哨兵分支 + 新增「### 哨兵处理」段：派子 agent → 子 agent 缺必填项返回哨兵 JSON → TARS 在调 `orca next` **之前** strict 识别（括号配平抽最外层 JSON + `_sentinel:"orca_ask_user_v1"` 魔键，非 substring）→ 捕获 task_id（CC `agentId`/opencode `ses_xxx`）→ 问用户（CC `AskUserQuestion`/opencode 聊天问）→ 恢复**同一**子 agent（CC `SendMessage`/opencode `Task(task_id=)`）→ MAX_ASK=3 兜底 fail loud → 真实产出才喂 `orca next`（**哨兵绝不进 `orca next`**，引擎零改动）。是 P3 spike `drive_node` 的 skill 指令投影（6 步控制流逐字翻译）。只改 SKILL.md，零引擎/workflow/agent.md 改动。spike 38 测试基线保持绿。code-reviewer 两轮闭环（design + spike-equivalence 并行，无 🔴，2 🟡 + 6 🟢 全修）。CC 主路径先 ship，opencode 标 experimental。Commit: `774aa46`。详见 [release note](../releases/2026-07-22-tars-skill-ask-user-sentinel.md).

## [2026-07-21] P3:0-b ask-user 哨兵闭环 spike（de-risk TARS 全量改造前的 ask-user 路径）

建独立最小 harness（`tests/spike_ask_user/`，2 节点 workflow + driver + 38 测试 + 2 真 claude integration）证明：子 agent 缺 Tier B 必填项 → 严格 JSON 哨兵（`_sentinel:"orca_ask_user_v1"`）→ driver strict 识别（非 substring）→ 捕获 task_id → SendMessage/Task/`claude --resume` **恢复同一子 agent**（task_id 复用断言）→ 拿真实 output → 喂 `orca next`（哨兵不进引擎，零引擎改动）；重入 3 次 fail loud（MAX_ASK）；造假检测（`torch.randn` 等）兜底。产出可复用 `SubagentBackend` ABC + `MockSubagentBackend`（scenario 全局时序）+ `ClaudeCliBackend`（`claude -p --session-id` + `--resume`，等价 CC SendMessage 的 headless 形态）+ `tars_loop.drive_node/drive_workflow`（SPEC §2 Python 投影）。code-reviewer 两轮闭环（impl+coverage 合并：1 🔴 哨兵泄漏断言空操作修复 + 5 SHOULD-FIX DRY/diagnose ABC 抽取/dead code 清理 + 8 新测试覆盖 OrcaBusyError/orca_cli 5 raises/nested JSON/node B sentinel 等）。**Spike pass**，可开 P4（TARS skill 全量改造）。详见 [release note](../releases/2026-07-21-spike-ask-user-sentinel.md).

## [2026-07-21] chart 加 x_label/y_label/caption 轴标签与图下说明能力（P1 workflow 重设计 Phase 0-a）

解「图表看不懂」根因 C：`render_chart` 签名加 `x_label/y_label/caption` 三参数（默认空串，仅在非空时塞 payload，与 `pareto_direction` 同款契约），单一真相源 = ChartPayload（backend `_render.py`/`_validate.py` + frontend `types.ts` 两端同源）。前端 `chartTheme.ts` 加 4 个 label helper（DRY，5 widget 共用）+ 新 `ChartCaption.tsx` 共享小组件，8 widget 全部接入（Line/Bar/Area/Scatter/Pareto 加 XAxis/YAxis label；Heatmap 加 caption + 矩阵下轴标题；Radar/Table 加 caption）。TUI `chart_canvas.py` plotext `xlabel`/`ylabel` + 空数据/非空数据两路径都保留 caption；heatmap 降级把 axis 拼进 hint 保语义。viz_struct `_push_champion_trace` 落地作证（候选序号 / 时延 / ★=达标）。**向后兼容**：旧 tape 无新字段 → 默认空串 → 三端回退旧行为；color（b820ef1）+ heatmap chart_type（ec3d598）零回归。code-reviewer 两轮闭环（一审删 Python 类 shadow 重复测试 + 修空数据 caption 丢 + 修 plotext reload cleanup 生效顺序；二审补 TUI 空数据 / heatmap 降级 / frontend 双轴缺省三个覆盖 🔴）。174 chart 相关测试 + 51 frontend chart 测试全绿；新增 27 测试。Commit: `a7de596`. 详见 [release note](../releases/2026-07-21-chart-axis-labels.md).

## [2026-07-21] workflow 产物路径拼接漏斜杠 BUG 修复（Phase 4-B / P2）

struct family_detect / kd teacher_setup 的 output_schema 新增显式带尾斜杠字段（snapshots_dir / worktree_root / viz_dir / ledger_path / champions_path + kd-only ckpts_dir / profile_report_path），setup prompt 强制 `OUTPUT_DIR=$(python3 -c "...os.path.abspath + '/'")` 一次计算；下游 struct-engineer / kd-engineer / kd-teacher-setup agent.md + yaml inline prompts（structure_gate / viz_* / profile_gate / kd_trainer）改读字段而非 `{{ output_dir }}<suffix>` 字符串拼根——从源头杜绝 `<run>snapshots/`、`<run>.worktrees/` 兄弟孤儿目录。code-reviewer 抓到 kb_cache/ 局部回归（m1）已修。范围外 7 个下游 agent.md（struct-evaluator / curator / analyst + kd-curator / analyst / hypothesizer / train-script）的同款拼接按计划留给 Phase 3 P7；CONTRACTS.md 节点 I/O 表 stale 同期处理。Commit: `e41974f`。详见 [release note](../releases/2026-07-21-workflow-path-concat-fix.md) + [计划](../plans/2026-07-21-workflow-redesign.md) §4-B。

## [2026-07-21] `orca open` 跨项目端口占用修复 + `bootstrap` 默认自动开 web

**A（`7d9b7eb`）**：7428 被别项目 orca 占用时不再静默挂错 tape。根因：`_open_run` 把相对 tape 路径跨进程 POST + 「7428 有 orca 就无脑复用」。修：`_identity.py`（新，`runs_dir_fingerprint=sha1(resolve)[:12]`，stdlib-only）+ health 加 `runs_dir_fp`（指纹非明文防 0.0.0.0 泄漏）+ `web_registry.py`（新，per-project 端口登记，探测权威 registry 仅 hint）+ `_open_run` 重写（绝对路径化 + 项目感知复用：本项目→registry→起新 server）+ SPEC §5a 同步。**B（`9677c1e`）**：`bootstrap` 默认开 web——post-lock 块 detach spawn `orca open`（与 chart/sidechain 守护同款 detach + soft-fail），stdout JSON 契约零污染；`--no-open-web`/`ORCA_BOOTSTRAP_OPEN_WEB=0` 关；schema-only 不触发。spec-reviewer 两轮 conditional-pass（6 blocker+5 HIGH 全闭环，B5 flock 降级为已知限制）+ code-reviewer 需修后合（3 🟡 测试缺口全补）。987 passed；2 既有失败（bg_integration/install nudge）git stash 证伪为基线既有。详见 [release note](../releases/2026-07-21-orca-open-cross-project-and-bootstrap-auto-open.md) + [计划](../plans/2026-07-21-orca-open-cross-project.md).

## [2026-07-21] Workflow 可视化全量优化（sensitivity bar/table + KD 0 图 bug + 横向优化）

7 个改动点（每点独立 agent 实现 + 逐 diff 验收）：前端 ChartPayload 加 `color` 字段（per-row 着色，hue 优先；BarChartWidget + ScatterChartWidget，`b820ef1`+`e1272e8`）；sensitivity bar 去 hue 改 color、table 改全层（`235ba98`）；**KD 0 图 bug 修复**——viz_round 复用 viz_struct 但 schema 不匹配致每行被剔→0 图，新建 viz_kd.py（4 图）+ 改 yaml 两节点（`f516223`）；struct 新增逐候选表（`0910c87`）；bit-curve 假 pareto 改真 pareto + 全候选 scatter（`70bb4ff`）；ptq-sweep 删无意义 hue + table 补失败行（`d154d1d`）；qat 补训练 loss 曲线（`f361171`）。KD 用真实账本 mock 捕获证实 5 图正确（修前 0 图）。详见 [release note](../releases/2026-07-21-workflow-viz-overhaul.md).

## [2026-07-20] sidechain family 由 env 身份决定（修 dotdir 误判回归）

`129fff8`（cac 优先）的回归修复：真 CC + `~/.cac` 存在（`orca install` 装 hook/skill 副作用）→ dotdir 探测误判 cac → daemon tail 空 `.cac` → 子 agent 消息进不了 web。根因：family 决策用 dotdir 存在性而非 env/进程身份。新增 `orca/iface/in_session/_hostenv.py`（stdlib-only）收敛 env 探测（提取 cli.py/sidechain_cmds.py 的 `_cac_session_id_from_pid`/`_host_session_from_env`/`_detect_backend_from_env` 副本 + 新增 `detect_family_from_env`：`CLAUDE_CODE_SESSION_ID`→cc / `CODEAGENT`+PID 回溯→cac）。三个 caller（`_spawn_sidechain_daemon` / doctor `_check_sidechain_backend` / `sidechain_cmds._print_effective`）统一 `detect_family_from_env() or config`，优先级 **env > config > dotdir 探测（兜底）**；events 层 `_family.py` 保留 probe 兜底不改。**builtins.next**：函数搬到 `_hostenv`（无 `def next` 遮蔽）后用普通 `next`（`__globals__` 绑定定义模块，CC/CAC 均安全）。code-reviewer 发现 3 处测试回归（test_sidechain_cmds / host_session_binding / sidechain_daemon 的 monkeypatch 路径 + config 断言）+ 2 stale docstring，全修。验证：doctor（真 CC+.cac）family=cc/resolved=.claude/available=True（修前 cac/fail）；daemon spawn family=cc（修前 None）；tape 34 个 agent_ 事件（修前 0）→ web 子 agent 可见。149 passed。Commit: `2f9be37`. 详见 [release note](../releases/2026-07-20-sidechain-family-env-identity.md).

## [2026-07-20] CAC session id PID 回溯替代 env 注入

撤回 `config.py` 的 `_normalize_cac_session_env()`（将 `CODEAGENG3_SESSION_ID` 注入 `CLAUDE_CODE_SESSION_ID`，仅在 Python 内存有效，子进程继承不到）。改用 **PID 链回溯**：`_cac_session_id_from_pid()` 沿 PID 链找 `codeagentcli` 父进程 → 读 `~/.cac/sessions/<pid>.json` 取 `sessionId`。`_host_session_from_env()` 加第三优先级（PID 回溯）、`_detect_backend_from_env()` 加 CAC 检测（`CODEAGENT=1` + session 可用 → `"cc"`）。同步更新 `cc_nudge.sh` / `sidechain_cmds.py` 两处副本。删除 `tests/iface/cli/test_config.py`（旧 `_normalize_cac_session_env` 测试），新增 CAC PID 回溯单元测试 ×4。

## [2026-07-20] sidechain cac 优先 + `orca sidechain family` 命令 + import 性能修复

CC sidechain resolver 探测改 **cac 优先**（`orca/events/adapters/_family.py::resolve_cc_sidechain_root`：`.cac` 存在即走 cac，含两存；原两存歧义默认 .claude）+ 新 `orca sidechain family` sub-Typer（set/show/unset，`--scope project|user`，照搬 `executor_cmds.set`）。配套：doctor fam_eff/hint 同步；`config.sidechain_family` helper（cli + sidechain_cmds 共享，DRY）；`load_merged_config` 合并 sidechain（修 project 级 `sidechain.family` 不生效既有 bug）。**import 性能回归修复**：`orca/iface/cli/__init__.py` eager import Textual TUI 壳 → 新命令 import config 拖慢 cli import（3.7s→5.9s）→ daemon pidfile 迟写 → 5 个 daemon e2e fail；改 PEP 562 `__getattr__` lazy + config profiles lazy 后 config import `4.4s→0.08s`，daemon e2e 全恢复。code-reviewer 核心检查全 pass（依赖单向/lazy/merge 边界/star import），修 2 minor（死 import / unset 空 dict 残留）。177 passed（含 5 daemon e2e）+ 86 非 daemon。Commit: `129fff8`. 详见 [release note](../releases/2026-07-20-sidechain-cac-priority.md).

## [2026-07-20] workflows 文档学术化重构（7 篇 + README）

按统一学术模板重写 `docs/workflows/` ×7（4 量化 + 3 NAS）+ README 索引：每篇**实现概览前置**（架构流程图 + 输入输出 + 激活）→ 定义 → 背景（含相关工作与引用）→ 方法（含公式推导）→ 实验 → 局限 → **附录库接口手册**（`ts_quant` / `nas_agent` 用户自调用法）。核心方法形式化并照库源码还原：PTQ 零空间 Q2N（Hessian 谱分解 + 能量骤降划零空间 + 子空间混合 + 行级闭式标量缩放 + 再量化回退）、W1 四种敏感度分析（mse / layer_stats 分布压力打分 / binary / mix）、W3 m0_pareto 三段（sensitivity probe → layer_policy 剪枝 → 主搜索）+ Pareto 支配关系、W4 CAGE 后校正（$W\!\leftarrow\!W-\eta\lambda_t(W-Q(W))$，不动点 $W^*\!=\!Q(W^*)$）、NAS 弹性超网 + NSGA-II 整数编码三算子 + 高权衡 Pareto 选择、struct-exploration 四不变量 + champion ratchet 单调下探。去除原版口语比喻，改学术风。Commits: `e94c45a`(PTQ) `66a9257`(W1) `3401212`(W3) `001ca77`(W4) `bd0da01`(hp-search) `78ca485`(agent-pipeline) `0c56995`(struct-exploration) `02e2225`(README).

## [2026-07-20] quant-bit-curve（W3）+ quant-qat（W4）—— 量化路线图收尾 + insession/7 workflow 文档

量化 pipeline W3/W4，类比 W1/W2（单 agent + folder-agent + `run_*.py` 确定性脚本 + adapter + render_chart + stdout JSON），全 mxint 基。**W3 `quant-bit-curve`**：与 W2 互补——精度约束下对比位宽/格式（INT8/W4A8/INT4/MX4/MX8），`search_mix_precision(strategy=m0_pareto, mode=explore)` 找 Pareto 前沿 + 格式分布可视化 + bake 最佳混合精度模型（`final.layer_configs`→`qconfig_dict`→`quantize_model`）。**W4 `quant-qat`**：对比 rtn/duquantpp 两训练态 fake-quant 方案，`prepare_trainable_fakequant_model` + `prepare_trainable_qat`(CAGE) + teacher-student label-free QAT，per-step 收敛 + 前/后恢复可视化 + bake 最佳 q_model。探针实证 W3（report/frontier/final 字段 + 格式→QConfig 表，因 candidate_format_space 只吃 QConfig 不吃字符串）+ W4（trainable API + duquantpp 两约束：显式 target_patterns + block_size 对齐）。验证：tars validate 0 error；ViT-Tiny 脚本级 smoke 双过（W3 cand_0002[INT8×26+INT4×24] bit 5.35 bake 21MB / W4 rtn+duquantpp 双方案 bake best）；in-session `orca <wf>` 返 schema。**文档**：`docs/in-session-usage.md`（安装+使用）+ `docs/workflows/` ×7（3 NAS + 4 量化，每篇激活→原理→结果+截图占位）+ README 索引。量化路线图完结。Commits: `e6646cf` + `da609ac`. 详见 [release note](../releases/2026-07-20-quant-w3-w4.md) + [计划](../plans/2026-07-20-quant-w3-w4.md)。

## [2026-07-19] quant-ptq-sweep workflow（W2 粗粒度 PTQ 扫描）

量化 pipeline 第二级。单 agent 节点 + folder-agent + `run_ptq_sweep.py`（833 行确定性脚本），双 mode：lightweight=4 累积路径 ablation（S/Q/A/R 派，~11 unique 候选，line 累积曲线）；full=位宽×预变换×求解×后处理全枚举（按 SDK §9.4 拒绝表过滤 rtn+q2n → 45 候选，heatmap 矩阵）。默认 `build_teacher_student_eval_fn` mse 评估 + bake 最佳 state_dict。修正 W1 `w4a16` 预设语义错位（`a_elem_format=fp16` 在 method=int 下不生效 → 改 `a_quant_enabled=False`）。code-reviewer 一轮（impl+coverage 合并）：6 🟡 全修（bake 顺序 / ts_quant 顶层 import / bake 白名单 / forward_fn 校验 / recipes DRY / w4a16 语义）+ 5 🟢 修；0 测试按任务范围（3 文件）+ plan §验证 deferred 阶段 5。Commit: `d356979`. 详见 [release note](../releases/2026-07-19-quant-ptq-sweep-w2.md).

## [2026-07-19] chart 加第 8 种 chart_type `heatmap`（行×列矩阵 cell 着色）

跨栈加 heatmap（量化实验对比矩阵：行=recipe，列=bitwidth，cell=accuracy）。**后端**（`_limits`/`_validate`/`_downsample`/`_render`）：加 `"heatmap"` 到共享 allowlist（两端同源）+ heatmap 必填 `x`/`y`/`value` fail loud + table 同款 top-N 降采样 + `render_chart` 加 `value` 参数。**前端**：`HeatmapChartWidget`（CSS Grid + 浅钢蓝→PALETTE[0] 钢蓝线性色阶，无新依赖）+ `ChartWidget` switch + `types.ts` 加 `value?: string`。**CLI**：修 CRITICAL DRY 违规（原 `chart_canvas.py` 复制 allowlist 漏更 heatmap → 改 import `_limits`）+ heatmap 终端 DataTable 降级。code-reviewer 两轮（C1/M1/M2/m1-m6 全闭环）：null/空串 cell 不 coerce 0 / 单值矩阵不除零 / 大数组 reduce 防 spread 栈溢出 / 色阶方向钉死 / 三端同源 contract test。78 后端 + 39 前端测试全过。Commit: `ec3d598`. 详见 [release note](../releases/2026-07-19-chart-heatmap-type.md)。

## [2026-07-19] 量化能力集成启动：W1 敏感层分析 + nas/create-workflow 配套修复

把 PatchTST_Optimal（ts_quant）量化能力集成成 Orca workflow 的第一块。**W1 `quant-sensitivity`**（`ca6bb60`）：单 agent + `run_sensitivity.py`（`analyze_low_precision_sensitive_layers` + `render_chart`），method 四选一、low_bits 默认 w4a4-mx 可配、按模型原始顺序可视化；ViT-Tiny 端到端实测通过（50 Linear 层 / 5 敏感层 / bar+table 推 web tape / done:completed）。实测修复 5 处：executor opencode→claude（当前环境 cc available）/ optional input 须 `[default]`/`[advanced]` 标签才能省略 / `module_types` 支持（CNN 需加 Conv）/ `ranked_layers` 真实字段名 `name` / `tars run --background` 的 `-i` 透传 bug → 改用 in-session `orca <wf> --inputs '{json}'`。配套：nas 4 agent 补 `.venv` activate fallback（`ce2158c`）；create-workflow 加 H8（description 须与 `orca list` 现有 wf 可区分，tars 选 wf 语义依据，`5e1f8f9`）；create-workflow validate 命令 orca→tars（in-session shell 无 validate 子命令，`7ee6276`）。ts_quant 已 editable 装入 conda orca env。路线图 W2（PTQ）/ W3（位宽曲线）/ W4（QAT）见 CURRENT。

## [2026-07-19] in-session 加固与性能（SPEC v4.1 整体交付：P3 + P1 + P5）

SPEC [`2026-07-19-in-session-hardening-and-perf.md`](../specs/2026-07-19-in-session-hardening-and-perf.md) v4.1 驱动的 in-session 路径加固与性能优化。**架构铁律（用户）**：orca 管所有状态/决策/compliance，主 session 只调度（派子代理/传 output），不过度设计、不跨层耦合。经 3 轮 spec-reviewer + 用户原则简化（弃 host_session 豁免 / on_emit_success 回调 / 三态枚举 / prompt_file / compliance_warning 让主 session 反应）。

**已交付 3 包**（各自 code-reviewer 两轮 0 🔴 + 测试全绿，详见下三条 + 各 release note）：
- **P3 O1a**（性能）：`advance_step` 两次 tape 遍历合一次，`next` 性能税减半（`256a843`）
- **P1**（8 项小合集）：S7 tape helper / S9 daemon liveness / S2 SKILL flag CI 守门 / D3 sidechain 探针 / O2 bootstrap 锁缩小 / O3 status 透 compliance / O4 busy retry_after_ms / F3 inputs 校验 + `_errors.py`（9 commits）
- **P5 F1**（resume，最高价值）：session 断了续跑半完成 run —— status resumable + SKILL 续跑段 + 占位 spec；零 marker 改动、复用 `advance_step` idempotent-replay（`705009a`）

**暂停**（用户决策，不阻塞 workflow）：P2 D4+D5（marker 损坏/孤儿，低）/ P4 D1+D2（失败兜底，中）/ P6 S1（contract-test，低）。累计 ~900+ 测试 0 回归。

## [2026-07-19] in-session 加固与性能 P5（F1 TARS resume v4.1 简化版）

SPEC [`2026-07-19-in-session-hardening-and-perf.md`](../specs/2026-07-19-in-session-hardening-and-perf.md) v4.1 §4 F1 落地。**架构铁律（用户）**：resume 是 run 级别的事（用 run_id 管，**与 host_session 无关**），复用 `advance_step` 现成 idempotent-replay（branch 4：`orca next --run-id X` 无 output 重发 prompt）+ SKILL 教续跑流程，**零 marker 字段改动、零 host_session、零 prompt_file**。改动：`cli.py status` 无参加 `resumable: True`（marker 在即 resumable，纯派生标志）+ 文本输出 + 尾行续跑提示；`SKILL.md` 加「续跑」段（status → next 无 output 重发 → 子代理 → next output 推进）；新建 `docs/specs/agent-interrupt-design-draft.md` 占位（in-session resume = F1 落地；engine-level interrupt = TBD）；修 `CURRENT.md` 断链。SPEC §7 F1 AC + §1 铁律 AC + v2→v3 changelog 闭环 stale（原写 host_session v3 语义，与 v4.1 矛盾）。守门双修：SKILL.md 用 `\bresume\b` word-boundary 守 tars 后端命令（允 `resumable` JSON 字段，禁孤立 `resume`）+ 去 `replay_state` 内部名。code-reviewer impl+coverage 两轮 0 🔴（🟡 全修：F1 测加 tape-not-added 否定断言 + 拆 `no_output_count` 为 `==1`/`==0` 精确断言 + 去掉 `--tape` 走默认路径解析真验生产形态）；196 in_session 测试全过。Commit: `705009a`。详见 [release note](../releases/2026-07-19-in-session-p5-f1-resume.md)。

## [2026-07-19] in-session 加固与性能 P1（8 项小合集：S2/S7/S9 + O2/O3/O4 + D3/F3）

SPEC [`2026-07-19-in-session-hardening-and-perf.md`](../specs/2026-07-19-in-session-hardening-and-perf.md) v4.1 §6 P1 行 8 项一次做（cli.py 串行组，单一 coder 按序）：**S7** 抽 `tape.read_last_complete_lines` helper DRY 三处 binary-mode tape 读（chart/sidechain 守护增量扫）+ 8 单元测；**S9** 抽 `_daemon_liveness.{socket,pidfile}_daemon_alive` helper DRY chart/sidechain liveness 探针（pidfile+cmdline run_id 校验防 pid 复用）+ 10 单元测；**S2** SKILL.md code fence flag ↔ CLI `--help` CI 守门（regex 不引 markdown lib）+ 5 测含负面守门；**D3** doctor 加 `sidechain_daemon` 存活探针（hard=False，覆盖死亡不覆盖持续 iterate 失败 §8#4）；**O3** `status --run-id` 加 `no_output_count`（raw 透出，主 session 不反应 compliance）；**O4** busy 信封加 `retry_after_ms:500` × 3 处（`_echo_busy_reply` helper DRY；主 session 不重派子代理/不重发 prompt）；**F3** bootstrap `--inputs` 校验（手写 TYPE_MAP 不引 jsonschema；新 `orca/run/_errors.py` 登记 `INPUTS_VALIDATION_ERROR` 铁律 5.1；bool/int 双向反陷阱）；**O2** bootstrap 锁临界区缩到 dupe check + gen run_id + advance+emit + write_marker，spawn daemons 移锁外（dupe-check 不变量仍成立）。code-reviewer impl+coverage 两轮 0 🔴 blocker（3 🟡 impl + 5 🔴 test 全修，commit `d3893b9`）；862 测试全过。架构铁律（orca 管所有状态/决策/compliance，主 session 仅调度）逐条核通过。Commits: `9100481`(S7)/`047629f`(S9)/`4bb81c5`(S2)/`1ed2c90`(D3)/`bc620e3`(O3)/`a3e28bd`(O4)/`e5d3c5b`(F3)/`b4e4b67`(O2)/`d3893b9`(review 闭环)。详见 [release note](../releases/2026-07-19-in-session-p1-hardening.md)。

## [2026-07-19] O1a —— `advance_step` 内合并两次 tape 全遍历为一次（in-session 性能 SPEC v3.1 §3 O1a，包 P3）

`advance_step` 此前两次全 tape 遍历（`replay_state(tape)` + `Orchestrator._inputs_from_tape(tape)`），合并为单次 `_replay_state_and_inputs(tape) -> (RunState, dict)`（落 `events/replay.py`，与 reducer 同文件，单次遍历既 fold state 又抽首条 ws.data.inputs）。`advance_step` 单次调用 tape 迭代 2→1；`_inputs_from_tape` 改薄封装保留对外 API（`from_tape`/`_bare_instance` 调用方零回归）；`replay_state` 对外 API 不变。pure refactor（state+inputs 逐字相等，决策三分支/emit 序列零改）。SPEC §1 铁律 + §7 O1a AC 逐条达成；code-reviewer impl+coverage 两轮 0 🔴/0 🟡（5 🟢 全修：砍 `since_seq` 防 footgun / snapshot 改固定值去自证 / 加 first-ws-bad 测试 / wrapper parity 参数化 / AST grep 守门 AC3）；654 测试全过（events+run+iface/in_session，+13 新测试）。Commit: `256a843`。详见 [release note](../releases/2026-07-19-o1a-tape-traversal-fold.md)。

## [2026-07-19] Web 界面视觉优化（P0–P4：token 收口 / lucide / 左栏增强 / TopBar+WS+暗色 / 三栏统一）

纯前端（后端零改、testid 与功能接口保留）。5 阶段：**P0** token 收口（179→26 hits 全白名单 NODE_STATUS_HEX/PALETTE/LEVEL_TEXT_COLOR/DiffView，status-style.ts DRY 出口）/ **P1** lucide 统一图标库（全量替换 emoji，保留 ▎ 流式光标，test oracle 迁移）/ **P2** AgentsRail 增量增强（元信息单行 + running ▎ + 色条加粗）/ **P3** TopBar（runId 复制 + status badge）+ WS 连接指示（ws-connection-store，SPEC §1.1 transport-only exception）+ 暗色三态开关（SPEC §7 双触发 `.dark/.light`）+ amendment 文档 / **P4** 三栏 surface 统一（orca-bg-app 治割裂，去双线 border）。spec-reviewer conditional-pass（3 决策 D1/D2/D3 全收敛）。318 test PASS（1 pre-existing flaky DAG lazy）。Commits: `644cc4f`(P0)/`a8c6a3e`(P1)/`a577367`(P2)/`13d0e1f`(P3)/`617d991`(P4)。详见 [release note](../releases/2026-07-19-web-visual-refinement.md)。

## [2026-07-18] 节点记忆（Node Memory）—— AgentNode 跨 run 记忆（写确定性 / 读注入 agent 判断）

in-session workflow 此前无跨 run 记忆（新 run_id 看不到旧 run 产出）。**否决确定性指纹缓存**（agent 非纯函数，收益薄改动大），改为把必然性与智能判断解耦：**写记忆 = 引擎确定性**（节点完成必然覆盖写 `<cwd>/.orca/memory/<wf.name>/<node.name>.md`，存上一轮 output 原文，不靠子 agent 自觉）+ **读+跳过 = agent**（prompt 注入「上一轮记忆+复用协议」，agent 自判复用/重跑，走正常推进路径，**引擎零 skip 分支**）。`AgentNode.memory: bool`（opt-in，仅 AgentNode）；新 `orca/run/memory.py`（write/read/inject helper，零 events/tape 依赖）；`_step_io.apply_step_result` emit_batch 后置写（cli/daemon 单一真相源）；`step._deliver` 注入；CLI `--no-memory`；best-effort 写失败不阻断（MD 是派生缓存，tape 才是真相）。覆盖式写 = 天然单份 + 过期清除。spec-reviewer conditional-pass（5 P0 + 3 决策全收敛，修正 2 处事实错误）；code-reviewer 2 🔴+6 🟡 全修；22 新测试 + 515 回归全 PASS；test-agent 真机 5 场景全过（首跑写/二跑注入/--no-memory 字节级不动/跨 cwd 隔离/写失败不阻断）。不碰 EventType/reducer/tape/advance_step 决策/Status 语义/render_prompt。Commit: `29c70b3`。详见 [release note](../releases/2026-07-18-node-memory.md)。

## [2026-07-18] Web 前端呈现层完善（P1-P5：log 降噪 / 子 agent 维度 / 左栏重做 / cac-nga / 美化）

B2 把子 agent 过程事件推 tape 后前端暴露 6 痛点（log 暴涨 / 对话异常长 / 执行完才显示 / 左栏割裂 / ITERATION 难观测 / cac-nga 不适用），根因同源于「子 agent 维度缺失 + 无事件分级」。5 阶段：**P1** LogStream 分级 classifier（e3b8ad 4779→19 行，过程事件归 ConversationView）/ **P2** 会话按 (node,session_id) 分段 + store in-order 增量 fold + nodesIndex（buildEntries 4226→~208）/ **P3** 左栏统一底色+根治 GAP(w-56→w-full)+NODE_STATUS_HEX 色条+Setup/Loop/Finalize 分组+R{iter}+子 agent 折叠 / **P4** cac/nga 家族路径解析（_family.py 零 iface import，env>config>probe>default）+ doctor sidechain_backend check / **P5** 图表可读(axis-tick slate-700)+统一 cursor 消 hover 黄+去 cost+9 token 明暗双套（reviewer 抓 R1 CSS hsl→rgb bug）。参考 microsoft/conductor 双 classifier 分流。spec-reviewer conditional-pass 全闭环（7 P0+5 P1+4 P2）；test-agent 真机（e3b8ad + react-dom/server）全 PASS 无 P0。事故：P2 stash 险丢并行 P4（已恢复零损失+memory 固化 git 禁令）。Commits: `0a4683d`(P1)/`b77422f`(P4)/`3a0f66e`(P2)/`7cc232e`(P3)/`f0cf695`(P5)/`2d416eb`(构建产物)。详见 [release note](../releases/2026-07-18-web-presentation-refinement.md)。

## [2026-07-17] B2 test-agent 真机 E2E 收尾：3 P0 bug 修复 + 5 回归测试

test-agent 真机 E2E（4435 真 CC `agent-*.jsonl` + 573 真 opencode `event` 表行 → 真 daemon subprocess → 真 tape → 真 `tars serve` HTTP → 真 react-dom 渲染）暴露原代码（`ed5cbeb`）3 个**单测盲区 P0**（79 单测全 PASS 但真机死）：① opencode DB 路径错（代码找 `session.db`，真机 v1.18 写 `opencode.db` → discover 静默返空 → ingest 0 事件）② opencode `source_id=opc:{seq}` 跨 child 撞车（event PK=`(aggregate_id,seq)`、seq per-session 非 global，多 child 44% 撞 → dedup 静默丢）③ text-mode `seek(字节)/read(字符)` 混算在多字节 UTF-8 tape 崩（offset 漂移到 continuation byte → `UnicodeDecodeError` 非 OSError 未兜住；波及共享 `chart_daemon`，B2 引入中文 agent_* 必崩）。修复：`opencode.db` 优先 + `source_id=opc:{child}:{seq}` + 三处 binary-mode（byte seek + `rfind(b"\n")` + decode）。补 5 回归测试。fix 后 64（B2+回归）+ 7（chart）+ 20（daemon）全 PASS；grep 守门 0 hit；test-agent V1-V10 全链路真机 PASS（实时 ≤1.0s / 幂等 / 无串台）。Commit: `99efcde`。

## [2026-07-17] B2 子 agent 过程推送 web（双 adapter：CC jsonl + opencode sqlite）

in-session 路径 detach 起 sidechain 守护，主动 tail CC sidechain jsonl / 查询 opencode sqlite event 表 → 经统一 IR `RawAgentEvent`（payload 1:1 = EventType.data，R1）→ `SidechainIngestor`（1:1 透传 R2 + source_id 查重 R3 + U1 读 tape 派生 node §6）→ `bus.emit` → `_FlockSafeTape`（复用 chart_daemon 七组件，零 DRY）→ follow_task → WS → 前端（**零改**，复用 B1 entries.ts agent_* 渲染）。SPEC-B **v4**（spec-reviewer conditional-pass，5 BLOCKER 全闭 R1-R7 + 4 决策 U1-U4）；接口同一性 grep 守门 0 hit；防御性 deviation 登记（CC source_id 扩 block_idx；opencode source_id 用 seq 而非 part.id，因单 part 双状态必撞）；code-reviewer 0 🔴 + 5 🟡 全修；79 新测试 + 352 events/in_session 回归全 PASS；e2e subprocess 测试覆盖实时 ≤2s / SIGKILL→respawn 幂等 / 终态自退。Commit: `ed5cbeb`。详见 [release note](../releases/2026-07-17-subagent-output-b2.md)。

## [2026-07-17] orca list 瘦身 + inputs_schema 移到启动命令

砍 `orca list` 的 `inputs_schema`（选 wf 阶段 84% 字节噪音；`agent-struct-exploration` 单 wf 21 input 字段占该 wf 输出 90%）→ 只返 `{name, description}`；schema 改由启动命令 `orca <wf>` 不带 `--inputs` 按需带出（带则真启动），**零新命令**（命令数 7 / 保留字 / CI 禁 describe 全不变）。改动：`cli.py` `list_workflows` 砍字段 + `bootstrap` 加 `inputs is None` 纯只读分流（不建 run/tape/marker）+ `catalog._inputs_to_schema_list` 公开化为 `inputs_schema_list`；SKILL 三步重组（list 选 → `<wf>` 看 schema → `<wf> --inputs` 启动）；SPEC §2.1/§2.3/§4.2/决策5/§8/§11 同步；测试 list 断言重写（按名定位，**顺手解 `~/.orca/workflows` 隔离缺陷**）+ 新增 schema 返回测试 + ~15 处 bootstrap 补 `--inputs "{}"`（3 个 `_bootstrap` helper 一处覆盖）。list 字节 4010→636（降 84%）；268 + 185 测试全过；`tars validate` 3 wf 过；code-reviewer 0 🔴（🟡 SPEC stale + 🟢 优化全修）。Commit: `ec3d598`。详见 [release note](../releases/2026-07-17-orca-list-slim-schema-via-start-cmd.md)。

## [2026-07-17] B1 前端渲染 node_completed output（子 agent 输出推送 web）

解 in-session web **不显示节点 output** 痛点：output 已在 tape `node_completed.data.output`，但前端 `entries.ts` 把 `node_completed` 归 `node-divider`（`NodeDivider` 不读 output）。**B1 纯前端零后端**：`entries.ts` 移 `node_completed` 出 `NODE_DIVIDER_TYPES` + 新增 `node-output` kind；`NodeOutputBlock.tsx`（新增）按 `typeof output` 分支（string→Markdown / object→`safeJson` JSON / null→dim）；`ConversationView` case + `estimateRowHeight:160`；删虚构 elapsed。spec-reviewer conditional-pass（修 dict BLOCKER + elapsed MAJOR）；4 commits；test-agent 真机 PASS（生产 build + 真 `tars serve` + attach 真 tape + `react-dom/server` 渲染 13 节点含 9 dict **零 `[object Object]`** + 17 单测）。Commits: `75116a0`…`8ebe45d`。详见 [release note](../releases/2026-07-17-subagent-output-b1.md)。**B2（过程推送）暂缓待用户决策**（spec-reviewer fail，5 设计洞 + U1-U4 + SoT 灰色）。

## [2026-07-17] host_session 绑定防串台（tape-only）

修 nudge「串台」：run-id ↔ 宿主 session 绑定，nudge 只提醒本 session 的活跃 run。host_session 只存 tape `workflow_started.data`（同 yaml_path tape-only 先例，**marker.py 零改动**，无 desync）；env 优先级 `ORCA_HOST_SESSION_ID` > `CLAUDE_CODE_SESSION_ID` > None；nudge 读 tape 首行过滤 + per-session 限流；emit 真链 lifecycle←step←cli；opencode `shell.env` hook 注入 + fail-open 安全网（防 nudge 静默死）。spec-reviewer 对抗评审 13 挑战全闭环（tape-only 是用户铁律直接推论）；25 单测 + test-agent 真机 E2E 全 PASS（多 session 不串台 + Stop-hook env 实证 + opencode 端到端）。Commits: `70c2ac8`…`3dae964`（8 commits）。详见 [release note](../releases/2026-07-17-host-session-binding.md)。

## [2026-07-16] nas-hp-search runner/select 反伪造 + output_schema 强制

修「假执行」bug（tape 铁证：runner(3s)/select(19s)/train_script_gen(1s) 没跑脚本、只复述上游散文；search.jsonl 640 条是诊断时手动跑的）。根因 = prompt 诱骗（顶部上游散文的「已完成」语域诱骗 deepseek 顺着复述）+ 无强制（fake 还静默标 completed）。① `nas-train-runner/agent.md` 重写（执行置顶、删上游散文灌入改用 `{{ inputs.output_dir }}`、反伪造、末尾 python 从真 search.jsonl 计数输出自校验 JSON）；② `nas-select/agent.md` 同样去诱骗+反伪造；③ `nas-hp-search.yaml` runner 加 `output_schema`（`search_records≥1`，in-session `step.py:_parse_output` 确定性强制：散文/0 记录 → `output_schema_mismatch` → `node_failed`，不真跑过不了）。共享 agent 契约变更：须显式传 output_dir。验证脚手架（FAST/MOCK）剔除不进生产。E2E 两次通过（opencode+flash+脚手架绕 deepseek 慢）：runner JSON 过 output_schema、select 真选 top-3 + final_report。Commit: `<SHA 见 git log>`。详见 [release note](../releases/2026-07-16-nas-hp-search-enforce-and-tars-skill-cleanup.md)。

## [2026-07-16] tars install skill 改名清理 + CLAUDE.md「TARS 是 SKILL」注记

CC 装出的 skill 名是陈旧 `orca`（tars 改名前装的残留），与命名约定（skill=`tars`）不符；且 install 不清旧 skill 目录名。`install_cmds.py:_install_skill` 加改名迁移清理（install 自动清陈旧 `skills/orca/`、`skills/teams/`，同 `command/orca` 清理 pattern，fail-soft）+ 修陈旧 docstring；`CLAUDE.md` 加「TARS 是 SKILL 不是 CLI」注记（skill 编排、驱动 `orca` CLI、不存在 `tars <wf>`）。重装 CC → 正名 `tars`，`orca doctor` `skill_install: PASS(cc,opencode)`；改名清理造陈旧目录实测命中。Commit: `<SHA 见 git log>`。详见 [release note](../releases/2026-07-16-nas-hp-search-enforce-and-tars-skill-cleanup.md)。

## [2026-07-16] nas-hp-search：轻量 NAS 超参搜索流水线（slim 5 节点）

新增 `workflows/nas-hp-search.yaml`（线性 `model_optimizer→train_script_gen→search_pipeline_gen→runner→select`）——重 7 节点 pipeline 的轻量版：① 新 slim folder-agent `elastic_optimizer`（只读 model + Elastic 速查 + 最小 supernet 模板，不展平/不读 optimize_rules，上下文从数十文件降到 3 文件）；② 新脚本化 `nas-select`（subprocess `nas-select-architecture` + 模板填空 `final_report.md` + 推 C5/C6，零 LLM，替代 22min evaluator）；③ 复用 `supernet-train-script` checklist 加 `[MAJOR] 28`（train_supernet.py 内联 `_push_chart()`，accumulate+全序列推、label/title 对齐 tail_metrics C3a/C3b 保 refresh-idempotent，无独立 viz 节点）；④ 复用 `nas-search-pipeline`/`nas-train-runner` 不改。节点名 `model_optimizer`（agent 指向 `elastic_optimizer`）对齐复用 agent body 的 `{{ model_optimizer.output }}` 硬契约（prompt+agent 互斥）。附带 `.gitignore` 修：`references/`→`/references/`（锚定根目录，避免误伤 folder-agent 的 skill 资源）。`tars validate` 0 error、5 agent 全 resolve；template 自测 diff=0；select_and_report 端到端 EXIT=0 SELECTED=3；code-reviewer impl+coverage 两轮 🔴 全修。Commit: `a5dd2cc`。详见 [release note](../releases/2026-07-16-nas-hp-search-slim.md)。

## [2026-07-16] in-session chart 守护 respawn —— `next` 路径补被杀后拉起

补 [in-session chart 接入](../releases/2026-07-16-in-session-chart.md) 的缺口：chart 守护**只在 bootstrap spawn 一次**，run 中途被杀（如 `pkill opencode` 误伤 detached 守护）后 `orca next` 不 respawn → 后续节点 `render_chart` 连不上 socket、chart 全丢（实测一次 run 0 chart）。本补丁：① `_chart_daemon_alive` 确定性 socket connect 探测（不靠进程名 grep）；② `_ensure_chart_daemon` 在 `next` 的 tape flock 临界区内 probe + 复用 `_spawn_chart_daemon` respawn；③ `_wait_for_sock` 从 `exists()` 加强为 connect 探（修 respawn 路径上 stale socket 假阳性）；④ 调用点守卫与 env 写对齐（`result.node is not None`，终态/no-marker 不 respawn）；⑤ spawn 失败降级 warn 不崩 next。+7 测试（含 SIGKILL→respawn→chart 落 tape 的 intent 级 e2e + 两个负向守卫测试）；158 in-session 测试 0 新回归（1 既有 list 测试隔离缺陷）；code-reviewer impl+coverage 两轮 0 🔴（🟡 全修：守卫/docstring/spawn 降级/负向测试）。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-16-in-session-chart-respawn.md)。

## [2026-07-16] in-session 路径接入 `orca.chart.render_chart`（per-run chart 守护 + run 级 env 文件 + 指针 source 行）

补 in-session skill 驱动路径的 chart 缺口：web/tars-run 路径下 ClaudeExecutor spawn 时一次性注入 `ORCA_*` env + 起 per-run ingestor（同进程）；in-session 路径下节点子代理由宿主 session（opencode/CC）派发不经 executor → env 无 `ORCA_*` 也无人起 ingestor → `render_chart` raise。本任务三件套补缺：① bootstrap detach 起 `_FlockSafeTape` 守护（跨进程 flock + 增量 disk max-seq 刷新，复用 `chart_ingestor` 协议零改动）；② `runs/<run_id>/orca_env.sh` per-node env 文件（5 var：4 chart + `ORCA_AGENT_RESOURCES`，folder-agent 资源定位缺口同补）；③ 节点 prompt 指针加 `source <env>` 行。守护 `_watch_terminal` 监听终态事件自退 + 6h TTL 兜底；partial-line race 防护（`last_size` 仅推进到最后 `\n`）。24 新测试（19 守护单测 + 5 集成：chart 落 tape / 并行不串台 / folder-agent + `$ORCA_AGENT_RESOURCES`）；710 in-session+chart+events+exec 测试 0 新回归；code-reviewer 两轮 0 🔴（R1 1 🔴 partial-line race + 5 🟡 全修；R2 0 🔴 0 🟡）。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-16-in-session-chart.md)。

## [2026-07-16] 后端命令 teams → tars 改名（品牌收口：skill=tars / 后端=tars / in-session=orca）

后端/运维命令 `teams`（install/run/serve/ps/validate/mcp/executor/list/logs/wait/resume）→ `tars`，与上一步 TARS skill rebrand 对齐——三套命名收口。改 `pyproject [project.scripts]` 入口 + `DEFAULT_BACKEND_CMD` 默认 + `validator` 保留字（`teams`→`tars`，防 wf 名撞命令）+ `commands.py` help/docstring + `teams_app` deprecated 别名保留（向后兼容）+ 用户面消息（orca epilog/doctor/skill 弃用警告）+ shipped 产物（cc_nudge.sh / SKILL.md / templates）+ `examples/mxint_analysis.yaml` 注释 + 测试 + SPEC live 段。`orca` in-session 命令不动；`ORCA_BACKEND_CMD` env 名不变（只改默认值）。重装后 `tars` 上 PATH、`teams` 退场（验：`which tars`✓ / `tars --help` 显示 tars / `orca --help` 指 tars）。768 单测 0 回归（+2 净增：pyproject 入口锁 + teams_app 别名锁）；code-reviewer 两轮 0 🔴（R1 🟡 examples 注释漏改 / R2 🟡 测试名实不符 + 🟢 别名锁，全修）。真机 `tars install/--help/list` 待 test-agent 验。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-16-teams-to-tars-rename.md)。

## [2026-07-15] TARS 品牌 rebrand —— skill 改名 orca→tars + TARS 描述（CLI 仍 orca）

用户面 = TARS：skill 名 `orca`→`tars`（`/tars`、description TARS 语气——触发「用 TARS 帮我 X / 用 TARS 做 Y」→ `orca list` 语义匹配 description → 命中唯一启动 / 多个则问（≤2 问）→ 抽 inputs → 派子代理 → `orca next` 循环到 done）。CLI/命令仍 `orca`（TARS 用 orca 引擎；orca.ts/cc_nudge 不动）。抽 `ENTRY_SKILL_NAME = "tars"` 常量单一真相源（`skill_cmds.py`，doctor `_scan_skill_install` + install re-export + 三处测试全经它，防目录名与 check 漂移）；SKILL.md body 命令引用全保 `orca`（仅 frontmatter name + 标题 + `<purpose>` 身份是 TARS）。SPEC §4.1/§8 措辞同步。176 单测 0 回归（+1 frontmatter name gate）；code-reviewer 两轮 0 🔴（test 轮 2 🟡 已修：install 断言改用常量 DRY + 补 frontmatter name 锁）。test-agent 真机待主 session 派。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-15-tars-skill-rebrand.md)。

## [2026-07-15] in-session v5 §8 step 6 —— teams install nga/cac 全套（CAC≡cc / NGA≡opencode）【spec v5 §8 全 step 收尾】

用户澄清 CAC ≡ Claude Code（`.claude`→`.cac`）、NGA ≡ opencode（`.opencode`→`.nga`），install 阶段两家族全套统一装（不只 skill）：cac 走 cc 家族（skill + nudge Stop-hook：`.cac/hooks/orca-nudge.sh` + `.cac/settings.json`）、nga 走 opencode 家族（skill + plugin `orca.ts` idle nudge + `opencode.json` 声明指 `.nga`）。`run_install` 按家族路由（opencode+nga / cc+cac，显式 `elif` + 末尾 fail-loud `AssertionError`），cc/opencode 零回归（byte-identical）；泛化 `_opencode_plugin_decl` project-scope 路径用 `hr.root.name`（去硬编码 `.opencode`，opencode 旧值不变）。SPEC §4.3/§4.4/§11/§9#1 同步升级为「家族全套」。164 单测 0 回归（+4 净增：cac/nga 全套 + nga project-scope 泛化闸门 + cac/nga 幂等）；code-reviewer 两轮 0 🔴（Rule 7 surface 一处镜像测试冗余）。真机加载（CAC/NGA 是否真读 `.cac`/`.nga` + nudge/plugin 生效）留 §9#1 跨平台用户侧。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-15-in-session-step6-nga-cac-install.md)。

## [2026-07-15] in-session 批量闭环 FU-2 + 3a + FU-3 —— status 活跃+结构化 / doctor 删 entry_hook dead / skill 补 error_kind

三个独立低复杂度 follow-up 合并单 commit。FU-3：`orca status` 无参对齐 SPEC §2.1/§2.3——只列活跃 run（marker `runs/orca-*.json`）+ 结构化 `{run_id,node,status,last_next_at,elapsed}`（时间字段取 tape `Event.timestamp` 末事件，**非** RunState 零时间字段 / **非** marker mtime；`elapsed` 用 `time.time()` 同基非 monotonic，spec-reviewer 时间基纠正）。FU-2：doctor 删 entry_hook check（step 4 整删 transform 后 PROBE_ENTRY 心跳永不再写，dead）+ 连带死代码（`PROBE_ENTRY_NAME` 常量 / `_read_probe` 死变量 / 报告路径行）；5→4 checks，advance_hook 保留（idle hook 仍写）。3a：SKILL.md 失败处理补 `error_kind` 一句（5b 信封加字段后），同步已装副本。132 单测 0 回归；code-reviewer 两轮 0 🔴（时间基钉死 / marker skip 路径 / 非 empty 人类可读分支全补测试）。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-15-in-session-batch-fu2-3a-fu3.md)。

## [2026-07-15] in-session v5 §8 step 3b —— catalog 物理迁 orca/compile/catalog.py（依赖铁律归位）

catalog（workflow 发现/加载/描述）从 `iface/mcp/catalog.py` 物理迁到 `orca/compile/catalog.py`（`git mv`，内容字节不变）：它是 compile 层关注却坐在 iface = 依赖方向越位，迁入 compile 与 parser/validator 同层方向正。7 处 lazy import → 顶层 **module import** `from orca.compile import catalog` + `catalog.<fn>()`（偏离原计划裸函数 import 的正当修正：commands.py/in_session 各有同名 typer 命令 `list_workflows`，裸 import 触发 RecursionError；且 `mock.patch("...catalog.list_workflows")` 守门单一真相源契约需 module 属性动态查找才 bite——code-reviewer 两轮实证）。9 处 mock target 同步（test_catalog 2 + 跨文件 7）；守门 grep `iface/mcp/catalog` = 0；1123 passed 0 回归（7 failed 全 pre-existing env-blocked，stash 对比复现）。test-agent 真机三路 list 一致待跑。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-15-in-session-step3b-catalog-relocate.md)。

## [2026-07-15] in-session v5 §8 step 5b —— daemon batch emit + in-session 错误信封统一（×2）

daemon `next()` 逐条 emit → `emit_batch`（注释「反例 A 消除」原为假，SIGTERM 落批内留半截 tape → resume state_corrupt，铁律 12）；in-session 失败信封统一（daemon + cli，MCP 出 scope：8 tool 全用 phase-11 ErrorKind 轴）：抽 `_step_io` helper（`apply_step_result` 吸收 `_emits_to_event_datas` + `fail_in_session`），daemon `_fail` 的 isinstance 塌缩消除，改读 `exc.error_kind`。信封加 `error_kind` 字段（tape `data.kind` 不变，两者同值——B4/B7 字段名陷阱）。新建 `test_daemon.py`（InSessionDaemon 零覆盖补齐 5 测试：成功路径 / batch emit spy / 畸形 output→kind+error_kind / 反向无 `in_session_error` / 终态+非终态幂等）；拆分误并入 malformed 的 render_error 测试。SPEC §7.5 ×3→×2 + MCP 排除；§2.3 信封加 error_kind。348 单测 0 回归；code-reviewer 两轮 0 BLOCKER（Round 1 M1 经 git show 核验非回归 disputed + m1/m2 fixed；Round 2 m1/m2 fixed + m3 登记）。跨阶段 debt：tape `workflow_failed.data.kind` 是 ErrorKind/error_kind 两值集共享字段，登记 CURRENT。test-agent 真机 E2E 待跑。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-15-in-session-step5b-daemon-error-envelope.md)。

## [2026-07-15] in-session FU-1 —— orca stop/open 加 --run-id option（命令族统一，套 DEFECT-2 e763e9e）

`stop`/`open` 都只有位置参数、缺 `--run-id`，但 SKILL.md + SPEC §2.1 教 `--run-id` → 主 session 照跑报 `No such option: --run-id`（test-agent 真机复现）。修：抽 `_merge_run_id` helper（status/stop/open 三处合流 DRY，防漂移）；stop 位置参数必填→可选 + None 守卫（保 missing fail loud exit 2）；open 加 option（None=活跃 run 默认）；status 既有内联合流替换为调 helper。125 单测 passed 0 回归（+15 FU-1）；code-reviewer 两轮 0 BLOCKER / 0 MAJOR。test-agent 真机 E2E 待跑。顺带回填 step 5a 文档 SHA 占位符为 `bce29f8`。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-15-in-session-fu1-stop-open-runid.md)。

## [2026-07-15] in-session v5 §8 step 5a —— 删 setup phase 全栈 + MCP migration note（A2 gate 保留）

删 setup phase 全栈（路径 B 死代码）：schema `Workflow.setup` / compile `_check_setup_phase_constraints` + jinja valid_root 去 setup / exec `RunContext.setup` + render setup ns / run orchestrator setup_ns / iface(mcp/web/cli) 全层；MCP breaking：删 `tool_get_agent_prompt` + `tool_start_workflow` 去 `setup_outputs`（migration note 兜底旧客户端）。m13 fail loud 靠 pydantic `extra="forbid"`（零新代码）。**A2 铁律**：execute phase gate 校验（`_check_execute_phase_no_gate_tools` / `_INTERRUPT_TOOL_NAMES` / `_check_no_interrupt_tools`）保留不删，唯一覆盖测试从 `test_setup_phase.py` 搬迁到 `tests/compile/test_validator.py`（防丢）。契约 doc 同步（setup 删后旧陈述变假）。1526 单测 passed 0 回归（8 failed 全 pre-existing env-blocked，stash 对比复现）；test-agent 真机 E2E 全绿（--help/list 契约 / 3 节点 bootstrap→next→completed / setup YAML fail loud exit 1 / A2 gate fail loud / doctor ok / MCP 8 工具）；code-reviewer 两轮 0 BLOCKER / 0 MAJOR。Commit: `bce29f8`。详见 [release note](../releases/2026-07-15-in-session-step5a-setup-removal.md)。

## [2026-07-15] in-session E2E defects 修复 + v5 §8 step 4（orca.ts transform 整删）

E2E 跑发现 2 defect 各独立 commit 修复：① cc_nudge.sh 缺 jq 时静默失败（fail-loud 违规）改用 python3 + marker 损坏时 stderr/exit 2。② `orca status` 加 `--run-id` option（与 SKILL.md/spec 一致；位置参数保留兼容；异值冲突 fail loud）。随后做 v5 §8 step 4 opencode 收尾：删 orca.ts transform marker 派发入口 + 9 个死代码 helper + `_constants.py`，**保留 idle nudge hook**（§4.4，opencode nudge 载体）；review 捕获 BLOCKER（test_web_default_and_open 跨文件漏扫 8 测试）+ MAJOR（advanceCount/lastAdvanceRunId 死代码）全闭环。spec 决策 #12 + 验收标准措辞修正对齐。185 affected passed 0 回归。Commits: `2de50e3`（DEFECT-1）+ `e763e9e`（DEFECT-2）+ `52cc9f3`（step 4）。详见 [release note](../releases/2026-07-15-in-session-defects-and-step4.md)。

## [2026-07-14] in-session v5 §8 step 2b —— 入口切 skill + list inputs_schema + doctor skill_install + 删 start/cc_hooks/command + nudge hook

实施 SPEC v5 §8 step 2b 全 7 项：in-session 入口统一切到 orca skill（三步指导：list→抽 inputs→<wf>+自调 next），删旧 command/start/cc_hooks 入口，nudge hook 提醒主 session 推进（**绝不自动推进**，B 路径铁律）。① 建 orca skill（CI 守门：三步指导 + 禁业务关键词 + 禁 teams 命令）。② `orca list` 返 `{workflows:[{name,description,inputs_schema}]}`（无 has_setup，无 describe）。③ doctor 加 skill_install 硬检查（A6）+ hook 心跳可选 + `hard` 字段定 ok。④ 禁用 orca.ts transform dispatch（early return，文件不整删——idle hook 保为 nudge 载体）。⑤ 删 4 个 command 模板。⑥ 删 start + cc_hooks（A 路径退场）。⑦ nudge（A5 修正入本步）：opencode idle hook 改提醒模式（listActiveRuns→节流→promptAsync 注入，不 spawn next）；CC 新 Stop hook（cc_nudge.sh，零反引号 decision:block）+ `teams install --target cc` 合并 settings.json。install 重构四前端（cc/opencode/cac/nga/all）装所有随包 skill，平台常量抽 skill_cmds 单一源（DRY/OCP）。208 affected passed 0 回归；code-reviewer 两轮（2 BLOCKER + 关键 MAJOR）全闭环。Commits: `e2bd989`（1-6）+ `4b90508`（7 nudge）。详见 [release note](../releases/2026-07-14-in-session-v5-step2b.md)。

## [2026-07-14] in-session v3 §8 step 1 —— orca 接口打包 + 14 命令归宿 + teams 变量化 + marker 精简

实施 SPEC v3 §8 step 1：① `orca` 顶层 = in-session 7 命令（`list/<wf>/next/status/stop/open/doctor`），删 `in-session` 子命令层；`bootstrap` → `orca <wf>` 语法糖（单一实现，hidden bootstrap + rewrite，非双入口）。② 14 后端命令归 `teams` entry point（`run/serve/ps/...`），`list`/`open` 共享单一实现。③ `ORCA_BACKEND_CMD` env 变量化（默认 teams）。④ marker 精简到 `{run_id, model, no_output_count}`（删 desync 向量 tape_path/yaml/session_id/owner），`marker_path(rundir, run_id)` O(1) 直定位（删扫描），yaml 从 tape.workflow_started.data.yaml_path 派生（唯一真相源）。⑤ 重复 bootstrap 同 wf → fail loud（m12，well-known `.orca-bootstrap.lock` serialize 防 TOCTOU，review B1 闭环）。⑥ 保留字黑名单（§2.2 MS1，compile fail loud）。⑦ B1 同 commit 改全活调用点（cli.py 驱动协议 / orca.ts spawn+argv / cc_hooks / command 模板）。⑧ `_inputs_from_tape` 首调噪声修复。in-session 134 passed（+37 新增），CLI 后端 + compile + orchestrator 281 passed 0 回归；code-reviewer 1 BLOCKER（并发 TOCTOU）+ 4 MAJOR + 5 MINOR 全闭环。详见 [release note](../releases/2026-07-14-in-session-v3-step1.md)。Commit: `d14cde5`。

## [2026-07-09] in-session 三件打磨（outputs 求值 + inputs 从 tape 恢复 + prompt 收紧）

model-driven advance 补丁（`4b3a4d6`）之上的 surgical polish：① `_final_outputs` fail-loud stub → `render_template` 求 `wf.outputs`（与 `Orchestrator._evaluate_outputs` 同源，渲染错 fail loud `ERR_RENDER_ERROR`，in-session 专用不动正常路径）；② `advance_step` 改 `Orchestrator._inputs_from_tape` 恢复 inputs（模型不必每步重传 `--inputs`，修非 entry 节点 `{{ inputs.* }}` 渲染隐患）；③ `run.md`/`_drive_protocol` 加「不许自己 Read 节点 .md」+ 修 stale 自动推进 + `bootstrap --format prompt` 补驱动协议（CURRENT 遗留 #2）。COMMAND→MCP 不换（解决不了实际失败模式 + 重复 phase-10）。in_session 96 passed（+3）；tests/run+iface 1007 passed 0 回归；code-reviewer PASS。Commit: `f86df86`。详见 [release note](../releases/2026-07-09-in-session-outputs-inputs-prompt-polish.md) + [计划](../plans/2026-07-09-in-session-outputs-inputs-prompt-polish.md)。

## [2026-07-08] Web attach + web 默认 + in-session open —— COMPLETE（e2e PASS，让 web 监控任意单 run）

Web v2 只认 in-process run 的 gap 补齐：**X** web 按 tape 路径 attach（read-only `tape_reader` + tail-follow + `RunView` 双 handle 单 registry）+ seq-windowed `/meta`/`/events` huge 模式 perf（103MB fixture `/meta` 5.2ms / `tail=500` 41.5ms）+ 安全 `relative_to` 三重守卫；**Y** `orca run` 默认起 web（浏览器自动开 + WS client-count 驱动 auto-exit）+ `orca open` / `/orca open` 打开任意 run（含 `--background` / in-session，observe-only）。SDD 全流程：SPEC rev2 spec-review PASS → Step1 `69e5c7b` → Step2 `fe81e42` → 3 e2e defect 修 `58947fd` → test-coverage-e2e 真跑 PASS（live P99=250ms / 安全 5+allowlist / §7 失败路径全过 / 铁律 grep）。pytest 674 + npm 262 绿。详见 [release note](../releases/2026-07-08-web-attach.md) + [SPEC](../specs/web-attach-and-default-spec.md)。Follow-up：`/orca open` fork-and-return、detached serve PID 管理。

## [2026-07-08] Web attach 3 e2e 缺陷修复（AC9 / AC11 / AC5 负向）

修 `test-coverage-e2e` 发现的 3 个真实缺陷：AC9 非 wf-started 首完整行被误判 running（upfront reject + 显式 probe_validated 参数替换 offset 推断 bypass + follow 立即拒 partial→complete 非 wf-started）；AC11 AskGate 忽略 writable=false（抽共享 gate-writable helper）；AC5 负向 活跃 WS 不挡 auto-exit（WebServer.active_ws_count + _wait_ws_autoexit count==0 AND window）。+5 后端 +3 前端测试；87 passed + 262 npm 绿。Commit: `58947fd`。routes 层 HTTP 403 端到端回归守门补 `test_attach_routes.py`（+2 TestClient 用例，code-review 🟡#2 闭环）。Commit: `3f7aa00`。详见 SPEC `docs/specs/web-attach-and-default-spec.md` §6.7/§8 AC9/AC11/AC5。

---

## [2026-07-08] Web attach Step2（Y）—— `orca run` web 默认 + `orca open` + `/orca open` slash

按 SPEC `web-attach-and-default-spec.md` rev2 §4/§5/§8 AC5-7/11 实现 Web attach Step2：`orca run <wf>` 默认走 web（probe 7428 → 复用 `POST /api/run` / 否则起新 in-process serve + RunManager.start_run in-process + `webbrowser.open` + WS 驱动 auto-exit（`last_ws_activity_at` env `ORCA_WEB_AUTOEXIT_SECONDS`）+ Ctrl-C 路径闭环）；`orca open <id>` CLI（probe / spawn detached serve / attach / browser）；`/orca open <id>` slash 走新 `spawnTopLevelCli`（plugin 哑传输 grep 守门 + 三元路由 signature-contract）。`--tui` opt-in 保留旧 Textual TUI；`--background` 不变。**code-reviewer 4 BLOCKER + 6 MAJOR + 3 MINOR 全闭环**（asyncio CancelledError / 双 shutdown / spawn FileNotFoundError / yaml_path resolve / --stay warning / routing signature）。674 passed / 30 skipped（+15 新增）+ npm 259 绿；铁律 grep 全过。Web attach feature COMPLETE（Step1 + Step2）。Commit: `fe81e42`。详见 [release note](../releases/2026-07-08-web-attach-step2.md) + [SPEC §4/§5](../specs/web-attach-and-default-spec.md)。

---

## [2026-07-08] Web attach Step1（X + perf）—— attach by tape path + huge-mode + perf

按 SPEC `web-attach-and-default-spec.md` rev2 §2/§3/§6/§8 实现：后端 `POST /api/runs/attach` + `RunView` ABC 双 handle（InProcess/Attached）+ read-only tail-follow（`EventBus.relay` fan-out only）+ 安全三重守卫（lstat + relative_to + open+fd-re-stat 防 TOCTOU）+ `GET /meta` huge 模式服务端 fold 派生 overview + `GET /events?since/limit/tail` 窗口化 + `GET /api/health`；前端 huge-mode（serverOverview slice + tail + 增量 prepend + load full）+ attached run gate observe-only。perf fast-path：`_scan_meta_overview` 单遍扫 + bulk-type substring skip + regex seq 提取（60k fixture ~150ms vs naive ~8700ms）+ `tail_events` 反向扫 O(tail)。**code-reviewer 2 BLOCKER + 6 MAJOR + 5 MINOR 全闭环**。1863 passed / 2 skipped（perf 默认 skip）。Commit: `69e5c7b`。详见 [release note](../releases/2026-07-08-web-attach-step1.md) + [SPEC §2/§3/§6/§8](../specs/web-attach-and-default-spec.md)。

---

## [2026-07-08] orca install —— 统一安装入口（全局默认 + 合并 skill/in-session）

收口碎片化安装（`pip` → `skill install` → `in-session start` 三步、两种 scope）为单条 `orca install [--target claude|opencode|all] [--scope user|project]`（全局默认）。**Step 0 spike 钉死承重事实**：opencode 1.14.22 无 `plugins/` 目录自动发现，plugin 加载**必须** `opencode.json` `"plugin":[<path>]` 声明（项目相对 / 用户绝对）——修掉既有「光丢文件不声明」缺口（`start` 之前只写两文件不碰 opencode.json，无加载 e2e 守门）。`skill install` 降为弃用别名（warn+委托）；`in-session start` 收窄为 CC-only run bootstrap（opencode 路运行时 `bootstrap` 自举）。code-reviewer 0 BLOCKER + 4🟡/3🟢 全闭环；`tests/iface` 689 passed + 新增零业务逻辑守门。详见 [release note](../releases/2026-07-08-unified-install.md) + [plan](../plans/2026-07-08-unified-install.md)。

## [2026-07-08] in-session compact prompt —— 文件交付 + 缺字段干净 fail loud（e2e PASS）

in-session shell 的节点 prompt 交付从"整段渲染文本注入主 session"改为**compact**：Orca 把渲染后 prompt 落盘到 `<rundir>/<run_id>/prompts/<node>.md`，主 session 只收一句 host-facing **指针**（"用 task 派子代理，完整指令已写入 `<path>`，先 Read 再执行"），子代理从文件读完整指令——主 session 上下文不再随节点数膨胀。两种 agent 形态（`agent:<name>` md 引用 / inline `prompt:`）渲染无差别（compile 已扁平化进 `node.prompt`）；plugin 零改动（仍读 `.prompt`）。**顺手修既有脏崩溃 bug**：`output_schema` 缺字段 / 畸形 schema / 下游 render 引用缺失字段，原本 `ExecError`/`SchemaError` 逃逸 → 无 `workflow_failed`、不清 marker、tape 悬挂、下次卡死；现 `_parse_output` 加 jsonschema 校验、`_render_or_fail` 包错 → 走既有干净 taxonomy（`output_schema_mismatch` / `render_error`）。不接 LLM `validator`——主 session 自己当判官。计划 [2026-07-08-in-session-compact-prompt](../plans/2026-07-08-in-session-compact-prompt.md)；SPEC `in-session-shell-design-draft.md` §2.1/§2.5 回填。code-reviewer 1 🔴（SchemaError 漏网）+ 🟡 全闭环。**顺手消既有债**：`InSessionError` 加 `error_kind` 显式字段 + `ERR_*` 常量，`_classify_in_session_error` 改直读字段（取代脆弱的消息子串匹配，类型安全）。92 in-session + 851 跨模块测试绿；e2e `/tmp/orca-compact-exp/repro.sh` PASS。

## [2026-07-08] Web Shell v2 —— 推倒重写 COMPLETE（单 tape + AH 风格，e2e PASS）

旧 Web 很差 → 按 SDD（SPEC→spec-review→clean-code→test-e2e）推倒重写前端：单 tape 唯一真相 + 单 Zustand store + codegen + AH 风格渲染（markdown/流式 RAF/工具折叠/DiffView/Charts/LogStream liveness/Gate/DAG）。后端 B1/B2（opencode translator lossless：reasoning/step_start/reasoning_tokens/unknown_event + `--thinking` 开关，EventType 37→39）。test-coverage-e2e 真跑（opencode+deepseek `--thinking` + Playwright + 全 39 类型 fixture）3 Must 全 PASS，铁律 AC 全过，npm 249 + py web 64 测试绿。Commits：c3a738f + 84a2645 + 5a26957 + 01af451 + 7d76934 + 60539b8。详见 [release note](../releases/2026-07-08-web-shell-v2.md) + [SPEC](../specs/web-shell-v2-spec.md)。Follow-up：demo_task 真 run 挂起（后端 opencode 冷启动，非前端）、DiffView LCS、Conv chunk 再拆、LogStream auto-scroll 真跑触发。

## [2026-07-08] Web Shell v2 Chunk D（completion + polish + bundle split）

完成前端**所有剩余项**（D1-D7）+ 86% bundle 减重（initial 2,035 KB → 290 KB / gzip 93.65 KB）。
D3 image URL rewrite（backend `/api/runs/<id>/assets/<path>` + 前端 `rewriteImageSrc` +
path traversal / symlink 守卫）/ D4 resume-fallback watchdog + `resume_ok` 协议 ack 帧（idle
场景不误触发全量重拉）/ D5 view 层 lazy 切分（ConversationView / ChartsView / WorkflowGraph
独立 chunk）/ D7 StatusLine 折叠修正（Chunk B YAGNI 偏离）+ e2e Gate / lazy DAG / markdown
渲染 / image rewrite / ws-fallback / dropBuffer 时序断言。1 BLOCKER + 3 MAJOR + 5 MINOR
全闭环。249 npm tests + 64 backend tests 双绿。**前端实现 COMPLETE，ready for e2e。**
详见 [release note](../releases/2026-07-08-web-shell-v2-chunk-d-completion-polish.md)。

## [2026-07-08] Web Shell v2 Chunk C（ChartsView + LogStream + TopBar + AgentsRail + useElapsedTick）

按 SPEC §5.1/§5.2/§5.4/§5.5/§5.6/§5.7 + §0 D5/D9 实现 6 个面板完整渲染 + 单一共享
elapsed tick。ChartsView（IntersectionObserver 懒挂 + 响应式 grid + scatter→bubble 扩
展 + selectCharts 唯一去重真相出口）/ LogStream（react-window v2 `scrollToRow` auto-scroll，
predictable-over-magic 状态机）/ TopBar（D5 elapsed live→snap，failed/cancelled 也 snap，
读 tape 末条 workflow_* 事件 ts）/ AgentsRail（per-agent elapsed + D9 stall + 单一 timer
断言）/ useElapsedTick（singleton useSyncExternalStore，N consumer = 1 setInterval）。
53 新测（170→223）全绿，build 绿。闭环 review 1 BLOCKER + 4 MAJOR + 6 MINOR 全闭环。
Commit: `01af451`。详见 [release note](../releases/2026-07-08-web-shell-v2-chunk-c-charts-log-tb-rail-tick.md)。

## [2026-07-08] Web Shell v2 Chunk B（ConversationView 全渲染）—— markdown + 折叠 + ▎ IFF + 工具展开 + 虚拟化

按 SPEC §5.3 实现中栏「会话」页签完整渲染。markdown stack（react-markdown + gfm +
math + katex + prism）+ per-EventType 全表（prompt/thinking/message/tool/dialog/
chart/custom/error/divider/status/unknown）+ 折叠规则（默认折叠/成组/永不折叠）+
▎ IFF（selectStreamingCursor：finished tape 必 false）+ smart arg（bash/read/write/
render_chart）+ DiffView/FileContentView（轻量自建）+ react-window v2 虚拟化（>500 条，
函数式 rowHeight 按 kind 估高）。闭环 review 1 BLOCKER + 4 MAJOR + 4 MINOR/NIT。
93 新前端测试（含 EventType 穷尽表驱动 + B1 回归 + 折叠 DOM oracle），170 passed。
Commit: `5a26957`。详见 [release note](../releases/2026-07-08-web-shell-v2-chunk-b-conversation.md)。

## [2026-07-08] Web Shell v2 Chunk A（foundation）—— codegen + 单 store fold + selectors + RAF 流式 + WS resume + 删除过期

按 SPEC §0 D1/D2/D6/D7/D8 + §3.1/§3.3/§4/§8/§10 实现前端基础层。新 `scripts/gen_events_ts.py`
（D1 codegen）+ pytest drift guard 根治 21↔39 漂移；删 Replay/multi-run/NodeDetail/formatLogLine
全部（§8 无兼容层）；单 Zustand store = fold(tape)，seq 升序 + refold（D7 序无关）；纯 selector
（selectAgents/Conversation/Charts/Log）；`useStreamingText` RAF 批处理 + 多 session sync-flush；
WS reconnect resume by seq（D6）+ server-side `_handle_resume`（重放 tape.replay(since_seq=N)）。
3-column 占位布局（AgentsRail/[会话|图表]/LogStream）。77 前端测试 + 55 后端测试全绿。
详见 [release note](releases/2026-07-08-web-shell-v2-chunk-a-foundation.md)。

---

## 模板

```
## [日期] 阶段名 —— 一句话描述
- commit: <SHA>
- 详情：[release note](releases/<date>-<name>.md)
```

---

## [2026-07-08] in-session shell v8.1 —— 修 5 bug + 签名契约测试（防 builder 回退）

按 SPEC v8 + e2e `/tmp/orca-e2e-v8/` 实证，修 shipped plugin 5 个真 bug（builder 上一轮从已验证
spike 回退导致）：A transform hook 签名（单参 → 两参 `(input, out)`）/ B event hook payload 包装
（裸 event → `input?.event ?? input`）/ F SDK message-fetch 非 list 改 REST fetch / G bootstrap+next
返 prompt 未 prepend Task-tool 指令（cli.py 端补，DRY 单一常量）/ E plugin 不透传 --model（从
info.model 动态抽，非 CLI 默认）。加 6 签名契约测试（断言 shipped 模板 transform/event/fetch/model
四处的代码形态 == spike 实证形态 + bootstrap prompt startswith Task 指令）—— 防再回退，根因教训
「TS 纯单测验不出运行时签名 bug」写进测试注释。baseline 83 → after 89 全绿，0 回归。守门 grep
（8 禁词）clean。Commit: `8bea9dd`。详见 [release note](../releases/2026-07-08-in-session-shell-v8.1-bugfixes.md)。

---

## [2026-07-07] web-shell-v2 B1/B2 —— opencode translator lossless + reasoning exposure

按 SPEC §3.2 + §11 step1 实现 web-v2 后端硬前置：opencode translator lossless（reasoning→agent_thinking / step_start→agent_step_started / step_finish 加 reasoning_tokens / 未知→unknown_event）+ EventType 加 2 项 + 全消费者 grep 审计（reducer no-op、LogStream/EventVISIBILITY/AgentHistory/summary 加 arm）+ B2 supports_reasoning opt-in + reasoning_flags_env env 注入（ORCA_OPENCODE_REASONING_FLAGS，默认 off）+ fixture 扩到 9 行。1758 passed / 0 新回归。Commit: `c3a738f`。详见 [release note](../releases/2026-07-07-web-b1-b2-translator-lossless.md)。

---

## [2026-07-07] in-session shell v8 —— 入口换 messages.transform + doctor 自检 + start 落 opencode 模板

按 SPEC v8（§2.6/§2.6.1/§2.6.2/§2.7）实现 v7→v8 增量。v7 CLI 大脑零改；本轮重写 plugin
模板（flat hooks + ctx.client + Bun.spawnSync + experimental.chat.messages.transform 入口，
spike 实证 v7 的 command.execute.before 在 opencode 1.14.22 不触发）、加 `orca in-session doctor`
3 项自检、统一 `/orca <sub>` 命令、start 落 .opencode/ 模板；CLI status 加 --json flag / stop
加 --owner（MAJOR-1/2 闭环），plugin spawnCli fail loud（MAJOR-3 闭环）。52 新测全绿
（31→83），全 unit 1775/1776（唯一 fail 预存 B-8）。
- commit: `56083c1`
- 详情：[release note](../releases/2026-07-07-in-session-shell-v8.md) + [SPEC](../specs/in-session-shell-design-draft.md) v8

## [2026-07-07] in-session shell v7 —— 薄 CLI 唯一大脑 + plugin/hook 哑传输
按 SPEC v7 + ADR v3 实现：CLI `bootstrap/next/stop/status/start` 唯一大脑（per-call flock
+ `Tape.append_batch` 单次 write 原子化 B1 + `--output` 空串 normalize B2 + 失败 taxonomy F6
+ 合规计数 F11 + marker RMW 在 flock 临界区内 N2）；plugin / CC hook = 哑传输（零业务逻辑，
grep 守门）；daemon 降级无头 CI。43 新测全绿，子集 1591 passed / 0 回归。
- commit: `6cd430c`
- 详情：[release note](../releases/2026-07-07-in-session-shell-v7.md)

## [2026-07-07] executor CLI 扩展 —— 命令唯一真相源 + spawn 参数全可改 —— `orca executor show` 打印完整生效 argv + 每字段来源（env/项目/用户/default）；`set --binary/--flags/--prompt-channel/--scope` 三维可改 + 项目/用户两层 config；接通 phase-14 遗留的 `resolve_flags` 死通道，新增 `resolve_prompt_channel`
- commit: `f4b10da`
- 详情：[release note](../releases/2026-07-07-executor-cli-extend.md)

## [2026-07-07] create-workflow skill + orca skill install + headless benchmark —— 通用 workflow 生成/转换 skill（吃描述或既有素材 → 归一化 DAG → Orca YAML+agent md，强制 orca validate 闭环），显式装 CC+opencode 两边；16 case 公平 headless benchmark + harness，评测闭环从 8/16 → 16/16，抽象 H1-H7 通用规则
- commit: `09fd7a8`
- 详情：[release note](../releases/2026-07-07-create-workflow-skill.md)

<!-- 新条目加在这里（本行下方）-->

## [2026-07-07] in-session shell（hook 驱动，宿主主 session 执行 workflow）—— 第四种执行驱动模式：宿主（opencode/CC）主 session 用自带 subagent 跑每个节点，Orca daemon 独占 tape + `observe`/`next` 单一接口 + `session.idle`/Stop hook 自动推进（立项、CCW 一致）。纯增量（drive_loop/from_tape/三壳零改），daemon 经 `advance_step` 原子决策、flock 独占 + 半写恢复 + 仅本地 FS（铁律 1 扩展走 ADR）。opencode serve 模式端到端验证：3 节点 `completed`、tape 事件序列与 `orca run` 逐 seq 对齐、并发两 run 隔离。v1：opencode serve + CC、仅 agent 节点（parallel/foreach/gate fail loud 走 TUI/Web）。
- commit: <待填>
- 详情：[release note](../releases/2026-07-07-in-session-shell.md)

## [2026-07-07] phase-16 —— AgentHistory 单流重构（CC 风格 inline + 工具配对折叠）
AgentHistory 从「两区」（RichLog 摘要 + 独立 detail 面板）重构为**单条 RichLog inline 流**：tool_call+tool_result 按 `tool_call_id` 配对成一条 entry（就地升级保 seq/位置，避 `_selected_seq` dangling）；message bold+主题色 / thinking dim italic / tool `✓/…/✗` icon 视觉分级；Enter 全量 reflow（detail 内联）。删 `#agent-history-detail*` DOM（铁律 #7 无兼容路径）。reducer fold 顺序无关（`_pending_results` 缓冲）。28 单测 + 3 真 tape boot smoke + 1 phase-12 e2e 断言回填；mxint report_painter 79 events fold 30.9ms（< 300ms SPEC §7 标准）。详见 [release note](releases/2026-07-07-phase-16-agent-history-single-stream.md)。

## [2026-07-07] TUI bugfix 批次 A —— layout + AgentHistory 三体感 bug
- layout：NodeDetail `display:none`（修右侧栏全黑：原 `height:0+offset` 不移出布局流，把 `#right-pane` 挤到 width=1）+ AgentsList `height:1fr`（修左栏 auto-size 截断）。
- A.1 Enter 无选中时默认作用于最后一条（修「Enter 没反应」）；A.2 移除死键 `c`（App + NodeDetail 两处，图表统一走 `C`）；A.3 `#agent-history-detail` 包 VerticalScroll（修长 report 截断）。
- code-reviewer 回改：删 NodeDetail 残留 `c` 绑定（接口统一性）+ docstring + 空 entries 显式测试。
- 详情：[release note](releases/2026-07-07-tui-bugfix-batch-a.md)。

## [2026-07-07] setup_outputs 注入 runtime context（phase-10 🔴 技术债回填）
MCP `start_workflow(setup_outputs=...)` 真注入：校验后穿透 RunManager.start_run → _run_with_sem → Orchestrator.__init__ 包成 `{agent: {"output": raw}}` 存 RunContext.setup → render 暴露 `{{ setup.<agent>.output.<field> }}`；_make_ctx 透传 setup。resume + setup phase → fail loud（边界声明）。code-reviewer 🔴 修复：`with_locals` 改用 `dataclasses.replace`（原手工列字段漏传 setup，foreach body 引用 `{{ setup.* }}` 静默拿空 dict）+ 补 foreach+setup 回归测试。E2E setup workflow 强化（deploy 真消费 setup 变量）。1688 passed / 0 回归。详见 [release note](releases/2026-07-07-setup-outputs-injection.md)。

## [2026-07-07] CLI `list` 与 MCP `list_workflows` 统一（catalog 同源）
CLI `list` 子命令委托 MCP 同源的 `catalog.list_workflows()`（按 `wf.name` 扫 `./workflows` + `~/.orca/workflows`，first-wins），删旧 `--dir` 扫 `./examples` 按文件名逻辑（接口统一铁律：全量替换）。CLI 与 MCP 现在看到完全一致的 workflow 列表。详见 [release note](releases/2026-07-07-cli-list-mcp-unify.md)。

## [2026-07-07] phase-10 MCP v4（9 工具 + setup/execute 分相 + Result 信封）
server.py 重写：6 旧工具（含 resolve_gate）→ 9 v4 工具（Discovery 4 + Lifecycle 3 + History 2）；setup/execute 分相（workflow.setup 字段 + compile validator execute phase 拦截 ask_user/gate + setup phase 结构约束）；三重杠杆防跳过 setup；Result 信封（kind 是 ErrorKind 值，无 layer）；新增 catalog / setup_phase / agent_catalog / tape_index 模块。Commit: df563f4。详见 [release note](releases/2026-07-07-phase-10-mcp-v4.md)。

## [2026-07-07] TUI v2 review remediation + 批 1 backend（Status.blocked + projections.py）
- 修 commit 5562e5e 回归（j/k hoist 后 down/up 无绑定，Enter 展开非末条 entry 失效）：
  App 级 BINDINGS 加 `down`/`up`（`priority=True` 覆盖 RichLog scroll）+ 3 pilot 测试。
- 批 1（ADR §4.3/§4.3.1）：Status Literal 加 `blocked`；`orca/run/projections.py` 单一
  派生算法源（node_status / node_usage / node_session_ids / node_iter），apply_event
  扩展 blocked fold（gate/interrupt 同源），TUI 删独立 fold 副本（`_node_session_ids` /
  `_per_node_last_usage_seq`）全部改调 projections（DRY）；`agents_list.py` 类型收紧
  Status + 删 `== "failed"` 字面量比较（P4）；AST 守门（`test_status_literal.py`）。
- 1596 passed / 0 回归（baseline 1558 + 38 新增）。
- commit: 见 `git log`（commit message 末尾含 Claude+Happy co-author）。
- 详情：[release note](releases/2026-07-07-tui-v2-review-batch1-projections.md)。

## [2026-07-07] phase-11-process-lifecycle —— 子进程生命周期管理（ProcessRegistry DI + 进程组 cancel + 退出码 5 档）
新增 `orca/exec/registry.py`（ProcessRegistry DI + 三段式 cancel SIGTERM→SIGKILL→cleanup + 平台分支 POSIX killpg/Windows CTRL_BREAK）+ `orca/iface/exit_codes.py`（ExitCode 5 档 0/1/2/3/130 + `exit_for_terminal_status` 纯函数派生）；runner.py / script.py 接入 `start_new_session=True` 进程组隔离（推翻 phase-3 §2.5 旧决策）+ registry.acquire/release；orchestrator.py 加 `shutdown()` 方法（不动 phase-11-error except 链）；run/__main__.py SIGTERM handler 只设 `threading.Event`（signal-safe，SPEC §1.3）+ 退出码经权威派生。code-reviewer 2 🔴 + 5 🟡 闭环（script.py 铁律 1+2 违规修复 / DI 闭环留 phase-12 follow-up / `_handle_timeout` 加 2s 超时防御 / singleton 测试复位 / asyncio.run+signal 交互注释 / script.py try/finally 覆盖 CancelledError）；test-coverage-e2e 真跑 5 项验证全过（退出码 0/1/2 / pgid==pid 证 start_new_session / shutdown 3 次幂等 / grep 守门 clean）。**1558 passed 0 回归**（baseline 1525 + 33 新增）。Commit：`cdc3469`。详见 [release note](releases/2026-07-07-phase-11-process-lifecycle.md)。

## [2026-07-07] TUI Redesign v2 —— 取消 DAG + agent 输出可见 + 切换 agent 看历史（三块布局重写）
TUI 三块布局重写：左 30% AgentsList + 右上 70% AgentHistory + 右下 30% LogStream。真删 v1.1.1 widget（DagGraph / dag_layout / _dag_render / activity_stream）+ display:none 双写兼容路径。用户核心需求闭环（last message 默认展开 + j/k 切换 + Log Stream 5 level icon）。
SPEC：[tui-redesign-v2-design-draft.md](../specs/tui-redesign-v2-design-draft.md) · release：[2026-07-07-tui-redesign-v2.md](../releases/2026-07-07-tui-redesign-v2.md) · commits：59021c9 + 5f9988c + e252653 + ab3b254 + 0e9e877 + 77f5685 + 85ecb61

## [2026-07-07] phase-11-error-handling —— 统一错误处理（ErrorKind 11 分类 + Result 信封 + classifier 双入口）
ExecError 字段集改 `{kind,message,phase,node,raw}`（kind 必填唯一分类轴）；新增 4 个 exec/ 层模块（`error_kinds.py` / `result.py` / `classifier.py` / `retry.py`）；`WorkflowAborted/MaxIter/RouteError` 改 ExecError 子类（固定 kind,phase），`WorkflowTerminated` 保留独立；error_type→kind 全量迁移（emit 写 kind + 读兼容期保留 error_type）；retry_started.data 扩展 layer/kind/reason/next_retry_at；编排 exception 子类化 + orchestrator except 顺序（WorkflowTerminated 先于 ExecError）。code-reviewer 3 个 🔴 + 8 个 🟡 闭环（wait.py 走标准 ExecError 路径 / `_classify_error` 用 ErrorKind.X.value / classifier profile 钩子加 warning log / DRY `_with_retryable` helper / 补 transport retry 测试）；test-coverage-e2e 真跑 demo_max_iter + opencode bad model 发现 2 处 emit defect（**Defect A**：orchestrator retry path 漏写 `next_retry_at` / **Defect B**：`layer` 与 `kind` 经两份派生表不一致）→ 已修 + 加 regression test。**1525 passed 0 回归**（baseline 1386 + 139 新增）。Commit：`451dd39`。详见 [release note](releases/2026-07-07-phase-11-error-handling.md)。

## [2026-07-04] TUI 重设计 v1.1.1 —— 真用户验证 4 GAP 收口（A/B/C/E）
修 test-coverage-e2e 真跑发现的 4 个 spec 违规：(1) **GAP-A** `app.py` agent_usage 同步投 `DagGraph.update_node_projection(tokens=...)`（DAG 行 3 由 `-- tok` 变实际数字，spec §4.4 acceptance）；(2) **GAP-B** Activity Stream 维护 `tool_call_id → (tool, args, call_ts)` cache，`agent_tool_result` 反查派生 tool/args（canonical Event result data 仅含 `{tool_call_id, result}`），summary 由 `?  {}` 变 `glob **/*.py` 等（spec §5.4「与 call 同 entry」语义）；(3) **GAP-C** elapsed 从 `call.timestamp + result.timestamp` 派生（顶层 Event 字段，spec §3），spec §5.4 订正为 `<N> lines · <elapsed>s`（exit_code 可选，canonical 不支持）；(4) **GAP-E** `DagGraph.build_from_workflow` 允许 self-loop（loop workflow `counter → counter` 重入语义），多节点环仍 fail loud。新增 8 测试 + 真 TUI 重放脚本（`_tui_gap_verify.py`），**1392 passed 0 回归**（baseline 1380 + 12 新断言），mxint tape 重放 5/5 节点 tokens 全非 None + 60/60 tool_result summary 含 tool name + meta 含 elapsed，demo_loop tape 重放 counter iter=3 与 node_started 次数一致。
Commit：`225933e`。详见 [release note](releases/2026-07-04-tui-redesign-v1-gaps-abce.md)。

## [2026-07-04] TUI 重设计 v1（spec v1.1 全 P0 闭环：3 行盒子 DAG + Activity Stream 双行 entry + EVENT_VISIBILITY 噪音治理 + 取消 NodeDetail + `f` 键 filter）
TUI 整体重设计对齐 spec v1.1（spec-review-adversarial conditional-pass → 5 P0 + 3 用户决策闭环）。新增 `_event_filter.EVENT_VISIBILITY`（7 tag 全 32 EventType 覆盖 + 完整性测试守门）+ `_dag_render` 独立渲染 helper（3 行盒子 + fan-in `(N inputs · M/N arrived)` 副标 + `after=None` 单独 section + ≥5 并行 fallback）+ `activity_stream` 双行 entry + 折叠详情（32 EventType per-type 字段级映射，复用 phase-15 `render_tool`/`render_message`/`render_thinking`）+ Header footer per-node usage（横向滚动 + running 优先）+ `f` 键 filter 模式（O1=c 取消 NodeDetail 但保留实例兼容）。reducer 派生 fold：iter 号 `node_session_ids`（重放产相同值，retry/skip/interrupt 不算新 iter）；fan_in arrived（dst 节点 node_completed 累加）。**单向依赖守住**（新模块零 orca.exec/run/events.bus 反向 import）。**1380 passed 0 回归**（baseline 1333 + 47 新测试），mxint 真跑 tape 重放 SVG 截屏（186 events → 152 进 Activity Stream，filter 掉 17 prompt_rendered + 17 agent_usage）。
Commit：`7bd43ef`。详见 [release note](releases/2026-07-04-tui-redesign-v1.md)。

## [2026-07-04] mxint_analysis 真实 bitx 量化分析迁移（替 stub + 5 agent prompts 真版）
将 `examples/mxint_analysis.yaml` + 5 个 agent prompts + `tests/e2e_mxint/` 从**简化 stub**（伪 SimpleNet + fake JSON，2 分钟跑完）迁移到**真实 bitx 量化分析**：target 换成 `ConfigurableMLP`（8970 params，sklearn digits 8x8，~90% eval_acc）+ 真调 bitx `Session` + 5 observers + `StudyReport.save` + `run_diagnostic_pipeline` 三阶段；2 个 driver script（`run_analysis.py` / `run_diagnostic.py`，后者含 bitx 1.1.1.dev395 `DistOverlayData.to_chart_data` bug 的进程内 monkey-patch）。**foreground 真跑 185s**（>2 分钟 stub baseline），5 张 chart（accuracy/bottleneck/sensitivity/qsnr_depth/recovery）真推 tape，76 行 REPORT.md 含真 QSNR 数据（51.37 dB avg，weight-dominated，recovery 31.7%）。**1333 passed 0 回归**。已知 follow-up：`_run_workflow_headless` 不起 chart ingestor，但 env 仍透传死 sock 路径（background 模式 chart 不通，prompt 让 agent 优雅 fallback）。
Commit：`838695f`。详见 [release note](releases/2026-07-04-mxint-real-bitx.md)。

## [2026-07-04] phase-15 render layer v1 —— e2e gaps 闭环（GAP#1 opencode read 文件 envelope + GAP#2 file_write subtitle）
修真跑发现的 2 个用户可见视觉异常：(1) opencode `read` **文件** result 同样是 XML envelope（与目录同形），原 `_normalize_file_read` 只检测 directory，file 走兜底 → envelope tag 泄漏 + opencode 自带 `N:` 前缀与 Rich Syntax 双重行号 + `(End of file)` marker 漏出；抽统一 `_parse_opencode_xml_envelope` helper（DRY），剥三层修饰（envelope 起手换行 + `N:` 前缀 + EOF marker）+ 仅 `<path>` 起手式才尝试 XML 解析（避免 claude Read 普通 HTML/XML 文件误判）+ fail visible（解析失败/未知 type/缺字段 → warning + 降级原文，§13）。(2) `_make_subtitle` 加 `file_write` 分支 → `new, NB`（spec §8.1）。spec §6.3 同步订正（原"opencode read 文件：同 claude"与实测不符）。**1333 passed** 0 回归（baseline 1327 + 6 新增）；真跑 tape seq=5 验证 72 行 TOML 干净渲染。Commit：`900fcfd`。详见 [release note](releases/2026-07-04-render-layer-v1-e2e-gaps.md)。

## [2026-07-04] phase-15 render layer v1（TUI 端）
实现 render-layer-design-draft §11.1 v1：在 canonical Event 之上加 iface 层纯函数渲染抽象（`normalize_tool` → RenderItem → `render_tool` → Rich renderable）。新增 `orca/schema/render_item.py` + `orca/iface/cli/widgets/tool_render/`（normalize/kinds/registry/reduce，单向依赖 only schema+rich+stdlib）+ `tests/e2e_phase15/_artifacts/render_tool_cases.json` 11 case fixtures + `tests/iface/cli/test_tool_render.py` 32 test（snapshot + fail loud + reducer + claude-code 对齐 acceptance §14.1）。迁移：log_stream 工具事件摘要共享 `describe_tool_event`（DRY，行为不变）；node_detail 流式 tab 工具事件升级为 Rich tool card（opencode read 目录现渲染为 17 条目树，不再 XML 一坨）+ thinking dim+italic 纯文本 + `t` 键切可见性（§12.8）。**1327 passed 0 回归**（baseline 1276）。Web 端 / shiki 流式 / 复制按钮 / codex 显式不做（v1 外）。
Commit：`ae0126b` + `edd738f`。详见 [release note](releases/2026-07-04-render-layer-v1.md)。

## [2026-07-03] examples 整理（固化 opencode 后端 + description + render_chart example + 全跑通 e2e）
13 agent example 固化 `executor: opencode` + `model: "deepseek/deepseek-v4-flash"`（with_ask_user 保留 claude——ask_user 需 mcp_tools=True）；补全 21 example description（TUI 信息明确）；`examples/README.md` 分类（纯 script / agent workflow / claude-only 例外）；新建 render_chart example（**文件夹化 agent** plotter + scripts/chart_demo.py 资源，演示 phase-14 `ORCA_AGENT_RESOURCES` + phase-13 chart 链路）；parallel_research 迁移 phase-14 `agent: <name>` 显式引用（消除旧约定 warn）。**验证**：8 script + 13 agent + render_chart 全跑通（opencode+deepseek-v4-flash **真跑不 mock**）；with_ask_user 例外（claude-only）。tests: test_examples_script + test_examples_opencode。
Commit：`c5c13b1`。详见 `examples/README.md`。

## [2026-07-03] phase 14 Agent 一等化（agent 池 + 文件夹化 + 统一解析层）+ Route 输出变换（批 1）
agent 从内嵌 prompt 升级为可命名/可复用/可携带资源的一等公民：新增 `orca/compile/agents.py` 统一解析层（`AgentResolver` Protocol + `LocalPoolResolver`，**删 `_load_prompts` + `_load_agent_md` 双加载债**）→ `AgentNode.agent` 显式引用 + 文件夹化（`<name>/agent.md` + 资源子目录）+ frontmatter 元数据 + `Route.output` 终点输出变换 + MCP `list_agents`/`get_agent`。**spec-review-adversarial 对抗审闭环**（2 P0 + 5 P1：warn 通道/skip end_route 统一/tools None 消歧/is_folder/frontmatter 精确算法/空串防御）。实现期修 SPEC 隐含缺陷（互斥预检须物化前）。**opencode+deepseek-v4-flash 真跑 e2e**：E2E-1 agent 引用（GREETER_OK）+ E2E-2 文件夹化 resources（`$ORCA_AGENT_RESOURCES` → SECRET_FLAG_42）。顺带修 executor capability guard（opencode + tools 不注 `--allowed-tools`）。**1276 passed 0 回归**。批 2（包分发 + workspace-instruction）留 phase-15。
Commit：`74d65b3`。详见 [release note](releases/2026-07-03-phase14-agent-first-class.md)。

## [2026-07-03] phase 13 script-side render_chart 接入（env 身份路由 + per-run Unix socket + 大数据三道关 + opencode+deepseek e2e）
让 claude/opencode/script 节点 spawn 的 script 子进程调 `orca.chart.render_chart` 推图：env 注入 4 个 ORCA_*（ClaudeExecutor + ScriptExecutor 都接，**executor-agnostic S5 闭环**）→ subprocess 链自然继承 → per-run Unix socket 传输 → tape 落 custom(chart) → 三壳零改动渲染。**对抗审闭环 16 处修订**（4 blocker + 9 major + 3 minor，含 ack timeout / sock 路径长度 / resume 边界 / opencode env 继承 / envelope 含义 / hue 分组降采样 / table 取前 N 等）。**大数据三道关**：自动降采样（max_points=2000，6 chart_type 各自策略）+ 2MB 硬上限 + ingestor 复核。**E2E-5 压测**：3 run × 10 chart 无丢失/串扰；**E2E-6 opencode+deepseek-v4-flash 真跑**：4 验证点（agent_message 完整性 / TUI 各面板合理 / render_chart 推送 / 图表排布）逐条通过；TUI snapshot 留档。**1224 passed 0 回归**（baseline 1208→1224，新增 16 测试）。S5 顺带修 2 实施 gap：ScriptExecutor 漏 chart env（违反 SPEC §11 #9）+ OrcaApp CLI shell 漏起 ingestor。
Commit：`1740a98`（S1-S4）+ `f260935`（S5 实施 gap 补丁）+ `b562a12`（S5 e2e）。详见 [release note](releases/2026-07-03-phase13-render-chart.md)。

## [2026-07-03] phase 12 CLI TUI 重设计（拓扑图 + NodeDetail + 终端图表 + opencode e2e）
重设计三面板：左 DagTree→DagGraph 拓扑图（分层+连边，max 33%）、右上 ActiveNode→NodeDetail（流式/输出/图表 tab，6 kind 永不空白）、新增终端图表渲染（plotext braille）+ ChartBrowser 全屏。6 新文件零后端 import、壳无真相、确定性 fold、`_selected_node`/`_auto_follow` 不写 tape（全有单测守护）。LayeredDagLayout spike 全过（未 fallback）。**S10 e2e：opencode 后端（glm-4.6v）真跑驱动 TUI 端到端通过**（SPEC §6 逐项 + 断言证据；图表渲染走解耦注入真路径——braille + 多图分组规整；`render_chart` 生产者未实现，待 phase-10）。e2e 顺带修真 bug：`ClaudeExecutor` 无条件注 `--allowed-tools`/`--mcp-config` → opencode spawn 失败，gate 到 `capabilities.mcp_tools` 修复。**1133 passed 0 回归**（基线 1082→1133，净增 51 测试）。
Commit: `38fd78c`（S0-S9）+ `cd6c1ee`（opencode spawn fix）+ `81d2f93`（S10 e2e）。详见 [release note](releases/2026-07-03-phase12-tui-redesign.md)。

## [2026-07-03] 后端统一抽象 + opencode 后端接入
把"后端怎么信号 done+result+usage+错误"下沉成 profile 字段 `TerminalContract`（`result_line` /
`events` 两模式）+ 共享 `RunAccumulator`，executor 保留一处小分支，runner 不动。加 opencode =
加 translator + profile 两文件（events 模式，prompt_channel=argv）。E2E 发现并修 runner 的
argv-channel stdin 不关闭导致 opencode 永久挂死的真实 bug。真实 orca CLI 双后端 E2E 跑通
（opencode glm-4.6v + claude/deepseek，均 completed）。688 passed 0 回归。
Commit: `f3129d1`。详见 [release note](releases/2026-07-03-opencode-backend.md)。

## [2026-07-02] orca executor —— 持久化后端二进制配置 + 健康检查
新增 `orca executor set/show/unset/list/test` 命令组：`~/.orca/config.json` 持久化 per-profile
binary override，`orca` 启动期 `os.environ.setdefault` 注入，复用既有 `resolve_cli_path()` 运行时
读 env——**exec/profile/registry 零核心改动**（OCP）。`pip install` 后 `orca executor set claude
"ccr code"` 一次设、全局生效；`executor test` 真起子进程自检协议兼容性（两层超时 + spawn 失败
fail loud）。顺带把 ccr profile 的 dummy translator 接上 `claude_translator`（ccr 协议兼容）。
config.py + executor_cmds.py（含纯函数 classify）+ 35 单测 + 9 e2e（假脚本走完整 spawn 链路，
不 mock CLIRunner）+ 2 integration。终审 0 🔴 1 🟡（已修）/ 2 🟢（跳过）。1031 passed 0 回归。
Commit: `ce559b6`。详见 [release note](releases/2026-07-02-executor-config.md)。

## [2026-07-02] agent 可观测性 + TUI 闪退 + 子进程泄漏修复（4 bug）
排查 demo_mixed 529 闪退时定位的 4 个 Orca 自身 bug：① OnResult 加 `api_error_status` 第 5 参
（全仓 11 处同步），executor `_result_diag()` 让 529 等 API 错误详情落到 `node_failed`（原只带空 stderr）；
② translator ApiRetry 对齐真实字段 `attempt`/`retry_delay_ms`/`error_status`（原读 `retry_count`/`wait_seconds`
永远 null，显示「第 ? 次」）；③ TUI 终态后停留 + notify 提示「按 q 退出」（原 `self.exit()` 闪退）；
④ `CLIRunner.stream()` finally terminate proc（原中途 q 强退留孤儿 claude）。7 新测试，985 passed 0 回归。
Commit: `f422d98`。详见 [release note](releases/2026-07-02-agent-observability-tui-fixes.md)。

## [2026-07-02] terminate step —— 新增 node kind `terminate`（业务级显式工作流终止节点）
新增第 6 个 node kind：触达即终止，`status=success` → `workflow_completed`（用 terminate.outputs），
`status=failed` → `workflow_failed{error_type=WorkflowTerminated, message=reason}`。补 `TerminateExecutor`
（仿 set_node 模板）+ factory 分派 + orchestrator 终态分发（新 `WorkflowTerminated` 异常 + `_finalize_terminated`
helper）+ compile 层 4 项 fail loud 校验（routes 空 / 非entry / 非parallel branch / 非foreach body）。
零 EventType/reducer 改动（复用既有 `node_completed`）；19 新测试，1013 passed 0 回归。
Commit: `41a5936`。详见 [release note](releases/2026-07-02-terminate-step.md)。

## [2026-07-02] phase 11 收官 —— CLI feature 补全全部完成（11 feature，652→959 测试，0 回归）
对抗评审（fail→conditional-pass，22 真问题闭环）→ 4 wave clean-code-builder + 4 wave test-coverage-e2e →
code-reviewer 横切审计（0 🔴 0 🟡）。交付 CI / Interrupt+Guidance / Resume / Retry / ask_user MCP /
Wait / Validator / Dialog / Skip / daemon 共 11 feature；e2e 审计狩猎并修复 2 个单 Tape 不变量
critical bug（interrupt_resolved 丢事件 / Ctrl+G 打不断 wait）；9 处 SPEC 偏离全部 Rule 7 裁定双落。
Budget（D3）/ attach（D2）descoped。commit: `120085f`→`d295922`（见各条）。
- 详情：[release note](releases/2026-07-02-phase11-complete.md)

## [2026-07-02] phase 11 P3.2 —— daemon `--background` 模式 + ps/logs/wait（attach descoped）
长跑 workflow 不占终端：`orca run --background` fork detached child（headless Orchestrator，
非 TUI——detached 无 TTY Textual 会崩，SPEC §11.9 裁定），父进程立即返回 run_id + pid；
配合 `ps`（dead pid 标 crashed，fail loud）/ `logs <id> [-f]` / `wait <id>` 三件套。
`daemonize` 5-callback seam 可测（CI 不留孤儿）；run_id 经 env 父子一致（metadata/tape/orchestrator
三处对齐，resume 可接）。code-reviewer 1 🔴（BaseException 漏 SIGTERM）+ 6 🟡 + 2 🟢 全修。
904→956（+52），0 回归。Commit: 见 git log。
- 详情：[release note](releases/2026-07-02-phase11-daemon.md)

## [2026-07-02] phase 11 P4 —— Skip to Agent（显式 skip 目标 + NodeSelectModal + §9.2 route 容错）
wave-1 SKIP 只能沿 route 跳，无兜底 route 时 NoRouteMatch 崩溃（SPEC §10.2 item12）。本 wave 补齐：
`request_interrupt` 加 `skip_target` 参数 → `_drive_loop` 直接跳该 node（不经 route 求值）；
`NodeSelectModal`（iface/cli/screens/）让用户选目标（pattern A：InterruptModal → app 推选择器）；
router §9.2 容错（skipped node 的 None output 让 when 求值失败走兜底，非崩溃）；`_validate_skip_target`
fail loud（ValueError，非 NoRouteMatch）；`interrupt_resolved.data.skip_target` 写 tape 可观测。
code-reviewer 1 🔴（验证顺序致脏 tape）+ 3 🟡 全修。888→904 零回归。Commit: 见 git log。
- 详情：[release note](releases/2026-07-02-phase11-skip-to-agent.md)

## [2026-07-02] phase 11 fix —— Ctrl+G 立即唤醒 sleeping wait node（wave-3 e2e 审计 bugfix）
wave-3 e2e 审计发现 SPEC §9.7.6 + §10.2 item9 承诺的「Ctrl+G 打断 wait node」实际不工作：
`notify_all_waits` 原本只在 node 边界 `_handle_interrupt` 触发，wait sleep 期间 drive_loop 阻塞
在 `_dispatch` 到不了边界 → 对 sleeping wait 是死代码。修复：`Orchestrator.request_interrupt`
登记 pending 的同时即时调 `bus.notify_all_waits()`（保留 record_resolved/resolve 里的同一调用
作 defense-in-depth）。xfail 复现测试翻转 pass + 8 新 wave-3 e2e 测试采纳，879→888 零回归。
- commit: 89b23ab
- 详情：[release note](releases/2026-07-02-phase11-wait-interrupt-fix.md)

## [2026-07-02] phase 11 P2.2 —— Dialog（agent 跑完后多轮追问，重 spawn claude 拼历史）
用户按 `d` 键就已完成 agent 的 output 多轮追问：`DialogHandler` 3-method split（start/send/end），
每轮重 spawn claude 把「output + 完整历史 + 本轮问题」拼进 prompt（`-p` 路线无 in-process
session，靠 prompt 拼历史）。Rule 7 裁定 3-method split（SPEC §6.2 单一 run_dialog 无法在轮间
交还 UI 控制）；`ctx.dialog_history` 是 web shell replay 预留位（真相在 tape）；抽
`orca/exec/env.py` 化解三处 `_build_env_overlay` 重复（Rule 6 DRY）。+27 测试断言 INTENT
（含历史累积核心契约 + send 失败 fail loud + 按钮复位），852→879 零回归。
- commit: caa3943
- 详情：[release note](releases/2026-07-02-phase11-dialog.md)

## [2026-07-02] phase 11 P2.1 —— Semantic Output Validator（LLM 二次语义校验 agent output）
agent 产出后 spawn 第二个 claude -p 做 LLM 语义校验（非 shape/type），失败时 issues 作 guidance
反馈重 spawn，直到通过或预算用尽（fail-safe：validator 自身崩 → 当作 passed）。`validate_output`
纯函数不持 bus（Rule 7 化解铁律 2），三类 validator_* 事件由 orchestrator loop 统一 emit；validator
与 retry 独立预算（SPEC §11.6 deviation）。822 → 852 passed（+30，0 回归）。Commit: e4eb07c。
详见 [release note](releases/2026-07-02-phase11-validator.md)。

## [2026-07-02] phase 11 P3.1 —— Wait Node（asyncio.sleep 节点，Ctrl+G 可打断）
SPEC §9.7：新 `kind: wait` 节点（`asyncio.sleep(duration)`，`interruptible=True` 时可被 Ctrl+G 打断）。新增 `orca/exec/wait.py`（`WaitExecutor` + `parse_duration` + `WaitHandleRegistry` Protocol）+ `WaitNode` schema（加入 `AnnotatedNode` 判别联合，5 kind）+ `wait_started`/`wait_completed` 事件 + `EventBus.register_wait_handle`/`unregister_wait_handle`/`notify_all_waits`（SPEC §9.7.6 公开契约，`threading.Lock` 保护集合）+ `make_executor` 加 `bus` 参（仅 wait 分支透传）+ `InterruptHandler.resolve`/`record_resolved` 双路径调 `notify_all_waits`（Ctrl+G 立即打断正在 sleep 的 wait）+ `_PHASE_TO_ERROR_TYPE` 登记 `config`/`ConfigError` + LogStream 描述。**关键设计**：`WaitHandleRegistry` Protocol 化解「WaitExecutor 需 bus 访问」与「铁律 2 禁 exec 持 bus」的张力（ISP/DIP，能力裁剪到最小，executor 无法写 tape/emit，契约测试全绿）。SPEC §11.5 记 3 处偏离。**全量 822 passed / 1 skipped**（基线 784 + 38 新测试，0 回归）。Commit: `3921c89`。详见 [release note](../releases/2026-07-02-phase11-wait-node.md)。

## [2026-07-02] phase 11 P1.2 —— ask_user MCP 工具挂载（被编排 claude 主动问用户）
SPEC §5：Orca 进程内嵌 socket SSE MCP server（`AgentToolsMcpServer`，`mcp.server.fastmcp`），注册 `ask_user` 工具；被编排的 claude -p 经 `--mcp-config` 连上，调 ask_user 触发 `HumanGate(source=agent_ask)` → 等壳 resolve → 返回 answer。**SSE spike 双轮全 PASS**（in-memory ClientSession round-trip + real claude `-p --mcp-config` 连通性 + 工具调用）。确定性 tool-params 路由（D4：`orca_run_id`/`orca_node`，**不**依赖 MCP session 反查）+ spike 实证 claude -p 默认不给 MCP 工具授权（自动 append `--allowed-tools mcp__orca-agent-tools__ask_user`，SPEC §11.3）。register 债补完（B2）+ gates `RunContext`→`SessionLoc` 改名（B2）+ `unregister_run` 按 run 批清（SPEC §6）+ orchestrator `run()`/`run_from_state()` lazy start/stop server（start 失败 → workflow_failed fail loud）+ `_append_ask_user_instruction` 把路由参值拼进 prompt。**两轮 code-reviewer 全反馈闭环**（🔴 tape 配对断言 + unregister 接线 + start fail loud + 4 个测试 gap）。SPEC §11.2-§11.4 记 3 处偏离。**全量 773 passed / 1 skipped**（基线 753 + 20 新测试，0 回归）。Commit: `dcc3e63`。详见 [release note](../releases/2026-07-02-phase11-ask-user-mcp.md)。

## [2026-07-02] phase 11 P0.3 —— Retry Policy（节点级自动重试 transient claude 失败）
SPEC §9.5：agent node 声明 `RetryPolicy`（max_attempts/backoff/retry_on/jitter）→ transient 失败（spawn_error/timeout/api_error/http_429）自动重试，带 exponential/linear/constant backoff + ±20% jitter 防雪崩。新增 `orca/run/retry.py::execute_with_retry`（核心 loop：was_interrupted 短路 + retry_on 白名单过滤 + retry_started/succeeded/exhausted 事件可观测）+ `_compute_delay`（DRY 单点 delay 计算）+ `_classify_for_retry`（**error_type 对齐层**：桥接 ClaudeExecutor 的 `CliExitNonZero`/`ExecTimeout`/`ClaudeStreamError` 到 retry_on 的 `spawn_error`/`timeout`/`api_error`/`http_429` 语义短名，SPEC §9.5.2 对齐表）+ `RetryPolicy` schema（`Field(ge=1)` 下界校验）+ `ExecError.from_failed_data` classmethod（DRY：retry loop 与 execute_and_emit 共享）+ orchestrator `_dispatch` 集成（agent+retry 走 retry loop，否则既有路径）+ reducer retry_* no-op + LogStream 描述。validator（wave 3）将复用本 loop。**全量 753 passed / 1 skipped**（基线 726 + 27 新测试，0 回归）。Commit: `95cdae4`。详见 [release note](../releases/2026-07-02-phase11-retry-policy.md)。

## [2026-07-02] phase 11 —— `interrupt_resolved` 同步写 Tape 修复（wave-1 e2e 审计）
wave-1 e2e 审计发现 critical bug：CLI 单壳中断路径 abort/skip（continue 偶发）分支的 `interrupt_resolved` 被 async broadcaster 与 `run()` 的 `bus.close()` 竞态丢失（Tape 缺配对事件，违反单 Tape 唯一真相源）。Option A 修复：`record_resolved` 改同步 `await bus.emit` 写 Tape，async broadcaster 仅留给同步 `resolve()` 入口。6 个 xfail(strict=True) 全转 PASS + 新增 emit-on-closed-bus fail-loud 契约测试。全量 726 passed / 1 skipped / 0 xfailed，0 回归。Commit: `a3ae691`。详见 [release note](../releases/2026-07-02-phase11-interrupt-resolved-fix.md)。

## [2026-07-02] phase 11 P2.2 —— Checkpoint Resume（`orca resume` 崩溃续跑）
SPEC §7：Orca 的 Tape 天生是 checkpoint（append-only JSONL，无需 Conductor 的独立状态序列化系统）。新增 `orca run/resume.py`（typed exceptions + 纯辅助：中段损坏检测/outputs aggregate 重建/parallel mid-crash 检测）+ `Orchestrator.from_tape` classmethod + `run_from_state`（emit `workflow_resumed{from_tape,resumed_node,replayed_events}` 后续跑）+ `_drive_loop` 抽出 `_drive_from(start_node, initial_outputs)` 让 `run()`/`run_from_state()` 共享（DRY）+ `workflow_resumed` 事件类型 + reducer no-op 分类（interrupt_*/prompt_rendered/workflow_resumed）+ CLI `resume` 子命令（参数解析 + 6 种失败模式 → exit code，headless 不启动 TUI）+ LogStream 描述。**code-reviewer 全部反馈闭环**：`_bare_instance` 字段漂移安全网（`_DRIVE_REQUIRED_FIELDS` + `_assert_drive_fields_complete`）/ `_find_first_corrupt_line` position-aware（末尾残行不算 corrupt，from_tape 不依赖调用方先截断）/ fallback 分支测试 / 消除冗余 tape 读（单遍扫描返 valid_count）/ `_inputs_from_tape` 空 inputs warning / Event-schema 损坏测试。parallel 组中间崩溃不支持（SPEC §7 risk，exit 1）。**全量 712 passed / 1 skipped**（基线 697 + 15 新测试，0 回归）。Commit: `0d53eed`。详见 [release note](../releases/2026-07-02-phase11-checkpoint-resume.md)。

## [2026-07-02] phase 11 P1.1 Step B —— mid-run Guidance 注入 + SIGINT + review §2.1 critical 修复
SPEC §4 Step B：RunContext 加 `user_guidance`/`interrupt_history` + `with_guidance`/`guidance_prompt_section`（逐字对齐 Conductor `[User Guidance]` 段）+ render_prompt 拼 guidance section + orchestrator `_make_ctx` 注入累积 guidance（SPEC §10.3 C3：走既有 _make_ctx）+ CLIRunner.send_sigint/was_interrupted + ClaudeExecutor SIGINT 优先判定（emit node_failed{was_interrupted}，不 raise，SPEC §9.5.2 retry 短路前置）+ spawn 前 emit prompt_rendered（preview ≤200 字符，guidance 注入可观测，SPEC §10.2 item3 B5）。**code-reviewer 发现 critical 时序死锁（§2.1）**：Step A 的 action_interrupt「登记 pending + 立即 resolve」连调，但 handler.request 要等 node 边界才注册 future → resolve 落空 + workflow 卡死。修复：CLI 单壳路径 `request_interrupt(ireq, answer=)` + 新 `InterruptHandler.record_resolved`（emit requested + 入队 resolved，不经 await-future）；多壳 await-future 路径保留给 P3。SPEC §11.1 记此偏离。**全量 697 passed / 1 skipped**（Step A 后 674 + 23 新测试，0 回归）。Commit: `01af451`。详见 [release note](../releases/2026-07-02-phase11-guidance-injection.md)。

## [2026-07-01] phase 11 P1.1 Step A —— 优雅中断 UI（InterruptHandler + InterruptModal + Orchestrator wiring）
SPEC §3 Step A：抽出 `orca/gates/_broadcaster_mixin.py`（HumanGateHandler/InterruptHandler 共享 start/stop/_broadcaster，DRY）+ 新增 `InterruptHandler`（request/resolve/first-wins/跨线程 broadcaster emit `interrupt_resolved`）+ `InterruptRequest` 原语 + 3 个新事件类型（interrupt_requested/interrupt_resolved/prompt_rendered）+ `WorkflowAborted` 异常 + Orchestrator `request_interrupt`/`_handle_interrupt`/node 边界 pending 检查（可选注入，None 向后兼容）+ Textual `InterruptModal`（CONTINUE/SKIP/ABORT + guidance textarea + Esc=abort）+ OrcaApp Ctrl+G 绑定 + LogStream format_event。**全量 674 passed / 1 skipped**（基线 652 + 22 新测试，0 回归）。本 commit 同时合入先前未提交的 mxint 端到端实测 bugfix 基线（orchestrator default-fill / app.py on_mount kickoff / log_stream agent_usage / commands.py，见下条），因 Step A 的 `_drive_loop` 改造建立在 mxint default-fill 循环之上、同 hunk 不可分。Commit: `9db57f4`。详见 [release note](../releases/2026-07-01-phase11-interrupt-ui.md)。

## [2026-07-01] phase 11 P0.1 CI —— GitHub Actions 双 workflow（gate + opt-in integration）
新建 `.github/workflows/test.yml`（gate：push/PR(master) → matrix Python 3.10/3.11/3.12 → `uv run pytest -m "not integration"`）+ `.github/workflows/integration.yml`（opt-in：PR comment 含 `/integration` → guard 校验 PR-only + write 权限 + 非 fork PR + API key 非空 → 真 claude E2E）。基线 `uv run pytest tests/ -m "not integration"` = **652 passed / 1 skipped / 37 deselected** 绿。code-reviewer 0 critical，2 major + 2 minor + 2 nit 全闭环（trigger 改 contains / fork 拒绝 / API key fail-loud / timeout-minutes / 注释订正）。Commit: `120085f`。详见 [release note](../releases/2026-07-01-phase11-ci.md)。

## [2026-07-01] 端到端实测 `orca run` 修 3 个真实 bug —— CLI 跑不起来 / inputs.default 缺失 / agent_usage 显示简陋
迁移 AgentHarness 的 mxint-analysis（5 agent 链：analyzer→configurator→runner→diagnostic_saver→report_painter，保骨架换内容无 torch/bitx 依赖）做端到端实测，**首次 `orca run` 撞 3 个真实问题**，全部是 phase 7/5 的功能 gap 且单测零覆盖：(1) **架构 bug**：`commands._run_workflow` 在 `tui.run()` 前调 `kickoff()`，`@work` decorator 需 loop running，撞 `RuntimeError: no running event loop` —— 真实 `orca run` 完全跑不起来；测试 mock 回避故未发现。修：commands 不调 kickoff，挪到 `OrcaApp.on_mount` 末尾（与既有 `_consume_events` 同 pattern）。(2) **功能缺失**：yaml 声明的 `inputs.x.default` 从未被消费（除 `iterations` 特例），render 时 UndefinedError；schema/执行层契约断裂。修：`Orchestrator.__init__` 添加 default 填充循环 + required 缺失 fail loud。(3) **UX 改进**：LogStream `agent_usage` 仅显示字面值，未展示 token 数。修：`format_event` 加 agent_usage case 显示 `usage: in=.. out=.. cache=.. cost=$..`。**实跑验收**：209s 全绿 exit 0，5 个 agent 全部按要求完成结构化输出（schema 100% 匹配），落盘 adapter.py / results.json / diagnostic/*.json / REPORT.md(126 行) 齐全。**tape 完整性 8 项校验全过**：seq 连续无空洞 / 5 个 node 生命周期完整 / tool_call-result 30/30 完美配对 / agent_usage 在 node_completed 前 / workflow 闭环 / tape replay 还原 RunState 全部 5 个 output。**全量回归 683 passed / 0 failed**。反思：phase 7 CLI 壳虽写了 24 个测试但**真实 `orca run` 路径无端到端覆盖**，建议未来每 phase 完成至少跑一次真实 `orca run examples/<demo>.yaml` 作 acceptance 硬条件。Commit: `9db57f4`。详见 [release note](../releases/2026-07-01-e2e-mxint-bugfix.md)。

## [2026-07-01] 阶段 10 iface/mcp 壳（外部 MCP 服务）—— 单进程多壳共存（MCP stdio + Web HTTP 共享 RunManager，gc 启动 assert 保护）+ HandleId 四件套工具（start_workflow / get_task_status / resolve_gate / cancel_task，每 tool 秒级返回规避 CC 60s 超时）+ tape-only query path（pending_gates_from_tape 纯函数派生 + RunManager.run_summary 合并，禁读 handler._pending/_gates_meta，反 AgentHarness 多真相源）+ source="mcp" 复用 handler.resolve（零新 resolve 路径，first-wins + broadcaster 与 Web 同款）+ workflow_cancelled 事件类型（cancel 写 tape 才是唯一真相）+ stdio 每消息 flush（FlushingStdoutWriter 兜底，规避 opencode #21516）+ stdin EOF 双行为（无 --with-web 随 CC 生灭 / 有 --with-web 转 daemon）+ orca mcp 命令（--with-web / --web-port / --max-concurrent / --idle-timeout / --runs-dir）；5 个 E2E 闭环（demo_linear 真 stdio round-trip / 合成 gate + source="mcp" 端到端 / MCP+Web first-wins + 广播写 tape / opencode flush 并发不丢 / 真 claude integration）+ 53 passed 2 skipped（tests/iface/mcp/）+ 652 passed 默认套件零回归 0 warnings；七铁律 grep 全过；6 个透明偏离（emit-before-cancel 顺序 / mcp<1.28 cryptography<49 构建地狱 / 慢 script 替 demo_linear 防 tape close race / 真 RunManager 替 mock 证 HandleId / daemon 60s tick 改 mock 单测 / 加 --runs-dir 测试隔离）；路径 A（CC agent + skill）明确不做留后续。Commit: `4860def`→`ca5ca4b`→`20472b1`→`c26307c`→`2cf5c66`。详见 [release note](../releases/2026-07-01-phase10-mcp.md)。

## [2026-07-01] phase 9 浏览器 E2E 修复 —— SPA fallback(深链 404) + live_server fixture + 测试 bug(run_id/WS/playwright API/async)
phase 9 前端浏览器实测可用但 playwright E2E 套件有测试代码 bug + 一个真实后端 bug：`server.py` 加 SPA fallback（catch-all GET → index.html，修深链 `/runs/<id>` 刷新返回 404 的生产 bug，注册在 API/WS 之后且仅 GET 不吞 `/api/*` `/ws` `/gate`）；4 个测试文件的 `live_server` fixture 端口轮询替代坏掉的 sleep；WS live 推送测试改慢 workflow（sleep 5）+ 三重断言（事件数/run_id 标签/真编排 type）确定性证明 pump 真推送；`test_new_run_form` 修错误的 `run-*` URL 模式为 `demo-*-*`（贴合 `gen_run_id` 真实格式）；`test_cyclic_layout_no_overlap` 修不存在的 `allBoundingBoxes()` → `evaluate_all` getBoundingClientRect；`test_playwright_9d.py` 6 个 async 测试改 sync `def` + `asyncio.run` + chart 测试导航到 RunDetailPage output tab（ChartRenderer 仅在 output tab 挂载，首页注入无组件消费）。验收：playwright E2E **20 passed**（3+6+5+6）、默认套件 599 passed 0 warnings、vitest 84 passed。Commit: `4f891e8`。详见 [release note](../releases/2026-07-01-phase9-browser-e2e-fix.md)。

## [2026-07-01] Tape 写句柄惰性打开 —— 消除 ~30 条 ResourceWarning（root-cause fix）
`orca/events/tape.py::Tape` 写句柄由 `__init__` eager-open 改为首次 `append()` 在 `async with self._lock` 内惰性打开（race-free）+ `close()` 对只读 Tape 幂等 + `__del__` leak 安全网；只读构造（replay/inspect）不再泄漏未关闭的 append handle。顺带修 `tests/gates/test_hook_bridge.py` 9 处 mock server 漏补 `server_close()`（不同根因、同属 ResourceWarning 卫生类、trivial）。验收：`-W "error::ResourceWarning"` 全绿（30→0）、RuntimeWarning 全绿、599 passed 零回归、vitest 84 passed。Commit: `f85bc48`。详见 [release note](../releases/2026-07-01-tape-lazy-open.md)。

## [2026-07-01] 阶段 9d iface/web gate 弹窗 + render_chart —— gate 富交互弹窗（两 source：tool_permission 4 按钮 / agent_ask radio|textarea）全读 store.gate（零本地 gate state）+ 走 backend POST /gate/respond（前端纯 forward 不决策）+ 不乐观更新（答后等 human_decision_resolved 才关，保唯一真相源）+ 三通道竞速广播（别壳先答 → store.gate=null + lastResolved → ResolvedToast「已被 [source] 答」）+ render_chart 迁移 AgentHarness 学术配色 chartTheme（PALETTE 8 色逐字）+ 扁平 record-array spec + 5 种 recharts widget（line/bar/scatter/pareto/table）+ chart 是事件（custom kind=chart 从 store.events filter 无独立通道）+ 同 label+title 替换（实时更新）+ replay 同步（chart ≤ replayPosition）+ hue pivot 共享 helper（DRY）+ ?debug=1 opt-in 调试入口（playwright 集成用，prod 默认不暴露）+ happy-dom 尺寸打桩（recharts ResponsiveContainer 渲染所需）；vitest 84 passed（gate 10 + chart 16 + 既有 58 零回归）+ build 成功 + 595 Python 全绿零回归 0 RuntimeWarning；review 全修复 3 建议（hue pivot 去重 / pareto 前沿线测试 / AskGate selected 重置）+ 1 可选；6 playwright integration。**phase 9 全部子阶段 9a/9b/9c/9d 完成，分支 phase9-web 可合并 master**。Commit: `6d0c5e1`。详见 [release note](../releases/2026-06-30-phase9d-web-gate-chart.md)。

## [2026-07-01] 阶段 9c iface/web DAG 可视化 + tape replay —— ReactFlow 12 + @dagrejs/dagre：拓扑进 workflow_started.data（tape 单一真相源，live+历史 replay 都从事件拿）+ findBackEdges DFS 三色识别回环边（反向喂 dagre，渲染保持原方向）+ 5 种 node widget（Agent/Script/Set/Foreach/End 共享 NodeShell，NODE_STATUS_HEX 5 色）+ WorkflowGraph 三 effect 增量（拓扑全量 build / 节点状态只改变化节点 data 未变保持引用 / route_taken 标记走过边）+ replay setReplayTarget 前进 apply / 后退 checkpoint restore（每 20 事件存 snapshot，enterReplay 建 -1 空态 checkpoint 消除全量重置分支）+ 单路径 fold（replay applyOne 复用 foldEvent 同一 handler 表，反双路径）+ live==replay byte-identical 断言（含 cost/gate/foreach 富流）+ react-window v2 虚拟日志（1000 事件 < 50 DOM row，session 分组）+ NodeDetail + ReplayBar（play/pause/速度 1×-20×）；后端 surgical：lifecycle.make_workflow_started 加 topology 摘要（非破坏）；vitest 58 passed（store 13 + graph 15 + replay 12 + hooks 9 + log-detail 9）+ build 成功 + 595 Python 全绿零回归 0 RuntimeWarning；review 全修复 3 Must-fix（progress 透传 / live==replay 富流断言 / checkpoint-1 消除全量重置）+ 5 Minor + Nit；5 playwright integration。**分支 phase9-web**。Commit: `adc856c`。详见 [release note](../releases/2026-06-30-phase9c-web-dag-replay.md)。

## [2026-07-01] 阶段 9b iface/web 前端骨架 —— React 19 + Vite 6 + TypeScript SPA：react-router v6 BrowserRouter（`/`·`/runs/new`·`/runs/:runId`，navigate push，后退 = 浏览器原生）+ Zustand 单 store（全 src 唯一 create()，immer middleware 锁不可变）+ eventHandlers 表覆盖全部 21 个 EventType（live/replay 共用 processEvent，seq 去重 + last-writer-wins 保证 fold 幂等）+ 懒加载（useRunsList 只轮询 /api/runs 元数据，useRunEvents mount 才拉 /events，unloadRun 清不累积）+ useWebSocket（按需 subscribe + run_id 过滤 + 指数退避重连，重连才全量重拉避免双拉竞态）+ 三页面骨架（RunDetailPage tab 占位 dag/log/output/yaml 给 9c/9d）；TS 类型逐字对齐后端 Event/RunMeta/RunStatus；vitest 22 passed（store 13 + hooks 9，含单 store 正则断言 + fold 幂等显式测试）+ build 到 static/ + 6 playwright integration（后退语义/懒加载网络/URL 直达）；review 全修复（immer / 单一加载路径 / WorkflowStatus 导出 / fail loud / cleanup callbacks / build 产物），n4 双轮询 deferred 9c；594 Python 全绿零回归 0 RuntimeWarning。**分支 phase9-web**。Commit: `0347a66`。详见 [release note](../releases/2026-06-30-phase9b-web-frontend-core.md)。

## [2026-07-01] 阶段 9a iface/web 后端 —— FastAPI（单进程同引擎 uvicorn）+ RunManager 真并发（asyncio.Semaphore 默认 3，每个 run 独立 bus+tape+gate_handler 隔离）+ 懒加载 REST（`/api/runs` 只元数据无 events，事件走 `/api/runs/<id>/events` tape.replay）+ WebSocket 单通道按需订阅（subscribe(run_id) 只推该 run，切 run cancel pump，反向 gate_response）+ 多 run gate 分发（session_id→registry→run_id→handle.gate_handler，复用 phase-6 共享 helper DRY）；五条铁律 grep 全过；review 全修复（shutdown 超时兜底 / EventBus.close 幂等 / has_pending 公开 / N+1 优化 / gate 路由 8 测试补齐）；37 web 单测全绿（0 RuntimeWarning 0 ResourceWarning），594 全量全绿（零回归）。**分支 phase9-web**。Commit: `b34c87d`。详见 [release note](../releases/2026-06-30-phase9a-web-backend.md)。

## [2026-07-01] 阶段 7 iface/cli CLI 壳 —— Textual TUI（DAG 进度 + 流式日志 + gate ModalScreen）+ typer 命令绑定（run/validate/list，parse_inputs 类型推断，退出码 0/1/2）+ OrcaApp @work 编排 worker + _GateHttpBridge（uvicorn 独立线程跑 hook 桥 /gate，socket 预 bind deterministic 就绪）+ GateModal 双 source 渲染（tool_permission/agent_ask）+ 广播输家哨兵；壳无业务真相（事件流驱动渲染）+ 依赖单向铁律（grep 验证）；fold 进 hook_script.py sys.path 阴影 surgical 修复（phase 6 hook 桥 9 测试由此转绿）；79 单测净增，557 全绿（零回归）。**里程碑：Orca 已是可用 CLI 工具**。Commit: `69a905e`。详见 [release note](../releases/2026-06-30-phase7-cli.md)。

## [2026-07-01] 阶段 6 gates/ HMIL 层 —— HumanGate 统一原语（tool_permission + agent_ask 共模型）+ HumanGateHandler（request/resolve + _broadcaster 广播协程）+ PreToolUse hook HTTP 桥（stdlib only，安全优先 exit 2 语义）+ /gate & /gate/respond FastAPI 端点 + SessionContextRegistry（claude session_id → run_id/node 映射）+ ask_user；session_id 透传 event 顶层；36 单元 + 4 integration 测试，478 全绿（+36 净增，零回归）
- commit: `2edcefc`
- 详情：[release note](../releases/2026-06-30-phase6-gates.md)

## [2026-07-01] 阶段 5-R follow-up —— 集合 bug 修复（补 `tests/__init__.py` 让 `tests.run` 可绝对导入，三个 run 测试文件原本 collection 失败）+ code-review 修复（foreach `max_concurrent<1` 编译期 fail loud / `resolve_max_iter` 非法值 fail loud 不静默降级 / 补 parallel+foreach continue_on_error 部分失败聚合透传下游的端到端测试）；442 测试全绿（+7 净增），零回归
- commit: `7bf0f97`
- 详情：[release note](../releases/2026-06-30-phase5-run.md)（§4.1 / §4.2）

## [2026-07-01] 阶段 5-R run/ 编排层 —— Orchestrator 单指针主循环（entry→…→$end）+ Router first-match-wins 纯函数 + ExecutorAdapter（executor AsyncIterator → bus.emit 拆四参桥接）+ parallel 组（asyncio.gather + 幂等 + failure_mode 三态）+ foreach（Semaphore + locals 注入 + 聚合）+ lifecycle（run_id / 生命周期事件 / max_iter）；扩展 RunContext 加 locals/task、ExecError 加 node 字段、validator 允许 inputs/parallel 组名作 Jinja2 root；9 demo 端到端（6 零 token + 3 agent）+ 439 测试全绿（353 基线 + 86 净增，零回归），5 条铁律全过
- commit: `6fa171b`
- 详情：[release note](../releases/2026-06-30-phase5-run.md)

## [2026-06-30] 阶段 5-M schema 单轨化迁移 —— 废除 `Node.after` 双轨制，统一为 routes 单指针 + `ParallelGroup` 显式并行（diamond）；validator 9 项重排（删 ③⑤ after 校验，加 ⑩ parallel 组结构 / ⑪ 兜底 route 位置 / ⑬ entry 非组）；3 examples + 9 fixtures + 3 测试文件全改 + 文档全覆盖；353 测试全绿（323 基线 + 30 净增，零回归），零 after 字段残留
- commit: `f0d7e99`
- 详情：[release note](../releases/2026-06-30-phase5-migration.md)

## [2026-06-30] 阶段 4 exec/ 执行内核 —— Executor 接口（AsyncIterator[Event]）+ ClaudeExecutor（claude -p 子进程 + 真 translator）+ ScriptExecutor / SetExecutor + CLIRunner（asyncio subprocess + stdin pump + 超时 SIGTERM→SIGKILL）+ Jinja2 渲染；3 条架构决策覆盖（translator 归 profiles / seq 占位 / result_extractor 拆半），322 测试全绿（196 基线 + 126 新增，零回归）
- commit: `c891f75`（feat(exec): phase 4 执行内核 — ClaudeExecutor + ScriptExecutor + SetExecutor + CLIRunner + translator 真实现）
- 详情：[release note](../releases/2026-06-30-phase4-exec.md)

## [2026-06-30] 阶段 3 events/ + profiles/ + capability 校验闭环 —— Tape 唯一真相源（append-only JSONL + Lock 覆盖 seq+write+flush + resume 清残行）+ EventBus（异步 fan-out + session_id 透传）+ 幂等 reducer + CliProfile/ProviderCapabilities 命令替换层 + compile `_check_profiles`（⑨），195 测试全绿（103 基线 + 92 新增，零回归）
- commit: `1b86019`（feat(events): phase 3 事件层 + profiles 命令替换层 + capability 校验闭环）
- 详情：[release note](../releases/2026-06-30-phase3-events-profiles.md)

## [2026-06-30] 阶段 2 compile/ 解析校验层 —— YAML→Workflow + 两层校验（结构 pydantic + 语义 8 项 + warnings），103 测试全绿
- commit: `5b5ba06`（feat(compile): phase 2 解析与校验层）
- 详情：[release note](../releases/2026-06-30-phase2-compile.md)

## [2026-06-29] 阶段 1 schema/ 数据层 —— 纯数据结构地基（workflow/event/state），50 测试全绿
- commit: `d69c47c`（实现）+ `6d7dfea`（二次 review 修复：SPEC 25→21 + 测试加固）
- 详情：[release note](../releases/2026-06-29-phase1-schema.md)

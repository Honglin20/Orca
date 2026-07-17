# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

## 当前状态（2026-07-17）：B2（子 agent 过程推送 web）已交付（commit `ed5cbeb`）；SPEC-B v4 全硬约束闭环；前端零改复用 B1。goal 两硬验收（①串台 ②B1 output 显示）+ B2 全交付。**push 待用户手动**（WSL SSH 无 github key，publickey denied，本地领先 46 commits）。详见下「已完成」+ B2 release note。

> **新 session 必读**：本块 + [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) **v5** + [TARS skill release note](../releases/2026-07-15-tars-skill-rebrand.md) + [nas-hp-search 反伪造 release note](../releases/2026-07-16-nas-hp-search-enforce-and-tars-skill-cleanup.md)。teams→tars 改名 + nas-hp-search runner/select 反伪造均已闭环（见下「已完成」+ CHANGELOG）。

**问题（已复现）**：nudge（CC `orca-nudge.sh` Stop-hook / opencode `orca.ts` `session.idle`）在 session idle 时扫活跃 run 提醒推进，但**不区分归属 session** → 任一 session idle 都被任一活跃 run 触发。实测：本 CC session 被 nudge 提醒用户**另一个 session** 的 `agent-struct-exploration` run（串台）。

**目标**：run-id ↔ 启动它的**宿主 session**（CC `session_id` / opencode `session.id`）绑定；nudge 只对**当前 session 自己的**活跃 run 提醒。

**待讨论（确认契约后开 SPEC）**：
1. **归属记录**：`orca <wf> --inputs` 是宿主 bash 子进程、默认拿不到宿主 session id → 需宿主经 env 注入（`ORCA_HOST_SESSION_ID`：CC Stop-hook 输入的 session_id / opencode plugin `session.id`），CLI 写进 run marker + `workflow_started.data`。
2. **nudge 过滤**：hook 取当前 session id，只 nudge `host_session == 当前` 的活跃 run。
3. **跨壳一致**：CC（Stop-hook JSON 输入）vs opencode（plugin ctx）取 id 路径不同，分别接线 + 抽公共；⚠️ 现有 `ORCA_SESSION_ID` 是**每节点 executor uuid（非宿主 session）**，勿混。
4. **边界**：marker 无 session 记录（旧 run / 手 CLI 起的 run）→ 兼容策略（默认 nudge 全部 or 忽略），不破单 session 现有用法。

---

### 2026-07-17 已完成（最新）

- **B2 子 agent 过程推送 web（双 adapter）**（`ed5cbeb`）：SPEC-B **v4** spec-reviewer conditional-pass → 实现：统一 IR `RawAgentEvent`（payload 1:1 = EventType.data，R1）+ 双 read-adapter（CC sidechain jsonl `~/.claude/projects/<enc-cwd>/<host_session>/subagents/` / opencode sqlite event 表 seq 游标，纠 v3 part 表）+ `SidechainIngestor`（1:1 透传 R2 + source_id 进 data.source_id 内存 set 查重 R3 + U1 emit 前增量扫 tape 派生 node §6）+ `sidechain_daemon.py`（detach spawn，复用 `chart_daemon._FlockSafeTape` + `_watch_terminal` 七组件零 DRY；crash callback 重建 ingestor；pidfile + /proc/cmdline liveness probe）。cli.py surgical 接线：bootstrap `_spawn_sidechain_daemon` + next `_ensure_sidechain_daemon`，与 chart 守护并列。**前端零改**（复用 B1 entries.ts:145-201）。硬约束闭环：接口同一性 grep 0 hit + 唯一真相源（agent_* 只经 bus.emit→_FlockSafeTape）+ 无串台（host_session scope）+ U1 per-run ≤0.5s trailing + fail loud（CCAdapterError / OpencodeAdapterError）。防御性 deviation 登记（CC source_id 扩 block_idx；opencode source_id 用 seq 而非 part.id）。code-reviewer 0 🔴 + 5 🟡 全修。79 新测试 + 352 events/in_session 回归全 PASS；e2e subprocess 测试覆盖实时 ≤2s（实测 ~0.5s）/ SIGKILL→respawn 幂等 / 终态自退。**前端构建未跑**（前端零改）；**opencode 真机 spike 未跑**（任务约束，契约实现 + 单测 fixture 覆盖；P2 spike part.id immutability 待补）。详见 [release note](../releases/2026-07-17-subagent-output-b2.md)。

### ~~下一步任务 2~~ ✅ 已完成（2026-07-17）：`orca list` 瘦身 + schema 移启动命令

见 CHANGELOG / [release note](../releases/2026-07-17-orca-list-slim-schema-via-start-cmd.md)。**实际方案 ≠ 原登记的选项 1（describe）**，用户拍板第 4 条：零新命令——`orca list` 砍 schema 只返 `{name,description}`，inputs_schema 改由 `orca <wf>` 不带 `--inputs` 按需带出。命令数 7 / 保留字 / CI 禁 describe 全不变。

### 2026-07-16 已完成（最新）

- **nas-hp-search runner/select 反伪造 + output_schema 强制**（`<SHA>`）：修「假执行」bug（tape 证 runner/select/train_script_gen 没跑脚本、只复述上游散文；根因 prompt 诱骗 + 无强制）。`nas-train-runner/agent.md` 重写（执行置顶 + 删上游散文改用 `{{ inputs.output_dir }}` + 反伪造 + 末尾 python 从真 search.jsonl 计数输出自校验 JSON）；`nas-select/agent.md` 去诱骗；`nas-hp-search.yaml` runner 加 `output_schema`（`search_records≥1`，in-session `_parse_output` 确定性强制：散文/0记录→`node_failed`，不真跑过不了）。共享 agent 契约：须显式传 output_dir。验证脚手架（FAST/MOCK）剔除不进生产。E2E 两次通过（opencode+flash+脚手架绕 deepseek 慢）。`search_pipeline_gen` 在 deepseek 慢时卡死是独立问题（nas-search-pipeline 重校验设计），非本次范围。详见 [release note](../releases/2026-07-16-nas-hp-search-enforce-and-tars-skill-cleanup.md)。
- **tars install skill 改名清理 + CLAUDE.md「TARS 是 SKILL」**（`<SHA>`）：CC 装的 skill 名陈旧为 `orca`（改名前残留）→重装正名 `tars`；`install_cmds._install_skill` 加改名清理（install 自动清陈旧 `skills/orca|teams/`，同 command/orca pattern）+ docstring 修；`CLAUDE.md` 加「TARS 是 SKILL 不是 CLI」注记。`orca doctor` skill_install PASS(cc,opencode)。
- **nas-hp-search 轻量 NAS 流水线（slim）**（`a5dd2cc`）：新增 5 节点 workflow `nas-hp-search.yaml`（model_optimizer→train_script_gen→search_pipeline_gen→runner→select）——重 pipeline 的轻量版。新 slim folder-agent `elastic_optimizer`（只读 model+速查+模板，不展平/不读 optimize_rules）+ 新脚本化 `nas-select`（零 LLM，替代 evaluator）+ 复用 `supernet-train-script` checklist 加 `[MAJOR] 28`（train_supernet.py 内联 `_push_chart()`，无独立 viz 节点）。节点名 `model_optimizer`（agent→`elastic_optimizer`）对齐复用 agent body 硬契约。附 `.gitignore` 修（`references/`→`/references/`，解 folder-agent skill 资源被误伤）。`tars validate` 0 error；template 自测过；端到端 EXIT=0 SELECTED=3。**与 in-session 工作流正交**（NAS 侧独立交付）。详见 [release note](../releases/2026-07-16-nas-hp-search-slim.md)。
- **in-session chart 守护 respawn**（`<本 commit>`）：补 [chart 接入](../releases/2026-07-16-in-session-chart.md) 的缺口 —— 守护只在 bootstrap spawn 一次，run 中途被杀（`pkill opencode` 误伤）后 `orca next` 不 respawn → 后续 `render_chart` 连不上 socket、chart 全丢（实测一次 run 0 chart）。`next` 路径补 `_chart_daemon_alive`（确定性 socket connect 探测，不靠进程名 grep）+ `_ensure_chart_daemon`（tape flock 临界区内 probe + 复用 `_spawn_chart_daemon` respawn）；`_wait_for_sock` 从 `exists()` 加强为 connect 探（修 respawn 路径 stale socket 假阳性）；调用点守卫与 env 写对齐（`result.node is not None`）；spawn 失败降级 warn。+7 测试（SIGKILL→respawn→chart 落 tape e2e + 负向守卫）；158 in-session 测试 0 新回归；code-reviewer impl+coverage 两轮 0 🔴（🟡 全修）。详见 [release note](../releases/2026-07-16-in-session-chart-respawn.md)。
- **in-session 路径接入 chart（render_chart）**（`<本 commit>`）：补 in-session skill 驱动路径的 chart 缺口。web/tars-run 路径下 ClaudeExecutor spawn 时注入 `ORCA_*` env + 起 per-run ingestor（同进程）；in-session 路径下节点子代理由宿主 session 派发不经 executor → env 缺、ingestor 没人起 → `render_chart` raise。三件套：① bootstrap detach 起守护进程 bind socket + 跑 `chart_ingestor`（复用零改动；`_FlockSafeTape` 子类加跨进程 flock + 增量 disk max-seq 刷新）；② `runs/<run_id>/orca_env.sh` per-node env 文件（5 var：4 chart + `ORCA_AGENT_RESOURCES`，folder-agent 资源定位缺口同补）；③ 节点 prompt 指针加 `source <env>` 行。守护 `_watch_terminal` 监听终态事件自退 + 6h TTL 兜底；partial-line race 防护（`last_size` 仅推进到最后 `\n`）。24 新测试；710 in-session+chart+events+exec 测试 0 新回归；code-reviewer 两轮 0 🔴（R1 1 🔴 partial-line race + 5 🟡 全修；R2 0 🔴 0 🟡）。详见 [release note](../releases/2026-07-16-in-session-chart.md)。
- **teams→tars 后端改名**（`<本 commit>`）：`pyproject` 入口 + `DEFAULT_BACKEND_CMD` + `validator` 保留字 + help/docstring + 用户面消息（orca epilog/doctor/skill 弃用警告）+ shipped 产物（cc_nudge.sh / create-workflow SKILL.md / templates / skills）+ `examples/mxint_analysis.yaml` 注释。`teams_app` deprecated 别名保留（向后兼容）；`orca` in-session 不动；`ORCA_BACKEND_CMD` env 名不变。重装后 `tars` 上 PATH / `teams` 退场。768 单测 0 回归；code-reviewer 两轮 0 🔴（全修）。详见 [release note](../releases/2026-07-16-teams-to-tars-rename.md)。
- **TARS rebrand / step 6 / 批量 FU / step 5b / step 3b / step 5a / defects / step 4 / step 2b / step 1**：见 CHANGELOG 索引。

### 待办（用户侧真机，无代码；§9 跨平台）

- **tars 真机**：`tars install --target cc` → `.claude/skills/tars/SKILL.md` 真生成；`tars --help` / `tars list` / `tars validate` 真工作；`orca` 命令不受影响（`teams` 已退场）；`orca doctor` skill_install pass。**纯 CLI 禁 MCP**。
- **§9#1 nga/cac 全套集成真机加载**：CAC/NGA 是否真读 `.cac`/`.nga`；cac Stop-hook / nga `opencode.json` plugin 是否真生效。

### follow-up / debt（用户暂缓 / 预存，非阻塞）

- ~~**既有测试隔离缺陷**~~ ✅ 已解（2026-07-17 orca list 瘦身）：原 `test_orca_list_returns_inputs_schema_json` 重写为 `test_orca_list_returns_name_and_description_only`，按名定位不再 `assert len==1`，`~/.orca/workflows` 全局 wf 不再干扰。
- **既有 `orca/iface/in_session/daemon.py:105` 裸 `sys.exit(128 + signum)`**（违反 SPEC §3.3 grep 守门）：本任务 chart_daemon.py 已用 `loop.add_signal_handler + asyncio.Event` 避免同款违规，但老 daemon.py 仍裸 `sys.exit` 致 `test_no_bare_sys_exit_or_raise_system_exit_outside_allowed_paths` baseline 即失败。择机按 chart_daemon.py 同款 pattern 修。
- **既有 `tests/e2e_phase13/test_e2e_1_basic_chart.py` + `test_e2e_2_multi_run_parallel.py` baseline 失败**：YAML 硬编码 `python3`，本机 `python3` 未装 orca → `render_chart` import 失败。CI 或装了 orca 的环境无此问题。择机把 e2e wf 的 `python3` 改为 `${ORCA_PYTHON:-python3}` 或测试 fixture 注入 `sys.executable`。
- **既有 `tests/iface/mcp/test_*` baseline 失败**：环境缺 `uv` 二进制（FileNotFoundError）。非本任务引入。
- **既有 `test_bg_run_ps_logs_wait_e2e` rot**：`orca run --background` 选项不存在（in-session CLI 无 run）。择机修或删。
- **MCP 移除**：用户暂不移除（spec v5 §8 留 MCP 8 tool 出 scope）。
- **`in-session-unified-backend-draft.md`**：推迟架构草稿，仍含 `teams` 残留（YAGNI，启用时再改）。
- `_load_wf_for_run` 的 `catalog.find_workflow` fallback 无测试触达（step 3b 预存）。
- tape `workflow_failed.data.kind` 是 `ErrorKind`/`error_kind` 两值集共享字段（跨阶段 debt，5b 登记）。

---

## 跨阶段其他待立项（与 in-session 正交，不影响当前）

- **三壳统一 ADR**（[`2026-07-08-shell-unification-adr.md`](../specs/2026-07-08-shell-unification-adr.md)）：单一读路径 + 渲染契约 + 视觉，待 spec-review。
- **agent interrupt**（[`agent-interrupt-design-draft.md`](../specs/agent-interrupt-design-draft.md)）：mid-stream cancel+resume，待立项 SPEC。
- **render layer v1.5**（codex 接入）/ **v2**（Web TS 镜像 + 流式 shiki + diff 虚拟化）。
- **TUI fold DRY**：fold 字段抽 `orca/run/projections.py`。
- **phase-16 批 2**：本地包分发 + workspace-instruction。

## 必读文件（下一任务开工前按需）

- [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5
- [teams→tars release note](../releases/2026-07-16-teams-to-tars-rename.md)
- [CHANGELOG](CHANGELOG.md)

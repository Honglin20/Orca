# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

## 当前状态（2026-07-17）：goal 两硬验收完成（①串台 ②B1 output 显示，均交付 + E2E PASS）；B2（过程推送）命门已解（spike A 路 = CC PostToolUse hook `agentId`），SPEC-B v2 修订完成待实现（工作量评估见对话）；**push 待用户手动**（WSL SSH 无 github key，publickey denied，本地领先 45 commits）。详见下「已完成」+ 三 release note

> **新 session 必读**：本块 + [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) **v5** + [TARS skill release note](../releases/2026-07-15-tars-skill-rebrand.md) + [nas-hp-search 反伪造 release note](../releases/2026-07-16-nas-hp-search-enforce-and-tars-skill-cleanup.md)。teams→tars 改名 + nas-hp-search runner/select 反伪造均已闭环（见下「已完成」+ CHANGELOG）。

**问题（已复现）**：nudge（CC `orca-nudge.sh` Stop-hook / opencode `orca.ts` `session.idle`）在 session idle 时扫活跃 run 提醒推进，但**不区分归属 session** → 任一 session idle 都被任一活跃 run 触发。实测：本 CC session 被 nudge 提醒用户**另一个 session** 的 `agent-struct-exploration` run（串台）。

**目标**：run-id ↔ 启动它的**宿主 session**（CC `session_id` / opencode `session.id`）绑定；nudge 只对**当前 session 自己的**活跃 run 提醒。

**待讨论（确认契约后开 SPEC）**：
1. **归属记录**：`orca <wf> --inputs` 是宿主 bash 子进程、默认拿不到宿主 session id → 需宿主经 env 注入（`ORCA_HOST_SESSION_ID`：CC Stop-hook 输入的 session_id / opencode plugin `session.id`），CLI 写进 run marker + `workflow_started.data`。
2. **nudge 过滤**：hook 取当前 session id，只 nudge `host_session == 当前` 的活跃 run。
3. **跨壳一致**：CC（Stop-hook JSON 输入）vs opencode（plugin ctx）取 id 路径不同，分别接线 + 抽公共；⚠️ 现有 `ORCA_SESSION_ID` 是**每节点 executor uuid（非宿主 session）**，勿混。
4. **边界**：marker 无 session 记录（旧 run / 手 CLI 起的 run）→ 兼容策略（默认 nudge 全部 or 忽略），不破单 session 现有用法。

---

### 下一步任务 2（待立项）：`orca list` 输出精简（catalog 增长后单命令 dump 过重）

**问题**：`orca list` 单命令返全部 wf 的 full description + **全部** inputs_schema（实测 ~5KB，`agent-struct-exploration` 的 **19 个 input** 占大头）。TARS skill 据 description 匹配意图时，未选中 wf 的 inputs 全是噪声上下文。

**v5 现决**：刻意**单命令无 describe**（"选 wf + 知 inputs" 一条命令搞定，避冗余；见 `orca list --help`）。

**待讨论**：catalog 还会涨（struct 系列 + 未来 wf），单命令 dump 成本上行。选项：
1. **反转 v5**：加 `orca describe <wf>`（单 wf 全 inputs_schema）+ `orca list` 瘦身（name + 短 desc，不带/少带 inputs_schema）。TARS：list 匹配 → describe 抽 inputs。**彻底**，代价：维护两命令 + 同步改 TARS skill 流程。
2. **`orca list --short`**（name + 一行 desc，无 inputs，匹配用）/ 默认全量（抽 inputs 用）——不新增命令、不反转 v5，仅加 flag。**但只省"匹配"步骤噪声**：TARS 抽 inputs 仍需 full list（dump 全部 wf inputs），除非配 `describe`，故单独用不彻底。
3. 保留单命令 + 截断（inputs desc 限长 / wf desc 限长）——简单但治标。
**倾向**：要彻底省上下文 → 选项 1（describe）；想最小改动且接受"inputs 抽取仍全量" → 选项 2。

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

- **既有测试隔离缺陷**（非本任务引入，code-reviewer R1 🟢 登记）：`test_orca_list_returns_inputs_schema_json` 未隔离 `~/.orca/workflows` user-level 扫描根，全局有 wf 时 `assert len==1` 失败。择机 monkeypatch `Path.home` 修。
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

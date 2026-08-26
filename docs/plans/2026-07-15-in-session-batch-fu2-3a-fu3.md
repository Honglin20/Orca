# Plan: 批量闭环 FU-2 + 3a + FU-3（低复杂度合并）

> 用户指示（2026-07-15）：工作量/复杂度低的 step 合并做。本批合并 3 个独立小 follow-up，走**一套** plan→spec-reviewer→coder-agent→test-agent。
> 分支 `in-session-unified-backend` | 前置：5b `6d76a19`（3a 补 error_kind 依赖它）+ FU-1 `73a47ea`
> **本批取代** standalone `docs/plans/2026-07-15-in-session-fu3-status-noarg-active.md`（FU-3 并入此批）

---

## 0. 批量目标

| 项 | 一句话 | 复杂度 | 文件 |
|---|---|---|---|
| **FU-3** | `orca status`（无参）对齐 SPEC：只列**活跃** run（marker）+ 结构化 `{run_id,node,status,last_next_at,elapsed}` | 中 | cli.py status |
| **FU-2** | doctor 删 `entry_hook` check（transform 退场后心跳永不再写，check 永久 unknown，dead）| 低 | cli.py doctor |
| **3a** | SKILL.md 失败处理补 `error_kind` 一句（5b 信封加字段后）| 低 | SKILL.md |

三者独立、皆 in-session CLI/skill 层、无跨层依赖。一个 coder-agent 顺序做，一个 test-agent 统验（**纯 CLI，禁 MCP**）。

---

## 1. FU-3：status 无参 → 活跃 + 结构化

### 根因（cli.py:719-737）
`glob("*.jsonl")` 列**所有** tape（含 completed）+ 返**裸 stem** `{runs:[stem]}`，违 docstring(L713「活跃」) + SPEC §2.1/§2.3。

### 改动
- 活跃枚举：`markers = sorted(runs_dir.glob("orca-*.json"))` → 派生 run_id。
- 每活跃 run：`replay_state(tape)` 取 `run_id`/`state.status`/`state.current_node`/`node_status`（**RunState 零时间字段**，见 `schema/state.py:49-64`）；时间字段用 **tape 事件 `Event.timestamp`**（`schema/event.py:101`，经 `tape.replay()` 取**末事件 ts = `last_next_at`**）。`elapsed` 语义 = **距上次活动**（`now - 末事件 ts`，对齐 SPEC §1 nudge 卡住兜底）。marker mtime 仅兜底近似（每次 next RMW 回写，≈ 上次 next）。**不新增时间追踪，不编造。**（spec-reviewer #1）
- `--json`：`{"runs":[{run_id,node,status,last_next_at,elapsed},...]}`；人类可读：`- <run_id> [status] node=… elapsed=…`。
- 无活跃 run → `{"runs":[]}`（`--json`）/ `(无活跃 run)`（人类可读）exit 0。**shape 与非空一致**（消费方恒读 `reply["runs"]`，spec-reviewer #5）。
- **completed（无 marker）不列**（修复核心）。

### 架构
- 单事实源：marker 存在 = 活跃（已是完成契约：bootstrap 写 / completed·stop 清，5a/FU-1 验）。复用，不新增活跃态。
- 修 bug 非 breaking：SPEC/docstring 一直承诺活跃+结构化，旧代码没兑现。

---

## 2. FU-2：doctor 删 entry_hook check

### 根因（cli.py:940-958）
`entry_hook` check 探 `PROBE_ENTRY_REL` 心跳，证 transform plugin 入口活着。但 **v5 step 2b transform 派发已禁用 + step 4 整删 orca.ts transform**（5a/step4 落地）→ 心跳**永不再写** → check 永久 unknown/无意义（CURRENT step4 follow-up 明记）。dead check，删。

### 改动
- 删 ④ entry_hook check 块（L940-958）**+ 连带死代码**（spec-reviewer #2）：`PROBE_ENTRY_NAME` 常量（L71）+ `entry = _read_probe(PROBE_ENTRY_NAME)`（L881，删后死变量）+ 报告心跳路径行（L988，列永不会产生的文件，误导）。
- doctor checks 5 项 → **4 项**（skill_install / cli_imports / diag_switch / advance_hook）。
- `ok` 计算（L977-978，仅 hard=True 计数）不受影响（entry_hook 本就 hard=False）。

### 架构 / scope
- 删 dead 诊断，零行为变化（hard=False 从不计入 ok）。
- **advance_hook（⑤）不动**：它**不 dead**（spec-reviewer #4 实读）——orca.ts idle nudge hook 按 SPEC §4.4 保留（`orca.ts:37/60/156` 仍写 `PROBE_ADVANCE`），check 仍验证 session.idle 接线，保留。（entry_hook 之所以 dead：step 4 整删 transform → `PROBE_ENTRY_REL` 永不再写；advance 走 idle hook 未删。）
- doctor 测试同步（spec-reviewer #3，逐处）：`test_in_session_v8.py`：
  - `test_doctor_json_structure`（L97）：len 5→4、names 去 entry_hook、hard_expected 去 entry_hook。
  - 删 3 个纯 entry_hook 测试：`test_doctor_diag_on_no_heartbeat_entry_unknown`（L177）/ `test_doctor_fresh_entry_heartbeat_passes`（L192）/ `test_doctor_stale_entry_heartbeat_unknown`（L211）。
  - `test_doctor_diag_off_hook_checks_unknown_ok_unaffected`（L164）：去 entry 断言行。
  - `test_doctor_report_describes_b_path`（L244）：去 `.orca-probe-entry.json` 路径断言。
  - `test_v3_step1.test_reserved_command_name_not_treated_as_wf`（L126）：无需改。

---

## 3. 3a：SKILL.md 失败处理补 error_kind

### 根因
5b 给 in-session 失败信封加 `error_kind` 字段（`InSessionError.error_kind` taxonomy：output_schema_mismatch/state_corrupt/...）。SKILL.md 失败处理段（L129-136）只教读 `reason`，未提 `error_kind`。

### 改动（小，SKILL.md L129-136 失败处理段）
- 补一句：失败信封除 `reason` 还带 `error_kind`（如 `output_schema_mismatch`/`state_corrupt`），可据它给用户更精确的失败归类（**增强，非必需**——reason 仍可用）。
- 同步已装副本 `~/.claude/skills/orca/SKILL.md`（核实 teams install 是否自动同步；若否手动 cp 或记 follow-up）。

### 架构
- 纯 skill 文档增强，对齐已落地的 5b 信封契约。无源码改。

---

## 4. 测试（纯 CLI，禁 MCP；合并验）

### 单测
- **FU-3**：status 无参 0/1/N 活跃 + completed-不列 + `--json` 结构化 shape（每元素 dict 含 run_id/node/status/last_next_at/elapsed **五键**，非裸串）；强化 `test_status_json_flag_no_run_id_lists_runs_json`（test_v8 L495，spec-reviewer #6）。
- **FU-2**：doctor 返 4 项 checks（无 entry_hook）；ok 计算不受影响。
- **3a**：SKILL.md grep `error_kind` 命中（守门）；静态守门仍过（无业务关键词）。

### E2E（test-agent 真机，纯 orca CLI）
- **FU-3**：bootstrap 活跃 run → `orca status`（无参）→ 列出（结构化）；推进到 done → 不再列。
- **FU-2**：`orca doctor` → 4 项 checks，无 entry_hook，ok 正常。
- **3a**：读 SKILL.md 确认 error_kind 一句在（+ 已装副本同步）。

---

## 5. 风险 / scope

- **FU-3 时间字段**（R1）：last_next_at/elapsed sourcing 用 marker mtime 近似 或 replay_state 字段；不编造，surface 决策。
- **FU-3 旧消费**（R2）：grep `orca status` 无参的断言消费点同步改。
- **scope**：3 项各自范围紧（status 无参 / entry_hook 删 / skill 句）。不动 status --run-id / marker / replay / advance_hook / bootstrap-stop / MCP。不重写。
- 单 commit 含 3 项（+ release note + CHANGELOG + CURRENT + 本计划）。

---

## 流程闭环
本计划（3 项合并）→ **spec-reviewer**（一次评审 3 项：FU-3 活跃判定+时间字段 / FU-2 entry_hook dead 确认+advance_hook 不动 / 3a scope）→ **coder-agent**（一次实现 3 项 + code-reviewer + 单测 + commit + 状态文档）→ **test-agent**（一次真机统验 3 项，纯 CLI）。6（teams nga/cac）独立另做。

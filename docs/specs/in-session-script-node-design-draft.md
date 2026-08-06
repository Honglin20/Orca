# in-session Script 节点支持 design-draft

> 状态：草稿（design-draft，跨阶段设计议题）。各 phase SPEC 撰写前必读。
> 动机来源：议题1「workflow 被主 session 误停」根因深挖——`continue_loop` 被 LLM 覆盖（`struct-curator/agent.md` 让 agent 产出 `continue_loop`，router 盲信字段值），治本需把确定性判定从 agent 节点解放为独立 script 节点，而 in-session 当前不支持 script。
> 日期：2026-07-27。

---

## 0. TL;DR

- **现状**：in-session per-call CLI（`orca next`）只支持 agent 节点（`orca/run/step.py:558 _check_agent_node` 硬卡）。所有确定性逻辑（reducer / measure / viz）只能塞进 agent 节点 prompt 让子 agent（LLM）"调脚本 + dumb copy 结果"，受 LLM 干扰——`continue_loop` 被覆盖就是症状。
- **目标**：让 in-session 支持 `kind: script` 节点。引擎在**一次 `orca next` 内自动连续消化 script 节点链**，停在"下一个 agent 节点"或"done"。确定性逻辑从 agent 节点解放，LLM 只干判断活。
- **首个使用案例**：`agent-struct-exploration` 加 `goal_check`（script）gate 节点，治"5 轮伪造 `continue_loop=false` 直进 finalize"（见 §12）。
- **工作量**：1–1.5 天含测试（属架构改动：in-session 执行模型从"纯宿主"变"宿主+引擎混合"，走 SDD）。

---

## 1. 现状基线（为什么现在不支持）

### 1.1 in-session 执行链（per-call CLI，主 UX 路径）

```
主 session: orca next --run-id X --output '<产出>'
  → cli.next()  [orca/iface/in_session/cli.py:1283]
      抢 flock（LOCK_NB，busy → 0 退出）[cli.py:1313]
      asyncio.run(_next_in_critical_section)  [cli.py:1328]  ← 临界区
        advance_step(tape, wf, output)  [orca/run/step.py:422]  ← 纯决策
        apply_step_result(bus, result)  [orca/iface/in_session/_step_io.py:94]  ← emit_batch 落 tape
      拼 reply JSON（done/node/prompt/recoverable）→ echo
      释放 flock
```

- **advance_step 是纯决策**：读 tape → 算下一节点 → 返 `StepResult`；不 IO、不写 tape、不跑命令（`step.py` 头注释明示"IO 由调用方 daemon 执行"）。
- **`_check_agent_node`**（`step.py:558`）在 entry 分支（`:487`）和 next 分支（`:532`）硬卡 `node.kind == "agent"`，否则 `unsupported_node_kind`。
- daemon 路径（`daemon.py:117 next`）也走 `advance_step`，主 UX 是 per-call CLI（`daemon.py` 头注释：daemon 仅无头 CI 形态）。

### 1.2 ScriptExecutor 现成（headless 用）

`orca/exec/script.py:54 ScriptExecutor`：
- `exec(node, ctx)` async generator：yield `node_started` → subprocess 跑 `render_command` → yield `node_completed(output)`（`script.py:77-170`）。
- **进程管理**：`ProcessRegistry.acquire/release` + `spawn_kwargs_for_process_group`（POSIX `start_new_session` / Windows 进程组），cancel 时整组杀防孤儿（`script.py:109-157`）。
- **env overlay**：`_build_spawn_env` 注入 `ORCA_RUN_ID/NODE/SESSION_ID/CHART_SOCK/ARTIFACTS_DIR/KB_DIR`（`script.py:244-269`），script 内 `orca.chart.render_chart` 据身份路由推图。
- **失败语义**：
  - timeout → `ExecError(phase="timeout")`，fail loud（`script.py:134-152`）。
  - 非零退出码 → **不 fail loud**（业务结果，由路由判断，`script.py:159` 注释）。
  - `parse_json` 失败 → `output["json"]=None`（降级，不阻断，`script.py:165-167`）。
- `make_executor`（`orca/exec/factory.py:101`）已分派 `ScriptNode → ScriptExecutor(runs_dir=...)`。

### 1.3 不需改的

- **compile validator**：grep 确认 compile 层**不挡** script（只 `catalog.py` 删 setup phase，无 in-session kind 限制）。仅 step.py 运行时挡。
- **cli 已 async**：`cli.py:1328` 已 `asyncio.run`，可直接 `await ScriptExecutor.exec`。

---

## 2. 整体运行逻辑（核心契约）

**一次 `orca next` 内，引擎自动连续消化 script 节点链，停在 agent 节点或 done。**

```
主 session: orca next --run-id X --output '<agent 产出>'
  │ cli 抢 flock + 进临界区
  ▼
  ┌─ loop（_next_in_critical_section 内新增）─────────────────────────┐
  │ result = advance_step(tape, wf, output)                          │
  │   ├─ result.done=true        → 返 done（workflow 完成）           │
  │   ├─ result.node_kind=agent  → 返 prompt/prompt_file 给主 session → 出 loop │
  │   └─ result.node_kind=script → 内联执行：                         │
  │        executor = ScriptExecutor(runs_dir)                       │
  │        async for ev in executor.exec(node, ctx):                 │
  │            emit(ev)  ← node_started/node_completed 落 tape        │
  │        output = script_output（output.json or raw）               │
  │        → 回 loop 顶（带 script output 继续 advance_step）          │
  └──────────────────────────────────────────────────────────────────┘
  │ 释放 flock + echo reply（agent prompt 或 done）
  ▼
主 session: 拿 agent prompt → 派子 agent → 下一次 next
```

**时序例**（workflow：`setup(agent) → goal_check(script) → finalize(agent)`）：
- 主 session `next --output '<setup 产出>'` → advance 算出 next=goal_check(script) → 引擎内联跑 goal_check → 拿 `goal_met` → 继续推进 → next=finalize(agent) → **返 finalize prompt**。主 session 看到的是"setup 后直接到 finalize"，goal_check 在引擎透明消化。

### 2.1 设计原则（分层）

- **step.py 保持纯决策**：只决定"下一节点是谁、什么 kind"，不跑命令。`advance_step` 对 script 节点**跳过 `_deliver`（不写 prompt 文件，script 无 prompt）**，返 `StepResult(node, node_kind="script")`。
- **执行在 iface 层**：cli `_next_in_critical_section` 与 daemon `next` 共享一个 helper（`run_inline_scripts`）调 ScriptExecutor。符合依赖方向 `schema→compile→exec→run→iface`（iface 调 exec 合法，run 不反向调 exec）。

---

## 3. 分层改动

| 层 | 文件 | 改动 |
|---|---|---|
| 决策 | `orca/run/step.py` | (a) `_check_agent_node` 放开 `script` kind（或新增 `_check_executable_node` 允许 agent+script）。(b) `advance_step` 对 script 节点跳过 `_deliver`（不写 prompt 文件），`StepResult` 加 `node_kind` 字段、`node_kind="script"` 时 `prompt/prompt_file=None`。(c) 首次启动 entry 若是 script 也走内联（entry=script 合法）。 |
| 共享 IO | `orca/iface/in_session/_step_io.py`（或新模块 `_inline_script.py`） | 新增 `async run_inline_scripts(bus, tape, wf, result, *, runs_dir, ...) -> script_output`：若 `result.node_kind=="script"` → `make_executor(node, runs_dir=runs_dir)` → `async for ev in executor.exec(node, ctx): await bus.emit(ev)` → 收集 output（`output.json` 优先，否则 raw stdout/exit_code dict）→ 返。cli+daemon 共用（DRY，parity 守门）。 |
| cli | `orca/iface/in_session/cli.py:_next_in_critical_section` | 拿到 `result` 后加 `while result.node and result.node_kind == "script" and not result.done:` 循环：`script_out = await run_inline_scripts(...)` → 把 script 产出过 `_parse_output`（若声明 output_schema）→ `advance_step(tape, wf, output=script_out)` 续推 → 直到 agent / done / 失败。 |
| daemon | `orca/iface/in_session/daemon.py:next` | 同样接 `run_inline_scripts` 循环（与 cli parity；daemon 无 marker/compliance，循环体更简）。 |
| compile | 无 | 不改（不挡 script）。 |

**StepResult 新字段**（`step.py:112`）：
```
node_kind: str | None = None   # "agent" | "script"（None 兼容旧调用）
```
> Q4 待定：是 step 显式返 `node_kind`，还是 cli 层自查 `wf.nodes[result.node].kind`。倾向前者（step 决策显式，cli 不必再查 schema）。

---

## 4. 错误映射（script 失败 → in-session taxonomy）

| script 情况 | ScriptExecutor 行为 | in-session 映射 | 信封字段 |
|---|---|---|---|
| exit=0 | yield node_completed | 继续 advance_step(output) | 正常推进 |
| **非零退出码** | 不 fail loud | output 含 `exit_code`，由下游路由 `when: exit_code==0` 判断 | 正常推进（路由决定） |
| **timeout** | `ExecError(phase=timeout)` | 捕获 → `InSessionError(error_kind=script_timeout)` → `fail_in_session` | `done:true, error_kind:script_timeout`，终态 |
| **spawn 失败** | `ExecError(phase=spawn)` | → `InSessionError(internal_error)` → 终态 | `error_kind:internal_error` |
| parse_json 失败 | `output.json=None` | 若 output_schema 要求 json → fail loud；否则降级继续 | 看 schema |
| **output 不合 output_schema** | —— | **终态 fail loud**（`output_schema_mismatch`，**不 recoverable**） | `done:true, error_kind:output_schema_mismatch` |

### 4.1 关键区分（fail loud 原则）

- **agent 节点**产出不合 schema = `recoverable`（重派子 agent 可能修对，`step.py:84 RecoverableInSessionError`）。
- **script 节点**产出不合 schema = **终态 fail loud**（script 确定性，重跑结果不变；要么脚本/契约写错，要么上游 output 缺字段——都该让用户修，不该静默重试）。
- 新 error_kind 常量（`step.py:61` 区段）：`ERR_SCRIPT_TIMEOUT = "script_timeout"`。

---

## 5. 事件 / Tape 对齐

- script 的 `node_started`/`node_completed` 经 `run_inline_scripts` 的 `bus.emit` 落 tape，序列须与 headless `drive_loop` 跑同一 workflow 的 tape **逐 seq 对齐**（每节点 `ns → nc → rt → ns(next)`）。
- **G2 回归守门**：daemon/cli 跑某 wf 的 tape vs `orca run`（headless）跑同 wf 的 tape，`(type, node, 关键字段)` 必须一致（`step.py` 头注释既定红线）。新增测试覆盖 script 节点。
- script 的 emit 不走 `apply_step_result.emit_batch`（那是 advance_step 的 emits），而是 `run_inline_scripts` 内逐条 `bus.emit`（executor 是 async generator，逐条 yield）。需评估：是否攒批后 `emit_batch` 原子化（避免 SIGTERM 半截 tape，对齐 `daemon.py:117` 注释的"反逐条 emit"原则）。**倾向**：script 内联段单独 emit_batch 一次（ns+nc 一批），与 advance_step 的 emits 分两批但都在同一 flock 临界区内。

---

## 6. 后端管理（进程 / env / 产物 / cancel）

- **进程注册 + 进程组**：复用 `ScriptExecutor` 的 `registry.acquire/release` + `spawn_kwargs_for_process_group`。in-session 下同一 `ProcessRegistry` 单例（`get_default_registry()`），atexit/SIGTERM 清理照旧。
- **env overlay**：`_build_spawn_env` 注入 `ORCA_CHART_SOCK/ARTIFACTS_DIR/KB_DIR` 等。cli 须传 `runs_dir`（从 `tape_path.parent` 推，同 headless `factory.py:80`）。
- **chart_daemon / sidechain_daemon**：`cli.py:1345 _ensure_chart_daemon` 已在 next 里 respawn；script 内 `orca.chart.render_chart` 推图经既有 socket → ingestor → tape，零额外改动。
- **cancel**：headless 下 `InterruptHandler` + `registry.kill_one` 能 cancel 正跑的 script。in-session per-call 下，`orca next` 阻塞在 `subprocess.communicate`，主 session cancel = kill 整个 `orca next` 进程（SIGTERM）→ registry 的 SIGTERM/atexit handler `kill_one` 整组杀，无孤儿。可接受（per-call 模式 cancel=kill 调用）。

---

## 7. 长跑阻塞（已知约束，非本 spec 解决）

- `orca next` 阻塞到 script 跑完（`subprocess.communicate`）。struct 训练命令几分钟 → next 阻塞几分钟。
- **现状对比**：训练现在塞 agent 节点 bash 里（子 agent 跑 bash）同样阻塞——script 不更糟，反而少一层 LLM。
- **本 spec 范围内接受同步阻塞**：script `timeout` 字段兜底 + 主 session 用长超时 Bash 调用 `orca next`。
- **异步化（next 返进行中 + 主 session 轮询）留 follow-up**（open q），不在此 spec。

---

## 8. 验收标准（AC，可客观判定）

- **AC1**：in-session workflow 含 script 节点能跑通：`setup(agent) → goal_check(script) → finalize(agent)`，`tars run` / `orca next` 链全程 0 `unsupported_node_kind`。
- **AC2**：一次 `orca next` 自动消化连续 script 链：`A(agent) → S1(script) → S2(script) → B(agent)`，主 session 从 A 产出后一次 next 直达 B 的 prompt（S1/S2 在引擎内透明跑完）。
- **AC3**：script timeout → 终态 `workflow_failed`，信封 `{done:true, error_kind:"script_timeout"}`，tape `data.kind=script_timeout`。
- **AC4**：script 非零退出码 → 不 fail loud，`output.exit_code` 落 tape，下游 route `when: exit_code==0` 正常分叉。
- **AC5**：script 产出不合 output_schema → 终态 fail loud（`output_schema_mismatch`），**不 recoverable**（不重 arm、不重派）。
- **AC6**：cli 路径与 daemon 路径跑同一含 script 的 wf，tape 的 `(type, node, 关键字段)` 逐 seq 对齐；且与 `orca run`（headless）对齐（G2 回归）。
- **AC7**（首例）：`agent-struct-exploration` 加 `goal_check(script)` 后，主 session 在第 5 轮无法通过伪造 `continue_loop=false` 进入 finalize——goal_check 读 `champions.jsonl` 算 `goal_met`，未达标强制回 hypothesizer（E2E 验证）。
- **AC8**：cancel（kill `orca next` 进程）→ script 子进程被 `registry.kill_one` 整组杀，无孤儿进程残留（`ps` 验证）。

---

## 9. 依赖纪律（铁律守门）

- `orca/run/step.py`（run 层）**不 import `orca.exec.script`**。step 只返 `node_kind` 决策信号，执行在 iface 层。
- 共享 helper（iface 层）依赖：`orca.run.step`（StepResult）+ `orca.exec.factory/script`（ScriptExecutor）+ `orca.events.bus`（EventBus）。方向：`iface → run/exec/events`，合法（不反向）。
- `make_executor` 已是 OCP 扩展点（`factory.py:56`），in-session 复用不改 factory。

---

## 10. open questions（需用户/spec-review 拍板）

- **Q1**：script 不合 output_schema → 终态 fail loud（本 spec 推荐）vs recoverable（让主 session 反馈重跑）？
- **Q2**：长跑阻塞 → 先接受同步（本 spec 范围）vs 一步到位异步化（next 返进行中 + 轮询）？
- **Q3**：本 spec 先做，`goal_check` 作首例（推荐）；还是 `goal_check` 用 agent 节点 dumb-copy 治标先上？
- **Q4**：`StepResult.node_kind` 字段（step 显式决策）vs cli 层自查 `wf.nodes[node].kind`？倾向前者。
- **Q5**：script 内联段的 emit 策略——逐条 `bus.emit` vs 攒批 `emit_batch`（原子化，对齐 daemon 反半截 tape 原则）？倾向攒批。
- **Q6**：entry 节点是 script（罕见）是否支持？倾向支持（首起即内联），但需测试覆盖。

---

## 11. 不做（YAGNI）

- foreach / parallel 节点的 in-session 支持（仍 `unsupported_node_kind`，归 phase 5 编排层）。
- script 异步化（next 返"进行中" + 主 session 轮询）—— follow-up。
- script 节点的 recoverable 重试 —— 不做（确定性，重跑无意义）。
- in-session 下 script 的 `InterruptHandler` 精细 cancel（Ctrl-G 式打断）—— per-call 模式 cancel=kill 进程，已够。

---

## 12. 首个使用案例：goal_check gate（agent-struct-exploration）

> 依赖本 spec 的 in-session script 支持。goal_check 的详细设计单独成 SPEC；本节仅锚定动机与契约。

### 12.1 现状根因（why）

`agent-struct-exploration.yaml` 路由（`:353`）：`when: curator.output.continue_loop → hypothesizer; else → finalize`。
`continue_loop` 由 `struct-curator/agent.md:163` 让 **curator 子 agent（LLM）产出**——`ledger_reducer.py:357` 虽确定性算了 true（第 5 轮必然 true），但 router 读的是 agent 产出的字段值（不重跑 reducer）。LLM 第 5 轮自作主张写 `continue_loop=false` → router 路由 finalize → workflow 合法 `done`，**目标未达成**。Stop hook 拦不住（不是 Stop，是合法推进）。

### 12.2 goal_check 方案（首例）

新增 `goal_check`（`kind: script`）节点：

```yaml
curator:
  routes:
    - to: goal_check        # 不再据 continue_loop 直路由

goal_check:                  # kind: script，确定性，零 LLM
  command: |
    python3 "{{setup.output.struct_scripts_dir}}/goal_check.py" \
      --champions "{{setup.output.champions_path}}" \
      --target_latency_ms "{{inputs.target_latency_ms}}" \
      --accuracy_target "{{setup.output.accuracy_target}}" \
      --max_rounds "{{inputs.max_rounds}}"
  parse_json: true
  output_schema:
    type: object
    required: [goal_met, terminate_reason]
    properties:
      goal_met: {type: boolean}
      terminate_reason: {type: string, enum: [champion_met, max_rounds, exploring]}
      gap_latency_ms: {type: number}
      gap_accuracy: {type: number}
  routes:
    - when: "goal_check.output.goal_met"
      to: finalize
    - to: hypothesizer       # 未达标强制回循环
```

- **治本**：goal_check 是 script（引擎内联跑），LLM 完全无判决权。即使 curator 伪造 `continue_loop`，goal_check 读 `champions.jsonl` 真实 KPI 复核，未达标打回 hypothesizer。
- **职责分离**：curator 不再产出 `continue_loop`（废字段，或保留为粗筛但路由不依赖）。循环退出唯一权威 = goal_check 的确定性判定。
- router 只读单字段 `goal_met`，**不需要复合表达式**（绕开 router 表达式能力限制）。

### 12.3 连带收益

in-session script 支持解锁的不止 goal_check——`struct` 所有确定性逻辑（`measure_baseline` / `ledger_reducer` / `viz_struct`）都可从 agent 节点 prompt 解放为独立 script 节点，LLM 只干"提假设/写代码/归因"。这是偿还 struct 的结构性债。

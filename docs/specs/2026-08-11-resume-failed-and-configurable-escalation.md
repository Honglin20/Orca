# SPEC — Resume Failed Runs + Configurable Recoverable Escalation

**日期**: 2026-08-11
**状态**: SPEC-REVIEW 闭环（conditional-pass → 修订定稿，2×HIGH + 5×MEDIUM + 6×LOW 全闭环）
**范围**: in-session shell（`orca next` / `advance_step`）+ events reducer + workflow schema
**动机**: 用户反馈两件事——① in-session recoverable 失败（output_schema_mismatch / agent_blocked）连续 3 次硬升格 `workflow_failed`（`_RECOVERABLE_ESCALATE_AT=3`，硬编码"不可配 YAGNI"），实际跑 nas-supernet 等带 `output_schema` 的 workflow 时太容易把 run 整死；② run 一旦 `workflow_failed` 就无法续跑（`advance_step` 终态守卫死锁），用户要「通过 run-id resume 任意一个没跑完的 run（包括 fail 的）」，且 resume 后 run 重新活跃、web 可见，像一直在续跑。

**不是**：per-node 可配（本 SPEC 只做 workflow 级标量）；resume `cancelled` run（用户主动 stop，保持终态）；改 headless `orca resume` 入口（其 `from_tape` 本就无 failed 守卫、偶然已能 resume failed，本 SPEC 的 reducer 翻转使其可观测正确——见 §1.3 已知行为变化——但不在测试范围）。

> **路径订正（SPEC-REVIEW E3）**：in-session 单步推进纯函数实际在 `orca/run/step.py`（非 `iface/in_session/step.py`）。本 SPEC 全篇用 `orca/run/step.py`。`apply_step_result` / `fail_in_session` / `merge_recoverable_envelope` 在 `orca/iface/in_session/_step_io.py`。

---

## 0. 现状诊断（契约事实，非本 SPEC 引入）

- **两个独立的"3"**：
  - `RetryPolicy.max_attempts`（`orca/schema/workflow.py:104`，默认 3）——`orca run` headless 的 transient 失败重试（429/timeout/spawn），**仅节点声明 `retry:` 才生效**。nas-supernet 节点不声明 retry → 不生效。**本 SPEC 不动它**。
  - `_RECOVERABLE_ESCALATE_AT = 3`（`orca/run/step.py:72`，硬编码）——in-session recoverable 失败升格上限。**本 SPEC 把它改成可配 + 默认 20**。
- **failed 死锁**：`advance_step`（`orca/run/step.py:693`）`if state.status in ("completed","failed","cancelled"): return StepResult(done=True, reason=f"already_{state.status}")` → failed run 的 `next` 永远返 `done`，不推进。
- **failed tape 的精确状态（三类失败路径，SPEC-REVIEW E3 补全）**：
  1. **escalation**（recoverable × N 撞上限）：`_recover_step_result`（`step.py:541`）emit `[node_failed(pending), node_started(pending), workflow_failed(kind=error_kind, node=pending)]`。fold 后 `status="failed"` / `current_node=pending`（wf_failed node≠None 覆盖）/ `node_status[pending]="running"`（ns 在 nf 后）。→ **失败节点 = current_node，精确可定位**。
  2. **compliance hard-limit**：`_emit_workflow_failed(bus, "subagent_compliance", ..., node=result.node)`（`cli.py:1782`，node=pending）→ fold 后 `current_node=pending`。→ **精确可定位**。
  3. **irrecoverable InSessionError**（render_error / state_corrupt / unsupported_node_kind / internal_error）：cli `except InSessionError` 调 `fail_in_session(bus, e)`（`_step_io.py:190`，**node 形参默认 None 且 cli 调用不传**）→ `workflow_failed(node=None)` → reducer **不覆盖** current_node → current_node 保留失败前的值（可能是上一个已完成节点 / stale）。→ **定位不保证**（见 §3 已知限制）。
- **marker 已被清**：终态时 `clear_marker`（`cli.py:1580` / `cli.py:1812`）删除 `runs/orca-<run_id>.json` → run 不再活跃（`_is_run_active` 判 marker 存在，`cli.py:2771`）。`next` 第一步 `marker is None → 返 "no-marker"`（`cli.py:1755`）→ **resume 必须重建 marker**。

---

## 1. 数据契约

### 1.1 Workflow schema（`orca/schema/workflow.py`，`Workflow` model line ~303，`extra="forbid"`）

新增字段：

```python
# in-session recoverable 失败升格上限（per-node 连续 output_schema_mismatch /
# agent_blocked 次数）。撞上限 → workflow_failed（终态，可经 orca next --run-id resume）。
# 与 RetryPolicy.max_attempts（transient 失败，独立预算，line 104 同 ge=1 fail loud 模式）
# 正交。ge=1：0 会让升格判定退化（首次失败即升格语义应显式写 1，而非 0 偷偷触发）。
recoverable_max_attempts: int = Field(default=20, ge=1)
```

- **默认 20**：留足空间让 agent 自我纠正，同时仍有兜底（防真无限烧 token）。
- **语义**：同节点**连续** recoverable 失败次数上限（`consecutive_fail_count` 口径）。任何 `node_completed`（任意节点）/ `workflow_resumed`（本 SPEC 引入的 reset 边界，§2.1.D）清零。
- compile validator 不需新增校验（`ge=1` 由 pydantic 在加载期保证；老 yaml 无此字段 → 取默认 20，向后兼容）。

### 1.2 事件契约（`orca/schema/event.py`）

`workflow_resumed` **不改 EventType 注册**（已存在，headless `run_from_state` 在用）。本 SPEC 只改其 **reducer 语义**（§1.3）+ 给 in-session resume 分支复用它。

in-session resume 分支 emit 的 `workflow_resumed.data`（additive，不破坏 headless 既有字段）：

```jsonc
{
  "from_tape": "<tape.path 绝对路径>",
  "resumed_node": "<失败节点名 = state.current_node>",
  "reason": "recovered_from_failure",
  "replayed_events": 0
}
```

`reason="recovered_from_failure"` 让 web/log_stream 能区分「崩溃 resume」vs「失败 resume」。reducer 不读 data 字段（只按 type 翻转 status，§1.3）。

### 1.3 reducer 契约（`orca/events/replay.py::apply_event`）

`workflow_resumed` 从 no-op 元组（`replay.py:269`）**抽出**为独立分支：

```python
if t == "workflow_resumed":
    # resume-failed：终态 failed → running 翻转（让 fold tape 的入口：web/status/list
    # 看到 resume 后 run 重新活跃）。崩溃-resume（headless run_from_state）时 tape 无
    # 终态事件，status 本就是 running → 翻转 no-op，不破坏既有语义。
    if state.status == "failed":
        return state.model_copy(update={"status": "running"})
    return state
```

- **幂等**：同一 `workflow_resumed` 应用 N 次 = 1 次（failed→running 第二次 no-op）。符合 SPEC §3.4 规则 8。SPEC-REVIEW E1 已验证全部状态组合（pending/running/failed/completed/cancelled）幂等。
- **不翻 `cancelled`/`completed`**：`cancelled` 保持终态（用户主动 stop，不可 resume）；`completed` 无意义（resume 一个已完成 run 不会发生——advance_step 不对 completed 走 resume 分支）。

**已知可观测行为变化（SPEC-REVIEW E11）**：headless `orca resume` on failed tape——pre-SPEC `workflow_resumed` no-op → status 留 `failed`；post-SPEC 翻 `running`。headless `_drive_from` **不读 state.status** 推进（不受影响），但 web/status 的 status 显示从 `failed` 变 `running`。这是预期改善（resume 即"重新跑起来"），不是回归。

---

## 2. 行为契约

### 2.1 `advance_step`（`orca/run/step.py`）—— 核心决策

**A. 终态守卫拆分**（替换 `step.py:693`）：

```python
# completed / cancelled → 仍终态（幂等，不 emit）
if state.status in ("completed", "cancelled"):
    return StepResult(done=True, reason=f"already_{state.status}")
# failed → resume 分支（§2.1.C）
```

**resume 无视 `--output`（SPEC-REVIEW E6，设计选择）**：resume 分支在终态守卫之后、output 处理之前触发——用户在 failed run 上传 `--output` 会被静默丢弃（resume 的 emits 是 `[workflow_resumed, node_started]` 而非 `node_completed`）。语义：resume = re-arm（重发 prompt），用户须在**下一次** next 喂 output。这是有意的（resume 的语义是"重新激活失败节点"，不是"提交产出"）。

**B. 可配升格上限贯穿**：模块常量 `_RECOVERABLE_ESCALATE_AT`（`step.py:72`）**删除**，替换为：
- `_DEFAULT_RECOVERABLE_MAX_ATTEMPTS = 20`（schema default 的单一来源，仅 `Workflow.recoverable_max_attempts=Field(default=...)` 引用）。
- 运行期读 `wf.recoverable_max_attempts`（advance_step / `_recover_step_result` / `_render_failure_history` / idempotent-replay 分支全部改读它）。

`_recover_step_result` 已收 `wf`（`step.py:542`），直接读 `wf.recoverable_max_attempts`。`_render_failure_history(records, retry_count, retry_budget)` 改签名加 `max_attempts: int`（纯渲染函数，显式传值，不引 wf——保持纯度 + 依赖单向）。idempotent-replay 分支（`step.py:791`）读 `wf.recoverable_max_attempts`。

**C. failed → resume 分支**（`advance_step` 内，紧跟守卫之后）：

```python
if state.status == "failed":
    target = state.current_node
    node = nodes.get(target) if target else None
    # 无可定位的失败节点（workflow 级失败 / current_node 已失效）→ done，不崩。
    if node is None or getattr(node, "kind", None) != "agent":
        return StepResult(done=True, reason="failed_no_resumable_node")
    # 渲染 prompt：含本节点历次失败历史（resume 前从 tape 取；reset 边界在 emit 后生效，
    # 而 advance_step 是 emit-only 纯函数——不写 tape，故同次调用内 consecutive_failures
    # 看不到 workflow_resumed，读到的是 emit 前的旧失败。时序正确。）
    records = consecutive_failures(tape, target)
    failure_history = _render_failure_history(
        records, retry_count=len(records),
        retry_budget=wf.recoverable_max_attempts - len(records),
        max_attempts=wf.recoverable_max_attempts,
    ) if records else None
    ctx = _build_ctx(wf, _outputs_acc_from_state(state), inputs, rid,
                     workflows_root=_workflows_root_from_yaml(yaml_path))
    prompt, prompt_file, rroot = _deliver(
        node, ctx, prompts_dir, wf=wf, project_root=project_root,
        no_memory=no_memory, failure_history=failure_history,
    )
    emits = [
        Emit("workflow_resumed", {
            "from_tape": str(getattr(tape, "path", "") or ""),
            "resumed_node": target, "reason": "recovered_from_failure",
            "replayed_events": 0,
        }),
        Emit("node_started", {"node": target}, node=target),
    ]
    logger.info("resume failed run（%s）从节点 %s 续跑", rid, target)
    return StepResult(
        emits=emits, done=False, node=target,
        prompt=prompt, prompt_file=prompt_file, resources_root=rroot,
        resumed=True, reason="recovered_from_failure",
        retry_count=0, retry_budget=wf.recoverable_max_attempts,
    )
```

**StepResult 新增字段**（`step.py` dataclass）：`resumed: bool = False`（CLI 据此 + marker 状态决定是否重建 marker / 回执加 `resumed` 标记）。

**D. `consecutive_failures` reset 边界扩展**（`step.py:183`）：

```python
for event in tape.replay():
    if event.type == "node_completed":
        records = []
    elif event.type == "workflow_resumed":   # NEW：resume = fresh start
        records = []
    elif event.type == "node_failed" and event.node == node:
        records.append(event.data or {})
```

`consecutive_fail_count` 是 `len(consecutive_failures)`（DRY delegate，`step.py:203`），自动跟随。

### 2.2 CLI（`orca/iface/in_session/cli.py::_next_in_critical_section`）—— marker 重建 + 写失败防护

**marker 重建 + 终态 reply 诚实**（替换 `cli.py:1754-1757` 的 `marker is None → no-marker` 早返）：

```python
marker = read_marker(mpath)
if marker is None:
    # marker 缺：三种情况——failed 待 resume（marker 已被终态清）/ 已终态 cancelled|completed
    # （同样清了 marker）/ 调用方未 bootstrap。peek tape 区分：
    state = replay_state(tape)
    if state.status == "failed":
        marker = ActivationMarker(run_id=run_id, no_output_count=0)
        # 下游 marker RMW（write_marker）持久化；此处先建对象让 compliance 计数有依托。
        logger.info("resume: 重建激活 marker（run=%s）", run_id)
    elif state.status in ("completed", "cancelled"):
        # SPEC-REVIEW E2E 发现 #1：cancelled/completed 的 marker 已清，旧逻辑返 no-marker
        # (done:false) 误导宿主以为"需 bootstrap"。advance_step 的 already_X 分支因 marker
        # 门控短路到不了 → CLI 层必须在此显式返 done:true + already_X（reply 诚实）。
        return StepResult(done=True, reason=f"already_{state.status}"), False, None
    else:
        logger.warning("next 找不到 %s 的激活 marker，无法推进（需先 bootstrap）", run_id)
        return StepResult(done=False, reason="no-marker"), False, None
```

下游既有逻辑天然正确：`advance_step` 走 resume 分支返 `result.resumed=True` / `emits=[workflow_resumed, node_started]` 非空 → compliance 不递增（`elif result.emits == []` 不命中，`cli.py:1779`）；env 文件按 `result.node=target` 重写（`cli.py:1795`）；`next` 顶层 `if result.node and not (done or compliance_failed)` → respawn chart+sidechain 守护（`cli.py:1566`）。

**marker 写失败防护（SPEC-REVIEW E1，HIGH，决策 b：包裹整个 write_marker）**：现有 `write_marker(mpath, marker)`（`cli.py:1814` 的 else 分支）**无 try/except**——resume 后 tape 已有 `workflow_resumed`（status=running），若 write_marker OSError → status=running + marker 缺 → 下次 next 的重建条件 `state.status=="failed"` 不命中（已是 running）→ **不可自愈死锁**。须补：

```python
# marker RMW（N2）：flock 临界区内回写。终态 → 清 marker；非终态 → write_marker。
if result.done or compliance_failed:
    clear_marker(mpath)
else:
    try:
        write_marker(mpath, marker)
    except OSError as e:
        # 对齐 bootstrap（cli.py:1331-1354）：emit workflow_failed 翻转 running→failed
        # （使下次 next 的重建条件 state.status=="failed" 可重新触发，自愈）+
        # clear_marker + 错误信封。不补 → 死锁。
        logger.exception("next write_marker 失败")
        await _emit_workflow_failed(
            bus, "internal_error", f"write_marker failed: {e}", node=result.node,
        )
        clear_marker(mpath)
        return StepResult(done=True, reason=f"failed: write_marker: {e}",
                          error_kind="internal_error"), True, None
```

包裹**整个** write_marker（非仅 resume 路径）——既修 resume 死锁又补 pre-existing gap（任何 next 的 marker 写失败都自愈），零额外代价。

- **回执**（`cli.py:1589` reply 区）additive：`result.resumed` 时 reply 加 `"resumed": True`。
- **model 字段**：重建的 marker `model=None`（旧 marker 已清，model 是 status 展示用 hint，非权威；保持 None，不为此再读 tape）。

### 2.3 双重计数一致性（invariant）

resume 后，「升格计数（`consecutive_fail_count`，tape 派生）」与「StepResult.retry_count（=0）」一致：前者因 `workflow_resumed` reset 边界归零，后者显式 0。两者后续同步递增直到 `wf.recoverable_max_attempts`。

**首次 resume 的例外（SPEC-REVIEW E8，设计选择，已确认可接受）**：首次 resume 的 StepResult `retry_count=0` 但 `failure_history` 非空（含 resume 前的旧失败）。语义：agent 需知历史以自我纠正，但**计数重开**（resume = fresh attempt 计数）。后续 `consecutive_fail_count` 与 `retry_count` 同步递增。

### 2.4 下游终态判定须认 `workflow_resumed`（SPEC-REVIEW E2E 发现 #2/#3 补强）

**问题**：`workflow_resumed` 是本 SPEC 引入的"重新激活"事件（run 从 failed 翻回 running）。但下游多个消费者用「扫终态事件类型」的启发式判定终态，**不认 `workflow_resumed`** → resumed run 被误判终态。E2E 实证三类缺口：

1. **chart/sidechain daemon 自退**（`orca/iface/in_session/chart_daemon.py::_watch_terminal` line ~172，sidechain 复用之 DRY）：`_TERMINAL_EVENT_TYPES = ("workflow_completed","workflow_failed","workflow_cancelled")`，tail tape 见任一即返 `"terminal"` 退出。resume 后 `next` respawn 新 daemon，新进程从 offset 0 读全序列 `[wf_failed, wf_resumed, node_started]`，**看到历史 wf_failed 秒退（~1ms）** → resumed run 的子代理 `render_chart` 连不上 socket，**live web 图表全丢**（违背"web 可见/如续跑"核心诉求）。
2. **web `meta.status` stale=failed**（`orca/iface/web/run_manager.py::_scan_terminal_type` line ~2352 + `_probe_head_and_terminal` line ~2372）：返"最末终态事件类型"，`workflow_resumed` 不清零 → attach_run 把 resumed run 当终态 failed，不起 follow、`meta.status=failed`（权威 `state.status`=running 正确，但前端若读 meta.status 显示错）。
3. **cli stop 误短路**（`orca/iface/in_session/_tape_probe.py::scan_terminal` line ~81，cli.py stop/status/gc 用）：同款"最末终态类型"判定 → `orca stop <resumed-run>` 见历史 wf_failed 短路 `already-terminal` exit 0，实际 run 在跑。

**修法（surgical，各模块教自己的终态判定认 `workflow_resumed`；分层不允许跨模块抽共享 helper——web ↔ iface/in_session 无依赖关系，3 处各改是架构诚实而非 DRY 违规）**：

- **`chart_daemon._watch_terminal`**（一处修，sidechain 自动跟随）：
  ```python
  terminated = False   # 跨 poll 持久；terminal 置 True，workflow_resumed 置 False
  ...  # 在 line 循环内：
      etype = obj.get("type")
      if etype in _TERMINAL_EVENT_TYPES:
          terminated = True
      elif etype == "workflow_resumed":
          terminated = False   # resume 重新激活 → 取消终态
  ...  # chunk 处理完（line 循环结束后）：
      if terminated:
          return "terminal"
  ```
  关键时序：resume 时 `next` 先把 `[wf_failed, wf_resumed, node_started]` 写 tape **再** respawn daemon（`cli.py:1566` guard），新 daemon 首次 poll 从 offset 0 读全序列 → terminated 终值 False → 存活。真终态（无后续 resume）chunk 末 terminated=True → 退（同旧行为）。

- **`run_manager._scan_terminal_type` + `_probe_head_and_terminal`**：扫到 `workflow_resumed` 清 `last_terminal=None`：
  ```python
  for event in tape_reader_replay(path, since_seq=0):
      if event.type in ("workflow_completed","workflow_failed","workflow_cancelled"):
          last_terminal = event.type
      elif event.type == "workflow_resumed":
          last_terminal = None   # resume 重新激活 → 非终态
  ```
  → resumed run 返 None（非终态）→ attach_run 起 follow + status=running。

- **`_tape_probe.scan_terminal`**：同款清零逻辑（terminal 置类型，workflow_resumed 清 None）。gc 路径不受影响（resumed run 有 marker → `_is_run_active` True → 不收集），但 stop/status 须诚实。

**AC8/AC10 订正**：原 SPEC §2.2 claim"下游既有逻辑天然正确"在 daemon/web 终态判定处**不成立**，本 §2.4 是其修正补强。AC8（守护存活）+ AC10（web 可见 running）的达成**依赖本节三处修复**。

---

## 3. 失败路径 / 鲁棒性（fail loud）

| 场景 | 行为 |
|---|---|
| resume 时 `state.current_node` 为 None / 非 agent 节点 | `StepResult(done=True, reason="failed_no_resumable_node")`，不崩 |
| **resume 时 marker 写失败（磁盘满/权限）**（SPEC-REVIEW E1） | **无既有 try/except**（区别于 bootstrap）；本 SPEC §2.2 补 try/except OSError → emit workflow_failed（翻 running→failed，使下次 next 重建条件可重新触发，**自愈**）+ clear_marker + 错误信封 + Exit(1) |
| 同 failed run 并发 next | tape flock `LOCK_NB` busy-exit（既有，`cli.py:1535`） |
| tape 中段损坏 | 既有 `_find_first_corrupt_line` / `InSessionError(state_corrupt)` |
| `cancelled` run next | 终态守卫（§2.1.A）返 done，不 resume |
| `completed` run next | 终态守卫返 done，不 resume |
| **irrecoverable resume 的定位精度（SPEC-REVIEW E2，已知限制）** | irrecoverable 失败（render_error/state_corrupt/...）经 `fail_in_session(bus, e)` emit `workflow_failed(node=None)` → reducer 不覆盖 current_node → resume target 可能指向上游已完成节点或 stale。**resume 定位精度仅对 escalation + compliance 保证**；irrecoverable 不保证（re-arm 上游 → feed output → 可能再次撞下游 render_error）。仍允许 resume（符合"resume 任意 fail"），失败历史 + 立即再败即反馈 |

---

## 4. 验收标准（AC，逐条可验）

- **AC1**（可配升格）：yaml `recoverable_max_attempts: 2` → 同节点连续 2 次 recoverable 失败 → `workflow_failed`；只 1 次 → re-arm（run 存活）。
- **AC2**（默认 20）：无 yaml 字段 → `wf.recoverable_max_attempts == 20`；需连续 20 次才升格。
- **AC3**（resume 触发）：failed run 调 `orca next --run-id X`（无 output）→ 回执 `done=false, resumed=true, node=<失败节点>`；tape 追加 `[workflow_resumed, node_started(<失败节点>)]`；prompt 含历次失败历史。
- **AC4**（status 翻转）：resume 后 `replay_state(tape).status == "running"`（reducer 翻转生效）；`read_marker` 返非 None（活跃）。
- **AC5**（计数清零）：resume 后 `consecutive_fail_count(tape, <失败节点>) == 0`。
- **AC6**（终态保留）：`cancelled` / `completed` run next → `done=true` 且 `emits == []`（无 `workflow_resumed` 落 tape）。
- **AC7**（无可定位节点）：failed 但 `current_node` 为 None / 非 agent → `done=true, reason="failed_no_resumable_node"`，不崩。
- **AC8**（守护 respawn）：resume 后 chart daemon + sidechain daemon socket 存活（既有 next guard `cli.py:1566` 验；断言 socket 文件存在 + `_chart_daemon_alive`）。
- **AC9**（端到端续跑）：resume 后喂合法 output → `node_completed` → workflow 续跑到 `workflow_completed`。
- **AC10**（web 可见）：resume 后 web（`tars serve`）该 run status 显示 running/活跃（fold tape status=running + marker 存在）。
- **AC11**（irrecoverable resume，SPEC-REVIEW E2 重写）：render_error 终态的 run resume → advance_step resume 分支调 `_deliver` → `_render_or_fail` 抛同一 `InSessionError(render_error)` → `[workflow_resumed, node_started]` emits **未落 tape**（advance_step emit-only 纯函数，raise 时 emits 丢弃；apply_step_result 未被调）→ cli `except InSessionError` → `fail_in_session` emit 第二条 `workflow_failed` → run 保持 failed。**断言点**：`reply.error_kind == "render_error"` 且 `reply.done == True` 且 tape 末条为 `workflow_failed`（**非** `workflow_resumed`）。

---

## 5. 测试矩阵（CODER 阶段单测 + TEST-AGENT 阶段真机）

**单测**（`tests/iface/in_session/` + `tests/events/`）：
- `test_advance_step_resume_failed`：AC1/3/5/7（advance_step 纯决策，inline `prompts_dir=None`）。
- `test_reducer_workflow_resumed_flip`：AC4（reducer failed→running；running→running no-op；cancelled/completed/pending 不翻）。**SPEC-REVIEW E4**：`test_replay.py::test_known_noop_events_dont_mutate_state`（line ~326）需把 `workflow_resumed` 标注为"条件 no-op（仅 non-failed state）"——该测试在 pending state 仍 pass，但注释 "MUST no-op" 须更新。
- `test_consecutive_failures_reset_on_resume`：AC5（reset 边界）。
- `test_recoverable_max_attempts_config`：AC1/2（wf 字段驱动升格阈值；fixture 显式设值）。
- `test_terminal_runs_not_resumed`（SPEC-REVIEW E9）：AC6——cancelled/completed tape → advance_step → `done=True` + `emits==[]`。
- `test_resume_failed_no_resumable_node`（SPEC-REVIEW E10）：AC7——手写 tape 含 `workflow_failed(node=None)` 且无对应 `node_started` → current_node=None → resume → `failed_no_resumable_node`。

**既有测试影响（SPEC-REVIEW E5）**：以下测试隐式编码阈值 3（通过行为，非 import 常量名）→ 须在 fixture 显式设 `recoverable_max_attempts=3` 或调整断言：
- `tests/iface/in_session/test_error_management.py`（如 `test_recoverable_escalation_after_3_consecutive`）。
- `tests/iface/in_session/test_failure_sentinel.py`（`test_ac3_three_mixed_kinds_escalate_with_last_kind` / `test_ac3_reverse_kind_order_escalates_with_last_kind`）。

**TEST-AGENT 真机 E2E**（in-session headless，AC3/4/8/9/10/11）：
- 真 `orca bootstrap` → 推进到某 output_schema 节点 → 连续喂坏 output 撞 `recoverable_max_attempts` → `workflow_failed` → `orca next --run-id X` 自动 resume → 断言 marker 重建 + status running + 失败历史进 prompt → 喂合法 output 续跑到 completed → 断言 web 可见。
- 再测 irrecoverable（注入 render_error）resume 立即再败（AC11 断言点）；cancelled 不可 resume（AC6）。

---

## 6. 过期代码清理

**穷举 `_RECOVERABLE_ESCALATE_AT` 全部引用**（SPEC-REVIEW E7，grep 已核实，不给不完整行号表）：
- `orca/run/step.py` code：72（定义）、524、589、604、614、620、794。
- `orca/run/step.py` docstring：189、517、549。
- 上述全部替换为 `_DEFAULT_RECOVERABLE_MAX_ATTEMPTS`（仅 schema default 引用）或 `wf.recoverable_max_attempts`（运行期）。
- 删常量定义（72）+ 全部引用改读 `wf.recoverable_max_attempts`。
- SPEC breadcrumb：实现后 `step.py` / `cli.py` / `replay.py` / `workflow.py` 顶部或函数 docstring 加一行「SPEC 2026-08-11 §x」指向本文件（项目惯例）。

---

## 7. 依赖纪律（单向铁律）

- `schema` ← 只加字段，无新依赖。
- `events/replay.py` ← reducer 改一个分支，不引新依赖。
- `orca/run/step.py` ← 依赖 `schema`（读 `wf.recoverable_max_attempts`）/`events`/`run`，无反向。`_render_failure_history` 加 `max_attempts: int` 形参（纯函数，不引 wf，保持依赖单向 + 纯度）。
- `orca/iface/in_session/cli.py` ← 依赖 `run.step`/`iface/in_session.marker`/`events`/`iface/in_session._step_io`，无反向。
- 不破「schema/run/exec/events/iface 单向」铁律；reducer 仍是单一 fold（一条读路径）。

---

## 8. SPEC-REVIEW 闭环记录 + 实施前置决策

SPEC-REVIEW（spec-reviewer，2 轮对抗，12 issue 全闭环）关键决策：

- **E1（HIGH）marker 写失败死锁**：决策 **(b)** 包裹 `_next_in_critical_section` 整个 write_marker（非仅 resume 路径），补 pre-existing gap。→ §2.2 + §3。
- **E2（HIGH）AC11 语义错**：重写 AC11（irrecoverable resume 时 workflow_resumed 未落 tape）；§3 加定位精度已知限制。→ §3 + §4 AC11。
- **E8 设计选择**：首次 resume `retry_count=0` / `failure_history` 非空——判定可接受（agent 需知历史，计数重开）。→ §2.3。
- E3/E4/E5/E6/E7/E9/E10/E11：分别落 §0/§5/§5/§2.1.A/§6/§5/§5/§1.3。

**实施前置条件（CODER 必须遵守）**：本 SPEC 已是定稿契约，逐字实现不自作主张加字段；行号锚点以 grep 实时核实为准（实现时行号会随改动漂移）。

**E2E 闭环记录（test-agent 真机，2026-08-11）**：核心 resume 逻辑（AC1/3/4/5/7/9/11）全过、零核心 bug。E2E 发现 3 个相邻层缺口（`workflow_resumed` 未传播到下游终态判定）→ 本 SPEC §2.4 补强 + §2.2 gate 诚实化（cancelled/completed reply）。**第二轮 CODER 须实现 §2.4 三处 + §2.2 gate 扩展，并重验 AC8（daemon 存活）/ AC10（web status running）/ AC6（cancelled reply done:true already_cancelled）。**

# in-session Script 节点支持 SPEC（v3，round-2 评审修订版）

> 状态：SPEC（契约，逐字实现）。设计依据：[in-session-script-node-design-draft.md](in-session-script-node-design-draft.md)（2026-07-27）+ 2026-08-21 用户接口讨论拍板。
> 日期：2026-08-21。v3 = round-1（21 项）+ round-2（5 MAJOR + 12 MINOR + U1/U2）评审全闭环；决策记录见 §9。
> 硬约束（用户拍板）：**纯增量实现**——不含 script 节点的既有 workflow 行为逐字节不变；不改动 schema / compile / exec 层。

---

## 0. 目标与非目标

**目标**：in-session 路径（`orca <wf>` bootstrap、`orca next`、daemon `next`）支持 `kind: script` 节点：一次调用内**同步阻塞**地就地执行连续 script 节点链（有上限，§2.5），停在下一个 agent 节点（返回其 prompt 交付）或终态；script 的判定结果（exit_code / parse_json 产物）经路由确定性分叉，并在返回 JSON 中以 `auto_executed` 摘要向主 session 报备（成功与失败信封都报备，§2.7）。

**非目标（YAGNI，维持现状拒绝）**：
- foreach / parallel / wait / set / terminate / gate 节点的 in-session 支持——继续 `unsupported_node_kind` fail loud。
- script 异步化（next 返"进行中"+ 轮询）——follow-up。
- script 重试（recoverable / RetryPolicy）——确定性重跑无意义。
- ScriptNode 加 `output_schema` 字段——schema 层零改动；结构化输出走既有 `parse_json`（headless 同语义）。
- bootstrap 期 chart 守护调序（entry-script 推图边界文档化，§2.6-C）——follow-up。

---

## 1. 现状基线（行号为 2026-08-21 master 状态）

- 守卫点：`orca/run/step.py:876-888` `_check_agent_node`——entry 分支（`step.py:768`）与 output 应用后（`step.py:834`）两处调用，非 agent kind 一律 `InSessionError(ERR_UNSUPPORTED_NODE_KIND)`。
- `advance_step`（`step.py`）：纯决策（emit-only，不写 tape、不跑命令）。分支 3（output 应用）：`nc(pending) → 路由 → rt + ns(next) + _deliver`；分支 4（无 output 幂等重发）：pending agent 重发 prompt，`emits=[]`。
- `ScriptExecutor`（`orca/exec/script.py:54`）：async generator——首 yield `node_started({kind:"script", command})`，subprocess 跑完 yield `node_completed({output:{stdout,stderr,exit_code[,json]}, elapsed})`；ExecError 时 yield `ns → node_failed → error` 三事件后终止（**尾随 `error` 事件在 headless 从不落 tape**：`executor_adapter.py:70-78` 在 node_failed 即 raise）。**本 SPEC 零改动复用**。
- `make_executor(node, runs_dir=, workflows_root=)`（`orca/exec/factory.py:56`）：ScriptNode → ScriptExecutor。**零改动复用**。
- headless 事件序（G2 基准，`orchestrator.py:894`）：`nc(cur) → rt(cur→next) → ns(next)`；ns/nc 由 executor 逐条 yield 逐条 emit（`executor_adapter.py:61-69`），rt 由编排层 emit。
- in-session CLI：`next`（`cli.py:1524`）临界区 `_next_in_critical_section`（`cli.py:1752`）；bootstrap 经 `_advance_and_emit`（`cli.py:1655`）。daemon：`orca/iface/in_session/daemon.py` `next`（其 `except InSessionError` 目前只包 advance_step，`daemon.py:134-143`）。
- `resolve_max_iter(wf, inputs)`（run 层，orchestrator `__init__` 同源调用）：循环上限解析。
- 回复拼装：`cli.py:1609-1652`（next）/ `cli.py:1499-1521`（bootstrap）。
- 既有测试锚点：`tests/iface/in_session/test_in_session_cli.py:971-987` `test_failure_unsupported_node_kind`（fixture 用 `kind: script`——本 SPEC 落地后需换 kind，见 §5.4）。

---

## 2. 接口契约

### 2.1 `StepResult` 新字段（`orca/run/step.py`）

```
node_kind: str | None = None   # "agent" | "script"；None ≡ "agent"（旧调用方零感知）
```

`node_kind == "script"` 时：`prompt` / `prompt_file` / `resources_root` 恒为 `None`（script 无 prompt，不落 prompt 文件）；`node` = 该 script 节点名；`emits` **不含该 script 节点自身的 ns/nc**（上游节点的 nc/rt 属路由前序，正常包含；该 script 的 ns/nc 由 executor 产出、helper 逐批 emit，见 2.4）。

### 2.2 `advance_step` 行为变更（四个点，其余路径逐字不变）

1. **entry 分支**（`step.py:766-782`）：entry 是 script → emits = `[workflow_started]`（**不 emit** `node_started`，executor 负责），返回 `StepResult(node=entry, node_kind="script", emits=[ws])`。
2. **分支 3 尾部**（`step.py:834`）：`_check_agent_node(nxt)` 改为 kind 分流——nxt 是 **agent** → 现行为逐字不变（rt + ns + _deliver）；nxt 是 **script** → emits 追加 `route_taken(from=pending, to=nxt)` 后返回 `StepResult(node=nxt, node_kind="script")`（不 emit ns、不 deliver）；其余 kind → 维持 `unsupported_node_kind` fail loud（守卫保留，仅对 script 放行；错误消息更新为「仅支持 agent/script」）。
3. **分支 4（幂等重发）**：pending 是 script → 返回 `StepResult(node=pending, node_kind="script", emits=[])`（无 prompt 可重发；语义 = 调用方重新执行 script，at-least-once，见 §2.6-C）。
4. **分支 3 入口守卫（防宿主劫持）**：`output is not None` 且 pending 节点 kind == script → `raise InSessionError(ERR_STATE_CORRUPT)`，message 明示「script 节点产出由引擎执行产生，宿主无权代交卷；请调 `orca next --run-id <id>`（不带 --output）让引擎重执行」。宿主的 output 对 script 节点没有合法来源（崩溃续跑期宿主拿到的仍是旧 agent prompt），任何到达此分支的 --output 都是异常路径。

`_check_agent_node` 更名或保留由实现定；错误文案更新列入改动清单。

### 2.3 新纯决策函数 `advance_after_script`（`orca/run/step.py`）

```
def advance_after_script(tape, wf, script_name: str, *, inputs, run_id,
                         prompts_dir, project_root=None, no_memory=False,
                         yaml_path=None) -> StepResult
```

前置条件：`script_name` 的 `node_completed` **已落 tape**（调用方在 executor nc emit 之后调用）。行为（镜像分支 3 尾部，nc 已在 tape 故不重复 emit）：

1. `_replay_fold(tape)` 取最新 state；`outputs_acc = _outputs_acc_from_state(state)`（script 产出已含）。
2. `nxt = Orchestrator._next_node_for_resume(wf, script_name, outputs_acc, inputs=inputs)`（§2.5-D2 扩参后的签名；`inputs` = 共享循环派生的 `merged_inputs`，见 §2.5；与分支 3 同函数，DRY）。
3. `nxt == END` → emits `[route_taken(from=script_name, to=END), workflow_completed(...)]`；`workflow_completed` 载荷 = `_final_outputs(wf, outputs_acc, inputs, run_id)`（分支 3 同函数、`step.py:828-830` 同源）、elapsed 从 tape `workflow_started` 时间戳差算（M5 同款）。返回 `done=True`。
4. nxt 是 agent → emits `[rt, node_started(next)]`，ns data = `{"node": nxt}`（分支 3 同形，`step.py:836`）+ **与分支 3 尾部逐形镜像的交付**：`_deliver(node, ctx, prompts_dir, wf=wf, project_root=project_root, no_memory=no_memory)`（**无** failure_history——分支 3 尾部同样不传）；StepResult 置齐 `prompt` / `prompt_file` / `resources_root` **三字段**（inline 回退路径 `prompt` 是唯一交付物；`resources_root` 是 env 文件 `ORCA_AGENT_RESOURCES` 来源）。
5. nxt 是 script → emits `[rt]` → `StepResult(node=nxt, node_kind="script")`（循环继续）。
6. 其余 kind → `unsupported_node_kind` fail loud。

### 2.4 iface 内联执行 helper（`orca/iface/in_session/_step_io.py` 或新模块 `_inline_script.py`，实现自选）

```
async def execute_script_inline(bus, tape, wf, node: ScriptNode, *, run_id,
                                inputs, yaml_path=None) -> dict  # script output（nc.data.output）
```

行为：

1. **ctx 构造**：`RunContext` 直构（`inputs=merged_inputs`（§2.5 同源派生，**不是** CLI 原始 `--inputs`）、`outputs=<state→outputs_acc 转换结果>`、`run_id`、`subagents_root` 同 `_build_ctx` 逻辑）。**state→outputs_acc 转换**（`{"output": raw}` 包装，`orca/run/resume.py:228-235` 的 `_outputs_acc_from_state` 是 run 层私有）**禁止 iface import**——随 `_build_ctx` 公开化一并提供（推荐：step.py 公开包装函数），或在 helper 内明示内联该单行转换。二选一：step.py 公开化（推荐）或 helper 内直构 + 内联转换。
2. **executor**：`make_executor(node, runs_dir=tape.path.parent, workflows_root=<yaml_path 父目录>)`；`yaml_path` 为 None（daemon/老 tape）→ 回落 `wf.workflows_root`（`step.py:274-275` `_build_ctx` 同款回落）。
3. **事件流式逐批 emit（D1=a，与 headless 逐条 emit 同形）**：消费 `executor.exec(node, ctx)` async generator——
   - 首 yield `node_started` → **立即 `emit_batch([ns])`**（ns 先落 tape：script 执行期间被杀的崩溃态可达，§2.6-C）；
   - 正常完成 yield `node_completed` → `emit_batch([nc])`；
   - ExecError 路径 yield `node_failed` → `emit_batch([nf])`，**尾随的 `error` 事件丢弃不 emit**（headless 同语义：`executor_adapter.py:70-78` 在 node_failed 即 raise，`error` 从不落 tape；reducer 对 `error` 事件 no-op，丢弃零状态影响）→ 随后 `raise InSessionError`（映射 §2.6）。
   - 批次间崩溃（nc/nf 未落）→ tape 停留 `ns(S)`，可恢复（§2.6-C 窗口 i）。Event→bus emit 时**保留 executor 产出的 session_id / timestamp / node / data**（session_id 是 web 按 session 分组的依据）。
4. 正常完成 → 返回 `nc.data.output`。
5. **auto_executed 摘要条目**：`{node, exit_code, elapsed, stdout_tail, stderr_tail}`，tail 各截断 ≤500 字符（`_AUTO_EXEC_TAIL_LIMIT = 500`；完整 output 只在 tape，web 可查）。

### 2.5 驱动循环（cli 与 daemon 共享，形如 `advance_with_scripts`）

`_advance_and_emit`（bootstrap）与 `_next_in_critical_section`（next）、daemon `next` 统一改为调共享循环：

```
merged_inputs = _resolve_inputs(wf, {**_replay_fold(tape).inputs, **cli_inputs})
    # M1：与 advance_step 内部（step.py:708-712）同源同式派生；全程唯一口径
result = advance_step(...)                      # 现行为
await apply_step_result(bus, result)            # 批：advance_step 的 emits
auto_executed = []
while (not result.done) and result.node_kind == "script":
    if len(auto_executed) >= resolve_max_iter(wf, merged_inputs):   # D3：防 script 路由成环
        raise InSessionError(ERR_INTERNAL_ERROR,
            f"内联 script 执行数撞 max_iter 上限（疑似路由成环）")
    output = await execute_script_inline(..., inputs=merged_inputs)  # ns / nc(或 nf) 各自成批 emit
    auto_executed.append(summary)
    result = advance_after_script(tape, wf, result.node, ..., inputs=merged_inputs)
    await apply_step_result(bus, result)        # 批：rt(+ns(next)/workflow_completed)
```

**`merged_inputs` 口径（M1）**：`merged_inputs = _resolve_inputs(wf, {**_replay_fold(tape).inputs, **cli_inputs})`——tape 是 inputs 真相源（CLI `--inputs` 默认 `{}`），default 填充后向 `execute_script_inline`（ctx + command 渲染）、`advance_after_script`（路由 when + `_final_outputs`）、`resolve_max_iter`（含 `inputs["iterations"]` 覆盖）**全程透传同一值**。禁止任何一处改用 CLI 原始 inputs（script command 引用 `{{ inputs.* }}` 会 StrictUndefined → 假 internal_error）。

- **失败路径**：`execute_script_inline` raise `InSessionError` → 现有 except 路径 `fail_in_session`（emit workflow_failed + 错误信封 + exit 1）——复用零新信封。**try 界**：cli 两入口现有临界区 try 不变（已包整个临界区体）；**daemon `next` 的 `except InSessionError` 扩为包住整个共享循环**（advance + apply + 内联执行；现状只包 advance_step，`daemon.py:134-143`）。
- **循环内 node_failed 不重试、不 recoverable**（§0 非目标）。
- **守护时序（D4，round-3 钉死口径）**：`cli.py:1586-1591` 既有尾部 ensure（条件 `result.node is not None and not (result.done or compliance_failed)`）**保持原位原条件不变**；新增**前置 ensure**：**仅 cli `next` 临界区内、调用共享循环之前**，若 `result.node_kind == "script"` 且非终态 → 先 `_ensure_chart_daemon` / `_ensure_sidechain_daemon` 再进循环（保证循环内 script 推图时守护在线）。**不进共享循环本体、不适用 bootstrap**（bootstrap 守护仍按现状锁外 spawn，见 §2.6-C 幂等契约②；B8 短路清单不变）**与 daemon**（无守护概念）。前置 ensure 在 flock 临界区内，与尾部 ensure 同款 probe+respawn 幂等语义，重复调用无害。
- **env 文件（`_write_orca_env`）**：仅当**共享循环结束后的最终** result 非终态、`result.node` 非空且 **`node_kind != "script"`（None 视同 agent）** 时按该节点重写（script 节点 env 由 executor spawn overlay 注入；env 文件是给子代理 source 的）。纯 agent workflow 行为不变（node_kind=None → 照写）。
- **合规计数与 marker RMW**：均取共享循环结束后的**最终** result 判定，逻辑不变（`result.emits == []` 判定对最终 result 施行；script 链执行的调用最终 emits 非空 → 不触发计数；`--output` 给了则清零不变）。**窗口 i 恢复调用（分支 4 script 重发首步 emits=[]）**：中间 result 不参与计数——计数只看循环最终 result；恢复后 script 链执行产出 emits → 不增 `no_output_count`。
- **bootstrap 终态短路（B8）**：共享循环返回 `done=True`（如 entry script 直通 `$end`）→ **不写 marker、不写 env 文件、不 spawn chart/sidechain 守护、不开 web、不注册项目（`_register_current_project` 也跳过）**——跳过 bootstrap 锁外全套动作，仅拼终态回复（run 不进 web 列表属已知取舍：排查走 tape 路径；下次任一 bootstrap 会再注册本项目）。

### 2.6 错误映射（script 失败 → in-session taxonomy）

| script 情况 | ScriptExecutor 行为 | in-session 映射 | tape 序列 | 结果 |
|---|---|---|---|---|
| exit=0 | ns + nc(output) | 正常 → `advance_after_script` | ns→nc→rt | 继续推进 |
| **非零退出码** | ns + nc(output 含 exit_code) | **不 fail loud**（业务结果） | ns→nc→rt | 路由 `when: output.exit_code == ...` 确定性分叉 |
| **timeout** | ns + nf + error | 丢弃 error；`InSessionError(error_kind="script_timeout")` | ns→nf→workflow_failed | 终态 fail loud |
| **spawn / render 失败** | ns + nf + error | 丢弃 error；`InSessionError(error_kind="internal_error")`（message 含 phase） | ns→nf→workflow_failed | 终态 fail loud |
| parse_json 失败 | ns + nc（`output.json=None`） | 降级不阻断；**路由 `when` 若引用 `output.json.*` → RouteError fail loud**（script output 恒为 dict 非 None，`skip_tolerant` 永不触发，`router.py:108`；StrictUndefined 包 RouteError 后 re-raise，`router.py:112-118`）。**RouteError 非 InSessionError，in-session 现有 except 不捕 → 裸崩（pre-existing，agent 节点路由同况，follow-up 包成 InSessionError）**。workflow 作者须写防御性 when（`output.json.goal_met is defined and ...` / `| default(false)`）或让兜底 route 不引用 json 字段 | ns→nc→(RouteError 裸崩，无终态事件) | 测试按防御性写法断言落兜底；裸崩路径断言非 0 退出 + 无 workflow_completed |

新 error_kind 常量：`ERR_SCRIPT_TIMEOUT = "script_timeout"`（`step.py` 常量区段）。

**（C）崩溃恢复 / at-least-once 语义（D1=a 后的真实窗口）**：

- **窗口 0（bootstrap entry script 的 ws/ns 批间隙，罕见）**：entry script 场景 bootstrap 首批拆为 `[ws]` 与 `[ns]` 两批，间隙被杀 → 下次 `next` 走分支 1b（failed resume）或分支 4，均无法 re-arm 非 agent 节点（`step.py:730-731`）→ **fail loud 且不可逆**（同窗口 ii 处置：fail loud、无 resume 路径，已知限制）。
- **窗口 i（可恢复，主窗口）**：CLI 在 script 执行期间被杀（宿主 bash 超时 / SIGKILL）→ ns 已落 tape、nc 未落；flock 随进程退出释放。下次 `next` **不带 --output** → 分支 4 → `node_kind="script"` → **重新执行 S**（at-least-once）。tape 允许同一节点重复 `node_started`（reducer 幂等，status 保持 running）。带 `--output` 重试 → §2.2.4 守卫 `state_corrupt` fail loud。
- **窗口 ii（罕见毫秒级，fail loud 且不可逆）**：nc 已落 tape、rt 未落（两批之间被杀）→ 下次 `next` 无 running 节点 → 分支 4 `state_corrupt` → `workflow_failed`；此后分支 1b 不 re-arm 非 agent 节点 → **无 resume 路径**。文档化为已知限制（headless 有 from_tape 重放处理 `orchestrator.py:499-537`；in-session 不做，YAGNI）。
- **子进程清理的诚实声明（B6）**：SIGINT/正常退出由 `registry` atexit 整组清理；**SIGTERM/SIGKILL 下 per-call CLI 无信号处置，script 子进程（`start_new_session=True` 脱钩）可能残留孤儿**——已知限制，与 agent 子代理 spawn 同况。因此幂等/单实例契约（下条）是必须项。
- **长 script 持 flock 的运维面（U1=a，已知限制）**：script 执行期间 `orca next`/`stop` 撞 `LOCK_NB` busy 即退（`cli.py:2082-2085`）。处置：kill 持锁的 next 进程（flock 随进程释放，孤儿问题见 B6）。`_echo_busy_reply` 的 hint 扩一句通用提示（不做持有者探测）：「锁可能被正在执行 script 的 next 持有；中止请 kill 该 next 进程，锁随进程释放」。
- **幂等契约**：workflow 作者必须保证 script 幂等或自守单实例（与既有 chain 脚本约定一致）。写入 [`orca/skills/create-workflow/reference/orca-workflow-contract.md`](../../orca/skills/create-workflow/reference/orca-workflow-contract.md) 三句话：① script 节点会被 at-least-once 重执行，必须幂等/自守单实例；② **bootstrap 期执行的 script 链（含 entry 链）不应推图**（chart 守护在锁外 advance+循环之后才 spawn，`cli.py:1466`；`render_chart` raise → 非零退出被当业务结果走兜底路由；D4 文档化边界，调序留 follow-up）；③ `inputs.iterations` 同时是**单次调用内联 script 链长度上限**（D3）——声明小值的 workflow 长 script 链会撞顶 workflow_failed(internal_error)。

### 2.7 回复契约（next / bootstrap / daemon 三入口一致）

- `auto_executed`：仅当本次调用**成功完成 ≥1 个 script** 时出现：`[{node, exit_code, elapsed, stdout_tail, stderr_tail}]`（顺序 = 执行顺序；首个 script 即失败 → 零成功条目 → 字段省略）。**失败信封同样附**——注入通道钉死一种：`execute_script_inline` 的 `InSessionError` 携带 `auto_executed` 属性（已成功条目），`fail_in_session`（`_step_io.py`）读取注入信封（bootstrap `cli.py:1335` / next `cli.py:1592` / daemon `daemon.py:142` 三个失败出口经同一 `fail_in_session`，单点注入）。「不显性化 ≠ 不可见」对失败路径同样成立。
- 其余字段（done / node / prompt / prompt_file / reason / recoverable / resumed…）语义不变；`prompt` = 下一个 agent 节点的**指针文本（compact 模式）或全量 prompt（inline 回退）** + 驱动协议；`prompt_file` 为渲染后 prompt 文件路径。script 对主 session 透明，只在 auto_executed 报备。
- `_drive_protocol` 文本零改动（主 session 行为不变：读 prompt → 派子代理 → next --output）。

---

## 3. 事件序列与 G2 对齐

含 script 的 workflow（`A(agent) → S(script) → B(agent)`），主 session `next --output '<A 产出>'` 的 tape 增量（各批 emit，同一 flock 临界区）：

```
批1: node_completed(A) → route_taken(A→S)          # advance_step emits
批2: node_started(S, {kind:"script", command})      # executor 首 yield，立即 emit
批3: node_completed(S, {output, elapsed})           # executor 完成 yield
批4: route_taken(S→B) → node_started(B, {"node": B})  # advance_after_script emits
```

与 headless（`orca run`）跑同 workflow 的 `(type, node)` 逐 seq 序列**必须一致**（ns/nc 同源自 executor、逐条 emit 同形；rt 同为编排层 emit）。**失败尾序两路同形**：headless 在 `node_failed` 即 raise（`error` 事件从不落 tape），in-session 丢弃 `error` 后同为 `ns → nf`（in-session 随后 `workflow_failed`，headless 由 drive loop 决定重试或失败——该差异是既定非目标，比对范围限 `(type, node)` 序列）。G2 回归测试钉死（§8）。

---

## 4. 依赖纪律

- `orca/run/step.py` **不新增 import `orca.exec.script` / `orca.exec.factory`**（现状已有的 `orca.exec.error/render` 不动；step 只产出决策信号 `node_kind`，执行在 iface 层）。`advance_after_script` 只依赖既有 `Orchestrator._next_node_for_resume` / `_replay_fold` / `_outputs_acc_from_state` / `_final_outputs`。step→orchestrator 的下划线私有静态方法调用是 run 层内部**既成豁免**（`step.py:819` 现状）；iface→run 的私有 import 禁止不变。
- **D2 扩参**：`Orchestrator._next_node_for_resume` 加可选参数 `inputs: dict | None = None`（默认 `{}` = 现行为，向后兼容；daemon 路径零改动）。in-session 分支 3 与 `advance_after_script` 两调用点传 §2.5 的 `merged_inputs`——修复路由 `when` 引用 `{{ inputs.* }}` 时 in-session `RouteError` 裸崩的既有分叉（问题本体即 `_next_node_for_resume` 内部构造 ctx 时 `inputs={}`，`orchestrator.py:577-579`；headless **live** 经 `_make_ctx` 正常，headless **resume**（from_tape）同样缺 inputs——`orchestrator.py:488/531` 手边有 inputs 未传，属 U2=b follow-up，本 SPEC 不动）。run 层内部改动，无跨层依赖。
- helper（iface 层）依赖：`orca.run.step`（StepResult / advance_after_script）+ `orca.run.lifecycle`（`resolve_max_iter`，`lifecycle.py:158`）+ `orca.exec.factory`（make_executor）+ `orca.events.bus`——方向 `iface → run/exec/events`，合法。
- schema / compile / exec / events 四层**零改动**。

---

## 5. 纯增量不变量（零回归守门）

1. 全 agent workflow 的 tape 事件序列、回复 JSON、prompt 文件内容与改动前**逐字节一致**（`node_kind` 字段不进 tape、不进既有回复字段；env 文件写条件对 node_kind=None 照写不变）。
2. `StepResult.node_kind` 默认 None，所有旧调用方（daemon / 单测）零感知。
3. 非 script 的非 agent kind（set/wait/foreach/parallel/terminate/gate）仍 `unsupported_node_kind`；错误消息文案更新为「仅支持 agent/script」。
4. 既有测试套件**保持绿**，唯一例外：`tests/iface/in_session/test_in_session_cli.py:971` `test_failure_unsupported_node_kind` 的 fixture 把 `kind: agent` 替换为 `kind: script`——落地后 script entry 合法，fixture 显式换为**仍不支持**的 kind（`kind: set` + 必填 `values: {k: "1"}`），**断言不变**（仍断言 `unsupported_node_kind` fail loud）。此为 fixture 对齐，非断言弱化，列入改动清单。

---

## 6. 验收标准（AC，可客观判定）

- **AC1**（基础跑通）：in-session workflow `A(agent) → S(script, exit 0) → B(agent)` 经 `orca <wf> --inputs` + `orca next --run-id --output` 全程推进完成，无 `unsupported_node_kind`；**该次 `next` 调用产生的 tape 增量** `(type, node)` 列表与 §3 批 1–批 4 **逐字相等**。
- **AC2**（连续 script 链透传）：`A(agent) → S1 → S2 → B(agent)`，A 产出后**一次** `next` 即返回 B 的 prompt；S1/S2 引擎内跑完，`auto_executed` 含 2 条按序条目。
- **AC3**（路由分叉）：S 非零退出 → 不失败，`when: output.exit_code == 0` 未命中走兜底分支到 A'；exit 0 走 B'。两分支 E2E 可复现。
- **AC4**（parse_json 判定）：S `parse_json: true` 输出 `{"goal_met": <bool>}`，路由 `when: output.json.goal_met` 分叉正确。**反例**：脚本输出非 JSON（`output.json=None`）+ 防御性写法 `when: "output.json.goal_met is defined and output.json.goal_met"` → 落兜底 route 正常推进；裸引用 `when: output.json.goal_met` → RouteError 裸崩（非 0 退出 + tape 无 workflow_completed，pre-existing 限制的行为钉死）。
- **AC5**（timeout 终态）：S `timeout: 2` 且脚本 sleep 5 → 终态 `workflow_failed`，回复 `{done:true, error_kind:"script_timeout"}`，tape 序列 `ns(S) → nf(S) → workflow_failed`（**无 error 事件**）。
- **AC6**（G2 对齐）：同一含 script 的 wf，**同一 fake `make_executor` 注入两路**（产确定性 output 的既有零 token 手法，先例 `tests/run/test_orchestrator.py` 全 script wf）；in-session 路径驱动至终态后，两路 tape 的**全长** `(type, node)` 序列（含 `workflow_started` / `workflow_completed`）逐项相等（忽略 timestamp/seq/session_id/output 内容）。
- **AC7**（entry=script）：entry 为 script 的 wf 可 bootstrap（直接内联跑 entry）；若 entry script 直通 `$end`（done=True at bootstrap）→ tape 终态 + **`runs/` 无该 run 的 marker**（monkeypatch 断言守护 spawn / web / 注册 not-called，或断言 socket / pidfile / marker 文件不存在）。
- **AC8**（auto_executed 契约）：成功与失败信封字段逐字符合 §2.7（tail ≤500；**成功条目为零 → 字段省略**；未执行 script 的调用无该字段；失败信封不含失败节点条目）。
- **AC9**（at-least-once，真实窗口）：tape 截断至 `ns(S)`（模拟窗口 i：script 执行中 CLI 被杀）→ 下次 `next` **不带 --output** 重新执行 S 并正常推进（tape 允许重复 ns）；**断言恢复调用后 marker.no_output_count 不增**（合规计数取最终 result，中间 emits=[] 不计数）；带 `--output` → `state_corrupt` fail loud（§2.2.4 守卫）。
- **AC10**（零回归）：全 agent 既有 wf 回归保持绿（§5.4 豁免清单外）；daemon `next` 路径同享循环（单测直构 daemon 对象 + `await next()` 验证 script 透传 + try 界包住循环 + **回复含 auto_executed 字段**）。

## 7. 验收用例

- **happy**：AC1/AC2/AC4/AC7/AC8。
- **sad**：AC3（非零退出=业务结果，含 command 引用不存在解释器的 shell exit 127 → 走兜底路由，**非** internal_error）/ AC5（timeout 终态）/ render 失败（command 模板引用未定义的 `{{ inputs.* }}` 字段 → Jinja Undefined → ExecError(render) → internal_error 终态；shell spawn 的 OSError 罕见路径同映射）/ 宿主代交卷（AC9 后半，state_corrupt）。
- **edge**：AC6 / AC9（窗口 i）/ script 链后直接 `$end`（一次 next 返 done，bootstrap 短路 §2.5）/ 路由引用 `{{ inputs.* }}`（D2 修复后不再裸崩，RouteError 语义正常）/ script command 引用 `{{ inputs.* }}`（M1 修复后正常渲染，不再假 internal_error）/ 循环上限（S 路由回自身的 wf → max_iter 撞顶 workflow_failed(internal_error)，防 flock 死锁）/ 窗口 0 与 ii（文档化 fail loud，不可逆）。

## 8. 测试与 E2E

- **单测**（WSL `.venv` pytest，项目惯例）：step.py 四分支 + `advance_after_script`（纯决策，fake tape）；helper（真 bash echo/exit/sleep + 事件批次断言 + error 事件丢弃）；共享循环（fake executor 注入 + max_iter 顶）；G2 对齐（§AC6）；零回归（既有套件 + §5.4 fixture 更新）。
- **E2E（真机，WSL）**：按项目 in-session 惯例——opencode + deepseek-v4-flash 会话内主 agent 触发 tars skill → `orca` CLI 驱动一个含 script 链 + 路由分叉（exit_code / parse_json 两型）的小 workflow（fixture wf 放 `workflows/`，产物落 playground 侧或 per-run artifacts）；逐节点核对 tape 事件序 + auto_executed 摘要 + agent 产出质量（不只看 node_completed）。timeout 场景用受控 sleep 脚本。E2E 后按惯例清理 fixture run。

## 9. 决策记录

| # | 决策 | 依据 |
|---|---|---|
| Q1 script 产出不合 output_schema | **作废**——ScriptNode 无 output_schema 字段（`extra="forbid"`），结构化输出走 parse_json | 2026-08-21 事实核查 |
| Q2 同步 vs 异步 | 同步阻塞 v1（CLI 进程即执行者）；异步化 follow-up | 用户 2026-08-21 拍板 |
| Q3 首例 goal_check | 本 SPEC 只做通用能力；goal_check 归后续使用方 | 用户任务范围 |
| Q4 node_kind 字段 vs cli 自查 | `StepResult.node_kind` 显式返回 | step 是决策单点 |
| ~~Q5 逐条 emit vs 攒批~~ | **修订（D1=a）**：ns 先行立即 emit；nc/nf 各自成批；executor 尾随 error 事件丢弃 | round-1 评审 B1/B2：攒批使 at-least-once 崩溃态不可达 + 失败序列与 headless 不同形 |
| Q6 entry=script | 支持 | AC7 |
| 可见性 | `auto_executed` 回复契约（成功 + 失败信封都报备） | 用户 2026-08-21：「不显性化 ≠ 不可见」 |
| at-least-once | script 必须幂等，写入 `orca-workflow-contract.md` | 用户 2026-08-21 确认 |
| D1 崩溃恢复方向 | **a**：ns 先行 emit（headless 同形、改动最小、窗口 i 可恢复）；窗口 ii 文档化 fail loud | round-1 评审推荐；2026-08-21 goal 授权自主采纳 |
| D2 路由 ctx inputs 缺失 | **修**：`_next_node_for_resume` 加可选 `inputs` 参数（默认 `{}` 向后兼容），in-session 两调用点传 §2.5 的 `merged_inputs` | round-1 评审推荐；修既有 in-session 裸崩分叉 |
| D3 循环上限 | **复用 `resolve_max_iter(wf, merged_inputs)` 作 per-call 内联 script 数上限**（与 headless 共享解析逻辑，**非同一预算语义**——headless 计全节点 dispatch，本处计单次调用 script 数）；超限 workflow_failed(internal_error) | round-1 评审推荐；round-2 m-D3 措辞修正 |
| D4 entry-script 推图边界 | **文档化**（workflow-contract 注明 bootstrap 期 script 链无 chart 守护）；调序留 follow-up；`next` 路径守护**前置 ensure**（仅当即将进 script 执行时，尾部 ensure 原位原条件不动，round-2 M2 修订） | round-1 评审推荐 + round-2 M2 钉死 |
| U1 长 script 持 flock 锁死 stop | **a 文档化接受**：§2.6-C 已知限制 + busy reply 通用 hint（不做持有者探测）；kill next 进程即释放锁 | round-2 评审推荐；2026-08-21 goal 授权自主采纳 |
| U2 headless resume 缺 inputs | **b follow-up 单独修**：修它会改变既有 wf 的 resume 行为，违反纯增量硬约束；本 SPEC 仅修 D2 引用措辞 | round-2 评审推荐；2026-08-21 goal 授权自主采纳 |

## 10. 实施改动清单（coder-agent 交付范围）

| 文件 | 改动 |
|---|---|
| `orca/run/step.py` | §2.1 字段、§2.2 四点、§2.3 新函数、`ERR_SCRIPT_TIMEOUT` 常量、错误文案 |
| `orca/run/orchestrator.py` | `_next_node_for_resume` 可选 `inputs` 参数（D2，默认现行为） |
| `orca/iface/in_session/_step_io.py`（或新 `_inline_script.py`） | §2.4 helper + §2.5 共享循环 + `fail_in_session` 读 `exc.auto_executed` 注入失败信封（m-Channel 单点） |
| `orca/iface/in_session/cli.py` | bootstrap/next 接共享循环；守护前置 ensure（D4/M2，仅 next）；env 条件（B3）；bootstrap 终态短路（B8，含注册跳过）；`merged_inputs` 派生（M1）；回复拼 `auto_executed`（**成功信封**；失败信封由 `fail_in_session` 单点注入）；busy reply 通用 hint（U1） |
| `orca/iface/in_session/daemon.py` | `next` 接共享循环 + try 界扩包（B4）+ **reply 拼 `auto_executed`**（E-3；daemon 路径 cli_inputs ≡ `self.inputs`，`daemon.py:62`） |
| `tests/iface/in_session/test_in_session_cli.py` | §5.4 fixture 更换（kind: set） |
| `orca/skills/create-workflow/reference/orca-workflow-contract.md` | §2.6-C 两句话（幂等 + entry-script 推图边界） |
| 新测试文件（按既有 tests/ 布局） | §8 单测全套 |
| schema / compile / exec / events 层 | **零改动**（§4） |

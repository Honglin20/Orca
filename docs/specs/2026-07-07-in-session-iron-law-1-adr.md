# ADR —— 铁律 1 扩展：in-session 写者作为 Tape 第一个跨进程写者

> **状态**：ADR v3（2026-07-07，经第二轮 spec-review-adversarial 审视后修订；v2 的"daemon 唯一形态"前提被 spike 推翻——主 UX 改 per-call 薄 CLI，v2 I3.3 的 pid 探活对 CLI 字面不适用，本版拆 I3.3a/b）。
> **类型**：架构底线扩展（铁律 1「唯一真相源」的写者集合扩大），约束 in-session shell 的实现。
> **依据**：[CLAUDE.md](../../CLAUDE.md) 铁律 1（唯一真相源）· [in-session-shell-design-draft.md](in-session-shell-design-draft.md) v7 · spike（2026-07-07，`/tmp/orca-spike`+`/tmp/orca-f4`+`/tmp/orca-f10`）· 两轮 spec-review-adversarial。
> **触发**：in-session shell 需要 tape 写者运行在 drive 进程之外（交互宿主进程触发），而现行铁律 1 把 tape 写者限定在 drive 进程内。本 ADR 正式论证（Q15：不得在草稿脚注悄悄改铁律）。
> **范围**：① 申明铁律 1 字面与精神；② 论证跨进程写者（**两种形态**：长驻 daemon / per-call 薄 CLI）如何不破坏精神；③ 不变量与守门；④ CLAUDE.md 铁律 1 新措辞；⑤ 用户命令面。
> **不在范围**：CLI 参数 / 事件字段 / plugin TS 细节（→ in-session shell phase SPEC）。

## 修订历史

| 版本 | 日期 | 修订内容 | 审视 |
|---|---|---|---|
| v1 | 2026-07-07 | 初版：4 不变量 + 3 被否方案 + CLAUDE.md 措辞 | — |
| v2 | 2026-07-07 | 闭环 B1/B2/B3/M1-M4（见 v2 行） | spec-review r1 |
| v3 | 2026-07-07 | **写者形态放宽**：主 UX 从长驻 daemon 改 per-call 薄 CLI（spike 实证）；**§2 I3.3 拆 I3.3a（长驻 daemon，无头 CI）/ I3.3b（per-call CLI，主 UX）**——v2 单一 pid-探活规约对 CLI 字面不适用；§1 决策放宽"写者形态不限"；§5 措辞同步。不变量 I1/I2/I3.1/I3.2/I3.4/I4 不变。 | spec-review r2（F3 blocker 闭环） |

---

## §0. 背景：铁律 1 防的是什么 + 现状写者实证

CLAUDE.md 铁律 1（唯一真相源）把底线落成一句操作约束。AgentHarness 的病根是**多真相源漂移**（多 store / sidecar 各写各的真相，live/replay/UI 永远对不齐）。Orca 用"单 tape + 幂等 reducer + 一条读路径"根治，"tape 写者受约束"是写侧护栏。

**现状写者实证**（v2 修正 v1 的事实错误）：tape 的写者从来就不止 `orchestrator` + `executor_adapter` 两个模块。grep 实证，drive 进程内经 `bus.emit` 写 tape 的模块至少：
- `orca/run/orchestrator.py`（workflow_started/completed/route_taken + parallel/foreach 子事件，15 处 emit）
- `orca/run/executor_adapter.py`（node_started/completed 桥接）
- `orca/gates/{dialog,interrupt,handler}.py`（dialog_*/interrupt_*/human_decision_* 事件）
- `orca/exec/retry.py` + `orca/run/retry.py`（retry_started/succeeded/exhausted）
- `orca/iface/web/run_manager.py:353`（`workflow_cancelled`，注释明示"写 tape 唯一真相"）

**关键定性（v2 论证基石）**：上述全部写者都在**同一个 drive 进程、同一个 asyncio loop、同一个 FD 表**内——事件天然串行、共享同一崩溃面、共享同一 `Tape` 句柄。它们被算作"唯一写处"合法，是因为**同一进程内共享 bus + 同 schema + 同 loop 串行**，而非"字面一个调用点"。**铁律 1 的真实含义 = 唯一写逻辑 + 唯一写格式 + 单一进程内串行写**。

**本 ADR 引入的新维度**：tape 写者运行在 drive 进程之外（**跨进程**），由交互宿主触发。v3 认可**两种跨进程写者形态**：
- **per-call 薄 CLI**（v3 主 UX）：每次 hook 触发 spawn 短命 CLI（`orca in-session bootstrap/next`），open(resume=True)→flock→advance_step→emit→close。无长驻进程。
- **长驻 daemon**（v3 无头 CI）：MCP server 进程，持续持锁。

两者都引入同进程没有的新风险（独立崩溃面、独立 FD 表、并发真可能性、半写）。**本 ADR 的论证不靠"跨进程与同进程天然等价"（那是偷换），而靠 §2 I3 三层保证把跨进程并发压回到"任一 tape 文件同一时刻单写者"，使任一形态的写语义与同进程串行写对齐。** 两形态的差异仅在**锁的生命周期护栏**（§2 I3.3a vs I3.3b），不变量 I1/I2/I3/I3.4 共用。

> §0 删去 v1 的"早已是字面代理的等价论证"——该论证被 B1 实证削弱且不严谨。同进程多写者只是历史证据，不是论证基石。

## §1. 决策

**允许 in-session 写者（per-call 薄 CLI 或长驻 daemon）作为 tape 的跨进程 sanctioned 写者**，条件是同时满足 §2 全部不变量（I1-I4，含 I3.3 按形态二选一 + I3.4 半写恢复）。写者与 drive 进程内的写者身份对等——都是"经共享 `step.py` helper、同 schema、同 `bus.emit→Tape.append` 的 sanctioned emitter"，差异仅在进程边界、驱动方式（loop 内迭代 vs hook 单步触发）、锁生命周期。

**v3 形态选择**：主 UX（交互 opencode plugin / CC hook）走 **per-call 薄 CLI**（I3.3b）——spike 实证可行、无孤儿锁反向问题；无头 CI / 长跑批处理走 **长驻 daemon**（I3.3a）。两形态共用 `advance_step` 决策与 helper，不两套真相源。

**方案 E（v2 采纳，v3 保留）**：跨进程写者（CLI 与 daemon）**单独使用** `orca/run/step.py` 的共享 helper；**`drive_loop` 零改动**（不重构、不切 helper）。理由：用户底线 #4"绝不能影响现有功能"优先级高于 CLAUDE.md DRY #6；drive_loop 重构（15 处 emit 跨多调用栈）对稳定功能是 regression 风险，不值当。DRY 违反（drive_loop 内联 emit 与跨进程 helper 短期并存）登记为 known-debt，留独立 phase 处理。

## §2. 不变量（缺一即违反铁律 1，必须守门）

### I1. 唯一写逻辑（仅对跨进程写者强制；drive_loop v3 不动）

跨进程写者（CLI 与 daemon）**不得内联事件构造**。所有事件由 `orca/run/step.py` 的共享纯函数产出：
- `_init_workflow(wf, inputs, run_id) -> list[Event]`
- `_advance_step(state, wf, current, output) -> list[Event]`
- `_finalize_workflow_completed(state, outputs) -> list[Event]`
- `_truncate_trailing_partial(path) -> int`（半写恢复，见 I3.4；与 `from_tape` 共享）

> v3 注：drive_loop（含 gates/retry/web-cancel）**保持现状内联 emit**（方案 E）。故 I1 仅约束跨进程写路径（CLI + daemon）。短期 drive_loop 与跨进程 helper 的 emit 逻辑不 DRY——方案 E 已知代价（known-debt），换取零回归。**CLI 与 daemon 必须共享同一组 step.py helper，不两套写逻辑**（v3 铁律：跨进程写者内部也只一套接口）。

### I2. 唯一写格式

跨进程写者经**同一个** `EventBus.emit`/`emit_batch` → `Tape.append`/`Tape.append_batch` 落盘（spec-review r2 B1 闭环：`append_batch` 为单次 write 原子化的 sanctioned 批写路径扩展），事件走**同一个** `orca/schema/event.py` 的 `EventType` Literal + payload schema（**不新增事件类型**，闭环铁律 8）。CLI 进程内与 daemon 进程内各持有标准 `Tape`/`EventBus` 实例（同一类，非两份格式）。`append_batch` 与 `append` 共用同一 `_lock` + Event 校验 + seq 分配，差异仅"一次 write+flush 落多行"——drive_loop 继续用单条 `emit`，批写仅 in-session 写者用。

### I3. 无并发写者（任一 tape 文件同一时刻单写者）—— 跨进程核心护栏

三层保证 + 失败面枚举（v3 按写者形态二选一）：

**I3.1 执行路径互斥（根本保证）**：一个 workflow run 的 tape 终身只有一个驱动者——要么 drive 进程（`orca run`/`resume`/CLI/Web/MCP 三壳各自的 drive_loop），要么 in-session 写者（per-call CLI 或 daemon）。它们是跑同一 workflow 的**替代方式，不并发**。

**I3.2 run 作用域隔离**：每个 run 独立 tape 文件 + run_id（`gen_run_id`）。`orca run wf.yaml` 与 in-session 跑同 wf.yaml 是两个 run、两份 tape，不撞文件。

**I3.3 进程级 flock（v3 拆 a/b）**：跨进程写者对 tape 文件 `flock(LOCK_EX|LOCK_NB)`，拿不到即按形态规约处理。`Tape._lock`（`tape.py:108`）是 threading lock，**不保护跨进程**——跨进程必须 flock。NFS / 网络盘 POSIX flock 语义不保证：**所有形态**启动检测 tape 路径所在 FS，非本地 FS（NFS / synced / fuse）**fail loud 拒绝**（D2=a）。按形态：

**I3.3a 长驻 daemon（无头 CI）——锁生命周期护栏**：daemon 持续持锁，必须处理"存活 daemon 持锁"类失败：
- **孤儿持锁**：宿主被 kill、daemon 存活继续持锁 → 新 daemon 起不来。**规约**：daemon 启动写 pid 文件；新 daemon 读 pid 文件探活（`os.kill(pid,0)`），pid 不存在则视为孤儿锁、强制接管。
- **SIGKILL / OOM**：`SIGTERM`/`SIGINT` handler 只做 cleanup（释放 flock + close tape），不 swallow；依赖 OS 对 SIGKILL 的 fd 回收兜底。
- **FD 泄漏 / asyncio cancel 跳过 cleanup**：`try/finally` 包 flock 释放；`atexit` 注册；`CancelledError` 走 cleanup path。

**I3.3b per-call 薄 CLI（主 UX）——锁生命周期护栏**：CLI 短命（open→emit→close），**无 pid 文件、无 os.kill 探活、无孤儿持锁反向问题**（无长驻持锁者）。护栏简化为：
- **flock 随进程退出释放**：本地 FS 上 CLI 进程结束（含 SIGKILL）→ OS 回收 fd → flock 自动释放。无需 pid 探活。
- **LOCK_NB 拿不到锁 = busy**：两 idle 紧邻触发使两 CLI 撞锁时，后到者 `LOCK_NB` 失败 → CLI **返 `{done:false, reason:"busy"}` + 0 退出**（**不 fail loud**，非错误），调用方（plugin/hook）下一轮 idle 重试。规约：宿主侧 event hook 串行保证通常不撞，busy 仅作竞态兜底。
- **`try/finally` 包 flock 释放**：emit 完/异常都 close fd。
- 仍在 `atexit` 注册最终释放（防御 fd 泄漏）。

> v2 单一 pid-探活规约对 per-call CLI 字面不适用（无 daemon、无 pid 文件）——v3 拆 a/b 闭环此 spec-review r2 F3 blocker。两形态共用 flock + 仅本地 FS + try/finally + atexit；差异仅在"是否有长驻持锁者需探活"。

### I3.4 半写恢复（v2 新增，闭环 B2；v3 两形态共用）

跨进程写者写到一半崩溃（SIGKILL/OOM/断电）→ tape 末尾半行。重启必须处理，否则下次 append 接到残行之后 → 坏行 → reducer fold json decode 失败 → seq 空洞。

**规约**：跨进程写者**必须**以 `Tape(path, resume=True)` 打开（触发 `_truncate_trailing_partial` 截断末尾残行 + 重算 `_last_seq`），**禁止** `resume=False` 直接续写。截断逻辑与 `from_tape`（resume 路径）**共享同一纯函数** `_truncate_trailing_partial`（DRY），但跨进程写者**不复用** `from_tape` 的 typed exception 与 resume 检测——用 `replay_state` + `_next_node_from_tape` 自判状态。

> **注（spec-review r2 F1）**：`_truncate_trailing_partial` 只截**字节级残行**（json decode 失败的末行），**不截完整事件**。若 SIGKILL 落在"nc 已完整落盘、rt 未落"之间，resume 不回退 nc。该窗口的消除由 **draft §6 的单次 write 原子化**保证（`[nc,rt,ns]` 合一次 `tape.append`+flush，POSIX 本地 FS 原子），不由 resume 负责。本 ADR 不涉及 emit 批次原子化（属 draft/Tape 层）。

### I4. 守门机制（防第三写者偷渡，v2 闭环 B1 G1）

**G1（emit 点基线快照）**：CI 测试维护一份"合法 emit 调用点基线文件清单"（v3 基线含 `orca/run/{orchestrator,executor_adapter,step}.py` + `orca/gates/` + `orca/exec/retry.py` + `orca/run/retry.py` + `orca/iface/web/run_manager.py` + `orca/iface/in_session/`（CLI 主写者 + daemon 无头 CI emit 桥））。CI grep 全仓 `bus.emit(` / `Tape.append(` 调用点，与基线 diff——**新增 emit 点必须显式登记进基线**，否则测试红。

**G2（编排骨架对齐回归，v3 修订）**：跨进程写者跑某 workflow 的 tape 与 `orca run` 跑同 workflow 的 tape，**只比编排骨架事件** `workflow_started/node_started/node_completed/route_taken/workflow_completed` 的 `(type, node, 相对 seq 序)`——**不比 `data.output`**（in-session 提取 `<task_result>` 干净 output vs `orca run` 模型完整文本，by design 不同）、**不比 executor 内部事件**（`agent_step_*/agent_tool_*/agent_usage/prompt_rendered` 仅 `orca run` 有；in-session 绕过 executor、宿主 subagent 即执行器，by design 无）。逐条全等跨形态结构性不可能（e2e 实证 38 vs 11 事件）；骨架对齐是 in-session 与 drive_loop 行为一致的真守门。**CLI 形态与 daemon 形态都要过 G2**（同一 helper）。

## §3. 方案评估（v3 增 per-call CLI 形态）

| 方案 | 处置 | 理由 |
|---|---|---|
| **E. 跨进程写者单独用 helper，drive_loop 不动** | **✅ 采纳（v2）** | 零回归（用户底线 #4）；DRY 违反登记 known-debt。代价：drive_loop 与跨进程 helper emit 短期不 DRY |
| **F. 主 UX 走 per-call 薄 CLI（无头 CI 保留 daemon）** | **✅ 采纳（v3）** | spike 实证 plugin+CLI 可行；per-call 无孤儿持锁反向问题（I3.3b 简化）；CLI 与 daemon 共 step.py helper（不两套写逻辑）。推翻 v2「daemon 唯一形态」前提 |
| A. 跨进程写者不写 tape，事件回吐宿主写 | ❌ 否决 | 宿主写 tape = 重引入 sidecar 多真相源；宿主无法用 Python `Tape` 抽象 → 必然直写 JSONL（破坏 I2） |
| B. drive_loop 也改走跨进程写者 | ❌ 否决 | 改 `orca run`/`resume` 语义，违反纯增量 |
| C. 不放宽铁律，放弃 in-session | ❌ 否决 | spike 已验证可行，本 ADR 证明可保精神放宽 |
| D. 字面合规：emit 代码物理挪进 orchestrator.py | ❌ 否决 | 跨进程调模块函数无意义；治标不治本 |
| ~~drive_loop 同期重构调 helper（v1 默认）~~ | ❌ 否决（B3） | 15 处 emit 跨多调用栈，regression 风险高，违反底线 #4。被方案 E 替代 |
| ~~主 UX = 长驻 daemon（v2 默认）~~ | ❌ 否决（v3） | 孤儿锁反向 + pid 探活复杂；per-call CLI 更简（spike 证）。daemon 降级无头 CI |

## §4. 后果

**正面**：
- in-session shell 在铁律 1 下合法落地（v3 主 UX = per-call CLI，spike 验证；daemon 形态保留无头 CI）。
- 跨进程 emit 逻辑集中在 helper（`step.py`），CLI 与 daemon 共用，可独立单测。
- per-call CLI 消解 v2 daemon 的孤儿锁反向问题（I3.3b 护栏更简）。
- G1 基线快照 + G2 序列对齐把"唯一写逻辑/格式"显式化、可测化。

**负面 / 新约束**：
- 铁律 1 **字面**变化（见 §5），CLAUDE.md 措辞同步更新（本 ADR 授权）。
- 跨进程 flock + 仅本地 FS（两形态共用）；daemon 形态额外 pid 探活（I3.3a），CLI 形态额外 busy 语义（I3.3b）。
- 半写恢复强制 `Tape(resume=True)` 打开（I3.4，两形态共用）；**emit 批次原子化**（`[nc,rt,ns]` 单次 write）由 draft §6 落实，不在本 ADR。
- **DRY 短期违反**（方案 E 代价）：drive_loop 内联 emit 与跨进程 helper 并存，登记 known-debt，独立 phase 处理。
- G1 基线快照需维护（新增 emit 点要登记）；G2 须 CLI + daemon 双形态都过。

## §5. CLAUDE.md 铁律 1 措辞更新（本 ADR 授权）

| | 措辞 |
|---|---|
| **现行** | "事件全程 `bus.emit` 落 Tape（**唯一写 Tape 处**，铁律 1）" |
| **新（本 ADR 授权，v3）** | "事件落 Tape 的**写逻辑唯一**（drive 进程内各模块 + in-session 跨进程写者经各自 sanctioned 路径，跨进程写者走 `step.py` 共享 helper）、**写格式唯一**（同一 `EventType` schema + `bus.emit→Tape.append`）；任一 tape 文件**同一时刻单写者**（执行路径互斥 + run 作用域 + `flock` 兜底）；**跨进程写者仅 in-session 一类**（per-call 薄 CLI 主 UX / 长驻 daemon 无头 CI，共 step.py helper），启动必经 `Tape(resume=True)` 半写恢复 + 仅本地 FS（daemon 形态额外 pid 探活）。禁止任何写者绕过 sanctioned 路径自造事件。" |

> 措辞更新落 CLAUDE.md 铁律 1 + `orchestrator.py:5` / `executor_adapter.py:9` 注释，由 in-session shell 实现 PR 一并改，引用本 ADR。

## §6. 用户命令面（v2 新增，闭环 M4；v3 按 CLI 形态重写）

用户侧最小可用命令（phase SPEC 落实细节，本 ADR 定契约）：

- **`orca in-session bootstrap <wf.yaml>`** —— 起一个 in-session run 的首步：`gen_run_id` + 生成 tape 路径（`<rundir>/<run_id>.jsonl`）+ 写激活标记 + per-call flock emit `workflow_started`+`node_started(entry)` → stdout entry prompt JSON。**幂等**：同 session 同 wf 已 bootstrap 则复用 run_id，不重发。
- **`orca in-session next --tape --run-id [--output]`** —— 推进一步：per-call flock + `advance_step` + emit（单次 write 原子）→ stdout `{done,node?,prompt?,reason?}` JSON。LOCK_NB 失败返 `{done:false,reason:"busy"}` 0 退出。
- **`orca in-session status [<run_id>]`** —— 读 tape replay_state 报进度。
- **`orca in-session stop <run_id>`** —— 清激活标记 + per-call emit `workflow_cancelled`（与 web run_manager 同款"写 tape 唯一真相"）。
- **`orca in-session serve`**（无头 CI）—— 长驻 daemon 形态（I3.3a），跑无人值守长 workflow。

> 崩溃续跑：per-call CLI 天然无状态，`bootstrap`/`next` 以 `Tape(resume=True)` 打开即续；无需显式 resume 命令（daemon 形态保留 `resume` 兜底）。

## §7. 与其他文档 / 架构的关系

- [in-session-shell-design-draft.md](in-session-shell-design-draft.md) §2.3/§4.1/§8：本 ADR 落实其铁律 1 前置。SPEC 须同步方案 E（drive_loop 不动）+ 方案 F（主 UX per-call CLI）。
- [interface-convergence-adr.md](2026-07-06-interface-convergence-adr.md)：
  - **D5（事件类型 1 套）**：本 ADR 不新增事件类型，一致 ✅。
  - **D7（子进程生命周期 → ProcessRegistry）**：**跨进程写者不纳入 ProcessRegistry**。ProcessRegistry 治理 executor 子进程（spawn，`Adapter.cancel()` 委托）；per-call CLI 是 hook 触发的短命子进程、daemon 是宿主连的 MCP server，语义不匹配。生命周期独立设计（CLI 无状态 / daemon pid 探活 + §6 命令面）。
- [phase-10-mcp.md](phase-10-mcp.md) §0.1 铁律 1（单进程单 RunManager）：跨进程写者是**独立进程**，不并入 phase-10 MCP server 单进程模型；phase-10 适用范围不受影响。
- **opencode 双模式共存**：`orca run --backend opencode`（spawn 子进程，drive_loop 模式）与 in-session（opencode plugin 触发 per-call CLI / 无头 daemon）**互斥**——同一 run 不可同时跑两种（一 run 一 tape，flock 保证），不同 run 各自独立。命令名（`run` vs `in-session`）清楚区分。

## §8. 证据

- **Spike（2026-07-07）**：per-call CLI 写者形态经 `/tmp/orca-spike`+`/tmp/orca-f4`（3 节点 task-subagent 链）+`/tmp/orca-f10`（task tool_result payload）实测可跑；真 tape 写入（helper + flock + 半写恢复）正确性由 §2 G2 回归测试在 phase 实现期证明。
- **现行写者多发性实证（v2 修正）**：`orca/run/orchestrator.py`（15 处）+ `executor_adapter.py` + `orca/gates/{dialog,interrupt,handler}.py` + `orca/exec/retry.py` + `orca/run/retry.py` + `orca/iface/web/run_manager.py:353`——全部同进程内经 `bus.emit` 共享 schema。证明"唯一写处"早已是"同进程内多写者共享 bus/schema"，本 ADR 扩展的是**跨进程**这一新维度。
- **半写行为源码**：`orca/events/tape.py:167-173`（replay fail-soft 跳过残行）、`:222-301`（`_truncate_trailing_partial` 截断 + 重算 last_seq，**仅截字节级残行，不截完整事件**——spec-review r2 F1 实证）。跨进程写者必须复用此截断（I3.4）；emit 批次原子化由 draft §6 落实。
- **跨进程锁必要性**：`tape.py:108` `Tape._lock` 为 threading lock，不含跨进程语义 → 跨进程必须 flock（I3.3）。

## §9. 实施前置条件（进 phase SPEC 前必须满足）

1. **B1 闭环**：✅ v2 §0/§8 写者清单实证修正 + §2 I4 G1 基线快照化（含 gates/retry/web）。
2. **B2 闭环**：✅ v2 §2 I3.4 半写恢复 + 共享 `_truncate_trailing_partial`。
3. **B3 闭环**：✅ v2 §1/§3 方案 E 采纳（跨进程写者单独用 helper、drive_loop 零改动）。
4. **M1-M4 闭环**：✅ v2 §2 I3.3 失败面 / §7 D7 / §7 opencode 双模式 / §6 命令面。
5. **D2/D3 决策**：✅ D2=a（仅本地 FS）/ D3=a（不纳入 ProcessRegistry）。
6. **F3 闭环（v3）**：✅ §2 I3.3 拆 a/b——per-call CLI 形态护栏（I3.3b）字面适用，不再依赖 v2 pid 探活。
7. **CLAUDE.md 铁律 1 措辞**：实现 PR 随 v3 §5 措辞更新。

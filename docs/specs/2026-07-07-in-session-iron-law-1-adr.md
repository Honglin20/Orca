# ADR —— 铁律 1 扩展：in-session daemon 作为 Tape 第一个跨进程写者

> **状态**：ADR v2（2026-07-07，经 spec-review-adversarial 对抗审视后修订；v1 的 3 blocker（B1/B2/B3）+ 4 major（M1-M4）已闭环）。
> **类型**：架构底线扩展（铁律 1「唯一真相源」的写者集合扩大），约束 in-session shell 的实现。
> **依据**：[CLAUDE.md](../../CLAUDE.md) 铁律 1（唯一真相源）· [in-session-shell-design-draft.md](in-session-shell-design-draft.md) v3 · Demo 4 实测（2026-07-07）· spec-review-adversarial v1 判决（conditional-pass）。
> **触发**：in-session shell 需要 daemon（MCP server 进程）写 tape，而现行铁律 1 把 tape 写者限定在 drive 进程内。本 ADR 正式论证（Q15：不得在草稿脚注悄悄改铁律）。
> **范围**：① 申明铁律 1 字面与精神；② 论证 daemon 作为第一个**跨进程**写者如何不破坏精神；③ 不变量与守门；④ CLAUDE.md 铁律 1 新措辞；⑤ 用户命令面。
> **不在范围**：daemon RPC schema / CLI 参数 / 事件字段（→ in-session shell phase SPEC）。

## 修订历史

| 版本 | 日期 | 修订内容 | 审视 |
|---|---|---|---|
| v1 | 2026-07-07 | 初版：4 不变量 + 3 被否方案 + CLAUDE.md 措辞 | — |
| v2 | 2026-07-07 | 闭环 B1（写者清单实证修正 + G1 白名单基线快照化）/ B2（半写恢复 I3.4 + 共享截断纯函数）/ B3（采纳方案 E：daemon 单独用 helper、drive_loop 零改动）/ M1（跨进程失败面 4 模式 + atexit/signal/pidfile + 仅本地 FS）/ M2（D7 不纳入 ProcessRegistry）/ M3（opencode 双模式互斥声明）/ M4（用户命令面 `orca in-session start/status/stop`） | spec-review-adversarial（grep 实证 + tape 半写源码 + drive_loop emit 面 + interface-convergence D7） |

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

**本 ADR 引入的新维度**：daemon 是 tape 的**第一个跨进程写者**。跨进程引入了同进程没有的新风险（独立崩溃面、独立 FD 表、并发真可能性、半写）——这些是 v1 没充分覆盖、v2 §2 I3/I3.4 重点处理的。**本 ADR 的论证不靠"跨进程与同进程天然等价"（那是偷换），而靠 §2 I3 三层保证把跨进程并发压回到"任一 tape 文件同一时刻单写者"，使 daemon 的写语义与同进程串行写对齐。**

> §0 删去 v1 的"早已是字面代理的等价论证"——该论证被 B1 实证削弱且不严谨。同进程多写者只是历史证据，不是论证基石。

## §1. 决策

**允许 `orca in-session serve` daemon（MCP server 进程）作为 tape 的第一个跨进程 sanctioned 写者**，条件是同时满足 §2 全部不变量（I1-I4，含 v2 新增 I3.4）。daemon 与 drive 进程的写者身份对等——都是"经共享 helper、同 schema、同 `bus.emit→Tape.append` 的 sanctioned emitter"，差异仅在进程边界与驱动方式（loop 内部迭代 vs RPC 单步触发）。

daemon 形态已由 Demo 4 定型：MCP server（stdio JSON-RPC），opencode/CC 经 MCP 连接。

**方案 E（v2 采纳，闭环 B3）**：daemon **单独使用** `orca/run/step.py` 的共享 helper；**`drive_loop` 零改动**（不重构、不切 helper）。理由：用户底线 #4"绝不能影响现有功能"优先级高于 CLAUDE.md DRY #6；drive_loop 重构（15 处 emit 跨多调用栈）对稳定功能是 regression 风险，不值当。DRY 违反（drive_loop 内联 emit 与 daemon helper 短期并存）登记为 known-debt，留独立 phase 处理。

## §2. 不变量（缺一即违反铁律 1，必须守门）

### I1. 唯一写逻辑（仅对 daemon 强制；drive_loop v2 不动）

daemon **不得内联事件构造**。所有事件由 `orca/run/step.py` 的共享纯函数产出：
- `_init_workflow(wf, inputs, run_id) -> list[Event]`
- `_advance_step(state, wf, current, output) -> list[Event]`
- `_finalize_workflow_completed(state, outputs) -> list[Event]`
- `_truncate_trailing_partial(path) -> int`（半写恢复，见 I3.4；与 `from_tape` 共享）

> v2 注：drive_loop（含 gates/retry/web-cancel）**保持现状内联 emit**（方案 E）。故 I1 仅约束 daemon 写路径。短期 drive_loop 与 daemon 的 emit 逻辑不 DRY——这是方案 E 的已知代价（known-debt），换取零回归。

### I2. 唯一写格式

daemon 经**同一个** `EventBus.emit` → `Tape.append` 落盘，事件走**同一个** `orca/schema/event.py` 的 `EventType` Literal + payload schema（**不新增事件类型**，闭环铁律 8）。daemon 进程内持有标准 `Tape`/`EventBus` 实例。

### I3. 无并发写者（任一 tape 文件同一时刻单写者）—— 跨进程核心护栏

三层保证 + v2 失败面枚举：

**I3.1 执行路径互斥（根本保证）**：一个 workflow run 的 tape 终身只有一个驱动者——要么 drive 进程（`orca run`/`resume`/CLI/Web/MCP 三壳各自的 drive_loop），要么 daemon（in-session shell）。二者是跑同一 workflow 的**替代方式，不并发**。

**I3.2 run 作用域隔离**：每个 run 独立 tape 文件 + run_id（`gen_run_id`）。`orca run wf.yaml` 与 in-session 跑同 wf.yaml 是两个 run、两份 tape，不撞文件。

**I3.3 进程级 flock + 跨进程失败面（v2 闭环 M1）**：daemon 启动对 tape 文件 `flock(LOCK_EX|LOCK_NB)`，拿不到即 fail loud。`Tape._lock`（`tape.py:108`）是 threading lock，**不保护跨进程**——跨进程必须 flock。跨进程引入而同进程没有的失败模式，daemon 实现必须显式处理：
- **孤儿持锁**：宿主（opencode/CC）被 kill，daemon 存活继续持锁 → 新 daemon 起不来。**规约**：daemon 启动写 pid 文件；新 daemon 起来读 pid 文件探活（`os.kill(pid,0)`），pid 不存在则视为孤儿锁、强制接管（重写 pid 文件 + 重新 flock）。
- **NFS / 网络盘**：POSIX flock 在 NFS 上语义不保证。**规约**：daemon 启动检测 tape 路径所在 FS，非本地 FS（NFS / synced folder / fuse）**fail loud 拒绝**（D2=a）。
- **SIGKILL / OOM**：OS 回收 fd 释放 flock（本地 FS 保证）；但若 daemon 装了 signal handler 把 SIGTERM 转成 swallow 则不释放。**规约**：`SIGTERM`/`SIGINT` handler 只做 cleanup（释放 flock + close tape），不 swallow；依赖 OS 对 SIGKILL 的 fd 回收作最后兜底。
- **FD 泄漏 / asyncio cancel 跳过 cleanup**：`try/finally` 包 flock 释放；`atexit` 注册最终释放；daemon 主循环对 `CancelledError` 捕获后走 cleanup path。

### I3.4 半写恢复（v2 新增，闭环 B2）

daemon 写到一半崩溃（SIGKILL/OOM/断电）→ tape 末尾半行。daemon 重启必须处理，否则下次 append 接到残行之后 → 坏行 → reducer fold json decode 失败 → seq 空洞。

**规约**：daemon 启动**必须**以 `Tape(path, resume=True)` 打开（触发 `_truncate_trailing_partial` 截断末尾残行 + 重算 `_last_seq`），**禁止** `resume=False` 直接续写。该截断逻辑与 `from_tape`（resume 路径）**共享同一纯函数** `_truncate_trailing_partial`（DRY），但 daemon **不复用** `from_tape` 的 typed exception（EmptyTape/AlreadyCompleted/parallel-mid-crash）与 resume 检测——daemon 用 `replay_state` + `_next_node_from_tape` 自判状态。

### I4. 守门机制（防第三写者偷渡，v2 闭环 B1 G1）

**G1（emit 点基线快照，v2 重构）**：CI 测试维护一份"合法 emit 调用点基线文件清单"（v2 基线含 `orca/run/{orchestrator,executor_adapter,step}.py` + `orca/gates/` + `orca/exec/retry.py` + `orca/run/retry.py` + `orca/iface/web/run_manager.py` + `orca/iface/in_session/`（daemon emit 桥））。CI grep 全仓 `bus.emit(` / `Tape.append(` 调用点，与基线 diff——**新增 emit 点必须显式登记进基线**，否则测试红。这避免 v1 的简单 glob 白名单对 gates/retry/web 误报，又防止偷渡新写者。

**G2（事件序列对齐回归）**：daemon 跑某 workflow 的 tape 与 `orca run` 跑同 workflow 的 tape，事件 `(type, seq, 关键字段)` 逐条对齐（回归测试）。注：因方案 E drive_loop 不动，daemon helper 是**新写**的 emit 逻辑，G2 是验证 daemon 与 drive_loop 行为一致的关键守门（而非 v1 说的"重构零行为变化"）。

## §3. 方案评估（v2 增方案 E 采纳）

| 方案 | 处置 | 理由 |
|---|---|---|
| **E. daemon 单独用 helper，drive_loop 不动** | **✅ 采纳（v2）** | 零回归（用户底线 #4）；DRY 违反登记 known-debt。代价：drive_loop 与 daemon emit 短期不 DRY |
| A. daemon 不写 tape，事件回吐宿主写 | ❌ 否决 | 宿主写 tape = 重引入 sidecar 多真相源；宿主无法用 Python `Tape` 抽象 → 必然直写 JSONL（破坏 I2） |
| B. drive_loop 也改走 daemon | ❌ 否决 | 改 `orca run`/`resume` 语义，违反纯增量 |
| C. 不放宽铁律，放弃 in-session | ❌ 否决 | Demo 4 已验证可行，本 ADR 证明可保精神放宽 |
| D. 字面合规：daemon emit 代码物理挪进 orchestrator.py | ❌ 否决 | 跨进程调模块函数无意义；治标不治本 |
| ~~drive_loop 同期重构调 helper（v1 默认）~~ | ❌ 否决（B3） | 15 处 emit 跨多调用栈，regression 风险高，违反底线 #4。被方案 E 替代 |

## §4. 后果

**正面**：
- in-session shell 在铁律 1 下合法落地（daemon = MCP server，Demo 4 验证形态）。
- daemon emit 逻辑集中在新 helper（`step.py`），可独立单测。
- G1 基线快照 + G2 序列对齐把"唯一写逻辑/格式"显式化、可测化。

**负面 / 新约束**：
- 铁律 1 **字面**变化（见 §5），CLAUDE.md 措辞同步更新（本 ADR 授权）。
- 引入跨进程 flock + pid 文件探活 + 仅本地 FS 限制（I3.3）—— daemon 实现必须正确释放（atexit/signal/try-finally）。
- 半写恢复强制 `Tape(resume=True)` 打开（I3.4）。
- **DRY 短期违反**（方案 E 代价）：drive_loop 内联 emit 与 daemon helper 并存，登记 known-debt，独立 phase 处理。
- G1 基线快照需维护（新增 emit 点要登记）。

## §5. CLAUDE.md 铁律 1 措辞更新（本 ADR 授权）

| | 措辞 |
|---|---|
| **现行** | "事件全程 `bus.emit` 落 Tape（**唯一写 Tape 处**，铁律 1）" |
| **新（本 ADR 授权）** | "事件落 Tape 的**写逻辑唯一**（drive 进程内各模块 + in-session daemon 经各自 sanctioned 路径，daemon 走 `step.py` 共享 helper）、**写格式唯一**（同一 `EventType` schema + `bus.emit→Tape.append`）；任一 tape 文件**同一时刻单写者**（执行路径互斥 + run 作用域 + `flock` 兜底）；**跨进程写者仅 in-session daemon 一类**，启动必经 `Tape(resume=True)` 半写恢复 + pid 探活 + 仅本地 FS。禁止任何写者绕过 sanctioned 路径自造事件。" |

> 措辞更新落 CLAUDE.md 铁律 1 + `orchestrator.py:5` / `executor_adapter.py:9` 注释，由 in-session shell 实现 PR 一并改，引用本 ADR。

## §6. 用户命令面（v2 新增，闭环 M4）

用户侧最小可用命令（phase SPEC 落实细节，本 ADR 定契约）：

- **`orca in-session start <wf.yaml>`** —— 一键起一个 in-session run：内部 `gen_run_id` + 生成 tape 路径（`<rundir>/<run_id>.jsonl`）+ 起 daemon（后台）+ **打印一段 opencode.json `mcp` 配置块**（含 run_id / tape 路径 / 命令）让用户贴进 opencode 即可连。友好、零手写路径。
- **`orca in-session status [<run_id>]`** —— 看当前 in-session run 状态（node 进度 / daemon pid / tape 路径）。无 run_id 列全部活跃 run。
- **`orca in-session stop <run_id>`** —— 停 daemon（SIGTERM cleanup → 释放 flock → close tape）+ emit `workflow_cancelled`（与 web run_manager 同款"写 tape 唯一真相"）。
- daemon 崩溃可被 `status` 检测（pid 探活失败）→ 提示 `orca in-session resume <run_id>`（重启 daemon，`Tape(resume=True)` 半写恢复续跑）。

> `--tape`/`--yaml` 是 daemon 内部参数，不对普通用户暴露；用户只碰 `start/status/stop/resume`。

## §7. 与其他文档 / 架构的关系

- [in-session-shell-design-draft.md](in-session-shell-design-draft.md) §2.3/§4.1/§8：本 ADR 落实其铁律 1 前置。SPEC 须同步方案 E（drive_loop 不动）。
- [interface-convergence-adr.md](2026-07-06-interface-convergence-adr.md)：
  - **D5（事件类型 1 套）**：本 ADR 不新增事件类型，一致 ✅。
  - **D7（子进程生命周期 → ProcessRegistry）**（v2 闭环 M2）：**daemon 不纳入 ProcessRegistry**。ProcessRegistry 治理的是 executor 子进程（spawn，`Adapter.cancel()` 委托）；daemon 是宿主连的 MCP server，非 spawn 子进程，语义不匹配。daemon 生命周期独立设计（pid 文件探活 + §6 命令面），phase SPEC 落地。
- [phase-10-mcp.md](phase-10-mcp.md) §0.1 铁律 1（单进程单 RunManager）：daemon 是**独立进程**（in-session D1=c），不并入 phase-10 MCP server 单进程模型；phase-10 适用范围不受影响。
- **opencode 双模式共存**（v2 闭环 M3）：`orca run --backend opencode`（spawn 子进程，drive_loop 模式）与 `orca in-session serve`（opencode 当宿主连 Orca MCP daemon，in-session 模式）**互斥**——同一 run 不可同时跑两种（flock 即保证），不同 run 各自独立。命令名（`run` vs `in-session`）清楚区分。opencode profile `mcp_tools=False` 保持不变（spawn 模式不透传 mcp）；in-session 模式不经 profile，直经 opencode.json mcp 配置。

## §8. 证据

- **Demo 4（2026-07-07）**：daemon = MCP server 形态实测可跑（opencode 1.14 + deepseek-v4-flash，`/tmp/orca-demo4`）。桩 state 代替真 tape，仅验证形态；真 tape 写入（helper + flock + 半写恢复）正确性由 §2 G2 回归测试在 phase 实现期证明。
- **现行写者多发性实证（v2 修正）**：`orca/run/orchestrator.py`（15 处）+ `executor_adapter.py` + `orca/gates/{dialog,interrupt,handler}.py` + `orca/exec/retry.py` + `orca/run/retry.py` + `orca/iface/web/run_manager.py:353`——全部同进程内经 `bus.emit` 共享 schema。证明"唯一写处"早已是"同进程内多写者共享 bus/schema"，本 ADR 扩展的是**跨进程**这一新维度。
- **半写行为源码**：`orca/events/tape.py:167-173`（replay fail-soft 跳过残行）、`:222-301`（`_truncate_trailing_partial` 截断 + 重算 last_seq）——daemon 必须复用此截断（I3.4）。
- **跨进程锁必要性**：`tape.py:108` `Tape._lock` 为 threading lock，不含跨进程语义 → 跨进程必须 flock（I3.3）。

## §9. 实施前置条件（进 phase SPEC 前必须满足）

1. **B1 闭环**：✅ v2 §0/§8 写者清单实证修正 + §2 I4 G1 基线快照化（含 gates/retry/web）。
2. **B2 闭环**：✅ v2 §2 I3.4 半写恢复 + 共享 `_truncate_trailing_partial`。
3. **B3 闭环**：✅ v2 §1/§3 方案 E 采纳（daemon 单独用 helper、drive_loop 零改动）。
4. **M1-M4 闭环**：✅ v2 §2 I3.3 失败面 / §7 D7 / §7 opencode 双模式 / §6 命令面。
5. **D2/D3 决策**：✅ D2=a（仅本地 FS）/ D3=a（不纳入 ProcessRegistry）。
6. **CLAUDE.md 铁律 1 措辞**：实现 PR 随 v2 §5 措辞更新。

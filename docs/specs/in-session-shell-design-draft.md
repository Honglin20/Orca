# In-Session Shell 设计草稿（v5：hook-driven，契约闭环）

> **状态**：草稿 v5（2026-07-07）。v4 经 spec-review-adversarial 审（fail，10 blocker），本 v5 **采纳全部 D-v4-1..4 决策 + 11 点修订清单**，闭环全部 blocker。待实现。
> **v5 关键决策（采纳 review 推荐）**：
> - **D-v4-1=a**：daemon **对外**暴露 observe/next 两 RPC（单一接口），**对内**委托 `advance_step` 原子（observe 只缓存 output 不落盘，next 一次原子产出 `[node_completed, route_taken, node_started]`）→ 消除中断反例 A（observe 落 nc、next 没调 rt 的悬空态）。
> - **D-v4-2=a**：v1 范围 = **opencode serve 模式 + CC**；opencode 交互 TUI 列 follow-up（Demo 6 后扩）。
> - **D-v4-3=b**：CC 路 daemon **只开 Unix socket**（hook-channel）；不开 MCP stdio（v4 模型不调任何 Orca 工具，MCP 通道冗余，删）。
> - **D-v4-4=a**：中断反例 B（emit 中途 SIGKILL）v1 接受低概率 + `orca in-session resume` 手动修；`emit_batch` 原子落盘列 follow-up。
> - **Q14**：v1 锁定 **一 run 一 daemon**（简单、隔离天然、一 tape 一 flock）。
> **依据**：Demo 1（CC Stop block）+ Demo 2（CC PostToolUse 回捕）+ Demo 4（opencode MCP 可用）+ **Demo 5（opencode session.idle→prompt_async 驱动 3-turn 循环，make-or-break 解除）** + `Orchestrator`/`from_tape` 源码 + ADR [2026-07-07-in-session-iron-law-1-adr.md](2026-07-07-in-session-iron-law-1-adr.md) v2。
> **必读**：[shells-design-draft.md](shells-design-draft.md)（三壳契约）、[phase-10-mcp.md](phase-10-mcp.md) §0.1（铁律 1/7）。
> **范围**：hook 驱动机制、daemon 单一接口（observe/next）、双宿主适配、时序、铁律、验收。
> **不是**：最终 phase SPEC（RPC 细节、hook 脚本逐行 → phase SPEC）。

---

## 0. 一句话定位

让**宿主主 session 用自带 subagent 执行 workflow 节点**；**hook 在每个节点 turn 结束时自动推进**——CC 用 `Stop` hook、opencode 用 `session.idle` 事件——调 Orca daemon 的 `observe/next`，daemon 独占 tape、确定性算下一节点、把下一节点 prompt 注入回主 session。**编排权在 Orca（daemon），执行权在宿主主 session，推进由 hook 自动（不依赖模型记得调工具）**。体验等价 CCW，真相源仍是 Orca 单 tape。

> **v4 vs v3**：v3 是 tool-pull（模型主动调 `orca_advance` 工具），依赖模型合规、无 push 保证、偏离立项。v4 回到立项 hook-driven：**模型不调任何 Orca 工具**，只接收 hook 注入的节点 prompt 并派 subagent 执行；hook 自动推进。v3 的 model-facing `orca_advance` 工具**删除**（过期代码）。
>
> **Demo 5 决定性结论（2026-07-07）**：opencode `session.idle`（SSE `/event`）→ `POST /session/{id}/prompt_async`（`model={providerID,modelID}`）**驱动 3-turn 连续循环**（3 idle、3 prompt_async 204、42 message.part.delta、模型每步真产文本）。非退出上下文（serve / 交互式）可靠——立项 hook 设计在 opencode 上**成立**。headless `opencode run` 的进程退出赛跑不适用于交互式（真实用户场景）。

---

## 1. 控制流倒置（核心设计事实）

[shells-design-draft.md §1](shells-design-draft.md)：三壳 = Orca 宿主进程、`drive_loop` 主动跑。本壳倒过来：

| 维度 | CLI/Web/MCP（三壳） | in-session 壳（v4） |
|---|---|---|
| 主循环驱动者 | Orca `drive_loop` | **宿主主 session**（hook 在 turn 末推进） |
| 节点执行者 | Orca spawn executor 子进程 | **宿主主 session 的 subagent**（Task/task 工具） |
| 推进触发 | drive_loop 内 `router.resolve` | **hook**（CC Stop / opencode idle）→ daemon.next |
| 模型是否调 Orca 工具 | — | **否**（模型只接收注入的 prompt） |
| tape 写入者 | drive_loop 内 `bus.emit` | **daemon 独占**（经共享 helper） |
| `drive_loop` | 使用 | **绕过**（一行不改） |

本壳不是 EventBus 订阅者，是新执行驱动模式。

---

## 2. 核心机制：hook 驱动 + daemon 单一接口

### 2.1 daemon 对外两 RPC、对内原子（单一接口，铁律 8，D-v4-1）

**对外**（hook 调用）唯一两个操作：
```
observe(output: str) -> {ok}
  # 仅缓存 output 到 daemon 内存（对应 current node），**不落 tape**。
  # 重复调用覆盖缓存；幂等。

next() -> {done: bool, node?: str, prompt?: str, reason?: str}
  # 一次原子委托 advance_step(output=cached_output)：
  #   产出 [node_completed(current, output), route_taken, node_started(next)] 整批
  #   → 经 EventBus 逐条 emit 落 tape → 清缓存 → current=next → 返回 prompt
  # 三分支（advance_step 内部）：
  #   1. bootstrap：state.pending（无 running）且无缓存 → emit workflow_started+node_started(entry)；current=entry
  #   2. advance：有缓存 → emit nc+rt+ns（或到 $end emit workflow_completed → done:true）
  #   3. idempotent-replay：无缓存但有 running（hook 重发/宿主丢失 prompt）→ 不 emit，重发 running 节点 prompt
```

**关键（消除中断反例 A）**：observe 不落盘 → 任何时刻 tape 只有完整 step（nc 后必跟 rt+ns 同批 emit），无"nc 落盘但 rt 没落"的悬空态。next 内部 `advance_step` 是原子决策（既有纯函数，`orca/run/step.py`，**不改**），daemon 包一层缓存+emit。

**所有宿主、所有 hook，都只映射到 observe/next。** 无 model-facing 工具、无第三操作（bootstrap 是 next 的一个分支，非独立操作）。Hook 2（提醒用 subagent）折进 `next()` 返回的 prompt 文本。

### 2.2 宿主 hook → daemon 操作映射

| 宿主 | observe 触发（subagent 完成） | next 触发（turn 末推进） | 注入方式 |
|---|---|---|---|
| **CC** | `PostToolUse` hook（matcher `Task\|Agent`）→ 拿 `tool_response.content` → `observe` | `Stop` hook → `next()` → `{done:false,prompt}` ⇒ `{"decision":"block","reason":prompt}`；`{done:true}` ⇒ 放行 | Stop `reason` 即下一 prompt（Demo 1 实测） |
| **opencode** | daemon 订阅 `/event`，见 `session.next.tool.success`(tool=task) → `observe` | daemon 见 `session.idle` → `next()` → `prompt_async` 注入 prompt（Demo 5 实测） | `POST /session/{id}/prompt_async`（`model={providerID,modelID}`） |

**关键**：两宿主机制不同（CC 阻断式 Stop / opencode 事件订阅式 idle），但都映射到**同一 daemon observe/next**——单一接口在 daemon 层守住。opencode 的"hook"= daemon 作为**事件驱动 sidecar**（订阅 opencode `/event` SSE，在 idle 时注入），**不依赖 bun/plugin**（避开 `.opencode/plugins/*.ts` 加载挂死），Demo 5 已证此形态可靠。

### 2.3 daemon 进程形态（独占 tape，两宿主前端不同）

daemon 进程独占 tape（ADR D1=c：`flock` + pid 探活 + 仅本地 FS + `Tape(resume=True)` 半写恢复）。**v1 一 run 一 daemon**（Q14）。前端按宿主分两种（内部都包同一 observe/next → advance_step）：

- **CC**：daemon = 被动 standalone 进程，开 **Unix socket**（`<rundir>/<run_id>.sock`）给 hook 脚本调 observe/next。**不开 MCP stdio**（D-v4-3=b：模型不调 Orca 工具，MCP 通道冗余，删）。CC 的事件 = hook 脚本本身（settings.json Stop/PostToolUse → 脚本 → socket）。
- **opencode**：daemon = **主动事件订阅者**，连 opencode serve 的 `/event` SSE，见 `session.next.tool.success`(tool=task)→observe、`session.idle`→next+`prompt_async`。自驱动，无需 hook-channel。

> 两宿主前端不同（被动 socket / 主动 SSE），但 **observe/next 内部接口唯一**（铁律 8 守在 daemon 内核层）。

### 2.4 hook→daemon 调用通道（D-v4-3，闭环 review Q3/Q5）

**CC**：`orca in-session start <wf.yaml>` → 起 daemon（写 socket 路径 + activation 标记）→ 生成 settings.json 片段（Stop/PostToolUse hook 脚本，硬编码 socket 路径 + run_id）给用户贴。hook 脚本是 shell，调 `orca in-session hook-observe/hook-next --socket <path> --run-id <id>`（薄 CLI 转 Unix socket 报文）。

**opencode**：`opencode serve --port P` → `orca in-session start --opencode-url http://localhost:P --session <sid> <wf.yaml>` → daemon 连该 SSE 自驱动。无需 hook 脚本、无需 settings.json。

> run_id / tape_path / socket / opencode-url / session 经 `start` 命令一次性传入并落 daemon 启动配置；hook 脚本经 settings.json 片段拿到 socket+run_id。**无隐式环境变量链。**

### 2.5 observe 入参契约（闭环 review Q12）

`observe(output)` 的 `output` 来源与解析（宿主侧 adapter 负责，喂给 daemon 前 flatten 成 `str`）：
- **CC**：`PostToolUse` payload 的 `tool_response.content`（`list[TextBlock]`）→ `"\n".join(block.text for block in content if type==text)`。
- **opencode**：`session.next.tool.success` 事件的 content（Demo 4/5 验证 task 工具返回末段文本）→ 同上 flatten。
- **解析失败**（如节点声明 output_schema 但 flatten 后非 JSON）：daemon 内 `advance_step._parse_output` 已 fail loud → `InSessionError` → `_fail` emit `workflow_failed`（M1/M2）。
- **observe 无 running 节点**（hook 时序错位）：v5 改**幂等吞 + warn**（review Q13②，不毁 run），不 raise。

---

## 3. 端到端时序（hook 驱动）

```
[启动] orca in-session start <wf.yaml>
  → daemon 起：gen run_id + tape + flock；emit workflow_started + node_started(entry)；current=entry
  → daemon 注入 entry 的 prompt（CC：首次 Stop block / opencode：首条 prompt_async）

[每节点 N]
  ① 主 session 收到注入的 prompt（含“用 subagent 执行”）
  ② 主 session 派 Task/task subagent 执行节点 N → 返回 <outN>
  ③ hook observe 触发（CC PostToolUse / opencode tool.success）→ daemon.observe(<outN>)
       daemon: emit node_completed(N, outN)
  ④ hook next 触发（CC Stop / opencode idle）→ daemon.next()
       daemon: from_tape-style 决策 →
         若下一 Y≠$end：emit route_taken(N→Y) + node_started(Y); current=Y → {done:false, node:Y, prompt:promptY}
         若 $end：emit route_taken(N→$end) + workflow_completed → {done:true}
  ⑤ 注入 promptY（CC reason / opencode prompt_async）→ 回到 ①

[结束] next 返 {done:true} → CC 放行停止 / opencode 不再注入 → tape 完整落盘。
```

事件序列（`workflow_started, ns,nc,rt ×N, workflow_completed`）与 `drive_loop` **逐 seq 对齐**（G2 守门）。每节点 = 主 session 一个 turn（CC）或一次 prompt_async 周期（opencode）。

---

## 4. 复用边界（纯增量，单一接口）

### 4.1 复用（零改动）
- `replay_state`（`orca/events/replay.py`）、`router.resolve`（`orca/run/router.py`）、`Tape`/`EventBus`/事件 schema、`render_prompt`（`orca/exec/render.py`）、节点 output_schema 解析、phase-10 MCP server 基建。
- 决策逻辑抽窄纯函数 `_next_node_from_tape`（`orca/run/step.py`，**不复用** `from_tape` 的 resume typed-exception，ADR v2 Q5）。

### 4.2 不复用（绕过，不改）
- `Orchestrator.drive_loop`（一行不改；daemon 是其"hook 驱动单步版"）。
- `Orchestrator.from_tape`（resume 专用，over-kill）。

### 4.3 新增 / 删除
| | 项 | 说明 |
|---|---|---|
| **删（v3 过期）** | model-facing `orca_advance` MCP 工具、tool-pull 循环逻辑 | v4 模型不调 Orca 工具，hook 驱动。**过期代码及时删除**（goal 要求） |
| 新增 | daemon `observe`/`next` 两操作（单一接口） | 替代 v3 的 advance 单工具；hook 调它 |
| 新增 | CC hook 脚本（Stop + PostToolUse，含激活标记） | 静态预装 + 按 session 标记 dormant/active |
| 新增 | opencode 事件订阅 sidecar（daemon 内） | 订阅 `/event`，idle/tool.success → observe/next + prompt_async |
| 新增 | `orca in-session start/status/stop` CLI | start 起 daemon + 打印接入指引；status 读 tape；stop 终止 |

> **铁律 1**（唯一写 Tape 处）扩展走 ADR v2：daemon 为第一个跨进程 sanctioned 写者，`flock` + pid 探活 + 仅本地 FS + `Tape(resume=True)` 半写恢复 + 共享 helper，不破坏精神（详见 ADR）。

---

## 5. hook 隔离（不动已有 hook，仅 command 生效时激活）

CC/opencode hook 都是**静态预装**（settings.json / daemon 启动注册），不能动态注册。隔离用**激活标记**：
- `orca in-session start` 起一个 run 时，写一条 session 作用域标记（`<rundir>/<run_id>.active`，含 session_id）。
- hook（CC 脚本 / opencode sidecar）每次先查"本 session 是否有活跃 Orca run"——**有才动作，无则 passthrough**。
- workflow 终态 / `stop` → 清标记。
- 效果：① 已有 hook 不受影响（多 hook 并存，本 hook inactive 时透明）；② 仅 command 生效时起作用。**= 用户要的隔离语义。**

---

## 6. 中断与恢复（"LLM 突然中断怎么办"，闭环 review Q10/Q13②）

- **mid-node subagent 挂**：hook observe 收到失败/无输出 → daemon.next 时 `advance_step` emit `node_failed` → tape 终态 `failed`（不卡 running）。
- **turn 间 LLM 停了不推进**：CC `Stop decision:block` 硬拦推继续（全保证）；opencode `session.idle` 注入推进（Demo 5 证可靠）。极端停了 → tape 停在 `node_started(current)` → `orca in-session resume <run_id>` 重启 daemon 续跑该节点。
- **反例 A（observe 落 nc、next 没调 rt 的悬空态）**：**D-v4-1 消除**——observe 不落盘，next 原子批量 emit `[nc,rt,ns]`。tape 任何时刻只有完整 step。
- **反例 B（next 批量 emit 中途 SIGKILL：nc 落盘、rt 没落）**：低概率（窗口 < 内部 emit 间隔，µs 级）。v1 处置：`orca in-session resume` 检测"node_completed 后无 route_taken"→ 截断 nc 回 started、重发该节点 prompt（D-v4-4=a）。`emit_batch` 原子落盘列 follow-up（EventBus 改造，影响面大，不在 v1）。
- **observe 无 running 节点（hook 时序错位）**：幂等吞 + warn（Q13②），不 raise、不毁 run。
- **宿主被 kill、daemon 存活持锁（孤儿锁反向）**：daemon 检测宿主存活（CC：hook socket 长期无调用 + pid 探活；opencode：SSE 心跳/连接断开）→ 宿主死后 daemon cleanup 释放 flock + close tape（ADR I3.3 补）。
- **进程崩**：tape 每事件落盘 → 重启从 fold 续跑。**状态永不丢**（单真相源）。

> 正确性（不丢状态）= tape + D-v4-1 原子；便利性（自动续跑）= hook（CC 全保证 / opencode 可靠 + resume 兜底）。

---

## 7. 风险

| 风险 | 严重度 | 处置 |
|---|---|---|
| opencode idle 注入非原子（fire-and-forget event hook） | 低 | Demo 5 实测：非退出上下文（serve/交互）可靠驱动 3-turn 循环；headless 退出赛跑不适用交互场景 |
| CC Stop 8-block 上限 | 低 | 每节点一 turn，>8 节点 workflow 走 opencode 或后续批处理 |
| 模型不用 subagent 自干（上下文膨胀） | 低 | 注入 prompt 强制"用 Task/task subagent"（Hook 2 折进文本） |
| 副作用：opencode sidecar 需 opencode serve 暴露 `/event` | 中 | 交互用户须以 serve 模式跑 opencode（或后续做 in-process plugin，待 bun 可用）—— phase SPEC 落实部署形态 |
| tape 粒度变粗 | 低 | 节点级事件 + subagent 最终输出；reducer 不依赖 subagent 内部；粒度由 shell 决定 |

---

## 8. 与现有功能关系（"不影响"核对）

- `orca run`/`resume`（drive_loop）：**零改**（ADR v2 方案 E）。
- CLI/Web/MCP 三壳：本壳独立；daemon 与 phase-10 MCP server 解耦（D1=c）。
- opencode profile（子进程后端）：对称不冲突；spawn 模式 vs in-session 模式互斥（flock 保证）。
- tape/reducer/render/router：完全复用，不新增字段/类型。
- v3 的 model-facing `orca_advance`：**删除**。

---

## 9. 验收（端到端，opencode 为目标，覆盖边界）

### 9.1 已完成 spike
- Demo 1（CC）：Stop `decision:block` 2 节点闭环。Demo 2（CC）：PostToolUse(Task) 回捕。
- Demo 4（opencode）：MCP 注册可用、模型可靠调工具、output 回流。
- **Demo 5（opencode，make-or-break）：`session.idle`→`prompt_async` 驱动 3-turn 循环 + 模型每步真产文本。✅**

### 9.2 phase 验收（真实 e2e，零 mock，opencode 为主）
- [ ] **基本循环**：opencode serve + daemon sidecar，3 节点 workflow 端到端，reducer `completed`。
- [ ] **G2 事件序列对齐**：本壳 tape 与 `orca run` 同 workflow tape，逐条比对 **(type, seq, node, data.output)** 四字段全等（review Q11；其余 started_at/duration 不强求对齐，单测声明）。
- [ ] **多次迭代**：≥8 节点长 workflow 跑通（**v1 CC 路径 ≤8 节点硬约束**验；opencode 无上限）。
- [ ] **并发**：两个 in-session run 同时跑 → tape/run_id 隔离、flock 独占、互不串（一 run 一 daemon）。
- [ ] **中断恢复 A**：observe 后、next 前 kill daemon → 重启 → tape 无悬空 nc（D-v4-1 验证）。
- [ ] **中断恢复 B**：next 批量 emit 中途 SIGKILL → `orca in-session resume` 截断残 step 重发。
- [ ] **hook 隔离**：无活跃 run 时 hook passthrough，不影响已有 hook；激活时才动。
- [ ] **用户中途打断**（review Q13①）：CC Stop block 期间手动输入 / opencode idle 期间手动发消息与 daemon 注入竞态 → 不死锁、tape 不腐。
- [ ] **孤儿锁反向**（review Q13③）：宿主被 kill、daemon 存活 → daemon 检测宿主死 → cleanup 释放 flock。
- [ ] **subagent 合规性**（review Q7）：注入 prompt 含"用 Task/task subagent"→ 观察 model 真用 subagent；不合规连续 N 次 next 无 observe → fail loud。
- [ ] **边界**：mid-node subagent 失败→`workflow_failed`；空 output；不支持节点（script/parallel/gate）fail loud；output_schema 不匹配 fail loud；observe 无 running→幂等吞 warn。
- [ ] **opencode 真链路**：真 opencode serve 子进程 + 真 deepseek + 真 daemon sidecar，跑完真 tape。
- [ ] **CC 真链路**：真 `claude -p` + Stop/PostToolUse hook 脚本 + daemon（Unix socket），跑完真 tape。
- [ ] grep：tape 写入仅 daemon；model-facing orca_advance 已删；drive_loop 零改；step.py 未改（D-v4-1a）。

---

## 10. 开放问题（phase SPEC）
1. opencode 交互 TUI 支持（v1 = serve 模式 only，D-v4-2=a；TUI 列 follow-up，需 Demo 6）。
2. CC hook 脚本分发与 `orca in-session start` 安装契约（写 settings.json？手贴？）—— 与"统一安装"小设计合并（§11）。
3. ~~daemon 多 run~~ → **v1 锁定一 run 一 daemon**（Q14 闭环）。
4. ~~observe 入参契约~~ → **§2.5 已定义**（Q12 闭环）。

---

## 11. 与"统一安装"的衔接
本壳与 phase-10 `orca mcp` 都是对外 MCP/集成入口。注册/安装的统一（`orca mcp install --host ...` 收口两壳）作为独立小设计，不在本 SPEC；本壳的 `orca in-session start` 先打印接入指引，待统一安装设计落地后并入。

---

## 12. 决策来源
- Demo 5（2026-07-07）：opencode serve `/event` SSE + `POST /session/{id}/prompt_async`（`model={providerID,modelID}`）→ `session.idle` 驱动 3-turn 循环（`/tmp/orca-demo5/loop.mjs`）。
- opencode 1.14.22 SDK 路由（`~/.config/opencode/node_modules/@opencode-ai/sdk`）：`/session/{id}/prompt_async`、`/event`。
- Demo 1/2（CC hook）、Demo 4（opencode MCP）。
- ADR [2026-07-07-in-session-iron-law-1-adr.md](2026-07-07-in-session-iron-law-1-adr.md) v2（铁律 1 扩展、方案 E、跨进程护栏）。
- `orca/run/orchestrator.py`（drive_loop/from_tape）、`orca/events/replay.py`、`orca/run/router.py`。

---

## 13. v3 → v5 文件级迁移清单（闭环 review Q9，"过期代码及时删除"）

| 文件 | 删（v3 过期） | 留/改 | 加（v5） |
|---|---|---|---|
| `orca/run/step.py` | — | **不改**（D-v4-1a：`advance_step` 原子纯函数，daemon 包一层缓存+emit） | — |
| `orca/iface/in_session/daemon.py` | `serve()` 内 `orca_advance` MCP 工具闭包 + `mcp.add_tool`（v3 model-facing，**整段删**）；`advance()` 单方法 | `__init__`/`_acquire`(flock+pid)/`_on_signal`/`cleanup`(幂等)/`_fail`(emit workflow_failed) **全留** | `observe(output)`（只缓存）+ `next()`（委托 advance_step 原子 emit）；CC 前端 = Unix socket server；opencode 前端 = SSE `/event` 订阅 + `prompt_async` 注入；宿主存活检测（孤儿锁）；observe-no-running 幂等吞 |
| `orca/iface/in_session/cli.py` | `serve` 的 MCP 入口语义 | `start`（起 daemon + activation 标记 + 按 `--opencode-url/--session` 或 CC 路生成接入指引/settings 片段）；`status`（留） | `stop`（终止 daemon+cleanup）；`resume`（截断残 step 重发，反例 B）；`hook-observe`/`hook-next`（薄 CLI 转 socket，CC hook 脚本用） |
| `orca/iface/cli/commands.py` | — | `add_typer(in_session_app)` 留 | — |
| **新增** CC hook 脚本模板 | — | — | `orca in-session start` 生成的 settings.json 片段（Stop→hook-next、PostToolUse(Task)→hook-observe，含 socket+run_id，激活标记 passthrough） |
| **删** v3 `mcp_server.py`（Demo 4 桩） | 桩文件 `/tmp/orca-demo4/mcp_server.py` 是 demo 产物，不在仓库 | — | — |

> 边界：`step.py` 零改（D-v4-1a）；`drive_loop`/`from_tape`/`replay`/`router`/`Tape` 零改（纯增量）；`daemon.py`/`cli.py` 重写前端（v3 MCP-tool 前端 → v5 socket/SSE 前端），内核（tape 所有权 + advance_step 委托）留。

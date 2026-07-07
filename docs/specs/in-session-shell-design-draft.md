# In-Session Shell 设计草稿（v7：闭环 spec-review r2，多 command 架构）

> **状态**：草稿 v7（2026-07-07）。v6 经 spec-review-adversarial r2 审（conditional-fail，3 blocker F1/F2/F3 + 6 major F4-F11）+ 补跑两个 spike（F4 多轮链 / F10 payload）后修订，闭环全部 finding 并新增一硬约束（子 session idle 过滤）。**第二轮 review 判 conditional-pass，再闭环 2 blocker（B1 `Tape.append_batch` / B2 空串 normalize）+ 6 major（N1/N2/M1-M4）**。F3/F6/F10/F11 确认 closed。可进实现（剩余 major 已落 phase 验收）。
> **v7 关键决策（在 v6 之上）**：
> - **D-v7-1（架构铁律：单一接口）**：**薄 CLI = 唯一大脑 + 唯一 tape 写者**；plugin / CC hook = **哑传输**（command → CLI 子进程 → JSON → 注入）。Orca 逻辑全在 Python CLI（可测、单真相源），TS plugin / shell hook 不含任何 Orca 业务逻辑。**加新 command = 加 CLI 子命令 + 加 slash-command 映射，plugin 核心循环零改**（可扩展，§2.6）。
> - **D-v7-2（F1 闭环）**：`[nc,rt,ns]` **单次 `tape.append`+flush 原子落盘**（POSIX 本地 FS 单次 write 原子），消除 mid-batch SIGKILL 窗口。删掉 v6 §6「resume 截 nc」虚构恢复（`_truncate_trailing_partial` 只截字节残行，不截完整事件）。
> - **D-v7-3（F2 闭环）**：CC 路 output 经**激活 marker 旁的 cache 文件**跨 hook 传递（PostToolUse 写 / Stop 读删），新增 §2.4.1 契约。
> - **D-v7-4（F10 闭环）**：opencode 路 output 从 **`ToolPart(tool=task, state.status=completed).state.output`** 提取（解 `<task_result>` 包装 + 剥 `task_id:` 行），**不取 assistant text**。
> - **D-v7-5（新硬约束，spike-2 发现）**：`task` 工具 spawn **子 session**，子 session **自发 `session.idle`**——plugin **必须按激活 marker 只对主 session 注入，忽略子 session idle**（§2.5/§5）。
> - **D-v7-6（F11 闭环）**：subagent 合规计数器入激活 marker，连续 N=3 次 `next` 无 output（模型自干）→ CLI emit `workflow_failed(error_type=subagent_compliance)` 终止，不无限重发。
> - **D-v7-7（F5 闭环）**：LOCK_NB busy 返 `{done:false,reason:"busy"}` 0 退出；plugin 侧 in-flight mutex 防并发 inject。
> - **ADR 升 v3**：F3 闭环，I3.3 拆 a/b（per-call CLI 主 UX / daemon 无头 CI），§1 写者形态放宽。draft 删「I1-I3.4 一字不改」claim。
> **依据**：**Spike-1 `/tmp/orca-spike/` + Spike-2 `/tmp/orca-f4/`（3 节点链）+ `/tmp/orca-f10/`（task tool_result）** + Demo 1/2（CC hook）+ `step.py`/`tape.py`/`replay.py` 源码 + ADR [2026-07-07-in-session-iron-law-1-adr.md](2026-07-07-in-session-iron-law-1-adr.md) v3 + spec-review r1/r2。
> **必读**：[shells-design-draft.md](shells-design-draft.md)（三壳契约）、[phase-10-mcp.md](phase-10-mcp.md) §0.1（铁律 1/7）。
> **范围**：hook 驱动机制、in-process plugin + 薄 CLI 单一接口、双宿主适配、时序、铁律、验收、失败 taxonomy。
> **不是**：最终 phase SPEC（CLI 参数细节、plugin 逐行 → phase SPEC）。

---

## 0. 一句话定位

让**宿主主 session 用自带 subagent 执行 workflow 节点**；**hook 在每个节点 turn 结束时自动推进**——CC 用 `Stop` hook、opencode 用进程内 plugin 的 `session.idle` event hook——调 Orca 薄 CLI 的 `bootstrap/next`，CLI 独占 tape（per-call flock）、确定性算下一节点、把下一节点 prompt 注入回主 session。**编排权在 Orca（薄 CLI 经 step.py，唯一大脑/唯一写者），执行权在宿主主 session，推进由 hook 自动（不依赖模型记得调工具）**。体验等价 CCW，真相源仍是 Orca 单 tape。

> **v7 vs v6**：v6 闭环 spike 机制；v7 闭环 spec-review r2 的 3 blocker + 6 major（单次 write 原子化 / CC cache 契约 / tool_result 提取 / 合规计数器 / busy 语义 / 失败 taxonomy）+ 新增子 session 过滤硬约束 + 明确「CLI 唯一大脑、plugin 哑传输」的可扩展多 command 架构。**v7 第二轮 review（conditional-pass）又闭环 2 blocker（B1 `Tape.append_batch` / B2 空串 normalize）+ 6 major（N1/N2/M1-M4）**，spike 措辞按实证降级。
>
> **Spike 决定性结论（2026-07-07）**——**实证范围 = plugin event hook + promptAsync 注入机制**（spec-review r2 M2 订正：spike 未调 orca CLI、未实现 `/orca` 命令，CLI/bootstrap 命令路径待 phase 实证）：
> - **Spike-1**（`/tmp/orca-spike/`）：opencode 内嵌 Bun runtime，`.opencode/plugin/orca.ts` 经 `plugin` 声明即加载（无需 `brew install bun`）；`event` hook 捕 `session.idle`；hook 内 `client.session.promptAsync` 驱动真第 2 轮。
> - **Spike-2**（`/tmp/orca-f4/`）：3 节点 task-subagent idle 注入链跑通（`bound main → inject node1 → child idle [skip] → node2 → [skip] → node3 → done`），主 session 实测 3 个 task `state.output=<task_result>NODE-N</task_result>`。（注：spike 用硬编码 prompt 注入，**非**调 `orca in-session next` CLI。）
> - **Spike-2**（`/tmp/orca-f10/`）：task 输出在 `ToolPart.state.output`（格式 `task_id: <sid>\n\n<task_result>\n<内容>\n</task_result>`），**不在 assistant text**；且 task spawn 子 session 自发 idle（D-v7-5 硬约束来源）。

## 0.1 Spike 证据索引（2026-07-07）

- **Spike-1**（`/tmp/orca-spike/{orca.ts,log.txt}`）：加载 + idle + 1 次注入闭环（BANANA）。
- **Spike-2 F4**（`/tmp/orca-f4/{chain.json,log.txt,orca.ts}`）：3 节点 idle 注入链 + 子 session 过滤（3 skip 实证）。
- **Spike-2 F10**（`/tmp/orca-f10/msg3.json`）：task `state.output` payload 结构。
- **未 spike 项（phase 实证）**：`/orca` 命令路径（`.opencode/command/*.md` + `command.execute.before` 拦截）、`orca in-session bootstrap/next` CLI、marker 绑定（M2/M3）。
- 操作发现：plugin transpile 使 opencode serve 启动延至 ~15-20s（无 plugin ~4s）；headless serve 需 `permission:{task:allow}`（交互 TUI 用户授权）。

---

## 1. 控制流倒置（核心设计事实）

[shells-design-draft.md §1](shells-design-draft.md)：三壳 = Orca 宿主进程、`drive_loop` 主动跑。本壳倒过来：

| 维度 | CLI/Web/MCP（三壳） | in-session 壳（v7） |
|---|---|---|
| 主循环驱动者 | Orca `drive_loop` | **宿主主 session**（hook 在 turn 末推进） |
| 节点执行者 | Orca spawn executor 子进程 | **宿主主 session 的 subagent**（Task/task 工具） |
| 推进触发 | drive_loop 内 `router.resolve` | **hook**（CC Stop 脚本 / opencode plugin event）→ 薄 CLI `next` |
| 模型是否调 Orca 工具 | — | **否**（模型只接收注入的 prompt） |
| tape 写入者 | drive_loop 内 `bus.emit` | **per-call 薄 CLI**（`step.py` helper + flock，每次 hook 触发短命开合） |
| `drive_loop` | 使用 | **绕过**（一行不改） |

本壳不是 EventBus 订阅者，是新执行驱动模式。

---

## 2. 核心机制：hook 驱动 + 薄 CLI 单一接口

### 2.1 薄 CLI 两子命令、对内原子（单一接口，铁律 8，D-v4-1）

**对外**（hook/plugin 调用）唯一两个 CLI 子命令：
```
bootstrap <wf.yaml> [--inputs '{}'] -> {run_id, tape, done:false, node, prompt}
  # 首次启动：gen run_id + tape 路径 + emit workflow_started + node_started(entry)
  # → 写激活标记 → stdout JSON（entry prompt）。幂等：同 wf 再次 bootstrap 不重发（按标记已有 run_id 复用）。

next --tape <p> --run-id <r> [--output <out>] [--inputs '{}']
     -> {done: bool, node?, prompt?, reason?}
  # per-call：open(resume=True) + flock → 委托 advance_step(output=<normalized>) 一次原子决策
  #   → **单次 write 原子 emit** [nc,rt,ns]（Tape.append_batch，见 §6 / B1）→ close → stdout JSON
  # **--output normalize（B2 闭环）**：CLI 入口把 `--output ""`（空串）与 `--output` 缺失
  #   **等价为 output=None**（step.py:201 `if output is not None` 会让空串走 branch 3 静默推进）。
  #   故 hook/plugin 在"无 output"时必须省略 `--output` 或传空串——CLI 都规约为 None。
  # 三分支（advance_step 内部，零改）：
  #   1. bootstrap：state.pending（无 running）且无 output → emit workflow_started+node_started(entry)
  #   2. advance：有 output → emit nc+rt+ns（或到 $end emit workflow_completed → done:true）
  #   3. idempotent-replay：无 output 但有 running（hook 重发/宿主丢失 prompt）→ 不 emit，重发 running 节点 prompt
```

**关键（消除中断反例 A，D-v4-1 保留）**：observe 不再独立落盘——output 直接作 `next --output` 入参，next 内一次原子批量 emit `[nc,rt,ns]`。任何时刻 tape 只有完整 step（nc 后必跟 rt+ns 同批 emit），无"nc 落盘但 rt 没落"的悬空态。`advance_step` 是既有原子纯函数（`orca/run/step.py`，**不改**），薄 CLI 只包 flock+emit+JSON。

**所有宿主、所有 hook/plugin，都只映射到 bootstrap/next。** 无 model-facing 工具、无第三操作（bootstrap 是 next 的一个分支，独立成 CLI 子命令仅为首次拿 entry prompt 的便利）。「提醒用 subagent」折进 `next`/`bootstrap` 返回的 prompt 文本。

### 2.2 宿主 hook → 薄 CLI 操作映射

| 宿主 | output 来源（喂 `next --output`） | next 触发（turn 末推进） | 注入方式 |
|---|---|---|---|
| **CC** | `PostToolUse` hook（matcher `Task\|Agent`）拿 `tool_response.content` flatten | `Stop` hook → 薄 CLI `next` → `{done:false,prompt}` ⇒ `{"decision":"block","reason":prompt}`；`{done:true}` ⇒ 放行 | Stop `reason` 即下一 prompt（Demo 1 实测） |
| **opencode** | plugin `event` hook 见 `session.idle`（仅主 session）→ 经 `client.session.message` 从最后 assistant message 的 **task ToolPart.state.output** 提取（解 `<task_result>`，D-v7-4） | plugin `event` hook 见 `session.idle` → 薄 CLI `next` → `client.session.promptAsync` 注入（Spike 实测） | `client.session.promptAsync({path:{id},body:{parts,model}})`（spike 验证 204/ok=true） |

**关键**：两宿主机制不同（CC 阻断式 Stop 脚本 / opencode 进程内 plugin event），但都映射到**同一薄 CLI `next`**——单一接口在 CLI 层守住（铁律 8）。opencode 的"hook" = **进程内 plugin 的 `event` 回调**（非外部 SSE sidecar），spike 已证其捕 `session.idle` 并能调 `client.session.promptAsync` 注入下一 prompt。observe 不再是独立 RPC——output 直接作为 `next --output` 入参（无状态 CLI 无需缓存）。

### 2.3 写者形态：per-call 薄 CLI（主 UX 无长驻 daemon）

**v6 推翻 v5 的长驻 daemon 主形态**。主 UX 的 tape 写者 = **per-call 无状态薄 CLI**（`orca in-session bootstrap` / `next`）：

```
每次 hook 触发 → spawn `orca in-session next --tape <p> --run-id <r> --output <out>`
  → Tape(path, resume=True)   # 半写恢复（ADR I3.4）
  → flock(LOCK_EX|LOCK_NB)    # 同时刻单写者（ADR I3）
  → advance_step(...) → 逐条 emit [nc, rt, ns]
  → close（flock 随 fd 关闭释放）
  → stdout: {done, node?, prompt?, reason?} JSON
```

**为什么放弃长驻 daemon（写者形态决策）**：
- ADR v2 I3.3 的 4 个跨进程失败面里，**3 个直接消失**——无孤儿持锁（无长驻持锁者）、无 pid 探活需求、无宿主死后 daemon 存活的反向问题。只剩"半写恢复"（`Tape(resume=True)`，per-call 开即触发）+ "仅本地 FS"（CLI 启动检测）两项。
- flock-per-call 仍保证 ADR I3 不变量「任一 tape 文件同一时刻单写者」——opencode plugin 的 idle hook 天然串行（一次只处理一个 idle 事件），CC Stop hook 亦串行。
- 决策由 `advance_step` 守住（与 v5 同一纯函数，零改），只是外壳从「长驻进程包一层缓存+emit」变成「短命 CLI 直接 emit」。**铁律 1 不变量 I1（step.py helper）/ I2（同 schema）/ I3（单写者）/ I3.4（半写恢复）全部保留。**

**两宿主前端**（都 spawn 薄 CLI，内部包同一 `advance_step`）：
- **opencode**：`.opencode/plugin/orca.ts`（进程内 plugin）。`/orca <wf>` 命令 → 调 `bootstrap` CLI 拿 entry prompt → `client.session.promptAsync` 注入；`session.idle` event hook（**仅主 session，子 session skip**）→ 经 client 从最后 assistant message 的 **task ToolPart.state.output** 提取（解 `<task_result>`）→ 调 `next` CLI → `promptAsync` 注入下一 prompt。**无 socket、无 SSE、无 settings.json**，全在 opencode 进程内（spike 已证）。plugin 零 Orca 业务逻辑（§2.6）。
- **CC**：settings.json 的 Stop/PostToolUse hook 脚本（shell）→ 调 `next`/`bootstrap` CLI（subprocess）→ 读 stdout JSON。**无 socket daemon**（v5 的 Unix socket 前端废弃——hook 脚本直接 spawn CLI 更简单，D-v4-3=b 精神保留：模型不调 Orca 工具、无 MCP stdio）。

> **长驻 daemon 不删，降级**：v5 的 SSE sidecar daemon（`daemon.py:run_opencode/_opencode_loop`）保留为**无头 CI / 长跑批处理**形态（无人值守跑 workflow，不依赖交互界面）。主 UX 不用它。

> **ADR v3 已闭环 F3**（不再「待修订」）：ADR §2 I3.3 拆 **I3.3a（长驻 daemon，无头 CI，pid 探活）/ I3.3b（per-call CLI，主 UX，无 pid，OS 回收 fd + busy 语义）**；§1 写者形态放宽为「per-call CLI 主 UX / 长驻 daemon 无头 CI，共 step.py helper」。per-call CLI 走 I3.3b（非 v2 的 pid 探活）。不变量 I1/I2/I3.1/I3.2/I3.4 共用，I3.3 按形态二选一。

### 2.4 宿主 → CLI 接入通道

**opencode**：项目 `opencode.json` 声明 `"plugin": ["./.opencode/plugin/orca.ts"]`（spike 验证此声明即加载）。plugin 启动时无状态；用户敲 `/orca <wf>` 触发 bootstrap，写一条 **session 作用域激活标记**（`<rundir>/orca-<sessionID>.json`，含 `run_id`/`tape_path`/`yaml`/`model`）；后续 `session.idle` event hook 按 `sessionID` 查标记——有才调 `next` CLI，无则 passthrough（§5 隔离）。CLI 的 `--tape`/`--run-id`/`--model` 从标记读，plugin 透传。

**CC**：`orca in-session start <wf.yaml>` 生成 settings.json 片段（Stop/PostToolUse hook 脚本 + 激活标记），用户贴入 `.claude/settings.json`。hook 脚本读标记拿 `--tape`/`--run-id`，spawn `orca in-session next`。

> **无隐式环境变量链**：run_id/tape/model 经激活标记文件显式传递；hook/plugin 从标记读、CLI 从 argv 收。**标记原子写**（F13）：CLI `write(tmp)+os.replace(tmp,final)`；plugin/hook 读时 `try/except JSONDecodeError → warn + passthrough`（半写态不崩）。

### 2.4.1 CC output cache 契约（F2 闭环）

CC 的 `PostToolUse(Task)`（产 output）与 `Stop`（推进 next）是**两个 hook 事件、两个独立 hook 脚本进程**——v6 删了 daemon 缓存后，output 必须经**文件**跨 hook 传递。契约：

- **cache 路径**：`<rundir>/orca-output-<run_id>.txt`（与激活 marker 同目录，run 作用域隔离）。
- **PostToolUse 脚本**（matcher `Task|Agent`，仅当激活 marker 属本 session）：
  - 提取 `tool_response.content`（`list[TextBlock]`）→ `"\n".join(block.text for block in content if type==text)` → **覆盖写** cache（`write(tmp)+os.replace`）。
  - **多 Task/turn**（一个 turn 多次 Task 调用）：**last-write-wins + warn**（每次覆盖；不 append，避免拼出非节点输出）。
  - **Task 非当前 Orca 节点**（marker inactive 或 Task 属用户日常）：passthrough，不写 cache。
- **Stop 脚本**：读 cache → 作为 `--output` 传 `next` CLI → **删 cache**（一次性，避免下轮复用陈旧 output）。
  - **cache 不存在**（模型自干、未派 Task）：**省略 `--output` 参数**（Stop 脚本 `[ -f cache ] && args+=(--output "$(cat cache)")`，cache 不存在则不追加 argv）→ CLI `output=None` → step.py branch 4（idempotent-replay）→ **合规计数器 +1**（D-v7-6，§2.5）；连续 N=3 次 → fail loud。**禁止传 `--output ""`**（即便传了，CLI 按 §2.1 normalize 为 None，B2 闭环）。
- **bootstrap 幂等 + busy session**（F14）：CLI `bootstrap` 以 advisory lock（`flock` marker 文件）守"同 session 同 wf 复用 run_id"；一个 session 一个 active run 是契约，同 wf 再 `/orca` 复用 run_id 不重发 `workflow_started`；不同 wf 在 busy session → CLI 报 `{error:"session-busy"}` + 提示先 stop。

> Demo 1/2 只证 Stop `decision:block` + PostToolUse 能捕，**未证 PostToolUse→cache→Stop 读→next 推进端到端**——§9.2 phase 验收须补此真链路。

### 2.5 next 入参契约 + 失败 taxonomy（F5/F6/F10/F11 + 子 session 过滤）

**output 来源（按宿主，喂 CLI 前 flatten 成 str）**：
- **CC**：见 §2.4.1（cache 文件）。
- **opencode**（F10 闭环，D-v7-4）：plugin 在 `session.idle` 时经 `client.session.message` 拉本**主** session 消息，找最后一条 assistant message 上**最近一个 `ToolPart` 满足 `tool==="task" && state.status==="completed"`** → 取 `state.output` → **剥 `task_id:` 首行 + 解 `<task_result>…</task_result>` 内文** → 作 `--output`。**禁止取 assistant text**（spike 实测后者是叙事改写如"子代理返回了 SUBMARKER"，非结构化 output）。无 task ToolPart（模型自干）→ 不传 `--output` → 合规计数器路径。
- **解析失败**（节点声明 output_schema 但 flatten 后非 JSON）：CLI 内 `advance_step._parse_output` raise `InSessionError` → CLI `_fail` → emit `workflow_failed` + 返 `{done:true, reason:"failed: ..."}` 非 0 退出。

**子 session 过滤（D-v7-5，硬约束）**：`task` 工具 spawn **子 session**，子 session **自发 `session.idle`**（spike-2 `/tmp/orca-f4` 实测 3 次 child idle）。plugin event hook **必须**：`if (event.properties.sessionID !== marker.sessionID) return`——只对绑定 run 的主 session 注入，**忽略一切子 session idle**。否则节点 prompt 注入子代理 session、污染其 turn。spike 已证此过滤可行（3 次 child idle 全 skip，主链正常推进）。

**plugin in-flight mutex（F5 闭环，D-v7-7）**：plugin event hook 内维护一个 `injecting: Set<sessionID>`；进 idle 处理前 check，已在注入中则跳过本 idle + 记日志（防 `await promptAsync` 期间下一 idle 重入并发 spawn 两 CLI 撞 flock）。

**LOCK_NB busy（F5 闭环）**：CLI 拿不到 flock（两 idle 极端紧邻撞锁）→ 返 `{done:false, reason:"busy"}` **0 退出**（非错误），plugin 下一 idle 再试。

**失败 taxonomy（F6 闭环，D-v7-6）**：per-call CLI `_fail` 统一 emit `workflow_failed`（**不 emit `node_failed`**——step.py 无此 emit，spike-2 源码实证），`error_type` 映射：

| 触发 | error_type | 来源 |
|---|---|---|
| output_schema 解析失败 | `output_schema_mismatch` | step.py `_parse_output` raise |
| 不支持节点（script/parallel/gate） | `unsupported_node_kind` | step.py `_check_agent_node` raise |
| 状态腐败（output 给但无 running） | `state_corrupt` | step.py branch raise |
| subagent 合规超限（N 次无 output） | `subagent_compliance` | marker 计数器 ≥3，CLI 主动 emit |
| CLI 内部异常 | `internal_error` | 兜底 |

**subagent 合规计数器（F11 闭环，D-v7-6）**：激活 marker 加 `no_output_count`。CLI `next` 每次进 step.py branch 4（无 output + 有 running）→ 计数 +1；有 output → 清零。**≥3 → CLI emit `workflow_failed(error_type=subagent_compliance)` + 返 `{done:true}`**（终止，不无限重发——否则非合规模型 → Stop→resend→Stop 死循环、session hung）。

**无 running 节点（next 早到，时序错位）**：CLI **幂等吞 + warn**（0 退出），返 `{done:false, reason:"no-running"}`。

### 2.6 单一接口与多 command 可扩展架构（D-v7-1）

**架构铁律：薄 CLI 是唯一大脑 + 唯一 tape 写者；plugin / CC hook 是哑传输。**

```
┌─ 宿主（opencode plugin / CC hook）─ 哑传输，零 Orca 业务逻辑 ─┐
│  slash command / event hook                                  │
│     ↓ spawn 子进程 + 读 JSON stdout                           │
│  ┌──────────── orca in-session <subcommand> ─────────────┐   │
│  │  唯一大脑：advance_step 决策 / tape 读写 / marker /    │   │
│  │  合规计数 / 失败 taxonomy —— 全在此，Python，可单测    │   │
│  └────────────────────────────────────────────────────────┘   │
│     ↓ JSON {done,node,prompt,reason} / {run_id,...}           │
│  client.session.promptAsync（opencode）/ Stop decision（CC）  │
└──────────────────────────────────────────────────────────────┘
```

**为什么这样切**：
- **唯一真相源**：tape 只由 CLI 写（ADR 铁律 1）；Orca 状态机/决策只由 CLI 算。plugin/hook 不持有也不推导任何 Orca 状态——它们只是「把用户意图翻译成 CLI 调用、把 CLI 回包翻译成宿主动作」。
- **不打补丁**：TS plugin / shell hook 里**禁止**出现 advance/router/marker 解析/合规判断等逻辑（那些是 CLI 的职责）。任何想在宿主侧"快速补一个行为"的冲动 → 应是 CLI 新子命令，而非 plugin 内塞逻辑。
- **可扩展多 command**：加一个 command = **三处协同**（spec-review r2 M1 订正）：
1. **CLI 加一个子命令**（Python，单测）—— 唯一大脑里加能力；
2. **`.opencode/command/<name>.md` 定义 slash 命令**（opencode 命令系统：plugin 的 Hooks 类型**无**"注册新命令" hook，只有 `command.execute.before` 拦截已存在命令；命令本体由 `.opencode/command/*.md` 静态文件定义）；
3. **plugin `command.execute.before` 拦截**该命令 → spawn 对应 CLI 子命令 → 改 `output.parts` 注入回包。
**plugin 核心 idle 循环（session.idle → next CLI → promptAsync）零改**；加 command 只动 .md + 拦截映射 + CLI 三处。

> **M1 未实证项**（phase 验收必须补）：`command.execute.before` 清空/改写 `output.parts` 是否真能阻止原 `.md` prompt 注入主 session——spike 未验，phase SPEC 须实证拦截行为（含 sessionID 从命令上下文捕获，M3）。

**当前 command 集（v7 最小可用）**：
| command | CLI 子命令 | 宿主侧映射 | 作用 |
|---|---|---|---|
| `/orca run <wf>` | `bootstrap` + idle hook 调 `next` | `.opencode/command/orca-run.md` + plugin `command.execute.before` 拦截（捕获 sessionID 写 marker，M3）；CC: `start` 生成 hook 片段 | 跑一个 workflow |
| `/orca status` | `status` | `.opencode/command/orca-status.md` + 拦截 → CLI → 回显 | 看 run 进度 |
| `/orca stop` | `stop` | `.opencode/command/orca-stop.md` + 拦截 → CLI → 回显 | 停 run（清 marker + emit cancelled） |
| （未来）`/orca skip`、`/orca inject <text>` | 新 CLI 子命令 + `.md` + 拦截 | 扩展，核心 idle 循环不改 |

> **实现守门**：CI grep `.opencode/plugin/*.ts` 与 CC hook 脚本，禁止出现 `advance`/`router`/`replay`/tape 路径/`<task_result>` 解析等关键词——宿主侧只允许 spawn CLI + parse JSON 顶层字段。违反 = 架构退化。

---

## 3. 端到端时序（in-process plugin 驱动）

```
[启动] 用户在 opencode 敲 /orca <wf.yaml>
  → plugin 调 `orca in-session bootstrap <wf>` （per-call CLI）
       CLI: gen run_id + tape；flock；emit workflow_started + node_started(entry)；close
       → stdout {run_id, tape, done:false, node:entry, prompt:promptEntry}
  → plugin 写激活标记（sessionID→{run_id,tape,model}）
  → plugin 调 client.session.promptAsync(entry prompt) 注入

[每节点 N]
  ① 主 session 收到注入的 prompt（含“用 subagent 执行”）
  ② 主 session 派 Task/task subagent 执行节点 N → 返回 <outN>
  ③ plugin event hook 见 session.idle（**仅主 session**，子 session idle skip — D-v7-5）
       → 经 client 从最后 assistant message 的 task ToolPart.state.output 提取 <outN>（解 <task_result> — D-v7-4）
       → 调 `orca in-session next --tape .. --output <outN>` （per-call CLI，in-flight mutex 防并发 — F5）
            CLI: flock → advance_step → **单次 write 原子 emit [nc,rt,ns]**（F1）→ close
            → stdout {done:false, node:Y, prompt:promptY}（或 busy/no-running/failed 信封）
       → plugin 调 client.session.promptAsync(promptY) 注入 → 回到 ①
       （若 done:true：plugin 不再注入，清激活标记）

[结束] next 返 {done:true} → plugin 停注入 / 清标记 → tape 完整落盘。
```

CC 路径同构：bootstrap 由 `orca in-session start` 生成 settings.json hook 片段 + 激活标记；③④改为 Stop hook → `next` CLI → Stop `decision:block, reason:prompt`；output 由 PostToolUse(Task) hook 提供。

事件序列（`workflow_started, ns, nc, rt ×N, workflow_completed`）与 `drive_loop` **逐 seq 对齐**（G2 守门）。每节点 = 主 session 一个 turn（CC）或一次 promptAsync 周期（opencode）。

---

## 4. 复用边界（纯增量，单一接口）

### 4.1 复用（零改动）
- `replay_state`（`orca/events/replay.py`）、`router.resolve`（`orca/run/router.py`）、`Tape`/`EventBus`/事件 schema、`render_prompt`（`orca/exec/render.py`）、节点 output_schema 解析、phase-10 MCP server 基建。
- 决策逻辑抽窄纯函数 `_next_node_from_tape`（`orca/run/step.py`，**不复用** `from_tape` 的 resume typed-exception，ADR v2 Q5）。

### 4.2 不复用（绕过，不改）
- `Orchestrator.drive_loop`（一行不改；薄 CLI 是其"hook 驱动单步版"）。
- `Orchestrator.from_tape`（resume 专用，over-kill）。

### 4.3 新增 / 删除
| | 项 | 说明 |
|---|---|---|
| **删（v3 过期）** | model-facing `orca_advance` MCP 工具、tool-pull 循环逻辑 | 模型不调 Orca 工具，hook 驱动。**过期代码及时删除** |
| 新增 | 薄 CLI `bootstrap`/`next` 两子命令（**单一接口，唯一大脑**，§2.6） | per-call flock(I3.3b) + advance_step + **单次 write 原子 emit**（F1）+ busy + 合规计数 + 失败 taxonomy |
| 新增 | 激活 marker 模块（`marker.py`） | run_id/tape/model/sessionID/no_output_count；`os.replace` 原子写 + advisory lock 去重（F13/F14） |
| 新增 | **opencode in-process plugin** `.opencode/plugin/orca.ts` | `/orca` 命令路由 + idle event hook（**子 session 过滤** D-v7-5 + in-flight mutex F5 + tool_result 提取 D-v7-4）→ spawn CLI + `promptAsync`；**零业务逻辑** |
| 新增 | CC hook 脚本（Stop + PostToolUse，含激活标记 + output cache §2.4.1） | settings.json 片段生成；PostToolUse 写 cache / Stop 读 cache+next |
| 降级 | v5 opencode SSE sidecar daemon | 保留为无头 CI/长跑（ADR I3.3a）；主 UX 不用 |
| 新增 | `orca in-session bootstrap/start/status/stop` CLI | 起 run/标记、读 tape、清标记+终态 |

> **铁律 1** 扩展走 **ADR v3**（已闭环 F3）：per-call 薄 CLI 为 sanctioned 写者（I3.3b），`flock` + 仅本地 FS + `Tape(resume=True)` 半写恢复 + 共享 `step.py` helper + 单次 write 原子化，不破坏精神。

---

## 5. hook 隔离（不动已有 hook，仅 command 生效时激活 + 子 session 过滤 + marker RMW 原子）

CC hook / opencode plugin 都是**静态预装**（settings.json / 项目 opencode.json `plugin` 声明），不能动态注册。隔离用**激活标记 + 主 session 过滤**：
- `bootstrap`（opencode `/orca run`）/ `start`（CC）起一个 run 时，写一条 **session 作用域标记**（`<rundir>/orca-<sessionID>.json`，含 run_id/tape/yaml(canonical realpath)/model/sessionID/no_output_count；`os.replace` 原子写）。
  - **主 session 绑定（M3，spec-review r2）**：sessionID 在 `/orca` 命令触发时由 plugin 从 `command.execute.before` 上下文捕获（**非** spike-2 的"首个 session.created"启发式——生产里用户已开多 session 后再 `/orca` 会绑错）。spike-2 用启发式仅证 idle 过滤可行，marker 绑定路径待 phase 实证。
  - **bootstrap 幂等键（N1）**：同 session + 同 `os.path.realpath(yaml)` 视为同一 run，复用 run_id 不重发 `workflow_started`（CLI advisory lock 守 check-write，§2.4.1）。
- hook/plugin 每次先按 sessionID 查"本 session 是否有活跃 Orca run"——**有才调 CLI，无则 passthrough**。
- **主 session 过滤（D-v7-5）**：`task` 工具 spawn 子 session 也发 `session.idle`——plugin **严格比对 `event.properties.sessionID === marker.sessionID`**，子 session idle 一律 skip（spike-2 实证 3 次 child skip）。嵌套 task 产生孙 session 同理过滤（非 marker.sessionID 全 skip）。CC 路无子 session 问题（Stop hook 只挂主 session）。
- **marker RMW 原子性（N2，spec-review r2）**：marker 的 read-modify-write（如 `no_output_count += 1`）必须在 **CLI 持 tape flock 的临界区内**完成（marker 文件操作纳入 flock 保护），否则两并发 `next` 会丢计数更新。规约：CLI `next` → acquire tape flock → read marker → advance_step → emit_batch → update marker（含计数）→ release flock。marker 无独立锁，靠 tape flock 串行化。
- workflow 终态（`next` 返 `done:true`）/ `stop` → 清标记。
- 效果：① 已有 hook/plugin 不受影响；② 仅 `/orca`/`start` 生效时起作用；③ 子代理 turn 不被注入污染；④ marker 计数不丢。

---

## 6. 中断与恢复（"LLM 突然中断怎么办"，闭环 review Q10/Q13②）

- **mid-node subagent 挂**：plugin 拉到失败/无输出 → `next --output` 时 `advance_step._parse_output` raise `InSessionError` → CLI `_fail` emit `workflow_failed(output_schema_mismatch 或 subagent_compliance)` → tape 终态 `failed`（不卡 running，不 emit `node_failed`）。
- **turn 间 LLM 停了不推进**：CC `Stop decision:block` 硬拦推继续（全保证）；opencode `session.idle` event hook 注入推进（spike 证可靠）。极端停了 → tape 停在 `node_started(current)` → 下次 `/orca`（或手动 `bootstrap --resume <run_id>`）续跑该节点。
- **反例 A（observe 落 nc、next 没调 rt 的悬空态）**：**D-v4-1 消除**——observe 不落盘（output 直接作 `next --output` 入参），next 原子批量 emit `[nc,rt,ns]`。tape 任何时刻只有完整 step。
- **反例 B（emit 批次中途 SIGKILL：nc 落盘、rt 没落）**——**v7 用单次 write 原子化消除窗口（F1/R1，D-v7-2；spec-review r2 B1 闭环）**：新增 sanctioned 批写路径 **`Tape.append_batch(list[dict]) -> list[int]`**（ADR I2 扩展）：在 Tape 内同一把 `_lock` 下，**先对全部事件做 Event 校验 + 连续分配 seq**（坏事件 fail loud 不留 seq 间隙），**再一次 `self._fh.write("\n".join(lines)+"\n")` + 单次 `flush`** 落盘整批。`EventBus.emit_batch(list)` 透传给它。CLI `next` 拿到 `advance_step` 的 `result.emits` 后**一次 `emit_batch`**（非逐条 emit）。
  - **POSIX 措辞订正（spec-review r2 B1）**：`PIPE_BUF` 只保证 pipe/FIFO 原子，**普通文件无 POSIX 原子规范保证**——但本地 FS（ext4/APFS）小 write（< page，三事件 JSONL 行远小于）**实践上原子**（要么全落、要么全不落），配合 `_truncate_trailing_partial` 兜底字节级残行（断电截到一半的字节）。非本地 FS 由 ADR I3.3 仅本地 FS 拒绝。**不再宣称"POSIX 规范保证原子"**。
  - **为什么必须 append_batch 而非逐条 append**：`advance_step` 是纯决策不写 tape（`step.py:158`），返回 `list[Emit]`；若 CLI 逐条 `Tape.append`，emit 循环中途 SIGKILL 仍产"nc 落 rt 没落"悬空态——**反例 B 只是被从决策层挪到 CLI emit 循环，未消除**（spec-review r2 N3）。`append_batch` 是消除窗口的唯一手段。
  - **删掉 v6「resume 截 nc 回 started」虚构描述**（`_truncate_trailing_partial` 只截字节级残行，不截完整 nc 行）。`append_batch` 不动 drive_loop（drive_loop 继续用单条 `emit`），是 in-session 写者专用的批写扩展。
- **无 running 节点（hook 时序错位，next 早到）**：CLI 幂等吞 + warn（Q13②），0 退出，返 `{done:false, reason:"no-running"}`。
- **无 running 节点（hook 时序错位，next 早到）**：CLI 幂等吞 + warn（Q13②），不 raise、不毁 run。
- **宿主被 kill（v6 简化）**：**无孤儿锁反向问题**——主 UX 无长驻 daemon 持锁，per-call CLI 的 flock 随进程退出释放。tape 每事件落盘，状态不丢；下次 `/orca` 续跑。（长驻 daemon 形态的孤儿锁仍按 ADR I3.3 处理，仅无头 CI 场景。）
- **进程崩**：tape 每事件落盘 → 重启从 fold 续跑。**状态永不丢**（单真相源）。

> 正确性（不丢状态）= tape + D-v4-1 原子；便利性（自动续跑）= hook/plugin（CC 全保证 / opencode 可靠 + 标记 + resume 兜底）。per-call CLI 把"长驻锁生命周期"问题整个消解。

---

## 7. 风险

| 风险 | 严重度 | 处置 |
|---|---|---|
| opencode idle 注入非原子（fire-and-forget event hook） | 低 | spike 实测：长驻上下文（serve/交互 TUI）可靠驱动第 2 轮（950ms 后第 2 idle、BANANA 实产）；`opencode run` one-shot 会在原 turn 末拆 server，不适用——主 UX 走交互/serve |
| CC Stop 8-block 上限 | 低 | 每节点一 turn，>8 节点 workflow 走 opencode 或后续批处理 |
| 模型不用 subagent 自干（上下文膨胀） | 低 | 注入 prompt 强制"用 Task/task subagent"（折进 prompt 文本） |
| plugin 加载（v5 误判 bun 挂死） | 已消除 | spike 证 opencode 内嵌 Bun runtime，`.ts` 经 `plugin` 声明即加载，无需 `brew install bun` |
| tape 粒度变粗 | 低 | 节点级事件 + subagent 最终输出；reducer 不依赖 subagent 内部；粒度由 shell 决定 |

---

## 8. 与现有功能关系（"不影响"核对）

- `orca run`/`resume`（drive_loop）：**零改**（ADR v2 方案 E）。
- CLI/Web/MCP 三壳：本壳独立；薄 CLI 与 phase-10 MCP server 解耦；降级 daemon（无头 CI）与 phase-10 解耦（D1=c）。
- opencode profile（子进程后端）：对称不冲突；spawn 模式 vs in-session 模式互斥（一 run 一 tape，flock 保证）。
- tape/reducer/render/router：完全复用，不新增字段/类型。
- v3 的 model-facing `orca_advance`：**删除**；v5 SSE sidecar daemon **降级**（无头 CI）。

---

## 9. 验收（端到端，opencode 为目标，覆盖边界）

### 9.1 已完成 spike
- Demo 1（CC）：Stop `decision:block` 2 节点闭环。Demo 2（CC）：PostToolUse(Task) 回捕。（注：未证 PostToolUse→cache→Stop 端到端，§9.2 补。）
- Demo 5（opencode，外部 SSE）：`session.idle`→`prompt_async` 驱动 3-turn 循环。✅
- **Spike-1（opencode，in-process plugin，`/tmp/orca-spike/`）**：plugin 加载 + `session.idle` hook + 1 次 `promptAsync` 注入驱动第 2 轮（BANANA 实产）。✅
- **Spike-2 F4（`/tmp/orca-f4/`）**：**3 节点 task-subagent 链端到端**（`bound main → inject node1 → child idle [skip] → node2 → [skip] → node3 → done`）；主 session 实测 3 个 task `state.output=<task_result>NODE-N</task_result>`。✅（闭环 review F4「只验 1 次」质疑）
- **Spike-2 F10（`/tmp/orca-f10/`）**：task 输出在 `ToolPart.state.output`（`<task_result>` 包装），不在 assistant text；task spawn 子 session 自发 idle。✅（闭环 F10 + 引出 D-v7-5 子 session 过滤）

### 9.2 phase 验收（真实 e2e，零 mock，opencode 为主）
- [ ] **基本循环**：opencode（serve 或交互 TUI）+ plugin + 薄 CLI，3 节点 workflow 端到端，reducer `completed`。
- [ ] **G2 事件序列对齐**：本壳 tape 与 `orca run` 同 workflow tape，逐条比对 **(type, seq, node, data.output)** 四字段全等。
- [ ] **多次迭代**：≥8 节点长 workflow 跑通（CC 路径 ≤8 节点硬约束验；opencode 无上限）。
- [ ] **并发**：两个 in-session run 同时跑 → tape/run_id 隔离、flock 独占、互不串（一 run 一 tape）。
- [ ] **单次 write 原子化（F1/B1）**：CLI `next` emit `[nc,rt,ns]` 经 **`Tape.append_batch`**（grep 实证 CLI 用 `emit_batch`，非逐条 `emit`）；中途 SIGKILL 测试：tape 要么全 3 条、要么 0 条，无 1-2 条悬空（`_truncate_trailing_partial` 兜底字节残行）。
- [ ] **--output 空串 normalize（B2）**：CLI `next --output ""` 与省略 `--output` 行为等价（走 branch 4 + 合规计数），不静默走 branch 3。
- [ ] **bootstrap 端到端（未 spike 项，M2）**：`/orca run <wf>` → plugin `command.execute.before` 捕 sessionID + spawn `bootstrap` CLI + promptAsync 注入 entry，真链路跑通（spike 仅证 idle hook + promptAsync，未证 CLI/bootstrap 命令路径）。
- [ ] **多 session 绑定（M3）**：用户已开 ≥2 session 后在某 session 触发 `/orca` → marker.sessionID 绑定正确 session，不绑"首个 created"。
- [ ] **marker RMW 原子（N2）**：两 `next` 并发 → flock 串行 → no_output_count 不丢更新。
- [ ] **`command.execute.before` 拦截实证（M1）**：拦截 `/orca` 命令、改写 parts 后，原 `.md` prompt 不注入主 session（spike 未验）。
- [ ] **子 session 过滤（D-v7-5）**：跑 task-subagent workflow，plugin 日志见子 session idle 全 skip、仅主 session 注入；子代理 turn 不被污染。
- [ ] **CC output cache 端到端（F2）**：真 `claude -p` + PostToolUse(Task)→写 cache→Stop 读 cache→`next` 推进，3 节点跑通；多 Task/turn last-write-wins；模型自干无 cache → 合规路径。
- [ ] **subagent 合规 fail loud（F11）**：注入 prompt 但模型连续 3 次不派 Task（无 output）→ CLI emit `workflow_failed(error_type=subagent_compliance)`，不死循环。
- [ ] **失败 taxonomy（F6）**：output_schema 不匹配 / 不支持节点 / 状态腐败 → 各自 `workflow_failed` 对应 `error_type`；**无 `node_failed` emit**（grep 守门）。
- [ ] **LOCK_NB busy（F5）**：人为并发触发两 `next` → 后到者返 `{done:false,reason:"busy"}` 0 退出，不 fail loud，下一 idle 恢复。
- [ ] **hook 隔离**：无激活标记时 plugin/hook passthrough；激活后才动。
- [ ] **用户中途打断**：CC Stop block 期间手动输入 / opencode idle 期间手动发消息与 plugin 注入竞态 → plugin mutex 防并发，不死锁、tape 不腐。
- [ ] **架构守门（D-v7-1）**：grep `.opencode/plugin/*.ts` + CC hook 脚本，无 `advance`/`router`/`replay`/tape 路径/`<task_result>` 解析（宿主侧零业务逻辑）。
- [ ] **opencode 真链路**：真 opencode + plugin + 薄 CLI + 真 deepseek，跑完真 tape。
- [ ] **CC 真链路**：真 `claude -p` + Stop/PostToolUse hook + 薄 CLI，跑完真 tape。
- [ ] grep：tape 写入仅薄 CLI（+ 降级 daemon）；model-facing orca_advance 已删；drive_loop 零改；step.py 未改。

> **测试载体（M4，spec-review r2）**：`opencode run` one-shot 会拆 server 截断注入 turn（spike 已知），不可用于本壳自动验收。**自动化载体 = `opencode serve`（headless，`--pure` off 以加载 plugin）+ SDK client 驱动**：SDK client 发 `session.prompt_async` 启动 → 订阅 `/event` 等 `session.idle` → 断言 tape / message。交互 TUI 路径由人工 smoke 覆盖（不进 CI）。phase SPEC 单列"自动化验收 harness"小节落实。

---

## 10. 开放问题（phase SPEC）
1. ~~opencode 交互 TUI 支持~~ → **v6 闭环**（in-process plugin serve/TUI 通用）。
2. CC hook 脚本分发与安装契约（写 settings.json？手贴？）—— 与"统一安装"小设计合并（§11）。
3. ~~daemon 多 run~~ → **锁定一 run 一 tape 一 flock**（per-call CLI 天然满足）。
4. ~~observe 入参契约~~ → **§2.5 已定义**。
5. ~~ADR §1 写者形态放宽~~ → **v7 闭环**：ADR v3 已拆 I3.3a/b + §1 放宽，draft 不再「待修订」。
6. **opencode event hook dispatch 策略**（spec-review r2 F5 残留）：plugin 串行化是断言非实证——v7 用 plugin 侧 in-flight mutex 确定性兜底（不依赖 opencode 内部调度），phase 验收 §9.2 mutex 项实证。

---

## 11. 与"统一安装"的衔接
本壳与 phase-10 `orca mcp` 都是对外 MCP/集成入口。注册/安装的统一（`orca mcp install --host ...` 收口两壳）作为独立小设计，不在本 SPEC；本壳的 `orca in-session start` 先打印接入指引，待统一安装设计落地后并入。

---

## 12. 决策来源
- **Spike-1（2026-07-07，`/tmp/orca-spike/`）**：plugin 经 `plugin` 声明即加载（opencode 内嵌 Bun runtime）；`event` hook 捕 `session.idle`；hook 内 `client.session.promptAsync` 驱动第 2 轮。
- **Spike-2 F4（`/tmp/orca-f4/`）**：3 节点 task-subagent 链端到端 + 子 session 过滤（D-v7-5 实证基石）。
- **Spike-2 F10（`/tmp/orca-f10/`）**：task 输出在 `ToolPart.state.output`（`<task_result>` 包装），D-v7-4 实证基石。
- **spec-review-adversarial r1/r2**：r1 闭环 v4 10 blocker；r2 判 v6 conditional-fail，产出 F1-F14（单次 write 原子化 / CC cache / tool_result / 合规计数 / busy / 失败 taxonomy / I3.3 拆分）。
- Demo 5（外部 SSE，降级无头 CI）、Demo 1/2（CC hook）。
- opencode 1.14.22 plugin + SDK 类型：`Plugin`/`Hooks.event`/`client.session.promptAsync`/`EventSessionIdle`/`ToolStateCompleted.output`/`ToolPart`。
- ADR [2026-07-07-in-session-iron-law-1-adr.md](2026-07-07-in-session-iron-law-1-adr.md) **v3**（铁律 1 扩展、方案 E+F、I3.3 拆 a/b）。
- `orca/run/{orchestrator,step,router}.py`、`orca/events/{replay,tape}.py`（`_truncate_trailing_partial` 仅截字节残行 — F1 实证）。

---

## 13. v6 → v7 文件级迁移清单（闭环 spec-review r2 + 多 command 架构）

| 文件 | 删/降级 | 留/改 | 加（v7） |
|---|---|---|---|
| `orca/run/step.py` | — | **不改**（`advance_step` 原子纯函数，薄 CLI 直调；spike r2 实证无 `node_failed` emit） | — |
| `orca/iface/in_session/daemon.py` | v5 SSE sidecar / socket 降级无头 CI（ADR I3.3a） | flock+pid+resume+cleanup+_fail 留（无头 CI 用） | — |
| `orca/iface/in_session/cli.py` | `hook-observe`/`hook-next` socket 转发删 | `status`/`serve`（降级）留 | **`bootstrap <wf>`**（realpath 幂等键 N1 + advisory lock + 原子写标记 + emit ws/ns → JSON）；**`next --tape --run-id [--output]`**（per-call flock I3.3b + **--output 空串 normalize None（B2）** + advance_step + **`emit_batch` 单次 write 原子化（B1）** + **marker RMW 在 flock 临界区内（N2）** + busy/no-running/合规计数 + 失败 taxonomy → JSON）；`start`（CC：生成 hook 片段+标记）；`stop`（清标记+emit cancelled） |
| `orca/events/{tape,bus}.py` | — | `append`/`emit` 留（drive_loop 用） | **`Tape.append_batch(list[dict])`** + **`EventBus.emit_batch(list)`**（B1 sanctioned 批写路径，单次 write+flush，共用 `_lock`+Event 校验+seq 分配） |
| `orca/iface/in_session/marker.py`（新增） | — | — | 激活 marker 读写（run_id/tape/model/sessionID/no_output_count）+ `os.replace` 原子写 + advisory lock（bootstrap 去重 F14） |
| `orca/iface/cli/commands.py` | — | `add_typer(in_session_app)` 留 | — |
| **新增** opencode plugin（项目内 `.opencode/plugin/orca.ts`，非仓库） | — | — | `/orca <wf|status|stop>` 命令路由（§2.6）→ spawn 对应 CLI 子命令；`session.idle` event hook：**子 session 过滤**（D-v7-5）+ in-flight mutex（F5）+ 从 `ToolPart.state.output` 提取（D-v7-4）+ spawn `next` CLI + `promptAsync`；**零 Orca 业务逻辑**（守门 §9.2） |
| **新增** CC hook 脚本模板（`orca in-session start` 生成） | — | — | settings.json 片段：PostToolUse(Task)→写 output cache（§2.4.1）；Stop→读 cache+spawn `next`+`decision:block,reason:prompt`；激活标记 passthrough |
| **删** v3 `orca_advance` MCP 工具 | 仓库内残留则删 | — | — |

> 边界：`step.py` 零改；`drive_loop`/`from_tape`/`replay`/`router`/`Tape` 零改（纯增量）；`cli.py` 加 `bootstrap`/`next` 两薄子命令（主 UX 写者，单次 write 原子化 + busy + 合规计数 + 失败 taxonomy），`daemon.py` 长驻形态降级无头 CI（ADR I3.3a）。**主 UX 不再有长驻 daemon 进程。** ADR v3 I3.3b 已为 per-call CLI 字面适用（不再「一字不改」claim）。

---

# 子 agent 输出/过程推送 web —— SPEC-B（v3：实时 daemon + 双 read-adapter）

> **v3**（2026-07-17）：两路命门坐实（CC live spike + opencode 静态 spike）→ 推翻 v2「PostToolUse(matcher=Agent) batch」假设，改**实时 daemon + read-adapter 缝**。
> **v2**：B1 已交付（output 显示）；B2 命门（task_id 来源）spike 解但设计仍 batch。
> **关联**：SPEC-A host_session（§4.6 env 契约，B2 复用 + U3 勘误扩用途）；plan `docs/plans/2026-07-17-subagent-output-b1.md`（B1）；release `2026-07-17-subagent-output-b1.md`。
> **评审**：spec-reviewer 两轮（B1 conditional-pass→交付；B2 v2 fail→v3 再过）。

---

## 0. 核心契约（用户铁要求，不可妥协）

**CC 与 opencode 后端处理完全相同、接口同一套；只有「数据获取方式」（read-adapter）不同。**

落点与守门：
- 统一中间表示 `RawAgentEvent`（discriminated union，backend 无关）是两 adapter 的**唯一共同输出**。
- ingestor（`RawAgentEvent → tape agent_*`）、tape 写路径、前端渲染器**三者对 backend 无感知**——代码里**禁止**任何 `if backend == cc/opencode` 分支。
- **grep 守门**（验收 §9 AC5）：`orca/events/` 与 `orca/iface/web/` 内不得出现 backend 名条件分支；backend 差异**只允许**存在于 `*_adapter` 模块内。

---

## 1. 目标

in-session 子 agent 过程（**msg + tool + thinking**）**回合级实时**推 web。满足用户「session 中分发子 agent 后，web 端实时看到 agent 的输出（msg+tool）」。

## 2. 精度边界（诚实声明）

- **不是逐 token 流式**；是**回合级实时**：每个 thinking / tool_call / tool_result 落盘后，经 daemon 增量读 → tape → follow_task(0.3s) → WS，**≤~1s 到前端**。滞后量 ≈ model 的回合节奏（秒级）。
- **已坐实两边都是「回合边界增量写」**：
  - CC：live spike——子 agent 跑 3 步期间 sidechain 文件 8 次离散 size 增长横跨 ~25s，原子整行。
  - opencode：静态 spike——child session 311 part，`time_created` 摊开 ~50min，相邻 +0.3s~+18s 逐步推进。（live 读时写由 sub-agent 补确认，见 §5。）

逐 token 打字机效果在 sidechain/sqlite 里拿不到（也不在需求内）。

---

## 3. 架构（铁律：后端同一套，acquisition 可插拔）

### 3.1 数据流（单路，唯一真相源不破）

```
[ReadAdapter] → RawAgentEvent → [sidechain_ingestor 确定性转换] → agent_* events
   ↓                                                              ↓
 CC: tail jsonl 文件                                          Tape（唯一写路径）
 opencode: 游标轮询 sqlite                                         ↓
                                                          follow_task(0.3s) → WS → 前端（零改，复用 B1 渲染器）
```

### 3.2 接口契约（统一，backend 无关 —— 「同一套接口」的落点）

**`ReadAdapter`（Protocol/ABC，两实现）：**
```python
class ReadAdapter(Protocol):
    def discover_children(self, host_session: str, since_ts: int) -> Iterator[ChildRef]:
      """定位本 run 的子 agent。CC: 枚举 <host_session>/subagents/*.jsonl；
         opencode: SELECT id FROM session WHERE parent_id=? AND time_created>=?。"""
    def stream(self, child: ChildRef, cursor: Cursor) -> Iterator[tuple[RawAgentEvent, Cursor]]:
      """从 cursor 增量读新事件，吐 (RawAgentEvent, 新cursor)。到 EOF 不阻塞，下次再 poll。"""
```

**`RawAgentEvent`（统一 IR，discriminated union —— 两 adapter 唯一共同产出）：**
```python
@dataclass
class RawAgentEvent:
    child_id: str          # 子 agent 身份：CC task_id / opencode child session id
    source_id: str         # 幂等 key：CC f"{agentId}:{line_idx}" / opencode part.id
    kind: Literal["thinking","tool_call","tool_result","text","step_boundary"]
    payload: dict          # kind 对应：text / {tool_name,input} / {tool_name,output} / text / {phase}
    # cursor 不进 IR（adapter 内部续信用），但 ingestor 用 source_id 做幂等
```

**`sidechain_ingestor`（确定性纯转换，backend 无感）：** `RawAgentEvent → list[agent_* tape event]`，emit 前**按 source_id 查 tape 重**（幂等）。无 model、无 I/O（除 tape 查重）。

**前端：零改。** B1 后 `entries.ts` 已支持 message / tool-single / tool-group / thinking 渲染；B2 的 `agent_*` 复用同一渲染器。

### 3.3 两 adapter（**只这层不同**）

| | `CCJsonlAdapter` | `OpencodeSqliteAdapter` |
|---|---|---|
| 数据源 | 文件 `<host_session>/subagents/agent-<id>.jsonl` | sqlite `part` 表（live WAL 只读连接） |
| discover_children | watch 目录 / glob `agent-*.jsonl` | `WHERE parent_id=host AND time_created>=run_start` |
| stream 增量 | tail 文件 mtime/size → readline（只完整行，尾行缓冲） | 游标 `(time_created,id)` 轮询 `part` |
| → RawAgentEvent | jsonl 行 type 映射（§4） | part.data.type 映射（§5） |

**两 adapter 产出同一个 `RawAgentEvent` → 后续 ingestor/tape/前端代码路径完全共享。** 这就是「接口同一套」的物理实现。

---

## 4. CC adapter（实现先行 P1，零前置）

- **host_session** = env `CLAUDE_CODE_SESSION_ID`（**开箱**，bash 子进程自带；见 [[host-session-id-source-of-truth]]）。
- **daemon**：复刻 `chart_daemon` 生命周期——bootstrap spawn，watch `<host_session>/subagents/`，新 `agent-*.jsonl` 出现即 tail；按 readline 读**完整 `\n` 行**（尾行缓冲，防撕裂）；映射 → tape；终态事件自退 + TTL 兜底。
- **映射表**（spike 实测的 sidechain 行结构 → RawAgentEvent）：

  | sidechain 行 | → kind |
  |---|---|
  | `type=user` + `attachment`（首行任务/附件） | skip |
  | `type=assistant` + `content[].type=thinking` | `thinking` |
  | `type=assistant` + `content[].type=tool_use` | `tool_call`（name + input） |
  | `type=user` + `content[].type=tool_result` | `tool_result`（output） |
  | `type=assistant` + `content[].type=text` | `text` |

- **幂等**：`source_id = f"{agentId}:{line_idx}"`。
- **spike 坑（已记）**：headless `claude -p --output-format stream-json` **必须 `--verbose`**。

## 5. opencode adapter（contract 先锁 P0，实现 P2 待 plugin 注入）

- **host_session** = plugin `orca.ts` 注入 `ORCA_HOST_SESSION_ID`（`ses_xxx`）。**共享前置**：与 SPEC-A 串台/nudge 是**同一个**注入（见 [[host-session-id-source-of-truth]] / [[opencode-subagent-storage]]）——落地后 nudge 防串台 + B2 child 定位**一起解锁**。
- **discover_children**：`SELECT id FROM session WHERE parent_id=:host AND time_created>=:run_start`。
- **stream**：游标 `WHERE session_id=:child AND (time_created,id) > (:t,:i) ORDER BY time_created,id`，只取 `json_extract(data,'$.type') IN ('reasoning','tool','text','step-start')`。
- **映射表**（静态 spike + **sub-agent 补精确字段，见报告**）：

  | part.data.type | → kind | 字段路径（待 sub-agent 终稿） |
  |---|---|---|
  | `reasoning` | `thinking` | `data.reasoning` / `data.text` |
  | `tool` | `tool_call` + `tool_result` | `data.tool.{name,input,output}` |
  | `text` | `text` | `data.text` |
  | `step-start` / `step-finish` | `step_boundary`（{phase}） | — |

- **读 live WAL**：只读 sqlite 连接，WAL 允许并发读，不阻塞 opencode 写。
- **精度同 CC**：回合边界增量（静态已证；live 读时写由 sub-agent 补）。

---

## 6. 节点归属（免费，消掉 v2「flush 时序」洞）

tape 是 append-only 有序流。daemon 在 node X 执行期间**增量** emit `agent_*`，事件天然落在 `node_started[X] … node_completed[X]` 之间 → 前端按序渲染自动归位到 X 节点下。**不用显式 node 标签**，不用 `orca next` 在边界 flush。这消掉了 v2 spec-reviewer 的「flush 时序」设计洞（4b）。

## 7. SoT（不破唯一真相源）

- sidechain/jsonl 与 sqlite = **数据源**（input，CC/opencode 运行时产物）。
- adapter = **确定性读**；ingestor = **确定性转换**（无 model）；tape = **唯一写路径**（output，append-only）。
- **单路**（adapter→ingestor→tape），不构成 SPEC-A §3.4 的「两路独立采集可发散」。
- **幂等 key `source_id`** 防 re-ingest 重复（daemon crash/restart/re-trigger 安全）。best-effort 投影（用户「有记录即可」），类比 `chart_ingestor`。
- **不触发停止铁律。**

## 8. host_session 勘误扩用途（承 v2 U3）

host_session = **宿主 session 身份**，多消费者：nudge 防串台（SPEC-A）+ sidechain/child 定位（B2）。语义自洽，非新真相源。

---

## 9. 验收

1. **CC**：in-session wf，子 agent thinking / tool_call / tool_result 在 web **回合级实时**显示（每事件 ≤~1s 到前端）。
2. tape 含 `agent_*`，**幂等**（daemon restart / `orca next` re-trigger 不重复）。
3. dict/string output（B1）+ 过程（B2）都显示。
4. **opencode** adapter 同样过验收 1-2（待 plugin 注入 P2）。
5. **接口同一性（核心契约）**：CC/opencode 经各自 adapter 产出**同 `RawAgentEvent`**，ingestor/tape/前端代码路径完全相同。grep 守门：`orca/events/` + `orca/iface/web/` 无 `cc`/`claude`/`opencode` 名条件分支（差异只许在 `*_adapter` 模块内）。

## 10. 实现 sequencing

- **P0**：本 v3 SPEC 过 spec-reviewer（两轮）。
- **P1**：`CCJsonlAdapter` + `sidechain_ingestor` + daemon（复刻 chart_daemon）→ **CC 实时 B2 上线**（零额外前置）。
- **P2**：opencode plugin host_session 注入（解锁串台 + opencode B2）→ `OpencodeSqliteAdapter`。contract 已在 P0 锁，P2 只加一个 adapter 模块。
- opencode 在 P2 前是 sequencing defer，**不是 fundamental defer**（v2 误判已纠）。

## 11. 余设计洞闭环（承 v2 spec-reviewer 五洞）

| v2 洞 | v3 闭环 |
|---|---|
| 幂等 key（6e BLOCKER） | `source_id`（CC `agentId:lineIdx` / opencode `part.id`）+ emit 前 tape 查重 |
| 字段映射（4c/2） | CC 映射 §4（spike 实测）；opencode 映射 §5（sub-agent 补精确字段） |
| flush 时序（4b） | 实时路径天然有序（§6），洞消 |
| task_id 来源（4a/U4） | 实时路 daemon 自己 discover（目录 watch / parent_id 查询），不再依赖 PostToolUse hook 给 agentId |
| opencode scope（4d） | 已解（§5），不再是 defer-only |

---

## 决策清单（v3）

1. **核心契约**：CC/opencode 后端处理同一套、接口同一套（`ReadAdapter` + `RawAgentEvent`），只有 read-adapter 不同；grep 守门（§0/§9 AC5）。
2. **实时 daemon**（替代 v2 batch hook）：回合级实时，两边命门已坐实（§2）。
3. **CC 先行**（P1，host_session env 开箱）；**opencode contract 先锁、实现待 plugin 注入**（P2，与 nudge 共享前置）。
4. **节点归属免费**（tape 有序，§6），消 v2 flush 时序洞。
5. **单路 ingestor→tape + 幂等 key**，不破唯一真相源（§7）。

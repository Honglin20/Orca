# 子 agent 输出/过程推送 web —— SPEC-B（v4：spec-review 5 BLOCKER 全闭 + opencode 坐实 + 实现地基测绘）

> **v4**（2026-07-17）：spec-reviewer conditional-pass → **5 P0 BLOCKER 全闭**（R1-R7）+ **4 架构决策**（U1-U4，goal 钩子下自主裁决）+ opencode sub-agent 坐实 + Explore 实现地基测绘。
> **v3**：实时 daemon + 双 read-adapter 缝（脊梁）。**v2**：batch hook（已推翻）。**B1**：已交付（output 显示）。
> **评审**：spec-reviewer 一轮 conditional-pass（5 BLOCKER + R1-R7 + U1-U4）；二轮 doc review 跳过（fixes 经 Explore 精确坐实，coder-agent code-reviewer + test-agent E2E 为更强闸）。

---

## 0. 核心契约（用户铁要求，不可妥协）

**CC 与 opencode 后端处理完全相同、接口同一套；只有「数据获取方式」（read-adapter）不同。**

- 统一 IR `RawAgentEvent`（**payload 1:1 对齐 `EventType.data`**，R1）= 两 adapter 唯一共同产出。
- ingestor **1:1 透传**（R2，零 rename）→ tape `agent_*` → 前端**零改**（Explore 坐实 `entries.ts` 已全支持 thinking/message/tool-single/tool-group）。
- **grep 守门（U3=a，R5）**——查：`orca/events/` + `orca/iface/web/` + `orca/events/adapters/`（adapter 物理位置 `cc_jsonl.py` / `opencode_sqlite.py`），**禁** backend 名条件分支；例外：`*_daemon.py` 内允许 backend-specific 路径 resolve / 启动参数。
- **物理位置钉死**：IR `orca/events/raw_agent_event.py`；ingestor `orca/events/sidechain_ingestor.py`；adapter `orca/events/adapters/cc_jsonl.py` + `opencode_sqlite.py`；daemon `orca/iface/in_session/sidechain_daemon.py`。
- **kind 集合（U2=a）**：`RawAgentEvent.kind` 是 backend **可能产出并集**；CC 产 `{thinking,tool_call,tool_result,text}`（sidechain 无 step，memory 坐实），opencode 额外产 `step_boundary`；前端对缺失 kind graceful 降级（`entries.ts:151-158` 就位）。**「接口同一性」= 相同代码路径消费相同 IR，非强制两后端产出相同 kind 集。**

## 1. 目标

in-session 子 agent 过程（**msg + tool + thinking**）**回合级实时**推 web。
**scope（U4）= msg+tool+thinking only；`agent_usage`（token/cost）不在 B2**（避免子/主 usage 双计致前端聚合错乱），未来 B3 单独立项。

## 2. 精度边界（R6 诚实）

回合级实时，**非逐 token**。**≤~2s**（daemon poll≤0.5s + follow_task 0.3s + WS tick + opencode batch commit）。两边增量写已坐实：CC live spike（~25s 内 8 次原子整行增长）；opencode 静态（311 part 摊开 ~50min + event firehose 9111≈2.1/part）。

---

## 3. 架构（铁律：后端同一套，acquisition 可插拔）

### 3.1 数据流（单路，唯一真相源不破）

```
[ReadAdapter] → RawAgentEvent → [sidechain_ingestor: 1:1 透传 + source_id 查重] → agent_* events
   ↓                                                                          ↓
 CC: tail jsonl                                                          Tape（_FlockSafeTape 唯一写路径）
 opencode: event 表 seq 游标                                                  ↓
                                                                  follow_task(0.3s) → WS → 前端（零改）
```

### 3.2 `RawAgentEvent`（R1：payload 1:1 `EventType.data`）

```python
@dataclass
class RawAgentEvent:
    child_id: str    # CC task_id / opencode child session id → 进 agent_*.session_id
    source_id: str   # 幂等 key：CC f"{agentId}:{line_idx}" / opencode part.id
    kind: Literal["thinking","tool_call","tool_result","text","step_boundary"]
    payload: dict    # 逐字 = EventType.data（见下），ingestor 零 rename 透传
```

**payload schema（= `orca/schema/event.py:32-39` agent_*.data，Explore 坐实）**：
- `thinking` → `{text}`
- `text` → `{text}`（→ EventType `agent_message`）
- `tool_call` → `{tool:str, args:dict, tool_call_id:str}`（前端 `pairToolEvents` 强依赖 `tool_call_id` 配对，`entries.ts:90/96-100`）
- `tool_result` → `{tool_call_id:str, result:str}`
- `step_boundary` → `{phase:"start"|"finish"}`（→ `agent_step_started {step_reason}`）

**ingestor 映射（R2：1:1 透传，禁内部 rename；rename 是 adapter 责任，grep 守门查不到）**：

| kind | EventType | data | node | session_id |
|---|---|---|---|---|
| thinking | agent_thinking | payload 1:1 | U1 派生 | child_id |
| text | agent_message | payload 1:1 | U1 派生 | child_id |
| tool_call | agent_tool_call | payload 1:1 | U1 派生 | child_id |
| tool_result | agent_tool_result | payload 1:1 | U1 派生 | child_id |
| step_boundary | agent_step_started | `{step_reason: payload.phase}` | U1 派生 | child_id |

### 3.3 `ReadAdapter` Protocol

```python
class ReadAdapter(Protocol):
    def discover_children(self, host_session: str, since_ts: int) -> Iterator[ChildRef]: ...
        # ChildRef = str（CC task_id / opencode session id 都是 str）
    def stream(self, child: ChildRef, cursor: Cursor) -> Iterator[tuple[RawAgentEvent, Cursor]]: ...
        # Cursor = int | tuple[int,str]（type-erased；CC byte-offset / opencode event.seq）
        # since_ts 通用化为「时间窗」（CC 可选用 mtime）；opencode 用它筛 parent_id 查询
```
（A3/B1/B2 类型细节 P1 实施时定，contract 已锁。）

---

## 4. CC adapter（P1 先行，零前置）

- **host_session** = env `CLAUDE_CODE_SESSION_ID`（开箱）。
- **daemon 必复刻 chart_daemon 7 组件**（R4，逐字复用零改造）：
  1. `_FlockSafeTape`（`chart_daemon.py:67-104`）——跨进程 `fcntl.flock` 与 cli next 写路径互斥；append 前 flock + `_read_max_seq_from_disk` 重置 `_last_seq`。
  2. `_flock_path`（`cli.py` 同源）——同锁文件路径防漂移。
  3. `_read_max_seq_from_disk`（`:119-180`）——O(delta) seq 重算缓存。
  4. `_watch_terminal`（`:183-254`）——tail tape 见终态自退 + partial-line race 防护。
  5. signal handler（`:285-301`）——不裸 `sys.exit`，graceful。
  6. crash callback（`make_crash_callback` 同款）——自重起 + 重挂 callback。
  7. TTL 兜底（`_DEFAULT_TTL_SECONDS = 6h`）——防泄漏。
- **daemon 主体新写（非 socket server，R4/H2）**：启动 glob 已存在 sidechain 文件（H4）+ watch 增量 + readline 完整行（尾行缓冲）+ 映射 `RawAgentEvent` + 查重 + `bus.emit`。
- **spawn 点**（Explore 坐实）：`cli.py:754` bootstrap 后并列 `_spawn_sidechain_daemon`；`cli.py:843` next respawn 后并列 `_ensure_sidechain_daemon`。
- **映射**（spike 实测 sidechain 行）：`assistant+thinking→thinking`；`assistant+tool_use→tool_call{tool,args=input,tool_call_id}`；`user+tool_result→tool_result{tool_call_id,result}`；`assistant+text→text`；`user/attachment→skip`。
- **CC sidechain 路径（R7）**：`~/.claude/projects/<encoded-cwd>/<host_session>/subagents/agent-<task_id>.jsonl`；`<encoded-cwd>` = cwd 的 `/→-`（如 `/mnt/d/Projects/Orca`→`-mnt-d-Projects-Orca`）；resolve 失败 **fail-loud**（CRITICAL log + 退出）；env `ORCA_CC_SIDECHAIN_ROOT` 测试覆盖。
- **幂等**：`source_id = f"{agentId}:{line_idx}"`。

## 5. opencode adapter（contract 锁，P2 实现已无前置阻塞）

- **host_session 注入已交付**（opencode sub-agent 坐实 + 我核实 file:line）：`orca/iface/in_session/templates/opencode/orca.ts:259-263` `shell.env` hook 注入 `ORCA_HOST_SESSION_ID=input.sessionID`（host-session-binding v2 §4.5，随串台闭环交付），`cli.py:93-103 _host_session_from_env()` 读 → tape，实测 bash 子进程收到。**P2 无前置**（v3「待注入」框架过时）；operational gap：非 in-session 起的 run `host_session=None`（fail-open：daemon 接受显式 `--host-session` 或扫近期 session）。
- **discover_children**：尾随父 session **event 流**遇 `task` tool part → 提 `state.metadata.sessionId`（child，10 样本 == `session.parent_id` 实证）→ 切 child event 流（低延迟、带 model 元数据）；备选 `SELECT id FROM session WHERE parent_id=?`（`session_parent_idx` 在）。
- **stream 用 `event` 表 seq 游标**（opencode sub-agent 坐实，**纠 v3 part 表**）：`event(aggregate_id, seq, type, data)`，`message.part.updated.1` 在 INSERT+UPDATE 各 fire（9111≈2.1/part），`seq` 连续整数无空洞，`data.part` 内联完整 part JSON → `WHERE aggregate_id=? AND seq>cursor ORDER BY seq`。**理由**：part 行状态翻转（task `running`→`completed`）时 `id`/`time_created` 不变 → part 游标漏 tool 完成态；event 表捕获翻转 → adapter 吐和 CC 一样的 `tool_call`→`tool_result` 序列（**强化接口统一**）。
- **映射**：`reasoning→thinking`；`tool` part **单行双状态**→ INSERT(running) emit `tool_call{tool, args=state.input, tool_call_id=callID}` + UPDATE(completed) emit `tool_result{tool_call_id=callID, result=state.output}`（dedup by `callID`，与 CC 两事件序列对齐）；`text→text`；`step-start/step-finish→step_boundary{phase}`。`state.input` 形状随 tool 变（bash={command}/read={filePath}/task={description,prompt,subagent_type}/…，per-tool 分派）；`output` 恒 string（结构化在 `metadata`）；`step-*.snapshot` = 回合分组 key。
- **读 live WAL**：只读连接，WAL 并发读，不阻塞 opencode 写。
- **P2 spike（实施时）**：part.id 生命周期内 immutable 证（E2）；WAL commit 间隔 vs ≤2s（N4）。

---

## 6. 节点归属（U1=a，**撤回 v3 §6「天然归位」**）

**v3 §6「免费归属」被 spec-review D1 证伪**：daemon 滞后 emit 可能在 host `orca next` 发 `node_completed[X]`+`node_started[X+1]` 之后 → 错位到 X+1。
- **决策 U1=(a)**：daemon emit 前**读 tape 派生 node**（取最后一条 `node_started` 的 node）——符合「读 tape 派生」先例（SPEC-A §2.2 yaml_path/host_session）。
- **multi-run race 不存在**（H3 闭环）：daemon + tape 均 **per-run**（同 chart_daemon），run A 的 agent_* 只进 run A tape，无跨 run 交错。spec-review 假设的「两 run tape 交错」不成立。
- **诚实声明**：单 run 内仍有 ≤1 poll cycle（≤0.5s）trailing 错位窗口（node X 收尾事件可能落 `node_started[X+1]` 之后被派生成 X+1）。
- **升级路径**：若 test-agent E2E 可见错位，升级 **U1=(b)**：`node_completed.data` 加 `child_agents:[task_id]` + 前端 reducer 按 `session_id` 归位（race-free，代价 B1 reducer 小改）——作 follow-up，由 E2E 证据触发，不在 B2 P1 scope。

## 7. SoT + 幂等（R3）

- sidechain/sqlite/event = **数据源**（input）；adapter = 确定性读；ingestor = 确定性 1:1 转换；tape = **唯一写路径**（`_FlockSafeTape`，R4）。单路（adapter→ingestor→tape），不构成 SPEC-A §3.4「两路独立采集可发散」。**不触发停止铁律。**
- **幂等（B2 vs chart 唯一增量）**：`chart_ingestor` **无查重**（socket 短连接无重发，模块 docstring §0.1）；sidechain daemon 主动 tail + crash 无状态 → **必须自建查重**：
  1. `source_id` 进 `agent_*.data.source_id`（data 是 free dict，零 schema 改；前端 reducer 对 agent_* no-op `replay.py:132-135`，零回归；`pairToolEvents` 不读 source_id，零影响）。
  2. daemon 内存「已 ingest source_id set」：emit 前 O(1) 查，命中 skip。
  3. crash restart：从 tape **一次性**扫 source_id 重建 set（O(N) 一次性，非每 emit 扫）——同 `_FlockSafeTape._read_max_seq_from_disk` 重启重置模式。
- **chart_ingestor 类比限定**为「best-effort 投影 + 不破单真相源」，**非** socket 查重（C1 措辞纠正）。

## 8. host_session（承 v2 U3 勘误扩用途）

host_session = 宿主身份，多消费者：nudge 防串台（SPEC-A）+ sidechain child 定位（B2）。CC env 开箱；opencode plugin 注入已交付。

---

## 9. 验收（R6 钉值）

1. **CC**：in-session wf 子 agent thinking/tool_call/tool_result 在 web **回合级实时**（≤~2s）。
2. **幂等**：daemon SIGKILL→respawn（`_ensure_*` probe+spawn），tape `agent_*` 的 `source_id` 唯一无重复。
3. dict/string output（B1）+ 过程（B2）都显示。
4. **opencode** adapter 同过验收 1-2（P2，无前置）。
5. **接口同一性 grep 守门**：
   ```
   grep -rnE 'if\s+backend\s*==|backend\s*(in|not in)|backend\s*=\s*["'"'"'](cc|claude|opencode)' \
     orca/events/ orca/iface/web/ orca/events/adapters/
   ```
   预期 **0 hit**（例外：`*_daemon.py` 路径 resolve / 启动参数）。
6. **testid 断言**：`thinking` / `message` / `tool-single` / `tool-group` / `node-output` 各存在。
7. **实时测试方法**：mock 子 agent 写 sidechain + 真 daemon + 真 WS，断言前端收到时间戳 − 写入时间戳 ≤ 2s。

## 10. 实现 sequencing

- **P0**：本 v4。
- **P1**：`CCJsonlAdapter` + `sidechain_ingestor` + daemon（零前置）→ **CC 实时 B2 上线**。
- **P2**：`OpencodeSqliteAdapter`（event 表游标）——**注入已就绪、无前置**（v3「待 plugin 注入」过时）；P2 = 加 adapter + P2 spike（part.id immutable / WAL commit 间隔）。
- opencode 是 sequencing defer，非 fundamental。

## 11. 设计洞闭环（v3 spec-review 五 BLOCKER）

| BLOCKER | v4 闭环 |
|---|---|
| 🔴 A2 字段不对齐 + 缺 tool_call_id | R1 payload 1:1 `EventType.data`（含 `tool_call_id`） |
| 🔴 D1 节点归属 race | U1=(a) 读 tape 派生 + per-run 无 multi-run race + 诚实窗口 + E2E 触发升级 (b) |
| 🔴 E3 幂等存储 + 二次方爆炸 | R3 `source_id` 进 data + 内存 set + restart 一次性重建 |
| 🔴 C2 跨进程 flock + 复刻清单缺 | R4 七组件逐字复刻清单 |
| 🔴 N3+N7 kind 不对称 + grep 盲区 | U2=(a) kind 并集 + U3=(a) adapter 强制进 grep 范围 |

## 12. 决策日志（goal 钩子下自主裁决，可纠）

- **U1=(a)** per-run 读 tape 派生 node；E2E 可见错位则升级 (b)。
- **U2=(a)** 接受 kind 并集不对称（CC 无 step_boundary 合法）。
- **U3=(a)** adapter 强制落 `orca/events/adapters/` 进 grep 范围，daemon 落 `orca/iface/in_session/`。
- **U4** defer `agent_usage`（B2 = 过程 only）。

# Web 前端呈现层完善 SPEC（B2 后续：log 降噪 / 子 agent 维度 / 左栏重做 / cac-nga 适配）

> 2026-07-18。状态：**v3，spec-reviewer conditional-pass → 7 P0 + 6 P1 + 4 P2 已闭环（P1-6 经核实为误判，前端 test/ 基建已存在）**。待用户确认后启动 coder-agent。
> 动因：B2（`ed5cbeb`/`99efcde`）把子 agent 过程事件（`agent_tool_call/result/thinking/step_started`）实时推到 tape 后，前端呈现层暴露 4 组症状——根因同源于「子 agent 维度缺失 + 无事件分级」。本 SPEC 给出修复方案，按功能分 4 阶段。
> 参考依据：`references/conductor/REFERENCE.md`（microsoft/conductor @ 32bf0b7）+ 真机 run `runs/agent-struct-exploration-…e3b8ad.jsonl`（4779 事件实证）。

---

## 0. 根因（一图四症）

B2 前 tape 几乎无 `agent_*` 过程事件，LogStream「看起来干净」。B2 后真机 run 实测（精确数）：

| 指标 | 实测值 |
|---|---|
| 总事件 | 4779 |
| `agent_*` 占比 | **99%**（tool_call 2283 / result 1000 / step 587 / thinking 584 / message 294） |
| `family_detect` 单节点事件 | **4226**（agent_* 4224 + node_started 2），**88%** |
| `family_detect` distinct session | **65**（含 2 个 MAIN_NULL = 节点自身），**64 个子 agent** |
| `node_*` 生命周期事件 | 17 |

四组症状 + 共同根因：

| # | 症状（用户反馈） | 根因 | 阶段 |
|---|---|---|---|
| 1 | 右侧 log 暴涨、`agent_tool_call` 没必要显示 | `selectLog`（`selectors.ts:550`）全量 `map` 无 classifier | **P1** |
| 2 | familydetect 对话异常长 | `selectConversation` 按 `node` 分组不区分 `session_id`，64 子 agent 的 4226 事件全堆一流 | **P2** |
| 3 | agent 后端执行一会、前端执行完才显示 | store 每次 `processEvent` **全量 `refold` O(N)**（`workflow-store.ts:429`）+ `selectConversation` 全量 filter + `buildEntries` 全量 fold + markdown 渲染 → 主线程积压 | **P2** |
| 4 | 左栏白底/灰底割裂、有 GAP、排列待美化 | `AgentsRail.tsx:87` 写死 `bg-white` + `w-56`（被百分比 Panel 包裹不匹配）；扁平无分组/迭代/子 agent | **P3** |
| 5 | agent 重复多次 ITERATION 难观测、加载慢 | 同 #2/#3 | **P2** |
| 6 | cac/nga 是否适用 | B2 adapter 路径硬编码，没接既有家族映射 | **P4** |

**关键认知（conductor 收敛）**：conductor 用两个并行 classifier（`buildLogEntry`/`buildActivityLogEntry`）把事件流切成 Log buffer（生命周期/routing/gate/失败/组摘要）与 Activity buffer（reasoning/tool/message）。**Orca 早有这两个区**（右 LogStream ≈ Log、中 ConversationView ≈ Activity），只是 LogStream 没装 classifier。修复 = 给 LogStream 装分级 classifier + 给 ConversationView 加子 agent 维度，**不是加 verbose 开关**。

---

## 全局约束（贯穿）

1. **不改 tape**（铁律 2）：所有降噪/分级/索引是前端 projection 或 store 派生，tape 零改。
2. **grep 守门**：adapter 无 backend 名**条件分支**（家族映射是数据 dict，resolver 是统一函数；P4 给具体 pattern）。
3. **fail loud**：classifier 遇未知 type → `console.warn` + 降级，不静默吞、不 crash。
4. **幂等**：所有派生是 `fold(events)` 纯投影，序无关（D7）。
5. **测试**：纯函数（`classifyLogLevel`/`selectNodeSessions`/resolver）单测 oracle + testid 断言。**前端 test/ 基建已存在**（`agents-rail.test.tsx`/`log-stream.test.tsx`/`conversation.test.tsx` 等），在现有文件加用例即可。真机回归用 `e3b8ad` run。

---

## P1 — LogStream 降噪 classifier（最高 ROI，独立）

### 问题
`selectLog`（`selectors.ts:550`）把每个 tape 事件 1:1 渲染一行，无分级。B2 后过程事件占 99%。git 证实从 Chunk A（`84a2645`）起就是全量 `map`，**从未过滤过**（用户印象中的「之前只显示重要信息」是 B2 前 tape 无 `agent_*` 的假象）。

### 方案
给 `selectLog` 装 `classifyLogLevel` classifier（纯函数，仿 conductor `buildLogEntry`）。Log 只收生命周期/routing/gate/失败/组摘要；过程事件（`agent_message/thinking/tool_call/tool_result/step_started`、`foreach_item_*`）归 ConversationView，不进 Log。引入 `LogLevel` 枚举，`route_taken` 标 `debug` **默认隐藏**（决策已定）。

### 接口契约
```ts
export type LogLevel = "info" | "success" | "error" | "warning" | "debug";
export function classifyLogLevel(type: WebEvent["type"]): LogLevel | null; // null = 不进 Log
export interface LogLine { seq: number; type: WebEvent["type"]; text: string; level: LogLevel; } // 取代 isError
export function selectLog(state: WorkflowState): LogLine[]; // filter：classifyLogLevel 非 null；debug 默认隐藏
```

**分级映射表**（对照 `events.ts` 全 39 EventType + `workflow-store.ts eventHandlers`，codegen 穷尽守门；P0-1 闭环：补 `workflow_resumed`、展开 `dialog_*`）：

| LogLevel | 收入的 EventType |
|---|---|
| `info` | `workflow_started` / `node_started` / `foreach_started` / `retry_started` / `validator_started` / `wait_started` / `human_decision_requested` / `interrupt_requested` / `dialog_started` |
| `success` | `workflow_completed` / **`workflow_resumed`** / `node_completed` / `foreach_completed` / `retry_succeeded` / `validator_passed` / `wait_completed` / `human_decision_resolved` / `interrupt_resolved` / `dialog_ended` |
| `error` | `workflow_failed` / `workflow_cancelled` / `node_failed` / `retry_exhausted` / `validator_failed` / `error` |
| `warning` | `node_skipped` |
| `debug` | `route_taken`（**默认隐藏**，开关展开） |
| **null（不进 Log）** | `agent_message` / `agent_thinking` / `agent_tool_call` / `agent_tool_result` / `agent_step_started` / `foreach_item_started` / `foreach_item_completed` / `prompt_rendered` / `agent_usage` / `custom`(chart) / `dialog_message` / `unknown_event` |

> `dialog_message` 是 agent 级过程（进 ConversationView）；`dialog_started/ended` 是生命周期（进 Log）。

### 验收
1. `e3b8ad` run：LogStream 行数 **4779 → ≤30**（实测 lifecycle 级 27 条，默认隐藏 route_taken 8 条后仅 **19**）。
2. LogStream 不再渲染 `agent_tool_call` / `agent_thinking`（`log-stream.test.tsx` testid 断言）。
3. 过程事件仍完整出现在 ConversationView（零回归）。
4. `route_taken` 默认不显示，开关展开后显示。
5. `LogStream.tsx:42` 改用 `level === "error"` 判红（替换 `item.isError`，testid=log-row-N className 断言）。
6. 单测：`classifyLogLevel` 对全 39 EventType 的 oracle 表（TS `never` 守门编译期穷尽）。

### 依赖 / 风险
- **无依赖**，纯前端 selector + LogStream.tsx 改。与 P4 并行（coder-agent 一起）。

---

## P2 — 子 agent 维度 + 性能（解症状 #2/#3/#5）

### 问题
- `selectConversation`（`selectors.ts:250`）按 `e.node === nodeId` 过滤不区分 `session_id` → 64 子 agent 的 4226 事件全堆一流。
- 性能：store `processEvent` → **全量 `refold` O(N)**（`workflow-store.ts:429-432`）+ `selectConversation` 全量 filter + `buildEntries` 全量 fold 4224 事件 + markdown 渲染 → 主线程积压 → 新事件到了画不出（症状 #3）。
- 循环节点/多子 agent 塌缩一行，ITERATION 不可观测（症状 #5）。

### 方案（4 点；P0-5/P0-6 闭环）
1. **会话按 `(node, session_id)` 分段**：ConversationView 顶部子 agent 选择器（`All(4226) | ses_090e(208) | …`），**默认选第一个子 session**（非 MAIN_NULL、非 All）。一次只 `buildEntries` 该 session（~208 而非 4224）→ buildEntries + 渲染量降一个数量级。
2. **store 增 `nodesIndex` 倒排索引**（`Record<node, { sessions: string[]; sessionSeqs: Record<session, number[]> }>`）；`selectConversation` 从全量 filter 降为索引查表。
3. **【P0-5 新增】store 轻量增量 fold**：`processEvent` 检测 `event.seq > lastSeqSeen`（in-order，WS 增量常态）→ **增量 fold**（只 fold 新事件，patch nodes/nodesIndex，不全量 refold）；`event.seq <= lastSeqSeen`（out-of-order，如 `loadEarlierChunk` prepend 历史）→ 走既有全量 `refold`。解症状 #3 的 refold 主因，幂等不变（in-order 增量 = seq 升序 fold 的特例）。
4. **【P0-6 闭环】nodesIndex 一致性**：nodesIndex 是 store 派生字段（非独立真相），**四路径都重算**——`refold`（重置后 fold）/ `loadFromEvents`（全量 fold）/ `loadEarlierChunk`（prepend 后**必须全量重建** nodesIndex，因 prepend 改变 firstTs/session 集合）/ `loadFull`（全量）。in-order 增量路径（方案 3）同步增量 patch nodesIndex。

> 方案 4（buildEntries 增量化）列 P2-debt——缩 session 后 buildEntries 输入 ~208 事件开销可接受。

### 接口契约
```ts
// store-types.ts
export interface NodeSessionIndex { sessions: string[]; sessionEventCounts: Record<string, number>; }
// WorkflowState 增 nodesIndex: Record<string, NodeSessionIndex>（派生，四路径维护）

// selectors.ts
export interface NodeSessionRow { sessionId: string; label: string; eventCount: number; firstTs: number; }
export function selectNodeSessions(state, nodeId): NodeSessionRow[];
export function selectConversation(state, nodeId, sessionId?: string): ConversationGroup;
//   sessionId 省略/="all" → 全 node 聚合（旧行为，作"All"）；指定 → 仅该 session

// store action（P1-3 闭环）：setSelectedNode(node) 联动重置 selectedSession
//   = 该 node 的第一个 sub session（依赖 nodesIndex）；无 sub → "all"
setSelectedNode: (node: string | null) => void; // 同步设 selectedSession
//   新增 UI 态 selectedSession: string | "all" | null
```

### 验收
1. `family_detect` 会话：顶部出现 64 个子 agent 选项（+ All + MAIN），默认选第一个 sub → 只渲染 ~208 事件（非 4224）。
2. `selectConversation` 不再全量 filter（读 nodesIndex）。
3. 症状 #3「执行完才显示」缓解：**方案 3 in-order 增量 fold** 解 refold 主因 + 方案 1 缩 buildEntries/渲染。
4. 循环节点不同轮子 session 可区分（ITERATION 可观测）。
5. 单测：`selectNodeSessions` oracle（fixture = e3b8ad family_detect 64 sub + 1 MAIN）；in-order 增量 vs out-of-order refold 等价性（D7）。
6. 零回归：选 All = 旧行为；切子 session 内容正确。

### 依赖 / 风险
- 建议在 **P1 后**（P1 降噪后 log 不刷屏，便于观察 P2）。逻辑独立。
- **实施首步 spike**（P2-循环 session 语义，决策记录 §4 前置）：循环回边重入时 `session_id` 是否变化。实测 family_detect 65 distinct 可区分；若循环复用同 session_id 则同组合并（可接受，不丢事件）。

---

## P3 — 左栏 agents 视觉重做（症状 #4）

### 方案方向 = (a) 列表美化（已确认）
保留三栏「列表/会话/log」语义，借鉴 conductor 美化；DAG 浮层（既有）作补充。(b)「DAG 主视觉」作 follow-up。

### 方案（6 点）
1. **底色统一**：`aside` 改 `bg-slate-50`（与中间 tab 栏 `RunDetailPage.tsx:75` 一致）；agent 行白底卡片（`bg-white`+`border`+`rounded`+hover 浅灰）。
2. **根治 GAP**：`AgentsRail.tsx:87` 去 `w-56`，改 `w-full h-full` 填满 `Panel`（`react-resizable-panels` 全弹性，conductor `ResizableLayout` 同款）。
3. **状态色条**：左竖条复用 `NODE_STATUS_HEX`（**`import { NODE_STATUS_HEX } from "@/components/graph/constants"`**，DRY；该常量现用于 DAG 浮层，左栏首次引用）替代文字 icon。`useStatusTransition` 400ms 动画 = **新建 hook**（或列 follow-up）。
4. **阶段分组**：`selectAgentGroups` —— **具体算法（P2-3 闭环）**：遍历 `workflowDef.nodes` 声明顺序；`entry` 之前归 Setup；若某 node 的 `routes` 含 `to ∈ 已访问节点集`（back-route）→ 该 `from` 及其后到下一个 back-route 之间归 Loop；最后 back-route `to` 之后归 Finalize。无 back-route → 全平铺（fallback）。
5. **迭代号**：循环节点显示 `R3`（从 `selectNodeSessions` distinct session 数派生，依赖 P2）。
6. **子 agent 折叠**：`sessionCount > 1` 显示 `▸ family_detect · 64 subs`，展开子 session 列表，点击切该 session 会话（与 P2 `selectedSession` 联动）。

### 接口契约
```ts
export interface AgentGroup { group: string; agents: AgentRow[]; }
export function selectAgentGroups(state): AgentGroup[]; // 上算法
export interface AgentRow { ...; sessionCount?: number; iteration?: number; } // 依赖 P2 nodesIndex
```

### 验收
1. 三栏底色统一（灰底+白卡片），**无 GAP**（`grep -E 'w-56' AgentsRail.tsx` 0 hit；aside `w-full h-full`）。
2. 节点状态用色条（import NODE_STATUS_HEX）；循环节点显示 `R3`；`sessionCount>1` 可折叠展开子 agent。
3. 点击子 agent → 中栏会话切该 session（P2 联动）。
4. 单测：`selectAgentGroups` 分组 oracle（e3b8ad 拓扑 → Setup/Loop/Finalize）；现有 `test/agents-rail.test.tsx` 加色条/折叠 testid。

### 依赖 / 风险
- **依赖 P2**（sessionCount/迭代号/子 agent 折叠）。在 P2 后。
- 风险：分组算法依赖 `workflowDef.routes` 形态——若复杂，fallback 平铺不破功能。

---

## P4 — cac/nga sidechain 路径解析 + doctor 诊断（独立）

### 背景（项目早有 cac/nga 家族概念，唯一缺口在 B2 adapter）
- `skill_cmds.HOST_DOTDIR = {"cc":".claude", "opencode":".opencode", "cac":".cac", "nga":".nga"}`（**P0-2 闭环：常量名是 HOST_DOTDIR，非 SKILL_ROOTS**）。
- `install_cmds` 家族路由：cc 家族（cc+cac）/ opencode 家族（opencode+nga）。
- `_host_session_from_env`（`cli.py:103`）已家族对称：`ORCA_HOST_SESSION_ID or CLAUDE_CODE_SESSION_ID`。cc/cac 走 `CLAUDE_CODE_SESSION_ID`。
- **events 层目前零 import iface**（grep 验证）→ P4 resolver 必须保持。
- **缺口**：B2 adapter（`cc_jsonl.py:100`/`opencode_sqlite.py:83`）路径硬编码，没接家族映射。

### 关键约束
cac 与 cc env **完全一样**（`CLAUDE_CODE_SESSION_ID`，cac 是 CC 换皮）→ env 区分不出 cc vs cac，靠**路径探测 + config 声明**（doctor 主责）。

### 方案（复用家族映射 + 探测 + doctor；P0-3/P0-4/P1-1/P1-2 闭环）
1. **抽共享 sidechain root resolver**（events 层 neutral，`orca/events/adapters/_family.py`，adapter + doctor 共用，DRY）：
   - 家族→dotdir 映射同源 `HOST_DOTDIR`（events 内独立声明 dict + 注释「与 `orca.iface.cli.skill_cmds.HOST_DOTDIR` 同源」——**不跨层 import iface**）。
   - **CC 家族** resolver 解析顺序（**family 决策全在 resolver 内部**，不依赖 daemon `--backend` 参数）：
     1. `ORCA_CC_SIDECHAIN_ROOT`（整体覆盖，已有）→ source=`"env"`
     2. `family` 参数显式（由 iface caller 从 config 读 `sidechain.family` 传入）→ `~/.<dotdir>/projects/...` → source=`"config"`
     3. **探测**：`~/.cac/projects/<enc>/<session>/subagents` 与 `~/.claude/projects/...` 哪个存在。**两存歧义 → 默认 `.claude`（保守，不破坏 cc 用户）**，source=`"probe"`，doctor 报告歧义提示设 config → source=`"probe"`
     4. 默认 `~/.claude` → source=`"default"`
   - **opencode 家族** resolver 同理（`ORCA_OPENCODE_DB` > config/probe `.nga`/`.opencode` > 默认）。
2. **【P0-4 闭环】_family.py 零 iface import**：resolver **只接收 `family` 参数，不读 config**。config 读取（新字段 `sidechain.family`）由 **iface 层 caller**（doctor / daemon 启动期，`cli.py`）做，结果作为 `family="cac"` 传 resolver。
3. **host_session 零改 + 【P0-7 fallback】**：`_host_session_from_env` 不改。**假设** cac 注入 `CLAUDE_CODE_SESSION_ID`（用户确认默认同 CC；未实证 → 见 spike）。fallback：若 env 缺，daemon 接受显式 `--host-session`（`sidechain_daemon.py:454` 已有），doctor `sidechain_backend` check 检测到无 env → fail-loud 提示「未检测到 host_session env，B2 不可用；设 sidechain.host_session 或显式 --host-session」。
4. **doctor 增强**：加可选 check（`hard=False`）`sidechain_backend`：检测家族（env+config+probe）+ **输出 resolved root/DB + source + 存在性 + host_session 可用性 + 修复建议**（「从 doctor 获取」）。

### 接口契约（P0-3 闭环：签名返 tuple）
```python
# orca/events/adapters/_family.py（新，events 层，零 iface import）
CC_FAMILY_DOTDIR = {"cc": ".claude", "cac": ".cac"}
OPENCODE_FAMILY_DOTDIR = {"opencode": ".opencode", "nga": ".nga"}  # 注释：与 skill_cmds.HOST_DOTDIR 同源

def resolve_cc_sidechain_root(host_session, *, cwd=None, family=None) -> tuple[Path, str]:
    """返回 (root, source)；source ∈ {"env","config","probe","default"}。
    解析：ORCA_CC_SIDECHAIN_ROOT(env) > family(config) > 探测 .cac/.claude(歧义默认.claude) > 默认.claude"""
def resolve_opencode_db(*, family=None) -> tuple[Path, str]: ...

# cc_jsonl.py / opencode_sqlite.py 改调 resolver（_family.py 在同包，合法依赖）。
# cli.py（iface 层）：daemon 启动 / doctor 读 config sidechain.family → 传 resolver 的 family 参数。
# doctor check：sidechain_backend = { family, resolved_root, root_source, root_exists, host_session_set, available, hint }
```

### 验收
1. cac 环境（`~/.cac/projects/<enc>/<session>/subagents` 存在 + 无 .claude）：CC adapter **自动读 .cac**（resolver probe 胜）。
2. cc+cac 同装无 config：默认 `.claude` + doctor 报告歧义 hint。
3. `orca doctor` 新增 `sidechain_backend` check，打印 family + resolved root + source + 可用性 + 建议。
4. **grep 守门**（P2-4 给具体 pattern）：`rg 'if\s+\w+\s*==\s*"(cac|nga)"' orca/events/adapters/` **0 hit**；允许 dict 字面量 `{"cac":".cac"}` 与 `for family in (...)` 迭代。
5. **依赖守门**：`rg 'from orca.iface|import orca.iface' orca/events/` **0 hit**（_family.py 零 iface import，维持现状）。
6. 单测：resolver 四优先级 oracle + 同源 HOST_DOTDIR 一致性断言 + 探测歧义默认 .claude。
7. host_session 零改回归（`_host_session_from_env` 既有测试不动）。

### 依赖 / 风险
- **无依赖**，与 P1 并行（coder-agent 一起）。
- **【P0-7 实施前置 spike】（用户侧真机）**：P4 实现可先做（resolver/doctor/fallback 都不依赖此），但上线前需用户在真机 cac 跑一次确认：cac 是否真注入 `CLAUDE_CODE_SESSION_ID`。若否 → 用 fallback（显式 `--host-session` / doctor fail-loud），不阻塞实现。

---

## P5 — Web 美化（图表可读性 + 去 cost + 整体设计 token）

> 独立于 P2（不依赖 nodesIndex）。**用户决策（2026-07-18 /goal）：不插队——P1→P4→P2→P3 + test-agent 统测后，单独用 coder-agent 实现。**

### 问题（用户 2026-07-18 反馈）
- 图表坐标轴字体太暗（`chartTheme.getAxisTick` 用 `--muted-foreground`=slate-500，对比度不足，fontSize 11）。
- hover 黄色刺眼（`LineChartWidget`/`AreaChartWidget`/Scatter 的 `<Tooltip>` **缺 `cursor`**，recharts 默认高亮带 + PALETTE 暖色系列 amber`#E29D3E`/gold`#C9A843` 显黄；BarChart 已设 `cursor:{fill:"rgba(0,0,0,0.04)"}` 但其他没设）。
- 整体风格缺统一 token（组件散落硬编码 `slate-*`）、无品牌强调色、左白中灰割裂。
- TopBar 计费显示需去除。

### 方案
**P5a 图表可读性 + 去 cost（半天）：**
1. `index.css` 新增 `--axis-tick`（亮 slate-700 / 暗 slate-300）+ `--accent`（钢蓝 `91 141 184` = PALETTE[0]，品牌强调色）。
2. `chartTheme.ts`：`getAxisTick` fill 用 `--axis-tick` + fontSize 11→12；新增 `getCursor(line: boolean)`（线/面/散点=细虚竖线无填充；柱/pareto=极淡灰 `rgba(0,0,0,0.04)`）。
3. 各 widget（Line/Area/Bar/Scatter/Pareto/Radar）`<Tooltip>` 统一 `cursor={getCursor(...)}` + 补 `labelStyle`/`itemStyle` 中性色防默认。
4. `TopBar.tsx` 删 cost span + `top-cost` testid（`store.cost` 保留，不破坏 fold/幂等）。

**P5b 整体设计 token（1 天，渐进迁移）：**
5. `index.css` 扩展明暗双套 token：`--app-bg`/`--surface`/`--surface-2`/`--border`/`--text`/`--text-muted`/`--text-faint`/`--accent`。
6. 字体：系统栈 + `tabular-nums`（body；坐标轴/表格数字对齐）。
7. 关键组件迁移到 `var(--token)`：TopBar / AgentsRail（P3 已改底色，对齐）/ 图表卡 / ConversationView 卡片化；`tailwind.config` `orca` 色板对齐 token。
8. 渐进策略：新组件强制 token，旧组件按需（不一次性重写）。

**设计原则**：中性灰底 + 单一钢蓝强调色（=PALETTE[0]，品牌与图表第一色一致）；4 级文字层次（text/text-muted/text-faint + title）；3 级表面（app-bg/surface/surface-2）；卡片化 + 统一圆角。对标 Linear/Vercel/Grafana。

### 接口契约
```css
/* index.css */
:root {
  --app-bg: 248 250 252;  --surface: 255 255 255;  --surface-2: 241 245 249;
  --border: 226 232 240;  --text: 15 23 42;  --text-muted: 71 85 105;
  --text-faint: 148 163 184;  --axis-tick: 51 65 85;  --accent: 91 141 184;
}
@media (prefers-color-scheme: dark) {
  :root { --app-bg: 15 23 42; --surface: 30 41 59; --surface-2: 51 65 85;
    --border: 51 65 85; --text: 226 232 240; --text-muted: 148 163 184;
    --text-faint: 100 116 139; --axis-tick: 203 213 225; }
}
```
```ts
// chartTheme.ts
getAxisTick(): { fontSize: 12, fill: getCSSVar("--axis-tick") }
getCursor(line) => line
  ? { stroke: getCSSVar("--border"), strokeWidth: 1, strokeDasharray: "3 3" }
  : { fill: "rgba(0,0,0,0.04)" }
// TopBar.tsx: 删除 <span data-testid="top-cost">🪙 ${cost.toFixed(4)}</span>
```

### 验收
1. 坐标轴字 slate-700（暗模式 slate-300）清晰可读，fontSize 12。
2. hover 无黄色高亮（所有 widget cursor 统一）。
3. TopBar 无 🪙 cost（`top-cost` testid 不存在）。
4. token 明暗双套；强调色钢蓝用于 active tab/选中/链接。
5. 三栏底色统一（`--app-bg`），无割裂。
6. 零回归：图表数据渲染不变（仅样式）；TopBar status/elapsed 不变。

### 依赖
- **独立于 P2**。**用户决策：不插队**——P1→P4→P2→P3→test-agent 统测后，单独 coder-agent 做 P5（P5a 半天 + P5b 1 天）。

---

## 实施顺序与依赖图

```
P1 (LogStream 降噪)     ── 独立，先做，ROI 最高          ┐
P4 (cac/nga + doctor)   ── 独立，与 P1 并行（coder-agent） ┘  ← 工作量少，一起做
P2 (子agent维度+性能)   ── 独立，P1 后（含 store 增量 fold + nodesIndex）
P3 (左栏视觉重做)       ── 依赖 P2，P2 后
P5 (web 美化)           ── test 统测后单独 coder-agent（独立于 P2，不插队）
```

**推进**：
1. ✅ spec-reviewer 一次评 4 阶段（conditional-pass，P0/P1/P2 已闭环入 v3）。
2. **coder-agent 并行 P1 + P4**（各 P0 已解，独立）→ 各自 code-reviewer 自检。
3. P2 → P3（工作量大，后续 coder-agent 逐个）。
4. **test-agent 统一真机测试**（e3b8ad：P1 行数 / P2 分段 / P3 视觉 / P4 cac 探测）。
5. 每阶段 release note + CHANGELOG + CURRENT 更新。

---

## 决策记录（用户 2026-07-18 确认 + review 闭环）

1. **P3 方向 = (a) 列表美化** ✓。
2. **P4 cac host_session = 同 CC**（`_host_session_from_env` 零改）✓ + 增强 doctor 诊断输出 resolved 路径。**review P0-7**：加 fallback（显式 `--host-session` + doctor fail-loud）+ 用户侧真机 spike 前置（不阻塞实现）。
3. **P1 `route_taken` = 默认隐藏**（debug 级）✓。
4. **P2 循环节点 session_id 语义** —— **前置到 P2 实施首步 spike**（影响 P3 iteration 派生）。
5. **推进方式**：spec-reviewer 一次评 4 阶段 → coder-agent 并行 P1+P4 → P2/P3 → test-agent 统测 ✓。
6. **P5 web 美化**：方案认可（图表可读性 + 去 cost + 整体设计 token）；**不插队**——P1-P4 + test 统测后单独 coder-agent 实现 ✓。

### spec-reviewer review 闭环（v3）
- **conditional-pass → 7 P0 全闭**：P0-1（补 workflow_resumed）/ P0-2（HOST_DOTDIR）/ P0-3（resolver 返 tuple）/ P0-4（_family.py 零 iface import）/ P0-5（P2 加轻量增量 fold）/ P0-6（nodesIndex 四路径重算）/ P0-7（cac env fallback + spike）。
- **6 P1 闭 5**：P1-1（family 决策在 resolver）/ P1-2（探测歧义默认 .claude）/ P1-3（setSelectedNode 联动）/ P1-4（LogStream level）/ P1-5（w-56 grep 守门）。**P1-6 不采纳（误判）**：前端 `test/` 基建已存在（agents-rail.test.tsx 等），spec-reviewer 漏看 `test/` 目录。
- **4 P2 全闭**：P2-1（精确数 4226/65/64，Log ≤30 实测 19）/ P2-2（NODE_STATUS_HEX import 路径）/ P2-3（selectAgentGroups 具体算法）/ P2-4（grep pattern）。

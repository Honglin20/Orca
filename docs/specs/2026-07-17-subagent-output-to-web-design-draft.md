# 子 agent 输出/过程推送 web —— 设计草稿

> **状态**：Draft（2026-07-17），待 spec-reviewer 评审。**B1（前端渲染 output）完整可实施；B2（sidechain→tape ingestor）框架 + 待 review 深化**。
> **前置**：依赖 SPEC-A（`host_session` 注入，`docs/specs/2026-07-17-host-session-binding-design-draft.md`）——B2 用 host_session 定位 CC sidechain。串台交付后开工。
> **根因调研**：2026-07-17 Explore 全链路诊断（output 在 tape，前端 `node-divider` 丢弃）。

---

## 0. 目标

in-session 路径下用 tars skill 跑 workflow，web 前端要能显示：
1. **节点 output 文字**（`orca next --output` 的产出）——当前完全不显示（只有 chart 链接）。
2. **子 agent 执行过程**（thinking / tool_use / tool_result）——in-session 下根本不产生这些事件。

---

## 1. 现状根因（Explore 坐实，三层）

| 层 | 状态 | 证据 |
|---|---|---|
| **output 进 tape** | ✅ 正常 | `step.py:337` `node_completed.data.output`；真实 tape `runs/nas-hp-search-*.jsonl` 实测含完整 output 文字 |
| **WS 透传** | ✅ 正常 | `ws_handler.py:233-243` 全量转发，无类型过滤 |
| **前端渲染** | ❌ **丢弃** | `entries.ts:63-67` 把 `node_completed` 归 `NODE_DIVIDER_TYPES` → `NodeDivider.tsx:15-17` 只渲染「■ node completed」，**不读 `data.output`** |

**对比**：`custom/data.kind=chart` 有完整渲染链（`chart-ref`→`ChartWidget`），所以图显；node output 无渲染器，所以文不显。

**第二缺口**：in-session 路径下子 agent 由宿主 session 派发、不经 ClaudeExecutor → **`agent_message`/`agent_thinking`/`agent_tool_call`/`agent_tool_result` 这些事件根本不产生**（web/tars-run 路径才产生）。所以会话页签对 in-session run 天然空旷。子 agent 过程在宿主自己的存储里（CC: `<host_session>/subagents/agent-<task_id>.jsonl`；opencode: sqlite `session.parent_id`）。

---

## 2. 方案（两层，独立交付）

### B1 —— 前端渲染 node_completed.data.output（纯前端，先做）
output 已在 tape，只补前端渲染。**后端零改动，确定性。** 解用户当前痛点。

### B2 —— sidechain→tape ingestor（子 agent 过程，深需求）
确定性 ingestor 读宿主 sidechain（CC jsonl / opencode sqlite）→ 转统一 `agent_*` tape 事件 → web 复用现有渲染器。CC adapter 必须、opencode 可选。

---

## 3. B1 契约（纯前端）

### 3.1 `entries.ts`
- `NODE_DIVIDER_TYPES`（:63-67）**移除 `node_completed`**（保留 `node_started`/`node_skipped` 作 divider，保边界感）。
- `ConvEntry` 联合（:28-42）新增：`| { kind: "node-output"; event: WebEvent }`。
- `buildEntries`（:195-198）：`node_completed` 命中 → `entries.push({ kind: "node-output", event: e })`（不再进 node-divider 分支）。

### 3.2 新增 `NodeOutputBlock.tsx`（仿 `MessageBlock.tsx`）
渲染 `event.data.output`（MarkdownText）。顶/底加细线兼顾边界感。`data-testid="node-output"`。

### 3.3 `ConversationView.tsx`
- `EntryRenderer` switch（:148-205）加 `case "node-output": return <NodeOutputBlock .../>`。
- `estimateRowHeight`（:222-247）加 node-output 高估值（参考 `message: 160`）。

### 3.4 可选增强 `selectors.ts`
- `eventDetail("node_completed")`（:456-457）：output 截断 60 字加进 LogStream 摘要（`node completed (3.2s): OUTPUT_DIR: …`）。

---

## 4. B2 设计（sidechain→tape ingestor，框架 + 待 review 深化）

### 4.1 架构（符合「tape 唯一真相源 + 无多套接口」）
```
宿主 session 跑子 agent → sidechain 落宿主存储
   CC:    ~/.claude/projects/<cwd>/<host_session>/subagents/agent-<task_id>.jsonl  （含 thinking + tool_use）
   opencode: sqlite session(parent_id) → message → part.data
        │
        ▼  【确定性 ingestor】（读 transcript → 结构化，无模型判断）
   adapter（CC / opencode 各一）→ 统一产出 agent_* 事件
        │
        ▼  EventBus → Tape.append（单一写路径）
   tape（唯一真相源）
        │
        ▼  ws_handler pump（已有，零改）
   web 前端：复用现有 agent_message/agent_tool_*/agent_thinking 渲染器（entries.ts 已支持，零改）
```

**单一接口**：CC/opencode 是两个 adapter（读不同源 sidechain），产出**统一的 `agent_*` tape 事件** → 无多套接口。
**单一真相源**：sidechain 是数据源（input），tape 是唯一真相源（output），ingestor 是确定性转换器（同 `chart_ingestor` 读 socket 写 tape 模式）。tape 自包含，不依赖 sidechain 持续存在。

### 4.2 待 review 深化的关键点
| # | 待定 | 候选 | 倾向 |
|---|---|---|---|
| 1 | **触发/生命周期** | (a) `orca next` 时同步读 host_session 的 subagents/ 增量（去重已转 task_id）；(b) detached 守护 tail（复用 chart_daemon 模式） | (a) per-call 确定性，符合 in-session 模型；但需 run 级「已转 task_id」状态 |
| 2 | **事件映射** | CC jsonl 的 thinking/bash tool_use/tool_result → `agent_thinking`/`agent_tool_call`/`agent_tool_result`；opencode part.data → 同 | 复用现有 `agent_*` 类型（web 已渲染），前端零改 |
| 3 | **定位该节点的 sidechain** | 按 mtime 时间窗（上次 next→本次）+ task_id 去重 | 需防「主 session 中途 spawn 非 orca 子 agent」误转 |
| 4 | **opencode adapter** | sqlite 读 `WHERE parent_id=host_session` 的 message/part | 可选；用户允许可暂缓 |

### 4.3 不破坏单一真相源的论证（用户铁律）
1. sidechain 是**宿主运行时产物**（数据源/输入），不是 orca 的真相源。
2. ingestor 是**确定性转换器**（读 transcript → 结构化 `agent_*` 事件，无模型判断）。
3. tape 是**唯一真相源**（append-only），ingestor 写入后 tape 自包含。
4. 类比 `chart_ingestor`（socket→tape）：外部产物经确定性转换入 tape，tape 是真相源。
5. **「破坏单一真相源」定义**（同 SPEC-A §3.4）：= 两路独立采集可发散。B2 是单路（sidechain→ingestor→tape），不构成破坏。
6. **若 spec-reviewer 认定 B2 破坏真相源 → 停止、出报告、明天讨论**（用户铁律）。

---

## 5. 验收

### B1
1. in-session 跑 workflow，节点完成后 web 前端**显示 output 文字**（不只 chart 链接）。
2. `node_started` 仍显 divider（▶ started）；`node_completed` 显 output block。
3. 前端单测：`buildEntries` 对 `node_completed` 产 `node-output`（非 node-divider）。

### B2（CC 必须 / opencode 可选）
4. in-session 跑 workflow，子 agent 的 thinking / tool_use 在 web 显示。
5. tape 含 `agent_*` 事件（ingestor 转入），`==` sidechain 内容（确定性，可重放校验）。
6. CC adapter 必须；opencode adapter 可选（困难可暂缓）。
7. 单一真相源：web 只读 tape（不直接读 sidechain）。

---

## 6. 风险 / 待定

| # | 项 | 处理 |
|---|---|---|
| 1 | B2 触发时序（next 时 sidechain 是否完整） | 子代理产出后主 session 才调 next → sidechain 应完整；review 验证 |
| 2 | B2 误转非 orca 子 agent sidechain | task_id 去重 + 时间窗；review 定边界 |
| 3 | B2 opencode adapter 可行性 | 用户允许暂缓；CC 先行 |
| 4 | B2 是否破坏单一真相源 | spec-reviewer 重点审；若破坏→停止报告 |

---

## 7. 决策清单（待 review 冻结）

1. **B1 纯前端**（output 已在 tape，补渲染器），先交付解痛点。
2. **B2 ingestor** 复用现有 `agent_*` 事件 + 前端渲染器（零改前端），CC/opencode 两个 adapter 产统一事件。
3. **tape 唯一真相源不破**（sidechain 是数据源，ingestor 确定性转换）。
4. B2 触发倾向「next 时同步增量读」（待 review 定）。
5. CC adapter 必须；opencode 可选（可暂缓）。

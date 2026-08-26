# B1 实施计划：前端渲染 node_completed.data.output

> SPEC-B B1（spec-reviewer conditional-pass，修两个洞后可实施）。解用户痛点：in-session 路径 web 前端**不显示节点 output 文字**（只有 chart 链接）。
> 根因（Explore 坐实）：output 在 tape `node_completed.data.output`，但前端 `entries.ts:63-67` 把 `node_completed` 归 `NODE_DIVIDER_TYPES`，`NodeDivider.tsx:15-17` 不读 `data.output`。

## 目标
`node_completed` 不再被前端归为 divider（丢弃 output），改为渲染 output 内容。**纯前端，后端零改动**。

## 实施（spec-reviewer 修订后）

### 1. `entries.ts`
- `NODE_DIVIDER_TYPES`（:63-67）**移除 `node_completed`**（保留 `node_started`/`node_skipped` 作 divider，保边界感；output 即完成信号，故 completed 升格为 block）。
- `ConvEntry` 联合（:28-42）新增 `| { kind: "node-output"; event: WebEvent }`。
- `buildEntries`（:195-198）：`node_completed` 命中 → `push({ kind: "node-output", event: e })`（不再进 node-divider 分支）。

### 2. 新增 `NodeOutputBlock.tsx`（仿 `MessageBlock.tsx`）
按 `typeof event.data.output` 分支（spec-reviewer BLOCKER 3a，`step.py:127-167` `_parse_output` 返 str|dict）：
- `"string"` → `<MarkdownText>`
- `"object"`（节点声明 output_schema 时返 dict）→ `<pre>{JSON.stringify(output, null, 2)}</pre>`
- `null`/`undefined` → dim「（无 output）」
- `data-testid="node-output"`，顶/底细线兼顾边界感。

### 3. `ConversationView.tsx`
- `EntryRenderer`（:148-206）加 `case "node-output": return <NodeOutputBlock .../>`。
- `estimateRowHeight`（:222-248）加 `node-output: 160`（同 message，不折叠；超长 output >2000 字加「展开/收起」，同 MessageBlock 约定）。

### 4. 【删 elapsed】（spec-reviewer MAJOR 3b）
in-session `step.py:345` emit 的 `node_completed.data` **只含 output、不含 elapsed**。B1-core 纯前端零后端，**不依赖 elapsed**。LogStream 摘要 elapsed 属 B1-enhancement（需 1 行后端改动），单列 defer。

## 验收（spec-reviewer 去模糊）
1. `node_completed` 渲染为 `node-output` block（`data-testid=node-output` 存在），含 `data.output` 文本；`chart-ref` 独立渲染不受影响。
2. `node_started` 渲染 `▶ ... started` divider（`data-testid=node-divider`）；`node_completed` 不再进 node-divider 分支。
3. 节点声明 output_schema（output 为 dict）时，NodeOutputBlock 以 JSON 预览渲染，**不崩、不显 `[object Object]`**。
4. 前端单测：`buildEntries` 对 `node_completed` 产 `node-output`（非 node-divider）；dict output fixture 走 JSON 分支。

## 测试
- **前端单测**（entries.ts buildEntries）：`node_completed` → `node-output`；dict output fixture；null output → dim 占位。
- **test-agent E2E**：in-session 跑 wf（真实 tape 含 `node_completed.data.output`），web 前端显示节点 output 文字（不只 chart 链接）。

## 不做（B2 暂缓）
B2（sidechain→tape ingestor，子 agent **过程**推送）经 spec-reviewer fail（5 设计洞 + 4 用户决策点 U1-U4 + SoT 灰色），**暂缓待用户决策**：
- U1：agent 过程事件完整性保证线（SoT 铁律是否触发 / best-effort 投影）。
- U2：opencode adapter commit 还是正式 defer。
- U3：host_session 范围扩张（B2 用它定位 sidechain）——涉及刚交付的 SPEC-A。
- U4：task_id 获取协议（扩展驱动协议 hook+`--task-id`，需 spike CC PostToolUse）。
B1 已满足用户验收「子 agent 输出推送 web 前端」（output 显示）。

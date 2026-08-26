# B1 前端渲染 node_completed output（子 agent 输出推送 web）

> 2026-07-17。SPEC-B B1（`docs/specs/2026-07-17-subagent-output-to-web-design-draft.md`，spec-reviewer conditional-pass）。plan：`docs/plans/2026-07-17-subagent-output-b1.md`。
> 解用户痛点：in-session 跑 wf，web 前端只显示 chart 链接、**不显示节点 output 文字**。

## 问题（Explore 坐实）
output 一直在 tape `node_completed.data.output`（`step.py`/`result_extractor.py`），但前端 `entries.ts:63-67` 把 `node_completed` 归 `NODE_DIVIDER_TYPES`，`NodeDivider.tsx:15-17` 只渲染「■ node completed」、**不读 `data.output`** → 文字被丢。chart 有完整渲染链（`chart-ref`→`ChartWidget`），所以图显文不显。

## 方案（B1 纯前端，后端零改动）
- `entries.ts`：`node_completed` 移出 `NODE_DIVIDER_TYPES`（保留 `node_started`/`node_skipped` 作 divider）+ `ConvEntry` 加 `node-output` kind + `buildEntries` 分派。
- `NodeOutputBlock.tsx`（**新增**，仿 `MessageBlock`）：按 `typeof data.output` 分支 —— string→`MarkdownText` / object→`<pre>{safeJson(output)}</pre>` / null→dim「（无 output）」。顶细线 + 「■ node output」标签承担边界感。
- `ConversationView.tsx`：`EntryRenderer` 加 `case "node-output"`（接 `never` 穷尽检查）+ `estimateRowHeight: 160`。
- **删虚构 elapsed**（spec-reviewer MAJOR）：in-session `node_completed.data` 只含 output、无 elapsed；B1-core 纯前端不依赖 elapsed。

## 实现（4 commits）
1. `75116a0` NodeOutputBlock 新增（按 typeof 三分支）
2. `812e0eb` entries.ts（移出 + node-output kind）+ ConversationView + conversation.test folding oracle
3. `6a34c2d` node-output.test（**17 例**）
4. `8ebe45d` code-reviewer 🟡 修复：`safeJson`（防循环引用/bigint throw 炸渲染）+ 删 NodeDivider 死分支

**spec-reviewer 三必修全坐实**：dict 分支（BLOCKER，`_parse_output`/`extract_and_validate` 返 str|dict）/ 删 elapsed / 保留 node_started divider。code-reviewer 0 🔴、3 🟡（2 修 + 1 KISS defer：不抽 `<Rail>` 视觉组件，三处 markup 语义差异真实）。

## 验证（test-agent 真机，全 PASS 零 bug）
- **生产 build 干净**（`tsc --noEmit` + `vite build` exit 0），bundle 守门确认含 B1 代码（`node-output`/`node-output-json`/`node-output-empty` testid + 「（无 output）」文案）。
- **真 `tars serve` + HTTP**：attach 两个真实 tape（`nas-hp-search` 2 string 节点 + `agent-struct-exploration` 11 节点含 **9 dict**）。
- **真事件经 HTTP 类型完整**：dict output 经 FastAPI 序列化后是真 JSON object（正是裸 React 会渲染成 `[object Object]` 的那种）。
- **`react-dom/server` 渲染真事件 → 真 DOM**：13 节点全正确（string→MarkdownText / dict→`node-output-json` 含完整字段），**零 `[object Object]` leak**。
- **17 前端单测**复跑 PASS；conversation folding oracle 同步更新 26 例过；全量 278/279（唯一 fail 是既有 baseline `agents-rail` lazy chunk 超时，与 B1 无关）。
- **缺口**（诚实登记）：无 headless 浏览器（playwright/puppeteer 未装），未做真实浏览器截图；react-dom/server 渲染已产预期 DOM + bundle 含代码。**manual 确认步骤**（30 秒）：`orca open <run_id>` → 选已完成节点 → 看「■ node output」+ 文字（string）或 JSON（dict），无 `[object Object]`。

## B2 暂缓（待用户决策）
B2（sidechain→tape ingestor，子 agent **过程**推送）经 spec-reviewer **fail / 退回设计**：5 个 load-bearing 设计洞（幂等 key / task_id 来源 / 字段映射 / flush 时序 / opencode scope）+ 4 个用户决策点（U1 完整性保证线 / U2 opencode scope / U3 host_session 扩张 / U4 task_id 协议）+ SoT 灰色（严格定义不破坏，但 chart_ingestor 类比破裂引出完整性/幂等真实风险）。B1 已满足用户验收「子 agent 输出推送 web」（output 显示）；B2（过程）是「最好能」，待用户拍板。

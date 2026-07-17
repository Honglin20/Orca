# Web 前端呈现层完善（P1-P5：log 降噪 / 子 agent 维度 / 左栏重做 / cac-nga / 美化）

> 2026-07-18。SPEC：[`docs/specs/2026-07-18-web-presentation-refinement.md`](../specs/2026-07-18-web-presentation-refinement.md) v3（spec-reviewer conditional-pass → 7 P0 + 5 P1 + 4 P2 全闭环）。
> 动因：B2（`ed5cbeb`/`99efcde`）把子 agent 过程事件实时推 tape 后，前端呈现层暴露 6 痛点，根因同源于「子 agent 维度缺失 + 无事件分级」。参考 microsoft/conductor（[`references/conductor/REFERENCE.md`](../../references/conductor/REFERENCE.md)）双 classifier 分流哲学。
> 真机基准：`runs/agent-struct-exploration-…e3b8ad.jsonl`（4779 事件；family_detect 4226 事件 / 65 session = 64 sub + 1 main）。

## commits

| 阶段 | commit | 摘要 |
|---|---|---|
| SPEC | `3057d82` | 5 阶段设计契约 v3 |
| P1 | `0a4683d` | LogStream 分级 classifier 降噪 |
| P4 | `b77422f` | cac/nga sidechain 路径解析 + doctor sidechain_backend |
| P2 | `3a0f66e` | 子 agent 维度 + 性能（会话 session 分段 + store 增量 fold + nodesIndex） |
| P3 | `7cc232e` | 左栏视觉重做（统一底色 + 根治 GAP + 状态色条 + 阶段分组 + 迭代号 + 子 agent 折叠） |
| P5 | `f0cf695` | 美化（图表可读性 + 去 cost + 整体设计 token） |
| 构建产物 | （本 release） | static/ 重新生成 |

## P1 — LogStream 分级 classifier 降噪（`0a4683d`）

**问题**：`selectLog`（`selectors.ts:550`）全量 `map` 每事件一行，B2 后过程事件占 99%（tool_call 2283 / thinking 584 ...），淹没重要信息。git 证实从未过滤过。

**方案**：`classifyLogLevel` 纯函数（仿 conductor `buildLogEntry`）把事件分 5 级（info/success/error/warning/debug）或 null。Log 只收生命周期/routing/gate/失败/组摘要；过程事件（agent_*/foreach_item_*/prompt_rendered）归 ConversationView。`route_taken`=debug 默认隐藏（`setLogShowDebug` 展开）。`LogLine.isError`→`level`（5 级配色）。

**验收**：e3b8ad LogStream **4779 → 19 行**（histogram info:10/success:8/error:1，默认隐藏 route_taken 8）；`agent_tool_call`/`agent_thinking` 真未渲染（react-dom/server 断言）；过程事件仍在 ConversationView（零回归）；tsc `never` 穷尽守门。22 单测 + reviewer PASS 0🔴。

## P2 — 子 agent 维度 + 性能（`3a0f66e`）

**问题**：`selectConversation` 按 node 分组不区分 session_id → family_detect 64 子 agent 的 4226 事件全堆一流；store 每次 `processEvent` 全量 `refold` O(N) + buildEntries 全量 fold → 主线程积压 →「执行完才显示」。

**方案**：① 会话按 `(node, session_id)` 分段（SessionTabs：All/main/sub，默认选第一个 sub → buildEntries 4226→~208）；② store `nodesIndex` 倒排索引（refold/loadFromEvents/loadEarlierChunk/loadFull/增量 五路径维护）；③ processEvent 双路径（in-order 增量 fold / out-of-order refold，D7 等价）；④ `selectedSession` + `setSelectedNode` 联动（选第一个 sub）；⑤ `eventMatchesNode` 抽取（workflow_failed DRY）→ 新 `conversation-types.ts`。

**spike**：family_detect 65 session（64 sub + 1 main），循环回边 session_id 每轮变化（ITERATION 可区分）。

**验收**：SessionTabs 66 testid（64 sub + All + main）；first sub → 10 events（all=4226）；D7 增量 vs refold 等价单测；70 测试 pass + reviewer 无🔴。AC#2 drift（nodesIndex 供选择器、selectConversation 仍 filter）诚实标注——性能主因 buildEntries 缩量已解。

## P3 — 左栏视觉重做（`7cc232e`）

**问题**：`AgentsRail` aside `bg-white`（与中栏灰底割裂）+ 写死 `w-56`（被百分比 Panel 包裹产生 GAP）+ 扁平无分组/迭代/子 agent。

**方案（(a) 列表美化）**：① 底色统一 `bg-slate-50` + agent 行白卡片；② 去 `w-56` → `w-full h-full`（根治 GAP）；③ 状态色条 `import NODE_STATUS_HEX`（DRY，与 DAG 浮层同源）替代文字 icon；④ `selectAgentGroups` back-route 算法 → Setup/Loop/Finalize 分组；⑤ 循环节点 `R{iteration}` 徽章；⑥ 子 agent 折叠（sessionCount>1 → `▸ N subs`，点子 session 切会话，P2 联动）。

**验收**：`grep w-56` 0 hit；NODE_STATUS_HEX `#22C55E` 色条真渲染；Setup/Loop/Finalize 分组真出现；family_detect `▸ 64 subs` 折叠；13 测试 pass + reviewer PASS。

## P4 — cac/nga sidechain 路径解析 + doctor（`b77422f`）

**问题**：B2 adapter 路径硬编码（`~/.claude`/`~/.local/share/opencode`），cac/nga 读不到。项目早有家族概念（`skill_cmds.HOST_DOTDIR`/家族路由/`_host_session_from_env` 家族对称），唯一缺口在 adapter。

**方案**：① 新 `orca/events/adapters/_family.py`（**零 iface import**）`resolve_cc_sidechain_root`/`resolve_opencode_db` 返 `tuple[Path,str]`，优先级 env>config>probe>default，探测歧义默认 `.claude`；② cc_jsonl/opencode_sqlite 改调 resolver（ctor family 参数）；③ doctor 加 `sidechain_backend` check（输出 resolved root + 可用性，「从 doctor 获取」）；④ daemon `--family` argv + config `sidechain.family`（iface 层读 config 传 resolver）；⑤ host_session 零改（`_host_session_from_env` 已家族对称）+ fallback 显式 `--host-session`。

**验收**：`orca doctor` sidechain_backend 真输出（family/resolved_root/root_source/available 字段齐）；34 单测 + grep 守门 0 hit + AST 验证 events 零 iface import。**P0-7 cac env spike 留用户侧真机**（本环境无 cac，单测 mock 覆盖）。

## P5 — Web 美化（`f0cf695`）

**问题**（用户反馈）：图表坐标轴太暗（`getAxisTick` 用 `--muted-foreground` slate-500）；hover 黄色刺眼（LineChart/AreaChart/Scatter 的 Tooltip 缺 cursor，recharts 默认高亮带 + PALETTE 暖色）；整体风格缺统一 token；TopBar 计费需去除。

**方案**：① `index.css` 加 `--axis-tick`（slate-700）+ `--accent`（钢蓝=PALETTE[0]）；② `chartTheme` getAxisTick 用 axis-tick + fontSize 12 + 新 `getCursor(line)`（线/面/散点细虚竖线 / 柱/pareto 淡灰）；③ 6 widget Tooltip 统一 cursor + labelStyle/itemStyle；④ TopBar 删 cost span（store.cost 保留）；⑤ index.css 9 token 明暗双套 + tabular-nums；⑥ 关键组件迁移 `var(--token)`（TopBar/RunDetailPage active tab 用 accent）；⑦ index.html body 去 bg-slate-50 防暗模式 FOUC。

**reviewer 抓到 R1 真 bug**：`getCSSVar` 用 `hsl(${raw})` 包 RGB 三元组 → CSS Color 4 要求 hsl 百分比 → 非法 → SVG fill 静默退回黑色 →「坐标轴 slate-700」实际未达成。修 `hsl→rgb` + 3 回归测试钉死。

**验收**：`getAxisTick().fill="rgb(51 65 85)"`（合法 CSS）；6 widget cursor 统一；TopBar 无 top-cost；token 明暗双套；43 测试 + tsc 0 + build 0 error。

## 事故与恢复（2026-07-18）

P2（第一次）coder-agent 的 code-reviewer 子 agent 执行 `git stash`（想获干净测试环境），把并行的 P4 全部 tracked 改动（cli.py 等 7 文件 +1240 行）+ dirty 文件 stash 掉、working tree reset。**幸 stash 被 pop 恢复，零损失**（P4 已 commit `b77422f`）。教训已固化：[`parallel-coder-agents-no-git-mutation`](../../memory/parallel-coder-agents-no-git-mutation.md)（并行子 agent 禁 git stash/checkout/reset）。重派 P2 加 git 禁令后顺利完成。

## test-agent 真机验证

驱动真 store/组件 + e3b8ad tape（react-dom/server，项目无 headless 浏览器）：
- P1 LogStream **19 行**，agent_tool_call 真未渲染 ✓
- P2 `selectNodeSessions` **65 = 1 main + 64 sub**，SessionTabs **66 testid**，first sub 联动 ✓
- P3 aside `bg-slate-50` + 白卡片 + `NODE_STATUS_HEX #22C55E` 色条真渲染 + Setup/Loop/Finalize 分组 + `▸ 64 subs` 折叠 ✓
- P4 `orca doctor` sidechain_backend 字段齐全 + 34 单测 + grep 0 hit ✓
- `npm run build` exit 0

**verdict：P1-P4 全 ready，无 P0。** 2 pre-existing flake（agents-rail D2 lazy 1s timeout，SPEC 豁免）+ cac 真机无法验（mock 覆盖）。

## follow-up / debt

- **P4 cac 真机 spike**（用户侧）：真机 cac 跑一次确认是否注入 `CLAUDE_CODE_SESSION_ID`（P0-7）；若否用 fallback。
- **P2 AC#2 严格化**（可选）：selectConversation 完全读 nodesIndex（扩 NodeSessionIndex 加 sessionSeqs），当前 filter 性能可接受（buildEntries 缩量是大头）。
- **agents-rail D2 lazy flake**：WSL2 下 React.lazy chunk 加载 >1s timeout，提高 timeout 或改加载策略。
- **static/ 构建产物**：本 release 已更新（clone 即用）。

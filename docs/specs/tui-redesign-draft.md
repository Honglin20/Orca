# TUI Redesign Design Draft

> **状态**：design draft v1.1.1（v1.1 + 真用户验证 4 GAP 收口：A/B/C/E）
> **关联**：phase-12（CLI TUI 当前实现）/ phase-15（render layer v1，已完成）/ 后续 phase（待立项）
> **必读前置**：[`render-layer-design-draft.md`](./render-layer-design-draft.md)（tool 渲染层契约）、[`phase-12-cli-tui-redesign.md`](./phase-12-cli-tui-redesign.md)（当前 TUI 结构）、[`shells-design-draft.md`](./shells-design-draft.md)（三壳共同契约）
> **裁决原则**：满足 CLAUDE.md「单 tape 唯一真相源 + 幂等 reducer + 一条读路径」底线；不动 canonical Event schema、不动 phase-15 render layer 契约；只动 widget 渲染层 + DagLayout 策略。
>
> **v1.1 决策记录**（spec-review-adversarial 闭环）：
> - **O1 = c**：取消 NodeDetail + `f` 键过滤模式（默认看全事件流，按 `f` 切换"只看选中节点"）
> - **O2 = a**：fan-in `(N inputs)` N 静态（拓扑入边数）+ 进度副标 `(M/N arrived)` 动态
> - **O3 = b**：`after=None` 独立分支单独 section（主流下方）
> - Q1/Q3/Q7/Q9/Q11 修订详见 §4.4 / §6.4 / §4.3 / §11
>
> **v1.1.1 真用户验证 GAP 收口**（test-coverage-e2e 报告 → surgical fix）：
> - **GAP-A**（§4.4 `<tok>`）：app.py 现仅投 Header footer，DagGraph NodeProjection.tokens
>   永远 None。修复：agent_usage 分派处同步调 ``update_node_projection(node, tokens=...)``
>   （与 Header footer 同源同步）。
> - **GAP-B**（§5.4 `agent_tool_result` title）：canonical Event result.data 仅含
>   ``{tool_call_id, result}``（无 tool/args）；原 ``data.get("tool", "?")`` 显 ``? {}``。
>   修复：Activity Stream 内部维护 ``tool_call_id → (tool, args, call_ts)`` cache（call
>   时填，result 时反查），实现 spec §5.4「与 call 同 entry」语义。
> - **GAP-C**（§5.4 `agent_tool_result` meta）：canonical Event 无 exit_code/elapsed 字段。
>   修复：``elapsed`` 从 call.timestamp + result.timestamp 派生（顶层 Event 字段，spec §3）；
>   ``exit_code`` 因 canonical 不支持，spec §5.4 改为可选（缺失时不显 ``· exit N``）。
> - **GAP-E**（§4.4.1 loop workflow）：build_from_workflow 把 self-loop（counter → counter）
>   当环 fail loud，loop tape 重放 crash。修复：build_from_workflow 允许 self-loop（不抛
>   CycleDetected）；多节点环（A→B→A）仍 fail loud。自循环节点 iter N ≥ 2 即视觉信号。

---

## §0. 摘要（TL;DR）

phase-15 完成了 widget 级 render layer（tool cards / thinking / message 渲染干净），但 TUI 整体观感仍有 4 个用户可见痛点：DAG 节点信息密度太低、LogStream 截断太狠、噪音事件刷屏、错误显示简陋。本 draft 提出 **3 项改造**：

1. **DAG 节点升级 3 行盒子**（名字 / 状态+iter / 耗时+token）+ **fan-in 标注**（`(N inputs)` 文字代替 5 条乱线）+ **同层横向 + 层间纵向**的混合布局（既有 `LayeredDagLayout` 的渲染升级，**不动 layout 算法**）
2. **LogStream → Activity Stream**：双行 entry（summary 行 + 元信息行）+ **可折叠 detail 块**（按 Tab 展开，复用 phase-15 `render_tool`），不再 60 字符硬截
3. **噪音治理**：`prompt_rendered` 移出主流（降 debug log）；per-step `usage` 收敛到 Header footer（每节点一行而非每步一行）；`error` / `node_failed` 在 LogStream 主流 + DAG 节点框双重显示

**复杂 workflow 安全性**：用 AgentHarness `simple-nas`（9 节点，4 fan-out）+ `nas`（15 节点，5 fan-in + 6 节点线性链）做对照验证，方案 C（分层纵向 + 同层横向）永不横向溢出主轴；同层并行 > 4 自动切 outline fallback（既有 `CompactOutlineLayout`）。

**复杂度裁决**：~3-4 天，1 个 phase；不动 canonical Event schema、不动 render layer 契约；仅 widget 渲染层 + DagLayout 策略。

---

## §1. 背景 + 问题

### 1.1 触发本 draft 的真实场景

跑 mxint_analysis 真实 bitx 工作流后用户观察 TUI 渲染，发现 4 个具体问题（每条都有真跑 tape 证据）：

| # | 问题 | 证据 |
|---|---|---|
| P1 | **DAG 节点信息密度太低** | `dag_layout.py:420-447` `box_label()` 仅渲染 `f"{icon} {short}"` 一行；无 iter 号、无耗时、无失败原因 |
| P2 | **LogStream 截断太狠**（60 字符硬截） | `log_stream.py:55-75` `_truncate()` 默认 limit=60；真实 bitx stdout `→ <path>/Users/...` 只看到前 60 字符 |
| P3 | **prompt_rendered 噪音** | `log_stream.py:141-143` 把 system prompt 前导（`ask_user` 工具签名）当业务事件显示，每节点必发一条 |
| P4 | **per-step usage 行 spam** | deepseek-v4-flash 每步发 `agent_usage`；mxint 5 agent × 多步 = 17 条 usage 行混在 log 主流（`mxint_analysis-20260704-105608-90fd22.jsonl` 实测） |

### 1.2 用户提出的方向

- DAG "简化、居中排布、agent 名称 + 下方状态 + iter 数"
- LogStream 应该显示真业务内容（含错误），不是截断的一行字符串
- 担心横向 DAG 布局"未来还会出问题"，要求针对复杂 workflow 验证

### 1.3 不在本 draft 范围

- **canonical Event schema** 不动（phase-3 契约）
- **phase-15 render layer** 契约不动（`render_item.py` / `tool_render/*`）
- **translator / orchestrator / gate / chart** 链路不动
- Web UI 重写不在范围（待 Web 整体重写时再启）

---

## §2. 设计目标 + 非目标

### 2.1 目标

1. **DAG 信息密度 ↑**：单节点显示名字 + 状态 + iter 号 + 耗时 + 失败摘要，3 行盒子
2. **DAG 复杂 workflow 不崩**：分层纵向 + 同层横向 + fan-in 文字标注 + > 4 并行 fallback 到 outline
3. **LogStream 不丢信息**：双行 entry + 折叠详情（复用 phase-15 `render_tool`），不再 60 字符硬截
4. **噪音 ↓**：`prompt_rendered` 降 debug；`usage` 收敛到 Header footer；错误 inline 显示
5. **不动既有契约**：Event schema / render layer / translator / orchestrator 全部不动
6. **可增量**：DAG / LogStream / 噪音 3 块独立可分期交付

### 2.2 非目标（显式不做）

| 项 | 原因 |
|---|---|
| 改 canonical Event schema | 底线契约，不改（CLAUDE.md） |
| 改 phase-15 render layer 契约 | 已闭环（spec-review-adversarial 通过），本 draft 复用它 |
| 重写 DagLayout 算法 | 现有 `LayeredDagLayout` + `CompactOutlineLayout` 算法正确，本 draft 只升级渲染（节点盒子样式） |
| 复刻 claude-code / opencode / Conductor 像素 | 视觉接近即可，不追细节 |
| Web UI 同步改 | Web 待整体重写，本 draft 锁 TUI；Web 重写时借本 draft 思路（不强制契约一致） |
| 加动画 / 微交互 / 流式打字效果 | Textual 4.x 默认无 animation，自建成本不值（v2 评估） |

---

## §3. 当前 TUI 现状（重放 mxint 真跑事件抓的渲染）

### 3.1 当前布局

```
┌─ Header ──────────────────────────────────────────────────────────────────┐
│ mxint_analysis · run_id · ●running · 3/5 nodes · $0.0123                  │
├──────────────────┬────────────────────────────────────────────────────────┤
│ DagGraph (33%)   │ NodeDetail (右上, 3fr)                                  │
│ ┌──────────────┐ │ ┌─ analyzer ─────────────────────────────────────────┐ │
│ │ ○ analyzer    │ │ │ [tool] read(...)                                    │ │
│ │  │            │ │ │ [tool] glob(...)                                    │ │
│ │  ▼            │ │ │ [msg] ```json {...}                                 │ │
│ │ ○ configurator│ │ └────────────────────────────────────────────────────┘ │
│ │  │            │ ├────────────────────────────────────────────────────────┤
│ │  ▼            │ │ LogStream (右下, 2fr)                                 │
│ │ ✽ runner      │ │ 12:07  analyzer · node started (kind=agent)           │
│ │  │            │ │ 12:07  analyzer · prompt rendered: ...ask_user(...)   │
│ │  ▼            │ │ 12:07  analyzer · tool: glob({'pattern':'**/*.py'})   │
│ │ ○ diagnostic  │ │ 12:07  analyzer · → /Users/.../target_project/_adapt..│
│ │  │            │ │ 12:07  analyzer · usage: in=102 out=367 cache=13440.. │
│ │  ▼            │ │ 12:07  analyzer · tool: read({...filePath: '/Users/..│
│ │ ○ report      │ │ 12:07  analyzer · → <path>/Users/mozzie/Desktop/Proje│
│ └──────────────┘ │ ...                                                    │
└──────────────────┴────────────────────────────────────────────────────────┘
```

### 3.2 4 个用户可见问题

**P1 DAG 信息密度低**：`box_label()` 一行 `icon name`，无 iter 无耗时无失败原因。loop workflow（demo_loop / nas）一节点跑多次看不出。

**P2 LogStream 截断**：`→` 行硬截 60 字符，真实 file_read XML envelope / bitx stdout / 关键指标都看不到。用户必须切 NodeDetail 才能看完整。

**P3 prompt_rendered 噪音**：每节点必发一条 `prompt rendered: ...ask_user(...)`，是 orca 注入 system prompt 前导（`ask_user` 工具签名），对用户无意义，刷屏。

**P4 per-step usage spam**：deepseek-v4-flash 每步发 `agent_usage`，5 agent × 多步 = 17 条 usage 行混在 log 主流，盖过真业务事件。

**P5（衍生）**：`error` / `node_failed` 一行显示 `node FAILED: <message>`，stack trace 藏 NodeDetail；用户看 LogStream 时一眼看不到出错位置。

---

## §4. DAG 布局方案对比（含复杂 workflow 验证）

### 4.1 复杂 workflow 拓扑（验证基准）

**simple-nas**（9 节点，selector 4 fan-out diamond）：
```
setup → baseline → selector ─┬─→ mutator_structural ─┐
                              ├─→ mutator_hyperparam ─┤
                              ├─→ mutator_lr ─────────┤─→ analyzer → reporter
                              └─→ mutator_compute ────┘
```

**nas**（15 节点，scout 5 fan-in + 6 节点线性链 + refiner 旁支）：
- scout 的 5 个上游分散在 4 个不同 layer（adapter_gen / domain_analyzer / baseline_runner / tier_planner / metrics_identifier）
- scout → selector → planner → trainer → judger → analyzer → validator → reporter（6+ 节点深链）
- refiner `after=None`（独立分支）→ reporter 末端汇聚

### 4.2 三方案对比

#### 方案 A：纯横向布局 ❌（用户已否决）

15 节点 × 8 字符 = 120 字符，80 列终端必崩；fan-in 5 路到 scout 的边无法画；refiner 末端汇聚画不出。**用户担忧成立，不选**。

#### 方案 B：纯纵向 top-to-bottom（fallback 选项）

每个节点纵向堆叠，**任何宽度都能显示**，但 fan-out / fan-in 完全看不出（4 个并行 mutator 视觉上像顺序跑）。

#### 方案 C：**分层纵向 + 同层横向**（推荐 = Orca 现有 `LayeredDagLayout` 的渲染升级）

**核心规则**：
- **层与层之间纵向流转**（top-to-bottom 主轴，永不横向溢出）
- **仅当一层内有并行兄弟**才横向并排
- **fan-in ≥ 2** 时节点下方文字标注 `(N inputs)`，不画 N 条线
- **同层并行 > 4** 时该层切 outline（`CompactOutlineLayout` 既有 fallback）

**对 mxint（5 节点线性）渲染**：
```
┌──────────────────────┐
│      analyzer        │
│   ✓ done · iter 1    │
│   14s · 1.2k tok     │
└──────────────────────┘
            │
            ▼
┌──────────────────────┐
│    configurator      │
│   ✓ done · iter 1    │
│   20s · 1.8k tok     │
└──────────────────────┘
            │
            ▼
   (3 个盒子纵向堆)
```

**对 simple-nas（selector 4 fan-out）渲染**：
```
┌──────────────────────┐
│       setup          │
│   ✓ done · 3s        │
└──────────────────────┘
            │
            ▼
┌──────────────────────┐
│      baseline        │
│   ✓ done · 8s        │
└──────────────────────┘
            │
            ▼
┌──────────────────────┐
│      selector        │
│   ✓ done · 2s        │
└──────────────────────┘
            │
   ┌────────┼────────┬────────┐
   ▼        ▼        ▼        ▼
┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐
│mut_s │ │mut_h │ │mut_l │ │mut_c │   ← 同层并行（4 兄弟）
│✓ 14s │ │✓ 20s │ │✽ run │ │○ pend│
│iter1 │ │iter1 │ │iter1 │ │      │
└──────┘ └──────┘ └──────┘ └──────┘
   └────────┴────────┴────────┘
            │
            ▼ (merge)
┌──────────────────────┐
│      analyzer        │
│   ○ pending          │
└──────────────────────┘
```
4 个并行 × 10 字符 = 40 字符，永远在 60 字符以内。

**对 nas（15 节点最复杂）渲染**：
```
L1:  ┌──────────────────────┐
     │  project_analyzer    │
     │  ✓ done · 5s         │
     └──────────────────────┘
                │
                ▼
L2:  ┌──────────────┐  ┌──────────────┐
     │adapter_gen   │  │domain_analy  │   ← 同层 2 兄弟
     │✓ 8s          │  │✓ 6s          │
     └──────────────┘  └──────────────┘
                │
                ▼
L3:  ┌──────────────────────┐
     │  baseline_runner     │
     │  ✓ done · 12s        │
     └──────────────────────┘
                │
                ▼
L4:  ┌──────────────┐  ┌──────────────┐
     │tier_planner  │  │metrics_id    │
     │✓ 3s          │  │✓ 4s          │
     └──────────────┘  └──────────────┘
                │  ┌───────────┘
                ▼
L5:  ┌──────────────────────┐
     │   scout (5 inputs)   │   ← fan-in 用文字 "(5 inputs)" 标
     │   ✽ running · 30s    │
     └──────────────────────┘
                │
                ▼
L6-L12:  线性链 6 个盒子纵向堆（selector → planner → trainer → judger → analyzer → validator）

旁支:  ┌──────────────────────┐
       │      refiner         │   ← after=None，独立分支
       │  ○ pending           │
       └──────────────────────┘
                  │
                  └──────▶ reporter (末端汇聚，文字标注)
```

### 4.3 方案 C 稳健性证明

| 风险场景 | 方案 C 应对 |
|---|---|
| fan-out 太宽（5+ 并行） | **列宽计算**：3 行盒子最小宽 12 字符（8 name + 2 padding + 2 border）；4 并行 + 3 间距 = 51 字符（fits 60 列）；5 并行 + 4 间距 = 64 字符（超 60 列临界）；6 并行 + 5 间距 = 77 字符（80 列临界）。**fallback 阈值：≥ 5 同层并行切 outline**（既有 `CompactOutlineLayout`） |
| 节点数太多（20+） | 纵向滚屏（文本 TUI 友好），每节点 3 行盒子信息密度高，不强求一屏看完 |
| fan-in 多路（5+ 入边） | 用文字 `(N inputs)` 标注（详见 §4.5），不画 N 条线 |
| `after=None` 独立分支 | 单独 section（详见 §4.6） |
| 屏幕窄（< 60 列） | 整体切 `CompactOutlineLayout` fallback（既有） |

**核心不变量**：纵向流转 = 主轴永远 top-to-bottom；横向只在"同层兄弟"局部使用且宽度受限。**主轴永不横向溢出**。

### 4.4 单节点盒子渲染契约（v1.1：字段级定义 + acceptance）

每节点固定 3 行（窄屏 fallback 时 1 行）：

```
┌──────────────────────┐
│      <name>          │  ← 行 1：节点名（居中，bold）
│   <status> · iter N  │  ← 行 2：状态 + iter 号
│   <elapsed> · <tok>  │  ← 行 3：耗时 + token 数（失败时显错误摘要）
└──────────────────────┘
```

**字段定义 + 来源 Event + acceptance criteria**：

| 字段 | 来源（Event 派生 fold） | 派生规则 | acceptance criteria |
|---|---|---|---|
| `<name>` | `Workflow.nodes[i].name` | 静态 | 渲染前已确定；超过盒子宽度 14 字符则 truncate + `…`（如 `mutator_str…`） |
| `<status>` | `node_started` / `node_completed` / `node_failed` / `human_decision_requested` / `interrupt_requested` | reducer 维护 `node_status: dict[node, str]`（`pending/running/done/failed/blocked`） | 同 node 同 session_id 内 idempotent；中断 + retry 不污染（详见 §4.4.1） |
| `iter N` | `node_started` 事件 | reducer 维护 `node_iter: dict[node, int]`，每次新 `session_id` 触发 +1（详见 §4.4.1） | 同 tape 重放必产相同 iter（fold 性质） |
| `<elapsed>` | `node_started.timestamp` + `node_completed.elapsed`（或 running 时的 `now - started.timestamp`） | completed 后静态；running 时 live timer（每秒 tick） | live timer 走 wall clock，**不进 tape**（UI 交互态） |
| `<tok>` | `agent_usage` 事件（同 session_id） | **取该 session_id 最后一条 `agent_usage.data.input_tokens + output_tokens`** | 依赖 opencode translator per-step 是累积值（`opencode.py:21-23` 已声明）；claude translator 走 `result_line` 模式只发一次，相同语义 |
| `<error_msg>` | `node_failed.data.message` 或 `error.data.message` | 取前 30 字符（`msg[:30]`），失败时替代 `<elapsed> · <tok>` 行 | 完整 message 在 Activity Stream 折叠块（§5） |

**状态色**：
- pending: 灰
- running: 蓝（live timer 更新）
- done: 绿
- failed: 红（+ 错误摘要）
- blocked: 黄

#### 4.4.1 iter 号派生规则（v1.1，闭环 Q1 + Q11）

**iter 号是 reducer 派生 fold**（重放必产相同值），**不是 UI 交互态**。两者严格区分：

| 类别 | 例子 | 重放是否重建 |
|---|---|---|
| **派生 fold**（reducer 维护，从 Event 投影） | `node_iter` / `node_status` / `node_elapsed_static` / `node_tokens` | ✅ 重建（idempotent） |
| **UI 交互态**（widget 内部，不持久化） | `_selected_node` / `live_timer_running_offset` / 折叠块展开状态 | ❌ 重放清零（重启即默认） |

**iter 号计数规则**：
- reducer 维护 `node_session_ids: dict[node, list[session_id]]`（按 `node_started` 顺序 append session_id）
- `iter N` = 该 node 的 session_id 在 list 中的位置 + 1
- **retry 不算新 iter**：retry 是同 `session_id` 的延续（`retry_started` 不触发新 `node_started`）
- **skip 不算新 iter**：`node_skipped` 不 append session_id
- **interrupt + resume 不算新 iter**：interrupt 后用户 continue 走同 session_id；用户 skip 走 `node_skipped`；用户 abort 走 `node_failed`
- **loop workflow 重入算新 iter**：每次 `node_started` 携带新 `session_id` → append → iter +1

**acceptance**：
- 重放 mxint tape（5 节点，无 loop）→ 5 节点全 `iter 1`
- 重放 demo_loop tape（loop 多次同节点）→ 该节点 `iter N` 与 `node_started` 次数一致
- 重放 with_retry tape（节点失败 + retry 成功）→ 该节点 `iter 1`（retry 不增量）

### 4.5 fan-in `(N inputs)` 标注（v1.1，O2=a 决策）

**N 的语义**（review O2 闭环）：
- **N = 静态拓扑入边数**（不变，从 `Workflow.nodes[*].routes` + parallel 组计算）
- **进度副标 `(M/N arrived)` 动态**：M = reducer 维护的"前置节点已完成数"（从 `node_completed` 事件累加）

**渲染位置**：盒子下方居中（与盒子等宽，不破坏对齐），格式 `<node_name> (N inputs · M/N arrived)`：

```
┌──────────────────────┐
│        scout         │
│   ✽ running · iter 1 │
│      30s · 24k tok   │
└──────────────────────┘
       (5 inputs · 3/5 arrived)
```

**acceptance**：
- 拓扑计算 N：`scout` 在 `nas` 中有 5 个 `after=[...]` 引用 → N=5
- M 实时增量：3 个前置 `node_completed` 后 → `(5 inputs · 3/5 arrived)`
- 5 个全到齐 + `scout` 进入 `running` → 副标消失（不再显示 arrived）
- N=1（线性）不显示副标

### 4.6 `after=None` 独立分支（v1.1，O3=b 决策）

`after=None` 节点（如 nas 的 `refiner`）单独 section，**不混入主流分层**：

```
─── 主流（project_analyzer → ... → reporter） ───

旁支:
┌──────────────────────┐
│      refiner         │
│   ✓ done · iter 1    │
│   45s · 5.2k tok     │
└──────────────────────┘
        │
        └────▶ reporter (末端汇聚，文字标注)
```

**渲染规则**：
- 主流末端节点（如 `reporter`）的盒子下方加 `◀ (receives: refiner)` 文字标注
- 旁支节点用相同 3 行盒子渲染
- 旁支 section 标题 `─── 旁支（after=None） ───`（中文标签）
- 多个 `after=None` 节点纵向堆叠在旁支 section

---

## §5. LogStream → Activity Stream 重构（v1.1：取消 NodeDetail + 加 `f` 过滤模式）

### 5.1 决策记录（v1.1）

**O1 = c**：取消 NodeDetail widget，**信息合并进 Activity Stream 折叠块**；同时加 **`f` 键过滤模式**：

- 默认：Activity Stream 显示**全事件流**（所有节点所有事件，按 seq 排序）
- 按 `f`：切换到**只看选中节点**模式（filter `event.node == _selected_node`）
- 再按 `f`：切回全事件流（toggle）
- 当前模式由 Header 显示：`[全部事件]` 或 `[仅 analyzer]`（filter on）

**取消 NodeDetail 的理由**：
- NodeDetail 与 Activity Stream 折叠块内容来源相同（都是 `render_tool(item)`），DRY 违规
- 双 widget 同源数据，状态漂移风险
- `f` 过滤模式保留"按节点导航"能力——用户想看 analyzer 全流程时，`f` 切到 analyzer filter 即可

### 5.2 当前 LogStream 渲染（format_event 一行制，将被替代）

```python
# log_stream.py:55-75（现状）
def _truncate(s, limit=60): ...

def _describe(event_type, data):
    if event_type == "agent_message":
        return _truncate(data.get("text", ""))
    if event_type == "agent_tool_call":
        return f"tool: {tool}({_truncate(args)})"
    if event_type == "agent_tool_result":
        return f"→ {_truncate(result)}"
    ...
```

每个事件一行 60 字符硬截，工具结果的真实内容（file_read XML / bitx stdout / 错误 stack）全丢。

### 5.3 Activity Stream 双行 entry + 折叠详情（参考 Conductor）

**新结构**（每个事件 = 一个 entry，2 行 + 折叠详情）：
```
12:07  analyzer  ▶ read   /Users/.../target_project/models/model.py
                    68 lines · ✓ · ▸ Tab 展开
12:07  analyzer  ▶ bash   python -c "import torch; ck=torch.load(...)"
                    exit 0 · 0.8s · ▸ Tab 展开
12:07  analyzer  ▶ message  ```json {"model_class": "ConfigurableMLP", ...
                    10 lines markdown · ▸ Tab 展开
12:07  runner    ! bash failed  python tests/.../run_analysis.py
                    ! RuntimeError: ... (full message, 不截断)
                    ▸ Tab 展开 (stack trace)
```

### 5.4 per-type entry 结构表（v1.1：字段级定义，闭环 review Q5）

| event_type | type_icon | title source | meta source | detail source（折叠块） |
|---|---|---|---|---|
| `agent_message` | `💬` | `data.text[:50].replace("\n"," ")` | `<N> lines markdown` | `render_message(full text)`（phase-15） |
| `agent_thinking` | `🤔` | `data.text[:50].replace("\n"," ")` | `<N> lines (dim)` | `render_thinking(full text)`（phase-15） |
| `agent_tool_call` | `▶` | `data.tool` + " " + `_arg_title(data.tool, data.args)` | `running...` | `render_tool(item, status="running")`（phase-15） |
| `agent_tool_result` | `✓` | （与 call 同 entry，meta 升级） | `<N> lines · <elapsed>s`（v1.1.1 GAP-C：``exit <code>`` 可选，canonical Event 不支持 exit_code） | `render_tool(item, status="completed")`（phase-15） |
| `node_started` | `▶` | `node started (kind=<kind>)` | — | (no detail) |
| `node_completed` | `✓` | `node completed (<elapsed>s)` | — | (no detail) |
| `node_failed` | `!` | `node FAILED: <data.message>` | `phase=<data.phase>` | full `data.message` + stack（若有） |
| `node_skipped` | `⏭` | `node skipped: <data.reason>` | — | (no detail) |
| `error` | `!` | `error: <data.message>` | `phase=<data.phase>` | full `data.message` |
| `route_taken` | `→` | `route: <data.from> → <data.to>` | — | (no detail) |
| `foreach_started` | `▶` | `foreach: <item_count> items · max_concurrent=<mc>` | — | (no detail) |
| `foreach_item_started` | `▸` | `item #<index>: <item_key>` | — | (no detail) |
| `foreach_item_completed` | `✓` | `item #<index> completed` | — | (no detail) |
| `foreach_completed` | `✓` | `foreach: <count>/<succeeded>` | — | (no detail) |
| `human_decision_requested` | `⏸` | `gate: <data.prompt[:50]>` | `gate_id=<id>` | full prompt + options |
| `human_decision_resolved` | `✓` | `gate resolved: <data.answer>` | — | (no detail) |
| `interrupt_requested` | `⏸` | `interrupt requested at <node>` | `elapsed=<sec>` | full guidance（若有） |
| `interrupt_resolved` | `✓` | `interrupt: <action>` | `by=<resolved_by>` | full guidance |
| `custom` | `📊`/`📈` | `custom(<data.kind>)` per data.kind | — | per data.kind（chart/table/image） |
| `retry_started` | `↻` | `retry #<attempt>/<max>` | `delay=<sec>s` | full error message |
| `retry_succeeded` | `✓` | `retry succeeded (after <total> attempts)` | — | (no detail) |
| `retry_exhausted` | `!` | `retry exhausted (<attempts>)` | `last_error=<type>` | full error |
| `wait_started` | `⏱` | `wait <duration>s` | `reason=<...>` | (no detail) |
| `wait_completed` | `✓` | `wait completed (<elapsed>s)` | `interrupted=<bool>` | (no detail) |
| `validator_started` | `🔍` | `validator: <criteria_preview>` | — | full criteria |
| `validator_passed` | `✓` | `validator passed` | — | (no detail) |
| `validator_failed` | `!` | `validator failed: <issues[0]>` | `retrying=<bool>` | full issues list |
| `dialog_started` | `💬` | `dialog started` | — | (no detail) |
| `dialog_message` | `💬` | `<role>: <text[:50]>` | `turn=<N>` | full text |
| `dialog_ended` | `✓` | `dialog ended (<turns> turns)` | — | conclusion |
| `workflow_started` | `🚀` | `workflow started: <name>` | `node_count=<N>` | (no detail) |
| `workflow_completed` | `✓` | `workflow completed (<elapsed>s)` | — | outputs preview（前 200 字符） |
| `workflow_failed` | `!` | `workflow FAILED: <error_type>` | `node=<failed_node>` | full message + stack |
| `workflow_cancelled` | `⏹` | `workflow cancelled: <reason>` | — | (no detail) |
| `workflow_resumed` | `↻` | `workflow resumed from <node>` | `replayed=<N>` | (no detail) |
| `prompt_rendered` | （§6.1 隐藏，不显示） | — | — | — |
| `agent_usage` | （§6.2 隐藏到 Header footer） | — | — | — |

**`_arg_title(tool, args)` 规则**（每个工具一句话标题）：
- `read`/`Read`：`args.filePath or args.file_path or args.path`（取其一）
- `bash`/`Bash`：`args.command[:50]`
- `glob`/`Glob`：`args.pattern`
- `grep`/`Grep`：`args.pattern`
- `write`/`Write`：`args.filePath or args.path` + ` (new)`
- `edit`/`Edit`：`args.filePath or args.path`
- 其他：`json.dumps(args)[:40]`

### 5.5 折叠详情契约

折叠块内容 = `render_tool(normalize_tool(...))` / `render_message(...)` / `render_thinking(...)` 输出（phase-15 完整渲染）。展开/收起用 Textual 的 `Collapsible` widget（v0.86+ 支持，已验证 textual 8.2.8 可用）。

- **默认收起**（避免 TUI 信息过载）
- **当前选中 entry 自动展开**（用 `j/k` 切换选中时自动展开新的、收起旧的）
- **Tab / Enter 切换展开/收起**（选中状态下）
- **展开 = 调 phase-15 渲染函数**，完整 file_read / file_write / shell / diff / markdown
- **大输出处理**：折叠块超 200 行时，内部 `VerticalScroll`（不全展，避免卡顿）

---

## §6. 噪音治理

### 6.1 prompt_rendered 降级

**当前**：`log_stream.py:141-143` 把 `prompt_rendered` 当业务事件显示（`prompt rendered: ...ask_user(...)`）。

**改造**：
- LogStream / Activity Stream 不再显示 `prompt_rendered`（filter 掉）
- 该事件仍写 tape（不丢真相），仅在 widget 层隐藏
- Header 可选显示 `prompt: ✓ rendered`（一行简标，不显内容）

**理由**：`prompt_rendered` 是框架调试事件（看 system prompt 注入是否对），不是业务事件，对用户无价值。

### 6.2 per-step usage 收敛

**当前**：每个 `agent_usage` 事件在 LogStream 显示一行 `usage: in=X out=Y cache=Z cost=$W`。deepseek-v4-flash 每步发一条 → 5 agent × 多步 = 17 条 usage 行混主流。

**改造**：
- LogStream / Activity Stream 不再显示 `agent_usage`（filter 掉）
- 收敛到 **Header footer 区**：每节点一行 `<node> <in+out tok> <$cost>`
  - 例：`analyzer 1.2k tok · $0.0004  configurator 1.8k tok · $0.0006  runner 24k tok · $0.0034`
- 节点多时 Header footer 可横向滚动或循环显示（Textual `HorizontalScroll`）

**理由**：usage 是付费/性能指标，看汇总即可；混在业务 log 主流盖过真业务事件。

### 6.3 错误显示增强

**当前**：`error` / `node_failed` / `workflow_failed` 一行 `node FAILED: <message>`，stack trace 藏 NodeDetail。

**改造**：
- LogStream 主流显示 `! <event_type> failed: <full message>`（**不截断**，红色）
- 同时 DAG 节点框第 3 行变红显示 `! <error[:30]>`
- 折叠详情含完整 stack trace / API error response

### 6.4 过滤规则集中（DRY，v1.1：32 EventType 全覆盖 + 完整性测试）

新增 `orca/iface/cli/widgets/_event_filter.py`：

```python
EVENT_VISIBILITY: dict[str, str] = {
    # ── workflow 生命周期 ──
    "workflow_started":      "show_compact",
    "workflow_completed":    "show_compact",
    "workflow_failed":       "show_error",
    "workflow_cancelled":    "show_compact",
    "workflow_resumed":      "show_compact",
    # ── node 生命周期 ──
    "node_started":          "show_compact",
    "node_completed":        "show_compact",
    "node_failed":           "show_error",
    "node_skipped":          "show_compact",
    # ── agent 流式 ──
    "agent_message":         "show",
    "agent_thinking":        "show_dim",
    "agent_tool_call":       "show",
    "agent_tool_result":     "show",
    "agent_usage":           "hide_main",      # §6.2 收敛到 Header footer
    # ── 路由 ──
    "route_taken":           "show_compact",
    # ── 并发 ──
    "foreach_started":       "show_compact",
    "foreach_item_started":  "show_compact",
    "foreach_item_completed":"show_compact",
    "foreach_completed":     "show_compact",
    # ── HMIL gates ──
    "human_decision_requested": "show_warn",
    "human_decision_resolved":  "show_compact",
    # ── 中断 ──
    "interrupt_requested":   "show_warn",
    "interrupt_resolved":    "show_compact",
    # ── prompt 调试 ──
    "prompt_rendered":       "hide_all",       # §6.1 隐藏（仅 tape）
    # ── Retry Policy ──
    "retry_started":         "show_warn",
    "retry_succeeded":       "show_compact",
    "retry_exhausted":       "show_error",
    # ── Wait Node ──
    "wait_started":          "show_compact",
    "wait_completed":        "show_compact",
    # ── Validator ──
    "validator_started":     "show_compact",
    "validator_passed":      "show_compact",
    "validator_failed":      "show_error",
    # ── Dialog ──
    "dialog_started":        "show_compact",
    "dialog_message":        "show",
    "dialog_ended":          "show_compact",
    # ── 自定义（chart 等）──
    "custom":                "show",           # 按 data.kind 分发渲染
    # ── 错误 ──
    "error":                 "show_error",
}
```

**5 个 visibility tag**（消费者按 tag 分派，避免重复 if-else）：
- `show`：Activity Stream 主流双行 entry
- `show_dim`：Activity Stream 主流 dim 行（thinking）
- `show_compact`：Activity Stream 主流单行（node_started / route / usage 等次要事件）
- `show_warn` / `show_error`：主流 + DAG 节点框双重显示（warn=黄、error=红）
- `hide_main`：不进 Activity Stream，但写到 Header footer（usage）
- `hide_all`：完全不显示（仅 tape，prompt_rendered）

**acceptance criteria**（Q7 闭环）：
- **完整性测试**：`test_event_visibility_completeness`：用 `orca.schema.event.EventType` 的 `__args__`（Literal 全集）逐一断言 `EVENT_VISIBILITY` 覆盖——少一个 EventType 立即 fail
- **新增 EventType 守卫**：CI 跑完整性测试，schema 加新 type 但忘加 visibility 时自动 fail（fail loud）

---

## §7. 整体布局调整

### 7.1 现状

```
Header
┌──────────────┬──────────────────┐
│              │  NodeDetail       │
│  DagGraph    │  (3fr)            │
│  (33%)       ├──────────────────┤
│              │  LogStream        │
│              │  (2fr)            │
└──────────────┴──────────────────┘
Footer
```

### 7.2 改造后（v1.1：取消 NodeDetail + Activity Stream 全高）

```
Header  (含 footer 区：每节点 token/cost 横向滚动 + 当前 filter 模式)
┌──────────────────────┬───────────────────────────────┐
│                      │  Activity Stream              │
│  DagGraph            │  (双行 entry + 折叠详情)      │
│  (50%)               │  (右半，全高)                 │
│  (3 行盒子节点)      │                               │
│                      │  按 f 切换：[全部] / [仅选中] │
│                      │                               │
└──────────────────────┴───────────────────────────────┘
Footer  (keybindings: j/k 选中 · f 切换 filter · Tab 展开 · q 退出 · t 切 thinking)
```

**关键变化（v1.1，O1=c 决策）**：
1. DagGraph 占宽 33% → **50%**（横向布局更舒展，3 行盒子节点）
2. **取消 NodeDetail**（信息合并进 Activity Stream 折叠块 + `f` 键过滤模式）
3. LogStream 改 Activity Stream（双行 + 折叠），占右半**全高**（不再 2fr/3fr 分割）
4. Header 加 footer 区显示 per-node token/cost（横向滚动）
5. Footer 加 `f` 键说明

---

## §8. 模块布局（依赖方向）

### 8.1 改动文件清单

**新增**：
- `orca/iface/cli/widgets/_event_filter.py` —— `EVENT_VISIBILITY` 集中表
- `orca/iface/cli/widgets/activity_stream.py` —— 新 widget（替代或改造 `log_stream.py`）

**修改**：
- `orca/iface/cli/widgets/dag_layout.py` —— `_render_layered_lines()` / `box_label()` 升级为 3 行盒子渲染；新增 `_fanin_annotation()` / `_iter_label()`
- `orca/iface/cli/widgets/dag_graph.py` —— `set_status` 扩展为 `set_status(name, status, *, iter_n, elapsed, tokens, error_msg)`（多字段投影）
- `orca/iface/cli/widgets/log_stream.py` —— 改造为 Activity Stream 双行 entry（或废弃，新建 `activity_stream.py`）
- `orca/iface/cli/app.py` —— `_dispatch_to_widgets` 改为按 `EVENT_VISIBILITY` 表分派；Header 加 per-node token/cost 投影
- `orca/iface/cli/widgets/header.py` —— 加 footer 区（per-node usage）
- `orca/iface/cli/widgets/node_detail.py` —— 取消（推荐）或保留（备选）

**不动**：
- `orca/schema/event.py` —— canonical Event 不动
- `orca/iface/cli/widgets/tool_render/*` —— phase-15 render layer 不动（Activity Stream 折叠块复用它）

### 8.2 依赖约束

- 新增模块只依赖 `orca.schema` + `textual` + `rich` + stdlib
- 不反向依赖 `orca.exec` / `orca.run` / `orca.events.bus`（与现有 widget 铁律一致）
- Activity Stream 折叠块调 `tool_render.render_tool(...)`（phase-15 既有 API，无新依赖）

---

## §9. 测试策略

### 9.1 测试金字塔

| 层 | 测试方式 |
|---|---|
| `EVENT_VISIBILITY` 表 | 单测：每 event_type 有唯一 visibility tag，无遗漏 |
| Activity Stream 双行 entry | snapshot 测试：注入 fixture event → 检查双行结构（summary + meta） |
| 折叠详情 | snapshot 测试：展开状态下输出 == phase-15 `render_tool(item)` 输出 |
| DAG 3 行盒子渲染 | snapshot 测试：3 种 workflow（mxint 线性 / simple-nas diamond / nas 复杂）的 DAG 渲染输出 |
| fan-in 标注 | 单测：注入 fan-in ≥ 2 的拓扑 → 渲染含 `(N inputs)` 文字 |
| 同层并行 fallback | 单测：注入同层 5 节点 → 该层切 outline 模式（不横向溢出） |
| 复杂 workflow 不溢出 | snapshot 测试：nas 15 节点的 DAG 渲染总宽度 < terminal width |

### 9.2 fixture（沿用 phase-15 模式）

- `tests/e2e_phaseNN/_artifacts/tui_redesign_dag_fixtures.json`：3 种拓扑（linear / diamond / complex）的 workflow + 期望 DAG 渲染
- 复用 `tests/e2e_phase15/_artifacts/render_tool_cases.json`（phase-15 既有，Activity Stream 折叠块直接消费）

### 9.3 真 TUI 验证

- 用 textual pilot 重放真 tape（`mxint_analysis-*.jsonl`）→ 截 SVG screenshot
- 对比改造前后视觉效果（release note 附 before/after SVG）

---

## §10. 分期

### 10.1 v1（本 draft 范围）

- ✅ DAG 3 行盒子渲染（名字 / 状态+iter / 耗时+tok 或 错误）
- ✅ DAG fan-in `(N inputs)` 文字标注
- ✅ DAG 同层横向 + 层间纵向 + > 4 并行 outline fallback（既有 `CompactOutlineLayout`）
- ✅ Activity Stream 双行 entry + 折叠详情（复用 phase-15）
- ✅ `EVENT_VISIBILITY` 表 + 噪音治理（prompt_rendered / per-step usage）
- ✅ Header footer per-node token/cost
- ✅ 复杂 workflow 不溢出验证（simple-nas + nas）

**预估工作量**：~3-4 天，1 phase（DAG 1.5d + Activity Stream 1.5d + 噪音治理 0.5d + 测试 0.5d）

### 10.2 v2（未来，按需）

- DAG 节点 hover tooltip（显示完整 error message）
- Activity Stream 流式语法高亮（agent_message 流式 markdown，参考 opencode PacedMarkdown）
- 错误堆栈的折叠树展开（多级折叠）
- 键盘导航增强（`j/k` 切换 entry，`g/G` 跳首/尾，`/` 搜索）

### 10.3 显式不做（v1 范围外）

- 动画 / 过渡效果（Textual 4.x 默认无 animation）
- 流式 markdown shiki 增量高亮（已在 render layer v2 路线）
- Web UI 同步改造（Web 整体重写时再考虑）

---

## §11. 风险 + 待校准（v1.1：O1-O3 已决，剩 4 项技术风险）

| 项 | 风险 | 缓解 |
|---|---|---|
| ~~取消 NodeDetail 影响用户习惯~~ | ~~双 widget vs 单 widget~~ | **已决（O1=c）**：取消 NodeDetail + `f` 键过滤模式保留按节点导航 |
| ~~fan-in N 语义~~ | ~~静态 vs 动态~~ | **已决（O2=a）**：N 静态（拓扑）+ 副标 `(M/N arrived)` 动态 |
| ~~`after=None` 旁支布局~~ | ~~位置未定~~ | **已决（O3=b）**：单独 section（§4.6） |
| Activity Stream 折叠块性能 | 大 workflow（100+ events）展开/折叠重渲可能卡 | 折叠块**懒渲染**（仅当前选中 entry 展开）+ 内部 `VerticalScroll` cap 200 行 |
| DAG 3 行盒子在小屏（< 30 列）溢出 | 窄屏盒子挤成一行 | `CompactOutlineLayout` fallback 全切 outline 模式（既有） |
| Header footer 区在节点多时（10+）显示 | 横向滚动还是循环 | **横向滚动**（Textual `HorizontalScroll`）；优先显示当前 running 节点（自动滚到视野）；workflow 末期无 running 时，自动滚到最后完成的节点 |
| 复杂 workflow（如 refiner `after=None`） | 旁支节点位置 | §4.6：单独 section（主流下方），文字标注汇聚点 |
| iter 号派生真相 | reducer fold 性质（review Q11） | §4.4.1：iter 是 fold（重放必等价），与 `_selected_node` UI 交互态严格区分 |
| EVENT_VISIBILITY 完整性 | 漏 EventType 导致事件消失（review Q7） | §6.4：完整性测试 `test_event_visibility_completeness`（用 `EventType.__args__` 断言全覆盖） |

---

## §12. 显式裁决记录（防漂移）

| # | 议题 | 裁决 | 理由 |
|---|---|---|---|
| 12.1 | DAG 布局选 A/B/C 哪个 | **C（分层纵向 + 同层横向 + > 4 fallback）** | A 在 nas 必崩；B 失去并行视觉；C 主轴永远纵向，横向局部受限，既有算法不动 |
| 12.2 | DAG 节点信息密度 | **3 行盒子（name / status+iter / elapsed+tok 或 error）** | 当前 1 行密度太低；3 行容纳 loop iter + 耗时 + 错误摘要 |
| 12.3 | LogStream 改 Activity Stream | **是**（双行 + 折叠详情） | 60 字符硬截丢真实业务内容；Conductor 的 Activity Stream 模式更适合 |
| 12.4 | NodeDetail 是否取消 | **推荐取消**（合并进 Activity Stream 折叠） | 双 widget 同源数据 DRY 违规；备选保留待 review |
| 12.5 | `prompt_rendered` 处理 | **降级（不显示，仍写 tape）** | 是框架调试事件，对用户无价值；写 tape 不丢真相 |
| 12.6 | per-step `agent_usage` 处理 | **收敛到 Header footer** | 混主流盖过业务事件；汇总即可 |
| 12.7 | 错误显示 | **LogStream + DAG 框双重显示，不截断** | 用户能立刻看到出错位置和完整 message |
| 12.8 | 动 canonical Event schema | **不动** | 底线契约（CLAUDE.md） |
| 12.9 | 动 phase-15 render layer 契约 | **不动**（Activity Stream 折叠块复用） | 已闭环（spec-review-adversarial 通过） |
| 12.10 | 复杂 workflow fallback | **`CompactOutlineLayout` 既有，复用** | 不重写 layout 算法，仅升级渲染 |

---

## §13. 开放问题（v1.1：原 8 问中 6 问闭环，剩 2 问待实施期决定）

| # | 原问题 | v1.1 状态 |
|---|---|---|
| 1 | NodeDetail 是否取消 | **已决（O1=c）**：取消 + `f` 键过滤模式 |
| 2 | Activity Stream 折叠默认状态 | **已决**：默认全部收起 + 当前选中 entry 自动展开 |
| 3 | iter 号显示位置 | **已决**：行 2 内（`<status> · iter N`），3 行盒子 |
| 4 | fan-in `(N inputs)` 阈值 | **已决**：N ≥ 2 显示（线性 N=1 不显示） |
| 5 | Header footer 节点多时 | **已决**：横向滚动 + 优先显示 running |
| 6 | `after=None` 独立分支位置 | **已决（O3=b）**：单独 section（§4.6） |
| 7 | DAG 节点 hover tooltip | **v2 评估**（v1 不做，Textual Static 不原生支持 hover，自建成本不值） |
| 8 | Activity Stream entry max height | **已决**：折叠块超 200 行用 `VerticalScroll`（内部滚动，不全展） |

---

**本 draft v1.1 状态**：spec-review-adversarial conditional-pass → 5 P0 全闭环（3 用户决策 + 5 SPEC 修订已落实）→ 可开 phase 实施。

**下一步**：clean-code-builder 实施 v1（~3-4 天）→ test-coverage-e2e 实测 workflow + TUI 渲染对照 spec。

# TUI Redesign v2 Release Note

> **日期**：2026-07-07
> **范围**：TUI 三块布局重写（取消 DAG + agent 输出可见 + 切换 agent 看历史）
> **前置 commit**：v1.1.1 `225933e`
> **本 release commits**：
>   - `59021c9` Step 1a：删 dag_* + 空壳占位
>   - `5f9988c` Step 1b：迁 activity_stream 函数 + 删文件
>   - `e252653` Step 2：AgentsList widget
>   - `ab3b254` Step 3：AgentHistory widget
>   - `0e9e877` Step 4：LogStream + _event_filter 改造
>   - `77f5685` Step 5：app.py 三块布局连通
>   - `85ecb61` Step 6：e2e fixture + v1.1.1 引用闭环
> **SPEC**：[`docs/specs/tui-redesign-v2-design-draft.md`](../specs/tui-redesign-v2-design-draft.md)
> **plan**：`~/.claude/plans/twinkling-cuddling-seal.md`
> **review**：spec-review-adversarial CONDITIONAL-PASS（6 P0 + 7 P1 全闭环）

---

## 背景：v1.1.1 上线后用户反馈

v1.1.1 双栏布局（DagGraph + ActivityStream）解决的是「复杂拓扑 fan-out/fan-in 怎么画」——但用户实际工作流（mxint 线性 / 5 agent）用不上 DAG，反而 agent 输出（用户最关心的东西）混在事件流里看不到。

v2 三块改造：
1. **左 30% AgentsList**（拓扑序纵向列表，j/k 切换）
2. **右上 70% AgentHistory**（单 agent 视图，Conductor Activity 风格 + last message 默认展开）
3. **右下 30% LogStream**（高层节点事件，Conductor Log View 风格 + 5 level icon）

## 三块布局真跑验证（mxint tape 重放）

```
AgentsList 5 agent 拓扑序：analyzer / configurator / runner / diagnostic_saver / report_painter
所有节点 ✓ done
AgentHistory auto-follow report_painter：80 entries + last message seq 182 默认展开
LogStream 12 行（node_started/completed 链）
```

## 用户核心需求闭环

| # | 用户原话 | v2 实现 |
|---|---|---|
| 1 | "看到每个 agent 的输出" | AgentHistory last message 默认展开（用户核心需求） |
| 2 | "切换 agent 查看历史记录" | j/k 切换 + _node_events 分桶（纯前端切换，不读 tape） |
| 3 | "TUI 没有 DAG 图也行" | 删 DagGraph widget（含 dag_layout / _dag_render） |
| 4 | "LogStream 报重要节点事件 + 失败原因" | 5 level icon + node_failed 不截断 + L 键 debug toggle |

## 接口统一性约束（spec §11）

CLAUDE.md 加铁律 8「接口统一性」+ CURRENT.md 加前置铁律「接口整理前置」。

实施期间识别的接口风险登记给后续 phase：
- **风险 A**（错误接口 5 套并存）：phase-11-error-handling 落地前需 ADR 明确三层映射
- **风险 B**（能力接口 2 套替换）：phase-12-capabilities 落地前需替换清单
- **风险 C**（fold DRY）：v2 follow-up 抽 `orca/run/projections.py`

## 删除清单（v1.1.1 真清理）

| 模块 | 状态 |
|---|---|
| `dag_graph.py` | 删 |
| `dag_layout.py` | 删 |
| `_dag_render.py` | 删 |
| v1.1.1 `activity_stream.py` | 删（6 函数迁入 `_event_summary.py`） |
| `tests/iface/cli/test_dag_layout.py` | 删 |
| `tests/iface/cli/test_tui_redesign.py`（v1.1.1） | 删 |
| v1.1.1 display:none 双写兼容路径 | 删（_dispatch_to_widgets LogStream / NodeDetail 双写分支） |
| `_node_arrived_count` 等 fan-in 字段 | 删（孤儿清掉） |

## 测试覆盖

- 单测：19 AgentHistory + 15 AgentsList + 31 LogStream + 9 TestV2Dispatch + 既有 Widget 测试
- e2e：phase12/13/14 e2e + mxint tape 重放 + demo_loop iter 重入
- 完整性：EVENT_LEVEL 表 37 EventType 全覆盖（fail loud 守门）+ EVENT_VISIBILITY 同
- 接口审计：spec §11.5 7 条全过

## Follow-up（不在 v2 范围，登记给后续）

1. **chart 路径清理**：NodeDetail 内嵌 ChartPanel 拆出独立 widget（spec §6.3，0.5d，v2.1）
2. **fold DRY 抽公共**：v1.1.1 fold 抽到 `orca/run/projections.py`（spec §11.2 / §11.4 风险 C，0.5d）
3. **node 名可点击**：LogStream RichLog 不支持点击子串，需重写为 ListView（v2.1）

## 验收标准全过

- TUI 三区域布局生效
- j/k 切换 agent 后下个 event loop tick 内 AgentHistory 与 _node_events 逐项相等
- last message 默认展开（切换 agent 时 _expanded_seqs reset）
- Log Stream 完整显示失败原因（不截断）
- v1.1.1 widget 全删（grep 验证空）
- EVENT_VISIBILITY / EVENT_LEVEL 完整性测试通过（37 EventType）
- 全部单测 + e2e 通过
- mxint tape 重放 SVG 验证

---

**SPEC**：[`docs/specs/tui-redesign-v2-design-draft.md`](../specs/tui-redesign-v2-design-draft.md)
**plan**：`~/.claude/plans/twinkling-cuddling-seal.md`

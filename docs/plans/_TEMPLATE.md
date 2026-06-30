# 事前实施计划模板

> 实现前**先写计划**（SDD 流程：读 SPEC → 写计划 → 确认 → 实现）。
> 文件名：`docs/plans/<date>-<phase>-<name>.md`
> **计划不写代码**，只列：做什么、怎么做、怎么测。

---

## 示例骨架（阶段 1 schema 用）

# 计划：阶段 1 schema/ 数据层

## 目标
实现 `orca/schema/` 纯数据结构层（3 文件 + __init__），零逻辑。

## 依据
- SPEC：[`docs/specs/phase-1-schema.md`](../specs/phase-1-schema.md)

## 文件清单

### 1. orca/schema/workflow.py
- `Workflow`：name / description / entry / inputs / nodes / outputs
- `InputDef`：type / required / default / description
- `Node`（基类）：name / routes（phase 5 单轨化：去 after）
- `Route`：when / to
- `AgentNode`：prompt / tools / executor / model / output_schema
- `ScriptNode`：command / parse_json / timeout
- `SetNode`：values
- `ForeachNode`：source / item_var / index_var / body / max_concurrent / failure_mode
- `AnnotatedNode`：discriminated union（Field discriminator="kind"）

### 2. orca/schema/event.py
- `Event`：seq / type / timestamp / node / data
- `EventType`：Literal[25 个值]

### 3. orca/schema/state.py
- `RunState`：run_id / workflow_name / status / current_node / node_status / context / usage
- `Status`：Literal[5 个值]
- `UsageSummary`：input/output/cache tokens / cost / node_breakdown（递归）

### 4. orca/schema/__init__.py
- 导出 __all__（见 SPEC §5）

## 测试清单
- tests/schema/test_workflow.py：各 kind 构造 / discriminator 分派 / extra 报错 / Route 校验
- tests/schema/test_event.py：Event 构造 / 25 个 type / Literal 拒绝非法
- tests/schema/test_state.py：RunState / UsageSummary 递归

## examples
- examples/nas.yaml（SPEC §6.1）
- examples/parallel_research.yaml（SPEC §6.2）
- examples/batch_assess.yaml（SPEC §6.3）

## 验收
见 SPEC §7（全部勾选）。

## 偏离 SPEC 处
（实现中如需偏离，在此记录 + 理由，release note 同步）

## 风险/疑问
（实现中遇到的疑问，先问监工再动手）

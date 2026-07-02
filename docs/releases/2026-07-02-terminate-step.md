# Release Note —— Terminate Step（显式工作流终止节点）

> 日期：2026-07-02
> 类型：小 feature（post-phase 11 incremental）
> 计划：[`docs/plans/2026-07-02-terminate-step.md`](../plans/2026-07-02-terminate-step.md)
> Commit：（待填）

---

## 1. 目标与动机

加一个新的 node kind **`terminate`**：用户主动声明工作流终止（业务级成功/失败退出点）。

**不是**错误处理（错误处理已由 `ExecError + node_failed + workflow_failed` 覆盖），
**是**业务兜底（如分类器走不到任何 handler 时显式 reject）。

与默认 `route.to="$end"` 的区别：`$end` 只能 success；terminate 能显式 failed。
典型用法：

```yaml
nodes:
  - name: classifier
    kind: agent
    routes:
      - {to: handler_a, when: "output.category == 'A'"}
      - {to: handler_b, when: "output.category == 'B'"}
      - {to: reject}          # 兜底
  - name: reject
    kind: terminate
    status: failed
    reason: "未知类别 {{ classifier.output.category }}"
    outputs: {rejected_category: "{{ classifier.output.category }}"}
```

---

## 2. 实际做了什么（5 处代码改动 + 测试 + example）

### 2.1 schema 层（`orca/schema/workflow.py` + `__init__.py`）

新增 `TerminateNode` 类（继承 `Node`）：

| 字段 | 类型 | 语义 |
|------|------|------|
| `kind` | `Literal["terminate"]` | 判别字段 |
| `status` | `Literal["success", "failed"]` | 业务级成功/失败 |
| `reason` | `str = ""` | Jinja2 渲染 → `workflow_failed.data.message`（status=failed 时） |
| `outputs` | `dict[str, str] = {}` | status=success 时替代 `workflow.outputs`（每 key 独立渲染） |

- 加入 `AnnotatedNode` 联合（5 → 6 个 kind）
- **不**加入 `ForeachBody`（terminate 在 foreach body 内无意义，schema 层就拦）

### 2.2 executor 层（新文件 `orca/exec/terminate.py`）

`TerminateExecutor` 仿 `set_node.py` 模板：

1. `session_id = uuid4().hex`
2. `yield node_started({"kind": "terminate", "status": ...})`
3. 渲染 `reason` + 每个 `outputs` value（任一失败 → `ExecError(phase="render")`）
4. `yield node_completed({"output": {...}, "status": ..., "reason": ..., "outputs": ..., "elapsed": ...})`

**关键约束**：executor 自身**不**判断 `status` 决定终态事件。它只 emit `node_completed`
（按 Executor 标准契约），让 orchestrator 在 `_drive_from` 里看到 `kind=terminate` 时做
终态分发。这是单向依赖铁律的要求（executor 不 emit workflow_completed/failed）。

`status="failed"` 在 executor 视角下**不是失败**——它是 terminate 节点的业务声明，executor
正常完成（渲染成功就 node_completed）。render 失败才是 executor 失败（走 `ExecError` →
`node_failed` + `error` 双发，fail loud）。

### 2.3 factory 分派（`orca/exec/factory.py`）

加 `TerminateNode → TerminateExecutor()` 分派项。docstring 分派规则表更新。
terminate 分支忽略 `agent_tools_server` / `bus`（向后兼容，同 script/set/foreach）。

### 2.4 orchestrator 终态分发（`orca/run/orchestrator.py` + `orca/run/errors.py`）

**`orca/run/errors.py`** 新增 `WorkflowTerminated` 异常（编排层第 6 类信号），
携带 `status` / `reason` / `outputs` / `node`。

**`_drive_from`**：在 `_dispatch` 返回 `raw_output` 后、`_next_node_after`（route 求值）前
插入判断：

```python
node_obj = self._node_by_name.get(current)
if node_obj is not None and node_obj.kind == "terminate":
    raise WorkflowTerminated(
        status=raw_output["status"],
        reason=raw_output["reason"],
        outputs=raw_output["outputs"],
        node=current,
    )
```

**`run()` / `run_from_state()`** 各加 `except WorkflowTerminated` 分支，调共享 helper
`_finalize_terminated`（DRY：两入口对称）：

- `status="success"` → emit `workflow_completed`，`outputs=terminate.outputs`（**不**走
  `_evaluate_outputs(wf.outputs)`）
- `status="failed"` → emit `workflow_failed{error_type=WorkflowTerminated, message=reason, node=...}`

与 `_classify_error` 路径并列但**不**走它——terminate 不是「错误」，是显式声明，status=success
时还要 emit workflow_completed（错误路径永远只 emit workflow_failed）。

### 2.5 compile 层 fail loud（`orca/compile/validator.py`）

新增 `_check_terminate_constraints`（4 项校验）：

1. `terminate.routes` 必须空（非空 routes 是死代码 + 语义冲突）
2. 不能作为 `wf.entry`（terminate 必须先经业务节点）
3. 不能在 `ParallelGroup.branches` 里（语义不清，同 Conductor 限制）
4. foreach body 不能含 terminate：schema 层 `ForeachBody` 判别联合（仅 agent/script）
   已拦，到不了 compile/validator（test 验证 pydantic ValidationError）

`_check_jinja2_refs` 扩展：terminate 的 `reason` / `outputs` Jinja2 模板也走 ⑦ 浅校验
（fail loud 在 compile 期而非 run 期暴露坏引用）。

`validate_workflow` 调用顺序加入新检查（在 `_check_profiles` 之前）。

### 2.6 测试（共新增 ~20 cases，1013 passed，0 回归）

| 文件 | 新增/扩展 | 覆盖 |
|------|----------|------|
| `tests/exec/test_terminate.py`（新） | 9 cases | success/failed status / reason & outputs Jinja2 / 空默认值 / 渲染 fail loud / session_id 一致 |
| `tests/exec/test_factory.py` | +1 case | TerminateNode → TerminateExecutor 实例 |
| `tests/compile/test_validator.py` | +8 cases | routes 空校验 / entry 校验 / parallel branch 校验 / foreach body schema 校验 / jinja ref / success+outputs 合法 |
| `tests/run/test_orchestrator_terminate.py`（新） | 5 cases | success → workflow_completed(terminate.outputs) / failed → workflow_failed(WorkflowTerminated) / 不评估 routes / e2e script→terminate failed / e2e script→terminate success |

**关键守门**：
- `tests/events/test_replay.py::test_every_event_type_has_reducer_branch_or_explicit_noop` 仍 pass ——
  terminate **不**引入新 EventType（用既有 `node_started` / `node_completed` / `workflow_completed` /
  `workflow_failed`），reducer 零改动。
- `tests/exec/test_contract.py`（单向依赖铁律守门）仍 pass —— executor 不依赖 run/compile/events.bus。

### 2.7 example（`examples/terminate.yaml`）

minimal 例：script 分类器路由到 handler_a/handler_b/reject_terminate。包含 success path
（`finish_ok` terminate success）和 failed path（`reject_terminate` terminate failed）。

实测三条路径（真 shell 跑通）：
- `category=A` → handler_a → finish_ok(success) → `workflow_completed{outputs: {handled_category: "A"}}`
- `category=B` → handler_b → finish_ok(success) → `workflow_completed{outputs: {handled_category: "B"}}`
- `category=X` → reject_terminate(failed) → `workflow_failed{error_type: WorkflowTerminated, message: "未知类别 X（无对应 handler）", node: reject_terminate}`

---

## 3. SPEC 偏离记录

| 项目 | 偏离 | why |
|------|------|-----|
| 无 | 无 | 实现完全贴合计划，无字段语义/EventType/reducer 改动 |

---

## 4. 验证结果

- `pytest tests/`：**1013 passed, 7 skipped**（原 959 + 新增 ~20 = ~979；实际基线已增长到 994
  + 新增 19 = 1013。0 回归）
- 单向依赖守门（`tests/exec/test_contract.py`）：22/22 pass
- reducer 穷尽守门（`test_every_event_type_has_reducer_branch_or_explicit_noop`）：pass
- example yaml 真跑 3 条路径：全 OK（见 §2.7）

---

## 5. Commit SHA

`41a5936` —— `feat(orchestrator): terminate step —— 显式工作流终止节点（业务级 success/failed 退出点）`

---

## 6. 不做的事（明确边界）

- **sub-workflow**：Orca 还没 sub-workflow，Conductor 的 `SubworkflowTerminatedError` 不做
- **Web 前端 widget**：延后（unknown kind 已有 fallback，DAG 拓扑摘要含 terminate 节点）
- **新 SPEC 文件**：这是小 feature，写在 mini 计划里（`docs/plans/2026-07-02-terminate-step.md`）
- **`reason` 多行模板支持**：复用既有 `render_template`（trim_blocks / lstrip_blocks），无新逻辑

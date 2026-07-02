# Mini Plan —— Terminate Step（显式工作流终止节点）

> 日期：2026-07-02
> 类型：小 feature（post-phase 11 incremental）
> 状态：已对齐设计，直接实现
> SPEC 偏离：无（小 feature，不写新 SPEC）

---

## 1. 目标（Goal）

加一个新的 node kind `terminate`：用户主动声明工作流终止（业务级成功/失败退出点）。
**不是**错误处理（错误处理由 ExecError + node_failed + workflow_failed 覆盖），
**是**业务兜底（如分类器走不到任何 handler 时显式 reject）。

### 语义（已对齐）

- 触达 `terminate` node → **不评估 routes**，直接终止工作流
- `status: success` → emit `workflow_completed`（用 `node.outputs` 替代 `workflow.outputs`）
- `status: failed` → emit `workflow_failed`，`error_type="WorkflowTerminated"`，
  `message=<渲染后的 reason>`，`node=<terminate node name>`
- 与默认 `route.to="$end"` 区分：那个只有 success；terminate 能显式 failed

---

## 2. 字段定义（schema 契约）

```python
class TerminateNode(Node):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["terminate"] = "terminate"
    status: Literal["success", "failed"]
    reason: str = ""              # Jinja2 渲染 → workflow_failed.data.message
    outputs: dict[str, str] = {}  # status=success 时替代 workflow.outputs
```

- 加入 `AnnotatedNode` 联合（5 → 6 个 kind）
- **不**加入 `ForeachBody`（terminate 在 foreach body 内无意义）
- 继承 `Node.routes: list[Route] = []`，但 compile 层强制为空

---

## 3. 改动清单（5 处，~60 行核心代码）

| # | 文件 | 改动 |
|---|------|------|
| 1 | `orca/schema/workflow.py` | 新增 `TerminateNode` 类 + 加入 `AnnotatedNode` |
| 1b | `orca/schema/__init__.py` | export `TerminateNode` |
| 2 | `orca/exec/terminate.py`（新） | `TerminateExecutor`，照抄 `set_node.py` 模板 |
| 3 | `orca/exec/factory.py` | 加 `TerminateNode → TerminateExecutor` 分派 + import |
| 4 | `orca/run/orchestrator.py` | `_drive_from` 在 route 求值前判断 `TerminateNode` → emit 终态事件 + return |
| 5 | `orca/compile/validator.py` | 4 项 fail loud 校验（routes 空 / 非entry / 非parallel branch / 非foreach body） |

### 关键约束（铁律）

- **单向依赖**：executor 只 yield Event，**不** emit workflow_completed/failed（那是 orchestrator 的职责）
- **fail loud**：terminate 的 routes 非空 / 当 entry / 在 parallel/foreach 里 → compile 层 raise
- **零改动**：events/EventType、reducer、Tape、EventBus、RetryPolicy、validator 的既有逻辑

---

## 4. 测试矩阵

### `tests/exec/test_terminate.py`（新建，~6 cases）
- success status → `node_started` + `node_completed(status=success, outputs=...)`
- failed status → 同上但 `status=failed`
- reason Jinja2 渲染（`{{ inputs.x }}`）
- outputs Jinja2 渲染（每 key 独立）
- 空 reason / 空 outputs（向后兼容）
- 渲染失败 → `node_failed` + `error`（phase=render）

### `tests/exec/test_factory.py`（扩展 1 case）
- `TerminateNode → TerminateExecutor` 实例

### `tests/compile/test_validator.py`（扩展 ~5 cases）
- terminate with non-empty routes → ValidationError
- terminate as entry → ValidationError
- terminate in parallel branches → ValidationError
- terminate in foreach body → ValidationError（schema 层就拦，但加 compile 层兜底）
- 正常 terminate → 通过

### `tests/run/test_orchestrator.py`（扩展 ~3 cases）
- terminate success → emit workflow_completed，outputs 用 terminate.outputs
- terminate failed → emit workflow_failed(error_type=WorkflowTerminated, message=reason, node=...)
- terminate 触达后不评估 routes（验证下游 node 不执行 / router 没被调）

### Reducer 不变性（关键守门）
- 既有 `test_every_event_type_has_reducer_branch_or_explicit_noop` 自然 pass —— terminate 不引入新 EventType
- terminate 的 `node_completed` 走现有 reducer 分支

### E2E（fake executor，`tests/exec/test_terminate.py` 内）
- minimal workflow: classifier → reject_terminate → workflow_failed with reason

---

## 5. Example

`examples/terminate.yaml`：minimal 例，含 success / failed 两个 path（用 script 模拟分类器）。

---

## 6. 偏离记录（预留）

若实现中发现设计需调整（如发现需要新 EventType / reducer 改动 / 字段语义需改），
按 CLAUDE.md Rule 7：选一个方案 + 在 release note "SPEC 偏离" 表里记录 why。

**当前预期偏离**：无。

---

## 7. 完成标准

- [ ] 5 处代码改动落地
- [ ] 测试通过：原 959 + 新增 ~15-20 → ~975-980 passed，**0 回归**
- [ ] `pytest tests/` 全绿
- [ ] 单向依赖守门通过（`tests/exec/test_contract.py`）
- [ ] code-reviewer 横切自检无 🔴
- [ ] example + release note + CHANGELOG + CURRENT 更新
- [ ] commit message 符合项目风格

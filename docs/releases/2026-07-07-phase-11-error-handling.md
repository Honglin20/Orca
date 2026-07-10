# phase-11 统一错误处理（ErrorKind 11 分类 + Result 信封 + classifier 双入口）

**日期**：2026-07-07
**范围**：横切层错误契约（exec / run / schema，不含 TUI widgets / app.py）
**依据 SPEC**：[`docs/specs/phase-11-error-handling.md`](../specs/phase-11-error-handling.md) v2.1 / [`docs/specs/2026-07-06-interface-convergence-adr.md`](../specs/2026-07-06-interface-convergence-adr.md) §4.1

## 改了什么

### 新增模块（4 个，纯 exec 层）

| 文件 | 内容 |
|---|---|
| `orca/exec/error_kinds.py` | `ErrorKind` 枚举（11 值）+ 默认策略表 `_DEFAULT_RETRYABLE` + layer 派生表 `_KIND_LAYER_PREFIX` + 反向映射 `_LEGACY_ERROR_TYPE_TO_KIND` + phase 默认 kind 表 `_DEFAULT_KIND_FOR_PHASE` |
| `orca/exec/result.py` | `Result{ok,data?,error?,_hint?}` + `Error{kind,message,raw,retryable,cause_id}` frozen dataclass（**无 layer 字段**，ADR §4.1 决策 1.3）+ `from_exec_error` 投影 + `with_hint` 跨边界重写 API + UNKNOWN raw `__post_init__` validator |
| `orca/exec/classifier.py` | 双入口 `classify_exception` + `classify_backend_output`，纯函数 first-match-wins 规则表（SPEC §2.2）；profile 钩子调度顺序：profile→通用→UNKNOWN |
| `orca/exec/retry.py` | 三层重试边界抽象：`RetryPolicy` kind 维度策略 + `compute_backoff_delay` 退避算法（constant/linear/exponential + jitter）+ `emit_retry_started/exhausted` 帮手（带 layer/kind/reason/next_retry_at）+ `is_retryable` retry_on 解耦 + `_RetryEventSink` Protocol（duck typing，避免 exec/ 硬依赖 events.bus） |

### 修改文件

**核心契约变更**：
- `orca/exec/error.py`：`ExecError` 字段集改 `{kind, message, phase, node, raw}`（kind 必填，唯一分类轴）；构造器接受 `kind: ErrorKind | str | None`，None 时按 `_DEFAULT_KIND_FOR_PHASE[phase]` 派生默认（保守默认，stream→PROTOCOL_PARSE）；`error_type` 降级为**派生只读属性**（迁移期诊断，返回 `raw["error_type"]` 或 `phase_to_error_type(phase)`）；`from_failed_data` 读兼容期：先 kind 后 error_type（经 `_LEGACY_ERROR_TYPE_TO_KIND` 反向映射）
- `orca/run/errors.py`：`WorkflowAborted / MaxIterationsError` 改 `ExecError` 子类（固定 `(kind, phase)` 元组：`(BUSINESS_GATE, "interrupted")` / `(BUSINESS_CONFIG, "max_iterations")`）；`WorkflowTerminated` **保留独立**（success 路径不发 error，failed 路径由 orchestrator 翻译为 `node_failed{kind=BUSINESS_AGENT}`）
- `orca/run/router.py`：`RouteError` 改 `ExecError` 子类（`(BUSINESS_CONFIG, "route_deadlock")`）
- `orca/run/orchestrator.py`：`_classify_error` 改返 ErrorKind 值不返字符串字面量（`"MaxIterations"` → `"business_config"` 等）；`except (ExecError, GroupFailure)` 简化（子类归 ExecError）；`_finalize_terminated` 翻译规则改 kind=business_agent

**emit 方迁移**（8 文件，写 kind + 保留 error_type 读兼容）：
- `orca/exec/{set_node,terminate,script,wait}.py`：node_failed/error data 加 kind
- `orca/exec/claude/executor.py`：4 处 raise ExecError + node_failed data 加 kind
- `orca/profiles/translators/{claude,opencode}.py`：error event data 加 kind

**消费方 / lifecycle**：
- `orca/run/lifecycle.py`：`make_workflow_failed` 同时写 `kind` + `error_type`（读兼容期）
- `orca/run/{retry,executor_adapter}.py`：raise ExecError 删 `error_type=` 参数；retry emit 加 kind/layer/last_kind
- `orca/schema/event.py`：注释 error_type → kind（retry_started.data 扩展 layer/reason/next_retry_at/kind）
- `orca/exec/__init__.py`：导出 `ErrorKind` / `Error` / `Result`
- `orca/exec/interface.py`：docstring 注释 data 字段含 kind

**测试 fixture 迁移**：
- 新增 4 个测试文件：`tests/exec/{test_result,test_classifier,test_retry,test_error_kind_mapping}.py`（126 测试）
- 迁移 fixture：`tests/exec/test_contract.py`（ExecError 字段断言）+ `tests/run/{test_orchestrator,test_orchestrator_terminate,test_demo_integration,test_interrupt_e2e}.py`（断言 `data["kind"]` 取代 `data["error_type"]`）

## 关键设计裁决

1. **kind 是唯一分类轴**（ADR §4.1 决策 1.4）：任何层不据 phase/message 重新分类；classifier 纯函数，profile 钩子优先但禁止抛错
2. **error_type 降级为派生只读属性**：旧测试读 `e.error_type` 仍工作（property 优先返回 raw 中保留的 legacy 值，否则 phase_to_error_type(phase) 派生）。这不是双分类轴——分类权威唯一（kind），error_type 仅诊断字符串
3. **ExecError.phase 保留为诊断子字段**：kind 必填但 phase 仍透传到 node_failed.data.phase；新增 phase `max_iterations` / `route_deadlock`（编排层诊断）
4. **读兼容期**：emit 写 `kind` + 保留 `error_type` 字段（值同 kind）；reducer 读 `data.get("kind") or data.get("error_type")`；旧 tape（仅有 error_type）经 `_LEGACY_ERROR_TYPE_TO_KIND` 反向映射
5. **未碰 TUI widgets / app.py**：并行 TUI 重构占用，消费方读兼容改动留给 TUI 工作树

## 偏离 SPEC 的地方（Rule 7 surface conflicts）

### 1. emit 端顶层双写 kind + error_type（**显式技术债**）

**SPEC 严格读法**（§4.2 / ADR §4.1.2）：「写路径只写 `kind`，不写 error_type」。本实现选择**顶层双写**（emit dict 同时含 `kind` + `error_type`，值同 `kind.value`，仅 `translators/claude.py` 的 `error_type="ApiRetry"` 是 legacy 名）。

**理由**（用户指令冲突下的折衷）：
- 用户明确「严禁碰 `orca/iface/cli/widgets/` 与 `app.py`」（并行 TUI 重构占用）
- TUI widgets 当前读 `data["error_type"]`（log_stream.py / _event_summary.py / app.py 共 10+ 处）
- 若 emit 只写 kind，TUI 显示立即回归「?」（直到并行 TUI 工作树迁移读兼容）
- 顶层双写让旧 widget 零改动即可工作，把读兼容**只**留给新 tape 重放场景的额外保险

**清理边界**：
- 并行 TUI 工作树负责迁移 widget 读路径到 `data.get("kind") or data.get("error_type")`
- 待 TUI 迁移完成（phase-13 PR），emit 端改只写 kind（删 `error_type` 字段），同时移除 ExecError.error_type 派生 property
- 已登记：本 release note + `docs/specs/phase-11-error-handling.md` §4.3 注释

### 2. SPEC §4.2 "ExecError 字段集 {kind,message,phase,node,raw}" vs 保留 error_type 派生属性

SPEC 严格读法应删 error_type 字段。本实现降级为只读 property（不存为字段），保留旧测试可读。**理由**：减少 116 处测试 fixture 迁移成本，且 property 是「派生只读」非「并存字段」，不构成第二套分类轴。**清理**：与上面 #1 同步在 phase-13 移除。

### 3. classifier 未覆盖编排 exception 的 isinstance 分支

SPEC §2.2 行 14/15 列了 RouteError/MaxIterationsError 的 isinstance 分支。本实现依赖它们是 ExecError 子类，走 classify_exception 的第一个分支（`isinstance(exc, ExecError)` → `Error.from_exec_error(exc)`），kind 透传——避免 exec/ 反向 import orca.run（依赖单向铁律，tests/exec/test_contract.py grep 守门）。

## 验收

- ✅ ErrorKind 11 值全覆盖（3 transport + 3 protocol + 4 business + unknown）
- ✅ ExecError 字段集 = `{kind, message, phase, node, raw}`；Error 信封投影（from_exec_error）
- ✅ WorkflowAborted/MaxIter/RouteError 是 ExecError 子类；WorkflowTerminated 保留独立
- ✅ error_type → kind 全量替换（emit 方）；读兼容期 `data.get("kind") or data.get("error_type")`
- ✅ retry_on Literal 保持独立不改名（retry_on 命中强制 retryable=True 覆盖默认）
- ✅ classifier 双入口 + profile 钩子调度顺序 + UNKNOWN 兜底
- ✅ ExecError 不依赖 orca.run（依赖单向铁律，tests/exec/test_contract.py 静态守门）
- ✅ 1512 单测全过（1386 既有 + 126 新增），0 回归
- ⏳ TUI 消费方读兼容改动（log_stream/_event_summary/app.py）留给并行 TUI 重构工作树

## 验证

```
$ python -m pytest tests/ --ignore=tests/e2e_phase13 --ignore=tests/e2e_phase14 -q
1512 passed, 30 skipped in 155.50s
```

## Commit SHA

`451dd39`

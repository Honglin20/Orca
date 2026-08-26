# 实施计划：in-session 失败哨兵 + 失败历史注入

> **配套 SPEC**：[`docs/specs/2026-08-04-in-session-failure-sentinel-and-injection.md`](../specs/2026-08-04-in-session-failure-sentinel-and-injection.md)（v3，两轮 spec-reviewer 闭环）。
> **范围**：仅 in-session 路径。`orca run`/executor 零改。造假检测不做。
> **流程**：本计划 → coder-agent 实现（含自我 code-review）→ test-agent 端到端闭环 → 提交。

---

## 0. 实现顺序（依赖序，不可乱序）

1. **step.py 引擎核心**（§1）—— 全部 helper + 数据流改完，先跑通纯函数。
2. **step.py 单测**（§2 AC1-3,5-7,9-11,13,15）—— 守契约。
3. **AST/grep 守门**（§2 AC12/AC14）—— 守零改前提。
4. **TARS skill + 2026-07-23 SPEC 注记**（§3）。
5. **前端回归 + 端到端**（§4 AC8 + E2E）。

---

## 1. `orca/run/step.py` 改动（逐点，附现状行号）

### 1.1 新常量（`~:65` ERR_* 区）
```python
ERR_AGENT_BLOCKED = "agent_blocked"
```

### 1.2 `RecoverableInSessionError`（`:99-100`，M1）
`__init__` 增可选参，error_kind 默认仍 schema_mismatch（向后兼容既有 raise）：
```python
def __init__(self, message: str, *, error_kind: str = ERR_OUTPUT_SCHEMA_MISMATCH,
             blocked_on: str | None = None, tried: list[str] | None = None):
    super().__init__(message, error_kind=error_kind)
    self.blocked_on = blocked_on
    self.tried = tried
```
哨兵路径 raise 时传 `error_kind=ERR_AGENT_BLOCKED, blocked_on=..., tried=...`。

### 1.3 `_parse_output` 哨兵 peek（`:184-223`，M3 + m7）
**在 `if not schema: return raw`（`:196`）之前**插入 peek（关键：无 schema 节点也要检测）：
```python
# peek：失败哨兵优先于 schema 校验（M3）。peek 成功的 dict 复用给 schema 路径（m7，避免双解析）。
peeked = None
try:
    peeked = json.loads(raw)
except (json.JSONDecodeError, TypeError):
    peeked = None
if isinstance(peeked, dict) and peeked.get("_sentinel") == "orca_node_failed_v1":
    blocked_on = _coerce_str(peeked.get("blocked_on"))  # 缺/空 → None
    tried = _coerce_tried(peeked.get("tried"))           # list coerce + 截断
    reason = _coerce_str(peeked.get("reason"))
    msg = blocked_on or reason or "malformed sentinel: missing or empty blocked_on"
    raise RecoverableInSessionError(msg, error_kind=ERR_AGENT_BLOCKED,
                                    blocked_on=blocked_on, tried=tried)
```
既有 schema 路径改为优先用 `peeked`（若非 None）而非二次 `json.loads`。新增两个 ingest helper：`_coerce_str(x)`（非 str → str()，截断 200）、`_coerce_tried(x)`（非 list → `[str(x)]`，元素 str() 化，≤5 项 × 120 字符）。

### 1.4 `consecutive_failures` + delegate（`:152-172`，N8/N7）
新增：
```python
def consecutive_failures(tape, node) -> list[dict]:
    """当前节点连续 node_failed(node) 的 data 记录（reset 谓词同 count：遇任意 node_completed 归零）。
    物化 list O(k)，k≤2（DRY 权衡，N7）。缺字段 data 由消费方 .get() 防御。"""
    records = []
    for event in tape.replay():
        if event.type == "node_completed":
            records = []
        elif event.type == "node_failed" and event.node == node:
            records.append(event.data or {})
    return records
```
`consecutive_fail_count` 改为 `return len(consecutive_failures(tape, node))`。

### 1.5 `_render_failure_history`（新增 helper）
```python
def _render_failure_history(records, retry_count, retry_budget) -> str | None:
    if not records:
        return None
    # kind-aware 格式化；防御 .get()；纯文本拼接（不进 Jinja）。
    lines = [f"## ⚠️ 本节点前序尝试失败（本次第 {retry_count}/{_RECOVERABLE_ESCALATE_AT} 次，"
             f"耗尽将终止 run）"]
    for i, d in enumerate(records, 1):
        kind = d.get("kind", "?")
        lines.append(f"\n### Attempt {i} — failed [{kind}]")
        if kind == ERR_AGENT_BLOCKED:
            lines.append(f"blocked_on: {d.get('blocked_on') or d.get('message', '')}")
            tried = d.get("tried") or []
            if tried:
                lines.append("tried: [" + ", ".join(str(t) for t in tried) + "]")
        else:
            lines.append(str(d.get("message", "")))
    return "\n".join(lines)
```

### 1.6 `_node_failed_data`（`:335-349`，M1 + N5）
改读 `exc.error_kind`（非硬编码）+ additively 读结构字段：
```python
def _node_failed_data(exc):
    data = {"kind": exc.error_kind, "error_type": exc.error_kind,
            "message": str(exc), "phase": "agent_self_report" if exc.error_kind==ERR_AGENT_BLOCKED else "output_validation"}
    if exc.error_kind == ERR_AGENT_BLOCKED:
        if getattr(exc, "blocked_on", None):
            data["blocked_on"] = exc.blocked_on
        if getattr(exc, "tried", None):
            data["tried"] = exc.tried
    return data
```

### 1.7 `_deliver`（`:265-288`，m1 + footer + history）
签名增 `failure_history: str | None = None`。渲染后顺序：memory 注入 → history prepend → footer append：
```python
rendered = _render_or_fail(node, ctx)
if memory: rendered = inject_memory_prompt(...)
if failure_history: rendered = failure_history + "\n\n" + rendered
rendered = rendered + "\n\n" + _FAILURE_SENTINEL_FOOTER  # 恒 append（host contract）
# 既而写文件 / 返 inline
```
`_FAILURE_SENTINEL_FOOTER` 为模块常量（§4.4 教学脚注，已删 `_orca_node_failed` 字段）。
**调用者**：advance_step 内 2 处（`:496`/`:537`）保 None；`_recover_step_result:401` + 幂等重发 `:551` 传计算值（V2-3）。

### 1.8 `_recover_step_result`（`:352-419`，R-N2 + m4/m6 + m2）
- 先构 `nf_emit`：`emits = [Emit("node_failed", _node_failed_data(exc), node=pending), Emit("node_started", {"node": pending}, node=pending)]`。
- **R-N2（含本次）**：`records = consecutive_failures(tape, pending) + [emits[0].data]`。
- `failure_history = _render_failure_history(records, this_attempt, retry_budget)` → 透传 `_deliver(..., failure_history=failure_history)`。
- **m4/m6（升格）**：`make_workflow_failed(exc.error_kind, reason, ...)`；reason = `consecutive recoverable exhausted: 节点 {pending!r} 连续 {N} 次失败（{_kind_breakdown(consecutive_failures(tape,pending)+[emits[0].data])}）`；升格 `StepResult.error_kind=exc.error_kind`。
- **m2（hint）**：改为「节点自报失败/产出不合 schema（第 {this_attempt}/{N} 次）。重 arm 的 prompt 已含历次失败原因（含本次）——按你的判断重派（复用同 agent 或 fresh），拿产出再 orca next --output（剩余 {retry_budget} 次）」。
- re-arm `StepResult.error_kind=exc.error_kind`（`:418`，M1）。

### 1.9 `advance_step` 幂等重发分支（`:544-556`，M2 + V2-2）
```python
# branch 4：无 output 幂等重发 pending prompt。count>0 时注入历史（cross-session resume）。
count = consecutive_fail_count(tape, pending)
failure_history = (_render_failure_history(consecutive_failures(tape, pending), count, _RECOVERABLE_ESCALATE_AT - count)
                   if count > 0 else None)   # V2-2：count 已含落 tape 的失败 = re-arm 的 this_attempt
prompt, prompt_file, rroot = _deliver(nodes[pending], ctx, prompts_dir,
                                     wf=wf, project_root=project_root, no_memory=no_memory,
                                     failure_history=failure_history)
```

---

## 2. 测试矩阵（AC → 测试，落 `tests/` 既有 in-session step 测试旁）

| AC | 测试 | 形态 |
|---|---|---|
| AC1 | 合法哨兵 → `{done:false,recoverable:true,error_kind:agent_blocked,retry_count:1,retry_budget:2}` + `node_failed.data` 含 blocked_on+tried | advance_step 单测（inline 模式） |
| AC2 | 第 2 次 recoverable → 重 arm prompt 顶部含 attempt 1 + attempt 2（本次）；count=0 时无块 | 单测断言 `_deliver` 返 / `prompt_file` 内容 |
| AC3 | 3 次混合 → 终态 + tape 顺序 `nf→ns→workflow_failed` + 升格 kind=本次 kind；**fixture**：2×blocked→nc→1×schema 不终态 | 单测 |
| AC5 | 有/无 schema 节点发哨兵均 → agent_blocked（peek 在 `if not schema` 前） | 单测 |
| AC6 | 畸形哨兵（blocked_on 缺/空）→ agent_blocked + `data.message` 含 "malformed sentinel" + data 无 blocked_on | 单测 |
| AC7 | output_schema 含 `_sentinel` 字段 → 不崩，peek 优先判 agent_blocked | 单测 |
| AC9 | StepResult 无 dispatch 副作用；信封无 fresh/复用指令字段 | 单测（grep StepResult 字段集） |
| AC10 | 跨 session：构造 tape（2 次 nf）→ 新 session `advance_step()` 无 output → 重发 prompt 含历史（2 条） | 单测（replay tape + advance） |
| AC11 | `consecutive_fail_count` delegate；`_render_failure_history` 缺字段不崩；**tried wrong-type**（str/dict/int）coerce 不崩 | 单测 |
| AC13 | `consecutive_failures` 4 fixture（连续/nc 重置/跨 ws/缺字段） | 单测 |
| AC15 | 超长 blocked_on(500)+tried(10) → data 字段截断到限长 | 单测 |
| AC12 | grep `_step_io.py`/`cli.py`/`daemon.py` 可执行代码（排除 docstring/comment）无 `agent_blocked` 字面分支 | grep 守门脚本/测试 |
| AC14 | AST 断言 `events/replay.py` 的 `node_failed` 分支不读 `data.*` | AST 守门测试 |

> AC4（教学脚注恒在 + additive）：并入 AC2 测试——断言首 arm + re-arm prompt 末尾均含 footer 串，且节点 `agent.md` 文件本身不含。

---

## 3. skill + SPEC 注记

### 3.1 `orca/skills/tars/SKILL.md`（SPEC §5 itemized）
- 删 `:140-141` 造假条。
- 改驱动循环：仅 ask-user 哨兵拦截，其余一律喂 next。
- `:144-150` recoverable 段增 agent_blocked 同处理。
- `:149` 删「手动注入 reason」→「历史已由引擎注入」。
- `:198` success_criteria 过时表述改。
- hint 措辞引用 SPEC §6.⑩。

### 3.2 `docs/specs/2026-07-23-in-session-error-management.md`
- 顶部加「被 2026-08-04 扩展」标记。
- §4.2 加注（4 字段为下限，可 additive；reducer 不读 data）。
- §5(A)3 改「引擎注入（见 2026-08-04 §4.3）」。

---

## 4. 前端回归 + 端到端（AC8 + E2E）

### 4.1 前端（AC8，零改回归）
确认/补一个前端测试：构造 `[node_failed,node_started]` event 流 → LogStream 出「node FAILED」+「node started」两行（`selectors.ts:694/698`）。若 `orca/iface/web/frontend/test/` 已有覆盖则只跑绿。

### 4.2 端到端（test-agent，真实 CLI 面）
驱动真实 `orca` CLI 表面（非单测 mock），最小 workflow（一个 agent 节点 + output_schema 或无 schema）：
1. `orca <wf> --inputs '{...}'` → run_id + 首节点 prompt 指针。
2. 读 prompt 指针，确认含教学脚注。
3. `orca next --run-id X --output '<失败哨兵 JSON>'` → 断言信封 `{recoverable:true, error_kind:agent_blocked, retry_count:1}`；读重 arm prompt 指针断言含「前序尝试失败」块 + 本次 blocked_on。
4. `orca next --run-id X --output '<失败哨兵 JSON>'`（第 2 次）→ 断言 prompt 含 attempt 1 + 2（含本次）。
5. `orca next --run-id X --output '<合法产出>'` → 断言推进到下一节点 / done。
6. （边界）第 3 次失败 → 断言终态 `workflow_failed`。
产出真实执行证据日志（命令 + stdout + 断言）。复用 `tests/e2e_phase13/` 既有 harness 约定。

---

## 5. 边界 / 约束（coder-agent 必守）

- **不 git commit**：当前工作树有大量与本任务无关的未提交改动（见 `git status`）。实现 + 测试 + 自我 review 即止，**提交由协调者（我）在 E2E 通过后只 stage 本任务相关文件**。
- **不改 `docs/status/CURRENT.md`**：该文件追踪另一任务（kd-train-script），不覆盖。
- **依赖铁律**：改动仅 `run/step.py` + skill + 2 个 SPEC md + 测试；`_step_io.py`/`cli.py`/`daemon.py`/`events/`/前端 零改（AC12/AC14 守门）。
- **不侵灵活度**：引擎不 dispatch、不决定 fresh/复用（AC9）。
- **fail loud**：畸形哨兵不当成功放行（AC6）；ingest 类型 coerce 不崩（AC11）。

## 6. 验收（实现完成的判定）

- 全量回归绿（既有 in-session 测试 + 新增 AC 测试）；前端 AC8 绿。
- code-reviewer 闭环（依赖铁律 / DRY / fail loud / 测试覆盖意图）。
- test-agent 端到端证据日志通过（步骤 1-6）。
- 上述全绿后，协调者提交本任务相关文件 + 更新 `docs/status/CHANGELOG.md`（本任务条目）。

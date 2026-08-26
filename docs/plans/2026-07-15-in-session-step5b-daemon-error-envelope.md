# Plan: in-session v5 §8 step 5b —— daemon batch emit + in-session 错误信封统一（×2）

> SPEC：[`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5 §7.5（**已回写：×3→×2 + MCP 排除**）/ §2.3（**已回写：信封加 error_kind**）/ §8 step 5b（独立 commit，C3）
> 状态：spec-reviewer CONDITIONAL-PASS，已按 Q1/Q2/Q3 + 字段陷阱 + 零覆盖 + SSOT debt 重写（B1-B7 闭环）| 分支 `in-session-unified-backend` | 前置：5a `bce29f8`
> 侦察：2026-07-15 实读 `daemon.py` / `events/bus.py` / `cli.py` / `lifecycle.py` / `step.py` / `mcp/server.py`

---

## 0. 目标与成功标准

SPEC §7.5 两件事，**经 spec-reviewer 实读裁定后定稿**：

1. **daemon batch emit（真活，非「已落地」）**：`daemon.py:129-130` 逐条 `bus.emit`，注释「原子批量 emit（反例 A 消除）」**为假**。`_on_signal→cleanup→bus.close→sys.exit`，SIGTERM 落批内 emit N 与 N+1 之间 → N 已 flush、N+1 永不发 → resume 见 `workflow_started` 无 running node → `next()` raise `state_corrupt`。**声称不成立的正确性属性本身就是 blocker（铁律 12）**。修：daemon 改用 `bus.emit_batch`（cli.py:585/674 已用）。
2. **in-session 错误信封统一（×2，非 ×3）**：daemon + cli 两个 in-session 失败信封统一以 `InSessionError.error_kind` 为分类轴 + 单 helper。**MCP 出 scope**（见 §1.3）。

**成功标准**：
1. daemon `next()` 用 `emit_batch`（非逐条）；注释不再撒谎。
2. daemon `_fail` 不再 isinstance 粗分（"in_session_error" 塌缩消除），读 `exc.error_kind`。
3. daemon + cli 失败信封形：`{done:True, error_kind:<kind>, reason:"failed: <msg>"}`（**信封新字段 `error_kind`**）；tape `workflow_failed.data.kind`（**字段名不变，仍 `kind`**）携带同一值。
4. 单一 helper（`apply_step_result` + `fail_in_session`，落 `iface/in_session/`），吸收 cli 的 `_emits_to_event_datas`，daemon/cli 共用，零重复。
5. daemon 测试覆盖从 0 → 有（`test_daemon.py` 前置）。
6. 守门：daemon/cli 失败路径 grep 不含 `"in_session_error"` 字面量（塌缩值消除）。
7. 全量单测 0 回归；E2E：故意失败 run（output 畸形 → `output_schema_mismatch`）daemon + cli 两路都 emit 正确 kind 到 tape + 信封含 error_kind。

---

## 1. 现状（spec-reviewer 实读裁定）

### 1.1 cli.py —— 参考骨架（基本到位，两处补）
- `_classify_in_session_error(exc)`（L281-287）：读 `exc.error_kind`，类型安全无消息匹配。✓
- `_emits_to_event_datas`（L303-313）：`list[Emit]→emit_batch 入参`，被 L585/674 用。✓（5b 抽进 helper）
- next/bootstrap 失败路径（L541-546 等）：emit workflow_failed with error_kind。✓
- **缺口**：用户可见信封 `{"done":True,"reason":...}`（L546）**不含 error_kind** → 主 session/监控拿不到结构化分类。5b 补 `error_kind` 字段。

### 1.2 daemon.py `_fail`（L143-152）—— ❌ 主要缺口
- `error_type = "in_session_error" if isinstance(exc, InSessionError) else "internal_error"` —— **isinstance 二分，完全不读 `exc.error_kind`**。`output_schema_mismatch`/`state_corrupt`/`render_error` 等具体分类**全塌缩成 "in_session_error"**。
- 返回 `{"done":True,"reason":...}` —— 无 error_kind。
- emit workflow_failed 用塌缩值（L148）→ tape 失败分类**丢精度**。

### 1.3 mcp/server.py —— ⚠️ **出 scope（spec-reviewer Q2 裁定）**
- 8 tool 全用 `Result`/`Error`/`ErrorKind`（phase-11 ExecError taxonomy，编排 run 轴）。
- `tool_start_workflow`→`manager.start_run`→Orchestrator 全量编排，**不调 advance_step，不产 InSessionError**。
- `tool_get_task_status`→registry-gated 内存表（in-session CLI/daemon 独立进程，run_id 从不进该表）；`tool_get_task_history`→tape 经 `_require_handle`。8 tool **无一扫 in-session tape**。
- **结论**：MCP 失败信封是 ErrorKind 轴（不同层不同轴），不经 InSessionError。**MCP 出 5b scope**。SPEC §7.5 已回写 ×3→×2。

### 1.4 两套 taxonomy 边界 + 共享 `kind` 字段 debt（spec-reviewer Q3/issue3）
- **`InSessionError.error_kind`**（str 常量，SPEC §2.5）：in-session 编排层（`internal_error`/`state_corrupt`/`output_schema_mismatch`/`render_error`/...）。5b 统一对象。
- **`ExecError.kind`**（`ErrorKind` 枚举，phase-11）：executor 层（`TRANSPORT_*`/`PROTOCOL_*`/`BUSINESS_*`）。**5b 不动**。
- **共享碰撞点**：`lifecycle.make_workflow_failed`（L139-144）对所有 `workflow_failed` 写同一个 `data.kind` 字段。orchestrator 经 `_classify_error` 写 ErrorKind 值（orchestrator.py:307/606/608），daemon/cli 写 error_kind 值。**tape `kind` 是两套值集的共享字段**。
- **5b 处理（登记 debt，不解决）**：只统一 in-session 两个信封的 `kind` 值**来源**（都读 `exc.error_kind`）；**不动 ErrorKind 值、不加字段区分两值集**（跨阶段 debt，登记 CURRENT，不改）。措辞严守「统一来源」非「tape 层合并值集」。

---

## 2. 架构审视（spec-reviewer 认可 SSOT / 两套 taxonomy 不合并 / helper 抽象）

- **SSOT**：失败分类当前 2 真相源（cli 读 error_kind 对 / daemon isinstance 错）。统一 → `InSessionError.error_kind` 唯一分类轴（in-session 层）。
- **DRY**：信封拼装 3 处重复（daemon/cli 各自 + emit）；`_emits_to_event_datas` cli 已有、daemon 改 emit_batch 需同转换 → 抽 helper 吸收。
- **fail loud**：batch emit 半截 tape 是静默正确性违规（铁律 12）；isinstance 塌缩丢精度。两者都修。
- **改后清理**：删 daemon isinstance 分流；删 cli/daemon 重复拼装；daemon 撒谎注释。

---

## 3. 三个 Q 裁定（spec-reviewer 已闭环）

| Q | 裁定 |
|---|---|
| Q1 batch emit | **真活**。daemon→`emit_batch`（照 cli.py:585）。注释「反例 A 消除」为假，删。 |
| Q2 mcp | **出 scope**。8 tool 全 ErrorKind，不产 InSessionError。SPEC §7.5 ×3→×2。 |
| Q3 helper 落点 | `iface/in_session/`（新模块）。边界扩为两函数（见 §4.1）。 |

---

## 4. 改动范围

### 4.1 抽 helper（落 `orca/iface/in_session/_step_io.py` 或同包新模块，spec-reviewer Q3）

```python
def apply_step_result(bus, result) -> dict:
    """成功路径：emit_batch(result.emits) + 构造回复 {done, node?, prompt?, reason?}。"""
    await bus.emit_batch(_emits_to_event_datas(result.emits))   # 吸收 cli.py:303-313
    reply = {"done": result.done}
    if result.node: reply["node"] = result.node
    if result.prompt: reply["prompt"] = result.prompt
    if result.reason: reply["reason"] = result.reason
    return reply

def fail_in_session(bus, exc, node=None) -> dict:
    """失败路径：classify error_kind + emit workflow_failed + 返 {done:True, error_kind, reason}。"""
    error_kind = getattr(exc, "error_kind", None) or "internal_error"
    t, d = make_workflow_failed(error_kind, str(exc), node=node)   # tape data.kind = error_kind 值
    try: await bus.emit(t, d)
    except Exception: logger.exception("emit workflow_failed 也失败")
    return {"done": True, "error_kind": error_kind, "reason": f"failed: {exc}"}
```

**副作用边界（spec-reviewer issue8，钉死）**：helper **只做 emit + 返信封**。marker 清理 / echo / exit 归调用方，顺序 `emit → clear_marker → echo → exit(1)`（daemon 无 marker 文件，只 emit→返信封）。

### 4.2 daemon.py
- `next()`（L116-141）：删逐条循环 L129-130，改 `reply = await apply_step_result(self.bus, result)`；`except InSessionError: return await fail_in_session(self.bus, e)`。
- 删 `_fail`（L143-152）的 isinstance 分流（被 `fail_in_session` 取代）。
- 删撒谎注释「原子批量 emit（反例 A 消除）」。

### 4.3 cli.py
- next/bootstrap 失败路径：改调 `fail_in_session`；信封加 `error_kind`。
- 成功路径 emit：改调 `apply_step_result`（吸 `_emits_to_event_datas`，删 cli 内联）。
- `_classify_in_session_error` 若被 `fail_in_session` 内聚则删（或留 helper 内部）。
- **marker RMW（clear_marker）保留在 cli 调用方**（helper 不碰）。

### 4.4 信封契约（§2.3 已回写 SPEC）
- 失败信封 = `{done:True, error_kind:<kind>, reason:"failed: <msg>"}`。
- **信封字段 `error_kind`（新）；tape event data 字段 `kind`（lifecycle 不变）。两者同值。**（spec-reviewer 字段陷阱 B4/B7）

### 4.5 不在范围
- 不动 ErrorKind / orchestrator 错误路径 / lifecycle.make_workflow_failed / EventBus（emit_batch 已存在，daemon 直接用）。MCP 全部。跨阶段 `kind` 值集 debt（登记 CURRENT，不改）。

---

## 5. 测试（spec-reviewer issue5/6）

### 5.1 前置：`tests/iface/in_session/test_daemon.py`（InSessionDaemon 当前**零覆盖**）
- tmp_path 构造 daemon（`InSessionDaemon(wf, tape_path, run_id)`）→ `observe(畸形 output)` → `await daemon.next()` → 断言：
  - (a) tape 末条 `workflow_failed` 的 `data["kind"] == "output_schema_mismatch"`（**字段名 `kind`，值断言**）
  - (b) `next()` 返回 `reply["error_kind"] == "output_schema_mismatch"`（信封新字段）
  - (c) `reply` 与 `data` 中**均不得出现** `"in_session_error"` 字面量（反向断言，塌缩消除）
- 成功路径：observe 正常 output → next → emit_batch 落多条 event → done。

### 5.2 cli 信封
- next/bootstrap 失败：断言 `reply["error_kind"]` **值**（非 key 存在）；反向断言无 "in_session_error"。

### 5.3 batch emit 原子性（可选，若易构造）
- daemon next 多 emit 时，emit_batch 单次 write（bus.py:187 tape.append_batch）—— 单测断言调用 emit_batch 而非逐条 emit（mock/spy bus）。

### 5.4 守门
- daemon/cli 失败路径 grep `"in_session_error"` = 0。

---

## 6. E2E（test-agent 真机）

故意失败 run（output 畸形 → `output_schema_mismatch`）：
- **daemon 路**：`InSessionDaemon`（或既可达入口）→ observe 畸形 → next → tape `workflow_failed.data.kind == output_schema_mismatch` + 信封 `error_kind` + 无 "in_session_error"。
- **cli 路**：`orca next --run-id <id> --output '<畸形>'` → stdout JSON `error_kind` + exit 1（或失败 exit）+ tape kind 正确。

---

## 7. SPEC 回写（spec-reviewer B6，已做）
- §7.5：`错误信封×3` → `×2（daemon+cli）` + MCP 排除说明（8 tool 全 ErrorKind，不产 InSessionError）。
- §2.3：返回契约表 next/bootstrap 失败行加 `error_kind` 字段。

---

## 8. 风险 / scope 纪律
- **R1**：helper 副作用边界（只 emit+返信封）须钉死，marker/echo/exit 归调用方（issue8）。
- **R2**：字段名 `kind`(tape) vs `error_kind`(信封) 别写错（B4/B7）—— coder + code-reviewer 把关。
- **debt 登记**：tape `kind` 两值集共享（ErrorKind + error_kind），跨阶段，5b 不解决，记 CURRENT。
- **scope**：不重构 EventBus / 不合并 taxonomy / 不动 MCP / 不动 orchestrator。

---

## 流程闭环
本计划（已重写过 spec-reviewer）→ **coder-agent**（实现 + code-reviewer + 单测 incl. test_daemon.py + commit + 状态文档）→ **test-agent** 真机 E2E（故意失败 run，daemon+cli 两路 kind/error_kind）。

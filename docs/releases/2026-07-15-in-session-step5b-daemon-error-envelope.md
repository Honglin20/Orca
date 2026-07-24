# Release: in-session v5 §8 step 5b —— daemon batch emit + in-session 错误信封统一（×2）

**日期**: 2026-07-15
**Spec**: [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5 §7.5（×3→×2 + MCP 排除）/ §2.3（信封加 error_kind）/ §8 step 5b
**Plan**: [`docs/plans/2026-07-15-in-session-step5b-daemon-error-envelope.md`](../plans/2026-07-15-in-session-step5b-daemon-error-envelope.md)（spec-reviewer CONDITIONAL-PASS，B1-B7 闭环；逐字执行 §4/§5）
**Branch**: `in-session-unified-backend`
**Commit**: `<本 commit，single-commit；SHA 见 git log / CHANGELOG>`
**前置**: step 5a `bce29f8` + FU-1 已 DONE

## 做了什么

SPEC §7.5 两件事，**经 spec-reviewer 实读裁定后定稿**（Q1 batch emit 真活 / Q2 MCP 出 scope / Q3 helper 两函数）：

### 1. daemon batch emit（spec-reviewer Q1「真活」裁定）

`daemon.next()` 旧逐条 emit（`for emit in result.emits: await self.bus.emit(...)`），紧邻的注释「原子批量 emit（反例 A 消除）」**为假**——逐条 emit 的 N 与 N+1 之间，`_on_signal → cleanup → bus.close → sys.exit`（SIGTERM）会落进窗口，N 已 flush、N+1 永不发 → resume 见 `workflow_started` 无 running node → `next()` raise `state_corrupt`。**声称不成立的正确性属性本身就是 blocker（铁律 12）**。

修：daemon 改用 `apply_step_result` → `bus.emit_batch`（单次 write 原子化整批，与 cli.py `next`/`bootstrap` 同模式）。撒谎注释删除。

### 2. in-session 错误信封统一（×2：daemon + cli；MCP 出 scope）

- **daemon `_fail` 缺口（spec-reviewer issue1）**：旧 `error_type = "in_session_error" if isinstance(exc, InSessionError) else "internal_error"` —— isinstance 二分**完全不读 `exc.error_kind`**，`output_schema_mismatch`/`state_corrupt`/`render_error` 等具体分类**全塌缩成一个粗粒度值**，tape 失败分类丢精度。
- **统一**：daemon + cli 两路 in-session 失败信封都改读 `InSessionError.error_kind`（SSOT，单一分类轴），经单一 helper `fail_in_session`。
- **MCP 出 scope（spec-reviewer Q2 裁定）**：8 tool 全用 phase-11 `ErrorKind` 轴（编排 run 层），不产 `InSessionError`、不扫 in-session tape（in-session CLI/daemon 独立进程）。SPEC §7.5 已回写 ×3→×2。

### 字段名契约（spec-reviewer B4/B7 陷阱，coder + code-reviewer 双查）

- **tape event data 字段 = `kind`**（`lifecycle.make_workflow_failed` 写，**不变**）。
- **信封新字段 = `error_kind`**。两者携带**同一值**（`InSessionError.error_kind`），字段名不同。
  - `fail_in_session`：`make_workflow_failed(error_kind, ...)` → tape `data["kind"] = error_kind`；返 `{"error_kind": error_kind}`。
  - 测试分别断言 `data["kind"]` 与 `reply["error_kind"]`。

### 副作用边界（spec-reviewer issue8，钉死）

helper **只做 emit + 返信封**。marker 清理 / echo / exit 归调用方：
- cli 顺序 `fail_in_session → clear_marker → echo → exit(1)`。
- daemon 无 marker，只 `fail_in_session → 返信封`。

## 改动清单

### 新增 `orca/iface/in_session/_step_io.py`（共享 IO 边界 helper）

四函数（daemon/cli 共用，DRY + 单一分类轴）：
- `_classify_in_session_error(exc)`：`getattr(exc, "error_kind", None) or "internal_error"`（用 getattr 非 isinstance——对任意异常给出结构化分类，如 OSError → `internal_error`）。
- `_emits_to_event_datas(emits)`：吸收自原 cli.py 内联（`list[Emit]` → `emit_batch` 入参形态）。
- `apply_step_result(bus, result) -> dict`：成功路径 `emit_batch` + 构造 `{done, node?, prompt?, reason?}`。
- `_emit_workflow_failed(bus, error_kind, message, node)`：落 `workflow_failed` 终态（吞错仅 log）。
- `fail_in_session(bus, exc, node) -> dict`：失败路径 `_classify_in_session_error` + `_emit_workflow_failed` + 返 `{done:True, error_kind, reason:"failed: ..."}`。

依赖单向：仅 `events.bus` + `run.lifecycle`（iface 调 run/events，铁律不破）。

### `orca/iface/in_session/daemon.py`

- `next()`：删逐条 emit 循环 + 撒谎注释，改 `reply = await apply_step_result(self.bus, result)`；`except InSessionError: return await fail_in_session(self.bus, e)`。
- 删 `_fail`（isinstance 塌缩消除）。
- import：`make_workflow_failed` 移除（不再用），加 `apply_step_result` / `fail_in_session`。

### `orca/iface/in_session/cli.py`

- bootstrap/next `except InSessionError`：改调 `fail_in_session`；信封经 helper 返，自动含 `error_kind` 字段（SPEC §2.3）。
- `_advance_and_emit`（bootstrap 成功 emit）/ `_next_in_critical_section`（next 成功 emit）：改调 `apply_step_result`。
- 删内联 `_classify_in_session_error` / `_emits_to_event_datas` / `_emit_workflow_failed`（移到 helper，单一来源）。
- **marker RMW（`clear_marker`）保留在调用方**（helper 不碰）。
- marker 写失败（OSError）路径：保留 informative reason + 加 `error_kind: "internal_error"`（信封形态一致）。
- 合规计数路径（`subagent_compliance`，字面 error_kind 非 InSessionException）：仍用 `_emit_workflow_failed`（从 helper import），不经 `fail_in_session`——分类轴不同，不强行统一。

## spec-reviewer 三个 Q 裁定（已闭环）

| Q | 裁定 | 落实 |
|---|---|---|
| Q1 batch emit | **真活**。注释「反例 A 消除」为假。 | daemon 改 `emit_batch`，注释删。 |
| Q2 mcp | **出 scope**。8 tool 全 ErrorKind，不产 InSessionError。 | SPEC §7.5 ×3→×2。 |
| Q3 helper 落点 | `iface/in_session/`（新模块 `_step_io.py`）。两函数边界。 | 四函数落 `_step_io.py`。 |

## 测试（spec-reviewer issue5/6：InSessionDaemon 零覆盖补齐）

### 新增 `tests/iface/in_session/test_daemon.py`（从 0 → 5 测试）

InSessionDaemon 此前**零覆盖**。新建脚手架：
1. **成功路径**：bootstrap → observe(output) → next → completed；tape 事件序列 `[ws, ns, nc, rt, wc]`。
2. **batch emit spy**（Q1 守门）：spy `bus.emit_batch` / `bus.emit`，断言 bootstrap 触发 `emit_batch` 一次（2 items）、`emit` 零次（成功路径走 batch，非逐条）。
3. **失败信封 + 字段名**（B4/B7）：observe 畸形 output → next → 断言 tape 末条 `data["kind"] == "output_schema_mismatch"`（字段名 kind）+ `reply["error_kind"] == "output_schema_mismatch"`（信封新字段）+ `reply`/`data` **均不得出现** `"in_session_error"` 字面量（反向断言，塌缩消除）。
4. **终态幂等**：completed 后再 next → `{done, reason:"already_completed"}`，不 emit。
5. **非终态幂等 replay**（Round 2 m2）：observe(None) → next → branch 4（emits=[]）→ `apply_step_result` 调 `emit_batch([])` no-op，tape 不增 + 重发同节点 prompt。守门 5b 新 helper 的空 emits 处理。

### `tests/iface/in_session/test_in_session_cli.py`

- next/bootstrap 失败信封加 `reply["error_kind"]` **值**断言（output_schema_mismatch / render_error / unsupported_node_kind）+ 反向 `assert "in_session_error" not in json.dumps(reply)`。
- `_classify_in_session_error` import 从 cli 改到 `_step_io`（函数移到 helper，单测直接守门 helper）。
- **拆分 `test_failure_render_error_clears_marker`**（Round 2 m1）：此前 render_error 测试体被误并入 `test_failure_output_schema_malformed`（裸字符串表达式分隔，`def` 行缺失，pre-existing），拆为独立测试——两者 yaml/run/断言皆独立，合并会误导诊断方向。

## 验证

### 单测
- `tests/iface/in_session/` + `tests/run/`：**348 passed 0 回归**（daemon 5 + cli 拆分净增）。
- 更广 `tests/iface/` + `tests/run/` + `tests/compile/`：1121 passed，7 failed **全 pre-existing**（6 `uv` not found env-blocked + 1 `test_bg_run_ps_logs_wait_e2e` 自 step 1 `run→teams` 搬迁后 `orca run` 派发失效——stash 对比 clean 5a HEAD 复现，**非本步引入**）。
- **守门 grep**：`"in_session_error"` 字面量在 daemon/cli/_step_io 源码路径 = **0**（塌缩值消除）。

### code-reviewer
两轮审计（代码 + 测试覆盖）。

**Round 1（代码）**：**0 BLOCKER**。三大高危（字段名 trap `kind`/`error_kind` 不互换 / 副作用边界 helper 只 emit+返信封 / 原子 batch emit）逐项通过。
- **M1（disputed，不采纳为回归）**：reviewer 担 daemon `except InSessionError` 窄捕获丢失无头兜底。**经 `git show HEAD:orca/iface/in_session/daemon.py` 核验：原 daemon.next 同为 `except InSessionError` 窄捕获**（`_fail` 的 `else "internal_error"` 分支是死代码）——本步保持相同捕获宽度，**无回归**。reviewer 的「无头 daemon 是否应宽捕获兜底」是**独立 follow-up**（crash 时 emit workflow_failed 避免留腐败 tape，比 stderr 崩溃更 fail loud），plan §4.2 明定窄捕获，本步不扩 scope。已登记 CURRENT。
- **m1（FIXED）**：`_emit_workflow_failed` 入口 `logger.exception` 在合规计数正常流路径（无 active exception）产 `NoneType: None` 假栈 → 改 `logger.warning`（带 error_kind + message）；inner except 仍 `logger.exception` 记真栈。
- **m2（FIXED）**：`_classify_in_session_error` docstring「对任意异常都给出结构化分类」over-promise（当前调用点均窄捕获 InSessionError）→ 收紧为部署边界说明 + 登记 headless 宽捕获 follow-up。

**Round 2（测试覆盖）**：**0 BLOCKER / 0 MAJOR**。高频 Bug 守门逐条通过（字段名 trap `kind`/`error_kind` 双向守门、反向 `in_session_error` gate、batch emit spy 经源码验证能在回退逐条 emit 时必然失败、daemon 零覆盖关闭）。三个 MINOR 全闭环：
- **m1（FIXED）**：`test_failure_output_schema_malformed` 内嵌 render_error 测试（pre-existing `def` 行缺失）→ 拆为独立 `test_failure_render_error_clears_marker`。
- **m2（FIXED）**：daemon 非终态幂等 replay（emits=[]）补测 → 守门 `apply_step_result` 的 `emit_batch([])` no-op。
- **m3（registered，out of scope）**：合规计数失败信封缺 `error_kind`（plan §1.1 明定合规不经 InSessionError，独立分类轴）→ 登记为一致性 follow-up，不阻塞 5b。

### test-agent 真机 E2E（待跑，不阻塞本步）
故意失败 run（output 畸形 → `output_schema_mismatch`）：daemon + cli 两路都 emit 正确 kind 到 tape + 信封含 `error_kind` + 无 `in_session_error`。

## 跨阶段 debt（登记 CURRENT，本步不解决）

**tape `workflow_failed.data.kind` 是 ErrorKind / error_kind 两值集的共享字段**：
- `InSessionError.error_kind`（in-session 编排层：`internal_error`/`state_corrupt`/`output_schema_mismatch`/`render_error`/...）。
- `ExecError.kind`（phase-11 `ErrorKind` 枚举，executor 层：`TRANSPORT_*`/`PROTOCOL_*`/`BUSINESS_*`）。
- `lifecycle.make_workflow_failed` 对所有 `workflow_failed` 写同一个 `data.kind` 字段——orchestrator 经 `_classify_error` 写 ErrorKind 值，daemon/cli 写 error_kind 值。

**5b 处理**：只统一 in-session 两个信封的 `kind` 值**来源**（都读 `exc.error_kind`）；**不动 ErrorKind 值、不加字段区分两值集**（跨阶段 debt，登记 CURRENT）。措辞严守「统一来源」非「tape 层合并值集」。

## 与计划的一致性

逐字执行 §4（改动范围）/ §5（测试）/ §8（风险）。无偏差。`_emit_workflow_failed` 抽到 helper（计划允许「或留 helper 内部」），让 `fail_in_session` + cli 合规路径共用——比计划伪代码的内联 try/except 更 DRY。

## 已知 follow-up（非本步）
- **test-agent 真机 E2E**（5b daemon+cli 两路 kind/error_kind）。
- **无头 daemon 宽捕获兜底**（code-reviewer Round 1 M1 引出）：daemon.next 当前 `except InSessionError` 窄捕获（与原行为一致，非回归）；无头 CI 场景下 advance_step 契约外 bug（如 KeyError）会 crash 留腐败 tape。可考虑改 `except Exception → fail_in_session`（emit workflow_failed 比无人读的 stderr 更 fail loud）。独立决策，不在 5b scope。
- **step 3a / 3b / 6 / FU-2 / FU-3**：见 CURRENT。
- **跨阶段 `kind` 两值集 debt**（见上节，登记 CURRENT）。
- **CLI test 结构瑕疵**（pre-existing，Round 2 m1 已顺手修）：`test_failure_output_schema_malformed` 内 `def test_failure_render_error` 定义行缺失——5b 拆为独立测试。
- **合规计数失败信封 `error_kind`**（Round 2 m3，一致性观察）：cli 合规计数路径（`subagent_compliance`，字面 error_kind 非 InSessionError）reply 不含 `error_kind`，与 InSessionException 失败信封形态有张力。plan §1.1 明定合规独立分类轴（不经 InSessionException），5b 不统一；如未来要对齐需同时改实现（cli reply 补 `error_kind`）+ 测试。登记 CURRENT。

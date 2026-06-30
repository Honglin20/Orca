# 2026-07-01 Tape 写句柄惰性打开（消除 ResourceWarning）

## 背景

终验阶段发现 `tests/run/` 跑 ~30 条 `ResourceWarning: unclosed file`，根因在
`orca/events/tape.py::Tape.__init__` —— **eager-open**：构造即 `open(path, "a", ...)`，
即便该 Tape 仅用于只读（replay / inspect）。

`_fh` 实际只被 `append()` 与 `close()` 触碰；`replay()` / `_truncate_trailing_partial`
/ `_scan_last_seq` 都用独立只读句柄。所以测试中 `Tape(path).replay()`（如
`tests/run/test_demo_integration.py` 的 `_completed_outputs` / `_failed_event` / `_event_types`
helper、以及 `tests/run/conftest.py::make_bus` 构造后 append 但不显式 close 的用例）会开一个永不用、永不关的 append 句柄 → GC 时报 `ResourceWarning`。

`EventBus.close() → Tape.close()` 的生产路径是正确的（`run_workflow` 终态后必跑）；漏的是只读构造 + 测试遗忘 close。但 eager-open 本身就是设计 smell —— 任何未来调用方（CLI `orca replay <id>` / inspect 工具）都会泄漏一个写句柄。

## 改动

### `orca/events/tape.py`（root-cause fix）

- `__init__`：`self._fh = None`（不再 eager-open）。resume / 续写重算 `_last_seq` 的逻辑不变（用 `read_text`/`write_text`，不碰 `_fh`）。
- `append()`：在既有 `async with self._lock` 块内、`_closed` guard 之后、seq 分配之前惰性 open —— `if self._fh is None: self._fh = open(self.path, "a", ...)`。锁内保证并发 append 不双开（race-free）。
- `close()`：guard `if self._fh is not None`（只读 Tape 从未 append 时也能幂等干净关闭）。
- `__del__`：leak 安全网 —— 兜底那些忘显式 close 的调用方（与 Python 内建 `open()` 对象自带 `__del__` 行为一致）。`try/except` 兜 GC 期异常不抛；不 mask 运行错误（生产走显式 close，运行错误已在 emit 侧 fail loud）。

### `tests/events/test_tape.py`（4 个新白盒测试 + 1 行增强断言）

- `test_readonly_construction_does_not_open_write_handle`：构造 + replay + `last_seq` 后 `_fh` 全程 None；`weakref` + `gc.collect` + `simplefilter("error", ResourceWarning)` 钉死「GC 无泄漏」。
- `test_append_opens_write_handle_lazily`：构造时 `_fh is None`；首次 append 后非 None；第二次 append 复用同一句柄（`fh_after_first is tape._fh`）；close 后 append 仍 fail loud。
- `test_resume_does_not_open_write_handle_until_append`：resume 截断（`write_text`）后 `_fh` 仍 None；首次 append 才开。
- `test_lazy_open_failure_is_fail_loud`：monkeypatch `builtins.open` 抛 `OSError` → append 仍 fail loud（不吞）+ `_fh` 保持 None 可重试。
- 增强 `test_replay_preserves_order`：非 resume 重开路径（`_scan_last_seq → replay`）显式断 `_fh is None`。

### `tests/gates/test_hook_bridge.py`（顺带修，9 处）

验收门 `-W "error::ResourceWarning"` 全绿时发现 9 条 `<socket.socket>` 泄漏 ——
`ThreadingHTTPServer` 测试只 `server.shutdown()`（停 `serve_forever` 循环）不 `server.server_close()`（才真正关监听 socket）。顺带补 `server.server_close()`（9 处 `finally`）。**与本任务不同模块**（phase 6 gate 桥测试），但同属 ResourceWarning 卫生类、trivial 加 1 行、不触逻辑 —— 不修则验收门无法转绿，故一并修。

## 偍离计划

无。逐字按 brief「root-cause surgical」执行。唯一偏离：brief 只点名 Tape 惰性打开，未预见 `tests/gates/` 的 socket 泄漏（不同根因、不同 phase）—— Rule 7 surface：选「一并修」，理由是 (1) 同属 ResourceWarning 卫生 (2) trivial (3) 验收门要求零 ResourceWarning。

## 验证

- `uv run pytest -q -m "not integration" -W "error::ResourceWarning"`：**0 ResourceWarning，599 passed**（修复前 30 条 file warning）。
- `uv run pytest -q -m "not integration" -W "error::RuntimeWarning"`：0 RuntimeWarning，599 passed（无回归）。
- `uv run pytest -q -m "not integration"`：599 passed，35 deselected（phase 1-9 零回归）。
- `npm test`（vitest）：84 passed（前端不受影响，确认无破坏）。
- `tests/events/test_tape.py`：20 passed（16 既有 + 4 新）。

## Commit

- `fix(events): Tape 写句柄惰性打开 —— 只读构造（replay/inspect）不再泄漏未关闭的 append handle`（master）

## Review

`code-reviewer` 8/8 验收清单全过（race-free / close 幂等 / resume 不受影响 / fail-loud 保持 / seq 不变量保持 / `__del__` GC 安全不 mask 运行错 / 测试验 intent / 项目规约）。2 建议（open 失败 fail-loud 测试 / 非 resume 重开路径显式断言）+ 1 docstring 清理 —— 全修。

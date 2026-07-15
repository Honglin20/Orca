# Plan: FU-1 —— `orca stop`/`open` 加 `--run-id` option（DEFECT-2 MINOR#1，test-agent 真机复现）

> 来源：DEFECT-2 review MINOR#1（CURRENT.md 记录）+ 2026-07-15 test-agent headless skill E2E **真机复现**（用户面缺陷）
> 状态：spec-reviewer CONDITIONAL-PASS，已按 ISSUE-1..5 修订（stop 必填→可选+None 守卫 / open 同改 / 抽 `_merge_run_id` helper / 测试按 stop 破坏性重写）| 分支 `in-session-unified-backend` | 前置：5a 已 commit `bce29f8`
> 模板：DEFECT-2 commit `e763e9e`（`orca status` 加 `--run-id`）—— 已 review 通过的成熟模式，本步逐字套用

---

## 0. 目标与成功标准

`orca` in-session 命令族 `--run-id` 形态**全统一**（§2.1 单一接口）：`next`/`status`/`stop`/`open` 都接受 `--run-id <id>` option（`status`/`next` 已是，`open` 待核实，`stop` 确认缺失）。

**成功标准**：
1. `orca stop --run-id <id>` 真实工作（exit 0，停 run + 清 marker），不再 `Error: No such option: --run-id`。
2. `stop`（及 `open` 若也偏）同时保留**位置参数兼容**（`orca stop <id>` 仍可用）—— 向后兼容既有调用 / 测试 / skill 位置形式。
3. 位置参数与 `--run-id` **同传同值 → 容错**；**同传异值 → `typer.BadParameter` fail loud**（铁律 12，与 DEFECT-2 status 一致）。
4. SKILL.md / CLI help / 模块 docstring 三处 `--run-id` 形态一致（skill 已教 `--run-id`，本步让 CLI 对齐，非改 skill）。
5. 单测：mirror DEFECT-2 的 5 个 status 测试（option mirror / json / 同值容错 / 异值 fail loud / 不存在 run 错误路径）→ stop（+open 若改）。
6. test-agent 真机：`orca stop --run-id <真实 run_id>` 真停 + 清 marker + exit 0；异值 fail loud。

---

## 1. 根因与证据（test-agent 真机复现）

- **症状**：`orca stop --run-id <id>` → `Error: No such option: --run-id`（exit ≠ 0）。
- **根因**：`orca/iface/in_session/cli.py:774-776` `stop` 命令只用位置参数 `run_id: str = typer.Argument(...)`，无 `--run-id` option。
- **对比**（同文件，spec-reviewer 实读核实）：`next`（:499）= `--run-id` option 必填；`status`（:699-706）= 位置参数 + `--run-id`（DEFECT-2 已修）。**`stop`（:776）和 `open`（:1019）都只有位置参数、都缺 `--run-id`**——DEFECT-2 commit message 已记「stop/open 同型」，二者同属本 FU。（test-agent 早先「open 已 --run-id」结论错误——它只测了 stop 就外推；spec-reviewer 实读 cli.py:1019 纠正：open 只有位置参数 + `--tape/--host/--port`。）
- **契约矛盾**：`orca/skills/orca/SKILL.md:28` + `:127` 两处教用户 `orca stop --run-id <id>`。用户/LLM 照 skill 跑必失败。
- **为何单测全绿**：所有 stop 测试用位置形式 `["stop", run_id]`（`tests/iface/in_session/test_in_session_cli.py:481,590` / `test_in_session_v8.py:455,517`），**无一**测 `["stop","--run-id",run_id]` —— 正是 skill→CLI 契约缝，mocked/positional 测试覆盖不到（test-agent 真机才暴露）。

---

## 2. 改前架构审视

- **单一接口（§2.1）**：`--run-id` 是命令族统一形态。`stop` 缺它 = 接口面不一致。修它 = 接口归一，非新增能力。
- **改前影响**：`stop` 签名加 option 是**增量**（位置参数保留），非 breaking。既有 `orca stop <id>` 调用 / 测试全兼容。
- **改后清理**：`stop` 模块 docstring / help 文案统一 `--run-id` 形态（与 status 一致）。
- **依赖**：纯 iface/cli 层，零跨层影响。

---

## 3. 改动范围

### 3.1 `orca/iface/in_session/cli.py` `stop`（核心，**注意 stop 起点与 status 不同** —— spec-reviewer ISSUE-1）

⚠️ **不能「逐字复制 status」**：status 位置参数改动**前**就是可选 `Argument(None)`，DEFECT-2 只加 option；stop 位置参数是**必填** `Argument(...)`。若不改必填性，`stop --run-id X` 仍会因「缺位置参数」exit 2（已实验验证）= FU-1 目标未达成。

改动：
1. **位置参数 `typer.Argument(...)` → `typer.Argument(None, ...)`**（必填→可选）。
2. 加 `run_id_opt = typer.Option(None, "--run-id", ...)`。
3. 合流：`rid = _merge_run_id(run_id, run_id_opt)`（见 §3.3 helper，同值容错 / 异值 BadParameter）。
4. **None 守卫**：`if rid is None: raise typer.BadParameter("stop 需指定 run_id：用 --run-id 或位置参数")`。status 的 None 分支是「列全部 run」，**stop 无此模式，None 必须 fail loud**（保 `test_stop_missing_run_id_fails_loud` exit 2，spec-reviewer ISSUE-3）。
5. docstring + help 文案统一 `--run-id` 形态。

### 3.2 `orca/iface/in_session/cli.py` `open`（spec-reviewer ISSUE-2：同样缺 `--run-id`，必改）
- `open`（cli.py:1019）**只有位置参数 + `--tape/--host/--port`，无 `--run-id`**（实读确认）。
- 位置参数本就是可选 `None`（open 的 None = 取活跃 run，合理默认）→ 比 stop 简单：加 `run_id_opt` option + `rid = _merge_run_id(...)`，**在既有 `if run_id is None: run_id = _default_active_run_id()` 之前**插入合流。open 的 None **不 fail loud**（走活跃 run 默认）。

### 3.3 抽 `_merge_run_id` helper（spec-reviewer ISSUE-5，DRY）
合流逻辑（同值容错 / 异值 BadParameter）改动后出现于 **status + stop + open = 3 处**，触发 CLAUDE.md DRY「禁止三处以上重复」。抽：
```python
def _merge_run_id(run_id: str | None, run_id_opt: str | None) -> str | None:
    """位置参数与 --run-id option 合流：同值容错 / 异值 BadParameter / 都空返 None（调用方按语义处理）。"""
    if run_id is not None and run_id_opt is not None and run_id != run_id_opt:
        raise typer.BadParameter(f"位置参数 {run_id} 与 --run-id {run_id_opt} 冲突")
    return run_id if run_id is not None else run_id_opt
```
status/stop/open 各调一次，None 语义各自处理（status→列全部 / stop→fail loud / open→活跃 run 默认）。**同时把 status 既有内联合流替换为调 helper**（消除第 1 处重复）。helper 独立单测——是 ISSUE-1 那类「看似同实则起点不同」错误的天然防线。

### 3.4 无 schema/compile/exec/run 改动（纯 CLI 层）。

---

## 4. 测试（spec-reviewer ISSUE-3/4：stop 非字面 mirror status）

stop 是**破坏性**（首次 stop 清 marker + emit cancelled；同 run 再 stop 非幂等），status 是只读——不能套 status 的「同 run 调两次 byte-equal」equivalence。且 stop **无 --json**；stop 的 nonexistent run 是**幂等 ok（exit 0）非 fail-loud（exit 1）**。

- **stop 测试集**（两独立 run 证同构）：
  1. 两个独立 run 各停一次（一 positional 一 `--run-id`），断言**同构 observable**（ok/done/marker 清/cancelled）。
  2. 位置形式回归（已有 v8.py:511-523，保留）。
  3. 同值容错（`stop <id> --run-id <同id>`）。
  4. 异值 BadParameter（`stop <a> --run-id <b>`）。
  5. **`test_stop_missing_run_id_fails_loud`（v8.py:527）exit 2 经显式 None 守卫保留 + docstring 更新**（去「必填位置参数」措辞，改「None 守卫」）。
  6. nonexistent `--run-id` 走幂等 ok/no-tape（v8.py:535 的 option 变体，exit 0）。
- **open 测试**：`open --run-id <id>` 形式 + 位置回归 + 同值/异值合流。
- **helper 单测**：`_merge_run_id` 同值/异值/都空/单边各分支。
- **守门**：新增至少一个 `["stop","--run-id",run_id]` 形式测试（补上原覆盖缝）。

---

## 5. E2E（test-agent 真机）

- 起一个真实 run（`orca demo_insession --inputs '{...}'`）→ 拿 run_id。
- `orca stop --run-id <run_id>` → 真停（marker 清除 + status 反映 stopped/done）+ exit 0。
- 异值 `--run-id` fail loud 真机验证。
- `open --run-id` 若改了也真机验。

---

## 6. 风险 / scope 纪律

- **R1（已决策，spec-reviewer ISSUE-5）**：合流逻辑达 3 处 → 抽 `_merge_run_id` helper（§3.3），status/stop/open 共用 + status 内联替换 + 独立单测。**不逐字复制**（ISSUE-1 已证「逐字复制」在不同起点下埋 bug）。
- **scope**：**不动** `status` 无参列表契约漂移（test-agent 另观察款，记独立 follow-up FU-3）。**不动** next/doctor/list。只统一 stop（+open 核实）的 `--run-id`。

---

## 7. 顺带（同 commit housekeeping，非 FU-1 本体）
回填 step 5a 状态文档的 commit SHA 占位符为 `bce29f8`（release note L7 / CHANGELOG 5a 条目 / CURRENT 5a 行）—— 5a 单 commit 无法自引用，本 commit（不同 SHA）回填，匹配仓库 `6f0d87f` 回填先例。

---

## 流程闭环
本计划 → **spec-reviewer**（小计划，快速）→ **coder-agent**（套 e763e9e 实现 + code-reviewer + 单测 + commit + 状态文档 + 5a SHA 回填）→ **test-agent** 真机（`stop --run-id` 真停 + 异值 fail loud）。

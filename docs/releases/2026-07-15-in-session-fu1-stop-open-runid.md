# Release: in-session FU-1 —— `orca stop`/`open` 加 `--run-id` option（命令族统一，套 DEFECT-2 e763e9e）

**日期**: 2026-07-15
**Spec**: [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) §2.1
**Plan**: [`docs/plans/2026-07-15-in-session-fu1-stop-open-runid.md`](../plans/2026-07-15-in-session-fu1-stop-open-runid.md)（spec-reviewer CONDITIONAL-PASS，ISSUE-1..5 闭环）
**Branch**: `in-session-unified-backend`
**Commit**: `<本 commit，single-commit；SHA 见 git log / CHANGELOG>`
**模板**: DEFECT-2 `e763e9e`（`orca status` 加 `--run-id`，已 review 通过的成熟模式）
**前置**: step 5a `bce29f8` 已 DONE

## 做了什么

`orca stop`/`open` 与 `status`/`next` 命令族 `--run-id` 形态**全统一**（SPEC §2.1 单一接口）。
test-agent headless skill E2E **真机复现**：`orca stop --run-id <id>` 报 `No such option: --run-id`（SKILL.md:28/127 + SPEC §2.1 都教 `--run-id`，但 stop/open CLI 只有位置参数）。

具体改动（纯 CLI 层，零跨层影响）：

1. **抽 `_merge_run_id` helper**（`cli.py:699`，ISSUE-5 DRY）——位置参数与 `--run-id` option 合流：
   同值容错 / 异值 `BadParameter` fail loud / 都空返 `None`。**None 语义由调用方按命令分别处理**。
   status/stop/open 三命令共用，消除原 status 内联合流（防三处漂移）。
2. **`stop`（`cli.py:784`，ISSUE-1 关键）**——位置参数 `typer.Argument(...)` 必填→可选 `Argument(None)`
   （不改则 `stop --run-id X` 仍 exit 2「Missing argument」，已实验验证）+ `--run-id` option + `rid = _merge_run_id(...)`
   + **None 守卫** `raise BadParameter`（stop 无 status 的「无参列全部」模式，None 必须 fail loud，保 exit 2 回归，ISSUE-3）。
3. **`open`（`cli.py:1046`，ISSUE-2）**——加 `--run-id` option + `rid = _merge_run_id(...)`，在既有
   `if run_id is None: run_id = _default_active_run_id()` **之前**插合流。open 的 None **不 fail loud**（走活跃 run 默认）。
4. **`status`（`cli.py:713`）**——既有内联合流替换为调 `_merge_run_id`（消除第 1 处重复，行为不变）。
5. 模块 docstring：`stop`/`open` 行统一 `--run-id` 形态（与 status 一致）。

### 为何不能「逐字复制 status」（spec-reviewer ISSUE-1）

status 位置参数改动**前**就是可选 `Argument(None)`，DEFECT-2 只加 option；stop 位置参数是**必填** `Argument(...)`。
若不改必填性，`stop --run-id X` 仍会因「缺位置参数」exit 2 = FU-1 目标未达成。这是 test-agent 早先
「open 已 --run-id」（实读 cli.py:1019 纠正：open 也缺）同类「看似同实则起点不同」错误——`_merge_run_id` 独立单测是防线。

## 测试

`tests/iface/in_session/test_in_session_cli.py` 新增 FU-1 块（15 测试 passed）：

- **stop**（破坏性，不能套 status 的「同 run 调两次 byte-equal」）：
  - `test_stop_run_id_option_mirrors_positional`——两独立 run 各停一次（positional vs `--run-id`），断言
    observable 同构（ok/done）+ 各自 tape 末尾 `workflow_cancelled` + marker 清。
  - `test_stop_positional_and_option_same_value_ok`——同值容错。
  - `test_stop_positional_and_option_conflict_fails_loud`——异值 BadParameter 含两个冲突值。
  - `test_stop_missing_run_id_fails_loud`——都省略 → exit 2（None 守卫，**守门 stop --run-id 形式**）。
  - `test_stop_run_id_option_nonexistent_is_idempotent_ok`——`--run-id <不存在>` 幂等 ok（note=no-tape，exit 0，与位置参数等价）。
- **open**（mock `_open_run_inproc` 避免真起 web server）：
  - `test_open_run_id_option_routes_to_open_run`——`--run-id` 合流后透传。
  - `test_open_positional_regression`——位置参数向后兼容。
  - `test_open_positional_and_option_same_value_ok`——同值容错（与 stop 对称）。
  - `test_open_positional_and_option_conflict_fails_loud`——异值 fail loud。
  - `test_open_no_run_id_uses_active_default`——都省略 → 取活跃 run 默认（None 不 fail loud）。
- **`_merge_run_id` helper 单测**——both-None / positional-only / option-only / same-value / conflict 全 5 分支。

`tests/iface/in_session/test_in_session_v8.py`：`test_stop_missing_run_id_fails_loud` docstring 更新
（去「必填位置参数」措辞，改「None 守卫」）。

**单测**：`tests/iface/in_session/` 125 passed 0 回归（110 baseline + 15 新增）。

## code-reviewer 两轮结论

- **代码质量**：0 BLOCKER / 0 MAJOR。SRP/OCP/DRY 达标，None 语义三分流逐字对齐计划，`run_id`→`rid`
  重命名无遗漏。🟢 MINOR 两项：① 空串 `run_id`（`stop ""`）绕过 None 守卫——**pre-existing 非 FU-1 引入**，
  纳入会扩张契约面，**决策留独立 follow-up**（见下）；② 两文件同名 `test_stop_missing_run_id_fails_loud`——
  分属不同 charter（薄 CLI 守门 / v8 签名契约），语义正当不合并。
- **测试覆盖**：0 BLOCKER / 0 MAJOR。stop mirror「两独立 run + 四层 observable」是 intent 级正面范例；
  helper 5 分支单测到位；守门缝隙双重锁。🟢 可选：补 open 同值命令面测试（对称性）——**已采纳补上**
  （`test_open_positional_and_option_same_value_ok`）。

## 验证

- 单测：14 FU-1 测试全过；既有 110 测试零回归（positional 形式全保留兼容）。
- smoke：`stop --help` / `open --help` 均列出 `--run-id`；`stop`（无参）exit 2（None 守卫）。
- test-agent 真机 E2E：待跑（`stop --run-id <真实 run_id>` 真停 + 清 marker + exit 0 / 异值 fail loud / `open --run-id`）。

## scope / 偏差

- **不改** `status` 无参列表契约漂移（test-agent 另观察款）——记独立 follow-up **FU-3**。
- **不动** next/doctor/list。只统一 stop（+open）的 `--run-id`。
- 完全按计划 §3/§4 逐字执行，无偏差。

## follow-up

- **FU-3**：`status` 无参列表契约漂移（test-agent 观察），独立收。
- **空串 run_id 边缘情况**：`orca stop ""` / `open ""` 会让 `run_id=""`（非 None）绕过 None 守卫走
  no-tape 幂等分支。**pre-existing**（FU-1 前 `typer.Argument(...)` 同此行为），非回归。code-reviewer
  判定纳入 FU-1 会扩张契约面（需在 helper 或入口加空串校验），**决策留独立 follow-up**，不阻塞本步。
- test-agent 真机 E2E（本 commit 单测覆盖 mocked，真机由主 session 接着派 test-agent）。

## 顺带（housekeeping，同 commit）

回填 step 5a 状态文档 commit SHA 占位符为 `bce29f8`（5a 单 commit 无法自引用，本 commit 回填，匹配仓库 `6f0d87f` 回填先例）：
release note L7 / CHANGELOG 5a 条目末 / CURRENT 5a 行。

# Release: in-session v5 §8 step 3b —— catalog 物理迁 orca/compile/catalog.py（依赖铁律归位）

**日期**: 2026-07-15
**Spec**: [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5 §8 step 3b（B7）/ §2.1（catalog 单一实现）
**Plan**: [`docs/plans/2026-07-15-in-session-step3b-catalog-relocate.md`](../plans/2026-07-15-in-session-step3b-catalog-relocate.md)（spec-reviewer CONDITIONAL-PASS→修订 PASS，逐字执行 §2）
**Branch**: `in-session-unified-backend`
**Commit**: `<本 commit，single-commit；SHA 见 git log / CHANGELOG>`
**前置**: step 5a `bce29f8`（已解锁）

## 做了什么

把 workflow catalog（发现/加载/描述 workflow）从 `orca/iface/mcp/catalog.py` 物理迁到 `orca/compile/catalog.py`，归位依赖层。**纯位置迁移，零逻辑/契约改动**（`git mv` similarity 100%，catalog 7 个公开函数签名与行为不变）。

### 为什么迁 —— 依赖铁律归位

Orca 单向依赖 `schema → compile → exec → run → iface`。catalog 做 workflow 发现/加载（`list_workflows`/`find_workflow`/`describe_workflow`/`find_workflow_by_name`/`find_workflow_yaml_path`），是 **compile 层关注**（它已 `from orca.compile import ConfigurationError, load_workflow` + `from orca.schema.workflow import Workflow`）。它原来坐在 `iface/mcp/` = **iface 提供了一个 compile 层关注**，方向越位。迁到 compile → 与 parser/validator 同层，方向正。

### 循环依赖核实（spec-reviewer 实测 + coder 复核）

catalog.py import 仅：stdlib + `orca.compile`（ConfigurationError, load_workflow）+ `orca.schema.workflow`。**对 iface/run/exec 零 import**。迁入 compile 后：`from orca.compile import ...` 变 intra-package（同包从 __init__ 取），无循环。实测 `python -c "import orca.compile.catalog; import orca.iface.cli.commands; import orca.iface.in_session.cli; import orca.iface.mcp.server"` → 无 ImportError / 无 CircularError。

## 关键设计决策：lazy→顶层 module import（偏离原计划的正当修正）

原计划 §2.2 写「lazy `from orca.iface.mcp.catalog import list_workflows` → 顶层 `from orca.compile.catalog import list_workflows`（裸函数 import）」。实施时发现裸函数 import 会触发两个正确性问题，故**改用 module import** `from orca.compile import catalog` + `catalog.<fn>()` 调用。code-reviewer 两轮均实证此修正成立：

1. **名称碰撞（RecursionError）**：`commands.py` 与 `in_session/cli.py` 各有一个 typer 命令函数**字面名 `list_workflows`**（commands.py:328 / in_session/cli.py:1002，命令体委托 `run_list()`）。若顶层裸 import `list_workflows`，则模块级该名先绑 catalog 函数、随后被 `def list_workflows()` 重绑为 typer 命令 → `run_list()` 调 `list_workflows()` 调到 typer 命令 → 命令又调 `run_list()` → **无限递归**（实测 RecursionError）。原代码靠 lazy aliased import `_catalog_list` 绕开；module import（绑名是 `catalog` 而非 `list_workflows`）干净消除碰撞。
2. **`mock.patch` 语义**：`test_v3_step1.py` 守门「单一 catalog 真相源」契约用 `mock.patch("orca.compile.catalog.list_workflows")`。裸 `from ... import list_workflows` 在 import 期绑函数到消费模块命名空间，patch module 属性**不可见**（patch-where-used 失效）。module import + `catalog.list_workflows()` 在调用时对模块对象做动态属性查找，patch 后即生效（patch-where-defined）。实测 `call_count==2` 断言通过为证。

`monkeypatch.setattr("orca.compile.catalog._workflow_dirs", ...)` 同样 bite：`_workflow_dirs()` 在 `list_workflows`/`find_workflow` 函数体内经模块 `__globals__` 查找。

## 改动清单

### 1. 迁移（`git mv`，保历史）
- `orca/iface/mcp/catalog.py` → `orca/compile/catalog.py`（内容字节级不变，catalog.py 自身 import 不改——`from orca.compile import ...` 在 compile 包内仍合法）。
- `tests/iface/mcp/test_catalog.py` → `tests/compile/test_catalog.py`（import L19 + 2 处 mock target L68/L141 改 `orca.compile.catalog`）。

### 2. 7 处 lazy import → 顶层 module import（3 文件，风格一致）
- `orca/iface/cli/commands.py`：`from orca.compile import ConfigurationError, catalog, load_workflow`（合并同包 import，DRY）；`run_list()` 调 `catalog.list_workflows()`。
- `orca/iface/in_session/cli.py`：同上 import；3 调用点 `catalog.find_workflow_yaml_path()`（_resolve_wf_path）/ `catalog.find_workflow()`（_load_wf_for_run fallback）/ `catalog.list_workflows()`（list 命令）。
- `orca/iface/mcp/server.py`：`from orca.compile import catalog`（置 exec import 前，字母序/分层序一致）；4 调用点 `catalog.list_workflows()` / `catalog.find_workflow_by_name()` / `catalog.describe_workflow()` / `catalog.find_workflow()`。

### 3. 注释/docstring 同步
- `commands.py:348`「catalog 本身在 iface/mcp/catalog.py」→ `orca/compile/catalog.py`。
- `server.py` 模块 docstring 依赖清单：`orca.iface.mcp.{transport, hints, catalog, tape_index}` → 去掉 `catalog`，另列 `orca.compile.catalog`。
- 守门 `grep -rn "iface/mcp/catalog\|iface\.mcp\.catalog" orca/ tests/` = **0**。

### 4. 7 处跨文件 mock target 同步（迁移后旧路径必崩，逐个核）
- `tests/iface/cli/test_commands.py:213, 228`（`_workflow_dirs` ×2）
- `tests/iface/in_session/test_v3_step1.py:501`（`list_workflows` patch）
- `tests/iface/mcp/test_unit_tools.py:109, 157, 176, 209`（`_workflow_dirs` ×4）
- conftest/fixtures：`tests/*/conftest.py` 无引用旧路径（grep 验证），无需改。

## 偏离计划

1. **module import 取代裸函数 import**（见上「关键设计决策」），spec-reviewer 原计划假设裸 import 可行，coder 实施时证伪并改方案，code-reviewer 两轮确认正确。
2. plan §2.2 提及 server.py 可「保 lazy 须 code-reviewer 给理由」——实际全改顶层（与另两文件一致），无需保 lazy。

## 验证

- **单测**：catalog 相关 35 测试全绿（test_catalog 13 + TestListCommand 2 + v3_step1 list/单一 catalog 2 + test_unit_tools 4 monkeypatch 站点覆盖）；`tests/compile/ tests/iface/ tests/run/` 共 **1123 passed / 33 skipped / 7 failed**，7 failed 全 pre-existing env-blocked（6 × `uv` FileNotFoundError + 1 × `test_bg_integration` background），stash 对比 pristine HEAD 同 7 failed 复现 → 零回归。
- **守门 grep** `iface/mcp/catalog` / `iface\.mcp\.catalog` in `orca/` + `tests/` = **0**。
- **循环依赖**：四模块 import 无 ImportError / 无 CircularError。
- **mock 语义**：9/9 mock 站点全部 bite（code-reviewer Round 2 逐站核验，0 stale no-op，0 虚假绿）。
- **code-reviewer 两轮**：Round 1（代码）0 BLOCKER / 0 MAJOR，3 个 🟢 可选（server import 字母序 + 合并同包 import + compile `__all__`）—— 前 2 已修，第 3 跳过（catalog 作为子模块可访问，`__init__` 保「对外极简」哲学，无 star-import 故 `__all__` 纯装饰，YAGNI）。Round 2（测试覆盖）0 BLOCKER，2 个 🟡 预存覆盖缺口（非 3b 回归）→ 登记 follow-up（见下），不内联（保 commit 纯迁移意图 + scope 纪律）。
- **test-agent 真机 E2E**：三路 catalog 消费者（`orca list` / `teams list` / MCP `list_workflows`）返一致结果，待主 session 派 test-agent 跑。

## Follow-up debt（预存，非 3b 引入，登记不内联）

- `_load_wf_for_run` 的 catalog.find_workflow fallback（`in_session/cli.py` 错误恢复分支，老 tape/daemon 无 yaml_path）无测试触达。
- `tool_describe_workflow` 的 found 分支（server 装配层，`catalog.describe_workflow(wf)`）无 server 层测试（函数本身在 test_catalog.py 有直接单测）。

以上两处是 code-reviewer Round 2 指出的预存覆盖缺口，与本次迁移无关（迁移未改这两条路径的逻辑）。按 scope 纪律（plan §4：纯迁移，不动逻辑/契约）登记为独立 follow-up，不并入 3b commit。

## 文件

- `orca/compile/catalog.py`（迁入）
- `orca/iface/cli/commands.py` / `orca/iface/in_session/cli.py` / `orca/iface/mcp/server.py`（import 改向）
- `tests/compile/test_catalog.py`（迁入）/ `tests/iface/cli/test_commands.py` / `tests/iface/in_session/test_v3_step1.py` / `tests/iface/mcp/test_unit_tools.py`（mock target 同步）

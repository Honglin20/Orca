# Plan: in-session v5 §8 step 3b —— catalog 物理迁 orca/compile/catalog.py

> SPEC：[`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5 §8 step 3b（B7，延后到 5a 后）
> 状态：草稿（待 spec-reviewer）| 分支 `in-session-unified-backend` | 前置：5a `bce29f8`（已解锁）

---

## 0. 目标与成功标准

把 workflow catalog（发现/加载/描述 workflow）从 `orca/iface/mcp/catalog.py` 物理迁到 `orca/compile/catalog.py`，归位依赖层（compile），消除「iface 子包延迟 import 同层 catalog」smell。

**成功标准**：
1. `orca/compile/catalog.py` 存在（从 iface/mcp 迁入），`orca/iface/mcp/catalog.py` 删除。
2. 7 处 import 点全改 `from orca.compile.catalog import ...`（commands.py:352 / in_session/cli.py:179,616,1014 / server.py:158,173,258）；**延迟 import（带「依赖边界」注释）改为顶层 import**（compile 在 iface 上游，无边界违规）。
3. 无循环依赖（catalog 只依赖 compile + schema；迁入 compile 后对 compile 是 intra-package，对 iface 零依赖）。
4. 测试 `tests/compile/test_catalog.py`（从 `tests/iface/mcp/test_catalog.py` 迁入），import + mock target（`_workflow_dirs`）更新。
5. 注释/docstring 里「catalog 在 iface/mcp/catalog.py」措辞同步到 compile/catalog.py。
6. 全量单测 0 回归；E2E：`orca list` / `teams list` / MCP `list_workflows` 三路仍返一致结果（catalog 单一实现不变，只是位置）。

---

## 1. 架构审视（改前）

### 1.1 为什么迁 —— 依赖铁律归位
Orca 单向依赖：`schema → compile → exec → run → iface`。catalog 做 workflow 发现/加载（`list_workflows`/`find_workflow`/`describe_workflow`/`_inputs_to_schema`），是 **compile 层关注**（它已 `from orca.compile import ConfigurationError, load_workflow` + `from orca.schema.workflow import Workflow`）。它坐在 `iface/mcp/` 里 = **iface 提供了一个 compile 层关注**，方向越位。

迁到 compile → 与 parser/validator 同层，方向正。**这是架构归位，非功能变化。**

### 1.2 smell 证据（改前）
- `iface/cli/commands.py:350` 注释「延迟 import：catalog 属 iface/mcp 子包，按本模块依赖边界不在顶层引入」+ L352 lazy import。
- `iface/in_session/cli.py:178` 注释「延迟 import catalog（iface/mcp 子包，按依赖边界不顶层引入）」+ L179/616/1014 lazy import。
- 延迟 import 本身是「知道方向不对、用 lazy 绕开」的 smell。迁到 compile 后 lazy 理由消失 → 改顶层，smell 消除。

### 1.3 单一实现不变
catalog 已是单一实现（注释「catalog 是唯一实现」，CLI/MCP 共用）。3b 只改位置 + import，不改逻辑/契约。**无多套事实源引入，反而消除位置 smell。**

### 1.4 循环依赖核实（侦察已证）
- catalog.py import 仅：stdlib + `orca.compile`（ConfigurationError, load_workflow）+ `orca.schema.workflow`。**对 iface/mcp 零 import**（L101 仅 docstring 提「server 层」，非 import）。
- 迁入 compile 后：`from orca.compile import ...` 变 intra-package（同包从 __init__ 拿），无循环。

---

## 2. 改动范围

### 2.1 迁移
- `git mv orca/iface/mcp/catalog.py orca/compile/catalog.py`（保 git 历史）。
- catalog.py 自身 import 不需改（`from orca.compile import ...` 在 compile 包内仍成立——从自家包 __init__ 导入；`from orca.schema...` 不变）。可选：改相对 import `from . import` / `from ._parser import`，但绝对 import 也可，保稳定。

### 2.2 更新 7 处 import 点（`orca.iface.mcp.catalog` → `orca.compile.catalog`）+ lazy→top-level
- `orca/iface/cli/commands.py:352`（list_workflows）—— 改顶层 import（删 L350 lazy 注释 + lazy 行，顶层 `from orca.compile.catalog import list_workflows`）。
- `orca/iface/in_session/cli.py:179`（find_workflow_yaml_path）/ `:616`（find_workflow）/ `:1014`（list_workflows）—— 三处 lazy 改顶层；删 L178 lazy 注释。
- `orca/iface/mcp/server.py:158`（list_workflows）/ `:173`（describe_workflow, find_workflow_by_name）/ `:258`（find_workflow）—— **建议全改顶层**，与 commands.py / in_session/cli.py 一致（消除「同 smell 三处理」不一致）；方向合法无环，扫描在调用体内不在 import 时，无性能损。若保 lazy 须 code-reviewer 给理由（不许「懒得改」）。

### 2.3 注释/docstring 同步
- `commands.py:347`「catalog 本身在 iface/mcp/catalog.py」→ compile/catalog.py。
- `in_session/cli.py:1011`「catalog 是唯一实现」+ 位置措辞。
- 任何 grep `iface/mcp/catalog` 命中（注释/docstring）改 compile/catalog。

### 2.4 测试迁移（含跨文件 mock target —— spec-reviewer issue#1 阻塞项）
- `git mv tests/iface/mcp/test_catalog.py tests/compile/test_catalog.py`：其内 import（L19）+ 2 处 mock target（L68, L141 `_workflow_dirs`）改 `orca.compile.catalog`。
- **其余 3 个测试文件的 mock target 字符串同步**（迁移后旧模块路径失效，必改，否则运行时 ImportError/AttributeError 崩 7 个测试）：
  - `tests/iface/cli/test_commands.py:213, 228`（`orca.iface.mcp.catalog._workflow_dirs` → `orca.compile.catalog._workflow_dirs`）
  - `tests/iface/in_session/test_v3_step1.py:501`（`orca.iface.mcp.catalog.list_workflows` → `orca.compile.catalog.list_workflows`）
  - `tests/iface/mcp/test_unit_tools.py:109, 157, 176, 209`（4 处 `_workflow_dirs` 同上）
- conftest/fixtures：实测 `tests/*/conftest.py` 无引用旧路径（grep 验证），无需改。

### 2.5 无逻辑/契约改动
- catalog 的 7 个公开函数（list_workflows/describe_workflow/find_workflow/find_workflow_by_name/find_workflow_yaml_path/_inputs_to_schema/_inputs_to_schema_list）签名 + 行为不变。

---

## 3. 测试 / E2E

- **单测**：`tests/compile/test_catalog.py` 全绿（迁入后）；grep 全仓 `orca.iface.mcp.catalog` = 0（import + 注释全清）；`tests/iface/ tests/compile/ tests/run/` 0 回归。
- **E2E（test-agent）**：三路 catalog 消费者真机返一致：
  - `orca list`（in_session CLI）→ workflows 列表。
  - `teams list`（commands.py）→ 同。
  - MCP `list_workflows`（若易起 server）→ 同；或 consult 单测。
  - 关键：catalog 单一实现位置变了，但三路仍一致（无多套）。

---

## 4. 风险 / scope 纪律

- **R1（循环依赖）**：迁入 compile 前确认 catalog 不 import 任何 iface/*（侦察已证零）。coder 复核 `python -c "import orca.compile.catalog"` 无 ImportError/CircularError。
- **R2（lazy→顶层）**：改顶层后若有循环（理论上不会，compile 不 import iface），回退该处保 lazy。code-reviewer 把关。
- **scope**：只迁位置 + 改 import + 同步注释/测试。**不动 catalog 逻辑/契约**。不合并 describe/list（那是 §10 follow-up）。不动 parser/validator/agents。

---

## 流程闭环
本计划 → **spec-reviewer**（核实循环依赖无风险 + lazy→顶层合理性 + scope）→ **coder-agent**（git mv + 改 8 import + lazy→顶层 + 测试迁移 + 注释同步 + code-reviewer + 单测 + commit + 状态文档）→ **test-agent** 真机（三路 catalog 一致）。

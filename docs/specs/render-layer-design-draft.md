# Render Layer Design Draft

> **状态**：design draft（跨阶段议题，对齐用，不写代码）
> **关联**：phase-7（CLI TUI）/ phase-9（Web，**待重写**）/ phase-10（MCP）；本 draft 是 Web 重写与 TUI 渲染升级的共同锚点。
> **必读前置**：[`shells-design-draft.md`](./shells-design-draft.md)（三壳共同契约）、[`phase-3-events.md`](./phase-3-events.md)（Event/tape 模型）、[`phase-12-cli-tui-redesign.md`](./phase-12-cli-tui-redesign.md)（当前 TUI 结构）。
> **裁决原则**：满足 CLAUDE.md「单 tape 唯一真相源 + 幂等 reducer + 一条读路径」底线；不动 translator、不动 canonical Event schema。

---

## §0. 摘要（TL;DR）

在 **canonical Event（已存在、唯一真相源）之上**加一层 **iface 内的渲染抽象**，由两段纯函数组成：

```
Event ──[tool normalizer]──▶ RenderItem ──[renderer registry]──▶ 像素
        (backend 工具差异归一)   (canonical kind+payload)   (per-end: TUI Rich / Web React)
```

- **唯一真相源**：tape 里的 canonical Event。RenderItem 是 Event 的**幂等投影**（pure function），不是第二真相。
- **规范单源**：本 spec 定义的 `RenderItem` schema + `(executor, tool_name) → kind` 表 + per-kind 渲染意图，是 TUI 和 Web 共同实现的契约。
- **backend 隔离**：translator（exec 层）消化协议形状差异；tool normalizer（iface 层）消化工具名/参数 shape 差异；**renderer 对 backend 完全无感知**。
- **复用边界**：TUI（Python/Textual）和 Web（React/TS）共享不了代码模块，但共享 ①canonical Event schema ②RenderItem schema ③`(executor, tool) → kind` 表 ④per-kind 渲染意图 —— 这四样是 spec 锁定的契约，两端各自实现，靠 snapshot 测试防漂移。
- **claude-code 对齐**：本 spec 的视觉与交互范式以 claude-code 为基准（industry de-facto standard、用户最熟悉）。opencode 的富渲染（PacedMarkdown / Shiki stream）作为 v2 演进方向参考。**§12.5-§12.9 五条裁决记录 claude-code 对齐的具体决策**。
- **复杂度裁决**：v1 中等复杂度，~300-500 行/端，一个 phase 的工程量；流式 shiki 高亮与千行 diff 虚拟化**显式推迟到 v2**。

---

## §1. 背景 + 问题

### 1.1 现状

1. **工具 I/O 渲染糙**：TUI 把工具调用截断成单行 `tool: read(...)` / `→ <60 字符>`（`orca/iface/cli/widgets/log_stream.py:69-75`、`node_detail.py:62-66`）；Web 更糙，`agent_tool_result` 只显 `tool_result` 四个字（`orca/iface/web/frontend/src/components/detail/LogStream.tsx:14-90`）。
2. **TUI 内部已经重复**：`log_stream.format_event` 与 `node_detail._format_stream_line` 是两套独立的 etype→display 映射（DRY 已破）。
3. **跨端零共享且不一致**：TUI 和 Web 各写一套，同一个 `agent_tool_call` 在三处显示三种样子。
4. **backend 工具异构未归一**：claude `Edit`、opencode `edit`、codex `apply_patch` 是同一语义但名字/参数 shape 不同；opencode 的 `read` 对目录返回 XML，TUI 没解析就直接糊一行。

### 1.2 用户痛点

跑复杂 workflow 时，工具调用信息密集且无结构，看不清。期望渲染成 claude-code / opencode 那种富显示（目录树、代码块、diff、终端块），且**规则要定好统一住**，TUI 现在实现、Web 未来重写时直接照搬。

### 1.3 关键事实（已在前期调研中确认）

- translator 层已经 vendor-neutral：`translator(line, session_id) -> list[Event]` 纯函数把协议差异（claude stream-json / opencode NDJSON / codex SSE）内化进单个文件，加 codex 是再写一个 translator 文件，**零 canonical schema 改动**。
- canonical Event（6 个 agent EventType：`message/thinking/tool_call/tool_result/usage/error`）已经是 TUI 和 Web 的共同数据契约（前端 `types/events.ts:9-38` 已逐字对齐）。
- 业界（opencode `session-ui` / codex `HistoryCell`）做法一致：**按 tool 名分发的 renderer 注册表 + 单数据源投影到渲染层**；AgentHarness（多 store + 多 sidecar）的反例印证 Orca 的单 tape 选择正确。

---

## §2. 设计目标 + 非目标

### 2.1 目标

1. **架构清晰**：四层（translator → Event → normalizer → renderer），每层职责单一、依赖单向。
2. **唯一真相源**：tape（canonical Event）是运行时唯一真相；本 spec 是规范层唯一真相；RenderItem 是幂等投影，非第二真相。
3. **backend 同/异清晰隔离**：translator 消化协议差异，normalizer 消化工具名/shape 差异，renderer 对 backend 零感知。新 backend 接入有清单可循。
4. **最大化复用**：spec 锁定四样共享契约，TUI/Web 各自实现但视觉与结构一致。
5. **可增量**：v1 先覆盖 6 个高频工具 + 兜底，未来按 kind 增量扩展。
6. **claude-code 对齐**：视觉与交互范式对齐 claude-code（用户最熟悉、行业事实标准）；claude-code 走朴素路线（thinking 纯文本、固定配色高亮、JSON 美化未知工具）证明 v1 不需要追 opencode 的富渲染复杂度。

### 2.2 非目标（显式不做）

| 项 | 原因 |
|---|---|
| 复刻 claude-code / opencode 像素 | 不必要的细节追逐；视觉"接近"即可 |
| 加 IR / display-spec 中间层（post-Event 的 vendor-neutral 渲染树） | canonical Event 已是 vendor-neutral 契约；再加 IR 只搬移复杂度不消除，且 IR 必丢富信息（Edit 的 diff、Bash 的 stdout 分块都得退化成文本块）。**裁决记录在此，防以后再提** |
| v1 流式 shiki 实时高亮 | 复杂度真高（worker + morphdom + 增量解析），延后到 v2 |
| v1 千行 diff 虚拟化 | 大 diff 截断 + "展开更多" 先扛，延后到 v2 |
| 动 translator | translator 保持薄，协议形状翻译职责单一 |
| 动 canonical Event schema | Event 是底线契约，不改 |
| 重写 TUI 整体架构 | 只抽 tool 渲染到新模块，TUI 现有 DAG / gate / chart 不动 |
| 修当前 Web | Web 整体待重写，本 spec 是其重建锚点，不动现有 Web 代码 |

---

## §3. 核心架构（四层 + 唯一真相源链）

### 3.1 四层数据流

```
┌──────────────────────────────────────────────────────────────────────┐
│ backend stream（claude stream-json / opencode NDJSON / codex SSE）   │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  [exec] translator（per-backend, 现状保留）
                              │   纯函数: (line, session_id) → list[Event]
                              │   职责: 协议形状 → canonical Event
                              │   后端差异: 协议 envelope、event type 词表、增量 vs 整块、
                              │            usage per-step vs 一次性、call/result 合/分
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│ canonical Event  ◀── 唯一真相源 #1（tape 持久化）                    │
│ {type: agent_tool_call/result, data: {tool, args, tool_call_id, result}} │
│ vendor-neutral 词汇表（6 个 EventType, schema/event.py:18-71）       │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  [iface] tool normalizer（per-end, NEW）
                              │   纯函数: (executor, tool_name, args, result) → RenderItem
                              │   职责: backend 工具名/参数 shape → canonical kind + payload
                              │   后端差异: 工具名别名（Read/read/read_file）、
                              │            参数 shape（Edit old/new vs apply_patch patch 字符串）、
                              │            特殊输出（opencode read 目录 XML）
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│ RenderItem  ◀── 派生投影（幂等，非第二真相）                          │
│ {kind: "file_edit", status, title, payload: {path, hunks, +N -M}}    │
│ vendor-neutral 渲染契约（本 spec 定义）                              │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  [iface] renderer registry（per-end, NEW）
                              │   纯函数: (RenderItem) → Rich renderable | ReactNode
                              │   职责: kind → 像素
                              │   后端差异: 零（kind 已统一）
                              ▼
                    像素（TUI: Rich renderable / Web: React node）
```

### 3.2 唯一真相源链（核心不变量）

| 层 | 是否真相源 | 理由 |
|---|---|---|
| backend 原始流 | ❌ | 协议异构、易变、不可重放 |
| canonical Event（tape） | ✅ **运行时唯一真相** | 持久化、可重放、幂等 reducer 输入。CLAUDE.md 底线 |
| RenderItem | ❌ | 纯函数投影 = `f(executor, event.data)`，无独立状态。**重新计算必等价** |
| 像素 | ❌ | 临时态，重新渲染必等价 |
| **本 spec**（RenderItem schema + 映射表 + 渲染意图） | ✅ **规范层唯一真相** | 两端实现的契约。spec 变 = 两端同步变 |

> **关键不变量**：给定相同 Event，normalizer 必产出相同 RenderItem；给定相同 RenderItem，renderer 必产出视觉等价的像素。**任何"中间状态"被丢弃后能从 Event 完全重建**——这是单 tape 原则在渲染层的延伸。

### 3.3 为什么 normalizer 是纯投影而非第二真相

- normalizer 输出 RenderItem，但**不持久化、不进 tape、不写文件**。每次 widget 重渲都从 Event 重算。
- 这意味着 normalizer 可以随便改（修 bug、加 kind），老 tape 不需要迁移——重放即得新输出。
- 与 AgentHarness 反例对比：它把渲染中间态写进 sidecar JSON 当真相源，导致同一事实被算 5 次且永久漂移。Orca 不重蹈。

---

## §4. backend 同/异 隔离矩阵

> 这是本 spec 的核心：**哪些差异在哪一层被消化**，新 backend 接入有清晰清单。

### 4.1 同的部分（所有 backend 共用，spec 一次性定义）

| 项 | 定义在 |
|---|---|
| canonical Event 词汇表（6 EventType） | `orca/schema/event.py`（已有） |
| canonical RenderToolKind 枚举 + payload schema | `orca/schema/render_item.py`（NEW，§5） |
| `(executor, tool_name) → kind` 映射表 | 本 spec §6 + 两端镜像实现 |
| per-kind 渲染意图（"file_edit 长什么样"） | 本 spec §8 |
| 渲染分发结构（kind → renderer 注册表） | 两端 iface，同构 |
| 流式累积 / tool_call-result 配对规则 | 本 spec §9 |

### 4.2 异的部分（per-backend，归一化层消化掉）

| 差异维度 | 消化层 | 消化方式 |
|---|---|---|
| 协议形状（stream-json / NDJSON / SSE） | translator | 每家一个 translator 文件 |
| event type 词表 | translator | translator 内部分派 |
| 文本流式语义（增量 vs 整块） | reducer | 一律 append-only 累积（§9） |
| usage 语义（per-step vs 一次性） | reducer | 一律"取最后一条"（已有，`opencode.py:21-23`） |
| tool_call / tool_result 合/分 | reducer | 按 `tool_call_id` 配对（§9） |
| **工具名别名**（Read/read/read_file） | **normalizer** | `(executor, tool) → kind` 表（§6） |
| **工具参数 shape**（Edit old/new vs apply_patch patch） | **normalizer** | per-kind payload 归一函数（§6） |
| **特殊输出格式**（opencode read 目录 XML） | **normalizer** | 在 `file_read.payload_normalizer` 解析 |

### 4.3 新 backend 接入清单（以 codex 为例）

| 步骤 | 层 | 工作量 |
|---|---|---|
| 1. 写 translator | exec（`orca/profiles/translators/codex.py`） | ~150 行，与 opencode 同构 |
| 2. 写 profile | exec（`orca/profiles/builtin/codex.py`） | ~60 行 |
| 3. 在 normalizer 加 codex 行 | iface（per-end） | `(executor, tool) → kind` 表加几行；payload_normalizer 加 codex 分支（如 apply_patch 解析） |
| 4. renderer | iface | **零改动**（kind 已统一） |
| 5. capabilities 声明 | exec | 已有 7 字段够，零新增 |

**关键收益**：步骤 4 零改动证明 backend 隔离成立——renderer 是 backend-blind 的。

---

## §5. Canonical RenderItem 契约

### 5.1 RenderItem 顶层 schema

新增 `orca/schema/render_item.py`：

```python
from typing import Literal, Any
from pydantic import BaseModel, ConfigDict

RenderToolKind = Literal[
    "file_read",     # 读文件 / 列目录
    "file_write",    # 写新文件
    "file_edit",     # 编辑已有文件（diff）
    "shell",         # 执行 shell 命令
    "glob",          # 文件名匹配
    "grep",          # 内容搜索
    "unknown",       # 兜底（未知工具走 generic fallback）
]

ToolStatus = Literal["running", "completed", "error", "interrupted"]

class RenderItem(BaseModel):
    """canonical 渲染单元。Event 的幂等投影，非真相源（重算必等价）。

    payload 按 kind 分派（§5.2）。raw 保留原始 args/result 供"查看原始"调试。
    """
    model_config = ConfigDict(extra="forbid")

    kind: RenderToolKind
    status: ToolStatus
    title: str             # 一行摘要（"src/foo.py" / "$ ls -la" / "pattern: *.py"）
    subtitle: str = ""     # 可选副标题（"+12 -3" / "3 matches"）
    payload: dict          # per-kind 结构化字段（§5.2）
    raw: dict[str, Any]    # 原始 args + result，调试兜底（永不参与渲染决策）
```

### 5.2 per-kind payload schema

| kind | payload 字段 | 说明 |
|---|---|---|
| `file_read` | `{path: str, is_dir: bool, content?: [{n:int, text:str}], entries?: [str], truncated: bool}` | `is_dir=true` 时只有 `entries`；否则 `content` 行号化 |
| `file_write` | `{path: str, content: [{n:int, text:str}], bytes: int}` | 新文件全文 |
| `file_edit` | `{path: str, hunks: [{start:int, lines: [{type: "add"\|"del"\|"ctx", text:str}]}], added:int, deleted:int}` | unified diff 解构 |
| `shell` | `{command: str, output: str, exit_code?: int, duration_ms?: int}` | 终端块 |
| `glob` | `{pattern: str, matches: [str]}` | 路径列表 |
| `grep` | `{pattern: str, matches: [{path: str, lines: [{n:int, text:str, hit_start?:int, hit_end?:int}]}]}` | 按文件分组 |
| `unknown` | `{tool_name: str, args_preview: str, result_preview: str}` | 兜底，预览截断 |

> payload 字段是**两端实现 RenderItem 构造时的契约**，也是 renderer 取数的契约。前端 `orca/iface/web/frontend/src/types/render_item.ts` 镜像（与 Event 同模式）。

---

## §6. `(executor, tool_name) → kind` 映射 + payload 归一化

### 6.1 工具名 → kind 主表（v1，三家 backend）

| kind | claude | opencode | codex | 备注 |
|---|---|---|---|---|
| `file_read` | `Read` | `read` | `read_file` | opencode `read` 对目录返回 XML（**已校准**，见 §6.3 实测 shape） |
| `file_write` | `Write` | `write` | `write_file` *(待校准)* | |
| `file_edit` | `Edit` | `edit` | `apply_patch` | codex `apply_patch` 的 `args.patch` 是 unified diff 字符串，**直接解析**；claude/opencode 给 `old_string/new_string`，**自算 diff** |
| `shell` | `Bash` | `bash` | `shell` | 三家几乎同形：`{command}` + stdout |
| `glob` | `Glob` | `glob` | *(codex TBD)* | |
| `grep` | `Grep` | `grep` | *(codex TBD)* | |
| `unknown` | 其余 | 其余 | 其余 | 兜底，**就是现在的渲染**（args JSON + result 截断） |

> codex 工具名标 *(待校准)* 的，等真跑 codex 流后填实（spec 留 TODO，不阻塞 v1 实施；v1 只覆盖 claude+opencode，codex 是 §11 v1.5）。

### 6.2 归一化函数契约

```python
# orca/iface/cli/widgets/tool_render/normalize.py（NEW）
def normalize_tool(
    executor: str,           # "claude" | "opencode" | "codex" | ...
    tool_name: str,          # 原始 backend 工具名
    args: dict,              # Event.data.args
    result: str | None,      # Event.data.result（tool_result 时有，tool_call 时 None）
    status: ToolStatus,      # running（call 阶段）/ completed（result 阶段）
) -> RenderItem:
    kind = _resolve_kind(executor, tool_name)        # 查 §6.1 表
    payload = _PAYLOAD_NORMALIZERS[kind](executor, args, result)
    return RenderItem(
        kind=kind,
        status=status,
        title=_make_title(kind, payload),
        subtitle=_make_subtitle(kind, payload),
        payload=payload,
        raw={"args": args, "result": result},
    )
```

**args 类型契约**（spec-review-adversarial P1-6 闭环）：`args: dict` 由 translator 层保证（phase-4 SPEC translator 契约）。若 normalizer 收到非 dict（如 codex 协议层 raw JSON 字符串未在 translator 解析）→ `raise NormalizeError(f"args must be dict, got {type(args).__name__}: {args!r}")`，**fail loud 不静默**。这条约束把"translator 保证 args 已解析"显式化，避免 codex 接入时 silent breakage。

**result 类型契约**：`result: str | None`；`None` 表示 tool_call 阶段（result 尚未到）；非 None 时由 translator 保证为 str（`_normalize_tool_output` in translator 已归一）。

### 6.3 per-kind 归一化策略

- **`file_edit`**：
  - claude/opencode `Edit/edit`：`args = {file_path, old_string, new_string}` → 自算 unified diff → `hunks`
  - codex `apply_patch`：`args = {patch: "<diff 字符串>"}` → 直接解析 → `hunks`
  - 实现：用 Python `difflib` / TS `diff` 库；diff 引擎是 normalizer 内部细节，不进契约
- **`file_read`**：
  - claude `Read`：`args.file_path` + `result`（文件内容）→ 行号化 `content`
  - opencode `read` 目录：**实测 shape（tape 证据：`runs/demo_task-20260703-221337-c94151.jsonl`）**：
    ```
    <path>/abs/path</path>
    <type>directory</type>
    <entries>
    .codegraph/
    .git/
    ...
    (17 entries)
    </entries>
    ```
    归一化策略：用 `_parse_opencode_xml_envelope(text)` 提取 `{type: "directory", path, entries}`，
    `entries` 来自 `<entries>` 内按行解析（剥 `(... entries)` 尾注）。
  - opencode `read` 文件：**同为 XML envelope**（实测 shape，tape 证据
    `runs/demo_task-20260704-085641-f15c8d.jsonl` seq=5）：
    ```
    <path>/abs/path/pyproject.toml</path>
    <type>file</type>
    <content>
    1: <line 1>
    2: <line 2>
    ...
    (End of file - total N lines)
    </content>
    ```
    归一化策略：用同一 `_parse_opencode_xml_envelope(text)` 提取
    `{type: "file", path, content}`；`<content>` 内文本需剥两层 opencode 自加修饰：
    (a) 每行行首的 `N: ` 行号前缀（空行可能无前缀，保留）
    (b) 尾部 `(End of file - total N lines)` marker
    然后行号化为 `content`（避免与 Rich `Syntax` 的 `line_numbers=True` 双重行号）。
  - **envelope 检测**：仅在 `result.lstrip().startswith("<path>")` 时尝试 XML 解析，
    避免把 claude `Read` 的普通文件原文（如含 `<html>` 的 HTML 文件）误判。
    XML 解析失败 / `<type>` 未知 → fail visible（§13：降级原样文本展示 + warning log，不 raise）。
- **`shell`**：三家一致，`args.command` + `result`（output）
- **`unknown`**：原样透传 args/result，截断预览

### 6.4 派生投影的纯函数性证明

`normalize_tool` 满足：
- 无 I/O（不读文件、不读环境）
- 无副作用（不写 tape、不缓存 mutable 状态）
- 给定相同输入必产出相同 RenderItem（包括 diff 算法是确定性的）

→ 允许任意时机重算，与 §3.2 唯一真相源链一致。

---

## §7. 模块布局（单向依赖铁律）

### 7.1 依赖方向（严格遵守 `schema → events → exec → iface`）

```
orca/schema/
  ├─ event.py            （已有）
  └─ render_item.py      （NEW: RenderItem + RenderToolKind, 见 §5）

orca/iface/cli/widgets/tool_render/        （NEW, TUI 侧）
  ├─ __init__.py
  ├─ normalize.py        （executor+tool+args+result → RenderItem, 纯函数）
  ├─ kinds.py            （per-kind Rich renderer: render_file_edit(...) → Rich renderable）
  ├─ registry.py         （kind → renderer 派发表）
  └─ reduce.py           （Event 流 → RenderItem 流的累积 reducer, §9）

orca/iface/cli/widgets/
  ├─ log_stream.py       （改造: tool 事件委托给 tool_render）
  └─ node_detail.py      （改造: tool 事件委托给 tool_render，先消除 DRY）

orca/iface/web/frontend/src/               （v2 Web 重写时, TS 镜像）
  ├─ types/render_item.ts （镜像 schema/render_item.py）
  └─ tools/
     ├─ normalize.ts
     ├─ kinds.tsx         （per-kind React component）
     ├─ registry.ts
     └─ reduce.ts
```

### 7.2 依赖约束（与现有 widget 铁律一致）

- `tool_render/*` **只依赖** `orca.schema` + `textual`/`rich` + stdlib
- **禁止反向依赖** `orca.exec` / `orca.run` / `orca.events.bus`（与 `widgets/__init__.py:7-9` 现有 docstring 一致）
- 违反 = 静态检查 + code review 拒收

### 7.3 现有代码迁移路径（TUI）

1. **先消 DRY（不改行为）**：把 `log_stream.format_event` 与 `node_detail._format_stream_line` 的工具部分抽到 `tool_render/normalize.py`，两处调用同一函数。
   - **既有 snapshot 测试允许更新**（输出格式字面不变，只改函数调用路径）。
   - **新增一致性测试 `test_normalize_dry`**：`normalize(老 input)` 与老 `format_event` output 字符级一致（防迁移过程偷偷改行为）。
   - **RenderState 与 `node_detail._stream_lines` 解耦边界**（spec-review-adversarial P0-4 闭环）：
     - (a) `node_detail._stream_lines: dict[node, list[str]]` 维持现状——它是"按选中节点展示历史流式行"的 **UI 缓存**，按 `node` key
     - (b) `RenderState`（§9.1）是 normalizer/reducer 的**内部累积态**，按 `session_id|node` key，**不暴露**给 node_detail
     - (c) `node_detail.append_event_stream` 在渲染 tool card 时**消费** RenderItem（一次调用），但**不替代** `_stream_lines`——两套状态各自独立、不互换
2. **再加 kind**：在 `tool_render/kinds.py` 逐 kind 实现 Rich renderer，registry 派发。
3. **`unknown` 兜底保留现状**：未知工具走老渲染（args + 截断 result），不阻塞 v1 上线。

### 7.4 Web 端策略

- **不动现有 Web 代码**（用户已声明要重写）
- Web 重写时，照本 spec §5/§6/§8 实现 TS 镜像：`types/render_item.ts` + `tools/normalize.ts` + `tools/kinds.tsx` + `tools/registry.ts`
- 届时再评估是否采纳 `assistant-ui` + `tool-ui`（前期调研结论：Web 端可复用其组件库，写 OrcaRuntime adapter 喂 tape 事件）—— **spec 不锁死**，只锁 RenderItem 契约

---

## §8. per-kind 渲染意图（TUI + Web 共享 spec）

> 这是两端视觉一致性的锚点。TUI 用 Rich/Textual 实现，Web 用 React 实现，**结构与视觉意图必须对齐**。

### 8.1 视觉意图表（claude-code 对齐基准）

| kind | 头部（title + subtitle） | 体（payload 渲染） | 折叠默认 |
|---|---|---|---|
| `agent_thinking` | *(无 header)* | dim + italic **纯文本**（**不**渲染 markdown），按 `session_id`+`node` 累积 | 默认展开；`/thinking` 命令切换可见性（参考 claude-code #36006） |
| `file_read` | `📄 <path>` | 行号化代码（Rich `Syntax` / Web shiki），目录则 `Tree` | 折叠（点击展开） |
| `file_write` | `✏ <path> (new, <bytes>B)` | 行号化代码 | 折叠 |
| `file_edit` | `✏ <path> (+<added> -<deleted>)` | unified diff：`+` 绿 / `-` 红 / ` ` 灰底 | 展开（diff 是核心信息） |
| `shell` | `▶ <command>` | 终端块（等宽，保留 ANSI） | 折叠（成功时）；展开（exit_code != 0） |
| `glob` | `༚ <pattern> (<N> matches)` | 路径列表 | 折叠（N>10 时） |
| `grep` | `🔍 <pattern>` | 按文件分组，命中行高亮 hit 区间 | 折叠 |
| `unknown` | `<tool_name>` | **args JSON 美化**（`json.dumps(args, indent=2)`）+ result 截断预览 | 折叠 |

### 8.2 共性渲染规则

- **可折叠 Panel** 包裹（Rich `Panel` / Web `Collapsible`）：所有 kind 一致
- **状态色**：`running` 灰 / `completed` 绿 / `error` 红 / `interrupted` 黄；体现在 header 边框色或 icon
- **截断策略**：单 kind 体 > 200 行时折叠 + 显示"展开更多（共 N 行）"；v1 不虚拟化（§11 v2）

### 8.3 markdown 渲染（agent_message 文本）

agent_message 可能含 markdown（代码块、列表、表格）：

- **TUI**：Rich `Markdown` + `Syntax`（**现有，零工作量**）。**代码块语法高亮开启**——claude-code（issue #48636，固定配色）与 opencode（Shiki worker）都做，Orca 跟齐（Rich `Markdown` 自带 `Syntax`，零额外工作）
- **Web v1**：`react-markdown` + `rehype-highlight`（简单，非流式）
- **Web v2**：升级 `@shikijs/stream` + web worker + morphdom（流式增量高亮，延后）
- **流式累积**（§9）：渲染层不区分 claude 增量 / opencode 整块，每个 `agent_message` 视为"追加文本"，reducer 按 `session_id`+`node` 累积成完整文本后再 markdown 渲染

### 8.4 thinking / reasoning 渲染（agent_thinking 文本，claude-code 对齐）

agent_thinking 是模型推理过程文本（claude 启用 extended thinking 时发 `thinking_delta`；codex 发 `reasoning_summary_text.delta`；opencode 当前不发）。

**v1 决定：抄 claude-code 朴素路线，不抄 opencode 富路线**（裁决见 §12.5）：

- **视觉**：`Rich Text(dim=True, italic=True)` 纯文本，**不渲染 markdown**（claude-code `<Text italic dim>` 一致；opencode 用 PacedMarkdown 是为 50+ turn 长文本卡顿优化，Orca 单 agent 短对话不需要）
- **默认状态**：展开（thinking 是 agent 当下状态指示）
- **可见性切换**：`/thinking` 命令切换全局可见性（参考 claude-code TUI 同名命令）
- **位置**：紧贴在最终 `agent_message` 之前，独立成段
- **流式**：每个 `agent_thinking` 事件视为"追加文本"，按 `session_id`+`node` 累积
- **无 markdown / 无 spinner / 无 PacedMarkdown / 无折叠动画**（v1 不做，v2 评估）

---

## §9. 流式累积 + tool_call/result 配对（渲染层 reducer 规则）

### 9.1 reducer 状态

每个 widget 维护一个"渲染状态"（仅内存，不持久化，从 Event 完全可重建）：

```python
@dataclass
class RenderState:
    messages: dict[str, str]              # session_id|node → agent_message 累积文本
    thinking: dict[str, str]              # session_id|node → agent_thinking 累积文本（dim+italic 渲染）
    thinking_visible: bool = True         # /thinking 命令切换的全局可见性
    tool_cards: dict[str, RenderItem]     # tool_call_id → RenderItem（call 创建，result 填充）
    order: list[tuple[int, str, str]]     # (seq, kind, key) 三元组按 seq 排序；kind ∈ {"message","thinking","tool"}
```

### 9.2 事件处理规则

| Event | reducer 动作 |
|---|---|
| `agent_message` | `messages[key] += text`；`(seq, "message", key)` 入 `order`；触发该 message 重渲 |
| `agent_thinking` | `thinking[key] += text`；`(seq, "thinking", key)` 入 `order`；若 `thinking_visible=False` 则不渲染（仍累积，保留可重建性） |
| `agent_tool_call` | `normalize_tool(status="running")` → 入 `tool_cards[tool_call_id]`；`(seq, "tool", tool_call_id)` 入 `order` |
| `agent_tool_result` | 取 `tool_cards[tool_call_id]`，用 result 重新 `normalize_tool(status="completed")` 覆盖；`order` 不变（位置已由 call 时的 seq 决定） |
| `agent_usage` | 累加到所属 node 的 footer（"取最后一条"语义，与 §4.2 一致） |
| `error` | 把对应 tool card / message 标 `error` 状态 |

### 9.3 中断/异常 + 并发排序

- **tool_call 无对应 tool_result**（agent 被中断）：card 状态 `interrupted`，渲染黄色边框 + "未完成"标记
- **并发 tool_call 排序**：**按 `seq` 线性展示，不分组不重排**（与 claude-code content-block-index 顺序一致；`order` 三元组按 seq 排序天然实现）。多个 tool_card 各自独立，按 call 时的 seq 决定显示位置
- **parallel / foreach 并发**：每个分支的 tool_card 走各自 `tool_call_id`，跨分支按 seq 混排

### 9.4 幂等性

reducer 是 `(state, event) → state` 的纯函数（Orca 已有 reducer 模式，phase-3 SPEC）。给定相同 Event 序列必产相同 RenderState。这与 §3.2 唯一真相源链一致。

---

## §10. 跨端一致性测试

### 10.1 测试金字塔

| 层 | TUI | Web | 一致性 anchor |
|---|---|---|---|
| normalizer | pytest：`normalize_tool(executor, tool, args, result)` snapshot | vitest：相同输入相同 RenderItem JSON | **共享 fixtures**：`tests/e2e_phase15/_artifacts/render_tool_cases.json`（canonical Event → 期望 RenderItem）；Web 端重写时镜像到 `orca/iface/web/frontend/src/__fixtures__/render_tool_cases.json` |
| renderer | textualism snapshot（Rich renderable → ASCII snapshot） | `@testing-library` + visual regression | 各自独立（视觉细节允许差异，结构必须一致） |
| reducer | pytest：Event 序列 → RenderState snapshot | vitest：同 | **共享 fixtures**：`tests/e2e_phase15/_artifacts/render_event_streams.jsonl`（沿用 Orca 既有 `tests/e2e_phaseNN/_artifacts/` artifact 约定，不引入新 `tests/fixtures/` 目录） |

### 10.2 跨端一致性 anchor

- **共享 fixtures**（一份 JSON，双端跑）是 spec 落地的硬约束：相同 Event 输入 → 相同 RenderItem JSON。任何一端实现偏离 spec 立即测试失败。
- 这取代了"代码共享"，用"契约 + 测试共享"达到同等效果。
- **路径约定**（spec-review-adversarial P0-5 闭环）：沿用 Orca 既有 `tests/e2e_phaseNN/_artifacts/` 模式（如 `tests/e2e_phase13/_artifacts/`），不引入新的 `tests/fixtures/` 顶层目录。Web 端 fixture 镜像到 `orca/iface/web/frontend/src/__fixtures__/`（前端测试惯例）。

### 10.3 fixture 覆盖（v1 必须有）

- 6 kind × 至少 2 backend × 至少 1 case（含目录/diff/长输出/中断）
- 至少 1 个完整 workflow 的 Event 流（推荐 `parallel_research.yaml` 的 tape）

---

## §11. 分期 + v2 路线

### 11.1 v1（本 spec 范围）

- ✅ RenderItem schema + 6 kind + `unknown` 兜底
- ✅ normalizer 覆盖 claude + opencode（codex 延后）
- ✅ TUI 实现（迁移现有 log_stream/node_detail 到 tool_render，先消 DRY 再加富）
- ✅ markdown 基础渲染（Rich Markdown / react-markdown，非流式）
- ✅ 截断兜底（单 kind > 200 行折叠）
- ✅ 跨端 fixture + snapshot 测试（TUI 先跑通；Web 重写时补）

**预估工作量**：TUI ~500-600 行（spec-review-adversarial P1-11 闭环后修正：含 DRY 消除 100 + schema 60 + normalizer 100 + kinds 200 + registry/reduce 40 + 跨端 fixture 60），1 phase。

### 11.2 v1.5（codex 接入时）

- 加 codex translator（独立 phase-14 风格任务）
- normalizer 加 codex 行（`apply_patch` 解析、`shell`/`read_file` 映射）
- renderer 零改动（验证 backend 隔离）

### 11.3 v2（未来，按需）

- 流式 markdown shiki 增量高亮（Web 端，参考 opencode `@shikijs/stream` + worker + morphdom）
- 千行 diff 虚拟化（参考 opencode `@pierre/diffs` 或 codex `TOOL_CALL_MAX_LINES` 截断策略）
- 更多 kind：`WebFetch` / `WebSearch` / `TodoWrite` / `WebAgent`
- 采纳 assistant-ui + tool-ui（Web 端，写 OrcaRuntime adapter）
- 动画 / 微交互（折叠缓动、流式打字）

### 11.4 Web 重写时机

- **不在本 spec 范围内**触发 Web 重写
- Web 重写时，照本 spec §5/§6/§8 实现 TS 端；TUI 已是验证过的参照实现
- Web 重写的 phasing 由 Web 端单独 spec 决定（本 spec 不锁死 Web 工具栈选型）

---

## §12. 显式裁决记录（防漂移）

| # | 议题 | 裁决 | 理由 |
|---|---|---|---|
| 12.1 | 是否加 IR / display-spec 中间层 | **否** | canonical Event 已是 vendor-neutral 契约；IR 必丢富信息（Edit diff、Bash stdout 分块）；只搬移复杂度。未来再提请先推翻此条裁决 |
| 12.2 | TUI/Web 是否共享代码模块 | **否**（Python vs TS 共享不了） | 共享四样契约：Event schema / RenderItem schema / 映射表 / 渲染意图；靠 fixture + snapshot 测试防漂移 |
| 12.3 | normalizer 是否进 tape | **否** | 派生投影，重算必等价；进 tape = 第二真相，违反单 tape 底线 |
| 12.4 | normalizer 放 exec 还是 iface | **iface** | view-shaped 数据，exec 不应感知渲染；与 widget 现有铁律一致 |
| 12.5 | 工具名归一化是否由 translator 做 | **否** | translator 保持薄，只翻协议形状；backend 工具名/shape 知识归 normalizer |
| 12.6 | 兜底是否保留现状 | **是** | `unknown` kind 走老渲染（args + 截断 result），保证未覆盖工具不阻塞 v1 |
| 12.7 | TUI 是否大改 | **否** | 只抽 tool 渲染到新模块；DAG / gate / chart 不动 |
| **12.8** | **agent_thinking v1 风格** | **抄 claude-code 朴素路线**：dim+italic **纯文本**（不渲染 markdown），默认展开，`/thinking` 切换可见性 | claude-code 源码观察（[ivanleo Ink 教程](https://ivanleo.com/blog/migrating-to-react-ink) + 公开 Ink 复刻）显示 thinking 走 `<Text italic dim>` 不渲染 markdown；opencode 用 PacedMarkdown 是为 50+ turn 长文本卡顿优化，Orca 单 agent 短对话用不上。markdown 渲染延后 v2 |
| **12.9** | **unknown kind 的 args 渲染** | **JSON 美化**（`json.dumps(args, indent=2)`） | claude-code `JSON.stringify(input, null, 2)` 一致；raw 一行字符串可读性差 |
| **12.10** | **tool card 复制按钮** | **v1 不做** | claude-code 终端靠选中 + `Cmd+C`（无单独 tool result 复制键）；TUI 同理；Web v2 做（参考 opencode `MessageActionButton` + `writeClipboard`） |
| **12.11** | **并发 tool card 排序** | **按 `seq`**（线性，不分组不重排） | 与 claude-code content-block-index 顺序一致；与 tape 全序一致；`order` 三元组天然实现。**Acceptance**：seq 单调递增是 Tape 不变量（phase-3 SPEC §3.2 Lock 覆盖），同 seq 不可能；排序实现 `sorted(order, key=lambda x: x[0])` 即可，无需稳定排序兜底 |
| **12.12** | **markdown 代码块语法高亮** | **开** Rich `Syntax` | claude-code 源码观察显示代码块走固定配色高亮；opencode 用 Shiki worker。Rich `Markdown` 自带 `Syntax`，零额外工作。Orca 跟齐（注：固定配色有可读性风险，[issue #21034](https://github.com/anthropics/claude-code/issues/21034) 反映"chaotic rainbow"问题，v2 评估可定制方案） |

---

## §13. 风险 + 待校准

| 项 | 风险 | 缓解 |
|---|---|---|
| codex 工具名/参数 shape 未真跑校准 | v1.5 接 codex 时映射表可能要改 | spec §6.1 标 *(待校准)*；不阻塞 v1（v1 只 claude+opencode） |
| opencode `read` 目录 XML 解析依赖 opencode 版本 | opencode 升级可能改 XML 格式 | **fail visible**（spec-review-adversarial P1-7 闭环）：解析失败时降级 `is_dir=false` + 原样文本 + 记 warning log（用户可见降级提示，不静默吞错）；**不 raise**（避免单工具渲染失败阻塞整个 TUI） |
| diff 算法两端不一致（Python `difflib` vs TS `diff`） | 同一 Edit 在 TUI 和 Web 渲染出微妙不同 hunk 边界 | snapshot fixture 用**结构化 hunks**（不依赖具体 diff 算法边界）；可接受视觉微差 |
| TUI 现有 `log_stream`/`node_detail` 重复迁移阻力 | 改动面触及活跃代码 | §7.3 强制"先消 DRY（不改行为）→ 再加 kind"两步走，每步独立 commit + 测试 |
| Web 重写时偏离 spec | 跨端漂移 | §10 fixture 是硬约束；Web 重写 PR 必须跑通相同 fixtures |
| args 非 dict 静默通过 | codex 接入时 translator 漏解析 JSON 字符串 args，normalizer silent breakage | §6.2 NormalizeError fail loud（P1-6 闭环） |
| tool_call_id 跨 backend 唯一性 | codex 的 id 格式与 claude/opencode 不兼容，配对失败 | v1.5 接 codex 时加 e2e 测试覆盖；v1 不接 codex 无风险 |
| RenderItem `extra="forbid"` 演进困难 | v1.5 接 codex 需新字段时 schema 不兼容 | v1 锁 `extra="forbid"`；v1.5 若需新字段走 RenderItem v2 + 兼容层（schema 版本化策略，v1.5 SPEC 再定） |

---

## §14. 已对齐决定（原开放问题，已闭环）

> 原 §14 五个开放问题已全部闭环到 §12.8-§12.12 裁决记录。下面是结论速查，每条都对照过 claude-code 实际做法（基于源码观察 / 公开 Ink 复刻 / ivanleo 教程，**不挂误用的 issue 编号**）。

| 原问题 | 结论 | 裁决号 | claude-code 证据 |
|---|---|---|---|
| agent_thinking v1 风格 | dim+italic **纯文本**（不渲染 markdown），默认展开，`/thinking` 切换 | §12.8 | `<Text italic dim>` 不做 markdown（ivanleo Ink 教程 + 公开 Ink 复刻源码观察） |
| unknown args 是否 JSON 美化 | **JSON 美化**（`json.dumps(indent=2)`） | §12.9 | `JSON.stringify(input, null, 2)`（ivanleo Ink 教程） |
| 复制按钮是否 v1 做 | **v1 不做**（终端原生选中+C） | §12.10 | claude-code 无单独 tool result 复制键 |
| 并发 tool card 排序 | **按 `seq`** 线性 | §12.11 | 按 content-block-index 线性展示 |
| markdown 代码块高亮 | **开** Rich `Syntax` | §12.12 | claude-code 固定配色高亮（源码观察；#21034 反映固定配色有"彩虹"风险，v2 评估） |

### §14.1 agent_thinking acceptance criteria（spec-review-adversarial P0 闭环）

**正例测试 `test_thinking_no_markdown`**：输入 `agent_thinking` 文本含 markdown 语法（`# 标题` / `**bold**` / `` `code` ``）→ snapshot 与 raw text（去除 ANSI 转义后）**字符级一致**（仅允许 dim+italic 文本样式包裹）。

**反例测试 `test_message_renders_markdown`**：相同文本走 `agent_message` 路径 → snapshot 与 raw text **不一致**（已渲染为大标题/粗体/代码块）。

两条测试合证 §12.8 裁决落实。

---

**本 draft 完成后下一步**：用户 review →（如通过）开新 phase SPEC（如 `phase-15-render-layer.md`），逐 kind 实施 TUI，先消 DRY 再加富；Web 端等整体重写时照本 draft 实现。

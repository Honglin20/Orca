# Orca Workflow YAML 契约参考

> 这是 `create-workflow` skill 的**知识源**。schema 变了只改本文件，SKILL.md 不重复契约细节。
> 权威实现：`orca/schema/workflow.py`（pydantic，`extra="forbid"`）+ `orca/compile/validator.py`。

## 1. 顶层字段（`Workflow`）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | 是 | 全局唯一标识 |
| `description` | str | 否 | 一两句话说清功能与目的；是 `orca list` 里 tars 选 wf 的语义依据，须与现有 workflow 有明确区别（无区别则问用户区分） |
| `entry` | str | 是 | 入口**节点**名（不能是 parallel 组名） |
| `inputs` | dict[str, InputDef] | 否 | 声明 workflow 输入 |
| `nodes` | list[Node] | 是 | 执行阶段节点（按 `kind` 判别联合） |
| `parallel` | list[ParallelGroup] | 否 | 静态并行组（与节点共享命名空间） |
| `outputs` | dict[str, str] | 否 | `{key: Jinja2 模板}`，终态输出映射 |

> **无 `setup` 字段**（in-session v5 §6.2 删除 setup phase 全栈）。YAML 含 `setup:` 段会被
> pydantic `extra="forbid"` 拒绝（fail loud）。主 session 经 `orca next --output` 直接产 output。

`InputDef`：`type`（`string`/`int`/`boolean`/`list`）、`required`（默认 true）、`default`、`description`。

## 2. 节点 kind

所有节点共享 `name: str`（顶层节点必填且全局唯一）+ `routes: list[Route]`（唯一控制流机制）。

### agent —— `AgentNode`
| 字段 | 默认 | 说明 |
|---|---|---|
| `prompt` | None | 内联短 prompt（Jinja2）。与 `agent` **互斥** |
| `agent` | None | agent 池引用：`agents/<name>.md` 或 `agents/<name>/agent.md`（文件夹） |
| `tools` | None | None=全开；list=白名单。execute 阶段**禁** `ask_user`/`gate` |
| `executor` | `"claude"` | `claude`/`ccr`/`opencode`/`codex` |
| `model` | None | 模型覆盖（如 `"deepseek/deepseek-v4-flash"`） |
| `output_schema` | None | JSON schema；None=自由文本 |
| `retry` | None | `RetryPolicy`（瞬时失败重试） |
| `validator` | None | `ValidatorConfig`（LLM 二次语义校验） |

> `resources_root` 是 compile 物化的 runtime cache，**不是 YAML 字段**，用户写无效。

### script —— `ScriptNode`
`command`（必填，Jinja2 shell）、`parse_json`（默认 false）、`timeout`。输出 `{stdout, stderr, exit_code}`（+`json`）。

### set —— `SetNode`
`values: dict[str, str]`（必填，`{key: Jinja2 表达式}`）。无 token、无 shell，纯求值存输出。

### foreach —— `ForeachNode`
`source`（必填，Jinja2 路径指向上游数组，首段必须是真实节点名）、`item_var`（默认 `item`）、`body`（**只**允许 agent/script）、`max_concurrent`（默认 10，≥1）、`failure_mode`（`fail_fast`/`continue_on_error`/`all_or_nothing`）。输出 `{outputs, errors, count}`。

### wait —— `WaitNode`
`duration`（必填，`"30s"`/`"5m"`/`"2h"`/`"1d"` 或裸秒数）、`reason`、`interruptible`（默认 true）。

### terminate —— `TerminateNode`
`status`（`success`/`failed`，必填）、`reason`、`outputs`。约束：`routes` 必空、不能是 `entry`、不能在 `parallel.branches`。

### （无 gate/dialog/ask_user 节点）
这些是**工具**不是节点 kind。**execute phase agent 禁用 `ask_user`/`gate`**（compile validator 强制，铁律 7：execute phase 永不中断）；Dialog 是 TUI 跑完按 `d` 触发，不在 YAML 契约里。setup phase 已在 in-session v5 §6.2 删除——不再有可挂中断工具的阶段。

### parallel 组（顶层 `parallel:` 列表项，非节点）
`name`（必填）、`branches`（≥2 个节点名，无重复、无组名）、`failure_mode`、`routes`（全分支完成后求值）。组聚合输出可达：`<group>.output.outputs.<branch>.stdout`。

## 3. routes 语义

`Route`：`when`（Jinja2 表达式，无 `{{}}`；None=catch-all 兜底，**必须放最后**）、`to`（节点名/组名/`$end`，必填）、`output`（仅 `to="$end"` 时生效，替换 `workflow.outputs`）。

**首匹配胜出，单指针**：routes 自上而下求值，第一个 `when` 为真的触发，其余跳过。`$end` = 显式终止。要显式失败用 `terminate`（`status: failed`）。`when` 可引用当前节点自身输出，如 `when: "output.json.category == 'A'"`。

## 4. agent MD 格式

两种形态（`AgentResolver` 发现）：
- **文件 agent**：`agents/<name>.md` —— 单 md（无脚本资源时用）。
- **文件夹 agent**：`agents/<name>/agent.md` + `agents/<name>/scripts/<file>`（有脚本资源时用）。
  spawn 时注入 `ORCA_AGENT_RESOURCES` 指向 `<name>/` 文件夹绝对路径，agent 的 Bash 工具据此引用自带脚本。

🔴 **文件夹 agent 三条硬约定**（易踩坑）：
1. 脚本**必须**放 `scripts/` 子目录（`agents/<name>/scripts/x.py`），**不要**平铺到 agent 根目录。
2. `agent.md` 用 **frontmatter**（`description`/`model`/`tools`）+ body prompt 形态，不要纯散文标题。
3. body 里引用脚本**必须**是 `$ORCA_AGENT_RESOURCES/scripts/x.py`（绝对 env 引用），
   **不要**用相对路径 `scripts/x.py`。从 CC/opencode skill 转换时，原相对引用要**重写**成这个。

frontmatter 识别字段：`description`、`model`、`tools`。body 即 prompt（Jinja2，可引用 `{{ inputs.* }}` 和 `{{ <上游节点>.output }}`）。节点用 `agent: <name>` 时 inline `prompt` 留空，resolver 从 MD body 填。

## 5. validate 错误类别（`tars validate <yaml>`）

`validate_workflow`（`validator.py:96`）聚合全部检查一次性抛 `ConfigurationError`（errors 阻断，warnings 不阻断）。主要类别：

- **结构**：节点缺 `name` / 重名（含 parallel 组）；`entry` 不存在或指向组；`route.to` 引用未知目标；`parallel.branches` <2 / 引用未定义节点 / 重复；catch-all route 未放最后；`foreach.source` 首段非真实节点；`terminate` 约束违反。
- **图**：从 `entry` 不可达任何终止节点（死路）；孤儿节点不可达（warning）。
- **模板**：Jinja2 语法错 / 引用未声明根（根必须是节点名 / `workflow` / `inputs`）；引用未声明 input key（warning）。
- **能力**：execute 阶段 agent `tools` 含 `ask_user`/`gate`；profile 校验（executor / output_schema 等）。

此外 pydantic schema 层先拦：`extra="forbid"` 拒未知字段；判别联合拒未知 `kind`；`RetryPolicy.max_attempts≥1`、`ValidatorConfig.criteria` 非空等。

## 6. 正确性 cheatsheet（生成后必过）

1. 有 `name`/`entry`/`nodes`，所有顶层节点命名且全局唯一（含 parallel 组）。
2. `entry` 引用节点非组。
3. 每条可达路径必须终止——`route.to: $end` 或 `terminate`，空 `routes` 是隐式终止。
4. 每个 `route.to`（非 `$end`）必须引用存在的节点/组。
5. catch-all route（`when: null` 或省略 `when`）必须是 `routes` 列表**最后**一条。
6. execute 阶段 agent 的 `tools` 白名单**不放** `ask_user`/`gate`。
7. `terminate` 不作 `entry`、不进 `parallel.branches`、`routes` 为空。
8. `foreach.body` 只能是 agent/script；`source` 首段必须是真实节点名。
9. `AgentNode` 上 `agent:` 与 `prompt:` 互斥。
10. `parallel` 组 `branches`：≥2、全是已定义节点名、无重复、不自路由。
11. Jinja2 必须能解析且引用真实根（节点名 / `workflow` / `inputs`；`when` 里可用 `output`；foreach body 里可用 `item_var`/`index_var`）。
12. **跑 `tars validate <yaml>` 验证**，对聚合 error 列表 fix-loop。

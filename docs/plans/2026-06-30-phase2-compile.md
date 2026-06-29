# 实施计划 —— 阶段 2 compile/ 解析与校验层

- **日期**：2026-06-30
- **SPEC**：[`docs/specs/phase-2-compile.md`](../specs/phase-2-compile.md)（逐字实现 + 见下方两处「裁决」）
- **依赖**：`orca.schema`（已实现）+ pydantic + pyyaml + jinja2
- **范围**：YAML → 校验过的 Workflow。两层校验（结构 pydantic / 语义 compile），**不做执行**。

---

## 1. 文件清单

| 文件 | 职责 |
|---|---|
| `orca/compile/__init__.py` | 导出 `load_workflow` / `ConfigurationError` / `ValidationResult`（`__all__` 仅此三个，对外极简） |
| `orca/compile/validator.py` | `ConfigurationError` + `ValidationResult` + `validate_workflow(wf)` + 8 项 `_check_*` + jinja/图算法 helper |
| `orca/compile/compile/parser.py`→`parser.py` | `load_workflow(path)` + `_load_prompts(wf, base_dir)`（约定加载） |
| `examples/agents/{optimizer,researcher_a,researcher_b}.md` | 让 3 个 examples 的约定加载跑通 |
| `tests/compile/{test_parser,test_validator}.py` + `tests/compile/fixtures/*.yaml` | 解析 + 8 项校验正反例 + 聚合 + warnings |

> compile/ 内部依赖单向：`parser` → `validator`（parser 用 `validate_workflow` + `ConfigurationError`）。零反向依赖：不 import run/exec/events/iface。

## 2. errors / warnings 模型（SPEC §1 逐字）

- `ConfigurationError(Exception)`：`errors: list[str]` + `warnings: list[str]`；`__init__` 调 `_format()` 生成多行中文消息（`❌` / `⚠️`）。
- `ValidationResult`（dataclass）：`errors` / `warnings` + `add_error` / `add_warning` / `raise_if_errors`（有 errors 抛 ConfigurationError，无则返回 warnings）。
- **聚合铁律**：8 个 `_check_*` 全部往同一个 `ValidationResult` 加，最后统一 `raise_if_errors`，**绝不第一个错就抛**（SPEC §1 决策 1-B）。

## 3. parser.py 流程（SPEC §3）

```
load_workflow(path):
  raw = yaml.safe_load(utf-8)            # YAMLError 透传
  try: wf = Workflow(**raw)              # 结构校验
  except ValidationError: 包装成 ConfigurationError([str(e)], [])   # 对外单一错误类型
  _load_prompts(wf, base_dir=path.parent)   # 约定加载（聚合所有缺失文件再抛）
  validate_workflow(wf)                  # 语义校验（内部 raise ConfigurationError）
  return wf
```

- `_load_prompts`：遍历顶层 `AgentNode`，`prompt is None` → 读 `base_dir/agents/<name>.md`（utf-8）。**聚合**所有缺失文件名，一次性抛 ConfigurationError（比 SPEC §3 伪码「首个即抛」更 LLM 友好，符合 §1 聚合精神，记为改进）。
- 校验后每个 agent.prompt 都是确定字符串，run/ 不再管文件加载。
- foreach 的 `body` 是嵌套 AgentNode/ScriptNode：body.prompt 为 None 时**不**触发约定加载（body 无 name，无约定文件路径）——仅顶层 agent 走约定。

## 4. validator.py —— 8 项语义校验（SPEC §4）

只对**顶层 node**校验（foreach `body` 无 name，不参与 ①~⑥；但 body 的模板参与 ⑦，source 参与 ⑧）。

| # | 校验 | 算法 | 错误消息关键词（SPEC §6.3） |
|---|---|---|---|
| ① | name 非空 + 全局唯一 | 收集顶层 name；空 → error；计数>1 → error | 「空」/「重复」 |
| ② | entry 存在 | `wf.entry in names` | 「entry」「不存在」 |
| ③ | after 引用有效 | 每个 after 项 ∈ names | 「after」「不存在」 |
| ④ | routes[].to 有效 | ∈ names ∪ {"$end"} | 「route」「不存在」 |
| ⑤ | after 静态边无环 | Kahn 拓扑（仅 after 边）；剩余节点→有环；再 DFS 取一条环路径拼消息 | 「环」+ 路径 `a → b → a` |
| ⑥ | entry 可达终态 | 见 §5 图模型 | 「死胡同」/「$end」 |
| ⑦ | Jinja2 浅校验 | `meta.find_undeclared_variables`，按字段类型判合法 root（见 §6） | 「引用」「不存在」/ warning「未声明 input」 |
| ⑧ | foreach.source node 存在 | 拆 `.`，首段 ∈ names | 「source」「不存在」 |

## 5. 图模型（⑤ 环检测 + ⑥ 可达性）

**前向边**（控制流）：
- route 边：`N → r.to`（`r.to != "$end"`）
- after 反向边：`X.after ∋ N` ⇒ `N → X`

**⑤ 环检测**：仅 after 边（route 是条件边、回指合法，不参与）。Kahn：对 `A→X (A∈X.after)` 构图算入度，拓扑排序后若有剩余节点 ⇒ 有环；DFS 在剩余子图取一条环写进消息。

**⑥ 可达性（裁决 A，见 §7）**：
- `terminal(N)` = `N` 有 `to="$end"` 的 route **或** `N.routes` 为空（**无 route = 隐式终态**）。
- `can_end(N)` = `terminal(N)` or `∃ M∈succ(N): can_end(M)`（三色 DFS，route 可成环，需环安全）。
- entry 的可达集 R 内任一 `¬can_end` 节点 → error「死胡同」；`¬can_end(entry)` → error（entry 即死胡同）。
- `∉ R` 的节点 → warning「孤立节点，从 entry 不可达」。

## 6. Jinja2 浅校验（⑦）—— 上下文相关合法 root

用 `Environment().parse(tpl)` + `meta.find_undeclared_variables(ast)`（**只 parse 不 render**）。

**待校验模板字段**（裁决 B，见 §7）：`AgentNode.prompt` / `ScriptNode.command` / `SetNode.values[*]` / `Route.when` / `Workflow.outputs[*]` / foreach `body` 的 prompt·command。

**合法 root 集合（按字段上下文）**：
- 默认（prompt/command/set.values/outputs/foreach body）：`node_names ∪ {"workflow"}`
- `Route.when` 额外允许 `output`（当前 node 自身输出，如 `output.exit_code == 0`）
- foreach `body` 额外允许 `{item_var, index_var}`（默认 `item`/`_index`；batch_assess 是 `candidate`）

**判定**：
- undeclared 变量 ∉ 合法 root ∉ jinja 内建白名单（`range/dict/lipsum/cycler/joiner/namespace`）→ **error**「`<field>` 引用了不存在的 node `<var>`」
- AST 里 `workflow.input.<key>`（`Getattr` 链）的 `<key>` ∉ `wf.inputs` → **warning**「引用了未声明的 workflow input `<key>`」
- **不**校验 `.output.field` 字段级（运行时归 run/）。

> `meta.find_undeclared_variables` 是否返回 jinja 内建 global，实测确认；白名单兜底防误报。

## 7. 两处裁决（SPEC 字面 vs 可通过验收，选一个 + 说明 why）

**裁决 A —— 无 route 的节点视为隐式终态。**
SPEC §4⑥ 字面「走到无路可走且无 $end route 的节点 = 死胡同」会误杀 `parallel_research.yaml`（synthesizer）/`batch_assess.yaml`（picker/finder/assessor 均无 route）——而 SPEC §6.2 要求这 3 个 example 必须加载通过。裁决：`routes==[]` ⇒ 隐式可达 $end。理由：sink 节点本就是路径自然终点；使 SPEC 自身 example 自洽。

**裁决 B —— Jinja2 校验覆盖全部模板字段，非仅 prompt/when/outputs。**
SPEC §4⑦ 字面列「prompt / when / outputs」（举例口吻）；但 schema 文档 `ScriptNode.command`/`SetNode.values`/foreach body prompt 同为 Jinja2 模板。裁决：对所有模板字段做同一套浅校验。理由：①「抓 80% typo」的 stated intent；② fail loud（否则 `{{ typo.output.x }}` 在 command 里静默漏到运行时）；③ 同一套规则 DRY 应用。记为对 SPEC §4⑦ 字段列表的有意扩展，非新增校验项类型。

> 注：`parallel_research.yaml` 的 `researcher_b` 是 entry 之外的并行根，按「from entry」模型不可达 → **warning**（不阻止返回，符合 §6.2/§6.4）。

## 8. 测试清单

`tests/compile/test_parser.py`：
- 3 个 example `load_workflow` 通过 + 所有 agent.prompt 为 str（约定加载生效）
- 约定加载：内联 prompt 不读文件；约定 prompt 有文件→加载；无文件→ConfigurationError 精确点名 agent
- pydantic 结构错 → ConfigurationError（包装）；YAML 语法错 → yaml.YAMLError 透传
- ConfigurationError.errors / .warnings 结构

`tests/compile/test_validator.py`（直接调 `validate_workflow`，8 项各正/反例）：
- ① name 空 / 重复 ② entry 不存在 ③ after 引用不存在 ④ route.to 不存在
- ⑤ after 环（含环路径消息）+ route 回指不算环（合法循环）
- ⑥ entry 死胡同（error）+ 孤立节点（warning）+ 隐式终态（无 route 通过）
- ⑦ 引用不存在 node（error）+ 未声明 input（warning）+ foreach body 的 item_var 合法 + when 的 output 合法
- ⑧ foreach.source 引用不存在 node（error）
- **errors 聚合**：一 YAML 多处错 → ConfigurationError.errors 含全部（不止首个）
- warnings 不阻止 `validate_workflow` 返回 / `load_workflow` 返回 Workflow

`tests/compile/fixtures/`：每种错误一个静态 bad YAML（E2E：load_workflow(file) → ConfigurationError），与内联 dict 单测互补。

**目标**：phase 2 新增 ~40 测试，叠加 phase 1 的 50 不回归。

## 9. 验收对齐（SPEC §6）

接口 / 解析 / 8 项校验 / errors 聚合 / warnings / 测试 / 文件 —— 全勾选，release note 逐项标 [x]。

## 10. 不做（后续阶段）

- run/exec/events/iface/gates/mcp —— 后续。
- `.output.field` 字段级、foreach source 字段级/数组性 —— 运行时归 run/（CURRENT.md 记遗留）。
- 不自作主张加 SPEC 没有的**校验项类型**或**公共接口**。

# Release：阶段 2 compile/ 解析与校验层

- **日期**：2026-06-30
- **commit**：`5b5ba06`（feat(compile): phase 2 解析与校验层）
- **计划**：[`docs/plans/2026-06-30-phase2-compile.md`](../plans/2026-06-30-phase2-compile.md)
- **SPEC**：[`docs/specs/phase-2-compile.md`](../specs/phase-2-compile.md)

## 做了什么

实现 `orca/compile/`——schema 与 run 之间的**翻译层**：YAML → 校验过的 Workflow。两层校验，失败 fail loud，绝不放行错误 workflow 到 run/。

| 文件 | 内容 |
|---|---|
| `orca/compile/__init__.py` | 导出 `load_workflow` / `ConfigurationError` / `ValidationResult`（`__all__` 仅此三个，对外极简） |
| `orca/compile/parser.py` | `load_workflow(path)`：读 YAML → Workflow（结构错包装）→ prompt 约定加载 → 语义校验；`_load_prompts` 聚合缺失文件 |
| `orca/compile/validator.py` | `validate_workflow(wf)` + 8 项 `_check_*` + 图/jinja 算法 helper；`ConfigurationError` + `ValidationResult` |
| `examples/agents/{optimizer,researcher_a,researcher_b}.md` | 让 3 个 example 的 prompt 约定加载跑通 |
| `tests/compile/{test_parser,test_validator,conftest}.py` + `fixtures/*.yaml` ×11 | 解析 + 8 项校验正反例 + 聚合 + warnings + E2E |
| `pyproject.toml` | `pyyaml` + `jinja2` 由 dev 提升为**运行时依赖**（compile/ 运行时读 YAML + 解析 jinja meta） |

## 8 项语义校验（SPEC §4，全部实现）

① name 非空+全局唯一 ② entry 存在 ③ after 引用有效 ④ routes[].to 有效（node 或 `$end`）
⑤ after 静态边无环（Kahn 拓扑；routes 条件边回指=合法循环，不校验）
⑥ entry 可达 `$end`（死胡同=error，孤立=warning）⑦ Jinja2 引用浅校验 ⑧ foreach.source node 存在。

errors **聚合**：8 项全跑完往同一 `ValidationResult` 加，最后统一 raise（SPEC §1 决策 1-B，LLM 生成 YAML 多处错一次报全）。

## 设计原则落实

- **依赖铁律**：`grep` 确认 `orca/compile/*.py` 仅 import `orca.schema` + `jinja2` + `pyyaml` + `pydantic` + stdlib，零跨层（run/exec/events/iface）。compile 内部单向：`parser → validator`。
- **对外极简、内部要全**（SPEC §0，学 Conductor）：公共 API 仅 `load_workflow`；8 项校验 + 图算法全藏内部。
- **fail loud + 精确**：每个错误指明哪个 node / 边 / 引用；pydantic `ValidationError` 包装成 `ConfigurationError`（对外单一类型）；YAML 语法错透传 `yaml.YAMLError`；模板语法错当校验错误报。
- **鲁棒性**：边界全显式处理——空 workflow / 自环 after(`a→a`) / after 重复项 / foreach 空 source / route 空目标 / `workflow.input['x']` 订阅写法 —— 全部 fail loud 成 `ConfigurationError`，无裸异常（review 验证）。
- **OCP**：加新校验项 = 加一个 `_check_*` 并在 `validate_workflow` 注册一行；零核心路径改动。

## 两处裁决（SPEC 字面 vs 可通过验收，选一个 + 说明 why）

**裁决 A —— 无 route 的节点视为隐式终态。**
SPEC §4⑥ 字面「走到无路可走且无 `$end` route = 死胡同」会误杀 `parallel_research.yaml`(synthesizer) / `batch_assess.yaml`(finder/assessor/picker 均无 route)，而 SPEC §6.2 要求这 3 个 example 必须 `load_workflow` 通过——直接冲突。裁决：`routes==[]` ⇒ 隐式可达 `$end`。理由：sink 节点本是路径自然终点；使 SPEC 自身 example 自洽。**副作用**：`parallel_research` 的 `researcher_b`（entry 之外的并行根，从 entry 不可达）产生一条孤立 warning（不阻止返回，符合 §6.2/§6.4）。

**裁决 B —— Jinja2 校验覆盖全部模板字段。**
SPEC §4⑦ 字面列「prompt / when / outputs」（举例口吻）；但 schema 文档里 `ScriptNode.command` / `SetNode.values` / foreach body prompt 同为 Jinja2 模板。裁决：对所有模板字段做同一套浅校验。理由：① SPEC stated intent「抓 80% typo」；② fail loud（否则 `{{ typo.output.x }}` 在 command 里静默漏到运行时）；③ 同一套规则 DRY 应用。属字段集扩展，非新增校验项类型（不违反 §9）。

**Jinja 实现细节（实测确认）**：
- `meta.find_undeclared_variables` 已自动排除 jinja 内建 global（`range`→`[]`），无需白名单。
- `Route.when` 是**裸表达式**（无 `{{ }}`），parse 前包进 `{{ ... }}`；`when` 上下文额外允许 `output`（当前 node 自身输出，如 `output.exit_code == 0`）。
- foreach body 上下文额外允许 `{item_var, index_var}`（`batch_assess` 是 `candidate`）。
- `workflow.input.<key>` 支持 dotted 与 `['key']` 订阅两种写法（注意 jinja2 `Getitem` 索引字段是 `.arg` 非 `.index`）。

## review 反馈处理（自我 review，code-reviewer agent）

verdict：Approve with 1 must-fix + 3 should-fix。全部处理：

🔴 **must-fix 1（已修）**：`_workflow_input_keys` 的 `Getitem` 分支用了不存在的 `.index`（jinja2 `Getitem` 字段是 `.arg`），`workflow.input['key']` 订阅写法会裸 `AttributeError` 崩溃。修为 `.arg`；新增 `test_jinja_workflow_input_subscription_form_{undeclared,declared}` 回归。

🟡 **should-fix 2（裁决：defer 到 run/，已记 CURRENT.md）**：SPEC §1 列了「routes 全是条件 when、无兜底」warning，未实现。理由：任何静态「无兜底」启发式都会对**枚举穷尽型 router** 误报（如 `nas.yaml` 的 `reviewer`，`decision∈{continue,done}` 必匹配但无静态兜底）。「没有 route 命中」本质是运行时属性；run/（阶段 5）能在运行时精确判「无 route 命中」。SPEC §1 自身措辞也 hedging（「可能…卡住」）。避免给旗舰 example 制造假阳性 warning。

🟡 **should-fix 3（已修）**：⑥ 死胡同原本对路由环每个节点各报一条近义 error。改为合并一条「从 entry 无法到达 $end（死胡同节点：a, b）」，减少噪声。

🟡 should-fix 4 / 🟢 可选项：消息措辞 / `_iter_templates` 的 isinstance 链（OCP 软肋，但集中式校验避免把校验知识塞进纯数据 schema，是正确权衡）/ `_find_cycle_path` 兜底分支——已确认 acceptable，不改。

## 验证结果

- `uv sync` 成功（jinja2 3.1.6 + markupsafe 3.0.3 进 venv）。
- `uv run pytest` → **103 passed**（schema 50 不回归 + compile 53：test_parser 16 / test_validator 37）。
- 3 个 example `load_workflow` 通过，所有顶层 agent.prompt 为确定字符串（约定加载生效）。
- review agent 确认：依赖铁律成立、两处裁决 sound、§6 覆盖矩阵（6.1–6.6）全 yes。

## SPEC §6 验收勾选

- [x] 6.1 接口：`from orca.compile import load_workflow, ConfigurationError`；`load_workflow(nas.yaml)` 返回 Workflow；agent.prompt 全为字符串
- [x] 6.2 解析：3 个 example 全通过；约定加载命中/缺失（→ ConfigurationError）
- [x] 6.3 校验：8 项各正/反例 + 关键词消息
- [x] 6.4 聚合：多处错一次报全；warnings 不阻止返回
- [x] 6.5 测试：test_parser + test_validator，schema 50 不回归
- [x] 6.6 文件：3 个 compile 文件 + tests/compile/ + 11 fixtures

## 未做（后续阶段，不在本阶段范围）

- run/exec/events/iface/gates/mcp —— 后续阶段。
- **运行时校验遗留给 run/**（已记 CURRENT.md）：`.output.field` 字段级存在性/类型；foreach source 字段是否为数组、元素格式；「无 route 命中」死锁检测；路由条件求值。这些归 run/（运行时才知道上下文）。

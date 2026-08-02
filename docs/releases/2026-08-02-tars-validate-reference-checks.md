# tars validate 引用合规校验门（2026-08-02）

> 把 workflow 引用合规校验加进现有 `tars validate`（不新设 `tars lint` 子命令），成为
> create-workflow skill 生成后的强制检验门。背景：终审发现
> `workflows/agent-struct-exploration.yaml:108` 的 `{%raw%}` 被误删 → setup prompt 自引用
> `setup.output.X` → StrictUndefined 渲染崩。现有结构校验抓不到这类「引用合规」bug。

## 做了什么

### 4 项新校验规则（`orca/compile/validator.py`，复用现有 `_ENV` + `_parse_for_meta` + `ValidationResult` 聚合）

1. **`_check_self_reference`（error）** — 节点 `prompt`/`command`/`values`（含 foreach body）
   引用 `<self>.output[.X]` → error。这些字段在节点跑之前渲染，render context 只含上游
   `ctx.outputs`，自身未产出 → 必 UndefinedError 崩。`route.when`/`route.output`/
   `workflow.outputs` 在节点跑后评估，引用自身 output 合法（不报）。
   - **`{%raw%}` 免疫**：Jinja2 parse 时 raw 内容记为 `Const`/`TemplateData` 文本节点，
     不进 `find_all(Getattr/Getitem)` 的 ref 集合 → raw 包裹的自引用提及天然不被报。
     （前置分析结论实证：复刻 struct yaml 真实场景的测试 `test_raw_wrap_verified_in_original_struct_scenario`）
2. **`_check_output_schema_field_alignment`（error）** — 模板引用 `{{ X.output.foo }}`，
   `X.output_schema` 设了 `additionalProperties: false`，但 `foo` 不在 `properties` → error。
   跳过：X 无 strict schema / schema 未关 additionalProperties / `field=None`（整段引用）。
3. **`_check_folder_agent_scripts_exist`（error）** — 文件夹/文件 agent 的 prompt（resolver
   物化的 body）引用 `$ORCA_AGENT_RESOURCES/scripts/<file>` 但 `<resources_root>/scripts/<file>`
   不存在 → error。内联 prompt（`resources_root=None`）不查。
4. **`_check_input_tier_labels`（warning）** — `inputs.<name>.description` 不以
   `[ask]`/`[infer]`/`[default]`/`[advanced]` 起头 → warning（contract §6 强制标签）。

新 helper：`_output_field_refs(ast)` 走 Jinja2 AST 提取所有 `<X>.output[.<field>]` 一级引用
（覆盖 dotted + subscript 共 4 种字面变体）；`_iter_templates` 重构为 5 元组新增 `self_name`
（预渲染字段 = 节点名；评估期字段 = None）。

### Warnings 上浮（修 pre-existing silent-warning bug）

`parser.load_workflow` 原本丢弃 `validate_workflow` 返回的 warnings → CLI `validate` 永远
看不到非阻断警告。新增 `load_workflow_with_warnings(path) -> tuple[Workflow, list[str]]`（单一
真相源，`load_workflow` 委托并弃 warnings），CLI `validate` 改用它并把 warnings echo 到 stderr。
errors 仍走 `ConfigurationError`（exit 非 0），warnings 非阻断（exit 0）。

### create-workflow skill 检验门更新

- `orca/skills/create-workflow/SKILL.md`「产出过程」第 3 步补一段说明 `tars validate` 现含的
  4 项引用合规校验。
- `reference/orca-workflow-contract.md` §5（validate 错误类别）补「引用合规深度校验」+
  「input 三档标签」两个新类别。

## 偏离说明（Rule 7）

**任务 spec 写了 ScriptNode + parse_json 跳过分支，但实现未保留**：spec 要求"X 是 ScriptNode
且 parse_json=True 且字段链是 output.json.<X>（运行时解析）→ 跳过"。但 ScriptNode schema 字段
不在 `orca/schema/workflow.py`（仅 AgentNode 有 `output_schema`）→ ScriptNode 根本不进
`schema_map`，跳过发生在"无 strict schema"分支，spec 要求的"json 字段特殊豁免"是 vacuous。
两路 code-reviewer 一致判定原实现里的 `is_script_json and field == "json"` 是不可达死代码
（违反 KISS/YAGNI）。**选 KISS**：删死代码 + 简化 `schema_map` 为 `dict[str, dict]`（不再
带 parse_json flag）。spec 意图（ScriptNode 引用不报字段对齐）仍满足——通过 `schema_map`
不收 ScriptNode 实现。测试 `test_output_schema_script_node_skipped_no_schema` 锁定真实跳过路径。

## 验证（WSL `.venv/bin/python`）

- **Baseline**：新规则加入前，9 个 `workflows/*.yaml` 全部 PASS（结构校验过）。新规则加入后，
  **0 新 error / 0 新 warning**（所有 input 已三档标签化、所有 folder agent 脚本直接引用存在、
  struct yaml 的 `{%raw%}` 修复后自引用检测 0 命中）。基线完全保留，无回归。
- **测试**：`tests/compile/test_validator.py` 99 → 112 passed（+13 新测试覆盖每项新规则正反例
  + foreach body 两侧 + 聚合 + baseline 扫描 + `{%raw%}` 免疫 + 边界）。
  `tests/compile/` 全套 167 passed（含 parser / fixtures）。
- **CLI warning 上浮**：构造含未标 label 的 input → `tars validate` stderr 显示 warning，exit 0。
- **code-reviewer**（两路并行）：代码 review + 测试覆盖 review。所有 🔴 must-fix 已修：
  - 死代码移除（validator.py 原 `is_script_json and field=="json"`）。
  - foreach body 自引用 + foreach body folder agent 脚本缺失测试补齐（实现意图原本零覆盖）。
  - 测试名实不符纠正（script parse_json skip / additional_properties_true）。
  - 新规则 + ⑦ 错误聚合测试补齐。
  - 🟡 建议项跟进：基线扫描扩到全部 `workflows/*.yaml`（端到端锁定零误报）；
    空 description + 第二行 label 边界测试；`_output_field_refs` 双重发射 docstring；
    regex docstring 措辞修正。

## 已知限制（可接受）

- **scripts 检查不递归二级文件**：folder agent 的 `agent.md` body 指向 `SKILL.md`，`SKILL.md`
  再引用脚本的场景不做静态分析（递归 brittle）。例如 `teacher-gen/agent.md` → `SKILL.md` →
  `$ORCA_AGENT_RESOURCES/scripts/validate_contract.py`（teacher-gen 无此脚本）—— 间接引用 bug
  不被抓。静态校验只覆盖 prompt（resolver 物化的 body）直接引用。
- **scripts 正则非 AST 感知**：`{% raw %}` 包裹的文档化示例（`示例：$ORCA_AGENT_RESOURCES/scripts/example.py`）
  也会被检。folder agent prompt 实际不会用 raw 包裹真实脚本路径，可接受。
- **字段对齐只一级**：`{{ X.output.foo.bar }}` 只校验 `foo`；嵌套 schema brittle 易误报，留给运行时。

## 文件清单

- `orca/compile/validator.py`（4 项新 `_check_*` + `_output_field_refs` + `_iter_templates` 5 元组重构）
- `orca/compile/parser.py`（`load_workflow_with_warnings` 单一真相源）
- `orca/compile/__init__.py`（导出新 API）
- `orca/iface/cli/commands.py`（`validate` 命令 warnings 上浮）
- `tests/compile/test_validator.py`（+13 新测试）
- `orca/skills/create-workflow/SKILL.md`（检验门说明）
- `orca/skills/create-workflow/reference/orca-workflow-contract.md`（§5 错误类别）
- `docs/status/CHANGELOG.md` / `docs/status/CURRENT.md`

## Commit

`<待补>`

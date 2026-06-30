# Release：阶段 1 schema/ 数据层

- **日期**：2026-06-29
- **commit**：`d69c47c`（feat(schema): phase 1 data layer）
- **计划**：[`docs/plans/2026-06-29-phase1-schema.md`](../plans/2026-06-29-phase1-schema.md)
- **SPEC**：[`docs/specs/phase-1-schema.md`](../specs/phase-1-schema.md)

## 做了什么

实现 Orca 架构最底层的纯数据结构层 `orca/schema/`（回答「跑什么 / 产出了什么 / 现在到哪了」三个第一性问题）：

| 文件 | 内容 |
|---|---|
| `orca/schema/workflow.py` | `Workflow` / `InputDef` / `Route` / `Node`（基类）/ 4 个 kind 子类（`AgentNode`/`ScriptNode`/`SetNode`/`ForeachNode`）/ 判别联合 `AnnotatedNode` + `ForeachBody` |
| `orca/schema/event.py` | `Event` / `EventType`（Literal 联合体，21 个值） |
| `orca/schema/state.py` | `RunState` / `Status` / `UsageSummary`（递归 `node_breakdown`） |
| `orca/schema/__init__.py` | 导出 `__all__`（SPEC §5） |

配套：`orca/__init__.py`（顶层包）、`pyproject.toml`（uv + hatchling，运行时仅 `pydantic>=2.0`，pytest/pyyaml 仅 dev 依赖）、`.gitignore`、3 个 examples yaml、3 个 schema 测试文件。

## 设计原则落实

- **依赖铁律**：schema 是最底层，零逻辑零依赖（除 pydantic）。`grep` 确认 `orca/schema/*.py` 仅 import `typing` + `pydantic`，无任何跨层（compile/run/exec/events）引用。
- **OCP / 可扩展**：新 node kind 靠加子类 + 进判别联合，零核心改动；`custom` 事件 + `data.kind` 让前端新渲染类型零核心改动。
- **fail loud**：所有模型 `extra="forbid"`；判别联合对未知/缺失 `kind` 报错；foreach `body` 仅允许 agent/script，拒绝 set/foreach。
- **DRY**：`ForeachBody` 与 `AnnotatedNode` 共用 `Annotated[Union, Field(discriminator)]` 模式（成员集不同，不可合并，已确认非重复）。

## 偏离 SPEC 处（三处，均有理由）

1. **`Node.name` 改为可选 `name: str = ""`**
   SPEC §2.2 画的是 `name: str`（必填），但 §6.3 的 foreach `body` 无 name，而验收 7.4 要求 `batch_assess.yaml` 能解析成 Workflow —— 二者直接冲突。
   裁决：让 name 可选。依据 §2.4 明文「name 全局唯一/存在性 = compile/ 层校验」+ §0「schema 零校验」。**这是刻意推迟到 compile/ 的检查，不是漏洞**；后续 compile/ 阶段须强制顶层 node 非空 + 全局唯一（已在计划中标注，勿在本层补 validator，否则破坏 foreach body 无名场景）。

2. **EventType 实为 21 个（非 prose 的 25）**
   以 §3.2 的 Literal **代码块**（真契约）为准逐字实现 21 个；§7.3/§8 prose 的「25」为 SPEC 笔误。
   测试 `test_event_type_count_matches_spec_literal` 显式断言 21，防后续误删/误增而不自知。**建议后续修正 SPEC §7.3/§8 prose 为 21**（docs 任务，非代码）。

3. **foreach `body` 用判别联合 `ForeachBody`**
   SPEC 写 `body: AgentNode | ScriptNode`（裸 Union）。改为 `Annotated[Union[AgentNode, ScriptNode], Field(discriminator="kind")]`，与 `AnnotatedNode` 同机制：确定性分派、未知 kind 报错更清晰。不增字段、不改语义。属改进，非违约。

## review 反馈处理（自我 review，code-reviewer agent）

- ✅ 已修：`InputDef.default` 由 `object` 改回 SPEC 的 `Any`（faithfulness；`object` 会改变 pydantic 校验语义）。
- ✅ 已加测试：`test_node_status_rejects_completed` —— 反向覆盖 Status（node 级，`done`）绝不容纳 `completed`（workflow 级），防后人误扩 Literal。
- 不改：裸可变默认值 `= []` / `{}` —— 与 SPEC 写法逐字一致，pydantic v2 已正确深拷贝（已测无共享别名）；`Event.data: dict` 无结构 —— 与 SPEC 一致，payload 校验属后续层。

## 二次 review 修复（用户 review 报告，commit `6d7dfea`）

针对用户 4-agent 交叉 review 报告，全部处理（🔴2 必须 + 🟡4 建议；🟢3 仅供了解无需改）：

🔴 必须
1. **SPEC 文档笔误**：EventType 实为 21 个（§3.2 Literal 代码块为准），修正 §7.3/§8/§10 四处 prose 的「25」→「21」（纯文档修复）。
2. **E2E 测试太浅**：原 `test_example_yaml_parses` 只断言 name/entry/len(nodes)。新增 `test_nas_yaml_deep_parse` / `test_batch_assess_yaml_deep_parse` / `test_parallel_research_yaml_deep_parse`，真正证明分派正确性（evaluator→ScriptNode，无名 foreach body→AgentNode）+ inputs/outputs/foreach 专属字段被解析。（phase 5 单轨化后，parallel_research 深解析改断言 `wf.parallel` 组结构，不再断言 after。）

🟡 建议
3. **extra=forbid 走 dict 路径**：新增 `test_extra_forbid_via_dict_path`，覆盖 YAML→dict→Workflow 真实失败模式（非仅直接构造子类）。
4. **Route `to="$end"` 正向测试**：新增 `test_route_to_end_marker`，锁定 §2.2 允许的终态标记，防回归误禁。
5. **UsageSummary 递归走 pydantic 校验**：新增 `test_usage_summary_recursive_from_dict`，从嵌套 dict 构造（非构造后 mutate），真正验证递归 model_rebuild 生效。
6. **ForeachBody 拒绝 foreach**：新增 `test_foreach_body_rejects_foreach`，与 `test_foreach_body_rejects_set` 对称闭合。

🟢 仅供了解（已确认，不改）
- Node.name 可选：唯一可行解，docstring + CURRENT.md 已记录，compile/ 须补校验（遗留项）。
- 可变默认值 `= []`/`{}`：pydantic v2 深拷贝安全；若后续开严格 lint（RUF012/B006）再迁 `default_factory`。
- SDD 流程：计划/release/CHANGELOG/CURRENT/自我 review 全齐。

## 验证结果

- `uv sync` 成功（Python 3.12.13, pydantic 2.13.4, pytest 9.1.1, pyyaml 6.0.3）。
- `uv run pytest` → **50 passed**（test_event 6 / test_state 10 / test_workflow 34）。
- 验收 7.1 import 通过；运行时 `Requires-Dist = ['pydantic>=2.0']`（pytest/pyyaml 不在运行时依赖）。
- 验收 7.4：nas / parallel_research / batch_assess 三个 yaml 经 `yaml.safe_load → Workflow(**data)` 均解析成功。

## SPEC §7 验收勾选

- [x] 7.1 结构：3 文件 + `__init__`，全 pydantic 零逻辑；import 通过；零依赖（除 pydantic v2）
- [x] 7.2 discriminated union：agent/script/set 分派、foreach 从 dict 分派、`kind="nonexistent"` 拒、缺 kind 拒、各 kind `extra="forbid"`
- [x] 7.3 EventType：合法构造、`type="nonexistent"` 拒、21 个 type 全覆盖
- [x] 7.4 端到端：3 个 examples yaml 全部解析成 Workflow
- [x] 7.5 测试：3 个测试文件，全绿（50 passed）
- [x] 7.6 文件：3 个 examples + pyproject（uv+hatchling+pydantic>=2.0+pytest）

## 未做（后续阶段，不在本阶段范围）

compile/（YAML→DAG 解析 + 结构校验，含 name 非空唯一 / entry 存在 / 引用合法 / 无环）、run/、exec/、events/、gates/、iface/ —— 均为后续阶段，本阶段只做数据结构。

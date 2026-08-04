# Release Note — nas-supernet workflow YAML（plan v5 §6/§7/§12）

> 日期：2026-08-04 ｜ Commit: `e02245c` ｜ 分支: `in-session-unified-backend`
> Plan: [`docs/plans/2026-08-04-nas-agent-pipeline-rebuild.md`](../plans/2026-08-04-nas-agent-pipeline-rebuild.md) v5 §15 Phase 5
> 前置：plan §15 Phase 2（3 生成 agent，commit `83552da`）/ Phase 3（3 auto-run + select agent）

## 做了什么

新增 `workflows/nas-supernet.yaml`（298 行，纯增量——零覆盖零删除，不碰现有 `nas-agent-pipeline.yaml` / `nas-hp-search.yaml` 等既有 workflow）。把 7 个已实现的 `ns_*` folder-agent 接成完整 DAG + inputs + output_schema + 路由守卫 + terminate 终态。

### DAG（plan §6）

```
entry: ns_expand_supernet (agent)
   │ model_type_supported != false ──false──► terminate_unsupported (failed)
   ▼ true
ns_train_script (agent) ──► ns_search_pipeline (agent) ──► ns_run_train (agent)
   ──► ns_run_search (agent) ──► ns_select (agent)
   │ selected_arch truthy AND pareto_size > 0 ──false──► terminate_select_failed (failed)
   ▼ true
ns_retrain (agent) ──► $end
```

注：本阶段不含 `ns_visualize`（plan §16 / Task 9，核心逻辑审查后再加）；yaml 以 `ns_retrain → $end` 收尾。

### inputs（plan §12，三档标签 contract §6）

| input | type | required | tier | default |
|---|---|---|---|---|
| `user_project_root` | string | true | [ask] | — |
| `model_path` | string | true | [ask] | — |
| `target_latency_ms` | number | true | [ask] | — |
| `latency_script_path` | string | false | [advanced] | `""` |
| `seed` | int | false | [default] | `0` |

### 路由守卫

- **`ns_expand_supernet`**：`when: "ns_expand_supernet.output.model_type_supported != false"` → `ns_train_script`；catch-all → `terminate_unsupported`。字段名逐字对齐 `ns_expand_supernet/agent.md` 实际 output 字段（非 `supported` / `model_type != "unsupported"`）。
- **`ns_select`**：`when: "ns_select.output.selected_arch and ns_select.output.pareto_size > 0"` → `ns_retrain`；catch-all → `terminate_select_failed`。**plan §4.1 note 关键**：不用 `is defined`（只测键存在，空 dict/null 都过），用 truthiness + `pareto_size > 0` 双条件——四象限（`{}` / `null` / `{"x":1}`+`pareto_size=0` / `{"x":1}`+`pareto_size=12`）全验证正确。

### output_schema

7 agent 节点全 `additionalProperties: false`，逐字对齐各 agent.md `## 输出` JSON 块：

- 生成节点（expand / train_script / search_pipeline）：`output_dir` / `model_type_supported` / `viable` / `evaluation_paradigm` / `*_path` / `fidelity_passed` / `workflow_verifier_passed` / `error` / `generated_artifacts`。
- auto-run 节点（run_train / run_search / retrain）：`status` / `artifacts` / `assessment` / `max_retries_hit` / `healed_files` / `fidelity_retriggered`。**status enum 按实际产出收窄**：`ns_run_train` 含 `skipped`（agent.md Step 3 python 有 skip 分支：`run_train_supernet.sh` 不存在 → skipped）；`ns_run_search` / `ns_retrain` 仅 `[executed, failed]`（agent.md Step 3 python 无 skip 分支）。
- `ns_select`：`selected_arch` (object|null) / `selected_acc` (number) / `selected_latency_ms` (number) / `pareto_size` (integer, minimum:0) / `select_reason` (enum: max-acc-under-target / pareto-knee / none)。

## 偏离计划 / 决策点

1. **`ns_select.selected_arch` type 用 `["object", "null"]`**（非 plan §7.2 的纯 `<dict>`）：任务指令显式 "允许 object 或 null（无候选时）"。truthiness 路由守卫两种类型同效（`bool({})==False` / `bool(None)==False`），防御性 schema 在 `select_architecture.py` 边界行为变化时仍兼容。code-reviewer MINOR-2 建议收窄到 `type: object`——按 Rule 7 选任务指令路径。
2. **cascade 设计**（ns_run_train / ns_run_search / ns_retrain 单出边、不分支 status）：plan §6 显式决策——失败信号沿 `status=failed` 字段穿透到 `ns_select` 路由守卫兜底 `terminate_select_failed`。code-reviewer MINOR-3/4 建议加 fail-loud 直通 terminate 省 token——按 plan §6 既定决策保留 cascade。
3. **ns_select YAML 不显式 `tools: [bash]`**：依赖 `parser.py:180-183` 的 merge 规则（`node.tools is None and meta.tools is not None → node.tools = meta.tools`）从 ns_select/agent.md frontmatter `tools: [bash]` 合并。code-reviewer MINOR-5 建议显式重复——按 DRY 不重复。

## 验证

- **YAML 结构**：node + js-yaml parse 通过（9 节点 / 5 inputs / 10 outputs / 全 catch-all 位置正确 / 全 output_schema required ⊆ properties / 全 input 三档标签）。
- **DAG reachability**：manual rule-trace 全部 9 节点可达终态（terminate 节点 routes 空 = 隐式终态；ns_retrain routes 含 $end；ns_select / ns_expand_supernet 两条出边都到 terminal）。
- **Jinja2 引用**：所有 `{{ X.output.foo }}` 的 `foo` ∈ X 的 output_schema properties（含 `ns_retrain/agent.md` 体的 `{{ ns_select.output.selected_arch }}` 跨节点引用 + `ns_select.route.when` 自引用 [合法，route.when 在节点跑后评估]）。
- **`tars validate` 未实跑**：当前 bash shell PATH 无 Python（仅 Windows Store stub）/ uv / py launcher，无法执行 `tars validate workflows/nas-supernet.yaml`。改用 manual rule-trace（覆盖 `_check_*` 9 项静态规则）+ node-based 结构性自检兜底。

## 已知 BLOCKER（不在本次 commit 范围）

**`workflows/agents/ns_select/agent.md:69` 自引用 bug**（pre-existing，plan §15 Phase 4 / Task 4 引入，非本 YAML 引入）：

```
terminate_select_failed。ns_retrain 引用 `{{ ns_select.output.selected_arch }}` 据此生成 retrain 脚本。
```

Jinja2 parser 不识别 markdown 反引号 → `{{ ns_select.output.selected_arch }}` 进 AST 作真实表达式。AgentResolver 物化 `ns_select.prompt = body` → `_check_self_reference`（`validator.py:770-795`）判 self-ref error → `tars validate` 在 ns_select 处崩。

**修复（一行级）**：反引号内套 `{% raw %}...{% endraw %}`（contract §5 免疫模式）：

```diff
- terminate_select_failed。ns_retrain 引用 `{{ ns_select.output.selected_arch }}` 据此生成 retrain
+ terminate_select_failed。ns_retrain 引用 `{% raw %}{{ ns_select.output.selected_arch }}{% endraw %}` 据此生成 retrain
```

按任务约束「纯增量——不碰现有 workflow/agent」，本 commit 不能改 agent.md。需 follow-up 一行级修补后 `tars validate` 方可真正闭环。

## Commit

- `e02245c` feat(workflow): nas-supernet workflow YAML——DAG + inputs + select 契约（1 file changed, 298 insertions）

## 文件清单

- NEW `workflows/nas-supernet.yaml`（298 行）

# Release — nas-supernet 生成节点 agent 三件（expand / train-script / search-pipeline）

> 日期：2026-08-04 ｜ commit：`83552da` ｜ 分支：`in-session-unified-backend`
> 计划：[`docs/plans/2026-08-04-nas-agent-pipeline-rebuild.md`](../plans/2026-08-04-nas-agent-pipeline-rebuild.md) v5（spec-review PASS）§7.1 / §9.1 / §10
> 阶段：plan §15 stage 2（迁 3 生成 node agent）

## 一句话

把 nas-agent 的 3 个源 SKILL.md 最小适配成 Orca folder-agent（`ns_expand_supernet` /
`ns_train_script` / `ns_search_pipeline`），references / assets 原样迁移，严格遵循 plan §9.1
八条适配替换 + read+embed subagent 协议。**纯增量零覆盖**——不碰既有 `supernet-train-script` /
`nas-search-pipeline` / `nas-train-runner` / `nas-hp-search.yaml` / `nas-agent-pipeline.yaml`。

## 实际做了什么

### 3 个 folder agent（agent.md + 原样迁移资源）

| 节点 | 源 SKILL | 资源迁移 | 调用 subagent（全名） |
|---|---|---|---|
| `workflows/agents/ns_expand_supernet/` | `expand-to-supernet/SKILL.md` | references（17 文件）+ assets（14 文件，含 supernet_readiness / cv / nlp / telecom optimize_rules） | `supernet-evaluator`、`workflow-verifier`、`memory-verifier` |
| `workflows/agents/ns_train_script/` | `supernet-train-script/SKILL.md` | references（3 文件：evaluation_paradigm + workflows + workflow-checklists） | `project-porter`、`project-fidelity-verifier`、`workflow-verifier`、`memory-verifier` |
| `workflows/agents/ns_search_pipeline/` | `nas-search-pipeline/SKILL.md` | references（9 文件：workflows / workflow-checklists / supernet_workflow_examples / evaluator_training_loop_guide）+ assets（agents_template.md） | `workflow-verifier`、`project-porter`、`project-fidelity-verifier`、`memory-verifier` |

references / assets 全部 `cp -r` 原样迁移，**保 `workflows/` + `workflow-checklists/` 兄弟结构**
（workflow-verifier 据此推导 checklist）。

### plan §9.1 八条替换全部落地

1. `<skill_dir>` → `$ORCA_AGENT_RESOURCES`（agent.md 内禁读 `references/workflow-checklists/` 明示）
2. `<output_dir>` → `$ORCA_ARTIFACTS_DIR`
3. `<user_project_root>` → `{{ inputs.user_project_root }}`；`<nas_agent_root>` Python probe bash 块保留（cwd 是产物目录非项目根）
4. `Explore` 子 agent → 退化 Read/Grep/Bash 直接探（3 份首次探源处均显式标注「原 `Explore` 子 agent 退化——opencode host 内无等价只读子 agent」）
5. **A/B consent + ask-user sentinel 全删**：
   - ns_expand Step 2 / 3：optional ruleset + A/B consent 全跳过，仅施加 mandatory supernet readiness（plan §9.1 rule 5）
   - ns_expand Step 6：user feedback loop 退化为 agent 自决
   - ns_expand Step 7.3 + ns_train Step 3.3：present next steps / new session 收尾删
   - 缺关键信息 → fail loud（`error` 字段写明缺哪个）或文档化假设
6. 「present next steps / new session」收尾语 → 删
7. **subagent read+embed 协议块**：3 份顶部「## Subagent 调用协议（read+embed）」段，含 `cat $HOME/.orca/nas-supernet/subagents/<name>.md` + `Task(subagent_type=<host 内置通用>, prompt=<body> + <任务+inputs>)`；**fresh-Task loop（verifier / evaluator / 多轮 porter 都适用）**：每轮 fresh Task 须 embed `<body> + <任务+inputs> + <上一轮完整 verifier report> + Fixed:[ids]/Context:[id]`（fresh Task 无记忆，禁静默推翻 verifier）。正文调用处以「按协议调 `<全名>`，inputs=…」引用。
8. **todolist 适配**：仅 ns_expand 源有 todolist（「Before starting, use the todolist tool」）→ 退化「回复中维护 markdown 编号清单（1–7）」；ns_train_script / ns_search_pipeline 源无 → 不新增

### subagent 全名使用（plan §7.3 / §14 + 4.3 note）

5 个全名贯穿 3 份 agent.md：`supernet-evaluator` / `workflow-verifier` / `memory-verifier` /
`project-porter` / `project-fidelity-verifier`。grep 全文未发现任何简写（`evaluator` / `porter` /
`fidelity-verifier` / `wf-verifier` / `mem-verifier`）。

各 agent 调用集合匹配 plan §7.1：

- ns_expand_supernet: supernet-evaluator + workflow-verifier + memory-verifier
- ns_train_script: project-porter + project-fidelity-verifier + workflow-verifier + memory-verifier
- ns_search_pipeline: workflow-verifier + project-porter + project-fidelity-verifier + memory-verifier

### ns_search_pipeline 特有规则（plan §10 + §7.2）

- **时延分支（§10 / B2 闭环）**：未提供 `{{ inputs.latency_script_path }}`（默认）→ nas-agent 内置
  PyTorch `measure_module_latency(subnet, dummy_input, ...)` @ `nas-agent/nas_agent/latency/pytorch_latency_utils.py:94`
  （**非 onnx**）；提供 → 包装用户脚本（onnx 单文件禁 `.data`，用
  `onnx.save_model(save_as_external_data=False)` 而非 `torch.onnx.export(external_data=)`；用户脚本契约：
  入参 onnx 路径、stdout 末行 / 返回值=ms、exit 0，非 0 → latency_estimator raise fail loud；
  dummy_input 构造 = latency_estimator 责任；IO 张量名 / shape / dtype 不匹配由 latency_estimator 适配
  禁改用户脚本）。MNIST E2E 走默认 PyTorch。
- **select_architecture.py 生成（§7.2，本节点增量产物）**：CLI 契约
  `python3 "$ORCA_ARTIFACTS_DIR/select_architecture.py" --target-latency-ms N --search-results search_results.jsonl`
  （`$ORCA_ARTIFACTS_DIR` 经 Git Bash 展开，N1 闭环）；stdout 单行 JSON
  `{selected_arch, selected_acc, selected_latency_ms, pareto_size, select_reason}`，
  `select_reason ∈ {max-acc-under-target, pareto-knee}`；无候选 → emit 空 selected_arch +
  `select_reason:"none"` **或** exit≠0 fail loud（禁静默选超 target）；命名权威 `search_results.jsonl`
  （与 ns_run_search 写出 / ns_select 读取一致）。下游 ns_select folder agent 确定性 Bash 调用此脚本。
- **Non-Searchable Logic**：latency estimator 须 freeze data-dependent convergence loop（嵌套函数测单次 iteration）
- **NPU foreach=False**：optimizer constructor + `clip_grad_norm_` 两处都传 `foreach=False if is_npu else None`；
  `is_npu = device.type == "npu"` 一次性确定复用

### output_schema（plan §2.3 tape 审计字段）

3 节点 output_schema 都含：
- `fidelity_passed: bool` + `workflow_verifier_passed: bool`（tape 可查）
- `error: string`（fail loud 时写根因；成功→空串）。命名 `error` 而非 Orca runner 惯例的 `last_error`：
  本节点无 self-heal 重试，"last" 语义不适用（**明确偏离 `nas-train-runner` 的 `last_error`，理由文档化**
  —— generation 节点无 retry，rule 7 surface conflict）
- 各自产物路径字段（supernet_path / train_script_path / latency_estimator_path / select_architecture_path 等）

vacuous true 语义文档化：
- ns_expand 无 fidelity-verifier 调用 → `fidelity_passed` 恒 true（vacuous——无 porting 即无 fidelity 失败）
- ns_train viability=No → 双 `*_passed` 均 true（vacuous——无 verifier loop 触发）

### 工程惯例

- **path 处理铁律**：3 份强制 `pathlib.Path` / `os.path.*`，禁字符串拼接 / f-string / `+` 拼路径
- **fail loud**：3 份 Validation 加 fix-loop 软约束「单步 ≤3 次；超限 fail loud（error 字段）」，非硬闸门
  （generation 节点 LLM-mediated fix 自带 verifier loop 天然终止，与 auto-run 节点 `max_retries=3` 硬上限不同）
- **英文标识符**：3 份 Guidelines 显式 bullet「生成 Python 变量名 / 函数名 / 类名 / string literal / comment / docstring 用英文」
- **Lazy Loading**：3 份显式段「禁预先读所有 reference / workflow / asset 文件」
- **frontmatter**：`description + tools: [bash, read, write, edit, glob, grep, task]`（与 `nas-train-runner` 风格一致 + 多 `task` 用于 spawn subagent）

## 偏离计划

无显著偏离。唯一设计决策：`error` 字段命名偏离 Orca runner 惯例（`last_error`），已在 agent.md 内
显式 surface conflict 并文档化理由（rule 7）。

## 验证结果

- **code-reviewer 两轮闭环**：
  - 第一轮：5 条反馈（1 must-fix + 2 should-fix + 2 minor）全修。must-fix = ns_train / ns_search
    output_schema 缺 `error` 字段但 instruction 引用它（contract 漂移）。
  - 第二轮：6 条新发现（1 must-fix + 3 should-fix + 2 minor）全修，**第一轮零遗留**。must-fix =
    ns_search Step 2b select_architecture.py test fixture 来源未明示（agent 可能误读不存在的真
    `search_results.jsonl`）。should-fix 包括 ns_expand contract parity（补 Required Inputs 段 +
    error 字段）、search 产物命名内部不一致（`search.jsonl` typo → `search_results.jsonl`）、3 份
    Validation fix-loop 加 ≤3 软约束。
- **grep 静态校验**：3 份 agent.md 全无 `<skill_dir>` / `<output_dir>` / `<user_project_root>` 实际引用
  残留（仅保留解释性 backtick「原 skill 的 `<output_dir>`」可读性提示）；5 个 subagent 全名拼写一致，
  零简写；read+embed 协议块 + `$ORCA_AGENT_RESOURCES` / `$ORCA_ARTIFACTS_DIR` 锚点齐备。
- **未跑**：未触发 `tars validate`（nas-supernet.yaml 是 plan §15 stage 5 的工作，本 stage 仅 3 个 agent）；
  未跑 MNIST E2E（plan §15 stage 6+ 的工作）。

## Commit SHA

- `83552da9dbfee788b700a1ede14d601b3a5c47c1`（短：`83552da`）
- 44 files changed, 6799 insertions(+), 0 deletions（纯新增）
- branch: `in-session-unified-backend`

## 后续

plan §15 后续阶段（不在本 release 范围）：
- stage 3: `ns_run_train` / `ns_run_search` / `ns_retrain` auto-run agent（§8）
- stage 4: `ns_select` agent + `ns_visualize` agent（§7.2 / §16）
- stage 5: 写 `nas-supernet.yaml`（§6 DAG + §12 inputs + output_schema + routes + select 契约）
- stage 6: `tars validate` + 两级 dry-run
- stage 7-8: 自 review + MNIST E2E

## 相关文件路径（绝对）

- `D:\Projects\Orca\workflows\agents\ns_expand_supernet\agent.md`
- `D:\Projects\Orca\workflows\agents\ns_train_script\agent.md`
- `D:\Projects\Orca\workflows\agents\ns_search_pipeline\agent.md`
- `D:\Projects\Orca\docs\plans\2026-08-04-nas-agent-pipeline-rebuild.md`（契约 / plan v5）
- 源 SKILL（只读对照）：`D:\Projects\nas-agent\.agents\skills\{expand-to-supernet,supernet-train-script,nas-search-pipeline}\SKILL.md`

# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 状态（2026-07-22）

- 🔄 **Workflow 全面重设计**（进行中，[计划](../plans/2026-07-21-workflow-redesign.md)）：8 workflow 审计 → 三根因（A 造假数据/B IN-SESSION 无法问用户/C render_chart 无轴标签）+ DAG 过度拆分 + 产物目录混乱。已冻结：[input 三档原则](../specs/workflow-input-design-principle.md) + [ask-user 哨兵契约 Arch 1](../specs/agent-ask-user-sentinel.md)（TARS 层拦截，引擎零改动，opencode task_id 恢复已验证）。 coder 拆解 9 包（P1-P9），批1 = P1(render_chart 轴标签)+P2(修路径拼接)+P3(0-b spike)。
  - ✅ **P1 完成**（`a7de596`）：`render_chart` 加 `x_label/y_label/caption` 三参数（单一真相源 ChartPayload，backend/frontend 两端同源）；前端 chartTheme 加 4 个 label helper + 新 `ChartCaption.tsx` 共享小组件，8 widget 全接入；TUI plotext `xlabel`/`ylabel` + 空数据/非空数据两路径保 caption；heatmap 降级把 axis 拼进 hint。向后兼容（旧 tape 无新字段 → 旧行为，color/heatmap 零回归）。code-reviewer 两轮闭环。详见 [release note](../releases/2026-07-21-chart-axis-labels.md)。
  - ✅ **P2 完成**（`e41974f`）：struct/kd setup 节点 output_schema 加显式带尾斜杠字段（snapshots_dir / worktree_root / 等），下游改读字段而非 `{{ output_dir }}<suffix>` 拼接——从源头杜绝兄弟孤儿目录。详见 [release note](../releases/2026-07-21-workflow-path-concat-fix.md)。范围外 7 个 agent.md 同款拼接 + CONTRACTS.md stale 登记给 Phase 3 P7。**注意**：本分支 viz 大修（b820ef1…f516223）已完成 color 字段/KD viz_kd/bit-curve 真 pareto 等，**render_chart 轴标签根因 C 已由 P1 解；剩余 champion-trace 横轴（已加 label 落地作证）/ pareto y=0 / exploration-tree / round-leader 等图表内容根因登记给 Phase 3 P7**。
  - ✅ **P3:0-b 完成**（spike pass）：`tests/spike_ask_user/` 独立 harness（2 节点 workflow + driver + 40 测试含 2 真 claude integration）证明 ask-user 哨兵闭环：strict 识别 → task_id 捕获 → 恢复同一子 agent → 真实 output → `orca next`（哨兵不进引擎，零引擎改动）；MAX_ASK=3 fail loud + 造假检测。产出可复用 `SubagentBackend` ABC + `MockSubagentBackend` + `ClaudeCliBackend`（`claude -p --session-id` + `--resume`，等价 CC SendMessage 的 headless 形态）+ `tars_loop.drive_node/drive_workflow`。code-reviewer 两轮闭环。详见 [release note](../releases/2026-07-21-spike-ask-user-sentinel.md)。
  - ✅ **P4 完成**（`774aa46`）：TARS skill（`orca/skills/tars/SKILL.md`）全量接入哨兵闭环——驱动循环第 2 步加哨兵分支 + 新增「### 哨兵处理」段（SPEC §2 skill 指令投影，spike `drive_node` 6 步控制流逐字翻译）：strict 识别（括号配平 + 魔键，非 substring）→ 捕获 task_id（CC `agentId`/opencode `ses_xxx`）→ 问用户（CC `AskUserQuestion`/opencode 聊天问）→ 恢复**同一**子 agent（CC `SendMessage`/opencode `Task(task_id=)`）→ MAX_ASK=3 fail loud → 真实产出才喂 `orca next`（**哨兵绝不进引擎**，compile validator 铁律 7 不触发，零引擎/workflow/agent.md 改动）。CC 主路径先 ship，opencode 标 experimental。spike 38 测试基线保持绿。code-reviewer 两轮闭环（design + spike-equivalence，无 🔴，2 🟡 + 6 🟢 全修）。详见 [release note](../releases/2026-07-22-tars-skill-ask-user-sentinel.md)。**根因 B（IN-SESSION 无法问用户）的 skill 侧已解；agent.md 哨兵段落（P5/P6/P7）+ opencode in-session E2E 待续**。
  - ✅ **P5 完成**：quant 四 workflow（ptq-sweep / sensitivity / qat / bit-curve）正确性修复。①**删造假**——agent.md 模板「torch.randn 兜底 / 复用 calib 当 eval / 复用 train 当 eval」全删，改 Tier B 契约（读代码找 loader dotted-path → 找不到 fail loud，stderr 明确 + exit 2）；脚本 grep 0 个 `torch.randn`。②**device**——新共享 `_quant_scripts/_device.py`（`resolve_device`/`is_npu_available`/`set_seed`/`move_batch_to_device`/`wrap_forward_with_device`/`add_device_seed_args`/`resolve_device_and_seed`，inline 自 nas-agent 不引跨包依赖）；4 yaml 加 `target_hardware`(Tier A [ask]) + `seed`(默认 0) input；4 脚本加 `--device`/`--seed`、`fp_model.to(device)` + `wrap_forward_with_device`（batch 搬 device 自动做）；NPU 经 `torch.npu.is_available()` 有路径。③**bit-curve bake 改动生效**——`_bake_selected` reload 落盘 state_dict + 重 eval（strict=True 键失配 fail loud），返 `(path, reeval_metric)`；`_check_bake_metric_consistency` 超 tol（相对 1e-4）exit 3；持久化顺序保证 exit(3) 时 summary 与 .pt 一致；bake 失败不阻断曲线产出（N7）。④`output_dir` 默认加 `/<wf-name>/` 子目录防撞；⑤qat 示例数字修正（recovery=after−before，mse 口径负=改善）；⑥sensitivity 补 `--env_file` 对齐 PTQ env 兜底；⑦qat recovery bar / bit-curve pareto 用 P1 轴标签，pareto 标题用 `metric_kind` 替代写死 "Accuracy"。eval_fn_ref 空 → WARN「用 teacher-student mse，精度仅自洽性参考」（SDK 合法默认，非造假）；eval_loader 缺 → fail loud（复用 calib/train 是禁掉的造假口径，code-reviewer Rule 7 surface）。code-reviewer 两轮闭环（impl + coverage 并行）：5 🔴 + 6 🟡 + 8 🟢 全处理（既有 7 类 helper 复制 + 死 required 参数登记给 P9 input slim 同期）。37 新测试 + 110 既有测试无回归；`tars validate` 0 error。详见 [release note](../releases/2026-07-22-quant-workflow-correctness-fix.md)。
  - ✅ **P6 完成**：NAS 系两 workflow 重设计。两 yaml（`nas-agent-pipeline` heavy / `nas-hp-search` slim）补 4 个 [ask] KPI input（target_hardware / latency_constraint / max_rounds / seed）+ `project_root` 下沉给 setup 节点 infer-once（从 model_path 向上走）+ output_schema 向后传（抄 agent-struct family_detect）；heavy **7→5 节点**对齐 slim 确定性护栏（删 viz_describe / LLM evaluator / viz_finalize，viz 内联进 setup、选架构复用 slim `nas-select`）；`train_runner` 加 output_schema `search_records minimum:1` 防假执行；`latency_estimator.py` 构造函数 device 无默认（forcing function）；dataset 缺失 fail loud。code-reviewer 一轮提 1 🔴（output_schema vs SKILL Step4 早退契约断裂）+ 3 🟡 全闭环：两 yaml `model_type` 加 `enum: [..., unsupported]` + 条件路由（`when: model_type != 'unsupported'` → train_script_gen；兜底 → $end）短路不烧算力 + 两 setup agent.md 加早退 JSON 分支。`tars validate` 0 error；6 NAS agent.md Jinja2 StrictUndefined 渲染全 OK。详见 [release note](../releases/2026-07-22-nas-workflow-redesign.md)。
  - 待办：P7（struct/kd）/ P8（产物目录）/ P9（input 精简收口）+ 批 3 后统一 headless TARS E2E harness（plan §6）。

- ✅ **`orca open` 跨项目端口占用修复 + `bootstrap` 默认自动开 web**（commit `7d9b7eb` + `9677c1e`）：A 修「7428 被别项目 orca 占 → 静默挂错 tape」（项目指纹复用 + registry + 绝对路径）；B 让 bootstrap 启动即自动开 web（detach `orca open`，stdout 契约零污染，默认开）。spec-review 两轮 + code-reviewer 全闭环；987 passed。详见 [release note](../releases/2026-07-21-orca-open-cross-project-and-bootstrap-auto-open.md) + [CHANGELOG](CHANGELOG.md)。Follow-up：`orca run` reuse 同类隐患（R4）/ `tars serve --runs-dir`（R8）/ 指纹隐私（H5）。
- ✅ **Workflow 可视化全量优化**（7 点，每点独立 agent + 逐 diff 验收，commit `b820ef1`…`f516223`）：前端加 `color` 字段（hue 优先的 per-row 着色）；sensitivity bar 去 hue 改 color、table 改全层；**修 KD 0 图 bug**（viz_round 复用 viz_struct 但 schema 不匹配致 0 图 → 新建 viz_kd.py 4 图 + 改 yaml）；struct 加逐候选表；bit-curve 假 pareto 改真 pareto + 全候选 scatter；ptq-sweep 删无意义 hue + table 补失败行；qat 补训练 loss 曲线。KD 用真实账本 mock 捕获证实修前 0 图→修后 5 图。详见 [release note](../releases/2026-07-21-workflow-viz-overhaul.md)。
- ts_quant 已 editable 装入 conda orca env（实测可用）；待正式加进 orca pyproject 依赖。
- 本地领先 origin 多 commit（push 待用户手动）。

## 待确认（收尾，非阻塞）

- ts_quant 正式进 orca pyproject 依赖（落实"装 orca 即装 ts_quant"）
- 各 workflow 真机 in-session E2E + `orca open` 截真图替换文档 📊 占位（含本次新可视化：sensitivity 统一色 bar / KD 4 图 / qat loss 曲线等）
- 量化 workflow sidecar 脚本是否统一补永久单测（本次按既定惯例未补，用 py_compile/tsc/真实账本 mock 捕获验证；详见 release note「测试策略说明」）

## 并行：in-session 加固（orca 引擎，可穿插）

P5（F1 resume）done。候选 P2（marker 三态）/ P4（失败兜底）/ P6（contract-test），待用户选定。既有 debt/follow-up 全量见 CHANGELOG，SPEC `docs/specs/2026-07-19-in-session-hardening-and-perf.md` v4.1。

## 必读文件（开工前按需）

- [CHANGELOG](CHANGELOG.md)
- `docs/workflows/README.md`（workflow 索引 + 量化 pipeline 顺序）
- `docs/in-session-usage.md`（in-session 安装与使用）
- 可视化契约：`orca/chart/_render.py`（render_chart，含 color + x_label/y_label/caption 字段）+ `orca/iface/web/frontend/src/components/chart/`（8 widget + ChartCaption 共享组件 + chartTheme label helpers）

# 2026-07-21 Workflow 全面重设计计划

> 来源：2026-07-21 对全部 8 个 workflow 的 fan-out 审计（5 个 review agent）。
> 审计原始结论见本会话；三个系统性根因：A 造假数据（quant 契约级）、B IN-SESSION 无法问用户（命门）、C render_chart 无轴标签。
> 本计划是 SDD 流程的「计划」环节，**未确认前不写代码**。

---

## 0. 决策记录（用户 2026-07-21 拍板）

| # | 议题 | 决策 |
|---|---|---|
| 1 | 校准/训练数据 | **必填**。agent 先读用户代码获取 loader/dotted-path；读不到→问用户（机制见 Phase 0-b）；**绝不造假** |
| 2 | ask_user 机制 | **A 档**：不接 MCP、不用自带 ask_user 工具。问-答循环放 TARS↔子 agent 层（SendMessage 恢复、上下文不丢）。**具体逻辑待文末 Q2 讨论确认后冻结** |
| 3 | render_chart 轴标签 | **同意**做平台改动：加 `x_label/y_label/caption` |
| 4 | structure_gate | agent-struct + kd-nas **两个都删** |
| 5 | kd trainer+measure_student | **合并为 candidate_eval 并改 latency-first**（先测时延→不达标不训练→通过才短训） |
| 6 | analyst + curator | **合并**（过→下个节点，不过→分析） |
| 7 | latency_provider | **作为 workflow input**：用户提供则**必须用**用户脚本，未提供才用默认；agent 据此 input 找脚本，杜绝「后面 agent 又退回默认」。用户脚本出错是用户的事。**ONNX 导出不能伴 `.data`，需可设置** |
| 8 | bit-curve bake | bake 后**强制 reload + 重 eval 对账**，超 tol fail loud |
| 9 | device | quant 系**抄 NAS 的 `resolve_device`**（`--device auto [cuda,npu,cpu]` + NPU `foreach=False`） |
| 10 | 产物目录 | 由我定 → 顺序 **B（修拼接 BUG）→ A（引擎注入 `$ORCA_ARTIFACTS_DIR`）→ C（`orca gc`）**，D（schema worktree 字段）列为可选 |

**新增需求（#1/#7 衍生）**：
- 数据获取三级优先：读用户代码 > 问用户 > （无）fail loud。**永远没有「造假」这一档**。
- ONNX 导出 `export_onnx.py` 加 `--no-external-data`（或 `--external-data`）开关，默认不产生 `.data` 伴生文件。

---

## 1. 总体顺序与依赖

```
Phase 0（平台前置）
  0-a render_chart 轴标签  ──┐
  0-b IN-SESSION ask-user   │  ⚠️ 0-b 待 Q2 讨论冻结
                            │
Phase 1（quant 系）─────────┤  数据必填 + device + bit-curve re-eval
Phase 2（NAS 系）───────────┤  补顶层输入契约 + heavy→slim 对齐
Phase 3（struct/kd）────────┤  精简 DAG + latency-first + device + latency_provider
Phase 4（产物目录）─────────┘  B→A→C
```

- Phase 0-a / 1 / 2 / 3 / 4 互相基本独立，可并行。
- **0-b 先做 spike**（claude 后端最小闭环），spike pass 才开 Phase 1/2/3 的 ask-user 落地。0-a、Phase 4-B 可与 spike 并行。
- Phase 1/2 里「缺数据」走 input 原则 Tier B（`[infer]` + 哨兵，**非 required:true input**）：0-b 落地前 = 读不到就 fail loud（绝不造假）；0-b 到位后 = 哨兵问用户。见 `docs/specs/workflow-input-design-principle.md`。
- Phase 3 的图表修复依赖 0-a（否则只能 workaround 字段名）。

---

## Phase 0：平台前置

### 0-a render_chart 加轴标签（根因 C）

**目标**：`orca.chart.render_chart` 支持 `x_label/y_label/caption`，前端用其渲染；一次改全 workflow 图表可读性受益。

**改动**：
- `orca/chart/_render.py:36-201`：签名加 `x_label=""`, `y_label=""`, `caption=""`，透传进 ChartPayload。
- `orca/chart/_validate.py`：放行新字段（如需）。
- ChartPayload schema（tape 落盘格式）加三字段。
- 前端 `orca/iface/web/frontend/src/components/chart/ChartRenderer.tsx` + TUI `orca/iface/cli/widgets/chart_{panel,canvas}.py` + `screens/chart_browser.py`：用 `x_label/y_label` 替代「字段名当轴标签」，`caption` 渲染为图下小字说明。

**验收**：任一 workflow 传 `x_label="latency (ms)"` 后，前端/TUI 轴标签显示该值而非字段名。

### 0-b IN-SESSION ask-user 通道（根因 B）— 已冻结 Arch 1（契约见 specs/agent-ask-user-sentinel.md）

**目标**：子 agent 缺必填数据时，能问用户、拿到答案后**继续**任务（不重头）。

**预定方案（A 档，TARS 层循环，详见文末 Q2）**：
- 不接 MCP server，不用 `mcp__orca-agent-tools__ask_user`，不改 compile validator。
- 子 agent prompt 约定缺数据时返回哨兵 `{"_orca_ask_user": "...", "options": [...], "context": "..."}`。
- TARS skill 检测哨兵 → 主 session 调 AskUserQuestion → SendMessage 恢复同一子 agent（上下文不丢）→ 子 agent 继续 → 返回最终 output → TARS `orca next --output` 推进。
- **Orca 引擎代码零改动**（Arch 1：哨兵在 TARS 层拦截、绝不进 `orca next`，故 `output_schema` 校验只作用在真实 output，无 schema mismatch）。改动全在 `orca/skills/tars/SKILL.md` + 各 agent.md。**2026-07-21 已验证**：opencode 1.18.3 原生 `task_id` 恢复保上下文，CC `SendMessage` 同效，A 档跨后端可行。

**已冻结**（U1=Arch1 / U2=infer+哨兵 / U3=spike 先行 / U4=不合并字段）。失败路径（重入≥3 fail loud）、跨后端差异、E2E 判据见 sentinel SPEC。**必须先做 spike**（§1）才开 Phase 1/2/3 的 ask-user。

---

## Phase 1：quant 系（ptq / sensitivity / qat / bit-curve）

**共性模板**（四个 workflow 统一）：
1. YAML：数据相关 input 改 `required: true`，删「假随机」描述；加 `device` input（默认空→脚本 `resolve_device`）；`output_dir` 默认加 `/<wf-name>/` 子目录防撞名；加 `latency_provider` input（若有 latency 测量；用户脚本优先）。
2. agent.md：删所有 `torch.randn`/「兜底假随机」/「复用 calib 当 eval」段；改为「先读用户代码找 loader dotted-path → 找不到返回哨兵问用户 → 绝不造假」；`load_model()` 段加 device 约定（抄 `resolve_device`）。
3. 脚本：加 `--device`；清理「required 占位但从不消费」的死参数。

### 1-a quant-ptq-sweep
- `quant-ptq-sweep.yaml`：`calib_data_ref`→必填；删描述里假随机；`output_dir` 默认 `llm_artifacts/<model>/ptq-sweep/`；加 `device`、`latency_provider` input。
- `agents/ptq-sweeper/agent.md`：删 line 18/33 假随机兜底；三级数据获取；`load_model()` 加 device；加哨兵问用户契约。
- `agents/ptq-sweeper/scripts/run_ptq_sweep.py`：清死参数 `--calib_data_ref/--eval_data_ref/--eval_fn_ref/--project_root`（仅 agent 生成期消费，运行时不用）；加 `--device`。
- 图表（待 0-a）：lightweight line 图 x=step_idx 无语义 → 加 caption 解释 step。

### 1-b quant-sensitivity
- 同 PTQ 模板：`calib_data_ref`→必填；`output_dir` 加 `sensitivity/`。
- **关键对齐**：`run_sensitivity.py` 补 `--env_file` 参数 + agent.md 补 `source orca_env.sh`（抄 PTQ 已踩过的坑，否则 opencode bash 拆调用丢 `ORCA_CHART_SOCK` → 推图静默失败）。
- 图表：rank 空字段渲染瑕疵（cosmetic）。

### 1-c quant-qat
- YAML：`train_data_ref`→必填；`eval_data_ref`→必填且**≠train**（eval=train 是数据泄漏）；删假随机/复用兜底描述；`output_dir` 加 `qat/`。
- agent.md：删 line 22-24 三层 fallback；三级数据获取；device。
- `run_qat.py`：删 `eval_loader = ... else train_loader` fallback（缺失 fail loud）；加 `--device`。
- **图表**：recovery bar 符号修正——mse 口径下 `after<before` 为负=好，标题/方向标注需明示（靠 0-a 的 `caption`/`y_label`）；修正示例数字 `agent.md:79-81`（现自相矛盾）。
- 精简：`cage` 三态→固定 `auto`；`lr`/`total_steps`→合并 `qat_intensity` 枚举（smoke/light/full）。

### 1-d quant-bit-curve
- YAML：`calib_data_ref`→必填；`eval_data_ref`→必填且≠calib；删假随机；`output_dir` 加 `bit-curve/`。
- agent.md：删 line 41 假随机（与 PTQ 逐字同模板）；删 line 22「project_root 推断」（非契约自由发挥）→改为 fail loud/问用户。
- **[改动生效] 核心修复** `run_bit_curve.py:409-448` `_bake_selected`：bake 后**重新 load baked model + 重 eval**，`|baked_metric - final.score| > tol` → fail loud；`best_metric` 改取 baked 实测值而非 search 内部值。
- Pareto 图标题「Accuracy」与 y=mse loss 冲突 → 修正（靠 0-a）。
- 加 `--device`。

**Phase 1 验收**：四个 quant workflow 在缺校准/训练数据时**不再造假**（读不到用户代码→问用户或 fail loud）；`--device cuda` 真在 GPU 跑；bit-curve bake 后 metric 与 baked artifact 对账一致；同模型串跑四个 workflow 产物不互覆。

---

## Phase 2：NAS 系（nas-agent-pipeline / nas-hp-search）

### 2-a nas-hp-search（slim，黄金模板，主要补输入契约）
- YAML 补**必填顶层输入**：`dataset`（train+val 路径）、`latency_constraint`、`target_hardware`（cuda/npu/cpu）、`eval_paradigm`。当前只 required `model_path/project_root/output_dir`，关键输入靠 LLM 从 project_root 推断、无护栏。
- `latency_estimator.py:23` 构造函数默认 `device="cpu"` → 改为强制传参（靠框架 worker 注入，但默认值是隐患）。

### 2-b nas-agent-pipeline（heavy，7→5 节点，向 slim 看齐）
- **删 `viz_describe`**（line 34-40）：`push_describe` 内联进 `pytorch-model-optimizer/agent.md`（slim 已这么做）。
- **删 `viz_finalize`**（line 134-140）：`push_pareto_final`/`push_funnel` 已在 `nas-select/scripts/select_and_report.py:215-216` 内联。
- **`evaluator`（内联 LLM, line 91-127）→ 替换为 `nas-select` 脚本化节点**：选架构 + 写 final_report + 推 C5/C6 全确定性，零 LLM（抄 slim）。
- **`train_runner` 加 `output_schema`**（line 67-73，抄 slim line 71-79 的 `search_records minimum:1`）防假执行。
- 补与 2-a 相同的必填顶层输入。
- 路径产物：`evaluator` 散落 `output_dir/` 根的 `retrain.sh/finetune.sh/final_report.md` → 收进 `runs/retrain/`。

**Phase 2 验收**：heavy 压到 5 节点且确定性护栏与 slim 一致；两 workflow 缺 dataset/latency_constraint 时不靠 LLM 瞎推。

---

## Phase 3：agent-struct + kd-nas 精简、device、latency_provider

### 3-a agent-struct-exploration（11→6 节点，P7 实现：headline 原写 7，bullet 算下是 6，以 6 为准）
- **删 `structure_gate`**（line 210-244）：零判决力，tag 只喂软配额告警。
- **合并 `family_detect`+`baseline_measure`→`setup`**。
- **合并 `analyst`+`curator`+`viz_round`→`curator`**：先 reducer 脚本 deterministic 决策 → 条件性 LLM 归因（失败候选才分析）→ 跑 viz。
- **内联 `viz_finalize`** 进 `finalize`。
- `evaluator` 已是「时延门→训练→判决」三合一 ✓，保留。
- **图表（待 0-a）**：
  - `viz_struct.py:180-191` Pareto y=0 → 过滤 `accuracy is None` 行（FAIL_latency 候选），WARN 日志。
  - 删 Round Ledger（line 243-285，每轮 1 候选无聚合价值）、Exploration Tree（line 210-240，scatter 充数 + path 恒 p1）。
  - Candidate Ledger（line 288-321）拆短字段（`tag`/`family`/`one_line_summary`），长 hypothesis 留 hover；前端列 wrap/expand。
  - Champion Trace（line 157-166）字段名自解释（`latency_ms`/`candidate_idx`）+ caption。
- **device**：`latency_onnxrt.py:56-58` 加 `--device` + 跑完打印实际 providers；`profile_onnx.py:139` 加 `--device`（默认 CPU 但可覆盖）；补 NPU provider（Ascend/CANN）。
- **latency_provider 暴露为 input**（当前固化，违反设计草稿 §5）；用户脚本优先。
- **ONNX 导出** `export_onnx.py` 加 `--no-external-data` 开关（默认不伴 `.data`）。
- **清理**：删 family_detect prompt 第 91 行死目录 `viz/`；删死代码 `run_candidates.py`。
- **device 统一**：struct/kd 抄 NAS `resolve_device`。

### 3-b kd-nas（13→6 节点，P7 实现：headline 原写 7，bullet 算下是 6，以 6 为准）
- **删 `structure_gate`**（line 180-206，节点自认 student 必然 structural）。
- **合并 `teacher_setup`+`profile_gate`+`kd_train_script_gen`→`setup`**。
- **[核心] 合并 `kd_trainer`+`measure_student`→`candidate_eval`，改 latency-first**：先 `build_model()` 默认权重导 ONNX 测 latency → 不达标 FAIL_latency**不训练** → 通过才短训 + 测 proxy_mse。（当前训练在时延测量之前，违反哲学 #2，烧短训算力。latency 是结构属性，不需训练权重。）
- **合并 `analyst`+`curator`+`viz_round`→`curator`**；内联 `viz_finalize` 进 `finalize`。
- **契约对齐**：`kd-curator/agent.md:84`（route_finalize 带 `phase==2` 门）vs `CONTRACTS.md:217`（无 phase 门）→ 选一个统一（倾向 agent.md 的 phase 门合理：Phase1 sweep 完才送 finalize，避免 round 0 烧 50 epochs）。
- **字段对齐**：`kd-hypothesizer/agent.md:95` `rationale_summary` → `rationale`（与 yaml output_schema 一致，否则 fail loud）。
- **图表**：短训阶段 `db_gap`/`met_acc` 为占位（curator 不消费，但 viz 入图误导）→ viz_kd.py round 模式不展示这两列或标 `(deferred)`。
- **device / latency_provider / ONNX**：同 3-a（measure_student.py、teacher_setup.py 透传 device；latency_provider 暴露；export 加 no-.data）。
- **隐性假数据**：`teacher_setup.py:271` teacher_accuracy 解析失败写 `0.0 + confidence=low` 当 baseline → confidence=low 时 fail loud 或图表显式标「teacher_accuracy 未知，dB gap 不可信」。

**Phase 3 验收**：两 workflow 各压到 6 节点（headline 原写 7 是 off-by-one，以 bullet 合并算式为准：struct 11−1(structure_gate)−1(family_detect+baseline_measure→setup)−2(analyst+curator+viz_round→curator)−1(viz_finalize→finalize)=6；kd 同款再 −1(kd_trainer+measure_student→candidate_eval)=6）；kd 的 candidate_eval 严格 latency-first（时延不达标不训练）；图表无 y=0/无标签/无意义表；`--device npu` 有路径；用户提供 latency 脚本时被采用；ONNX 导出可控不伴 `.data`。

---

## Phase 4：产物目录收敛（B→A→C）

### 4-B 修路径拼接漏斜杠 BUG（纯 workflow，先做）
- **根因**：模板 `{{ family_detect.output.output_dir }}.worktrees/` 赌 LLM 输出带尾 `/`，不一致 → 产生 `<run>.worktrees/`（兄弟）vs `<run>/.worktrees/`（子目录）+ `<run>snapshots/`、`<run>viz/` 孤儿目录。证据：`struct-engineer/agent.md:31,37`、`kd-engineer/agent.md:74,89-90`、`kd-nas.yaml:116`。
- **改法**：`family_detect`/`teacher_setup` 的 output_schema 加显式字段 `worktree_root`/`snapshots_dir`/`viz_dir`（计算一次、带尾 `/`），下游用 `{{ ...output.worktree_root }}<candidate>/` 而非字符串拼接。

### 4-A 引擎注入 `$ORCA_ARTIFACTS_DIR`
- `orca/exec/env.py` 加 env var `$ORCA_ARTIFACTS_DIR`（=`runs/<run_id>/artifacts/`，bootstrap 时 mkdir）。
- workflow YAML/agent.md 模板用它替换 `llm_artifacts/<model>/runs/<timestamp>/` → 消除两套 run_id 不合流。

### 4-C `orca gc --max-age 14d`
- 按 tape mtime 扫 `runs/<run_id>.jsonl`，老的删 `runs/<run_id>/{,artifacts/}` + `.jsonl{,.lock,.sock}` + marker。
- worktree 可靠回收需 `artifacts/.worktrees/MANIFEST.json`（记录 add 过的 worktree，gc 时 `git worktree remove`）。

### 4-D（可选，大改）`AgentNode.worktree: Literal[None,"per_candidate","shared"]`
- 引擎托管 worktree 生命周期（含终态自动 remove）。schema 加字段 + exec 加 worktree executor。**列为可选，非本次必须**。

### 顺手清理
- 删历史 run 的 HTML 产物（pre-migration，~4.8MB/个，纯磁盘）。
- 删 `viz_struct.py:381-386` 废弃 `--out_dir` 死参。
- 重复 push 脚本（`nas-viz/scripts/` 与 `elastic_optimizer/scripts/`、`nas-select/scripts/` 同逻辑）→ DRY 抽到共享 `_nas_scripts/`。
- 修 `kd-nas.yaml:62` doc 漂移（`viz_struct` 列在 KD 可复用脚本，实际用 `viz_kd.py`）。

**Phase 4 验收**：同一 run 不再出现孤儿拼接目录；产物归 `$ORCA_ARTIFACTS_DIR` 托盘；`orca gc` 能清老 run + worktree。

---

## 2. 风险与注意

- **0-b 是命门**：在它落地前，Phase 1/2 的「缺数据」只能退回 fail loud。建议 0-b 优先做（引擎改动为零，但 TARS skill + agent 契约要先定 + spike 验证）。
- **opencode 后端**（2026-07-21 已验证解除）：opencode 1.18.3 Task 工具原生 `task_id` 恢复保上下文，CC `SendMessage` 同效。A 档跨后端可行。残留注意：opencode 无原生 AskUserQuestion，结构化选项由 TARS prompt 强制。
- **0-a 前端/TUI 双壳**：ChartRenderer.tsx + TUI widgets 都要改，否则只改后端字段前端不显示。
- **bit-curve re-eval** 会增加一次完整 forward eval，候选多时耗时上升——可接受（正确性优先）。
- **latency-first 重构**（kd）改变短训触发条件，需重跑 E2E 验证收敛行为不变。

## 3. 不在本次范围

- 命令行模式（`orca run` 后端）的 ask_user 已存在，不动。
- 各 workflow 的 LLM 生成 adapter.py 脆弱性（forward_fn 解包 batch 易错）——本次只标记，不展开。
- 新建 workflow。

## 4. 决策更新（Stage-1 综合，2026-07-21）

spec-review + input 辩论 + opencode 验证后冻结（覆盖上文任何模糊处）：
- **U1**：0-b = Arch 1（TARS 层哨兵拦截，引擎零改动）。见 `specs/agent-ask-user-sentinel.md`。
- **U2**：数据类 input（calib/train/eval）= Tier B `[infer]` + 哨兵，**非 required:true input**。见 `specs/workflow-input-design-principle.md`。
- **U3**：0-b spike 先行；0-a、Phase 4-B 并行。
- **U4**：qat 不合并 `qat_intensity`；`lr/total_steps`→`[infer]`、`cage`→`[default]=auto`，移除 input 时同步改 agent.md Jinja。
- **新增**：全部 workflow 加 `seed`（默认 0）；提供 `smoke` 开关（非 smoke 时 lr/epochs 必须 [ask]）。
- **resolve_device**：在 `nas-agent/nas_agent/train/distributed.py:214`（monorepo），**inline 到共享模块** `_struct_scripts/_device.py` + `_kd_scripts/` 复用，不引跨包依赖；区分 onnxruntime provider 与 torch.device 两套语义。
- **struct evaluator vs kd candidate_eval 语义差异**（spec-review N4）：struct evaluator 测 latency+真实 accuracy（按需抢卡全量训），kd candidate_eval 测 latency+proxy_mse（短训，真实精度推迟 finalize）。非同模板复制。
- **bit-curve bake 失败语义**（N7）：bake 失败（返空串）时对账跳过、不阻断曲线产出（保既有语义）；bake 成功才 reload+重 eval 对账，超 tol（相对 1e-4）fail loud。

## 5. Coder 任务拆解（耦合高合做、工作量小合做）

| 包 | 范围 | 依赖 | 并行批 |
|---|---|---|---|
| **P1** | 0-a render_chart 轴标签（chart API + 前端 + TUI） | 无 | 批1 |
| **P2** | Phase 4-B 修路径拼接 BUG（output_schema 显式字段） | 无 | 批1 |
| **P3** | 0-b spike（claude 后端最小闭环：哨兵→SendMessage 恢复） | 无 | 批1（最高优先） |
| **P4** | 0-b 全量（TARS skill 哨兵分支 + 跨后端 task_id 捕获/恢复 + agent.md 契约 snippet） | P3 pass | 批2 |
| **P5** | quant 系四合一（删造假 + Tier B 哨兵 + device 共享模块 + bit-curve re-eval + output_dir 子目录 + qat 数字修正） | P1(图表)/P4(哨兵) | 批3 |
| **P6** | NAS 系（补 KPI inputs + sink project_root + heavy 7→5 + train_runner output_schema） | P1/P4 | 批3 |
| **P7** | struct/kd（11→6 / 13→6 含 latency-first + latency_provider input + device + ONNX no-.data + 图表根因） | P1/P4 | 批3 |
| **P8** | Phase 4-A 引擎注入 `$ORCA_ARTIFACTS_DIR` + 4-C `orca gc` | 无 | 批3 |
| **P9** | input 精简全员（按 input 原则收口各 workflow inputs + 更新 create-workflow-skill） | P5/P6/P7 | 批4 |

每包派一个 coder-agent，自带 review+commit。批内并行、批间按依赖。

## 6. E2E（统一，headless TARS-SKILL，禁 CLI）

- **禁**：`e2e_check.sh` 式手动 `orca next` 循环、`orca run` 后端、`OrcaApp.run_test()` TUI pilot 驱动编排。
- **要**：headless agent（opencode + deepseek-v4-flash，CLAUDE.md 约定）装 TARS skill → 喂用户意图 → TARS 内部 orca list/match/next → 完成 → 断言。
- **当前仓库无 TARS 驱动的 headless harness**（现有 e2e 要么手动 CLI、要么 TUI pilot）→ P4 之后、批3 之前先建此 harness（复用 `tests/exec/claude/*` 与 opencode 子进程 spawn 基建）。
- **断言**：不造假（无 torch.randn 痕迹）、device 真上 GPU/NPU、改动生效（bit-curve baked metric=reported）、图表有轴标签、哨兵机制触发正确、tape 事件序列正确。

## 7. 下一步

1. 批1：P1（0-a）+ P2（4-B）+ P3（0-b spike）并行派 coder-agent。
2. P3 spike pass → P4（0-b 全量）+ 建 headless TARS harness。
3. 批3：P5/P6/P7/P8 并行。
4. 批4：P9 input 收口 + create-workflow-skill。
5. 统一 E2E（§6）→ viz 优化（Stage 4）。
6. 每包：SPEC/草稿（若跨模块）→ 实现 → 自我 review → release note + CHANGELOG + CURRENT。

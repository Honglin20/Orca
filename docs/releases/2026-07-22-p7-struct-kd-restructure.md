# 2026-07-22 P7：struct/kd 精简 + latency-first + 图表根因 + device

> 计划：[`docs/plans/2026-07-21-workflow-redesign.md`](../plans/2026-07-21-workflow-redesign.md) §Phase 3-a / 3-b（P7）
> Commit：`66f74ea`
> code-reviewer 闭环：一轮 review（R1-R4 必修 + M1-M7 中等 + L1-L7 轻微，全修）

## 节点数变化（plan headline off-by-one）

| workflow | 原 | plan headline | 实际（以 bullet 算式为准） | 偏差原因 |
|---|---|---|---|---|
| struct | 11 | 7 | **6** | 11 − 1(structure_gate) − 1(family_detect+baseline_measure→setup) − 2(analyst+curator+viz_round→curator) − 1(viz_finalize→finalize) = 6 |
| kd | 13 | 7 | **6** | 同 struct 再 −1(kd_trainer+measure_student→candidate_eval) + 多合 1(profile_gate / kd_train_script_gen 进 setup)，13 − 7 = 6 |

**Surface-conflict（Rule 7）**：plan 标题与验收段写"11→7 / 13→7"，但详细 bullet 算下是 6。以 bullet 为准（更具体），
plan 标题已就地订正为"11→6 / 13→6"。

## struct workflow 改动（11 → 6 节点）

新节点：`setup` / `hypothesizer` / `engineer` / `evaluator` / `curator` / `finalize`。

- **`family_detect` + `baseline_measure` → `setup`**：探测 project_root/build_fn/dummy_input + 浅层族检测
  + KB 切片缓存 + 调 measure_baseline.py 测 latency/accuracy + seed champions。
- **删 `structure_gate`**：零判决力（tag 只喂软配额告警）。tag/diff_summary 改由 `curator` 内联跑 ast_diff.py
  deterministic 推导（topology/op 变 → structural；纯数值 → hyperparam；其余 mixed）。
- **`analyst` + `viz_round` 折进 `curator`**：单节点职责链 = reducer 脚本 → 失败候选才 LLM 归因 → viz_struct。
  SUCCESS 候选跳过 LLM（省 token，原 analyst 总是跑）。
- **`viz_finalize` 内联进 `finalize`**：champion 重训 + ONNX + final_report + 终态对比 bar 一气呵成。

## kd workflow 改动（13 → 6 节点）

新节点：`setup` / `hypothesizer` / `engineer` / `candidate_eval` / `curator` / `finalize`。

- **`teacher_setup` + `profile_gate` + `kd_train_script_gen` → `setup`**：一次性编排——探测 + 6 层 hint 编辑 +
  训 teacher + teacher_setup.py 缓存 + profile_onnx.py + 套模板生成 train_kd.py。
- **删 `structure_gate`**：kd 候选必然 structural（整族替换）。
- **`kd_trainer` + `measure_student` → `candidate_eval`（核心 latency-first 重构，见下）**。
- **`analyst` + `viz_round` 折进 `curator`**；**`viz_finalize` 内联进 `finalize`**。
- **字段对齐**：kd-hypothesizer `rationale_summary` → `rationale`（对齐 yaml output_schema）。
- **路由契约对齐**：kd-curator `route_finalize = new_champion ∧ met_latency ∧ phase==2`（保留原 agent.md 的 phase 门，
  vs CONTRACTS.md 原版无；以 agent.md 为准——Phase1 sweep 完才送 finalize，避免 round 0 烧 50 epochs）。

## latency-first 怎么实现（kd candidate_eval）

哲学#2：**latency 是结构属性，不需训练权重**。原 kd 流程"短训 → 测 latency"违反之（短训白烧算力换 latency）。
P7 反转顺序，分 Step A/B/C 在 `candidate_eval` 节点 prompt 里硬约束：

1. **Step A：导 student ONNX（默认权重，确定性，不训练）**
   - 调 `measure_student.py`，传 `--student_ckpt ""`（空 → 用 build_model 默认权重导 ONNX）
   - 不传 `--eval_command` / `--eval_dataset`（measure_student 检测此情况进入 latency-only 模式，db_gap=-1 sentinel）
   - 解析 stdout `STUDENT_LATENCY_MS` / `MET_LATENCY` / `STUDENT_ONNX`
   - 失败 → `status=FAIL_export`，跳过 B/C

2. **Step B：时延门（核心护栏）**
   - `latency_ms > target_latency_ms` → **`status=FAIL_latency`，不训练**，跳过 C
   - 通过 → 进 C

3. **Step C：短训 student（仅时延门通过才跑）**
   - 跑 `train_kd.py --epochs short_epochs` → 解析 `KD_PROXY_MSE` / `KD_LOSS_FINAL` / `STUDENT_CKPT`
   - 失败（OOM/NAN）→ `status=FAIL_train`
   - 成功 → `status=SUCCESS`

`measure_student.py` 同步加 **latency-only 模式**（M3 修复）：既无 eval_command 又无 eval_dataset → 跳过 db_gap 计算，
写 `db_gap_deferred=true` 进 measure_report.json，stdout `db_gap=-1`。避免之前白算占位 0.0/unknown 误导 debug。

## 图表根因怎么修

viz_struct.py / viz_kd.py 用 P1 已落地的 `x_label/y_label/caption`，三处根因清理：

1. **viz_struct Pareto y=0 伪点根因**：FAIL_latency 行 accuracy=-1（未训练），`_to_float(-1)` 返 None，
   之前被前端渲染成 0 → 整列 y=0 误导。修复：显式过滤 `acc is None or acc < 0`，加 WARN 计数。
2. **删两张零信息图**：Round Ledger（每轮 1 候选无聚合价值）+ Exploration Tree（path 恒 p1，scatter 充数）。
   viz_struct 从 5 图压到 3 图。
3. **Candidate Ledger 重构**：原表把长 hypothesis 直接塞列里前端 wrap 难读。改：短字段（round/id/tag/latency_ms/
   accuracy/status/one_line_summary）进 columns；长 hypothesis 留 row data 的 `hypothesis` 字段（前端 tooltip）。
4. **viz_kd round 模式 db_gap/met_acc 移出默认 columns**：短训阶段这两列是占位（推迟 finalize），
   保留 row data 供 hover，但不进默认列（防误导）。
5. **viz_kd `_push_final_compare` 修 R4 + L7**：champion 短训阶段无真实 dB gap，**显式不进 dB gap bar**
   （之前 silently 丢，title 写 "teacher vs champion vs final" 实际只画 teacher+final）；title 改
   "teacher=0 baseline; champion deferred"；caption 标 champion 推迟 finalize。
   teacher_accuracy_known=false（teacher_setup 解析失败）→ caption 加 "⚠ teacher_accuracy 未知，dB gap 不可信"。

## device / latency_provider / ONNX

- **`_device.py`（共享单源，inline 自 NAS）**：`resolve_device`（torch.device，auto/cuda/npu/cpu）+
  `ort_providers`（onnxruntime provider 顺位，NPU=Ascend CANNExecutionProvider）+ `describe_device`。
  按"不引跨包依赖"硬约束，struct 与 kd 各一份同内容副本（DRY 让位 self-contained）。
- **`--device` CLI 全暴露**：latency_onnxrt / export_onnx / profile_onnx / measure_student / teacher_setup /
  measure_baseline。默认 `auto`（cuda→npu→cpu 探测）。
- **`--seed` CLI 全暴露**：默认 0。两 yaml 加 `seed` input。
- **解开硬编码**：export_onnx / measure_student / teacher_setup 原硬编码 `device="cpu"` → 透传 args.device。
- **`--no-external-data`（export_onnx）**：默认 True（断言导出后无 .data 伴生）；`--allow-external-data` 显式开禁
  （超大模型 >2GB protobuf 时必开）。
- **`latency_provider` 作为 [advanced] input 暴露**：默认 struct 自带 `latency_onnxrt.py::measure`；用户自有
  部署硬件真测脚本给定则**必须用**（违反设计草稿§5 固化要解开）。
- **`--device` 透传给 latency_provider**：用 `inspect.signature(measure).parameters` 检测是否含 device 形参
  （不用裸 try/except TypeError——会误吞用户脚本内部 TypeError）。

## P2 遗留收口（7 agent.md 拼接 + CONTRACTS.md stale）

- kd-hypothesizer selection_spec 落盘改用 setup.output.output_dir 拼接（setup 是 P7 合并后的单一真相源，
  尾斜杠由 setup 显式 `os.path.abspath(...) + "/"` 保证，拼接安全）。
- 7 个 agent.md（struct-evaluator/curator/analyst + kd-curator/analyst/hypothesizer/train-script）的
  `{{ output_dir }}<suffix>` / `{{ family_detect.output.X }}` / `{{ teacher_setup.output.X }}` / `{{ profile_gate.output.X }}`
  全部切到 `{{ setup.output.X }}` 专用字段。
- CONTRACTS.md §0 目录布局 + §4 CLI + §5 train_kd + §6 节点 I/O 表 + §7 hint 全同步 P7 6-节点形态。

## 隐性假数据治理

- **teacher_accuracy 解析失败**（`_parse_accuracy` 返 0.0 + confidence=low）：原静默当 baseline 用。
  P7：teacher_setup.py 写 `teacher_accuracy_known=false` 进 teacher_meta.json + stdout；stderr WARN。
  下游 finalize 见 false 须在 final_report 标 "⚠ teacher_accuracy 未知，dB gap 不可信"；viz_kd 同步 caption 警告。
  `--strict-accuracy` CLI 提供 fail-loud 选项（reserve；agent 默认 lenient 路径）。
- **champion_db_gap 短训阶段**：原无来源逼 LLM 造假（0.0 / 复制 proxy_mse）。P7：yaml output_schema 标注
  "短训阶段恒为 -1 sentinel（推迟 finalize）"，agent.md 输出示例固定 -1，**绝不编数**。

## 测试

新增 `tests/workflows/test_struct_kd_p7.py`（24 用例，覆盖关键不变量）：
- _device：resolve_device / ort_providers（cuda/npu=CANN/cpu 顺位）/ struct↔kd 副本同内容
- viz_struct：Pareto 过滤 accuracy=None / 只剩 3 图（无 Round Ledger + Exploration Tree）
- viz_kd：round 模式 db_gap/met_acc 不在默认列 / teacher_accuracy_known=false → caption 警告
- measure_student：latency-only 模式（无 eval → db_gap=-1 sentinel + measure_report.db_gap_deferred=true）
- teacher_setup `_parse_accuracy`：garbage → (0.0, unknown, low)；NMSE 命中 → high
- 6 脚本 CLI 全暴露 --device / --seed（+ export --no-external-data / --allow-external-data / teacher_setup --strict-accuracy）
- struct yaml 6 节点 + kd yaml 6 节点（断言 plan headline off-by-one 已订正）
- candidate_eval prompt 含 latency-first / Step A→B→C / FAIL_latency 不训练 + Step A 位置 < Step C
- setup output_schema 暴露所有下游路径字段（snapshots_dir / ledger_path / kd_recipe_path 等）
- agent.md 不再有 `{{ <非-setup 节点>.output.output_dir }}<suffix>` 拼接（setup 例外：尾斜杠保证）

回归：tests/workflows + tests/compile + tests/schema + tests/chart 共 **319 passed**。
另 10 失败全在 iface/mcp + exit_codes + frontend（pre-existing，与 P7 无关）。

## 已知 follow-up（未在 P7 范围）

- `kd-curator` 的路由决策（route_finalize / exhausted / continue_loop）仍是 LLM 在 prompt 里算
  （违反 deterministic-over-model-mediated）；修需写 kd_ledger_reducer.py 脚本，登记给后续 phase。
- `candidate_eval` 用 inline prompt（76 行）而非独立 agent.md（与 struct-evaluator 范式不一致）；
  选择保留 inline 因 latency-first 顺序契约紧绑 routes/output_schema 更可读，surface-conflict 已记。
- 各 workflow 真机 in-session E2E + 替换文档占位图（pre-existing follow-up）。

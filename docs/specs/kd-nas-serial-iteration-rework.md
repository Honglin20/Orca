# SPEC：kd-nas 串行迭代重写（v2，SPEC-REVIEW 修订版）

> 状态：v3（2026-08-03）。spec-reviewer 三轮对抗 **PASS**：v1→v2 闭环 17 问题（5 FATAL+4 MAJOR+5 MEDIUM+2 权衡）+ v2→v3 闭环 21 新问题（5 FATAL+7 MAJOR+9 MINOR），命令 flag 与 train_pipeline.py/tune_latency.py/teacher_setup.py 的 argparse 契约逐条对齐。取代 v1 + 批量候选路线。已写的 `derive_lightweight.py`/`test_derive_lightweight.py`（批量调参）废弃。

## 1. Context（为什么重写）

当前 `kd-nas` 是**批量并发**模式（gate 一次性 gate 全部 KB 预置候选 → 并发蒸馏池 → select），不符合用户串行迭代逻辑：无迭代回路 / student 候选与用户模型脱节（KB 预置 shape 写死）/ 无 web 推送 / teacher 参数自定。用户要 6 步串行迭代：flatten → 生成 teacher + 初始 student → 训 teacher → 蒸馏 student → 性能+KB 生成下一变体 → 再蒸馏 → 达标停止，全程 metrics 推 web。

## 2. 目标

重写为**串行迭代 KD 蒸馏**（仿 `agent-struct-exploration.yaml` 回路，KD 训练替代原样 train_command）：每轮 1 个 student → KD 蒸馏 → 测 latency+accuracy → 决策。student 由结构变换派生（首轮固定规则缩1层+FFN→pointwise；迭代轮 KB+perf 驱动），shape 跟用户真实输入。全程 metrics 推 web。teacher 用用户默认参数。终止：latency≤target ∧ accuracy 达 baseline（方向按 kind）/ max_rounds。

## 3. 设计原则

1. **串行化 DAG**（用户决策）：全串行链，零引擎改动，避免 router first-match-wins / 无 fan-in 陷阱。
2. **复用优先**：flatten / teacher-gen / kd-train-script / train_pipeline / tune_latency / export_onnx / render_chart / struct 回路骨架。
3. **student = 结构变换**：struct-engineer 式 LLM 整文件改写 + worktree + 快照 + AST 校验。
4. **全程 web 推送**：每节点 sidecar 调 render_chart，dumb copy stdout 进 output_schema。无新 event 类型。
5. **catch 协议**（SPEC-REVIEW B5）：业务失败（训练/eval/validate rc≠0）转结构化 output 落账 continue，agent 退 0；系统失败（agent 自身崩）workflow_failed。两者边界显式。
6. **fail loud / 无伪造**。

## 4. 6 步逻辑 → DAG 映射

| 用户步骤 | 节点 | 复用/新写 |
|---|---|---|
| 1. flatten+校验+时延+web | `flatten` | 复用 model-flatten + viz_status |
| —（shared infra） | `setup` | 精简 kd-setup（拆出 train_teacher）：paths + seed baseline champion + device 探测 |
| 2. teacher(深×3/宽×2)+student(缩1层+FFN→pointwise)+测时延+web | `gen_teacher` / `gen_student` | 复用 teacher-gen；gen_student 新写（首轮固定规则 / 迭代 KB） |
| 3. 训练脚本(用户逻辑)+校验+训teacher+metrics web+用户默认参数 | `gen_train_script` / `train_script_verify` / `train_teacher` | 复用 kd-train-script；train_script_verify 新写；train_teacher 独立节点（从 setup 拆出）+ env_anchor + 用户默认 lr/epochs + metrics_tail |
| 4. 蒸馏+校验正确性+训student+实时metrics web | `distill` | 新写（tune_latency → distill 训练 → eval + 实时 push） |
| 5. 性能+KB→下一变体→回4（串行） | `gen_student`（迭代轮）+ `decide` back-route | gen_student 读 ledger perf + KB |
| 6. 轮次+目标达标停止 | `decide` / `finalize` | decide 新写（KD reducer min-latency）；finalize KD 专版 |

## 5. DAG（串行化，零引擎改动）

```
flatten → setup → gen_teacher → gen_train_script → train_script_verify → train_teacher
                                                                       ↓
          ┌── gen_student → distill → decide ───────────────────────┐
          │                                                           │ when: "decide.output.continue_loop"
          └───────────────────────────────────────────────────────────┘
                              ↓ 兜底（continue_loop=false）
                           finalize → $end
```

**routes（每条写明 when，禁多个 catch-all；router first-match-wins）**：
- `flatten.routes` = `[{to: setup}]`
- `setup.routes` = `[{to: gen_teacher}]`
- `gen_teacher.routes` = `[{to: gen_train_script}]`
- `gen_train_script.routes` = `[{to: train_script_verify}]`
- `train_script_verify.routes` = `[{to: train_teacher}]`（校验通过；fail loud 阻塞——见 §15）
- `train_teacher.routes` = `[{to: gen_student}]`（teacher_cache 就绪，进循环首轮）
- `gen_student.routes` = `[{to: distill}]`
- `distill.routes` = `[{to: decide}]`
- `decide.routes` = `[{when: "decide.output.continue_loop", to: gen_student}, {to: finalize}]`
- `finalize.routes` = `[{to: $end}]`

**MaxIterations**（SPEC-REVIEW m1 修正）：总 visits ≤ `3×max_rounds + 7`（前置 6：flatten/setup/gen_teacher/gen_train_script/train_script_verify/train_teacher + 循环体 3×max_rounds + finalize 1）。默认 `max_iter=100` → `max_rounds ≤ 31` 安全（3×31+7=100）。**默认 max_rounds=10**（37 visits，远安全）；`max_rounds ≥ 31` 须 `--max-iter <max_rounds*3+10>` 覆盖。

## 6. 节点契约

### 6.1 `flatten`（复用 model-flatten + viz_status）

- 复用 `workflows/agents/model-flatten/`（SKILL + validate_contract + __main__ latency）。
- output: `baseline_contract_path` / `project_root` / `model_name` / `baseline_latency_us` / `viz_status`。
- web push：flatten **不推图**（单柱 baseline latency bar 信息量低，与 setup `baseline_seed_table` 冗余；baseline latency/accuracy 由 setup 承载）。`viz_status` 固定 `{"env_status":"skipped","charts":{}}`。

### 6.2 `setup`（精简 kd-setup，**拆出 train_teacher**）

- **职责**：探测 shared infra 路径 + seed baseline champion + device 探测。**不含 train_teacher**（拆到独立节点）。
- 产出（单一真相源，下游经 setup.output 透传）：`kd_artifacts_dir`（末尾带 /）/ `per_run_artifacts_dir`（$ORCA_ARTIFACTS_DIR）/ `project_root` / `kd_scripts_dir`（`workflows/agents/_kd_scripts` 绝对路径，下游 distill/decide 调脚本用）/ `struct_scripts_dir`（`workflows/agents/_struct_scripts` 绝对路径，export_onnx 用）/ `ledger_path` / `champions_path` / `ckpts_dir`（末尾带 /）/ `snapshots_dir`（末尾带 /）/ `worktree_root`（末尾带 /，KD 可选——见 §6.7 注）/ `device`（探测）/ `concurrency`（串行版=1）/ `baseline_latency_us`（透传 flatten）/ `baseline_accuracy`（=inputs.accuracy_baseline）/ `viz_status`。
- **seed baseline champion**：`champions.jsonl` 第一行 = `{id:"baseline", round:0, latency_us:baseline_latency_us, accuracy:baseline_accuracy, met_latency:false, met_accuracy:false, snapshot:baseline_contract_path}`。ledger 初始化（不截断已有）。
- 复用现 `kd-setup/agent.md` 的 step1（路径解析 + lock）+ step7（GPU 探测），**删 step2-6**（baseline 校验移 flatten / teacher 训练移 train_teacher）。GPU 探测走 **device-only 模式**（setup 在 train_teacher 之前执行，teacher 未训、无 `teacher_cache.pt`，故不传 `--teacher_cache`；gpu_probe 只解析 device，`concurrency` 恒 1）——禁止传 baseline `.py` 契约当 teacher_cache（gpu_probe VRAM 模式会 `torch.load` 它 → UnpicklingError → exit 2 → workflow_failed）。
- web push：baseline champion seed table。

### 6.3 `gen_teacher`（复用 teacher-gen + viz_status）

- 复用 `workflows/agents/teacher-gen/`（深×3/宽×2 wrapper + validate_teacher + measure_latency）。
- output: `teacher_model_path` / `teacher_latency_us` / `depth_axis` / `width_axis` / `viz_status`。
- web push：teacher vs baseline latency bar。

### 6.4 `gen_train_script`（复用 kd-train-script）

- 复用 `workflows/agents/kd-train-script/`（生成 train_pipeline.py，搬用户 loss/dataloader/optimizer，teacher/distill/eval 三模式 + _make_live_push）。
- **teacher 参数提取**（SPEC-REVIEW M1）：从 `inputs.user_train_script` 提取用户默认 lr/epochs（grep argparse default / 变量赋值），写入 train_pipeline 的 teacher 模式默认 + output `teacher_default_lr` / `teacher_default_epochs`（供 train_teacher 用）。提取不到 → **fail loud**（非 WARN——用户 teacher 可能因 lr 错不收敛，违步骤3）。
- output: `train_pipeline_path` / `teacher_default_lr` / `teacher_default_epochs`。

### 6.5 `train_script_verify`（**新写**，校验 agent；SPEC-REVIEW M2 修订）

校验 gen_train_script 产出的 train_pipeline.py：
- (a) grep `def run_teacher_mode / run_distill_mode / run_eval_mode` 三函数存在（模板:456/536/676）。
- (b) grep 用户 `compute_loss / build_dataloader` 函数名被搬进模板（非 placeholder）。
- (c) `_make_live_push / _maybe_bootstrap_env` 保留（web live loss）。
- (d) `--mode eval` 用 micro dummy ckpt（torch.save 一个空 state_dict）跑一步，确认能 load + forward 不崩。
- **删** v1 的「--help/dry-run」（模板无 --dry-run；--help 验不了 mode）。
- output: `verified`（bool）/ `issues`（fail loud 列表，空=通过）。verified=false → fail loud 阻塞（不进 train_teacher）。

### 6.6 `train_teacher`（**独立节点**，从 setup 拆出；SPEC-REVIEW M1 修订）

- 读 `gen_teacher.output.teacher_model_path` + `gen_train_script.output.train_pipeline_path` + `flatten.output.baseline_contract_path` + `setup.output.*`（paths）+ `gen_train_script.output.teacher_default_lr/epochs`。
- **幂等 check**（复用 kd-setup step4 逻辑）：`teacher_cache.pt` + `teacher_meta.json` + `teacher_ckpt.pt` 三者存在 ∧ `meta.teacher_model_hash == sha256(teacher_model_path)` ∧ `meta.teacher_ckpt_sha256 == sha256(teacher_ckpt)` → 跳过训练，透传 output。否则 NEED_TRAIN=1。
- **teacher 训练命令**（用户默认参数 + 全 required flag；非硬编码 --epochs 1）：
  ```bash
  ORCA_KD_SCRIPTS_DIR="{{ setup.output.kd_scripts_dir }}" python3 "{{ gen_train_script.output.train_pipeline_path }}" \
    --mode teacher --model_path "{{ gen_teacher.output.teacher_model_path }}" \
    --build_fn build_model --build_cfg '{}' \
    --epochs "{{ gen_train_script.output.teacher_default_epochs }}" \
    --lr "{{ gen_train_script.output.teacher_default_lr }}" \
    --out_ckpt "{{ setup.output.kd_artifacts_dir }}teacher_ckpt.pt" \
    --device "{{ inputs.device }}" --seed "{{ inputs.seed }}" \
    --env_anchor "{{ setup.output.per_run_artifacts_dir }}" \
    > "{{ setup.output.kd_artifacts_dir }}teacher_train.log" 2>&1
  ```
  stdout 重定向到 `teacher_train.log`（metrics_tail 读此；_make_live_push 仍实时推 loss 到 web）。
- **teacher_setup 命令**（产 cache + meta；latency 透传 gen_teacher，不自测）：
  ```bash
  python3 "{{ setup.output.kd_scripts_dir }}/teacher_setup.py" \
    --teacher_model_path "{{ gen_teacher.output.teacher_model_path }}" \
    --teacher_ckpt "{{ setup.output.kd_artifacts_dir }}teacher_ckpt.pt" \
    --build_fn build_model --dummy_input "<json.dumps(flatten baseline DUMMY_INPUT)>" \
    --output_dir "{{ setup.output.kd_artifacts_dir }}" --opset 17 \
    --teacher_latency_us "{{ gen_teacher.output.teacher_latency_us }}" \
    --device "{{ inputs.device }}"
  ```
- **metrics_tail**（新写 sidecar）：读 `teacher_train.log`，按 `inputs.metrics_template`（可选 §9 schema）摘 metrics 推 web（line per-epoch）；无模板则默认推 loss curve（_make_live_push 已由 --env_anchor 激活）。分工：_make_live_push 推 loss（live，训练循环内），metrics_tail 推模板字段（post-hoc，训练后或 tail）。
- output: `teacher_cache` / `teacher_meta` / `teacher_ckpt` / `teacher_latency_us`（透传）/ `viz_status`。
- web push：teacher 训练 loss/epoch line + 摘取 metrics。

### 6.7 `gen_student`（**新写**，结构变换；首轮固定 + 迭代 KB；SPEC-REVIEW T2 合并）

仿 struct-engineer（LLM 整文件改写 model.py + worktree + 不可变快照 + AST 可编译校验）。合并 hypothesizer+engineer 为一节点（减循环节点数）。

- **首轮**（train_teacher 后入口，ledger 无 student 行）：读 `flatten.output.baseline_contract_path`，按固定规则（§7）改写：缩1层 + FFN→pointwise。产出 student model.py + KNOBS + DUMMY_INPUT 逐字复制 baseline。
- **迭代轮**（decide back-route，ledger 有 student 行）：读 ledger 最后轮 student perf + champion + KB（`$ORCA_KB_DIR/families/receiver/spt_*.py` 技术点 + struct directions/latency_moves 若有）→ 提结构假设（降时延/补精度）→ 改写上轮 student model.py。
- **DUMMY_INPUT 字节级 deterministic 校验**（SPEC-REVIEW m2）：末尾 python 比较 `student.DUMMY_INPUT == flatten.output.baseline_contract_path 加载的 DUMMY_INPUT`（dict 相等），不等 → fail loud（非"严禁项"软约束；防 LLM 写死 shape）。
- validate_contract.py PASS（fail loud；3 轮修不过 → catch 协议转 FAIL_build，§15）。
- worktree/snapshots 路径从 `setup.output` 取（仿 struct-engineer）。**worktree 在 KD 可选**——struct 用 worktree 是因 evaluator 跑用户原 train_command；KD 用 train_pipeline.py importlib by path 加载 student，不强制 worktree（gen_student 可直接写 snapshots_dir，worktree 仅作 AST instantiate 校验的 sys.path fallback）。
- **student 契约额外字段**（N4）：distill 的 kd_config 含 ofd/fitnets/rkd 特征蒸馏时，student model.py 须暴露 `feature_hook_names() -> list[str]`（train_pipeline.py:555 `getattr(student,"feature_hook_names",None)` 读）。baseline 有可对齐特征层时 gen_student 必移植此 fn，否则特征蒸馏静默退化为零。
- **FAIL_build 分支**（Q7）：baseline 无可变换结构（纯 Linear 无 knob/FFN，或首轮固定规则全不适用）→ gen_student 3 轮修不过 → catch 协议转 `status=FAIL_build`，agent 退 0；decide 落账，下轮纯 KB 驱动（跳过固定规则）。
- output: `student_model_path`（snapshots/r<round>_student_model.py）/ `round` / `hypothesis` / `direction_id` / `knobs`（student KNOBS dict 的 JSON 串，省 distill 重复加载）/ `status`（OK/FAIL_build）/ `fail_reason` / `viz_status`。
- web push：hypothesis table。

### 6.8 `distill`（**新写**，单 student KD；SPEC-REVIEW B3 调顺序 + B5 catch + m3 字段）

**顺序**（B3 修正，accepted_cfg 不再鸡生蛋；全 required flag 补全 N1/N17/N18/N21）：
1. **tune_latency**（产 accepted_cfg + latency + met_latency）：
   ```bash
   python3 "{{ setup.output.kd_scripts_dir }}/tune_latency.py" \
     --variant_path "{{ gen_student.output.student_model_path }}" \
     --build_fn build_model --dummy_input "<json.dumps(flatten baseline DUMMY_INPUT)>" \
     --knobs "{{ gen_student.output.knobs }}" \
     --target_latency_us "{{ inputs.target_latency_us }}" \
     --latency_provider "{{ inputs.latency_provider }}" \
     --artifacts_dir "{{ setup.output.kd_artifacts_dir }}" \
     --device "{{ inputs.device }}" --seed "{{ inputs.seed }}"
   ```
   → stdout `TUNE_STATUS: ACCEPTED|FAIL_latency` + `ACCEPTED_CFG`/`BEST_EFFORT_CFG` + `LATENCY_US_MEDIAN`/`LATENCY_US_STD`。shape 跟 baseline DUMMY_INPUT。
2. **FAIL_latency 分支**（M4）：`TUNE_STATUS=FAIL_latency` → **跳过训练省 GPU** → output `status=FAIL_latency, tune_status=FAIL_latency, latency_us=<best_effort_median>, accuracy=-1, met_latency=false, met_accuracy=false`，agent 退 0（catch 协议），交 decide 落账 continue。
3. **distill 训练**（ACCEPTED 才跑；**N1 必传 --kd_config 否则 KD 名存实亡**）：
   ```bash
   ORCA_KD_SCRIPTS_DIR="{{ setup.output.kd_scripts_dir }}" python3 "{{ gen_train_script.output.train_pipeline_path }}" \
     --mode distill \
     --student_model_path "{{ gen_student.output.student_model_path }}" \
     --build_fn build_model --build_cfg "<accepted_cfg JSON>" \
     --teacher_cache "{{ train_teacher.output.teacher_cache }}" \
     --kd_config '{"kd_losses":["mse","ofd"],"weights":{"mse":1.0,"ofd":0.3},"ema":true}' \
     --variant_id "r{{ gen_student.output.round }}_student" \
     --epochs "{{ inputs.full_epochs }}" \
     --out_ckpt "{{ setup.output.ckpts_dir }}r{{ gen_student.output.round }}_student.pt" \
     --device "{{ inputs.device }}" --seed "{{ inputs.seed }}" \
     --project_root "{{ setup.output.project_root }}" \
     --env_anchor "{{ setup.output.per_run_artifacts_dir }}"
   ```
   kd_config recipe 持有在 distill agent（对齐 v1 train_pool.py:162-163）；可选 inputs.advanced `kd_recipe` 让用户调。
4. **eval 取精度**（**N18 补全 required flag**；eval 不写 ckpt 但 argparse 仍校验）：
   ```bash
   python3 "{{ gen_train_script.output.train_pipeline_path }}" \
     --mode eval \
     --student_model_path "{{ gen_student.output.student_model_path }}" \
     --build_fn build_model --build_cfg "<accepted_cfg JSON>" \
     --student_ckpt "{{ setup.output.ckpts_dir }}r{{ gen_student.output.round }}_student.pt" \
     --out_ckpt "{{ setup.output.ckpts_dir }}r{{ gen_student.output.round }}_student.pt" \
     --accuracy_baseline "{{ inputs.accuracy_baseline }}" \
     --accuracy_baseline_kind "{{ inputs.accuracy_baseline_kind }}"
   ```
   → stdout `STUDENT_ACCURACY` / `MET_ACCURACY`（方向按 kind）。distill 训练同样可选 metrics_tail（同 §6.6 契约，N14）。
- **catch 协议**（B5/N11）：训练/eval 子进程 rc≠0 → `status=FAIL_train, tune_status=ACCEPTED, fail_reason=<stderr 尾 300 字>`，agent 退 0（不抛→不 workflow_failed），交 decide。**FAIL_train 时 met_latency=true（tune 已过）, met_accuracy=false, accuracy=-1**（对齐 v1 train_pool.py:172-173）。
- **tune_status ↔ status 映射**（N21）：`FAIL_latency → status=FAIL_latency ∧ tune_status=FAIL_latency`；`SUCCESS → status=SUCCESS ∧ tune_status=ACCEPTED`；`FAIL_train → status=FAIL_train ∧ tune_status=ACCEPTED`。
- output（m3 补全）：`round` / `student_model_path` / `accepted_cfg` / `cfg_hash` / `latency_us` / `latency_us_std` / `accuracy` / `accuracy_kind` / `met_latency` / `met_accuracy` / `ckpt` / `tune_status` / `status`（SUCCESS/FAIL_latency/FAIL_train）/ `fail_reason` / `viz_status`。
- web push：本轮 student latency/accuracy table + distill loss line。

### 6.9 `decide`（**新写**，KD 专版 reducer min-latency + continue_loop；SPEC-REVIEW B4 写死）

确定性脚本 `kd_reducer.py`（**KD 专版重写**，参考 ledger_reducer 骨架但 schema/排序键不同——SPEC-REVIEW B4）：
- **append ledger**：`{variant_id:r<round>_student, student_path, round, parent:<上轮 student>, latency_us, accuracy, met_latency, met_accuracy, accuracy_kind, direction_id, hypothesis, accepted_cfg, cfg_hash, ckpt, status}`。
- **champion ratchet**（B4 写死，min-latency）：准入 = `met_latency ∧ met_accuracy`；准入集合内按 **latency 最小** ratchet（FIFO tiebreak）。无达标 → 维持 baseline（setup seed 的 round=0）。
- **continue_loop**（§13）：champion_met（准入集合非空）→ false, reason="target_met"；round ≥ max_rounds → false, reason="max_rounds"；else → true。
- output: `round` / `continue_loop` / `champion_id` / `champion_latency_us` / `champion_accuracy` / `terminate_reason` / `viz_status`。
- web push：champion 轨迹 line + 逐轮汇总 table。

### 6.10 `finalize`（**KD 专版 inline prompt**；SPEC-REVIEW M3 + N10/N19）

- **不重蒸馏**（N10 省 GPU）：每轮已用 `full_epochs` 训练 champion，finalize **直接用 champion 已有 ckpt** 跑 eval + ONNX + latency（不重训）。champion snapshot + accepted_cfg + ckpt 从 `decide.output.champion_id` 查 ledger 拿。
- **eval + ONNX + latency 命令**（N19 全 flag）：
  ```bash
  # eval（复用 champion ckpt，确认 final_accuracy）
  python3 "{{ gen_train_script.output.train_pipeline_path }}" --mode eval \
    --student_model_path "<champion student_model_path>" --build_fn build_model \
    --build_cfg "<champion accepted_cfg JSON>" \
    --student_ckpt "<champion ckpt>" --out_ckpt "<champion ckpt>" \
    --accuracy_baseline "{{ inputs.accuracy_baseline }}" --accuracy_baseline_kind "{{ inputs.accuracy_baseline_kind }}"
  # ONNX 导出（确定性）
  python3 "{{ setup.output.struct_scripts_dir }}/export_onnx.py" \
    --model_path "<champion student_model_path>" --build_fn build_model \
    --dummy_input "<json.dumps(baseline DUMMY_INPUT)>" --opset 17 \
    --out "{{ setup.output.kd_artifacts_dir }}final.onnx" \
    --device "{{ inputs.device }}" --seed "{{ inputs.seed }}"
  # latency：动态加载 inputs.latency_provider（path::func），measure(final.onnx, device)
  ```
- **champion=baseline 兜底**（MAJOR-1）：若 `decide.output.champion_id == "baseline"`（round=0，即所有轮 FAIL_latency/FAIL_train/FAIL_build，admitted 空），finalize **跳过 student eval/ONNX/latency 命令**（baseline snapshot 是 flatten 产的 contract 非 student .py，student 命令会崩），直接用 `setup.output.baseline_latency_us` / `baseline_accuracy` 写 final_report（标注「无 student 达标」+ 各轮失败汇总）。
- 写 final_report.md（teacher vs students + 选择依据 + 帕累托 + 探索轮数 + terminate_reason）。
- output: `final_model` / `final_onnx` / `final_latency_us` / `final_accuracy` / `final_report` / `viz_status`。
- web push：终态对比 bar（baseline/teacher/champion）+ 帕累托 scatter。
- KD 专版 inline prompt（参考 struct finalize 结构，**非复用 agent.md**——struct finalize 是 yaml inline）。

## 7. student 结构变换规则（§6.7 细化）

**首轮固定规则**：
1. 读 baseline build_model + KNOBS + DUMMY_INPUT。
2. **缩1层**：深度 knob（num_blocks/num_layers，按 teacher-gen 轴识别模式）default − 1。无深度 knob → 跳过（agent.md 注明）。
3. **FFN → pointwise**：baseline 的 FFN block（expand→act→contract）替换为 pointwise（Conv1d kernel=1）。具体由 LLM 按 baseline 实际 FFN 改写（不同模型 FFN 不同，给规则不给死代码）。
4. KNOBS 保留 baseline 可调维度（缩层后值作 default，min/step/leverage 继承）。
5. DUMMY_INPUT 逐字复制 baseline + 字节级校验（§6.7）。
6. validate_contract PASS。

**迭代轮**：
1. 读 ledger 最后轮 student（latency/accuracy/met_*）+ champion + KB 技术点。
2. 提假设：latency 未达 → 降时延方向（砍层/瘦身/轻量算子，参考 KB latency_moves）；精度未达 → 补精度（attention/残差，参考 KB）。
3. LLM 整文件改写上轮 student model.py + AST 可编译校验 + 快照。
4. direction_id 记 KB 方向（审计 + 下轮 coverage）。
5. DUMMY_INPUT 字节级校验 + validate_contract PASS。

## 8. web 推送设计（不变，复用 render_chart）

**唯一管道**：`orca.chart.render_chart(chart_type, data, label, title, ...)` + `load_run_env_from_artifacts($ORCA_ARTIFACTS_DIR)` 自举 env。

| 节点 | chart | chart_type | data |
|---|---|---|---|
| setup | baseline champion seed | table | `[{round:0, latency, accuracy}]` |
| gen_teacher | teacher vs baseline | bar | baseline/teacher |
| train_teacher | 训练 loss/metrics | line | per-epoch（_make_live_push + metrics_tail） |
| gen_student | hypothesis | table | `[{round, hypothesis, direction_id}]` |
| distill | student latency/accuracy | table | `[{round, latency, accuracy, met_*}]` |
| distill | distill loss | line | per-epoch |
| decide | champion 轨迹 | line | `[{round, champion_latency, champion_accuracy}]` |
| finalize | 终态对比 + 帕累托 | bar + scatter | baseline/teacher/champion |

- 每节点 sidecar `viz_kd_stage.py`（新写，复用 viz_kd.py pusher 结构 + `_classify_exc` + `_main` 兜底）dumb copy stdout JSON 进 `viz_status`（output_schema required，sidecar 失败值合法不阻断）。
- **viz_status schema**（N9，对齐 struct-exploration.yaml:328-344）：
  ```json
  {"env_status": "ok|env_loaded_from_file|env_missing|import_failed|generic",
   "charts": {"<图名>": {"pushed": <bool>, "reason": "<str>"}, ...}}
  ```
  `additionalProperties: false`；全 pushed=true 即成功；env_missing/generic 等失败值合法（sidecar 不阻断主流程）。
- **设计取舍 T1**（SPEC-REVIEW）：student 时延推迟到 distill 测（tune_latency 在 distill 内），避免 gen_student 测一次 + distill 又测一次。步骤2 的 web 只推 teacher vs baseline + student hypothesis 摘要。

## 9. metrics 摘取（metrics_tail；SPEC-REVIEW m5 补 schema）

- **默认**：train_pipeline `_make_live_push` 推 loss curve（per-epoch，已有）。
- **可配置模板**（`inputs.metrics_template`，可选 JSON）：schema =
  ```json
  {"source_log": "<train log/jsonl 路径>",
   "metrics": [{"name": "nmse", "regex": "nmse=(?P<val>[0-9.]+)", "chart_type": "line", "x": "epoch", "y": "val"}]}
  ```
  `metrics_tail.py`（新写，抄 `tail_metrics.py` 骨架）：读模板 → tail log → regex named group 提字段 → render_chart。
- 无模板 → 默认只推 loss。

## 10. 复用清单（SPEC-REVIEW §7.14 修订）

| 组件 | 路径 | 改动 |
|---|---|---|
| model-flatten | `workflows/agents/model-flatten/` | 扩展 viz_status（加 sidecar 调用） |
| teacher-gen | `workflows/agents/teacher-gen/` | 扩展 viz_status |
| kd-train-script | `workflows/agents/kd-train-script/` | 加 teacher 默认 lr/epochs 提取 + output |
| validate_contract.py | `model-flatten/scripts/` | 零改动（student 契约校验） |
| train_pipeline.py 模板 | `kd-train-script/references/templates/` | 零改动（distill/eval/teacher 三模式 + _make_live_push） |
| tune_latency.py | `_kd_scripts/` | 零改动（student latency，shape 跟 baseline DUMMY_INPUT） |
| export_onnx.py | `_struct_scripts/` | 零改动 |
| latency_provider | inputs | 零改动 |
| kd_common.accuracy_direction | `_kd_scripts/` | 零改动（精度双向） |
| render_chart + load_run_env | `orca/chart/` | 零改动 |
| router + MaxIterations | `orca/run/` | 零改动（串行 DAG + 回路通用） |
| ledger_reducer.py | `_struct_scripts/` | **参考非复用**（KD 专版 kd_reducer.py，schema + min-latency 排序键不同） |

## 11. 新写清单

| 组件 | 路径 | 职责 |
|---|---|---|
| setup（精简） | 改 `kd-setup/agent.md` | shared infra + seed champion + device（删 train_teacher） |
| gen_student agent | `workflows/agents/gen-student/agent.md` | 结构变换（首轮固定 + 迭代 KB），DUMMY_INPUT 字节级校验 |
| train_script_verify agent | `workflows/agents/train-script-verify/agent.md` | 校验 train_pipeline（mode 函数 + micro 跑） |
| train_teacher agent | `workflows/agents/train-teacher/agent.md` | 独立训 teacher + 用户默认参数 + metrics_tail |
| distill agent | `workflows/agents/distill/agent.md` | tune→distill→eval + catch 协议 + 实时 push |
| decide agent | `workflows/agents/decide/agent.md` | KD reducer min-latency + continue_loop |
| finalize（inline） | kd-nas.yaml inline prompt | 终态重蒸馏 + web |
| kd_reducer.py | `_kd_scripts/` | KD 专版决策 reducer |
| viz_kd_stage.py | `_kd_scripts/` | 每节点 web push sidecar |
| metrics_tail.py | `_kd_scripts/` | 可配置模板 metrics 摘取 |
| kd-nas.yaml | `workflows/` | 重写（串行迭代 DAG） |

**废弃**：`derive_lightweight.py`/`test_derive_lightweight.py`（批量调参）；旧 kd-nas.yaml 批量节点（gate_all/train_pool/pick_variant 在串行版不接线，保留文件供回滚）。

## 12. 输入契约

```yaml
inputs:
  baseline_model_path:     [ask] 用户模型入口
  user_train_script:       [ask] 用户 train.py（teacher 默认 lr/epochs 从此提取）
  target_latency_us:       [ask] latency 目标
  accuracy_baseline:       [ask] 精度基线
  accuracy_baseline_kind:  [ask] nmse/mse/ber/db | snr/acc
  latency_provider:        [ask] 用户 latency 脚本 path::func
  max_rounds:              [ask] 最大迭代轮数（默认 10）
  metrics_template:        [advanced] 可选 metrics 摘取模板 JSON（§9 schema；空=只推 loss）
  device:                  [advanced] auto/cuda/npu/cpu（默认 auto）
  full_epochs:             [advanced] 每轮蒸馏 epoch（默认 50）
  seed:                    [advanced] 默认 0
```

## 13. 终止条件（decide；min-latency）

```
admitted = ledger 中 met_latency ∧ met_accuracy 的 student 集合
champion = admitted 中 latency 最小，**tie 不 ratchet（保 FIFO 最早，即 strict-improvement，N12）**；admitted 空 → 维持 baseline
champion_met = admitted 非空（即 champion 是真 student 非 baseline）
if champion_met: continue_loop=false, reason="target_met"
elif round >= max_rounds: continue_loop=false, reason="max_rounds"
else: continue_loop=true
```

## 14. 验收标准（SPEC-REVIEW m4 修订）

1. `tars validate kd-nas` 0 error。
2. E2E（opencode+deepseek）：用一个 **baseline 不达标的 fixture**（target_latency 调低 / accuracy_baseline 调高）强制 ≥2 轮 distill→decide 循环 → finalize；验收 decide 在 max_rounds 耗尽时正确终止。
3. **核心判据**：每轮 student `DUMMY_INPUT` 字节级 == baseline DUMMY_INPUT（§6.7 deterministic 校验；非写死 64）。
4. web：每阶段 chart 推 web（teacher latency bar、teacher 训练 line、每轮 student table、champion 轨迹 line、终态对比）。flatten 不推图（baseline 信息由 setup baseline_seed_table 承载）。
5. teacher 参数：从 user_train_script 提取默认 lr/epochs（非硬编码；提取不到 fail loud）。
6. 串行：一次一个 student（不并发），decide back-route。
7. 达标终止：admitted 非空 → finalize；或 max_rounds 耗尽。
8. fail loud / catch 协议：student validate_contract FAIL→FAIL_build 落账 continue；distill 训练崩→FAIL_train 落账 continue；train_script_verify fail→阻塞；teacher 参数缺→阻塞。
9. ledger：每轮 student 行含 round/parent/direction_id/latency/accuracy/met_*/accepted_cfg，champion ratchet min-latency。

## 15. 失败路径 + catch 协议（SPEC-REVIEW B5）

**catch 协议**（业务失败 vs 系统失败边界）：
- **业务失败**（子进程 rc≠0：distill 训练/eval、gen_student validate_contract）：agent 内部捕获，转结构化 output（status=FAIL_*，fail_reason），agent 退 0 → decide 落账 → continue_loop（除非 max_rounds）。**不抛异常**（避免 workflow_failed）。
- **系统失败**（agent 自身崩 / 脚本语法错 / 引擎错误）：不吞，workflow_failed。
- **catch pattern bash 模板**（Q4，各业务失败节点 agent.md 引用，避免跨节点漂移）：
  ```bash
  OUT="$(python3 <cmd> ... 2>&1)"; RC=$?
  if [ $RC -ne 0 ]; then
    # 业务失败：emit FAIL JSON，agent 退 0（不 workflow_failed → decide 可落账 continue）
    FAIL_REASON="$(echo "$OUT" | tail -c 300)"
    cat <<EOF
  {"status":"FAIL_train","fail_reason":"rc=$RC: $FAIL_REASON", ...}
  EOF
    exit 0
  fi
  # 成功：解析 stdout KEY，组 SUCCESS JSON
  ```

具体：
- flatten validate_contract FAIL → agent 不返 JSON（flatten agent.md fail loud）→ workflow_failed。
- gen_student validate_contract FAIL 3 轮 → catch → status=FAIL_build → decide 落账 continue。
- train_script_verify issues 非空 → **fail loud 阻塞**（verified=false 不进 train_teacher；这是配置错误非业务波动）→ workflow_failed。
- train_teacher 训练崩 → fail loud（teacher_cache 缺，整个循环无意义）→ workflow_failed。
- train_teacher 参数提取不到 → fail loud（gen_train_script 阶段）。
- distill tune FAIL_latency → 跳训练 → status=FAIL_latency → decide continue（省 GPU）。
- distill 训练/eval rc≠0 → catch → status=FAIL_train → decide continue。
- decide 自身 reducer 异常（ledger 坏）→ fail loud → workflow_failed。

## 16. SPEC-REVIEW 闭环记录（v2）

- **B1 DAG 拓扑**：✅ 串行化（用户决策），§5 重写，每 route 写明 when。
- **B2 setup 节点**：✅ §6.2 补 setup（shared infra + seed champion）。
- **B3 distill 顺序**：✅ §6.8 调换 tune→distill→eval + FAIL_latency 分支。
- **B4 champion ratchet**：✅ §6.9+§13 写死 min-latency，KD 专版 reducer。
- **B5 catch 协议**：✅ §3+§15 显式业务/系统失败边界。
- **M1 teacher 参数**：✅ §6.4+§6.6 三项改动 + fail loud。
- **M2 train_script_verify**：✅ §6.5 改 mode 函数 + micro 跑。
- **M3 finalize**：✅ §6.10 KD 专版 inline + 重蒸馏。
- **M4 FAIL_latency**：✅ §6.8 分支。
- **m1 MaxIterations**：✅ §5 改 3R+7。
- **m2 DUMMY_INPUT 校验**：✅ §6.7 字节级 deterministic。
- **m3 distill output**：✅ §6.8 补 accepted_cfg/cfg_hash/latency_us_std。
- **m4 验收**：✅ §14 #2 加 fixture 前置。
- **m5 metrics_template schema**：✅ §9。
- **T1 student 时延推迟**：✅ §8 声明。
- **T2 gen_student 合并**：✅ §6.7。

### v2→v3 review 闭环（21 新问题；P0/P1 落 SPEC，P2 coder 实现时落）

**FATAL（5）**：N1 distill --kd_config ✅ §6.8 step3 / N2 train_teacher --out_ckpt ✅ §6.6 / N17 tune --artifacts_dir ✅ §6.8 step1 / N18 eval 全 flag ✅ §6.8 step4 / N19 finalize 完整 ✅ §6.10
**MAJOR（7）**：N3 teacher_setup 命令 ✅ §6.6 / N4 feature_hook_names ✅ §6.7 / N7 setup scripts_dir ✅ §6.2 / N8 metrics_tail↔_make_live_push 分工 ✅ §6.6+§9 / N9 viz_status schema ✅ §8 / N10 finalize 用 champion ckpt 省 GPU ✅ §6.10 / N21 tune_status↔status 映射 ✅ §6.8
**MINOR 落 SPEC**：N6 worktree 可选 ✅ §6.7 / N11 FAIL_train met_* ✅ §6.8 / N12 tie 不 ratchet ✅ §13 / N14 distill metrics_tail ✅ §6.8 / N20 gen_student output knobs ✅ §6.7 / Q4 catch pattern 模板 ✅ §15
**MINOR coder 实现时落**：N5 字节级→值等措辞 / N15 micro eval 用 `flatten.output.baseline_contract_path` / N16 fixture 参数（target_latency=baseline×0.7, accuracy_baseline=baseline+margin）

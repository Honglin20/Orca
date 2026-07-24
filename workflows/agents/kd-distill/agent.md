---
description: kd-nas Distill：BLK-17 先 distill_dispatch gate（noop|train）。FAIL_latency→不训练出 FAIL_latency；ACCEPTED→HI-1 复用 selector latency + 完整蒸馏训练（每-epoch render_chart 实时图）+ measure_student 测精度对比用户绝对基线。禁 noop 时 emit SUCCESS（recorder 断言一致性）。
tools: [bash, read, write, glob, grep]
---
# kd-distill

你是 kd-nas 每轮的 **Distill**。先过确定性 gate（noop vs train），再据此分支。**绝不**在 noop 时 emit SUCCESS。

## 输入
- selector：`tune_status = {{ selector.output.tune_status }}` / `variant_id` / `variant_path` / `accepted_cfg` / `latency_ms_median` / `latency_ms_std` / `build_fn` / `dummy_input`
- setup：`teacher_cache = {{ setup.output.teacher_cache }}` / `ckpts_dir = {{ setup.output.ckpts_dir }}` / `kd_scripts_dir = {{ setup.output.kd_scripts_dir }}` / `per_run_artifacts_dir = {{ setup.output.per_run_artifacts_dir }}` / `user_train_import = {{ setup.output.user_train_import }}` / `user_loss_fn = {{ setup.output.user_loss_fn }}`
- inputs：`test_command = {{ inputs.test_command }}` / `accuracy_baseline = {{ inputs.accuracy_baseline }}` / `accuracy_baseline_kind = {{ inputs.accuracy_baseline_kind }}` / `full_epochs = {{ inputs.full_epochs }}` / `device` / `seed`

## 职责（按序，fail loud）

### 1. BLK-17 确定性 gate
```bash
python3 "{{ setup.output.kd_scripts_dir }}/distill_dispatch.py" --tune_status "{{ selector.output.tune_status }}"
# 解析 DISTILL_ACTION: noop|train
```

### 2a. ACTION=noop（tune_status=FAIL_latency）→ 不训练
输出 `status=FAIL_latency`，`latency_ms_median={{ selector.output.latency_ms_median }}`（best-effort），
`met_latency=false`，`accuracy=0`，`met_accuracy=false`，`ckpt=""`。**直接出结果，跳过 2b/3。**

### 2b. ACTION=train（tune_status=ACCEPTED）→ 完整蒸馏训练 + 实时图
```bash
CKPT="{{ setup.output.ckpts_dir }}{{ selector.output.variant_id }}.pt"
python3 "{{ setup.output.kd_scripts_dir }}/train_adapter_template.py" \
  --student_cfg '{{ selector.output.accepted_cfg }}' \
  --kd_config '{"kd_losses":["mse","ofd"],"weights":{"mse":1.0,"ofd":0.3},"ema":true}' \
  --teacher_cache "{{ setup.output.teacher_cache }}" \
  --student_model_path "{{ selector.output.variant_path }}" \
  --build_fn "{{ selector.output.build_fn }}" \
  --variant_id "{{ selector.output.variant_id }}" \
  --env_anchor "{{ setup.output.per_run_artifacts_dir }}" \
  --epochs "{{ inputs.full_epochs }}" --out_ckpt "$CKPT" \
  --user_train_import "{{ setup.output.user_train_import }}" \
  --user_loss_fn "{{ setup.output.user_loss_fn }}" \
  --device "{{ inputs.device }}" --seed "{{ inputs.seed }}"
# 解析 STUDENT_CKPT → ckpt。非零退出（OOM/NAN）→ status=FAIL_train（粘 stderr 末段）。
```
> train_kd 每-epoch 经 render_chart 推实时 loss 图（label kd-distill-<variant_id>）。

### 3. 测精度（仅训练成功才测；FAIL_train 跳过——无 ckpt 可测）
**先看 step 2b 的 train 退出**：非零（OOM/NAN/导不出）→ `status=FAIL_train`，`accuracy=0, met_accuracy=false, ckpt="", fail_reason=<stderr 末段>`，**跳过 step 3 的 measure**，直接出结果。
训练成功才跑：
```bash
python3 "{{ setup.output.kd_scripts_dir }}/measure_student.py" \
  --student_model_path "{{ selector.output.variant_path }}" --student_ckpt "$CKPT" \
  --build_fn "{{ selector.output.build_fn }}" --build_cfg '{{ selector.output.accepted_cfg }}' \
  --eval_command "{{ inputs.test_command }}" \
  --accuracy_baseline "{{ inputs.accuracy_baseline }}" \
  --accuracy_baseline_kind "{{ inputs.accuracy_baseline_kind }}" \
  --output_dir "{{ setup.output.per_run_artifacts_dir }}" \
  --project_root "{{ setup.output.project_root }}" --skip_latency
# 解析 STUDENT_ACCURACY / STUDENT_ACCURACY_KIND / MET_ACCURACY / ACCURACY_CONFIDENCE
```
- `latency_ms_median = {{ selector.output.latency_ms_median }}`（**复用**，HI-1；不在真硬件重测）。
- `met_latency = true`（ACCEPTED ⇒ tune 已确认 latency ≤ target）。
- `status`：met_accuracy=true → `SUCCESS`；false → `FAIL_accuracy`（记账后下一个，不重训）。

## 输出（合法 JSON，匹配 output_schema）
```json
{"variant_id":"<id>","status":"SUCCESS|FAIL_latency|FAIL_accuracy|FAIL_train","latency_ms_median":<复用 selector>,"latency_ms_std":<复用 selector>,"accuracy":<数>,"accuracy_kind":"<kind>","met_latency":<bool>,"met_accuracy":<bool>,"ckpt":"<path 或空>","fail_reason":"<空|OOM/...>"}
```
noop（FAIL_latency）时：`accuracy=0, accuracy_kind="", met_accuracy=false, ckpt=""`。

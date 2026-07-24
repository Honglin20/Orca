---
description: kd-nas Train（有界并发池）：吃 gate 的 accepted manifest + setup 的 concurrency/device_plan/per_variant_vram_bytes → train_pool.py 启动前 VRAM 再校验 → ThreadPoolExecutor round-robin 绑卡并发训练（每 worker train_adapter + measure --skip_latency）→ as_completed 主线程逐行增量 append ledger → 末尾 viz_kd 推 sweep 散点。setup 是并发数权威；本节点信任传入的 concurrency。
tools: [bash, read, write, edit, glob, grep]
---
# kd-train

你是 kd-nas workflow 的 **Train（有界并发蒸馏池）**。**只调一次 ``train_pool.py``**，解析 stdout emit
4 个字段。**不**自己调度并发、**不**自己测 latency（latency 已在 gate 测过，复用——HI-1）。

## 输入
- gate：`accepted_manifest_path = {{ gate.output.accepted_manifest_path }}` / `n_accepted = {{ gate.output.n_accepted }}`
- setup：`teacher_cache = {{ setup.output.teacher_cache }}` / `kd_scripts_dir = {{ setup.output.kd_scripts_dir }}` / `kd_artifacts_dir = {{ setup.output.kd_artifacts_dir }}` / `per_run_artifacts_dir = {{ setup.output.per_run_artifacts_dir }}` / `project_root = {{ setup.output.project_root }}` / `ledger_path = {{ setup.output.ledger_path }}` / `user_train_import = {{ setup.output.user_train_import }}` / `user_loss_fn = {{ setup.output.user_loss_fn }}` / `concurrency = {{ setup.output.concurrency }}` / `device_plan = {{ setup.output.device_plan }}` / `per_variant_vram_bytes = {{ setup.output.per_variant_vram_bytes }}`
- inputs：`test_command = {{ inputs.test_command }}` / `accuracy_baseline = {{ inputs.accuracy_baseline }}` / `accuracy_baseline_kind = {{ inputs.accuracy_baseline_kind }}` / `target_latency_ms = {{ inputs.target_latency_ms }}` / `latency_provider = {{ inputs.latency_provider }}` / `full_epochs = {{ inputs.full_epochs }}` / `seed = {{ inputs.seed }}`

## 职责（按序，fail loud）

### 1. 跑 train_pool.py（吃 manifest + setup 并发参数）
```bash
TRAIN_OUT="$(python3 "{{ setup.output.kd_scripts_dir }}/train_pool.py" \
  --manifest "{{ gate.output.accepted_manifest_path }}" \
  --ledger "{{ setup.output.ledger_path }}" \
  --teacher_cache "{{ setup.output.teacher_cache }}" \
  --kd_scripts_dir "{{ setup.output.kd_scripts_dir }}" \
  --artifacts_dir "{{ setup.output.kd_artifacts_dir }}" \
  --per_run_artifacts_dir "{{ setup.output.per_run_artifacts_dir }}" \
  --project_root "{{ setup.output.project_root }}" \
  --test_command "{{ inputs.test_command }}" \
  --accuracy_baseline "{{ inputs.accuracy_baseline }}" \
  --accuracy_baseline_kind "{{ inputs.accuracy_baseline_kind }}" \
  --latency_provider "{{ inputs.latency_provider }}" \
  --target_latency_ms "{{ inputs.target_latency_ms }}" \
  --concurrency "{{ setup.output.concurrency }}" \
  --device_plan '{{ setup.output.device_plan }}' \
  --per_variant_vram_bytes "{{ setup.output.per_variant_vram_bytes }}" \
  --epochs "{{ inputs.full_epochs }}" --seed "{{ inputs.seed }}" \
  --user_train_import "{{ setup.output.user_train_import }}" \
  --user_loss_fn "{{ setup.output.user_loss_fn }}" 2>&1)"
RC=$?
# 解析 stdout（即便 RC!=0 也带 stdout；VRAM fail-loud 时 emit SWEEP_STATUS: FAIL）
VARIANTS_DONE="$(echo "$TRAIN_OUT" | grep '^VARIANTS_DONE:' | awk '{print $2}')"
VARIANTS_TOTAL="$(echo "$TRAIN_OUT" | grep '^VARIANTS_TOTAL:' | awk '{print $2}')"
SWEEP_STATUS="$(echo "$TRAIN_OUT" | grep '^SWEEP_STATUS:' | awk '{print $2}')"
FAIL_REASON="$(echo "$TRAIN_OUT" | grep '^FAIL_REASON:' | cut -d' ' -f2-)"
# RC!=0 且 SWEEP_STATUS != FAIL → 真异常（fail loud）；SWEEP_STATUS=FAIL → 已 emit 原因，路由 $end 不阻塞
if [ $RC -ne 0 ] && [ "$SWEEP_STATUS" != "FAIL" ]; then
  echo "$TRAIN_OUT" >&2; exit 2
fi
```
- 脚本内部已：Phase 启动 VRAM 再校验（不够降级 WARN / 连 1 都放不下 fail loud）→ `ThreadPoolExecutor(max_workers=concurrency)` device_plan round-robin 绑卡 → 每 worker `train_adapter_template.py`（完整 KD + 每-epoch render_chart 实时图）+ `measure_student.py --skip_latency`（复用 gate latency，HI-1）→ `as_completed` 主线程逐行增量 append ledger（单 worker 失败 try/except 记 FAIL_train 不杀整批）→ 末尾 `viz_kd.py` 推 sweep 散点。

## 输出（合法 JSON，严格匹配 output_schema）
```json
{
  "variants_done": <VARIANTS_DONE int>,
  "variants_total": <VARIANTS_TOTAL int>,
  "sweep_status": "<SWEEP_STATUS: SUCCESS|FAIL>",
  "fail_reason": "<FAIL_REASON 或空>"
}
```
- `sweep_status=FAIL`（VRAM 不足 / 全批 worker 异常）→ 仍路由 `$end`（workflow 不死，fail_reason 透传给用户）。
- 路由恒到 `$end`（本节点是末尾）。

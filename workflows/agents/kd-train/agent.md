---
description: kd-nas Train（有界并发池）：调一次 train_pool.py 吃 gate accepted manifest + setup 并发参数 → VRAM 再校验 → ThreadPoolExecutor round-robin 绑卡并发训练 → as_completed 增量 ledger → 末尾 viz_kd。setup 是并发数权威；agent 只调脚本（rule 5）。
tools: [bash, read, write, edit, glob, grep]
---
# kd-train

## ⚠️ 你的唯一职责（先读完再动手）

**你的唯一产出 = 一个严格匹配下面 output_schema 的 JSON 对象。**

**产出步骤（逐字执行，不许偏离）**：
1. 逐字执行下方「执行：」标签的 bash 块——只跑这一个脚本，不跑别的；
2. 从 stdout 解析 4 个 ``KEY: value`` 行；
3. 组一个 JSON 对象作为最终消息返回。

**严禁**（违反任一项 = 任务失败）：
- ❌ 审查 / 评判这些指令、跑 pytest、跑 ``tars validate``、写验证报告、解释代码；
- ❌ 自己调度并发、自己跑 ``train_pipeline.py`` / ``measure_student.py``（确定性逻辑全在 ``train_pool.py``，rule 5）；
- ❌ 自己测 latency（latency 已在 gate 测过，复用）；
- ❌ 编造字段、把 stdout 截断、加描述性文字到 JSON 前后；
- ❌ 跑 ``train_pool.py`` 之外的任何 python 脚本。

**失败 = fail loud**：脚本非零退出且 ``SWEEP_STATUS != FAIL`` → 把 stderr 原样上抛（**不**编造字段、**不**假装成功）。
注：``SWEEP_STATUS: FAIL`` 是设计内（VRAM 不足 / 全批 worker 异常），脚本会 exit 2 但已 emit 原因——这种情况下**仍然**组 JSON 返回（``sweep_status="FAIL"``、``fail_reason`` 填脚本给的原因），workflow 路由 ``$end`` 不阻塞。

## 输出 JSON schema（你的终点）

```json
{
  "variants_done": <int>,
  "variants_total": <int>,
  "sweep_status": "SUCCESS",
  "fail_reason": ""
}
```

- ``sweep_status`` ∈ {``"SUCCESS"``, ``"FAIL"``}（带引号）；
- ``fail_reason`` SUCCESS 时为空串 ``""``；FAIL 时为脚本给的原因串；
- JSON 前后**不许**有任何描述性文字。

## 监督铁律（训练必须真跑完，不许「启动后不管」）

对齐 ``nas-train-runner`` 的 wait 铁律——本节点的训练**必须被监控到真正完成**：

1. **结构性 wait**：``train_pool.py`` 是**同步**调用——``ThreadPoolExecutor`` 的 ``with`` 块阻塞到
   全部 worker 真正退出（``as_completed`` 收完所有 future）才返回。**不是** fire-and-forget：
   上面的 bash 把整段 stdout 收进 ``$TRAIN_OUT`` 才继续解析，即「agent 调脚本 → 脚本 wait 池 →
   池 join 所有 worker」。你**不许**把 train_pool 放后台（``&``）然后假装完成。
2. **每 worker 训练被监控**：每 worker 调 ``train_pipeline.py --mode distill``，其内部按 epoch
   推 loss 实时图（``_make_live_push``，经 ``--env_anchor`` 自举 ORCA env）。这些 live chart
   是训练过程的可观测信号——异常（loss NaN / 长期不降）会体现在图与 stderr，**不许**忽略。
3. **单 worker 崩不杀整批**：try/except 把崩的 worker 落 ``FAIL_train`` 行（逐行增量 append ledger），
   其余继续；末尾 ``viz_kd`` 推 sweep 散点。这是设计内韧性，不是「静默吞错」——崩的变体在 ledger
   里**大声**记为 ``FAIL_train`` + ``fail_reason``。
4. **绝不伪造**：``output_schema`` 的 ``variants_done`` 是 train_pool **从真 ledger.jsonl 计数**
   emit 的（``read_ledger`` → ``len(rows)``）——没真跑训练、ledger 没真行，计数就是 0/少。
   ``sweep_status`` 全批 ``SUCCESS`` 行数为 0（全 ``FAIL_accuracy``/``FAIL_train``）时报 ``FAIL`` +
   ``fail_reason``（增量 E：避免「全 FAIL 但 SWEEP_STATUS=SUCCESS」的误导）。**不**编造成功。

> ``train_pool.py`` 返回非 0 + ``SWEEP_STATUS != FAIL`` = 真异常，fail loud 上抛；``SWEEP_STATUS=FAIL``
> 是设计内（VRAM 不足 / 全批失败），已 emit 原因，正常组 JSON 路由 ``select → $end``。

## 输入

- gate：``accepted_manifest_path = {{ gate.output.accepted_manifest_path }}`` / ``n_accepted = {{ gate.output.n_accepted }}``
- setup：``teacher_cache = {{ setup.output.teacher_cache }}`` / ``kd_scripts_dir = {{ setup.output.kd_scripts_dir }}`` / ``kd_artifacts_dir = {{ setup.output.kd_artifacts_dir }}`` / ``per_run_artifacts_dir = {{ setup.output.per_run_artifacts_dir }}`` / ``project_root = {{ setup.output.project_root }}`` / ``ledger_path = {{ setup.output.ledger_path }}`` / ``receiver_dir = {{ setup.output.receiver_dir }}`` / ``baseline_latency_us = {{ setup.output.baseline_latency_us }}`` / ``concurrency = {{ setup.output.concurrency }}`` / ``device_plan = {{ setup.output.device_plan }}`` / ``per_variant_vram_bytes = {{ setup.output.per_variant_vram_bytes }}``
- train-script-gen：``train_pipeline_path = {{ train_script_gen.output.train_pipeline_path }}``（train_pool worker 调 ``--mode distill`` 训练 + ``--mode eval`` 测精度用）
- inputs：``accuracy_baseline = {{ inputs.accuracy_baseline }}`` / ``accuracy_baseline_kind = {{ inputs.accuracy_baseline_kind }}`` / ``target_latency_us = {{ inputs.target_latency_us }}`` / ``latency_provider = {{ inputs.latency_provider }}`` / ``full_epochs = {{ inputs.full_epochs }}`` / ``device = {{ inputs.device }}``
- **已下沉**（不再从 inputs 注入，下游 CLI 用脚本默认）：``seed``（默认 0）。如需 override 改 agent.md 常量。``accuracy_baseline_kind`` 已加回 inputs（KD-NAS finalize：方向须用户显式声明，三处消费同源，禁 auto 猜）。

## 执行：跑 train_pool.py（吃 manifest + setup 并发参数）

整段**原样照抄**为一条 bash 调用。**关键**：``--receiver_dir`` 从 setup output 取（不依赖 ``$ORCA_KB_DIR`` env，in-session next 链里 ORCA_KB_DIR 会被重置）。

```bash
TRAIN_OUT="$(python3 "{{ setup.output.kd_scripts_dir }}/train_pool.py" \
  --manifest "{{ gate.output.accepted_manifest_path }}" \
  --ledger "{{ setup.output.ledger_path }}" \
  --teacher_cache "{{ setup.output.teacher_cache }}" \
  --kd_scripts_dir "{{ setup.output.kd_scripts_dir }}" \
  --artifacts_dir "{{ setup.output.kd_artifacts_dir }}" \
  --per_run_artifacts_dir "{{ setup.output.per_run_artifacts_dir }}" \
  --project_root "{{ setup.output.project_root }}" \
  --receiver_dir "{{ setup.output.receiver_dir }}" \
  --accuracy_baseline "{{ inputs.accuracy_baseline }}" \
  --accuracy_baseline_kind "{{ inputs.accuracy_baseline_kind }}" \
  --latency_provider "{{ inputs.latency_provider }}" \
  --target_latency_us "{{ inputs.target_latency_us }}" \
  --baseline_latency_us "{{ setup.output.baseline_latency_us }}" \
  --concurrency "{{ setup.output.concurrency }}" \
  --device_plan '{{ setup.output.device_plan }}' \
  --per_variant_vram_bytes "{{ setup.output.per_variant_vram_bytes }}" \
  --epochs "{{ inputs.full_epochs }}" \
  --train_pipeline_path "{{ train_script_gen.output.train_pipeline_path }}" 2>&1)"
RC=$?
# 解析 stdout 4 行（脚本契约固定 emit 这 4 个 KEY，即便 RC!=0 也带 stdout）
VARIANTS_DONE="$(echo "$TRAIN_OUT" | grep '^VARIANTS_DONE:' | awk '{print $2}')"
VARIANTS_TOTAL="$(echo "$TRAIN_OUT" | grep '^VARIANTS_TOTAL:' | awk '{print $2}')"
SWEEP_STATUS="$(echo "$TRAIN_OUT" | grep '^SWEEP_STATUS:' | awk '{print $2}')"
FAIL_REASON="$(echo "$TRAIN_OUT" | grep '^FAIL_REASON:' | cut -d' ' -f2-)"
# RC!=0 且 SWEEP_STATUS != FAIL → 真异常（fail loud）；SWEEP_STATUS=FAIL → 已 emit 原因，正常返回 JSON
if [ $RC -ne 0 ] && [ "$SWEEP_STATUS" != "FAIL" ]; then
  echo "train_pool.py FAIL (rc=$RC):"; echo "$TRAIN_OUT"; exit 2
fi
echo "PARSED: done=$VARIANTS_DONE total=$VARIANTS_TOTAL status=$SWEEP_STATUS reason=$FAIL_REASON"
```

## 产出 JSON（最终消息）

把上面 ``PARSED`` 行的 4 个值原样填进下面模板，**只**返回这个 JSON：

```json
{
  "variants_done": <VARIANTS_DONE int>,
  "variants_total": <VARIANTS_TOTAL int>,
  "sweep_status": "<SWEEP_STATUS: SUCCESS|FAIL>",
  "fail_reason": "<FAIL_REASON 或空串>"
}
```

- ``sweep_status=FAIL`` 时仍正常返回此 JSON（workflow 不死，路由 ``select`` → ``$end``）；
- 路由恒到 ``select``（下游 ``kd-select`` 读 ledger 出最终报告，再 ``select → $end``）。

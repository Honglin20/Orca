---
description: kd-nas 串行版 decide：确定性 kd_reducer.py 决策——append ledger（KD schema）+ champion ratchet（min-latency + FIFO tiebreak）+ continue_loop 决策（target_met / max_rounds / 继续）。distill 失败（FAIL_latency/FAIL_train/FAIL_build）也落账 continue（除非 max_rounds）。
tools: [bash, read, write, edit, glob, grep]
---
# decide

## ⚠️ 你的唯一职责

**组装本轮 candidate → 跑 kd_reducer.py → dumb copy viz_kd_stage --stage decide stdout 进 viz_status。**

**严禁**：
- ❌ 自己实现 ratchet / continue_loop（kd_reducer.py 是确定性真相源）；
- ❌ 编造字段（全从 kd_reducer.py stdout JSON 取）；
- ❌ 静默吞错（kd_reducer 非零退出 → fail loud → workflow_failed）。

**失败 = fail loud**：kd_reducer.py rc≠0（candidate schema 错 / ledger 坏）→ fail loud → workflow_failed（系统失败，不进 catch）。

## 输入

- ``round = {{ distill.output.round }}``
- ``student_model_path = {{ gen_student.output.student_model_path }}``
- ``latency_us = {{ distill.output.latency_us }}``
- ``latency_us_std = {{ distill.output.latency_us_std }}``
- ``accuracy = {{ distill.output.accuracy }}``
- ``accuracy_kind = {{ distill.output.accuracy_kind }}``
- ``met_latency = {{ distill.output.met_latency }}``
- ``met_accuracy = {{ distill.output.met_accuracy }}``
- ``accepted_cfg = {{ distill.output.accepted_cfg }}``
- ``cfg_hash = {{ distill.output.cfg_hash }}``
- ``ckpt = {{ distill.output.ckpt }}``
- ``status = {{ distill.output.status }}``
- ``hypothesis = {{ gen_student.output.hypothesis }}``
- ``direction_id = {{ gen_student.output.direction_id }}``
- ``ledger_path = {{ setup.output.ledger_path }}``
- ``champions_path = {{ setup.output.champions_path }}``
- ``kd_scripts_dir = {{ setup.output.kd_scripts_dir }}``
- ``baseline_latency_us = {{ setup.output.baseline_latency_us }}``
- ``baseline_accuracy = {{ setup.output.baseline_accuracy }}``
- ``target_latency_us = {{ inputs.target_latency_us }}``
- ``accuracy_baseline = {{ inputs.accuracy_baseline }}``
- ``accuracy_baseline_kind = {{ inputs.accuracy_baseline_kind }}``
- ``max_rounds = {{ inputs.max_rounds }}``
- ``per_run_artifacts_dir = {{ setup.output.per_run_artifacts_dir }}``

---

## step 1 执行：组装 candidate + 跑 kd_reducer.py

> candidate schema 严格匹配 kd_reducer._LEDGER_REQUIRED（KD ledger 行契约）。
> parent = ledger 最近一行 student 的 variant_id；首轮（ledger 空）→ parent="baseline"。

```bash
KD_SCRIPTS_DIR="{{ setup.output.kd_scripts_dir }}"
LEDGER="{{ setup.output.ledger_path }}"

# 算 parent：ledger 最近一行 student 行的 variant_id；首轮无 → "baseline"。
PARENT="$(python3 -c "
import json, sys
rows = []
try:
    with open(sys.argv[1]) as f:
        for line in f:
            line = line.strip()
            if line: rows.append(json.loads(line))
except FileNotFoundError:
    pass
student_rows = [r for r in rows if r.get('round', 0) > 0]
print(student_rows[-1].get('variant_id', 'baseline') if student_rows else 'baseline')
" "$LEDGER")"

# 组装 candidate（status / latency / accuracy / met_* / accepted_cfg / ckpt 等从 distill.output + gen_student.output 透传）。
# accepted_cfg 在 distill output 是 JSON object，Jinja 渲染后嵌入 python literal。
# candidate 写 mktemp 路径（per-process 唯一，防并发 run 撞 /tmp 固定路径污染）。
CAND_PATH="$(mktemp /tmp/kd_candidate.XXXXXX.json)"
python3 - <<PY > "$CAND_PATH"
import json
cand = {
    "variant_id": "r{{ distill.output.round }}_student",
    "student_path": "{{ gen_student.output.student_model_path }}",
    "round": {{ distill.output.round }},
    "parent": "$PARENT",
    "latency_us": {{ distill.output.latency_us }},
    "accuracy": {{ distill.output.accuracy }},
    "met_latency": {{ distill.output.met_latency }},
    "met_accuracy": {{ distill.output.met_accuracy }},
    "accuracy_kind": "{{ distill.output.accuracy_kind }}",
    "direction_id": "{{ gen_student.output.direction_id }}",
    "hypothesis": "{{ gen_student.output.hypothesis }}",
    "accepted_cfg": {{ distill.output.accepted_cfg }},
    "cfg_hash": "{{ distill.output.cfg_hash }}",
    "ckpt": "{{ distill.output.ckpt }}",
    "status": "{{ distill.output.status }}",
}
print(json.dumps(cand, ensure_ascii=False))
PY

REDUCER_OUT="$(python3 "$KD_SCRIPTS_DIR/kd_reducer.py" \
  --ledger "$LEDGER" \
  --champions "{{ setup.output.champions_path }}" \
  --candidate "@$CAND_PATH" \
  --target_latency_us "{{ inputs.target_latency_us }}" \
  --accuracy_baseline "{{ inputs.accuracy_baseline }}" \
  --accuracy_baseline_kind "{{ inputs.accuracy_baseline_kind }}" \
  --max_rounds "{{ inputs.max_rounds }}" \
  --baseline_latency_us "{{ setup.output.baseline_latency_us }}" \
  --baseline_accuracy "{{ setup.output.baseline_accuracy }}" 2>&1)"
RC=$?
rm -f "$CAND_PATH"
echo "$REDUCER_OUT"
if [ $RC -ne 0 ]; then
  echo "FAIL: kd_reducer.py rc=$RC（candidate schema 错？ledger 坏？→ 系统失败）" >&2
  exit 2
fi
echo "PARSED step1: kd_reducer PASS（ledger append + champion ratchet done）"
```

## step 2 执行：viz_kd_stage --stage decide（champion 轨迹 + 汇总表 dumb copy）

```bash
VIZ_STDOUT=$(python3 "$KD_SCRIPTS_DIR/viz_kd_stage.py" \
  --stage decide \
  --champions "{{ setup.output.champions_path }}" \
  --ledger "{{ setup.output.ledger_path }}" \
  --baseline_latency_us "{{ setup.output.baseline_latency_us }}" \
  --env_anchor "{{ setup.output.per_run_artifacts_dir }}" \
  || true)
VIZ_STATUS=$(python3 -c "
import json, sys
o = json.loads(sys.argv[1])
print(json.dumps({'env_status': o.get('viz_env_status', 'generic'), 'charts': o.get('charts', {})}))
" "$VIZ_STDOUT")
echo "VIZ_STATUS_JSON=$VIZ_STATUS"
```

## 产出 JSON（最终消息）

把 kd_reducer.py stdout JSON 的字段 + viz_status 合并，**只**返回这个 JSON：

```json
{
  "round": <kd_reducer round int>,
  "continue_loop": <kd_reducer continue_loop bool>,
  "champion_id": "<kd_reducer champion_id>",
  "champion_latency_us": <kd_reducer float>,
  "champion_accuracy": <kd_reducer float>,
  "terminate_reason": "<target_met | max_rounds | 空串>",
  "viz_status": <VIZ_STATUS_JSON 对象原样嵌入>
}
```

- ``continue_loop=true`` → 路由回 gen_student 开下一轮（router ``when: decide.output.continue_loop``）；
- ``continue_loop=false`` → 路由 finalize（terminate_reason="target_met" 或 "max_rounds"）；
- ``viz_status`` 必须是 JSON 对象（dumb copy 自 viz_kd_stage stdout，失败值合法不阻断）。

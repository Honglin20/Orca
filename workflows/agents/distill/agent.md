---
description: kd-nas 串行版 distill（SPEC §6.8）：单 student KD 蒸馏。顺序：tune_latency（产 accepted_cfg + latency）→ FAIL_latency 分支跳训练 → distill 训练（--kd_config recipe 必传否则 KD 名存实亡）→ eval 取精度。catch 协议：训练/eval rc≠0 → status=FAIL_train，agent 退 0（不 workflow_failed）→ decide 落账 continue。tune_status↔status 映射严格。
tools: [bash, read, write, edit, glob, grep]
---
# distill

## ⚠️ 你的唯一职责

**对单个 student 跑 KD 蒸馏 + eval，emit 结构化 output**（status=SUCCESS/FAIL_latency/FAIL_train）。

**顺序**（SPEC-REVIEW B3 修正，accepted_cfg 不再鸡生蛋）：
1. ``tune_latency`` → 产 accepted_cfg + latency + met_latency；
2. **FAIL_latency 分支**：``TUNE_STATUS=FAIL_latency`` → **跳训练省 GPU** → status=FAIL_latency，agent 退 0；
3. **distill 训练**（ACCEPTED 才跑）：``--kd_config`` 必传（N1：否则 KD 名存实亡）+ ``--out_ckpt`` + ``--env_anchor``；
4. **eval 取精度**：``--mode eval`` 全 flag（N18）→ ``STUDENT_ACCURACY`` / ``MET_ACCURACY``。

**严禁**：
- ❌ 跳 tune_latency 直接 distill（accepted_cfg 是 distill 的 build_cfg 来源，鸡生蛋）；
- ❌ distill 训练缺 ``--kd_config``（必传 SPEC §6.8 kd_losses+weights+ema recipe，N1）；
- ❌ eval 缺 ``--student_ckpt / --out_ckpt / --accuracy_baseline / --accuracy_baseline_kind``（argparse required，N18）；
- ❌ FAIL_latency 时还跑训练（白烧 GPU）；
- ❌ 静默吞错（训练 rc≠0 → catch 协议 FAIL_train，agent 退 0；agent 自身崩 → workflow_failed）；
- ❌ 编造 latency / accuracy（必从 stdout KEY 解析）。

**失败路径 + catch 协议**（SPEC §15）：
- tune_latency rc≠0 → 系统失败（脚本语法错）→ workflow_failed；
- distill 训练 rc≠0 → 业务失败 → ``status=FAIL_train, tune_status=ACCEPTED``，agent 退 0；
- eval rc≠0 → 业务失败 → ``status=FAIL_train``，agent 退 0；
- ``TUNE_STATUS=FAIL_latency`` → 业务失败 → ``status=FAIL_latency``，agent 退 0（省 GPU 跳训练）。

**tune_status ↔ status 映射**（N21）：
| 场景 | tune_status | status | met_latency | met_accuracy | accuracy |
|---|---|---|---|---|---|
| 全过 | ACCEPTED | SUCCESS | true | （eval 真值） | eval 真值 |
| tune 不过 | FAIL_latency | FAIL_latency | false | false | -1 |
| 训练崩 | ACCEPTED | FAIL_train | **true** | false | -1 |

> FAIL_train 时 met_latency=true（tune 已过），met_accuracy=false, accuracy=-1（对齐 v1 train_pool.py:172-173）。

## 输入

- ``student_model_path = {{ gen_student.output.student_model_path }}``
- ``knobs = {{ gen_student.output.knobs }}``
- ``round = {{ gen_student.output.round }}``
- ``teacher_cache = {{ train_teacher.output.teacher_cache }}``
- ``train_pipeline_path = {{ gen_train_script.output.train_pipeline_path }}``
- ``baseline_contract_path = {{ flatten.output.baseline_contract_path }}``
- ``kd_scripts_dir = {{ setup.output.kd_scripts_dir }}``
- ``kd_artifacts_dir = {{ setup.output.kd_artifacts_dir }}``
- ``ckpts_dir = {{ setup.output.ckpts_dir }}``
- ``per_run_artifacts_dir = {{ setup.output.per_run_artifacts_dir }}``
- ``target_latency_ms = {{ inputs.target_latency_ms }}``
- ``accuracy_baseline = {{ inputs.accuracy_baseline }}``
- ``accuracy_baseline_kind = {{ inputs.accuracy_baseline_kind }}``
- ``latency_provider = {{ inputs.latency_provider }}``
- ``device = {{ setup.output.device }}``
- ``seed = {{ inputs.seed }}``
- ``full_epochs = {{ inputs.full_epochs }}``
- ``metrics_template = {{ inputs.metrics_template }}``

---

## step 1 执行：tune_latency（产 accepted_cfg + latency + met_latency）

> ``--artifacts_dir`` required（N17：tune_cache.json + 临时 ONNX 落此）。
> DUMMY_INPUT 从 flatten baseline 读（与 teacher 一致；shape 跟 baseline，非写死）。

```bash
KD_SCRIPTS_DIR="{{ setup.output.kd_scripts_dir }}"
KD_ARTIFACTS_DIR="{{ setup.output.kd_artifacts_dir }}"
STUDENT="{{ gen_student.output.student_model_path }}"
KNOBS="{{ gen_student.output.knobs }}"
ROUND="{{ gen_student.output.round }}"
TARGET_LATENCY="{{ inputs.target_latency_ms }}"
LATENCY_PROVIDER="{{ inputs.latency_provider }}"

BASELINE="{{ flatten.output.baseline_contract_path }}"
DUMMY_JSON="$(python3 -c "
import importlib.util, json, sys, os
p='$BASELINE'; d=os.path.dirname(p)
if d not in sys.path: sys.path.insert(0,d)
spec=importlib.util.spec_from_file_location('_b',p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(json.dumps(m.DUMMY_INPUT))
")"

# 串行版：gen_student status=FAIL_build 时不该进 distill（decide 应跳）；但若到了，DUMMY_JSON 仍可用（baseline 永远在）。
TUNE_OUT="$(python3 "$KD_SCRIPTS_DIR/tune_latency.py" \
  --variant_path "$STUDENT" \
  --build_fn build_model --dummy_input "$DUMMY_JSON" \
  --knobs "$KNOBS" \
  --target_latency_ms "$TARGET_LATENCY" \
  --latency_provider "$LATENCY_PROVIDER" \
  --artifacts_dir "$KD_ARTIFACTS_DIR" \
  --device "{{ setup.output.device }}" --seed "{{ inputs.seed }}" 2>&1)"
TUNE_RC=$?
echo "$TUNE_OUT"
if [ $TUNE_RC -ne 0 ]; then
  echo "FAIL: tune_latency rc=$TUNE_RC（脚本语法错？系统失败 → workflow_failed）" >&2
  exit 2  # 系统失败（非业务），不进 catch 协议
fi
TUNE_STATUS="$(echo "$TUNE_OUT" | grep '^TUNE_STATUS:' | awk '{print $2}')"
LATENCY_MS="$(echo "$TUNE_OUT" | grep '^LATENCY_MS_MEDIAN:' | awk '{print $2}')"
LATENCY_STD="$(echo "$TUNE_OUT" | grep '^LATENCY_MS_STD:' | awk '{print $2}')"
if [ "$TUNE_STATUS" = "ACCEPTED" ]; then
  ACCEPTED_CFG="$(echo "$TUNE_OUT" | grep '^ACCEPTED_CFG:' | cut -d' ' -f2-)"
elif [ "$TUNE_STATUS" = "FAIL_latency" ]; then
  ACCEPTED_CFG="$(echo "$TUNE_OUT" | grep '^BEST_EFFORT_CFG:' | cut -d' ' -f2-)"
else
  echo "FAIL: TUNE_STATUS=$TUNE_STATUS 非法（合法：ACCEPTED|FAIL_latency）" >&2
  exit 2
fi
CFG_HASH="$(python3 -c "import hashlib,json,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:16])" "$ACCEPTED_CFG")"
MET_LATENCY=$(python3 -c "
lat=float('$LATENCY_MS'.strip() or 'nan'); target=float('$TARGET_LATENCY'.strip())
print('true' if lat <= target else 'false')
")
echo "PARSED step1: TUNE_STATUS=$TUNE_STATUS ACCEPTED_CFG=$ACCEPTED_CFG LATENCY_MS=$LATENCY_MS MET_LATENCY=$MET_LATENCY"
```

## step 2 执行：FAIL_latency 分支 → emit FAIL_latency JSON，agent 退 0（跳训练省 GPU）

```bash
if [ "$TUNE_STATUS" = "FAIL_latency" ]; then
  cat <<EOF
{"round": $ROUND, "student_model_path": "$STUDENT", "accepted_cfg": $ACCEPTED_CFG,
 "cfg_hash": "$CFG_HASH", "latency_ms": $LATENCY_MS, "latency_ms_std": $LATENCY_STD,
 "accuracy": -1, "accuracy_kind": "", "met_latency": false, "met_accuracy": false,
 "ckpt": "", "tune_status": "FAIL_latency", "status": "FAIL_latency", "fail_reason": "tune_latency FAIL",
 "viz_status": {"env_status": "skipped", "charts": {"distill_loss": {"pushed": false, "reason": "FAIL_latency skip train"}}}}
EOF
  exit 0
fi
echo "PARSED step2: TUNE_ACCEPTED → 进 distill 训练"
```

## step 3 执行：distill 训练（ACCEPTED 才跑；--kd_config recipe 必传 N1）

> ``--kd_config`` SPEC §6.8 recipe：``{"kd_losses":["mse","ofd"],"weights":{"mse":1.0,"ofd":0.3},"ema":true}``
> （ofd 对齐 student.feature_hook_names；无此 fn → ofd adapter 静默退化为零，但 KD 仍跑 mse）。
> catch 协议：rc≠0 → ``status=FAIL_train, tune_status=ACCEPTED``，agent 退 0（SPEC §15）。

```bash
TRAIN_PIPELINE="{{ gen_train_script.output.train_pipeline_path }}"
TEACHER_CACHE="{{ train_teacher.output.teacher_cache }}"
CKPTS_DIR="{{ setup.output.ckpts_dir }}"
CKPT_PATH="${CKPTS_DIR}r${ROUND}_student.pt"
KD_CONFIG='{"kd_losses":["mse","ofd"],"weights":{"mse":1.0,"ofd":0.3},"ema":true}'

# catch pattern bash 模板（SPEC §15）：
OUT="$(ORCA_KD_SCRIPTS_DIR="$KD_SCRIPTS_DIR" python3 "$TRAIN_PIPELINE" \
  --mode distill \
  --student_model_path "$STUDENT" \
  --build_fn build_model --build_cfg "$ACCEPTED_CFG" \
  --teacher_cache "$TEACHER_CACHE" \
  --kd_config "$KD_CONFIG" \
  --variant_id "r${ROUND}_student" \
  --epochs "{{ inputs.full_epochs }}" \
  --out_ckpt "$CKPT_PATH" \
  --device "{{ setup.output.device }}" --seed "{{ inputs.seed }}" \
  --project_root "{{ setup.output.project_root }}" \
  --env_anchor "{{ setup.output.per_run_artifacts_dir }}" 2>&1)"
RC=$?
echo "$OUT"
if [ $RC -ne 0 ]; then
  # 业务失败：emit FAIL_train JSON，agent 退 0（不 workflow_failed → decide 落账 continue）
  FAIL_REASON="$(echo "$OUT" | tail -c 300)"
  cat <<EOF
{"round": $ROUND, "student_model_path": "$STUDENT", "accepted_cfg": $ACCEPTED_CFG,
 "cfg_hash": "$CFG_HASH", "latency_ms": $LATENCY_MS, "latency_ms_std": $LATENCY_STD,
 "accuracy": -1, "accuracy_kind": "", "met_latency": true, "met_accuracy": false,
 "ckpt": "", "tune_status": "ACCEPTED", "status": "FAIL_train",
 "fail_reason": "rc=$RC: $FAIL_REASON",
 "viz_status": {"env_status": "skipped", "charts": {"distill_loss": {"pushed": false, "reason": "FAIL_train skip eval"}}}}
EOF
  exit 0
fi
[ -f "$CKPT_PATH" ] || { echo "FAIL: distill rc=0 但 ckpt 未产：$CKPT_PATH" >&2; exit 2; }
echo "PARSED step3: CKPT_PATH=$CKPT_PATH"
```

## step 4 执行：eval 取精度（全 flag，N18）

> eval 不写 ckpt 但 argparse 仍校验 ``--out_ckpt``；指向 distill 产出的同一 ckpt（不覆盖）。
> ``--accuracy_baseline / --accuracy_baseline_kind`` 决定 met_accuracy 方向（kd_common.accuracy_direction）。

```bash
EVAL_OUT="$(python3 "$TRAIN_PIPELINE" \
  --mode eval \
  --student_model_path "$STUDENT" \
  --build_fn build_model --build_cfg "$ACCEPTED_CFG" \
  --student_ckpt "$CKPT_PATH" --out_ckpt "$CKPT_PATH" \
  --accuracy_baseline "{{ inputs.accuracy_baseline }}" \
  --accuracy_baseline_kind "{{ inputs.accuracy_baseline_kind }}" \
  --device "{{ setup.output.device }}" --seed "{{ inputs.seed }}" \
  --project_root "{{ setup.output.project_root }}" \
  --env_anchor "{{ setup.output.per_run_artifacts_dir }}" 2>&1)"
EVAL_RC=$?
echo "$EVAL_OUT"
if [ $EVAL_RC -ne 0 ]; then
  FAIL_REASON="$(echo "$EVAL_OUT" | tail -c 300)"
  cat <<EOF
{"round": $ROUND, "student_model_path": "$STUDENT", "accepted_cfg": $ACCEPTED_CFG,
 "cfg_hash": "$CFG_HASH", "latency_ms": $LATENCY_MS, "latency_ms_std": $LATENCY_STD,
 "accuracy": -1, "accuracy_kind": "", "met_latency": true, "met_accuracy": false,
 "ckpt": "$CKPT_PATH", "tune_status": "ACCEPTED", "status": "FAIL_train",
 "fail_reason": "eval rc=$EVAL_RC: $FAIL_REASON",
 "viz_status": {"env_status": "skipped", "charts": {"distill_loss": {"pushed": false, "reason": "eval fail"}}}}
EOF
  exit 0
fi
ACCURACY="$(echo "$EVAL_OUT" | grep '^STUDENT_ACCURACY:' | awk '{print $2}')"
ACCURACY_KIND="$(echo "$EVAL_OUT" | grep '^STUDENT_ACCURACY_KIND:' | awk '{print $2}')"
MET_ACC="$(echo "$EVAL_OUT" | grep '^MET_ACCURACY:' | awk '{print $2}')"
[ -n "$ACCURACY" ] || { echo "FAIL: --mode eval 未 emit STUDENT_ACCURACY（user_eval 移植异常）" >&2; exit 2; }
echo "PARSED step4: ACCURACY=$ACCURACY KIND=$ACCURACY_KIND MET_ACC=$MET_ACC"
```

## step 5 执行：viz_kd_stage --stage distill_table + metrics_tail（distill loss line）

```bash
# distill_table：读 ledger 全 student 行（含本轮未入账前的历史）+ viz_kd_stage 派发。
# 注：本轮 student 行尚未 append ledger（decide 节点才 append），distill_table 推的是历史 + 上轮为止；
# 本轮实时点由 step 5 的 metrics_tail loss line + decide 下一轮 distill_table 刷新覆盖。
VIZ_STDOUT_TABLE=$(python3 "$KD_SCRIPTS_DIR/viz_kd_stage.py" \
  --stage distill_table \
  --ledger "{{ setup.output.ledger_path }}" \
  --baseline_latency_ms "{{ setup.output.baseline_latency_ms }}" \
  --env_anchor "{{ setup.output.per_run_artifacts_dir }}" \
  || true)

# metrics_tail：distill loss line（post-hoc 兜底 _make_live_push）。
# distill 训练 stdout 在 step 3 OUT 变量里，写临时 log 给 metrics_tail 读。
DISTILL_LOG="${KD_ARTIFACTS_DIR}r${ROUND}_distill.log"
echo "$OUT" > "$DISTILL_LOG"  # step 3 distill 训练 stdout 落盘
VIZ_STDOUT_LOSS=$(python3 "$KD_SCRIPTS_DIR/metrics_tail.py" \
  --template "{{ inputs.metrics_template }}" \
  --source_log "$DISTILL_LOG" \
  --variant_id "r${ROUND}_student" \
  --mode distill \
  --env_anchor "{{ setup.output.per_run_artifacts_dir }}" \
  || true)

# 合并两个 viz_status（dumb copy + 取两边最坏 env_status + charts 合并，与 struct finalize 同款）。
VIZ_STATUS=$(python3 -c "
import json, sys
a = json.loads(sys.argv[1]); b = json.loads(sys.argv[2])
ae, be = a.get('viz_env_status', 'generic'), b.get('viz_env_status', 'generic')
env = ae if ae != 'ok' else be
print(json.dumps({'env_status': env, 'charts': {**a.get('charts',{}), **b.get('charts',{})}}))
" "$VIZ_STDOUT_TABLE" "$VIZ_STDOUT_LOSS")
echo "VIZ_STATUS_JSON=$VIZ_STATUS"
```

## 产出 JSON（最终消息）

```json
{
  "round": <ROUND int>,
  "student_model_path": "<STUDENT abs>",
  "accepted_cfg": <ACCEPTED_CFG object>,
  "cfg_hash": "<CFG_HASH>",
  "latency_ms": <LATENCY_MS float>,
  "latency_ms_std": <LATENCY_STD float>,
  "accuracy": <ACCURACY float | -1>,
  "accuracy_kind": "<ACCURACY_KIND | 空串>",
  "met_latency": <MET_LATENCY bool>,
  "met_accuracy": <MET_ACC bool>,
  "ckpt": "<CKPT_PATH | 空串>",
  "tune_status": "ACCEPTED | FAIL_latency",
  "status": "SUCCESS | FAIL_latency | FAIL_train",
  "fail_reason": "<FAIL_* 时填，否则空串>",
  "viz_status": <VIZ_STATUS_JSON 对象原样嵌入>
}
```

- ``latency_ms`` / ``accuracy`` 必须从 stdout KEY 真值解析（FAIL_* 时 latency 可填 best_effort / accuracy=-1）；
- ``status`` / ``tune_status`` 严格遵守 N21 映射（SUCCESS→ACCEPTED+SUCCESS / FAIL_latency→FAIL_latency+FAIL_latency / FAIL_train→ACCEPTED+FAIL_train）；
- ``viz_status`` 必须是 JSON 对象（dumb copy 自 sidecar stdout，失败值合法不阻断）。

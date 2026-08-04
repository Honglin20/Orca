---
description: kd-nas 串行版 distill：单 student KD 蒸馏。顺序：tune_latency（产 accepted_cfg + latency）→ FAIL_latency 分支跳训练 → distill 训练（read→patch run_config.yaml kd_config；唯一真相源）→ eval 取精度。catch 协议：训练/eval rc≠0 → status=FAIL_train，agent 退 0（不 workflow_failed）→ decide 落账 continue。tune_status↔status 映射严格。
tools: [bash, read, write, edit, glob, grep]
---
# distill

## ⚠️ 你的唯一职责

**对单个 student 跑 KD 蒸馏 + eval，emit 结构化 output**（status=SUCCESS/FAIL_latency/FAIL_train）。

**顺序**：
1. ``tune_latency`` → 产 accepted_cfg + latency + met_latency；
2. **FAIL_latency 分支**：``TUNE_STATUS=FAIL_latency`` → **跳训练省 GPU** → status=FAIL_latency，agent 退 0；
3. **distill 训练**（ACCEPTED 才跑）：AST 决定 kd_config（mse+ofd / mse-only）→ **read→patch
   run_config.yaml 的 kd_config 字段（E4：唯一真相源；禁 inline --kd_config）** + inline flag 调引擎；
4. **eval 取精度**：``--mode eval`` 全 flag（student_model_path / build_cfg / student_ckpt inline）→ ``STUDENT_ACCURACY`` / ``MET_ACCURACY``。

**严禁**：
- ❌ 跳 tune_latency 直接 distill（accepted_cfg 是 distill 的 build_cfg 来源，鸡生蛋）；
- ❌ distill 训练传 inline ``--kd_config``（E4：唯一真相源是 run_config.yaml；每轮 read→patch kd_config）；
- ❌ eval 缺 ``--student_ckpt / --accuracy_baseline / --accuracy_baseline_kind``（argparse required）；
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

> FAIL_train 时 met_latency=true（tune 已过），met_accuracy=false, accuracy=-1（与 decide reducer / viz_kd_stage / finalize_kd 的 FAIL_train ledger row 字面一致；旧 v1 train_pool.py 已删，语义已 port 到串行 distill 节点）。

## 输入

- ``student_model_path = {{ gen_student.output.student_model_path }}``
- ``knobs = {{ gen_student.output.knobs }}``
- ``round = {{ gen_student.output.round }}``
- ``teacher_cache = {{ train_teacher.output.teacher_cache }}``
- ``train_pipeline_path = {{ gen_train_script.output.train_pipeline_path }}``
- ``baseline_contract_path = {{ flatten.output.baseline_contract_path }}``
- ``kd_scripts_dir = {{ setup.output.kd_scripts_dir }}``
- ``kd_artifacts_dir = {{ setup.output.kd_artifacts_dir }}``
- ``checkpoints_dir = {{ setup.output.checkpoints_dir }}``
- ``per_run_artifacts_dir = {{ setup.output.per_run_artifacts_dir }}``
- ``target_latency_us = {{ inputs.target_latency_us }}``
- ``accuracy_baseline = {{ inputs.accuracy_baseline }}``
- ``accuracy_baseline_kind = {{ inputs.accuracy_baseline_kind }}``
- ``latency_provider = {{ inputs.latency_provider }}``
- ``device = {{ setup.output.device }}``
- ``seed = {{ inputs.seed }}``
- ``full_epochs = {{ inputs.full_epochs }}``
- ``metrics_template = {{ inputs.metrics_template }}``

---

## step 0 执行：FAIL_build 早退（gen_student.status=FAIL_build → 不调 tune_latency，直接落账）

> SPEC §15 catch 协议：gen_student validate_contract 3-strike FAIL_build 时 student 文件
> import 必坏（py_compile / AST 错），调 tune_latency 会再崩一次（系统失败 → workflow_failed）。
> 此处 early-return：emit FAIL_build JSON，agent 退 0 → decide 落账 continue。

```bash
GEN_STATUS="{{ gen_student.output.status }}"
ROUND="{{ gen_student.output.round }}"
STUDENT="{{ gen_student.output.student_model_path }}"
if [ "$GEN_STATUS" = "FAIL_build" ]; then
  python3 -c '
import json, sys
print(json.dumps({
  "round": int(sys.argv[1]),
  "student_model_path": sys.argv[2],
  "accepted_cfg": {}, "cfg_hash": "fail_build",
  "latency_us": -1, "latency_us_std": 0,
  "accuracy": -1, "accuracy_kind": "",
  "met_latency": False, "met_accuracy": False,
  "ckpt": "", "tune_status": "FAIL_latency",
  "status": "FAIL_build",
  "fail_reason": "gen_student FAIL_build (validate_contract 3 strikes)",
  "viz_status": {"env_status": "skipped",
                 "charts": {"distill_loss": {"pushed": False, "reason": "FAIL_build skip"}}},
}))
' "$ROUND" "$STUDENT"
  exit 0
fi
echo "PARSED step0: GEN_STATUS=$GEN_STATUS → 进 tune_latency"
```

## step 1 执行：tune_latency（产 accepted_cfg + latency + met_latency）

> **整段 step 1-5 必须作为连续一条 bash 调用执行**（catch pattern 跨 step 共享变量；
> 变量声明在 step 1，step 2-5 直接复用；任何 step 之间不应重开 bash 会话——否则 $ACCEPTED_CFG
> / $LATENCY_US / $CKPT_PATH 等会丢）。每个 ``## step N`` 是注释分隔，不是 bash 边界。
>
> ``--artifacts_dir`` required（tune_cache.json + 临时 ONNX 落此）。
> DUMMY_INPUT 从 flatten baseline 读（与 teacher 一致；shape 跟 baseline，非写死）。

```bash
# ─── step 1: tune_latency ─────────────────────────────────────────────────
KD_SCRIPTS_DIR="{{ setup.output.kd_scripts_dir }}"
KD_ARTIFACTS_DIR="{{ setup.output.kd_artifacts_dir }}"
STUDENT="{{ gen_student.output.student_model_path }}"
KNOBS="{{ gen_student.output.knobs }}"
ROUND="{{ gen_student.output.round }}"
TARGET_LATENCY="{{ inputs.target_latency_us }}"
LATENCY_PROVIDER="{{ inputs.latency_provider }}"

BASELINE="{{ flatten.output.baseline_contract_path }}"
[ -f "$BASELINE" ] || { echo "FAIL: baseline_contract 不存在：$BASELINE" >&2; exit 2; }
DUMMY_JSON="$(python3 -c '
import importlib.util, json, sys, os
p=sys.argv[1]; d=os.path.dirname(p)
if d not in sys.path: sys.path.insert(0,d)
spec=importlib.util.spec_from_file_location("_b",p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(json.dumps(m.DUMMY_INPUT))
' "$BASELINE")"

TUNE_OUT="$(python3 "$KD_SCRIPTS_DIR/tune_latency.py" \
  --variant_path "$STUDENT" \
  --build_fn build_model --dummy_input "$DUMMY_JSON" \
  --knobs "$KNOBS" \
  --target_latency_us "$TARGET_LATENCY" \
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
LATENCY_US="$(echo "$TUNE_OUT" | grep '^LATENCY_US_MEDIAN:' | awk '{print $2}')"
LATENCY_STD="$(echo "$TUNE_OUT" | grep '^LATENCY_US_STD:' | awk '{print $2}')"
if [ "$TUNE_STATUS" = "ACCEPTED" ]; then
  ACCEPTED_CFG="$(echo "$TUNE_OUT" | grep '^ACCEPTED_CFG:' | cut -d' ' -f2-)"
elif [ "$TUNE_STATUS" = "FAIL_latency" ]; then
  ACCEPTED_CFG="$(echo "$TUNE_OUT" | grep '^BEST_EFFORT_CFG:' | cut -d' ' -f2-)"
else
  echo "FAIL: TUNE_STATUS=$TUNE_STATUS 非法（合法：ACCEPTED|FAIL_latency）" >&2
  exit 2
fi
CFG_HASH="$(python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:16])" "$ACCEPTED_CFG")"
MET_LATENCY=$(python3 -c "
lat=float('$LATENCY_US'.strip() or 'nan'); target=float('$TARGET_LATENCY'.strip())
print('true' if lat <= target else 'false')
")
echo "PARSED step1: TUNE_STATUS=$TUNE_STATUS ACCEPTED_CFG=$ACCEPTED_CFG LATENCY_US=$LATENCY_US MET_LATENCY=$MET_LATENCY"

# ─── step 2: FAIL_latency 分支 → emit FAIL_latency JSON，agent 退 0（跳训练省 GPU）────────
if [ "$TUNE_STATUS" = "FAIL_latency" ]; then
  python3 -c '
import json, sys
print(json.dumps({
  "round": int(sys.argv[1]), "student_model_path": sys.argv[2],
  "accepted_cfg": json.loads(sys.argv[3]), "cfg_hash": sys.argv[4],
  "latency_us": float(sys.argv[5]), "latency_us_std": float(sys.argv[6]),
  "accuracy": -1, "accuracy_kind": "", "met_latency": False, "met_accuracy": False,
  "ckpt": "", "tune_status": "FAIL_latency", "status": "FAIL_latency",
  "fail_reason": "tune_latency FAIL",
  "viz_status": {"env_status": "skipped",
                 "charts": {"distill_loss": {"pushed": False, "reason": "FAIL_latency skip train"}}},
}))
' "$ROUND" "$STUDENT" "$ACCEPTED_CFG" "$CFG_HASH" "$LATENCY_US" "$LATENCY_STD"
  exit 0
fi

# ─── step 3: distill 训练（ACCEPTED 才跑；E4：kd_config 写 run_config.yaml，唯一真相源）──
# kd_config recipe：mse+ofd + EMA（ofd 仅在 student 暴露 feature_hook_names 时启用）。
# AST 判定 student 是否暴露 feature_hook_names()（不用 grep '^def'——class method 缩进，^def 漏判）。
# 无 hook → KD_CONFIG 退 mse-only（不崩）；有 hook → mse+ofd（compose 守卫 §1.2(1) fail-loud 兜底）。
# ★ E4：distill 每轮 read→patch run_config.yaml 的 kd_config 字段（不传 inline --kd_config；
#   CLI > yaml 否则 yaml 形同虚设——禁用 inline 才能让 yaml 成唯一真相源）。
# catch 协议（SPEC §15）：rc≠0 → status=FAIL_train, tune_status=ACCEPTED，agent 退 0。
TRAIN_PIPELINE="{{ gen_train_script.output.train_pipeline_path }}"
TEACHER_CACHE="{{ train_teacher.output.teacher_cache }}"
CKPTS_DIR="{{ setup.output.checkpoints_dir }}"
CKPT_PATH="${CKPTS_DIR}r${ROUND}_student.pt"
PER_RUN="{{ setup.output.per_run_artifacts_dir }}"
EXP="r${ROUND}_student"   # = variant_id = experiment
RUN_CONFIG="{{ gen_train_script.output.run_config_path }}"
HAS_HOOK=$(python3 -c '
import ast,sys
t=ast.parse(open(sys.argv[1]).read())
print(any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=="feature_hook_names" for n in ast.walk(t)))
' "$STUDENT")
if [ "$HAS_HOOK" = "True" ]; then
  KD_CONFIG='{"kd_losses":["mse","ofd"],"weights":{"mse":1.0,"ofd":0.3},"ema":true}'
else
  KD_CONFIG='{"kd_losses":["mse"],"weights":{"mse":1.0},"ema":true}'
fi
# E4：read→patch run_config.yaml 的 kd_config 字段（保留 epochs/lr/eval_every/patience/...）。
python3 -c '
import json, sys, yaml
path, kd_cfg = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}
cfg["kd_config"] = json.loads(kd_cfg)
with open(path, "w", encoding="utf-8") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
' "$RUN_CONFIG" "$KD_CONFIG"
# ★ E13/M1：redirect stdout → runs/<exp>/train.log（引擎只 print stdout；调用方 redirect）。
mkdir -p "$PER_RUN/runs/$EXP"
ORCA_KD_SCRIPTS_DIR="$KD_SCRIPTS_DIR" python3 "$TRAIN_PIPELINE" \
  --mode distill --config "$RUN_CONFIG" \
  --artifacts_dir "$PER_RUN" --experiment "$EXP" \
  --student_model_path "$STUDENT" \
  --build_fn build_model --build_cfg "$ACCEPTED_CFG" \
  --teacher_cache "$TEACHER_CACHE" \
  --variant_id "$EXP" \
  --epochs "{{ inputs.full_epochs }}" \
  --out_ckpt "$CKPT_PATH" \
  --device "{{ setup.output.device }}" --seed "{{ inputs.seed }}" \
  --project_root "{{ setup.output.project_root }}" \
  --env_anchor "$PER_RUN" \
  > "$PER_RUN/runs/$EXP/train.log" 2>&1
RC=$?
OUT="$(tail -c 4000 "$PER_RUN/runs/$EXP/train.log" 2>/dev/null || true)"
echo "$OUT"
# DISTILL_LOG 兼容（post-hoc 兜底路径；保留旧名以减少 viz 路径 churn）
DISTILL_LOG="$PER_RUN/runs/$EXP/train.log"
if [ $RC -ne 0 ]; then
  FAIL_TAIL="$(echo "$OUT" | tail -c 300)"
  python3 -c '
import json, sys
print(json.dumps({
  "round": int(sys.argv[1]), "student_model_path": sys.argv[2],
  "accepted_cfg": json.loads(sys.argv[3]), "cfg_hash": sys.argv[4],
  "latency_us": float(sys.argv[5]), "latency_us_std": float(sys.argv[6]),
  "accuracy": -1, "accuracy_kind": "", "met_latency": True, "met_accuracy": False,
  "ckpt": "", "tune_status": "ACCEPTED", "status": "FAIL_train",
  "fail_reason": "rc=" + sys.argv[7] + ": " + sys.argv[8],
  "viz_status": {"env_status": "skipped",
                 "charts": {"distill_loss": {"pushed": False, "reason": "FAIL_train skip eval"}}},
}))
' "$ROUND" "$STUDENT" "$ACCEPTED_CFG" "$CFG_HASH" "$LATENCY_US" "$LATENCY_STD" "$RC" "$FAIL_TAIL"
  exit 0
fi
[ -f "$CKPT_PATH" ] || { echo "FAIL: distill rc=0 但 ckpt 未产：$CKPT_PATH" >&2; exit 2; }
echo "PARSED step3: CKPT_PATH=$CKPT_PATH"

# ─── step 4: eval 取精度（inline flag：student_model_path / build_cfg / student_ckpt inline）──
EVAL_OUT="$(python3 "$TRAIN_PIPELINE" \
  --mode eval --artifacts_dir "$PER_RUN" --experiment "$EXP" \
  --student_model_path "$STUDENT" \
  --build_fn build_model --build_cfg "$ACCEPTED_CFG" \
  --student_ckpt "$CKPT_PATH" \
  --accuracy_baseline "{{ inputs.accuracy_baseline }}" \
  --accuracy_baseline_kind "{{ inputs.accuracy_baseline_kind }}" \
  --device "{{ setup.output.device }}" --seed "{{ inputs.seed }}" \
  --project_root "{{ setup.output.project_root }}" \
  --env_anchor "$PER_RUN" 2>&1)"
EVAL_RC=$?
echo "$EVAL_OUT"
if [ $EVAL_RC -ne 0 ]; then
  EVAL_TAIL="$(echo "$EVAL_OUT" | tail -c 300)"
  python3 -c '
import json, sys
print(json.dumps({
  "round": int(sys.argv[1]), "student_model_path": sys.argv[2],
  "accepted_cfg": json.loads(sys.argv[3]), "cfg_hash": sys.argv[4],
  "latency_us": float(sys.argv[5]), "latency_us_std": float(sys.argv[6]),
  "accuracy": -1, "accuracy_kind": "", "met_latency": True, "met_accuracy": False,
  "ckpt": sys.argv[9], "tune_status": "ACCEPTED", "status": "FAIL_train",
  "fail_reason": "eval rc=" + sys.argv[7] + ": " + sys.argv[8],
  "viz_status": {"env_status": "skipped",
                 "charts": {"distill_loss": {"pushed": False, "reason": "eval fail"}}},
}))
' "$ROUND" "$STUDENT" "$ACCEPTED_CFG" "$CFG_HASH" "$LATENCY_US" "$LATENCY_STD" "$EVAL_RC" "$EVAL_TAIL" "$CKPT_PATH"
  exit 0
fi
ACCURACY="$(echo "$EVAL_OUT" | grep '^STUDENT_ACCURACY:' | awk '{print $2}')"
ACCURACY_KIND="$(echo "$EVAL_OUT" | grep '^STUDENT_ACCURACY_KIND:' | awk '{print $2}')"
MET_ACC="$(echo "$EVAL_OUT" | grep '^MET_ACCURACY:' | awk '{print $2}')"
[ -n "$ACCURACY" ] || { echo "FAIL: --mode eval 未 emit STUDENT_ACCURACY（user_eval 移植异常）" >&2; exit 2; }
echo "PARSED step4: ACCURACY=$ACCURACY KIND=$ACCURACY_KIND MET_ACC=$MET_ACC"

# ─── step 5: viz_kd_stage --stage distill_table + metrics_tail（distill loss line）──────
# distill_table：读 ledger 全 student 行（含本轮未入账前的历史）。本轮 student 行尚未 append ledger
# （decide 节点才 append），distill_table 推的是历史；本轮实时点由 metrics_tail loss line 暂代，
# decide 下一轮 distill_table 刷新覆盖。
VIZ_STDOUT_TABLE=$(python3 "$KD_SCRIPTS_DIR/viz_kd_stage.py" \
  --stage distill_table \
  --ledger "{{ setup.output.ledger_path }}" \
  --baseline_latency_us "{{ setup.output.baseline_latency_us }}" \
  --env_anchor "{{ setup.output.per_run_artifacts_dir }}" \
  || true)

VIZ_STDOUT_LOSS=$(python3 "$KD_SCRIPTS_DIR/metrics_tail.py" \
  --template "{{ inputs.metrics_template }}" \
  --source_log "$DISTILL_LOG" \
  --variant_id "r${ROUND}_student" \
  --mode distill \
  --env_anchor "{{ setup.output.per_run_artifacts_dir }}" \
  || true)

VIZ_STATUS=$(python3 -c '
import json, sys
a = json.loads(sys.argv[1]); b = json.loads(sys.argv[2])
ae, be = a.get("viz_env_status", "generic"), b.get("viz_env_status", "generic")
env = ae if ae != "ok" else be
print(json.dumps({"env_status": env, "charts": {**a.get("charts", {}), **b.get("charts", {})}}))
' "$VIZ_STDOUT_TABLE" "$VIZ_STDOUT_LOSS")
echo "VIZ_STATUS_JSON=$VIZ_STATUS"

# ─── step 6: emit SUCCESS JSON（python json.dumps 防 injection）─────────────────────────
python3 -c '
import json, sys
print(json.dumps({
  "round": int(sys.argv[1]), "student_model_path": sys.argv[2],
  "accepted_cfg": json.loads(sys.argv[3]), "cfg_hash": sys.argv[4],
  "latency_us": float(sys.argv[5]), "latency_us_std": float(sys.argv[6]),
  "accuracy": float(sys.argv[7]), "accuracy_kind": sys.argv[8],
  "met_latency": sys.argv[9] == "true", "met_accuracy": sys.argv[10] == "true",
  "ckpt": sys.argv[11], "tune_status": "ACCEPTED", "status": "SUCCESS",
  "fail_reason": "",
  "viz_status": json.loads(sys.argv[12]),
}))
' "$ROUND" "$STUDENT" "$ACCEPTED_CFG" "$CFG_HASH" "$LATENCY_US" "$LATENCY_STD" \
   "$ACCURACY" "$ACCURACY_KIND" "$MET_LATENCY" "$MET_ACC" "$CKPT_PATH" "$VIZ_STATUS"
```

## 产出 JSON（最终消息）

step 0 / step 2 / step 3 (catch) / step 4 (catch) / step 6 任一早退或末尾 emit 即最终 output。
所有 emit 走 ``python3 -c json.dumps``（防 stderr 引号 / 裸换行注入破坏 JSON）。字段：

- ``round`` / ``student_model_path`` / ``accepted_cfg`` (object) / ``cfg_hash`` /
- ``latency_us`` / ``latency_us_std`` / ``accuracy`` / ``accuracy_kind`` /
- ``met_latency`` / ``met_accuracy`` / ``ckpt`` / ``tune_status`` (ACCEPTED|FAIL_latency) /
- ``status`` (SUCCESS|FAIL_latency|FAIL_train|FAIL_build) / ``fail_reason`` / ``viz_status`` (object)。

- ``latency_us`` / ``accuracy`` 必须从 stdout KEY 真值解析（FAIL_* 时 latency 可填 best_effort / accuracy=-1）；
- ``status`` / ``tune_status`` 严格遵守 N21 映射（SUCCESS→ACCEPTED+SUCCESS / FAIL_latency→FAIL_latency+FAIL_latency / FAIL_train→ACCEPTED+FAIL_train）；
- ``viz_status`` 必须是 JSON 对象（dumb copy 自 sidecar stdout，失败值合法不阻断）。

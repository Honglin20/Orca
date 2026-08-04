---
description: kd-nas 串行版 train_teacher（独立节点）：inline flag 调固定引擎 train_pipeline.py --mode teacher（用用户默认 lr/epochs + --artifacts_dir per-run）+ teacher_setup 产 cache + metrics_tail 推训练 metrics。幂等：teacher_cache/meta/ckpt 三者存在 + sha256 匹配 → 跳过训练。teacher 训练崩 → fail loud（teacher_cache 缺整个循环无意义）。
tools: [bash, read, write, edit, glob, grep]
---
# train-teacher

## ⚠️ 你的唯一职责

**产出 = 一个严格匹配下面 output_schema 的 JSON 对象。**

**产出步骤**：
1. 幂等 check（teacher_cache + meta + ckpt 存在 ∧ sha256 匹配 → 跳过训练）；
2. NEED_TRAIN=1 时跑 train_pipeline.py --mode teacher（用 gen_train_script 提取的用户默认 lr/epochs）+ teacher_setup 产 cache（含 teacher eval：复用 train_pipeline --mode eval 测 teacher 精度进 meta）；
3. metrics_tail 推 teacher 训练 metrics（live loss / 自定义模板）；
4. 末尾 viz_kd_stage --stage baseline_seed 不再跑（已在 setup seed）—— 这里无新 stage 推送，
   viz_status 直接复用 metrics_tail stdout（dumb copy）；train_teacher 不调 viz_kd_stage，
   其 viz_status 全部来自 metrics_tail。

**严禁**：
- ❌ 硬编码 epochs=1 / lr=1e-3（必用 ``gen_train_script.output.teacher_default_lr/epochs``）；
- ❌ 缺 ``--out_ckpt`` / ``--env_anchor``（SPEC-REVIEW N2：distill/eval/teacher 三 mode 都 required）；
- ❌ 跳过 metrics_tail（即使 ``inputs.metrics_template`` 空，默认 loss 推送也得跑）；
- ❌ 改 train_pipeline.py / teacher_model_path / 用户训练函数；
- ❌ 编造字段、假装训练成功。

**失败 = fail loud**：
- teacher 训练 rc≠0（teacher_cache 缺，整个 KD 循环无意义）→ **fail loud 阻塞**（workflow_failed，
  SPEC §15 不走 catch 协议——这是 setup/前置错误非业务波动）；
- teacher_setup rc≠0 → fail loud；
- teacher 参数缺（teacher_default_lr/epochs 上游没产出）→ fail loud；
- metrics_tail / viz_kd_stage sidecar 失败 → **不阻断**（sidecar，失败值合法）。

## 输入

- ``teacher_model_path = {{ gen_teacher.output.teacher_model_path }}``（teacher wrapper .py，纯调参派生）
- ``teacher_latency_us = {{ gen_teacher.output.teacher_latency_us }}``（teacher_setup 透传进 meta，不再自测）
- ``train_pipeline_path = {{ gen_train_script.output.train_pipeline_path }}``
- ``teacher_default_lr = {{ gen_train_script.output.teacher_default_lr }}``（用户默认 lr）
- ``teacher_default_epochs = {{ gen_train_script.output.teacher_default_epochs }}``（用户默认 epochs）
- ``baseline_contract_path = {{ flatten.output.baseline_contract_path }}``
- ``kd_scripts_dir = {{ setup.output.kd_scripts_dir }}``
- ``kd_artifacts_dir = {{ setup.output.kd_artifacts_dir }}``
- ``per_run_artifacts_dir = {{ setup.output.per_run_artifacts_dir }}``
- ``device = {{ setup.output.device }}``
- ``seed = {{ inputs.seed }}``
- ``metrics_template = {{ inputs.metrics_template }}``（SPEC §9 JSON，可空）

---

## step 1 执行：幂等 check（teacher_cache + meta + ckpt 存在 ∧ sha256 匹配 → 跳过）

```bash
KD_SCRIPTS_DIR="{{ setup.output.kd_scripts_dir }}"
KD_ARTIFACTS_DIR="{{ setup.output.kd_artifacts_dir }}"
TEACHER_MODEL_PATH="{{ gen_teacher.output.teacher_model_path }}"
TEACHER_CACHE="${KD_ARTIFACTS_DIR}checkpoints/teacher_cache.pt"
TEACHER_META="${KD_ARTIFACTS_DIR}meta/teacher_meta.json"
TEACHER_CKPT="${KD_ARTIFACTS_DIR}checkpoints/teacher_ckpt.pt"
NEED_TRAIN=1
if [ -f "$TEACHER_CACHE" ] && [ -f "$TEACHER_META" ] && [ -f "$TEACHER_CKPT" ]; then
  NEED_TRAIN=$(python3 -c "
import json,hashlib
meta=json.load(open('$TEACHER_META'))
mh=hashlib.sha256(open('$TEACHER_MODEL_PATH','rb').read()).hexdigest()
ch=hashlib.sha256(open('$TEACHER_CKPT','rb').read()).hexdigest()
ok = meta.get('teacher_model_hash')==mh and meta.get('teacher_ckpt_sha256')==ch
print(0 if ok else 1)
")
fi
echo "PARSED step1: NEED_TRAIN=$NEED_TRAIN TEACHER_CACHE=$TEACHER_CACHE TEACHER_META=$TEACHER_META TEACHER_CKPT=$TEACHER_CKPT"
```

## step 2 执行：NEED_TRAIN=1 → 跑 train_pipeline.py --mode teacher + teacher_setup

> **命令 flag 完整**（SPEC §6.6 + SPEC-REVIEW N2）：
>   - ``--out_ckpt`` required（teacher mode 也必传，模板 argparse 校验）；
>   - ``--epochs / --lr`` 用 gen_train_script 提取的用户默认（非硬编码）；
>   - ``--env_anchor`` 激活 _maybe_bootstrap_env（防 live push 静默 no-op）；
>   - stdout 重定向到 teacher_train.log（metrics_tail 读此）。
> teacher_setup 5 required flag 齐全（teacher_model_path/teacher_ckpt/build_fn/dummy_input/output_dir）。

```bash
TRAIN_PIPELINE="{{ gen_train_script.output.train_pipeline_path }}"
TEACHER_DEFAULT_LR="{{ gen_train_script.output.teacher_default_lr }}"
TEACHER_DEFAULT_EPOCHS="{{ gen_train_script.output.teacher_default_epochs }}"
# 必备字段 fail loud（spec-review M1）
python3 -c "
lr='${TEACHER_DEFAULT_LR}'.strip(); ep='${TEACHER_DEFAULT_EPOCHS}'.strip()
assert lr and float(lr) >= 0, f'teacher_default_lr 缺失/无效：{lr!r}（gen_train_script 应已 fail loud）'
assert ep and int(ep) > 0, f'teacher_default_epochs 缺失/无效：{ep!r}（gen_train_script 应已 fail loud）'
"
# DUMMY_INPUT 从 baseline 契约读（与 teacher wrapper 一致；不硬编码 shape）
BASELINE="{{ flatten.output.baseline_contract_path }}"
[ -f "$BASELINE" ] || { echo "FAIL: baseline_contract 不存在：$BASELINE" >&2; exit 2; }
TEACHER_DUMMY="$(python3 -c '
import importlib.util, json, sys, os
p=sys.argv[1]; d=os.path.dirname(p)
if d not in sys.path: sys.path.insert(0,d)
spec=importlib.util.spec_from_file_location("_bd",p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(json.dumps(m.DUMMY_INPUT))
' "$BASELINE")"

if [ "$NEED_TRAIN" = "1" ]; then
  # 2a) 固定引擎 train_pipeline.py --mode teacher（inline flag + --artifacts_dir per-run；
  #     叶子由 gen_train_script 产在 $PER_RUN/user/，引擎加载它们跑循环）
  PER_RUN="{{ setup.output.per_run_artifacts_dir }}"
  TEACHER_EXP="teacher"
  mkdir -p "$PER_RUN/runs/$TEACHER_EXP"
  ORCA_KD_SCRIPTS_DIR="$KD_SCRIPTS_DIR" python3 "$TRAIN_PIPELINE" \
    --mode teacher --artifacts_dir "$PER_RUN" --experiment "$TEACHER_EXP" \
    --model_path "$TEACHER_MODEL_PATH" \
    --build_fn build_model --build_cfg '{}' \
    --epochs "$TEACHER_DEFAULT_EPOCHS" \
    --lr "$TEACHER_DEFAULT_LR" \
    --variant_id teacher \
    --out_ckpt "$TEACHER_CKPT" \
    --device "{{ setup.output.device }}" --seed "{{ inputs.seed }}" \
    --env_anchor "$PER_RUN" \
    > "$PER_RUN/runs/$TEACHER_EXP/train.log" 2>&1
  TP_RC=$?
  if [ $TP_RC -ne 0 ]; then
    echo "FAIL: train_pipeline.py --mode teacher rc=$TP_RC（teacher_cache 缺，KD 循环无意义）。log 尾：" >&2
    tail -c 800 "$PER_RUN/runs/$TEACHER_EXP/train.log" >&2 || true
    exit 2
  fi
  [ -f "$TEACHER_CKPT" ] || { echo "FAIL: teacher 训练未产 ckpt：$TEACHER_CKPT" >&2; exit 2; }
  # 兼容旧 metrics_tail 兜底路径：把 runs/<exp>/train.log 软拷贝到旧 meta/teacher_train.log（post-hoc 读此）
  cp "$PER_RUN/runs/$TEACHER_EXP/train.log" "${KD_ARTIFACTS_DIR}meta/teacher_train.log" 2>/dev/null || true
  # 2b) teacher_setup 产 cache + meta（latency 从 gen_teacher.output 透传，不再自测）
  #     teacher 可视化须由 evaluate 驱动（非 training loss）：复用引擎 --mode eval
  #     跑 teacher ckpt+model → STUDENT_ACCURACY，teacher_setup._parse_accuracy 解析进 meta。
  #     E6：eval_command 是 shell 字符串嵌套；--artifacts_dir 须拼入此字符串字面量。
  #     eval 失败不阻断（teacher_setup 默认 lenient → teacher_accuracy_known=False，图表标 unknown）。
  TEACHER_EVAL_CMD="python3 '$TRAIN_PIPELINE' --mode eval \
    --artifacts_dir '$PER_RUN' --experiment '$TEACHER_EXP' \
    --student_model_path '$TEACHER_MODEL_PATH' \
    --build_fn build_model --build_cfg '{}' \
    --student_ckpt '$TEACHER_CKPT' \
    --accuracy_baseline '{{ inputs.accuracy_baseline }}' \
    --accuracy_baseline_kind '{{ inputs.accuracy_baseline_kind }}' \
    --device '{{ setup.output.device }}' --seed '{{ inputs.seed }}' \
    --project_root '{{ setup.output.project_root }}' \
    --env_anchor '$PER_RUN'"
  python3 "$KD_SCRIPTS_DIR/teacher_setup.py" \
    --teacher_model_path "$TEACHER_MODEL_PATH" \
    --teacher_ckpt "$TEACHER_CKPT" \
    --build_fn build_model --dummy_input "$TEACHER_DUMMY" \
    --output_dir "$KD_ARTIFACTS_DIR" --opset 17 \
    --teacher_latency_us "{{ gen_teacher.output.teacher_latency_us }}" \
    --eval_command "$TEACHER_EVAL_CMD" \
    --project_root "{{ setup.output.project_root }}" \
    --device "{{ setup.output.device }}" \
    > "${KD_ARTIFACTS_DIR}meta/teacher_setup.log" 2>&1
  TS_RC=$?
  if [ $TS_RC -ne 0 ]; then
    echo "FAIL: teacher_setup.py rc=$TS_RC（teacher_cache 产不出，distill 无法跑）" >&2
    exit 2
  fi
fi
[ -f "$TEACHER_CACHE" ] && [ -f "$TEACHER_META" ] || { echo "FAIL: teacher_cache/meta 未生成（NEED_TRAIN=$NEED_TRAIN 但产物缺）" >&2; exit 2; }
# teacher_accuracy / known 从 teacher_meta.json 读（train 与幂等 skip 两条路径都覆盖；
# eval 未跑或失败 → teacher_accuracy_known=false，下游图表标 unknown，不阻断）
read TEACHER_ACC TEACHER_ACC_KNOWN <<< "$(python3 -c "
import json
m=json.load(open('$TEACHER_META'))
print(m.get('teacher_accuracy', 0.0), str(m.get('teacher_accuracy_known', False)).lower())
")"
echo "PARSED step2: TEACHER_CACHE=$TEACHER_CACHE TEACHER_META=$TEACHER_META TEACHER_CKPT=$TEACHER_CKPT TEACHER_ACC=$TEACHER_ACC TEACHER_ACC_KNOWN=$TEACHER_ACC_KNOWN"
```

## step 3 执行：metrics_tail（live loss + 自定义模板 metrics）

> 分工（引擎 + metrics_tail）：
>   - 引擎 ``_make_live_push``（训练循环内）：实时推 per-epoch loss（--env_anchor 激活）；
>   - ``metrics_tail``（post-hoc 兜底）：扫引擎 redirect 出的 ``runs/teacher/train.log``
>     （step 2 已软拷贝到旧 ``meta/teacher_train.log``，兼容此路径）推 loss / 自定义 metrics。
> 两者互补：live push 失败时 metrics_tail 兜底。metrics_template 空 → 走默认 loss。

```bash
TEACHER_LOG="${KD_ARTIFACTS_DIR}meta/teacher_train.log"
VIZ_STDOUT=$(python3 "$KD_SCRIPTS_DIR/metrics_tail.py" \
  --template "{{ inputs.metrics_template }}" \
  --source_log "$TEACHER_LOG" \
  --variant_id teacher \
  --mode teacher \
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

```json
{
  "teacher_cache": "<TEACHER_CACHE abs>",
  "teacher_meta": "<TEACHER_META abs>",
  "teacher_ckpt": "<TEACHER_CKPT abs>",
  "teacher_latency_us": <gen_teacher.output.teacher_latency_us 透传 float>,
  "teacher_accuracy": <TEACHER_ACC float, eval 真测；eval 缺/失败=0.0>,
  "teacher_accuracy_known": <TEACHER_ACC_KNOWN bool, true=eval 命中真值>,
  "viz_status": <VIZ_STATUS_JSON 对象原样嵌入>
}
```

- ``teacher_cache`` / ``teacher_meta`` / ``teacher_ckpt`` 必须是 step2 产出的实际文件路径；
- ``teacher_latency_us`` 透传 ``gen_teacher.output.teacher_latency_us``（非自测）；
- ``teacher_accuracy`` / ``teacher_accuracy_known`` 读自 ``teacher_meta.json``（train_pipeline --mode eval 真测；
  eval 未跑或解析失败 → 0.0 / false，下游总表标 "teacher(unknown acc)"，不阻断）；
- ``viz_status`` 必须是 JSON 对象（dumb copy 自 metrics_tail stdout，失败值合法不阻断）。

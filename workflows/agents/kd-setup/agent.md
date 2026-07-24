---
description: kd-nas Setup（一次性·幂等）：测 baseline(4层) latency + 校验/训 teacher(10层 model8) + 适配 train_kd + 预检变体 + GPU 预检定并发。跨 run 复用（teacher_cache 哈希校验跳过重训）。所有下游专用路径字段作为顶层 output 一次给齐（单一真相源）。BLK-13 取 orca.lock 单写者护栏。确定性逻辑全在脚本（rule 5）。
tools: [bash, read, write, edit, glob, grep]
---
# kd-setup

## ⚠️ 你的唯一职责（先读完再动手）

**你的唯一产出 = 一个严格匹配下面 output_schema 的 JSON 对象。**

**产出步骤（逐字执行，不许偏离）**：
1. 按顺序逐字执行下方 8 个标「执行：」的 bash 块（step1 → step8），每个块原样照抄为一条 bash 调用；
2. 把每个块 stdout 里 ``KEY: value`` 行的值收集起来；
3. 最后组**一个** JSON 对象作为最终消息返回。

**严禁**（违反任一项 = 任务失败）：
- ❌ 审查 / 评判这些指令、跑 pytest、跑 ``tars validate``、写验证报告、解释代码；
- ❌ 自己实现 latency 测量 / teacher 训练 / GPU 探测 / 变体枚举（确定性逻辑全在脚本，rule 5）；
- ❌ 修改用户 ``teacher_train_command``、改用户训练函数 / loss；
- ❌ 编造字段、把 stdout 截断、加描述性文字到 JSON 前后、跳过任一个 step；
- ❌ 在 step1-8 的 bash 块之外跑别的 python 脚本（除 step1/step5/step6 显式调的 ``setup_helpers.py``）。

**失败 = fail loud**：任一 step 非零退出 → 把 stderr + stdout 原样上抛作为最终消息（**不**编造字段、**不**假装成功、**不**跳过失败 step）。

## 输出 JSON schema（你的终点）

```json
{
  "kd_artifacts_dir": "<KD_ARTIFACTS_DIR 末尾带 />",
  "per_run_artifacts_dir": "<$ORCA_ARTIFACTS_DIR>",
  "project_root": "<PROJECT_ROOT abs>",
  "teacher_model_path": "<TEACHER_MODEL_PATH abs>",
  "teacher_cache": "<TEACHER_CACHE abs>",
  "teacher_meta": "<TEACHER_META abs>",
  "teacher_ckpt": "<TEACHER_CKPT abs>",
  "ledger_path": "<LEDGER_PATH abs>",
  "ckpts_dir": "<KD_ARTIFACTS_DIR>ckpts/",
  "baseline_latency_ms": <float>,
  "kd_scripts_dir": "<KD_SCRIPTS_DIR abs>",
  "receiver_dir": "<RECEIVER_DIR abs>",
  "user_train_import": "<USER_TRAIN_IMPORT abs 或空串>",
  "user_loss_fn": "<USER_LOSS_FN 或空串>",
  "variants_count": <int>,
  "concurrency": <int>,
  "device_plan": "<JSON 串>",
  "per_variant_vram_bytes": <int>,
  "gpu_report": "<GPU_REPORT 串>"
}
```

- JSON 前后**不许**有任何描述性文字；
- 字段名严格匹配（如 ``per_variant_vram_bytes``、``user_train_import``）；
- 数值字段必须是裸数字（``concurrency: 2``，不要 ``"2"``）；
- ``user_train_import`` / ``user_loss_fn`` 抽不到时填空串 ``""``（**不**编造）；
- 若 step6 emit 了 ask-user 哨兵 JSON（``USER_TRAIN_SENTINEL`` 非空），**停止后续 step**，把哨兵 JSON 原样作为最终消息返回（不组上面的 output JSON）。

## 输入

- ``teacher_train_command = {{ inputs.teacher_train_command }}``（10 层 teacher 从头训，无 KD；原样 shell 执行）
- ``baseline_model_path = {{ inputs.baseline_model_path }}``（原始 4 层 baseline，契约文件：``build_model`` + ``DUMMY_INPUT.shape``）
- ``latency_provider = {{ inputs.latency_provider }}``（用户真硬件 latency 脚本 ``path::func``，必填）
- ``device = {{ inputs.device }}`` / ``seed = {{ inputs.seed }}`` / ``kd_artifacts_dir = {{ inputs.kd_artifacts_dir }}``
- ``target_latency_ms = {{ inputs.target_latency_ms }}``（step7 pick_variant 用）
- 引擎已注入 ``$ORCA_ARTIFACTS_DIR``（per-run 产物目录）+ ``$ORCA_KB_DIR``（KB 根目录）。

---

## step 1 执行：解析路径 + 单写者锁（BLK-13）

```bash
KD_SCRIPTS_DIR="$(dirname "$(find workflows/agents/_kd_scripts -name teacher_model.py -print -quit)")"
KD_SCRIPTS_DIR="$(python3 -c "import os,sys;print(os.path.abspath(sys.argv[1]))" "$KD_SCRIPTS_DIR")"
PER_RUN_ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-}"
[ -z "$PER_RUN_ARTIFACTS_DIR" ] && { echo "FAIL: \$ORCA_ARTIFACTS_DIR 未注入（非 orca run 上下文）" >&2; exit 2; }
INPUT_ART="{{ inputs.kd_artifacts_dir }}"
if [ -n "$INPUT_ART" ]; then KD_ARTIFACTS_DIR="$(python3 -c "import os,sys;print(os.path.abspath(sys.argv[1])+'/')" "$INPUT_ART")"; \
else KD_ARTIFACTS_DIR="$(python3 -c "import os;print(os.path.abspath('kd-nas-artifacts')+'/')")"; fi
mkdir -p "$KD_ARTIFACTS_DIR"ckpts
BASELINE="{{ inputs.baseline_model_path }}"
PROJECT_ROOT="$(python3 -c "
import os,sys
p=os.path.dirname(os.path.abspath(sys.argv[1]))
while p and p!=os.path.dirname(p) and not any(os.path.exists(os.path.join(p,m)) for m in ('.git','pyproject.toml')):
    p=os.path.dirname(p)
print(p)
" "$BASELINE")"
# KB receiver 绝对路径（setup 探测一次，下游 train_pool 经 output 取——不依赖 ORCA_KB_DIR env，BUG-3）
RECEIVER_DIR="$(python3 -c "import os;print(os.path.abspath(os.path.join(os.environ.get('ORCA_KB_DIR',''),'families','receiver'))+'/' if os.environ.get('ORCA_KB_DIR') else '')")"
[ -z "$RECEIVER_DIR" ] || [ ! -d "$RECEIVER_DIR" ] && { echo "FAIL: ORCA_KB_DIR 未注入或 ${RECEIVER_DIR:-<empty>} 不存在" >&2; exit 2; }
# ledger 跨 run 复用铁律：仅首次创建，**绝不截断已有行**（否则历史蒸馏全丢 → 重复训练）
LEDGER_PATH="${KD_ARTIFACTS_DIR}ledger.jsonl"
[ -f "$LEDGER_PATH" ] || : > "$LEDGER_PATH"
export KD_SCRIPTS_DIR KD_ARTIFACTS_DIR PER_RUN_ARTIFACTS_DIR LEDGER_PATH BASELINE PROJECT_ROOT RECEIVER_DIR
python3 -c "
import sys; sys.path.insert(0,'$KD_SCRIPTS_DIR')
from kd_common import acquire_run_lock
print('LOCK:', acquire_run_lock('$KD_ARTIFACTS_DIR', __import__('os').environ.get('ORCA_RUN_ID','')))
"
echo "PARSED step1: KD_SCRIPTS_DIR=$KD_SCRIPTS_DIR KD_ARTIFACTS_DIR=$KD_ARTIFACTS_DIR PROJECT_ROOT=$PROJECT_ROOT LEDGER_PATH=$LEDGER_PATH RECEIVER_DIR=$RECEIVER_DIR"
```

## step 2 执行：校验 baseline 契约 + 测 baseline latency（参考线，HI-13 median+std）

```bash
BASELINE_DUMMY="$(python3 -c "
import importlib.util, json, os, sys
p=os.path.abspath('$BASELINE'); d=os.path.dirname(p)
if d not in sys.path: sys.path.insert(0,d)
spec=importlib.util.spec_from_file_location('_b',p)
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert callable(getattr(m,'build_model',None)), 'baseline_model_path 缺 build_model（契约）'
assert isinstance(getattr(m,'DUMMY_INPUT',None),dict) and m.DUMMY_INPUT.get('shape'), 'baseline 缺 DUMMY_INPUT.shape'
print(json.dumps(m.DUMMY_INPUT))
")"
TUNE_OUT="$(python3 "$KD_SCRIPTS_DIR/tune_latency.py" \
  --variant_path "$BASELINE" --build_fn build_model \
  --dummy_input "$BASELINE_DUMMY" --knobs '{}' \
  --target_latency_ms 0 --latency_provider "{{ inputs.latency_provider }}" \
  --artifacts_dir "$KD_ARTIFACTS_DIR" --device "{{ inputs.device }}" --seed "{{ inputs.seed }}" \
  --max_measurements 1 --measure_repeats 3 2>&1 || true)"
BASELINE_LATENCY_MS="$(echo "$TUNE_OUT" | grep '^LATENCY_MS_MEDIAN:' | awk '{print $2}')"
[ -n "$BASELINE_LATENCY_MS" ] || { echo "FAIL: baseline latency 未测到"; echo "$TUNE_OUT"; exit 2; }
echo "PARSED step2: BASELINE_LATENCY_MS=$BASELINE_LATENCY_MS"
```

## step 3 执行：校验 teacher_model.py（repo 写死，10 层 t1/t2 交替）

```bash
TEACHER_MODEL_PATH="$KD_SCRIPTS_DIR/teacher_model.py"
python3 -c "import ast; ast.parse(open('$TEACHER_MODEL_PATH').read())"
python3 -c "
import sys; sys.path.insert(0,'$KD_SCRIPTS_DIR')
from teacher_model import build_model
import torch
t=build_model(); blocks=list(t.main)
assert len(blocks)==10, f'块数!=10: {len(blocks)}'
mt=[b.m_a.m_type for b in blocks]; assert mt==['t1','t2']*5, f'非 t1/t2 交替: {mt}'
o=t(torch.randn(1,4,48,64,1)); assert o.shape==torch.Size([1,4,48,64,1]), o.shape
print('TEACHER_OK')
"
echo "PARSED step3: TEACHER_MODEL_PATH=$TEACHER_MODEL_PATH"
```

## step 4 执行：幂等护栏（teacher_cache 存在 + 哈希匹配 → 跳过 teacher 训练，HI-3）

```bash
TEACHER_CACHE="${KD_ARTIFACTS_DIR}teacher_cache.pt"
TEACHER_META="${KD_ARTIFACTS_DIR}teacher_meta.json"
TEACHER_CKPT="${KD_ARTIFACTS_DIR}teacher_ckpt.pt"
NEED_TRAIN=1
if [ -f "$TEACHER_CACHE" ] && [ -f "$TEACHER_META" ] && [ -f "$TEACHER_CKPT" ]; then
  NEED_TRAIN=$(python3 -c "
import json,hashlib,os
meta=json.load(open('$TEACHER_META'))
mh=hashlib.sha256(open('$TEACHER_MODEL_PATH','rb').read()).hexdigest()
ch=hashlib.sha256(open('$TEACHER_CKPT','rb').read()).hexdigest()
ok = meta.get('teacher_model_hash')==mh and meta.get('teacher_ckpt_sha256')==ch
print(0 if ok else 1)
")
fi
echo "PARSED step4: TEACHER_CACHE=$TEACHER_CACHE TEACHER_META=$TEACHER_META TEACHER_CKPT=$TEACHER_CKPT NEED_TRAIN=$NEED_TRAIN"
```

## step 5 执行：若 NEED_TRAIN=1，从头训 teacher + teacher_setup 产 cache

确定性 teacher_ckpt 解析下沉到 ``setup_helpers.py find-teacher-ckpt``（R4：原实现把 ckpt 路径留给 LLM grep / 字符串拼，违反 rule 5）。

```bash
if [ "$NEED_TRAIN" = "1" ]; then
  # 5a) 原样跑 teacher_train_command（不改用户脚本）；wait 阻塞
  ( cd "$PROJECT_ROOT" && {{ inputs.teacher_train_command }} )
  # 5b) 确定性解析 teacher_train_command 产物 → 拷到 $TEACHER_CKPT（setup_helpers.find-teacher-ckpt）
  python3 "$KD_SCRIPTS_DIR/setup_helpers.py" find-teacher-ckpt \
    --project_root "$PROJECT_ROOT" \
    --train_command "{{ inputs.teacher_train_command }}" \
    --target "$TEACHER_CKPT"
  # 5c) teacher_setup 产 cache + meta + ONNX + latency
  python3 "$KD_SCRIPTS_DIR/teacher_setup.py" \
    --teacher_model_path "$TEACHER_MODEL_PATH" --teacher_ckpt "$TEACHER_CKPT" \
    --build_fn build_model --dummy_input '{"shape":[1,4,48,64,1],"dtype":"float32"}' \
    --output_dir "$KD_ARTIFACTS_DIR" --opset 17 \
    --latency_provider "{{ inputs.latency_provider }}" --device "{{ inputs.device }}" --seed "{{ inputs.seed }}"
fi
[ -f "$TEACHER_CACHE" ] && [ -f "$TEACHER_META" ] || { echo "FAIL: teacher_cache/meta 未生成"; exit 2; }
echo "PARSED step5: TEACHER_CACHE=$TEACHER_CACHE TEACHER_META=$TEACHER_META TEACHER_CKPT=$TEACHER_CKPT"
```

## step 6 执行：适配 train_kd（确定性 grep user train.py loss/dataloader，BLK-7）

确定性 AST 解析下沉到 ``setup_helpers.py grep-user-train``（R4：原实现留给 LLM grep / ask-user 决策，违反 rule 5）。抽不到 → ``USER_TRAIN_SENTINEL`` 非空 → **停止后续 step**，把哨兵 JSON 原样作为最终消息返回。

```bash
GREP_OUT="$(python3 "$KD_SCRIPTS_DIR/setup_helpers.py" grep-user-train \
  --project_root "$PROJECT_ROOT" \
  --train_command "{{ inputs.teacher_train_command }}" \
  --baseline_model_path "$BASELINE")"
USER_TRAIN_IMPORT="$(echo "$GREP_OUT" | grep '^USER_TRAIN_IMPORT:' | cut -d' ' -f2-)"
USER_LOSS_FN="$(echo "$GREP_OUT" | grep '^USER_LOSS_FN:' | cut -d' ' -f2-)"
USER_TRAIN_SENTINEL="$(echo "$GREP_OUT" | grep '^USER_TRAIN_SENTINEL:' | cut -d' ' -f2-)"
# 持久化到 user_train.json（distill 读，CLI --user_train_import/--user_loss_fn 注入 train_kd）
python3 -c "
import json,os
out={'user_train_import': os.environ.get('KD_USER_TRAIN_IMPORT','$USER_TRAIN_IMPORT'),
     'user_loss_fn':       os.environ.get('KD_USER_LOSS_FN','$USER_LOSS_FN')}
json.dump(out, open('${KD_ARTIFACTS_DIR}user_train.json','w'), indent=2)
print('USER_TRAIN:', json.dumps(out))
"
echo "PARSED step6: USER_TRAIN_IMPORT=$USER_TRAIN_IMPORT USER_LOSS_FN=$USER_LOSS_FN SENTINEL=$USER_TRAIN_SENTINEL"
# 抽不到 loss fn → 哨兵非空 → 立即停止后续 step，把哨兵 JSON 作为最终消息返回（不编造字段）
if [ -n "$USER_TRAIN_SENTINEL" ]; then
  echo "ASK_USER_SENTINEL: $USER_TRAIN_SENTINEL"
fi
```

> 若 ``ASK_USER_SENTINEL:`` 行出现（``USER_TRAIN_SENTINEL`` 非空），**立即停止**，把那个 JSON 作为最终消息返回——主 session 会问用户后用 SendMessage 把答案追加给你，你**继续**从 step 6 重跑（用户答案填到 ``$USER_TRAIN_IMPORT`` / ``$USER_LOSS_FN``），不要重做 step1-5。

## step 7 执行：预检 KB 变体 ≥1（BLK-14）

```bash
python3 "$KD_SCRIPTS_DIR/pick_variant.py" --receiver_dir "$RECEIVER_DIR" \
  --ledger "$LEDGER_PATH" --target_latency_ms "{{ inputs.target_latency_ms }}" \
  --latency_provider "{{ inputs.latency_provider }}" --out "${KD_ARTIFACTS_DIR}_first_variant.json"
# exit 0/0(ALL_DONE)/3(NO_VARIANTS)。NO_VARIANTS(exit 3) → fail loud（KB 无变体）。
VARIANTS_COUNT=$(ls "$RECEIVER_DIR"/*.py 2>/dev/null | grep -v '/_' | wc -l)
[ "$VARIANTS_COUNT" -gt 0 ] || { echo "FAIL: KB receiver_dir=$RECEIVER_DIR 无变体（.py）"; exit 2; }
echo "PARSED step7: VARIANTS_COUNT=$VARIANTS_COUNT RECEIVER_DIR=$RECEIVER_DIR"
```

## step 8 执行：GPU 预检（定并发数 + 多卡 device_plan；setup 是并发数唯一权威）

```bash
GPU_OUT="$(python3 "$KD_SCRIPTS_DIR/gpu_probe.py" \
  --teacher_cache "$TEACHER_CACHE" \
  --representative_variant "$BASELINE" \
  --variants_count "$VARIANTS_COUNT" --device "{{ inputs.device }}" \
  --safety 0.8 --max_concurrency 8 --seed "{{ inputs.seed }}" 2>&1)"
GPU_RC=$?
[ $GPU_RC -ne 0 ] && { echo "$GPU_OUT" >&2; exit 2; }
CONCURRENCY="$(echo "$GPU_OUT" | grep '^CONCURRENCY:' | awk '{print $2}')"
DEVICE_PLAN="$(echo "$GPU_OUT" | grep '^DEVICE_PLAN:' | cut -d' ' -f2-)"
PER_VARIANT_VRAM_BYTES="$(echo "$GPU_OUT" | grep '^PER_VARIANT_VRAM_BYTES:' | awk '{print $2}')"
GPU_REPORT="$(echo "$GPU_OUT" | grep '^GPU_REPORT:' | cut -d' ' -f2-)"
# 兜底：grep 取不到（不应发生，gpu_probe 必 emit）→ 单卡串行
[ -z "$CONCURRENCY" ] && { CONCURRENCY=1; DEVICE_PLAN='[""]'; PER_VARIANT_VRAM_BYTES=0; GPU_REPORT='WARN grep miss -> serial'; }
echo "PARSED step8: CONCURRENCY=$CONCURRENCY DEVICE_PLAN=$DEVICE_PLAN PER_VARIANT_VRAM_BYTES=$PER_VARIANT_VRAM_BYTES GPU_REPORT=$GPU_REPORT"
```

## 产出 JSON（最终消息）

把上面 8 个 ``PARSED stepN:`` 行里的值原样填进下面模板，**只**返回这个 JSON：

```json
{
  "kd_artifacts_dir": "<KD_ARTIFACTS_DIR>",
  "per_run_artifacts_dir": "<PER_RUN_ARTIFACTS_DIR>",
  "project_root": "<PROJECT_ROOT>",
  "teacher_model_path": "<TEACHER_MODEL_PATH>",
  "teacher_cache": "<TEACHER_CACHE>",
  "teacher_meta": "<TEACHER_META>",
  "teacher_ckpt": "<TEACHER_CKPT>",
  "ledger_path": "<LEDGER_PATH>",
  "ckpts_dir": "<KD_ARTIFACTS_DIR>ckpts/",
  "baseline_latency_ms": <BASELINE_LATENCY_MS float>,
  "kd_scripts_dir": "<KD_SCRIPTS_DIR>",
  "receiver_dir": "<RECEIVER_DIR>",
  "user_train_import": "<USER_TRAIN_IMPORT 或空串>",
  "user_loss_fn": "<USER_LOSS_FN 或空串>",
  "variants_count": <VARIANTS_COUNT int>,
  "concurrency": <CONCURRENCY int>,
  "device_plan": "<DEVICE_PLAN JSON 串>",
  "per_variant_vram_bytes": <PER_VARIANT_VRAM_BYTES int>,
  "gpu_report": "<GPU_REPORT 串>"
}
```

> step6 若返回 ask-user 哨兵，则**不**返回此 output JSON；返回哨兵 JSON。收到用户答案后从 step6 续跑。

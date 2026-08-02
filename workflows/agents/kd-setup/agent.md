---
description: kd-nas Setup（一次性·幂等）：跑 teacher 训练（train_pipeline.py --mode teacher，teacher-gen 产出的 wrapper）+ teacher_setup 产 cache（latency 透传 teacher_gen.output）+ 预检变体 + GPU 预检定并发。跨 run 复用（teacher_cache 哈希校验跳过重训）。所有下游专用路径字段作为顶层 output 一次给齐（单一真相源）。取 orca.lock 单写者护栏。确定性逻辑全在脚本（rule 5）。
tools: [bash, read, write, edit, glob, grep]
---
# kd-setup

## ⚠️ 你的唯一职责（先读完再动手）

**你的唯一产出 = 一个严格匹配下面 output_schema 的 JSON 对象。**

**产出步骤（逐字执行，不许偏离）**：
1. 按顺序逐字执行下方 7 个标「执行：」的 bash 块（step1 → step7），每个块原样照抄为一条 bash 调用；
2. 把每个块 stdout 里 ``KEY: value`` 行的值收集起来；
3. 最后组**一个** JSON 对象作为最终消息返回。

**严禁**（违反任一项 = 任务失败）：
- ❌ 审查 / 评判这些指令、跑 pytest、跑 ``tars validate``、写验证报告、解释代码；
- ❌ 自己实现 latency 测量 / teacher 训练 / GPU 探测 / 变体枚举（确定性逻辑全在脚本，rule 5）；
- ❌ 修改 ``train_pipeline.py``（train-script-gen 产出的生成物）、改用户训练函数 / loss；
- ❌ 编造字段、把 stdout 截断、加描述性文字到 JSON 前后、跳过任一个 step；
- ❌ 在 step1-7 的 bash 块之外跑别的 python 脚本。

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
  "variants_count": <int>,
  "concurrency": <int>,
  "device_plan": "<JSON 串>",
  "per_variant_vram_bytes": <int>,
  "gpu_report": "<GPU_REPORT 串>"
}
```

- JSON 前后**不许**有任何描述性文字；
- 字段名严格匹配（如 ``per_variant_vram_bytes``）；
- 数值字段必须是裸数字（``concurrency: 2``，不要 ``"2"``）。

## 输入

- ``teacher_model_path = {{ teacher_gen.output.teacher_model_path }}``（teacher-gen 派生的 teacher wrapper .py，纯调参放大 baseline；经 validate_contract.py + validate_teacher.py 双重 PASS。setup 透传作 teacher_model_path output）
- ``teacher_latency_ms = {{ teacher_gen.output.teacher_latency_ms }}``（teacher-gen __main__ 实测的 latency；teacher_setup 透传进 teacher_cache/meta，不再自测）
- ``train_pipeline_path = {{ train_script_gen.output.train_pipeline_path }}``（train-script-gen 生成的统一 train_pipeline.py；step5 调 ``--mode teacher`` 训 teacher）
- ``baseline_contract_path = {{ flatten.output.baseline_contract_path }}``（flatten 节点产出的 KD 变体契约 .py，已保证 ``build_model`` + ``DUMMY_INPUT.shape`` + ``KNOBS``，经 ``validate_contract.py`` PASS）
- ``project_root = {{ flatten.output.project_root }}``（flatten 推断的 project_root；step1 fallback 用）
- ``latency_provider = {{ inputs.latency_provider }}``（用户真硬件 latency 脚本 ``path::func``，必填）
- ``device = {{ inputs.device }}``
- ``target_latency_ms = {{ inputs.target_latency_ms }}``（step7 pick_variant 用）
- 引擎已注入 ``$ORCA_ARTIFACTS_DIR``（per-run 产物目录）+ ``$ORCA_KB_DIR``（KB 根目录）。
- **已下沉**：``seed``（默认 0）/ ``kd_artifacts_dir``（默认 ``<repo>/kd-nas-artifacts/``）从 inputs 移除——下面 step 用常量默认；如需 override 改 agent.md 常量。
- **user train.py 适配已下沉到 train-script-gen**：setup 不再 grep-user-train（旧 step6 删除）；loss/dataloader/optimizer 已由 train-script-gen 生成 train_pipeline.py 时搬入。

---

## step 1 执行：解析路径 + 单写者锁

```bash
KD_SCRIPTS_DIR="$(dirname "$(find workflows/agents/_kd_scripts -name kd_common.py -print -quit)")"
KD_SCRIPTS_DIR="$(python3 -c "import os,sys;print(os.path.abspath(sys.argv[1]))" "$KD_SCRIPTS_DIR")"
PER_RUN_ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-}"
[ -z "$PER_RUN_ARTIFACTS_DIR" ] && { echo "FAIL: \$ORCA_ARTIFACTS_DIR 未注入（非 orca run 上下文）" >&2; exit 2; }
# kd_artifacts_dir 已下沉：默认 <repo>/kd-nas-artifacts/。如需 override 改下行常量。
KD_ARTIFACTS_DIR="$(python3 -c "import os;print(os.path.abspath('kd-nas-artifacts')+'/')")"
mkdir -p "$KD_ARTIFACTS_DIR"ckpts
# baseline 来源从 inputs.baseline_model_path 改为 flatten.output.baseline_contract_path
# （flatten 已保证 build_model + DUMMY_INPUT + KNOBS 契约，validate_contract.py PASS）。
BASELINE="{{ flatten.output.baseline_contract_path }}"
[ -f "$BASELINE" ] || { echo "FAIL: flatten 产物不存在：$BASELINE（flatten 节点是否 PASS？）" >&2; exit 2; }
# project_root 优先取 flatten 推断；低置信时 flatten 会附 " (low-confidence: ...)" 后缀，
# 用 python 剥掉后缀取真实路径前缀（bash %% 对含 "(" 的 pattern 行为依赖 extglob，python 更稳）。
FLATTEN_PROJECT_ROOT="{{ flatten.output.project_root }}"
PROJECT_ROOT="$(python3 -c "
import os,sys
p=sys.argv[1].split(' (low-confidence')[0].strip()
print(os.path.abspath(p) if p else '')
" "$FLATTEN_PROJECT_ROOT")"
# flatten 推断失败 / 空 → fallback 从 BASELINE 向上走找 .git/pyproject.toml
[ -n "$PROJECT_ROOT" ] && [ "$PROJECT_ROOT" != "/" ] || PROJECT_ROOT="$(python3 -c "
import os,sys
p=os.path.dirname(os.path.abspath(sys.argv[1]))
while p and p!=os.path.dirname(p) and not any(os.path.exists(os.path.join(p,m)) for m in ('.git','pyproject.toml')):
    p=os.path.dirname(p)
print(p)
" "$BASELINE")"
# KB receiver 绝对路径（setup 探测一次，下游 train_pool 经 output 取——不依赖 ORCA_KB_DIR env）
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

## step 2 执行：校验 baseline 契约 + 读 flatten 的 baseline_latency_ms（参考线）

> flatten 已保证 ``build_model`` + ``DUMMY_INPUT`` 契约（``validate_contract.py`` PASS），且 flatten
> 的 ``__main__`` 已测出 ``baseline_latency_ms``（统一「跑 ``__main__`` = 正确性 + latency」契约）。
> 下面这段 contract assert 保留作 **fail-loud 复核**——更安全，不删（若 flatten 产出与 setup 读取不一致，
> 立即炸而非静默）。baseline latency **不再重测**，直接读 ``flatten.output.baseline_latency_ms``
> （避免重复测量；latency_provider 已在 flatten 阶段用过）。

```bash
BASELINE_DUMMY="$(python3 -c "
import importlib.util, json, os, sys
p=os.path.abspath('$BASELINE'); d=os.path.dirname(p)
if d not in sys.path: sys.path.insert(0,d)
spec=importlib.util.spec_from_file_location('_b',p)
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert callable(getattr(m,'build_model',None)), 'baseline 缺 build_model（契约）'
assert isinstance(getattr(m,'DUMMY_INPUT',None),dict) and m.DUMMY_INPUT.get('shape'), 'baseline 缺 DUMMY_INPUT.shape'
print(json.dumps(m.DUMMY_INPUT))
")"
# baseline_latency_ms 来源下沉到 flatten.output（flatten __main__ 已用 latency_provider 测过；
# setup 不再调 tune_latency 重测——避免重复测量 + 让 latency_provider 在 flatten 阶段就生效）。
BASELINE_LATENCY_MS="{{ flatten.output.baseline_latency_ms }}"
python3 -c "
v='${BASELINE_LATENCY_MS}'.strip()
assert v, 'baseline_latency_ms 为空（flatten 是否产出？）'
float(v)  # 必须是合法 float（fail loud）
print('BASELINE_LATENCY_OK:', v)
"
echo "PARSED step2: BASELINE_LATENCY_MS=$BASELINE_LATENCY_MS (source: flatten.output)"
```

## step 3 执行：校验 teacher-gen 产出的 teacher wrapper（透传 teacher_gen.output）

> teacher_model_path 来源从「repo 写死 ``_kd_scripts/teacher_model.py``」改为「``teacher_gen.output.teacher_model_path``」
> （teacher-gen 纯调参派生的 wrapper，已过 validate_contract + validate_teacher 双重硬校验）。
> 这里只做 fail-loud 复核：文件存在 + 可 import + 有 build_model + DUMMY_INPUT。

```bash
TEACHER_MODEL_PATH="{{ teacher_gen.output.teacher_model_path }}"
[ -f "$TEACHER_MODEL_PATH" ] || { echo "FAIL: teacher-gen 产物不存在：$TEACHER_MODEL_PATH（teacher-gen 节点是否 PASS？）" >&2; exit 2; }
python3 -c "import ast; ast.parse(open('$TEACHER_MODEL_PATH').read())"
python3 -c "
import importlib.util, sys, os
p='$TEACHER_MODEL_PATH'; d=os.path.dirname(p)
if d not in sys.path: sys.path.insert(0,d)
spec=importlib.util.spec_from_file_location('_tchk',p)
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert callable(getattr(m,'build_model',None)), 'teacher wrapper 缺 build_model'
assert isinstance(getattr(m,'DUMMY_INPUT',None),dict) and m.DUMMY_INPUT.get('shape'), 'teacher wrapper 缺 DUMMY_INPUT.shape'
print('TEACHER_OK')
"
echo "PARSED step3: TEACHER_MODEL_PATH=$TEACHER_MODEL_PATH (source: teacher_gen.output)"
```

## step 4 执行：幂等护栏（teacher_cache 存在 + 哈希匹配 → 跳过 teacher 训练）

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

## step 5 执行：若 NEED_TRAIN=1，跑 train_pipeline.py --mode teacher + teacher_setup 产 cache

> teacher 训练从「原样跑 ``teacher_train_command`` + ``setup_helpers find-teacher-ckpt`` 解析产物」改为
> 「调 ``train_script_gen.output.train_pipeline_path`` 的 ``--mode teacher``，固定 ``--out_ckpt`` 路径」。
> train_pipeline 直接存固定 out_ckpt 路径，setup 知道路径 → 不再需要 find-teacher-ckpt grep。
> teacher_setup latency 从 ``teacher_gen.output.teacher_latency_ms`` 透传（不再 teacher_setup 自己测——
> latency 已在 teacher-gen ``__main__`` 测掉，避免重复测量）。

```bash
TRAIN_PIPELINE_PATH="{{ train_script_gen.output.train_pipeline_path }}"
[ -f "$TRAIN_PIPELINE_PATH" ] || { echo "FAIL: train-script-gen 产物不存在：$TRAIN_PIPELINE_PATH（train-script-gen 节点是否 PASS？）" >&2; exit 2; }
# DUMMY_INPUT 从 baseline 契约读（与 teacher wrapper 一致；不硬编码 shape）
TEACHER_DUMMY="$(python3 -c "
import importlib.util, json, sys, os
p='$BASELINE'; d=os.path.dirname(p)
if d not in sys.path: sys.path.insert(0,d)
spec=importlib.util.spec_from_file_location('_bd',p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(json.dumps(m.DUMMY_INPUT))
")"
if [ "$NEED_TRAIN" = "1" ]; then
  # 5a) 跑 train_pipeline.py --mode teacher（train-script-gen 产物；固定 --out_ckpt）
  ORCA_KD_SCRIPTS_DIR="$KD_SCRIPTS_DIR" python3 "$TRAIN_PIPELINE_PATH" \
    --mode teacher --model_path "$TEACHER_MODEL_PATH" \
    --build_fn build_model --build_cfg '{}' \
    --epochs 1 --batch_size 2 --device "{{ inputs.device }}" \
    --variant_id teacher --out_ckpt "$TEACHER_CKPT"
  TP_RC=$?
  [ $TP_RC -eq 0 ] || { echo "FAIL: train_pipeline.py --mode teacher rc=$TP_RC（读 stderr 修脚本 / 检查 teacher_model_path / user_train 适配）" >&2; exit 2; }
  [ -f "$TEACHER_CKPT" ] || { echo "FAIL: train_pipeline.py --mode teacher 未产出 ckpt：$TEACHER_CKPT" >&2; exit 2; }
  # 5b) teacher_setup 产 cache + meta + ONNX（latency 从 teacher_gen.output 透传，不再自测）
  python3 "$KD_SCRIPTS_DIR/teacher_setup.py" \
    --teacher_model_path "$TEACHER_MODEL_PATH" --teacher_ckpt "$TEACHER_CKPT" \
    --build_fn build_model --dummy_input "$TEACHER_DUMMY" \
    --output_dir "$KD_ARTIFACTS_DIR" --opset 17 \
    --teacher_latency_ms "{{ teacher_gen.output.teacher_latency_ms }}" \
    --device "{{ inputs.device }}"
fi
[ -f "$TEACHER_CACHE" ] && [ -f "$TEACHER_META" ] || { echo "FAIL: teacher_cache/meta 未生成"; exit 2; }
echo "PARSED step5: TEACHER_CACHE=$TEACHER_CACHE TEACHER_META=$TEACHER_META TEACHER_CKPT=$TEACHER_CKPT"
```

## step 6 执行：预检 KB 变体 ≥1

```bash
python3 "$KD_SCRIPTS_DIR/pick_variant.py" --receiver_dir "$RECEIVER_DIR" \
  --ledger "$LEDGER_PATH" --target_latency_ms "{{ inputs.target_latency_ms }}" \
  --latency_provider "{{ inputs.latency_provider }}" --out "${KD_ARTIFACTS_DIR}_first_variant.json"
# exit 0/0(ALL_DONE)/3(NO_VARIANTS)。NO_VARIANTS(exit 3) → fail loud（KB 无变体）。
VARIANTS_COUNT=$(ls "$RECEIVER_DIR"/*.py 2>/dev/null | grep -v '/_' | wc -l)
[ "$VARIANTS_COUNT" -gt 0 ] || { echo "FAIL: KB receiver_dir=$RECEIVER_DIR 无变体（.py）"; exit 2; }
echo "PARSED step6: VARIANTS_COUNT=$VARIANTS_COUNT RECEIVER_DIR=$RECEIVER_DIR"
```

## step 7 执行：GPU 预检（定并发数 + 多卡 device_plan；setup 是并发数唯一权威）

```bash
GPU_OUT="$(python3 "$KD_SCRIPTS_DIR/gpu_probe.py" \
  --teacher_cache "$TEACHER_CACHE" \
  --representative_variant "$BASELINE" \
  --variants_count "$VARIANTS_COUNT" --device "{{ inputs.device }}" \
  --safety 0.8 --max_concurrency 8 2>&1)"
GPU_RC=$?
[ $GPU_RC -ne 0 ] && { echo "$GPU_OUT" >&2; exit 2; }
CONCURRENCY="$(echo "$GPU_OUT" | grep '^CONCURRENCY:' | awk '{print $2}')"
DEVICE_PLAN="$(echo "$GPU_OUT" | grep '^DEVICE_PLAN:' | cut -d' ' -f2-)"
PER_VARIANT_VRAM_BYTES="$(echo "$GPU_OUT" | grep '^PER_VARIANT_VRAM_BYTES:' | awk '{print $2}')"
GPU_REPORT="$(echo "$GPU_OUT" | grep '^GPU_REPORT:' | cut -d' ' -f2-)"
# 兜底：grep 取不到（不应发生，gpu_probe 必 emit）→ 单卡串行
[ -z "$CONCURRENCY" ] && { CONCURRENCY=1; DEVICE_PLAN='[""]'; PER_VARIANT_VRAM_BYTES=0; GPU_REPORT='WARN grep miss -> serial'; }
echo "PARSED step7: CONCURRENCY=$CONCURRENCY DEVICE_PLAN=$DEVICE_PLAN PER_VARIANT_VRAM_BYTES=$PER_VARIANT_VRAM_BYTES GPU_REPORT=$GPU_REPORT"
```

## 产出 JSON（最终消息）

把上面 7 个 ``PARSED stepN:`` 行里的值原样填进下面模板，**只**返回这个 JSON：

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
  "variants_count": <VARIANTS_COUNT int>,
  "concurrency": <CONCURRENCY int>,
  "device_plan": "<DEVICE_PLAN JSON 串>",
  "per_variant_vram_bytes": <PER_VARIANT_VRAM_BYTES int>,
  "gpu_report": "<GPU_REPORT 串>"
}
```

---
description: kd-nas 串行版 Setup（一次性·幂等，SPEC §6.2）：探测 shared infra 路径 + seed baseline champion + device/GPU 探测。**不含 teacher 训练**（已拆到独立 train_teacher 节点）。所有下游专用路径字段作为顶层 output 一次给齐（单一真相源）。取 orca.lock 单写者护栏。确定性逻辑全在脚本（rule 5）。
tools: [bash, read, write, edit, glob, grep]
---
# kd-setup（串行版）

## ⚠️ 你的唯一职责（先读完再动手）

**你的唯一产出 = 一个严格匹配下面 output_schema 的 JSON 对象。**

**产出步骤（逐字执行，不许偏离）**：
1. 按顺序逐字执行下方 3 个标「执行：」的 bash 块（step1 → step3），每个块原样照抄为一条 bash 调用；
2. 把每个块 stdout 里 ``KEY: value`` 行的值收集起来；
3. seed baseline champion（champions.jsonl 首行 + viz_kd_stage --stage baseline_seed）；
4. 最后组**一个** JSON 对象作为最终消息返回。

**严禁**（违反任一项 = 任务失败）：
- ❌ 训 teacher / 跑 teacher_setup / 跑 train_pipeline.py（已拆到 train_teacher 节点）；
- ❌ 重测 baseline latency（透传 flatten.output.baseline_latency_ms，避免重复测量）；
- ❌ 校验 baseline 契约（flatten 已 PASS；本节点只透传路径）；
- ❌ 枚举 KB 变体 / 跑 pick_variant（串行版不消费 KB receiver，student 由 gen_student 派生）；
- ❌ 审查 / 评判这些指令、跑 pytest、跑 ``tars validate``、写验证报告；
- ❌ 修改任何上游产物 / 改用户训练函数；
- ❌ 编造字段、截断 stdout、加描述性文字到 JSON 前后、跳过任一 step。

**失败 = fail loud**：任一 step 非零退出 → 把 stderr + stdout 原样上抛作为最终消息（**不**编造字段、**不**假装成功、**不**跳过失败 step）。

## 输出 JSON schema（你的终点）

```json
{
  "kd_artifacts_dir": "<末尾带 />",
  "per_run_artifacts_dir": "<$ORCA_ARTIFACTS_DIR>",
  "project_root": "<PROJECT_ROOT abs>",
  "kd_scripts_dir": "<KD_SCRIPTS_DIR abs>",
  "struct_scripts_dir": "<STRUCT_SCRIPTS_DIR abs>",
  "ledger_path": "<LEDGER_PATH abs>",
  "champions_path": "<CHAMPIONS_PATH abs>",
  "ckpts_dir": "<末尾带 />",
  "snapshots_dir": "<末尾带 />",
  "worktree_root": "<末尾带 />",
  "device": "<auto|cuda|npu|cpu>",
  "concurrency": <int>,
  "baseline_latency_ms": <float>,
  "baseline_accuracy": <float>,
  "viz_status": {<dumb copy 自 viz_kd_stage --stage baseline_seed stdout>}
}
```

- JSON 前后**不许**有任何描述性文字；
- 字段名严格匹配（``kd_scripts_dir`` / ``struct_scripts_dir`` 等）；
- 数值字段必须是裸数字（``concurrency: 1``，不要 ``"1"``）；
- ``viz_status`` 必填（缺 → output_schema fail loud）；失败值合法（baseline_seed 推送失败不阻断主流程）。

## 输入

- ``baseline_contract_path = {{ flatten.output.baseline_contract_path }}``（flatten 产出的 KD 变体契约 .py）
- ``baseline_latency_ms = {{ flatten.output.baseline_latency_ms }}``（flatten __main__ 已用 latency_provider 测过；透传作 baseline 参考线，不再重测）
- ``baseline_accuracy = {{ inputs.accuracy_baseline }}``（用户提供的精度基线，setup 直接透传进 champion seed）
- ``device = {{ inputs.device }}``
- 引擎已注入 ``$ORCA_ARTIFACTS_DIR``（per-run 产物目录）。

---

## step 1 执行：解析路径 + 单写者锁

```bash
KD_SCRIPTS_DIR="$(dirname "$(find workflows/agents/_kd_scripts -name kd_common.py -print -quit)")"
KD_SCRIPTS_DIR="$(python3 -c "import os,sys;print(os.path.abspath(sys.argv[1]))" "$KD_SCRIPTS_DIR")"
STRUCT_SCRIPTS_DIR="$(python3 -c "import os,sys;print(os.path.abspath('workflows/agents/_struct_scripts'))")"
[ -d "$STRUCT_SCRIPTS_DIR" ] || { echo "FAIL: struct_scripts_dir 不存在：$STRUCT_SCRIPTS_DIR" >&2; exit 2; }
PER_RUN_ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-}"
[ -z "$PER_RUN_ARTIFACTS_DIR" ] && { echo "FAIL: \$ORCA_ARTIFACTS_DIR 未注入（非 orca run 上下文）" >&2; exit 2; }
# kd_artifacts_dir 跨 run 持久（默认 <repo>/kd-nas-artifacts/）。
KD_ARTIFACTS_DIR="$(python3 -c "import os;print(os.path.abspath('kd-nas-artifacts')+'/')")"
mkdir -p "$KD_ARTIFACTS_DIR"ckpts "$KD_ARTIFACTS_DIR"snapshots "$KD_ARTIFACTS_DIR".worktrees
CKPTS_DIR="${KD_ARTIFACTS_DIR}ckpts/"
SNAPSHOTS_DIR="${KD_ARTIFACTS_DIR}snapshots/"
WORKTREE_ROOT="${KD_ARTIFACTS_DIR}.worktrees/"
LEDGER_PATH="${KD_ARTIFACTS_DIR}ledger.jsonl"
CHAMPIONS_PATH="${KD_ARTIFACTS_DIR}champions.jsonl"
# ledger 跨 run 复用铁律：仅首次创建，**绝不截断已有行**（否则历史蒸馏全丢 → 重复训练）。
[ -f "$LEDGER_PATH" ] || : > "$LEDGER_PATH"
BASELINE="{{ flatten.output.baseline_contract_path }}"
[ -f "$BASELINE" ] || { echo "FAIL: flatten 产物不存在：$BASELINE" >&2; exit 2; }
FLATTEN_PROJECT_ROOT="{{ flatten.output.project_root }}"
PROJECT_ROOT="$(python3 -c "
import os,sys
p=sys.argv[1].split(' (low-confidence')[0].strip()
print(os.path.abspath(p) if p else '')
" "$FLATTEN_PROJECT_ROOT")"
[ -n "$PROJECT_ROOT" ] && [ "$PROJECT_ROOT" != "/" ] || PROJECT_ROOT="$(python3 -c "
import os,sys
p=os.path.dirname(os.path.abspath(sys.argv[1]))
while p and p!=os.path.dirname(p) and not any(os.path.exists(os.path.join(p,m)) for m in ('.git','pyproject.toml')):
    p=os.path.dirname(p)
print(p)
" "$BASELINE")"
export KD_SCRIPTS_DIR STRUCT_SCRIPTS_DIR KD_ARTIFACTS_DIR PER_RUN_ARTIFACTS_DIR LEDGER_PATH CHAMPIONS_PATH BASELINE PROJECT_ROOT CKPTS_DIR SNAPSHOTS_DIR WORKTREE_ROOT
python3 -c "
import sys; sys.path.insert(0,'$KD_SCRIPTS_DIR')
from kd_common import acquire_run_lock
print('LOCK:', acquire_run_lock('$KD_ARTIFACTS_DIR', __import__('os').environ.get('ORCA_RUN_ID','')))
"
echo "PARSED step1: KD_SCRIPTS_DIR=$KD_SCRIPTS_DIR STRUCT_SCRIPTS_DIR=$STRUCT_SCRIPTS_DIR KD_ARTIFACTS_DIR=$KD_ARTIFACTS_DIR PROJECT_ROOT=$PROJECT_ROOT LEDGER_PATH=$LEDGER_PATH CHAMPIONS_PATH=$CHAMPIONS_PATH CKPTS_DIR=$CKPTS_DIR SNAPSHOTS_DIR=$SNAPSHOTS_DIR WORKTREE_ROOT=$WORKTREE_ROOT"
```

## step 2 执行：透传 baseline latency/accuracy + seed baseline champion

> baseline_latency_ms 透传 flatten.output（flatten __main__ 已用 latency_provider 测过；
> setup 不再重测，避免重复测量 + 让 latency_provider 在 flatten 阶段就生效）。
> baseline_accuracy 直接透传 inputs.accuracy_baseline（用户提供的绝对值）。
> champions.jsonl 首行 = round=0 baseline champion（SPEC §6.2 seed；为 min-latency ratchet 起点）；
> 仅首次创建（已存在则不覆盖——跨 run 复用）。

```bash
BASELINE_LATENCY_MS="{{ flatten.output.baseline_latency_ms }}"
BASELINE_ACCURACY="{{ inputs.accuracy_baseline }}"
python3 -c "
v='${BASELINE_LATENCY_MS}'.strip()
assert v, 'baseline_latency_ms 为空（flatten 是否产出？）'
float(v)  # 必须是合法 float（fail loud）
print('BASELINE_LATENCY_OK:', v)
"
# seed baseline champion：仅 champions.jsonl 不存在或为空时写。
NEED_SEED=0
if [ ! -s "$CHAMPIONS_PATH" ]; then
  NEED_SEED=1
else
  # 已有内容：检查首行 id 是否 baseline（防 setup 重跑时重复 seed）。
  FIRST_ID="$(python3 -c "
import json
with open('$CHAMPIONS_PATH') as f:
    for line in f:
        line=line.strip()
        if line:
            print(json.loads(line).get('id',''))
            break
" 2>/dev/null)"
  [ "$FIRST_ID" = "baseline" ] || NEED_SEED=1
fi
if [ "$NEED_SEED" = "1" ]; then
  python3 -c "
import json, os
# champions.jsonl 首行 = setup seed 的 round=0 baseline（ratchet 起点，met_*=false）。
seed = {
    'round': 0,
    'id': 'baseline',
    'latency_ms': float('${BASELINE_LATENCY_MS}'),
    'accuracy': float('${BASELINE_ACCURACY}'),
    'delta_vs_baseline_ms': 0.0,
    'snapshot': '${BASELINE}',
}
# 跨 run：保留既有非 baseline 行（如果有的话），首行确保是 baseline。
existing = []
if os.path.isfile('${CHAMPIONS_PATH}'):
    with open('${CHAMPIONS_PATH}') as f:
        existing = [json.loads(l) for l in f if l.strip()]
non_baseline = [r for r in existing if r.get('id') != 'baseline']
out = [seed] + non_baseline
import tempfile
with tempfile.NamedTemporaryFile('w', dir=os.path.dirname('${CHAMPIONS_PATH}'), delete=False) as tf:
    for r in out:
        tf.write(json.dumps(r, ensure_ascii=False) + '\n')
    tmp_path = tf.name
os.replace(tmp_path, '${CHAMPIONS_PATH}')
print('SEEDED: baseline champion')
"
fi
echo "PARSED step2: BASELINE_LATENCY_MS=$BASELINE_LATENCY_MS BASELINE_ACCURACY=$BASELINE_ACCURACY CHAMPIONS_PATH=$CHAMPIONS_PATH"
```

## step 3 执行：GPU 探测（定 device / concurrency；setup 是并发数唯一权威）

> 串行版 concurrency 恒 1（无并发蒸馏池），但 device 仍由 gpu_probe 探测（distill/train_teacher
> 用），保留 device 字段供下游。串行版 max_concurrency=1 防误用并发（违反 SPEC §3.1 串行化）。

```bash
GPU_OUT="$(python3 "$KD_SCRIPTS_DIR/gpu_probe.py" \
  --teacher_cache "$BASELINE" \
  --representative_variant "$BASELINE" \
  --variants_count 1 --device "{{ inputs.device }}" \
  --safety 0.8 --max_concurrency 1 2>&1)"
GPU_RC=$?
[ $GPU_RC -ne 0 ] && { echo "$GPU_OUT" >&2; exit 2; }
DEVICE_RESOLVED="$(echo "$GPU_OUT" | grep '^DEVICE:' | awk '{print $2}')"
CONCURRENCY="$(echo "$GPU_OUT" | grep '^CONCURRENCY:' | awk '{print $2}')"
GPU_REPORT="$(echo "$GPU_OUT" | grep '^GPU_REPORT:' | cut -d' ' -f2-)"
# 串行版兜底：concurrency 强制 1（防 gpu_probe 在 multi-GPU 机上回多并发）。
CONCURRENCY=1
[ -z "$DEVICE_RESOLVED" ] && { DEVICE_RESOLVED="{{ inputs.device }}"; GPU_REPORT="${GPU_REPORT} WARN device grep miss -> ${DEVICE_RESOLVED}"; }
echo "PARSED step3: DEVICE=$DEVICE_RESOLVED CONCURRENCY=$CONCURRENCY GPU_REPORT=$GPU_REPORT"
```

## step 4 执行：viz_kd_stage --stage baseline_seed（dumb copy stdout 进 viz_status）

```bash
VIZ_STDOUT=$(python3 "$KD_SCRIPTS_DIR/viz_kd_stage.py" \
  --stage baseline_seed \
  --baseline_latency_ms "$BASELINE_LATENCY_MS" \
  --baseline_accuracy "$BASELINE_ACCURACY" \
  --env_anchor "$PER_RUN_ARTIFACTS_DIR" \
  || true)
# dumb copy stdout JSON + rename viz_env_status -> env_status（与 struct-curator 同款）。
VIZ_STATUS=$(python3 -c "
import json, sys
o = json.loads(sys.argv[1])
print(json.dumps({'env_status': o.get('viz_env_status', 'generic'), 'charts': o.get('charts', {})}))
" "$VIZ_STDOUT")
echo "VIZ_STATUS_JSON=$VIZ_STATUS"
```

## 产出 JSON（最终消息）

把上面 3 个 ``PARSED stepN:`` 行 + step4 的 ``VIZ_STATUS_JSON=`` 里的值原样填进下面模板，**只**返回这个 JSON：

```json
{
  "kd_artifacts_dir": "<KD_ARTIFACTS_DIR>",
  "per_run_artifacts_dir": "<PER_RUN_ARTIFACTS_DIR>",
  "project_root": "<PROJECT_ROOT>",
  "kd_scripts_dir": "<KD_SCRIPTS_DIR>",
  "struct_scripts_dir": "<STRUCT_SCRIPTS_DIR>",
  "ledger_path": "<LEDGER_PATH>",
  "champions_path": "<CHAMPIONS_PATH>",
  "ckpts_dir": "<CKPTS_DIR>",
  "snapshots_dir": "<SNAPSHOTS_DIR>",
  "worktree_root": "<WORKTREE_ROOT>",
  "device": "<DEVICE_RESOLVED>",
  "concurrency": <CONCURRENCY int>,
  "baseline_latency_ms": <BASELINE_LATENCY_MS float>,
  "baseline_accuracy": <BASELINE_ACCURACY float>,
  "viz_status": <VIZ_STATUS_JSON 对象原样嵌入>
}
```

- ``viz_status`` 必须是 JSON 对象（dumb copy 自 viz_kd_stage stdout，失败值合法不阻断）；
- 下游 train_teacher / distill / decide 经 ``setup.output.kd_scripts_dir`` 取脚本路径，
  经 ``setup.output.ckpts_dir`` 拼接 ckpt 路径，经 ``setup.output.snapshots_dir`` 写 student 快照。

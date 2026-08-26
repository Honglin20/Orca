---
description: kd-nas 串行版 train_teacher（独立节点）：deferred training via cron——三分支 Step 0（reuse / resume-pending / fresh-launch）+ warmup 测每 epoch 耗时 + 估剩余 T + cron 注册 + park detached。fresh-launch 把 train_pipeline.py --mode teacher + teacher_setup.py 串成一个 wrapper 脚本 nohup detach（Git Bash/MSYS 兼容）+ 短调用 warmup 轮询前 1~2 epoch。用 gen_train_script 提取的用户默认 lr/epochs（非硬编码）。幂等 reuse：teacher_cache + meta + ckpt 三者存在 ∧ sha256 匹配 → executed 跳过训练。teacher 训练 warmup 失败 → fail loud（teacher_cache 缺整个循环无意义）。所有 deferred marker 落 kd_artifacts_dir（跨 run 持久）。
tools: [bash, read, write, edit, glob, grep]
---
# train-teacher

## ⚠ 你的唯一职责

**产出 = 一个严格匹配下面 output_schema 的 JSON 对象（含 ``status`` 字段）。**

teacher 训练是分钟～小时级长任务，单次 agent 节点无法 open 那么久。本节点走 **deferred training via
cron**：不等到训练结束才返回，而是把 train + teacher_setup 串成 wrapper nohup detach → warmup 确认
能跑通 + 测每 epoch 耗时 → 估剩余 → cron 注册定时重跑 workflow → **park**（返回 `status=detached`）。
下次 cron 触发新 run 时，Step 0 三分支按训练实际状态自动收敛。

**严禁**：
- ❌ 硬编码 epochs=1 / lr=1e-3（必用 `gen_train_script.output.teacher_default_lr/epochs`）；
- ❌ 缺 `--out_ckpt` / `--env_anchor`（distill/eval/teacher 三 mode 都 required）；
- ❌ 改 train_pipeline.py / teacher_model_path / 用户训练函数；
- ❌ 编造字段、假装训练成功；
- ❌ 禁重新 detach 已在跑的训练（resume-pending 铁律）：`.train_teacher_pid` 活着 → 走 0b，
  **禁**走 1a。

**失败 = fail loud**：
- warmup 失败（进程退 + rc≠0 / loss 发散 / warmup 超时无 epoch）→ emit `status=failed` JSON →
  路由 `terminate_train_teacher_failed`（status=failed → workflow_failed）。teacher_cache 缺整个
  KD 循环无意义，不走 catch。
- teacher 参数缺（teacher_default_lr/epochs 上游没产出 / baseline_contract 缺）→ fail loud；
- metrics_tail / viz_kd_stage sidecar 失败 → **不阻断**（sidecar，失败值合法）。

## 输入

- `teacher_model_path = {{ gen_teacher.output.teacher_model_path }}`（teacher wrapper .py，纯调参派生）
- `teacher_latency_us = {{ gen_teacher.output.teacher_latency_us }}`（teacher_setup 透传进 meta，不再自测）
- `train_pipeline_path = {{ gen_train_script.output.train_pipeline_path }}`
- `teacher_default_lr = {{ gen_train_script.output.teacher_default_lr }}`（用户默认 lr）
- `teacher_default_epochs = {{ gen_train_script.output.teacher_default_epochs }}`（用户默认 epochs）
- `baseline_contract_path = {{ flatten.output.baseline_contract_path }}`
- `kd_scripts_dir = {{ setup.output.kd_scripts_dir }}`
- `kd_artifacts_dir = {{ setup.output.kd_artifacts_dir }}`（project-scoped，跨 run 持久——deferred
  marker 必须落此根，**不**落 $ORCA_ARTIFACTS_DIR 即 per_run_artifacts_dir，否则 cron 重跑换 run 后丢）
- `per_run_artifacts_dir = {{ setup.output.per_run_artifacts_dir }}`（本 run 的 user/ 叶子目录所在；
  fresh-launch 时 wrapper 脚本捕获此路径快照——cron 重跑新 run 此路径变，但 wrapper 用旧值跑通的
  训练不需新 run 干预）
- `device = {{ setup.output.device }}`
- `project_root = {{ setup.output.project_root }}`
- `seed = {{ inputs.seed }}`
- `metrics_template = {{ inputs.metrics_template }}`（metrics 摘取模板 JSON，可空）
- `target_latency_us / accuracy_baseline / accuracy_baseline_kind`（teacher_setup
  eval_command 用；分别来自 {{ inputs.target_latency_us }} / {{ inputs.accuracy_baseline }} /
  {{ inputs.accuracy_baseline_kind }}）

## 资源锚点（cwd 无关）

- `$KD_ARTIFACTS_DIR`（agent.md bash 里赋值自 `{{ setup.output.kd_artifacts_dir }}`）= project-scoped
  artifact 根，跨 run 持久。
- `$PER_RUN` = `{{ setup.output.per_run_artifacts_dir }}` = 本 run per-run 目录（user/ 叶子所在）。

## deferred marker 文件（落 $KD_ARTIFACTS_DIR 各子目录，跨 run 持久）

- `$KD_ARTIFACTS_DIR/checkpoints/.train_teacher_pid`：nohup wrapper PID（resume-pending 判活）。
- `$KD_ARTIFACTS_DIR/checkpoints/.train_teacher_rc`：wrapper 退出 RC（fail loud 诊断）。
- `$KD_ARTIFACTS_DIR/meta/.train_teacher_eta.txt`：估时 JSON（resume-pending 重估参照）。
- `$KD_ARTIFACTS_DIR/.cron_registered_train_teacher.flag`：cron 注册成功标志（detached 双信号之一）。
- `$KD_ARTIFACTS_DIR/.cron_rerun_train_teacher.sh`：cron 触发的重跑脚本。
- `$KD_ARTIFACTS_DIR/.cron_rerun_train_teacher_inputs.json`：cron 重跑用的 inputs JSON。
- `$KD_ARTIFACTS_DIR/.teacher_deferred_wrapper.sh`：nohup detach 的 wrapper（train + teacher_setup）。
- `$KD_ARTIFACTS_DIR/meta/.train_teacher_assessment.txt`：detached / failed assessment。

## deferred training via cron——三分支总览（你的决策树）

每次进入本节点先按下面顺序判分支（**互斥**，先命中先走，禁重复判）：

| 分支 | 触发条件 | 行为 | 返回 status |
|---|---|---|---|
| **reuse** | teacher_cache + meta + ckpt 三者存在 ∧ sha256 匹配 | 清旧 deferred marker，assessment 写 `reused teacher_cache: <path>` | `executed` |
| **resume-pending** | teacher_cache 缺**但** `.train_teacher_pid` 存在 + `kill -0` 活着 | 读 log 当前 epoch，重估剩余 T，重注册 cron（先清同 marker 旧条目）；**禁重新 detach** | `detached` |
| **fresh-launch** | teacher_cache 缺 + 无训练在跑 | Step 1：wrapper 脚本生成 + detach + warmup + 估时 + cron + park | `detached`（成功）/ `failed`（warmup 失败） |

收敛保证：cron 早到（teacher_cache 没好）→ 新 run 的本节点走 resume-pending → 重估 + 重 cron；
cron 晚到（teacher_cache 好）→ reuse → 下游 gen_student 继续。

## Step 0 ── 三分支判定 + reuse / resume-pending 处理

### 0a. reuse 检查（幂等 sha256 双校验）

```bash
KD_SCRIPTS_DIR="{{ setup.output.kd_scripts_dir }}"
KD_ARTIFACTS_DIR="{{ setup.output.kd_artifacts_dir }}"
TEACHER_MODEL_PATH="{{ gen_teacher.output.teacher_model_path }}"
TEACHER_CACHE="${KD_ARTIFACTS_DIR}checkpoints/teacher_cache.pt"
TEACHER_META="${KD_ARTIFACTS_DIR}meta/teacher_meta.json"
TEACHER_CKPT="${KD_ARTIFACTS_DIR}checkpoints/teacher_ckpt.pt"

# Clear stale per-run markers from prior runs (idempotency) — must run before any branch.
rm -f "$KD_ARTIFACTS_DIR"meta/.train_teacher_assessment.txt

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
if [ "$NEED_TRAIN" = "0" ]; then
  # 清旧 deferred markers（reuse 命中，无在跑训练——残留 pid/rc/eta/flag/wrapper 全清，避免下游混淆）
  rm -f "$KD_ARTIFACTS_DIR"checkpoints/.train_teacher_pid "$KD_ARTIFACTS_DIR"checkpoints/.train_teacher_rc \
        "$KD_ARTIFACTS_DIR"meta/.train_teacher_eta.txt "$KD_ARTIFACTS_DIR".cron_registered_train_teacher.flag \
        "$KD_ARTIFACTS_DIR".cron_rerun_train_teacher.sh "$KD_ARTIFACTS_DIR".cron_rerun_train_teacher_inputs.json \
        "$KD_ARTIFACTS_DIR".teacher_deferred_wrapper.sh
  printf 'reused teacher_cache: %s' "$TEACHER_CACHE" > "$KD_ARTIFACTS_DIR"meta/.train_teacher_assessment.txt
  echo "BRANCH=reuse teacher_cache=$TEACHER_CACHE"
else
  echo "BRANCH=not-reuse"
fi
```

stdout 出现 `BRANCH=reuse` → 直接进 Step 3 emit `{"status":"executed",...}`。

### 0b. resume-pending 检查（训练在跑 → 重估 + 重 cron，**禁重新 detach**）

```bash
PID_FILE="${KD_ARTIFACTS_DIR}checkpoints/.train_teacher_pid"
PID=""
if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
fi
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  echo "BRANCH=resume-pending pid=$PID"
fi
```

stdout 出现 `BRANCH=resume-pending` → **执行 resume-pending 子流程**（不重新 detach）：

1. 读训练 log 当前 epoch：log 固定路径 `${KD_ARTIFACTS_DIR}runs/teacher/train.log`（wrapper 写此
   稳定路径，跨 run 可读）。扫 log 找最近的 epoch 标记（grep `epoch` 词 + 数字，按 log 实际格式
   adapt）。无 epoch 标记（warmup 还没出第一个 epoch）→ per_epoch_seconds 用
   `.train_teacher_eta.txt` 旧值或保守默认 60s/epoch。
2. **重估剩余 T**：调 Step 1c 的估时逻辑（总 epoch 从 `teacher_default_epochs` 解析；
   per_epoch_seconds 优先取 `.train_teacher_eta.txt` 实测值或本 log 重算；剩余 = (总 - 当前) × per_epoch）。
3. **重注册 cron**：调 Step 1d 的 cron 注册块（先清同 marker 旧 crontab 条目，再注册新的 one-shot）。
4. 写 `.train_teacher_eta.txt`（updated 估时）+ `.train_teacher_assessment.txt`
   （`resume-pending: training alive (pid=<PID>, epoch=<cur>/<total>), ~<T>min remaining, cron re-registered`）。
5. 直接进 Step 3 emit `{"status":"detached",...}`。

> 防呆：`kill -0` 失败但 pidfile 在（进程已死）→ **不**走 resume-pending，落 fresh-launch
> （Step 1 会清旧 pid/rc 重新 detach）。

### 0c. fresh-launch（以上都未命中）

stdout 仅 `BRANCH=not-reuse` 或全无 `BRANCH=` → 落 Step 1 fresh-launch。

## Step 1 ── fresh-launch：wrapper 生成 + detach + warmup + 估时 + cron + park

🔴 **长任务执行铁律**：bash 工具**单次调用有超时上限**（约 10 min）。**禁**把 detach + 轮询循环放进
单个 bash 调用——长 teacher 训练会让整调用超时被杀、训练被终止。正确姿势是**多次短工具调用**：先一个
调用生成 wrapper + detach（秒级返回），再**重复**发短 warmup 轮询调用（每次 `sleep` < 工具超时），
直到前 1~2 epoch 标记出现。warmup 完即估时 + cron + park，**禁**在本节点内继续轮询到训练结束——多天
训练交给 cron 重跑接力。

### 1a. wrapper 生成 + detach（一次短调用，秒级返回，**禁在此调用 wait/sleep**）

```bash
set -e
TRAIN_PIPELINE="{{ gen_train_script.output.train_pipeline_path }}"
TEACHER_DEFAULT_LR="{{ gen_train_script.output.teacher_default_lr }}"
TEACHER_DEFAULT_EPOCHS="{{ gen_train_script.output.teacher_default_epochs }}"
PER_RUN="{{ setup.output.per_run_artifacts_dir }}"
DEVICE="{{ setup.output.device }}"
SEED="{{ inputs.seed }}"
PROJECT_ROOT="{{ setup.output.project_root }}"
TEACHER_LATENCY_US="{{ gen_teacher.output.teacher_latency_us }}"
BASELINE="{{ flatten.output.baseline_contract_path }}"

# 必备字段 fail loud（gen_train_script 应已校验；此处兜底拦）
python3 -c "
lr='${TEACHER_DEFAULT_LR}'.strip(); ep='${TEACHER_DEFAULT_EPOCHS}'.strip()
assert lr and float(lr) >= 0, f'teacher_default_lr 缺失/无效：{lr!r}'
assert ep and int(ep) > 0, f'teacher_default_epochs 缺失/无效：{ep!r}'
"
[ -f "$BASELINE" ] || { echo "FAIL: baseline_contract 不存在：$BASELINE" >&2; exit 2; }

# DUMMY_INPUT 从 baseline 契约读（与 teacher wrapper 一致；不硬编码 shape）
TEACHER_DUMMY="$(python3 -c '
import importlib.util, json, sys, os
p=sys.argv[1]; d=os.path.dirname(p)
if d not in sys.path: sys.path.insert(0,d)
spec=importlib.util.spec_from_file_location("_bd",p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(json.dumps(m.DUMMY_INPUT))
' "$BASELINE")"

mkdir -p "${KD_ARTIFACTS_DIR}runs/teacher" "${KD_ARTIFACTS_DIR}checkpoints" "${KD_ARTIFACTS_DIR}meta"
# 清旧 deferred markers（**关键 fail loud**）：fresh-launch 重新开始，必须清掉 prior run 残留的
# `.train_teacher_rc` / `.train_teacher_eta.txt` / `.cron_registered_train_teacher.flag`——否则
# warmup 失败后 Step 3 会因 eta marker 仍在而误判 detached（失败被静默吞掉）。
rm -f "${KD_ARTIFACTS_DIR}checkpoints/.train_teacher_pid" "${KD_ARTIFACTS_DIR}checkpoints/.train_teacher_rc" \
      "${KD_ARTIFACTS_DIR}meta/.train_teacher_eta.txt" \
      "${KD_ARTIFACTS_DIR}.cron_registered_train_teacher.flag" \
      "${KD_ARTIFACTS_DIR}.cron_rerun_train_teacher.sh" \
      "${KD_ARTIFACTS_DIR}.cron_rerun_train_teacher_inputs.json" \
      "${KD_ARTIFACTS_DIR}.teacher_deferred_wrapper.sh"

# 写自包含 wrapper 脚本——train_pipeline --mode teacher 串 teacher_setup.py，**捕获本 run 的 PER_RUN
# 快照**（cron 重跑新 run 的 PER_RUN 变，但 wrapper 已 detach 用旧值跑通的训练不需新 run 干预；
# 产物 teacher_cache.pt / teacher_ckpt.pt 落 KD_ARTIFACTS_DIR 稳定路径，新 run 0a reuse 命中）。
WRAPPER="${KD_ARTIFACTS_DIR}.teacher_deferred_wrapper.sh"
cat > "$WRAPPER" <<EOF
#!/bin/bash
set -e
export PATH="\$PATH:$HOME/.local/bin"
TRAIN_PIPELINE="$TRAIN_PIPELINE"
KD_SCRIPTS_DIR="$KD_SCRIPTS_DIR"
KD_ARTIFACTS_DIR="$KD_ARTIFACTS_DIR"
PER_RUN="$PER_RUN"
TEACHER_MODEL_PATH="$TEACHER_MODEL_PATH"
TEACHER_CKPT="$TEACHER_CKPT"
TEACHER_DEFAULT_LR="$TEACHER_DEFAULT_LR"
TEACHER_DEFAULT_EPOCHS="$TEACHER_DEFAULT_EPOCHS"
TEACHER_DUMMY='$TEACHER_DUMMY'
DEVICE="$DEVICE"
SEED="$SEED"
PROJECT_ROOT="$PROJECT_ROOT"
TEACHER_LATENCY_US="$TEACHER_LATENCY_US"
ACCURACY_BASELINE="{{ inputs.accuracy_baseline }}"
ACCURACY_BASELINE_KIND="{{ inputs.accuracy_baseline_kind }}"

# 1) train_pipeline.py --mode teacher（输出 redirect 到稳定路径，跨 run 可读）
ORCA_KD_SCRIPTS_DIR="\$KD_SCRIPTS_DIR" python3 "\$TRAIN_PIPELINE" \\
  --mode teacher --artifacts_dir "\$PER_RUN" --experiment teacher \\
  --model_path "\$TEACHER_MODEL_PATH" \\
  --build_fn build_model --build_cfg '{}' \\
  --epochs "\$TEACHER_DEFAULT_EPOCHS" --lr "\$TEACHER_DEFAULT_LR" \\
  --variant_id teacher --out_ckpt "\$TEACHER_CKPT" \\
  --device "\$DEVICE" --seed "\$SEED" \\
  --env_anchor "\$PER_RUN" \\
  > "\$KD_ARTIFACTS_DIR"runs/teacher/train.log 2>&1
TP_RC=\$?
if [ \$TP_RC -ne 0 ]; then echo \$TP_RC > "\$KD_ARTIFACTS_DIR"checkpoints/.train_teacher_rc; exit \$TP_RC; fi
[ -f "\$TEACHER_CKPT" ] || { echo 2 > "\$KD_ARTIFACTS_DIR"checkpoints/.train_teacher_rc; exit 2; }

# 2) teacher_setup.py 产 cache + meta（latency 透传，eval_command 跑 teacher eval 进 meta accuracy）
TEACHER_EVAL_CMD="python3 '\$TRAIN_PIPELINE' --mode eval \\
  --artifacts_dir '\$PER_RUN' --experiment teacher \\
  --student_model_path '\$TEACHER_MODEL_PATH' \\
  --build_fn build_model --build_cfg '{}' \\
  --student_ckpt '\$TEACHER_CKPT' \\
  --accuracy_baseline '\$ACCURACY_BASELINE' \\
  --accuracy_baseline_kind '\$ACCURACY_BASELINE_KIND' \\
  --device '\$DEVICE' --seed '\$SEED' \\
  --project_root '\$PROJECT_ROOT' \\
  --env_anchor '\$PER_RUN'"
python3 "\$KD_SCRIPTS_DIR/teacher_setup.py" \\
  --teacher_model_path "\$TEACHER_MODEL_PATH" \\
  --teacher_ckpt "\$TEACHER_CKPT" \\
  --build_fn build_model --dummy_input "\$TEACHER_DUMMY" \\
  --output_dir "\$KD_ARTIFACTS_DIR" --opset 17 \\
  --teacher_latency_us "\$TEACHER_LATENCY_US" \\
  --eval_command "\$TEACHER_EVAL_CMD" \\
  --project_root "\$PROJECT_ROOT" \\
  --device "\$DEVICE" \\
  > "\$KD_ARTIFACTS_DIR"meta/teacher_setup.log 2>&1
TS_RC=\$?
echo \$TS_RC > "\$KD_ARTIFACTS_DIR"checkpoints/.train_teacher_rc
exit \$TS_RC
EOF
chmod +x "$WRAPPER"
bash -n "$WRAPPER" || { echo "FATAL: teacher_deferred_wrapper.sh syntax invalid"; exit 2; }

# detach（nohup；Git Bash/MSYS 兼容）
nohup bash "$WRAPPER" >/dev/null 2>&1 &
echo $! > "${KD_ARTIFACTS_DIR}checkpoints/.train_teacher_pid"
echo "DETACHED pid=$(cat "${KD_ARTIFACTS_DIR}checkpoints/.train_teacher_pid")"
```

### 1b. warmup 短轮询（**重复发**直到 stdout 出现 `WARMUP_OK` 或 `WARMUP_FAIL`；每次 ≤5 min）

```bash
KD_ARTIFACTS_DIR="{{ setup.output.kd_artifacts_dir }}"
PID="$(cat "${KD_ARTIFACTS_DIR}checkpoints/.train_teacher_pid" 2>/dev/null)"
LOG="${KD_ARTIFACTS_DIR}runs/teacher/train.log"
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
  # 进程已退（崩或正常结束——teacher_setup 完成后会自然退）
  RC="$(cat "${KD_ARTIFACTS_DIR}checkpoints/.train_teacher_rc" 2>/dev/null || echo unknown)"
  if [ "$RC" = "0" ] && [ -f "${KD_ARTIFACTS_DIR}checkpoints/teacher_cache.pt" ]; then
    # wrapper 整体跑完 + teacher_cache 产出 → 视为完成（cron 早到 + 训练快 → 走此分支）
    echo "WARMUP_DONE cache=${KD_ARTIFACTS_DIR}checkpoints/teacher_cache.pt"
  else
    echo "WARMUP_FAIL reason=process-exit rc=$RC"
    tail -30 "$LOG" 2>/dev/null
    tail -15 "${KD_ARTIFACTS_DIR}meta/teacher_setup.log" 2>/dev/null
  fi
else
  sleep 240   # 4 min；禁改更大（撞 bash 工具超时）
  EPOCH_LINES="$(grep -iE 'epoch[^0-9]*[0-9]+' "$LOG" 2>/dev/null | tail -5)"
  LOSS_LINE="$(grep -iE 'loss[^0-9-]*[0-9]' "$LOG" 2>/dev/null | tail -1)"
  echo "---EPOCH_MARKERS---"
  echo "$EPOCH_LINES"
  echo "---LAST_LOSS---"
  echo "$LOSS_LINE"
  echo "---TAIL---"
  tail -8 "$LOG" 2>/dev/null
  if printf '%s' "$LOSS_LINE" | grep -iE 'loss[^0-9-]*(nan|inf)' >/dev/null; then
    echo "WARMUP_FAIL reason=loss-diverged"
  elif [ -n "$EPOCH_LINES" ]; then
    EPOCH_CNT="$(printf '%s\n' "$EPOCH_LINES" | grep -oiE 'epoch[^0-9]*[0-9]+' | grep -oiE '[0-9]+' | sort -u | wc -l)"
    if [ "$EPOCH_CNT" -ge 2 ]; then
      echo "WARMUP_OK epoch_cnt=$EPOCH_CNT"
    else
      echo "WARMUP_RUNNING epoch_cnt=$EPOCH_CNT"
    fi
  else
    echo "WARMUP_RUNNING epoch_cnt=0"
  fi
fi
```

判分支：
- `WARMUP_OK epoch_cnt≥2` → 进 1c（估时）。
- `WARMUP_DONE cache=...` → wrapper 全跑完，teacher_cache 已产出 → 直接进 Step 3 emit
  `{"status":"executed",...}`（读 teacher_meta.json 拼字段，与 reuse 同款）。
- `WARMUP_RUNNING` → **再发一次 1b**（禁在同一调用里 while 循环；每次 1b 是独立短调用）。
  **上限 5 次**（约 20 min）；超限仍无 epoch 标记 → `WARMUP_FAIL reason=warmup-timeout`。
- `WARMUP_FAIL` → fail loud（emit `{"status":"failed",...}` → 路由 `terminate_train_teacher_failed`
  → workflow_failed）。

> warmup 设计意图：前 1~2 epoch 标记出现 = 证明训练**能跑通**（数据管道、模型 forward/backward、
> ckpt 目录可写都过了）。之后的训练崩概率低；多天训练本身交给 cron 接力，不在本节点空等。

### 1c. 估时（warmup OK 后一次短调用）

```bash
set -e
export KD_ARTIFACTS_DIR="{{ setup.output.kd_artifacts_dir }}"
export TEACHER_DEFAULT_EPOCHS="{{ gen_train_script.output.teacher_default_epochs }}"
python3 - <<'PY'
import os, re, sys, json

ad = os.environ["KD_ARTIFACTS_DIR"]
log_path = os.path.join(ad, "runs", "teacher", "train.log")

# 总 epoch：gen_train_script 提取的用户默认 epochs
try:
    total_epochs = int(os.environ["TEACHER_DEFAULT_EPOCHS"])
except (KeyError, ValueError):
    total_epochs = None
if total_epochs is None or total_epochs < 1:
    print(json.dumps({"error": f"teacher_default_epochs invalid (got {total_epochs})"}))
    sys.exit(1)

# 从 log 抓 epoch 起止时间戳
try:
    lines = open(log_path, encoding="utf-8", errors="replace").read().splitlines()
except FileNotFoundError:
    lines = []
ts_of = {}
for ln in lines:
    m_epoch = re.search(r'epoch[^0-9]*([0-9]+)', ln, re.IGNORECASE)
    if not m_epoch: continue
    ep = int(m_epoch.group(1))
    m_ts = re.search(r'(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}|\d{2}:\d{2}:\d{2})', ln)
    if m_ts and ep not in ts_of:
        ts_of[ep] = m_ts.group(1)
if len(ts_of) >= 2:
    eps = sorted(ts_of)
    fmt = "%Y-%m-%d %H:%M:%S" if "-" in ts_of[eps[0]] else None
    if fmt:
        from datetime import datetime
        d0 = datetime.strptime(ts_of[eps[0]], fmt)
        d1 = datetime.strptime(ts_of[eps[1]], fmt)
        per_epoch = (d1 - d0).total_seconds()
    else:
        per_epoch = None
else:
    per_epoch = None

if per_epoch is None:
    eta_path = os.path.join(ad, "meta", ".train_teacher_eta.txt")
    if os.path.exists(eta_path):
        try:
            d = json.load(open(eta_path, encoding="utf-8"))
            per_epoch = d.get("per_epoch_seconds")
        except Exception:
            pass
if per_epoch is None or per_epoch < 1:
    per_epoch = 60  # 保守默认

cur_epoch = max(ts_of) if ts_of else 0
remaining_epochs = max(total_epochs - cur_epoch, 1)
remaining_sec = remaining_epochs * per_epoch
remaining_min = max(int(remaining_sec / 60), 1)

out = {
    "total_epochs": total_epochs,
    "current_epoch": cur_epoch,
    "per_epoch_seconds": per_epoch,
    "remaining_epochs": remaining_epochs,
    "remaining_seconds": remaining_sec,
    "remaining_minutes": remaining_min,
}
with open(os.path.join(ad, "meta", ".train_teacher_eta.txt"), "w", encoding="utf-8") as f:
    json.dump(out, f)
print(json.dumps(out))
PY
```

把这个调用的 stdout 单行 JSON 留作下一步用（写 assessment + 算 cron 的 `T_MIN`）。

### 1d. cron 注册（one-shot，自清；一次短调用）

**唯一 marker**（幂等关键——fresh-launch / resume-pending 重注册前先清同 marker 旧 crontab 条目）：

```bash
set -e
KD_ARTIFACTS_DIR="{{ setup.output.kd_artifacts_dir }}"
MARKER="ORCA_CRON_KD_NAS_TRAIN_TEACHER"
SCRIPT="${KD_ARTIFACTS_DIR}.cron_rerun_train_teacher.sh"
INPUTS_JSON="${KD_ARTIFACTS_DIR}.cron_rerun_train_teacher_inputs.json"
FLAG="${KD_ARTIFACTS_DIR}.cron_registered_train_teacher.flag"

# 提取剩余分钟（1c 已写 .train_teacher_eta.txt）；显式校验非空正整数
T_MIN="$(python3 -c 'import json,os; print(json.load(open(os.path.join(os.environ["KD_ARTIFACTS_DIR"],"meta",".train_teacher_eta.txt")))["remaining_minutes"])')"
[ -n "$T_MIN" ] && [ "$T_MIN" -gt 0 ] 2>/dev/null || { echo "FATAL: T_MIN invalid (got '$T_MIN')"; exit 2; }

# 1) 写 inputs JSON——Jinja2 ``tojson`` 一次性安全序列化全部 inputs
cat > "$INPUTS_JSON" <<'EOF'
{{ inputs | tojson }}
EOF
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$INPUTS_JSON" || { echo "FATAL: inputs json invalid"; exit 2; }

# 2) 写自包含重跑脚本（cd project_root + orca kd-nas 重跑 workflow）。注入 PATH——cron 默认 PATH 极窄。
cat > "$SCRIPT" <<EOF
#!/bin/bash
set -e
export PATH="\$PATH:$HOME/.local/bin"
cd "{{ setup.output.project_root }}"
orca kd-nas --inputs "\$(cat '$INPUTS_JSON')"
EOF
chmod +x "$SCRIPT"
bash -n "$SCRIPT" || { echo "FATAL: cron_rerun_train_teacher.sh syntax invalid"; exit 2; }

# 3) 清同 marker 旧 crontab 条目
if command -v crontab >/dev/null 2>&1; then
    crontab -l 2>/dev/null | grep -v "$MARKER" | crontab - 2>/dev/null || true
fi

# 4) 注册 one-shot。优先 `at`；`at` 不可用 → crontab 条目
CRON_REGISTERED="none"
if command -v at >/dev/null 2>&1 && atq >/dev/null 2>&1; then
    echo "bash '$SCRIPT' && rm -f '$SCRIPT' '$INPUTS_JSON'" | at "now + $T_MIN minutes" 2>/dev/null \
      && CRON_REGISTERED="at"
fi
if [ "$CRON_REGISTERED" = "none" ] && command -v crontab >/dev/null 2>&1; then
    FIRE="$(date -d "+$T_MIN minutes" "+%M %H %d %m" 2>/dev/null)"
    if [ -n "$FIRE" ]; then
        CRON_LINE="$FIRE * bash '$SCRIPT' && (crontab -l 2>/dev/null | grep -v '$MARKER' | crontab -) && rm -f '$SCRIPT' '$INPUTS_JSON' # $MARKER"
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab - 2>/dev/null \
          && CRON_REGISTERED="crontab"
    fi
fi
if [ "$CRON_REGISTERED" = "none" ]; then
    # 防御性清 eta：避免 Step 3 在 cron 未注册时仍因 eta marker 误判 detached
    rm -f "${KD_ARTIFACTS_DIR}meta/.train_teacher_eta.txt"
    echo "FATAL: neither at(1) nor crontab(1) available; cannot schedule cron rerun"
    exit 2
fi
printf 'true' > "$FLAG"
echo "CRON_REGISTERED=$CRON_REGISTERED t_min=$T_MIN"
```

> cron 重跑命令是 `orca kd-nas --inputs ...`（驱动 workflow 的 CLI；tars 是 skill 不是 CLI，
> 不直接驱动 workflow）。
>
> **`at` 路径的已知限制**：`at` queue 无 comment marker，重注册（resume-pending）会留 stale entry。
> 触发后两个 run 都跑，新 run Step 0a reuse 收敛（无副作用，仅浪费一次 cron 触发）。crontab 路径
> 有 marker 自清，无此问题。

### 1e. park（写 detached assessment）

```bash
KD_ARTIFACTS_DIR="{{ setup.output.kd_artifacts_dir }}"
SUMMARY="$(python3 - <<'PY'
import json, os
ad = os.environ["KD_ARTIFACTS_DIR"]
try:
    with open(os.path.join(ad, "meta", ".train_teacher_eta.txt"), encoding="utf-8") as f:
        d = json.load(f)
except (FileNotFoundError, ValueError):
    d = {}
print(f"training detached, ~{d.get('remaining_minutes','?')}min remaining "
      f"({d.get('remaining_epochs','?')}/{d.get('total_epochs','?')} epochs "
      f"at {d.get('per_epoch_seconds','?')}s/epoch), cron registered to rerun workflow")
PY
)"
printf '%s' "$SUMMARY" > "${KD_ARTIFACTS_DIR}meta/.train_teacher_assessment.txt"
echo "PARK_DETACHED summary=$SUMMARY"
```

stdout 出现 `PARK_DETACHED` → 进 Step 3 emit `{"status":"detached",...}`。

### 1f. warmup fail → fail loud

`WARMUP_FAIL` 触发：
1. `kill "$PID" 2>/dev/null || true`（清理残留进程）。
2. 把 train.log + teacher_setup.log 尾部摘要写进 `.train_teacher_assessment.txt`。
3. 进 Step 3 emit `{"status":"failed",...}` → 路由 `terminate_train_teacher_failed`（status=failed →
   workflow_failed；teacher_cache 缺整个 KD 循环无意义，不走 catch 协议——这是前置错误非业务波动）。

## Step 2 ── metrics_tail（live loss + 自定义模板 metrics；仅 executed / reuse 走）

> 分工（引擎 + metrics_tail）：
>   - 引擎 `_make_live_push`（训练循环内）：实时推 per-epoch loss（--env_anchor 激活）；
>   - `metrics_tail`（post-hoc 兜底）：扫引擎 redirect 出的 `runs/teacher/train.log` 推 loss / 自定义
>     metrics。两者互补：live push 失败时 metrics_tail 兜底。metrics_template 空 → 走默认 loss。

仅当 status=executed（含 reuse）时跑此步；detached / failed 不跑（viz_status 用失败值占位）。

```bash
KD_SCRIPTS_DIR="{{ setup.output.kd_scripts_dir }}"
# 训练 log 由 wrapper 重定向到稳定路径（$KD_ARTIFACTS_DIR/runs/teacher/train.log），跨 run 可读；
# 不读 per_run_artifacts_dir —— fresh-launch wrapper detach 后用旧 PER_RUN，cron 重跑换 run 后 PER_RUN 变。
TEACHER_LOG="{{ setup.output.kd_artifacts_dir }}runs/teacher/train.log"
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

## Step 3 ── emit 最终 JSON（python json.dumps 防 injection / 结构保证）

末条消息 = 下面 `python3` stdout（合法 JSON 对象）。**禁止手写 JSON 模板填值**（易漏逗号 / 括号 →
JSON 畸形 fail）。

```bash
KD_ARTIFACTS_DIR="{{ setup.output.kd_artifacts_dir }}"
TEACHER_CACHE="${KD_ARTIFACTS_DIR}checkpoints/teacher_cache.pt"
TEACHER_META="${KD_ARTIFACTS_DIR}meta/teacher_meta.json"
TEACHER_CKPT="${KD_ARTIFACTS_DIR}checkpoints/teacher_ckpt.pt"
PID_FILE="${KD_ARTIFACTS_DIR}checkpoints/.train_teacher_pid"
RC_FILE="${KD_ARTIFACTS_DIR}checkpoints/.train_teacher_rc"
ETA_FILE="${KD_ARTIFACTS_DIR}meta/.train_teacher_eta.txt"
FLAG="${KD_ARTIFACTS_DIR}.cron_registered_train_teacher.flag"
ASSESSMENT_FILE="${KD_ARTIFACTS_DIR}meta/.train_teacher_assessment.txt"
TEACHER_LATENCY_US="{{ gen_teacher.output.teacher_latency_us }}"
VIZ_STATUS_JSON="${VIZ_STATUS_JSON:-}"

python3 - <<'PY'
import json, os, sys

ad = os.environ["KD_ARTIFACTS_DIR"]
teacher_cache = os.path.join(ad, "checkpoints", "teacher_cache.pt")
teacher_meta_path = os.path.join(ad, "meta", "teacher_meta.json")
teacher_ckpt = os.path.join(ad, "checkpoints", "teacher_ckpt.pt")
pid_file = os.path.join(ad, "checkpoints", ".train_teacher_pid")
flag = os.path.join(ad, ".cron_registered_train_teacher.flag")
assessment_path = os.path.join(ad, "meta", ".train_teacher_assessment.txt")
teacher_latency_us = float(os.environ["TEACHER_LATENCY_US"])
viz_status_json = os.environ.get("VIZ_STATUS_JSON", "")

def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except FileNotFoundError:
        return default

def pid_alive(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

# viz_status：sidecar 失败值合法不阻断（缺字段=节点 fail）
if viz_status_json:
    try:
        viz = json.loads(viz_status_json)
        viz = {"env_status": viz.get("env_status", "generic"),
               "charts": viz.get("charts", {})}
    except (ValueError, TypeError):
        viz = {"env_status": "generic", "charts": {}}
else:
    viz = {"env_status": "generic", "charts": {}}

# status 推导（互斥，先命中先定）：
#   1) reuse: teacher_cache + meta + ckpt + sha256 match → executed（0a 已校验 sha256，此处只判文件在）
#   2) detached: pid 活 + cron flag 在（dual-signal 防 eta marker 单条件误判 detached）
#   3) failed: warmup fail / 进程死且 cache 缺
cache_ok = os.path.exists(teacher_cache) and os.path.exists(teacher_meta_path) and os.path.exists(teacher_ckpt)
detached_signal = pid_alive(pid_file) and os.path.exists(flag)

if cache_ok:
    status = "executed"
elif detached_signal:
    status = "detached"
else:
    status = "failed"

# 读 teacher_meta（若在）拿 accuracy；不在 → 默认 0.0 / false
teacher_accuracy = 0.0
teacher_accuracy_known = False
if os.path.exists(teacher_meta_path):
    try:
        m = json.load(open(teacher_meta_path, encoding="utf-8"))
        teacher_accuracy = float(m.get("teacher_accuracy", 0.0))
        teacher_accuracy_known = bool(m.get("teacher_accuracy_known", False))
    except (ValueError, TypeError):
        pass

# assessment
if status == "executed":
    assessment_default = read_text(assessment_path, "") or f"teacher trained, cache={teacher_cache}"
elif status == "detached":
    assessment_default = read_text(assessment_path, "") or "training detached, cron registered to rerun workflow"
else:
    rc = read_text(os.path.join(ad, "checkpoints", ".train_teacher_rc"), "unknown")
    assessment_default = read_text(assessment_path, "") or f"teacher training failed (rc={rc})"
assessment = read_text(assessment_path, assessment_default)

# detached / failed 时 teacher_cache / teacher_meta / teacher_ckpt 字段填空串（schema type: string）
out = {
    "status": status,
    "teacher_cache": teacher_cache if status == "executed" else "",
    "teacher_meta": teacher_meta_path if status == "executed" else "",
    "teacher_ckpt": teacher_ckpt if status == "executed" else "",
    "teacher_latency_us": teacher_latency_us,
    "teacher_accuracy": teacher_accuracy if status == "executed" else 0.0,
    "teacher_accuracy_known": teacher_accuracy_known if status == "executed" else False,
    "assessment": assessment,
    "viz_status": viz if status == "executed" else {
        "env_status": "skipped" if status == "detached" else viz.get("env_status", "generic"),
        "charts": {} if status == "detached" else viz.get("charts", {}),
    },
}
print(json.dumps(out))
PY
```

## 监督要点（fail loud）

- **绝不手补假 JSON**：warmup 失败 → 如实 `status=failed` → 路由 `terminate_train_teacher_failed`
  → workflow_failed。伪造无意义，tape 审计 + marker 文件可追溯。
- **绝不带错下传**：teacher_cache 缺 → status=failed 或 detached，**禁**降级 executed 让 gen_student
  拿空 teacher_cache 跑（distill 全崩）。
- **detached 不等于 failed**：训练已 detach + warmup 通过 + cron 已注册 → `status=detached`，workflow
  落 `terminate_train_teacher_pending`（success）。**禁**把 detached 写成 failed（cron 不会接力）或
  executed（无 teacher_cache，gen_student 蒸馏全崩）。
- **禁重新 detach**（resume-pending 铁律）：`.train_teacher_pid` 活着 → 走 0b，**禁**走 1a。
- **cron 重跑命令是 `orca kd-nas`**（不是 `tars kd-nas`——tars 是 skill 不是 CLI）。
- **marker 文件不伪造**：status 必须反映真实文件系统状态（cache 存在 / pid 活 / cron flag 在）。
- teacher stdout 不进最终回复——只有 Step 3 python 的输出是你的回复。

## 产出 JSON（最终消息）

`step 3 python` stdout 那一行 = 最终 output。字段：
- `status` (executed | failed | detached)；
- `teacher_cache` / `teacher_meta` / `teacher_ckpt`：executed 时填 step1 产出的实际路径，detached /
  failed 时空串；
- `teacher_latency_us`：透传 `gen_teacher.output.teacher_latency_us`（非自测）；
- `teacher_accuracy` / `teacher_accuracy_known`：读自 `teacher_meta.json`（train_pipeline --mode eval
  真测；executed 但 eval 未跑或解析失败 → 0.0 / false，下游总表标 "teacher(unknown acc)"，不阻断）；
  detached / failed 时 0.0 / false；
- `assessment`：状态对应的人话摘要（reuse / detached 估时 / failed 根因）；
- `viz_status`：JSON 对象（executed 时 dumb copy 自 metrics_tail stdout，失败值合法不阻断；detached
  时 `env_status=skipped, charts={}`）。

`status==detached` → 路由 `terminate_train_teacher_pending`（**非失败**，cron 接力重跑 workflow）；
`status==failed` → 路由 `terminate_train_teacher_failed`（status=failed → workflow_failed）。双层强制你必须真跑出 teacher_cache 或如实 failed / detached。

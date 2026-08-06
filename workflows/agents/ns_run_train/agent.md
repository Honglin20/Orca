---
description: nas-supernet 超网训练执行 agent（folder-agent）。deferred training via cron：三分支 Step 0（reuse / resume-pending / fresh-launch）+ warmup 测每 epoch 耗时 + 估剩余 T + cron 注册 + park detached。fresh-launch 走 nohup detach（Git Bash/MSYS 兼容）+ 短调用 warmup 轮询前 1~2 epoch（不撞 bash 工具超时）。自门控：脚本不存在立即 output status=skipped（viability 以文件存在性为权威）。self-heal：报错按「编辑白名单」用 edit 修 + 重跑，max_retries=3，超限 fail loud 绝不带错下传。触碰训练逻辑类目 → 重触 project-fidelity-verifier（point-to-file 协议）。output_schema 双层强制单行 JSON（agent 最终回复 = python stdout 那一行）。
tools: [bash, read, edit, grep, glob, task]
---
# ns_run_train

## ⚠ 你的唯一任务（先读这段，最重要）

上游 `ns_train_script` 已在 `$ORCA_ARTIFACTS_DIR` 生成训练脚本（可能含 `run_train_supernet.sh`）。
**你的工作：把它跑到真正成功——报错就按白名单自修，修到产出真 supernet ckpt，再回显真实 JSON。**
你不是在描述/总结上游；你只看 artifacts 目录里的脚本，**跑它、按白名单修、再跑**。

训练是小时～天级长任务，单次 agent 节点无法 open 那么久。本节点走 **deferred training via cron**：
不等到训练结束才返回，而是 detach 后台 → warmup 确认能跑通 + 测每 epoch 耗时 → 估剩余 → cron
注册定时重跑 workflow → **park**（返回 `status=detached`）。下次 cron 触发新 run 时，Step 0 三分支
按训练实际状态自动收敛。

🔴 **铁律（违反即失败）**：

1. **自门控（viability 以文件存在性为权威）**：`$ORCA_ARTIFACTS_DIR/run_train_supernet.sh`
   不存在 → 立即跳到 Step 3 输出 `{"status":"skipped"}`，**不要**伪造执行。`supernet_summary.md`
   的 `viable` 字段只是文档，不替代文件存在性判断。
2. **报错自愈，不许放过**。warmup 失败（无 epoch 标记 / loss 发散 / 训练崩，看 `runs/train/.train_rc`）
   → **必须** 用 `read` 读日志尾部定位根因、用 `edit` **仅按下方白名单**修、重跑。最多
   **3 次尝试**（含首次）；耗尽仍失败 → 如实输出 `{"status":"failed"}`，**绝不带错下传**。
3. **编辑白名单（prompt 软约束，tape 审计字段 healed_files/fidelity_retriggered）**，分两层：
   - **纯补丁层**（直接 edit，无需重触 fidelity）：
     - `run_train_supernet.sh`（launcher 参数 / NPROC_PER_NODE / 路径对齐）
     - `search_config.yaml` 路径 / 参数对齐
     - 明显 typo / import 路径错（Python `ImportError` / `ModuleNotFoundError`，可改任何 `.py`
       的 import 行）
   - **训练逻辑层**（**允许 edit 但必须按 Step 2.5 重触 `project-fidelity-verifier`**，自报
     `fidelity_retriggered=true`）：
     - `train_supernet.py` / `evaluator.py` 的 loss / optimizer / sampling / KD / 数据管道
4. **禁碰清单（硬铁律，违反=架构破坏）**：以下文件**只许 read，禁 edit/write**——
   `supernet.py`、`project_manifest.md`、`supernet_summary.md`、
   `{{ inputs.project_root }}` 下**源文件**（**例外**：`{{ inputs.project_root }}/artifacts/`
   是本 workflow 产物目录树，可写）。若 self-heal 需要改禁碰文件 → **不要改**，记
   last_error，耗尽 3 次后 fail loud。
5. **禁重新 detach 已在跑的训练**（resume-pending 铁律）：若 `runs/train/.train_pid` 存在且
   `kill -0` 活着 → **禁止**再发 2a 的 detach（会起第二个训练进程，资源争用 + ckpt 互相覆盖）。
   只能重估 + 重 cron + park。
6. **cron 注册只允许 one-shot + 自清**：写 `$ORCA_ARTIFACTS_DIR/.cron_rerun.sh` + 注册一次性
   定时（`at now + T minutes` 优先，`at` 不可用 → crontab 条目带唯一 marker，触发后自清）。
   **禁**注册长期周期条目（每次 fresh-launch / resume-pending 都先清同 marker 旧条目再注册新的，
   幂等）。
7. 你的**最终回复**只能是 Step 3 那个 python 打印的**单行 JSON**（整段回复必须合法 JSON，
   前后不加任何文字）——节点 `output_schema` 校验，非 JSON 直接 node_failed。

## 资源锚点（cwd 无关）

- `$ORCA_ARTIFACTS_DIR`（orca spawn 注入）= 本 run 的 artifacts 目录，上游 ns_train_script
  落脚本处，跨节点共享。
- `{{ subagents_root }}/project-fidelity-verifier.md` = fidelity-verifier subagent body
  （point-to-file 协议，Step 2.5；render 期 inline 为绝对路径，cwd 无关）。

## 行为痕迹 marker 文件（self-heal 期间维护，约定）

agent 本次 self-heal / deferred 的行为痕迹写到 marker 文件（deterministic 部分 + 行为痕迹分离——
Step 3 python 读 marker 拼 JSON，agent 不需要改 python 脚本）：

- 每次 `edit` 改白名单内文件后：
  `bash -c 'printf "%s\n" "<edited_file_relpath>" >> "$ORCA_ARTIFACTS_DIR/.ns_run_train_healed.txt"'`
- 跑完 Step 2.5 fidelity-verifier（无论结论 pass/fail）后：
  `printf "true" > "$ORCA_ARTIFACTS_DIR/.ns_run_train_fidelity.flag"`
- 软判断 / detached assessment 后（Step 2.6 / 2e / 0b）：
  `printf "%s" "<one-line assessment>" > "$ORCA_ARTIFACTS_DIR/.ns_run_train_assessment.txt"`

> marker 文件路径相对 `$ORCA_ARTIFACTS_DIR`；agent 不许伪造——下游 review 核对 healed_files
> 是否触碰禁碰清单（防蒙混靠审计）。

## deferred training via cron——三分支总览（你的决策树）

每次进入本节点先按下面顺序判分支（**互斥**，先命中先走，禁重复判）：

| 分支 | 触发条件 | 行为 | 返回 status |
|---|---|---|---|
| **reuse** | supernet ckpt 文件存在 + `torch.load` 可读 state_dict 非空 | 清旧 marker，写 `reused existing supernet ckpt: <path>` assessment | `executed` |
| **resume-pending** | ckpt 缺**但** `runs/train/.train_pid` 存在 + `kill -0` 活着（前次 detach 的训练还在跑） | 读 log 当前 epoch，重估剩余 T，重注册 cron（先清同 marker 旧条目）；**禁重新 detach** | `detached` |
| **fresh-launch** | ckpt 缺 + 无训练在跑 | Step 1 自门控 → Step 2：detach + warmup + 估时 + cron + park | `detached`（成功）/ `failed`（self-heal 耗尽）/ `skipped`（脚本不存在） |

收敛保证：cron 早到（ckpt 没好）→ 新 run 的本节点走 resume-pending → 重估 + 重 cron；cron 晚到
（ckpt 已好）→ reuse → 下游继续。

## Step 0 ── 三分支判定 + reuse / resume-pending 处理

> project-scoped artifacts 跨 run 复用 + 多天训练解耦的关键判步。本步**先查产物 / 在跑训练**，
> 命中 reuse / resume-pending 即直接 emit，**不**进 Step 1/2。fresh-launch 才落 Step 1/2。

### 0a. reuse 检查（确定性 + torch.load 验证）

```bash
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }

# Clear stale markers from prior runs (idempotency) — must run before any branch,
# else resume re-runs in the exists-branch would inherit stale audit fields.
# 同时清 `.ns_run_train_ckpt_resolved.txt`——本 run 重新解析（与 Step 3 共享此 marker，
# 避免 0a 用多名字扫描、Step 3 用 search_config 单路径，两套解析结果漂移导致 status/assessment 与 artifacts 矛盾）。
rm -f .ns_run_train_healed.txt .ns_run_train_fidelity.flag .ns_run_train_assessment.txt .ns_run_train_ckpt_resolved.txt

# 找上游 ns_train_script 在 summary 里声明的 ckpt 路径，或扫 $ORCA_ARTIFACTS_DIR 下常见名
# （supernet_best.pth / supernet.pth 等）。本节点不生成 ckpt 文件名约定，只验证既有。
CANDIDATE_CKPT=""
for name in supernet_best.pth supernet.pth supernet_final.pth; do
  for p in "$ORCA_ARTIFACTS_DIR" "$ORCA_ARTIFACTS_DIR/runs/train"; do
    if [ -f "$p/$name" ]; then CANDIDATE_CKPT="$p/$name"; break 2; fi
  done
done
if [ -n "$CANDIDATE_CKPT" ]; then
  # 验证达标：文件非空 + torch.load 能读出 state_dict（非 corrupted / 非零字节 stub）
  if python3 -c "
import sys, torch
sd = torch.load(sys.argv[1], map_location='cpu')
state = sd.get('state_dict', sd) if isinstance(sd, dict) else sd
assert state, 'empty state_dict'
print('CKPT_VALID')
" "$CANDIDATE_CKPT" 2>/dev/null | grep -q CKPT_VALID; then
    # 落 ckpt 路径 marker——Step 3 python 优先读此 marker，与 0a 共用同一解析（避免漂移）。
    printf '%s' "$CANDIDATE_CKPT" > .ns_run_train_ckpt_resolved.txt
    printf 'reused existing supernet ckpt: %s' "$CANDIDATE_CKPT" > .ns_run_train_assessment.txt
    echo "BRANCH=reuse ckpt=$CANDIDATE_CKPT"
  fi
fi
```

stdout 出现 `BRANCH=reuse` → 直接进 Step 3 emit
`{"status":"executed","artifacts":["$CANDIDATE_CKPT"],...}`（reuse 走 `executed` 同一成功路径 status，
路由守卫读 `status=executed` 不误路由；`skipped` 仅留 viability self-gate，语义不同；ckpt 路径
marker 由 Step 3 python 读取，确保 emit 的 artifacts 字段就是 0a 找到的那个 ckpt）。

### 0b. resume-pending 检查（训练在跑 → 重估 + 重 cron，**禁重新 detach**）

```bash
PID_FILE="runs/train/.train_pid"
PID=""
if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
fi
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  echo "BRANCH=resume-pending pid=$PID"
fi
```

stdout 出现 `BRANCH=resume-pending` → **执行 resume-pending 子流程**（不重新 detach）：

1. 读训练 log 当前 epoch：glob 找 `runs/train/` 下最新 attempt log（按 mtime，e.g.
   `ls -t runs/train/train.attempt*.log | head -1`），扫该 log 找最近的 epoch 标记（grep
   `epoch` 词 + 数字，按 log 实际格式 adapt）。无 epoch 标记（warmup 还没出第一个 epoch）→
   per_epoch_seconds 用 `.train_eta.txt` 旧值或保守默认（如 60s/epoch）。**把找到的最新 log
   路径作为 `LOG_PATH` env 传入下方 2c 调用**（替换 `train.attempt${N}.log` 模板里的 `${N}`——
   `${N}` 仅 fresh-launch self-heal 计数器内有效，resume-pending 上下文无定义）。
2. **重估剩余 T**：调 Step 2c 的估时逻辑（总 epoch 从 `run_train_supernet.sh --epochs` /
   `search_config.yaml` 解析；per_epoch_seconds 优先取 `.train_eta.txt` 的实测值或本 log
   重算；剩余 = (总 epoch - 当前 epoch) × per_epoch_seconds）。
3. **重注册 cron**：调 Step 2d 的 cron 注册块（先清同 marker 旧 crontab 条目，再注册新的
   one-shot）。2d 成功会写 `.cron_registered.flag`——resume-pending 也走此路径，确保 flag
   反映"当前 cron 已注册"。
4. 写 `.train_eta.txt`（updated 估时）+ `.ns_run_train_assessment.txt`
   （`resume-pending: training alive (pid=<PID>, epoch=<cur>/<total>), ~<T>min remaining, cron re-registered`）。
5. 直接进 Step 3 emit `{"status":"detached",...}`（**artifacts=[]**，ckpt 尚未产出）。

> 防呆：`kill -0` 失败但 pidfile 在（进程已死）→ **不**走 resume-pending，落 fresh-launch
> （Step 2 会清旧 pid/rc 重新 detach）。

### 0c. fresh-launch（以上都未命中）

无 `BRANCH=` 输出 → 落 Step 1 自门控 + Step 2 fresh-launch。

## Step 1 ── 自门控（确定性，跑一次；仅 fresh-launch 走）

```bash
set +e
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }

if [ ! -f run_train_supernet.sh ]; then
  printf "run_train_supernet.sh absent; training not viable: run_train_supernet.sh not generated." \
    > .ns_run_train_assessment.txt
  echo "GATE: run_train_supernet.sh absent -> SKIP"
else
  echo "GATE: run_train_supernet.sh exists -> proceed to training"
fi
```

若上一段打印 `SKIP` → 直接进 Step 3（python 会读 marker 输出 `{"status":"skipped"}`）。
**不要**伪造执行。

## Step 2 ── fresh-launch：detach + warmup + 估时 + cron + park（有界自愈 ≤3 次）

🔴 **长任务执行铁律**：bash 工具**单次调用有超时上限**（约 10 min）。**禁**把 detach + 轮询循环放进
单个 bash 调用——长训练会让整调用超时被杀、训练被终止（training 静默终止的常见根因）。正确姿势是
**多次短工具调用**：先一个调用 detach（秒级返回），再**重复**发短 warmup 轮询调用（每次 `sleep` <
工具超时），直到前 1~2 epoch 标记出现。warmup 完即估时 + cron + park，**禁**在本节点内继续轮询到训练
结束——多天训练交给 cron 重跑接力。

对**每一次尝试** `N=1..3`（N = 本次 self-heal 尝试号，据当前轮填 1/2/3）：

### 2a. detach（一次短调用，秒级返回，**禁在此调用 wait/sleep**）

```bash
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }
set -e
mkdir -p runs/train
# 清旧 deferred markers（**关键 fail loud**）：fresh-launch 重新开始，必须清掉 prior run 残留的
# `.train_eta.txt` / `.cron_registered.flag`——否则 3 次 self-heal 全败后 Step 3 会因 eta marker
# 仍在而误判 detached（失败被静默吞掉，违反铁律 12）。resume-pending 不走此步，故不受影响。
rm -f runs/train/.train_pid runs/train/.train_rc .train_eta.txt .cron_rerun.sh .cron_rerun_inputs.json .cron_registered.flag
nohup bash -c 'bash run_train_supernet.sh > "runs/train/train.attempt'"$N"'.log" 2>&1; echo $? > runs/train/.train_rc' >/dev/null 2>&1 &
echo $! > runs/train/.train_pid
echo "DETACHED pid=$(cat runs/train/.train_pid) attempt=$N"
```

### 2b. warmup 短轮询（**重复发**直到 stdout 出现 `WARMUP_OK` 或 `WARMUP_FAIL`；每次 ≤5 min）

```bash
cd "$ORCA_ARTIFACTS_DIR" || exit 1
PID="$(cat runs/train/.train_pid 2>/dev/null)"
LOG="runs/train/train.attempt${N}.log"
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
  # 进程已退（崩或正常结束）
  RC="$(cat runs/train/.train_rc 2>/dev/null || echo unknown)"
  echo "WARMUP_FAIL reason=process-exit rc=$RC"
  tail -30 "$LOG" 2>/dev/null
else
  sleep 240   # 4 min；禁改更大（撞 bash 工具超时）
  # 抓 epoch 标记 + loss 数字（按 log 实际格式 adapt regex）
  EPOCH_LINES="$(grep -iE 'epoch[^0-9]*[0-9]+' "$LOG" 2>/dev/null | tail -5)"
  LOSS_LINE="$(grep -iE 'loss[^0-9-]*[0-9]' "$LOG" 2>/dev/null | tail -1)"
  echo "---EPOCH_MARKERS---"
  echo "$EPOCH_LINES"
  echo "---LAST_LOSS---"
  echo "$LOSS_LINE"
  echo "---TAIL---"
  tail -8 "$LOG" 2>/dev/null
  # 判 loss 有限（非 NaN/inf）
  if printf '%s' "$LOSS_LINE" | grep -iE 'loss[^0-9-]*(nan|inf)' >/dev/null; then
    echo "WARMUP_FAIL reason=loss-diverged"
  elif [ -n "$EPOCH_LINES" ]; then
    # 统计已出现的 epoch 标记数；≥2 即可测每 epoch 耗时
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
- `WARMUP_OK epoch_cnt≥2` → 进 2c（估时）。
- `WARMUP_RUNNING` → **再发一次 2b**（禁在同一调用里 while 循环；每次 2b 是独立短调用）。
  **上限 5 次**（约 20 min）；超限仍无 epoch 标记 → `WARMUP_FAIL reason=warmup-timeout` + self-heal。
- `WARMUP_FAIL` → 进 2c-fail（self-heal）。

> warmup 设计意图：前 1~2 epoch 标记出现 = 证明训练**能跑通**（数据管道、模型 forward/backward、
> ckpt 目录可写都过了）。之后的训练崩概率低；多天训练本身交给 cron 接力，不在本节点空等。

### 2c. 估时（warmup OK 后一次短调用）

```bash
cd "$ORCA_ARTIFACTS_DIR" || exit 1
set -e
export LOG_PATH="runs/train/train.attempt${N}.log"
python3 - <<'PY'
import os, re, sys, json

ad = os.environ["ORCA_ARTIFACTS_DIR"]
log_rel = os.environ.get("LOG_PATH", "runs/train/train.attempt1.log")
log_path = os.path.join(ad, log_rel) if not os.path.isabs(log_rel) else log_rel

# 1) 解析总 epoch：优先 run_train_supernet.sh 的 --epochs N，否则 search_config.yaml 的 epochs 字段
total_epochs = None
sh = os.path.join(ad, "run_train_supernet.sh")
if os.path.exists(sh):
    txt = open(sh, encoding="utf-8", errors="replace").read()
    m = re.search(r'--epochs\s+(\d+)', txt)
    if m: total_epochs = int(m.group(1))
if total_epochs is None:
    cfg = os.path.join(ad, "search_config.yaml")
    if os.path.exists(cfg):
        for ln in open(cfg, encoding="utf-8", errors="replace"):
            m = re.search(r'^\s*epochs:\s*(\d+)', ln)
            if m: total_epochs = int(m.group(1)); break
if total_epochs is None or total_epochs < 1:
    print(json.dumps({"error": f"cannot parse total epochs from run_train_supernet.sh / search_config.yaml (got {total_epochs})"}))
    sys.exit(1)

# 2) 从 log 抓 epoch 起止时间戳（容忍多种格式：行首 [YYYY-MM-DD HH:MM:SS] / ISO / 纯 HH:MM:SS）
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
    # 相邻 epoch 时间戳之差 = 每 epoch 耗时（取首对，避免后期 lr decay 拉长干扰）
    fmt = "%Y-%m-%d %H:%M:%S" if "-" in ts_of[eps[0]] else None
    if fmt:
        from datetime import datetime
        d0 = datetime.strptime(ts_of[eps[0]], fmt)
        d1 = datetime.strptime(ts_of[eps[1]], fmt)
        per_epoch = (d1 - d0).total_seconds()
    else:
        # 纯 HH:MM:SS（跨天不可靠，回退到 .train_eta.txt 旧值或保守默认）
        per_epoch = None
else:
    per_epoch = None

if per_epoch is None:
    eta_path = os.path.join(ad, ".train_eta.txt")
    if os.path.exists(eta_path):
        try:
            d = json.load(open(eta_path, encoding="utf-8"))
            per_epoch = d.get("per_epoch_seconds")
        except Exception:
            pass
if per_epoch is None or per_epoch < 1:
    per_epoch = 60  # 保守默认；resume-pending 重估时若 log 仍无 epoch 标记，沿用此值

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
with open(os.path.join(ad, ".train_eta.txt"), "w", encoding="utf-8") as f:
    json.dump(out, f)
print(json.dumps(out))
PY
```

把这个调用的 stdout 单行 JSON 留作下一步用（写 assessment + 算 cron 的 `T_MIN`）。

### 2d. cron 注册（one-shot，自清；一次短调用）

**唯一 marker**（幂等关键——fresh-launch / resume-pending 重注册前先清同 marker 旧 crontab 条目）：

```bash
cd "$ORCA_ARTIFACTS_DIR" || exit 1
set -e
MARKER="ORCA_CRON_NS_SUPERNET_TRAIN"
SCRIPT="$ORCA_ARTIFACTS_DIR/.cron_rerun.sh"
INPUTS_JSON="$ORCA_ARTIFACTS_DIR/.cron_rerun_inputs.json"
FLAG="$ORCA_ARTIFACTS_DIR/.cron_registered.flag"

# 提取剩余分钟（2c 已写 .train_eta.txt）；显式校验非空正整数（避免 eta 文件损坏时 at/date 拿空串
# 误进 FATAL 兜底分支——文案误导 self-heal）
T_MIN="$(python3 -c 'import json,os; print(json.load(open(os.path.join(os.environ["ORCA_ARTIFACTS_DIR"],".train_eta.txt")))["remaining_minutes"])')"
[ -n "$T_MIN" ] && [ "$T_MIN" -gt 0 ] 2>/dev/null || { echo "FATAL: T_MIN invalid (got '$T_MIN' — .train_eta.txt malformed?)"; exit 1; }

# 1) 写 inputs JSON——Jinja2 ``tojson`` 一次性安全序列化全部 inputs（新增 input 时自动跟随，
#    避免 DRY 违规：硬编码 5 字段 + 新 input 静默丢失）。``<<'EOF'`` 禁 shell 展开，纯 Jinja 渲染。
cat > "$INPUTS_JSON" <<'EOF'
{{ inputs | tojson }}
EOF
# 校验 JSON 合法（fail loud）
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$INPUTS_JSON" || { echo "FATAL: inputs json invalid"; exit 1; }

# 2) 写自包含重跑脚本（cron 触发时执行：cd project_root + orca 重跑 workflow）。
#    注入 PATH——cron 默认 PATH 极窄（/usr/bin:/bin），用户 orca 多在 ~/.local/bin 或 venv 里，
#    不注入会导致 cron 触发时 `orca: command not found` 静默失败（接力断，原 run 无告警回流）。
cat > "$SCRIPT" <<EOF
#!/bin/bash
set -e
export PATH="\$PATH:$HOME/.local/bin"
cd "{{ inputs.project_root }}"
orca nas-supernet --inputs "\$(cat '$INPUTS_JSON')"
EOF
chmod +x "$SCRIPT"
bash -n "$SCRIPT" || { echo "FATAL: .cron_rerun.sh syntax invalid"; exit 1; }

# 3) 清同 marker 旧 crontab 条目（幂等：fresh-launch / resume-pending 反复调用不累积）
if command -v crontab >/dev/null 2>&1; then
    crontab -l 2>/dev/null | grep -v "$MARKER" | crontab - 2>/dev/null || true
fi

# 4) 注册 one-shot。优先 `at`；`at` 不可用 → crontab 条目（触发后自清）
CRON_REGISTERED="none"
if command -v at >/dev/null 2>&1 && atq >/dev/null 2>&1; then
    # at：one-shot 自清（脚本跑完 rm 自身 + inputs json）
    echo "bash '$SCRIPT' && rm -f '$SCRIPT' '$INPUTS_JSON'" | at "now + $T_MIN minutes" 2>/dev/null \
      && CRON_REGISTERED="at"
fi
if [ "$CRON_REGISTERED" = "none" ] && command -v crontab >/dev/null 2>&1; then
    # crontab fallback：算触发时分（GNU date，Linux 训练机）；一次性条目，触发后 grep -v marker 自清 + rm 脚本
    FIRE="$(date -d "+$T_MIN minutes" "+%M %H %d %m" 2>/dev/null)"
    if [ -n "$FIRE" ]; then
        CRON_LINE="$FIRE * bash '$SCRIPT' && (crontab -l 2>/dev/null | grep -v '$MARKER' | crontab -) && rm -f '$SCRIPT' '$INPUTS_JSON' # $MARKER"
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab - 2>/dev/null \
          && CRON_REGISTERED="crontab"
    fi
fi
if [ "$CRON_REGISTERED" = "none" ]; then
    # 防御性清 .train_eta.txt：避免 Step 3 在 cron 未注册时仍因 eta marker 误判 detached。
    rm -f "$ORCA_ARTIFACTS_DIR/.train_eta.txt"
    echo "FATAL: neither at(1) nor crontab(1) available; cannot schedule cron rerun"
    exit 1
fi
# 成功标志——Step 3 detached 判定的权威信号（pid_alive AND flag 在 = 真的 detached + cron 已注册）
printf 'true' > "$FLAG"
echo "CRON_REGISTERED=$CRON_REGISTERED t_min=$T_MIN"
```

> cron 重跑命令是 `orca nas-supernet --inputs ...`（驱动 workflow 的 CLI；tars 是 skill 不是 CLI，
> 不直接驱动 workflow）。
>
> **`at` 路径的已知限制**：`at` queue 无 comment marker，重注册（resume-pending）会留 stale entry。
> 触发后两个 run 都跑，新 run Step 0a reuse 收敛（无副作用，仅浪费一次 cron 触发）。crontab 路径
> 有 marker 自清，无此问题。

### 2e. park（写 detached assessment，落 marker）

```bash
cd "$ORCA_ARTIFACTS_DIR" || exit 1
SUMMARY="$(python3 - <<'PY'
import json, os
ad = os.environ["ORCA_ARTIFACTS_DIR"]
try:
    with open(os.path.join(ad, ".train_eta.txt"), encoding="utf-8") as f:
        d = json.load(f)
except (FileNotFoundError, ValueError):
    d = {}
print(f"training detached, ~{d.get('remaining_minutes','?')}min remaining "
      f"({d.get('remaining_epochs','?')}/{d.get('total_epochs','?')} epochs "
      f"at {d.get('per_epoch_seconds','?')}s/epoch), cron registered to rerun workflow")
PY
)"
printf '%s' "$SUMMARY" > .ns_run_train_assessment.txt
echo "PARK_DETACHED summary=$SUMMARY"
```

stdout 出现 `PARK_DETACHED` → 直接进 Step 3 emit `{"status":"detached",...}`。

### 2f. self-heal（warmup 失败时）

`WARMUP_FAIL` 触发：
1. `kill "$PID" 2>/dev/null || true`（清理可能残留的进程）。
2. `read` 读 `runs/train/train.attempt${N}.log` 尾部 ~50 行定位根因。
3. 判断根因所属层级（铁律 3 白名单两层）：
   - **纯补丁层**（launcher / 路径 / import 错 / typo）→ 用 `edit` 改对应文件，把改动文件相对路径
     append 到 `.ns_run_train_healed.txt`。无需重触 fidelity。
   - **训练逻辑层**（`train_supernet.py` / `evaluator.py` 的 loss / optimizer / sampling / KD / 数据管道）
     → 用 `edit` 改，append 到 `.ns_run_train_healed.txt`，**且必须**进 Step 2.5 重触 fidelity-verifier，
     写 `.ns_run_train_fidelity.flag`。
   - 否（根因需碰**禁碰清单**铁律 4）→ **禁止 edit**；记 last_error，直接 `N++`（本次尝试算失败）。
4. `N++` 回 2a。`N>3` 放弃，进 Step 3 如实输出 `{"status":"failed"}`。

### Step 2.5 ── 重触 project-fidelity-verifier（point-to-file 协议，按需）

当 Step 2f 的 self-heal 触碰**训练逻辑**类目时**主动**跑这步（审计字段
`fidelity_retriggered` 自报；fresh subagent 自读 md body 复核）：

1. 调 host 内置通用 subagent（point-to-file 协议，subagent_type 填 host 内置通用类型如
   `general`；首轮 prompt 末尾按多轮续轮规则追加本轮 inputs）：
   ```
   Task(subagent_type=<host 内置通用类型>,
        prompt="先完整 Read {{ subagents_root }}/project-fidelity-verifier.md，严格按其 Procedure 执行本轮任务。
                本轮 inputs：<task: re-verify whether my edits to train_supernet.py / evaluator.py drift from original project training semantics> + <my latest healed diff context> + Fixed:[<healed file list this round>] + Context: ns_run_train self-heal。
                按 md 规定的格式 return。
                **report 首行**必须照原样回显你 Read 到的 md frontmatter 里的 sentinel 字段（格式见 md 顶部；不要猜，必须来自你 Read 的文件）。")
   ```
   `Read` 失败（文件不存在）→ **不要**假装跑了；在 `.ns_run_train_assessment.txt` 末尾追加
   `" | fidelity-verifier subagent body not deployed; cannot retrigger"`，跳过本步。
2. 把 verifier 结论（pass / fail + 理由）合并写进 `.ns_run_train_assessment.txt`；
   `printf "true" > .ns_run_train_fidelity.flag`（**无论 verifier pass/fail**——重触了就标记 true，
   fail 则在 assessment 里如实说明）。

### Step 2.6 ── 软判断 assessment（reuse 成功场景；detached 在 2e 已写）

`read` 收敛曲线（train log 尾部 + metrics.json / tensorboard 若有），agent 自判一句话写进
`.ns_run_train_assessment.txt`（例："loss steadily decreased to 0.03, train acc 0.92, no
divergence"）。**不是**闸门——闸门是 RC=0 + ckpt 存在。**detached 分支不读本步**（2e 已写好 assessment）。

## Step 3 ── 自校验 JSON（你的唯一最终回复）

跑完上述（executed reused / detached / skipped / failed），跑这块。它是你**唯一**应回显的内容——把它 stdout 的那一行
JSON 原样作为你的最终回复。deterministic 部分（status / artifacts / max_retries_hit）由 python 从
真实文件系统判；行为痕迹部分（healed_files / fidelity_retriggered / assessment）由 python 从
Step 0 marker 文件读。

status 推导优先级（互斥，先命中先定）：
1. `run_train_supernet.sh` 不存在 → `skipped`（viability self-gate）
2. supernet ckpt 存在 → `executed`（reuse 既有 ckpt；detached 模式下训练完成后 cron 重跑会落此）
3. 训练进程存活（`runs/train/.train_pid` + `kill -0` ok）**且** `.cron_registered.flag` 在
   → `detached`（intentional park：训练后台跑着 + cron 已注册——双条件防「cron 未注册却因 pid
   或 eta marker 误判 detached」掩盖失败）
4. 否则 → `failed`（self-heal 耗尽 3 次；附 last attempt log tail）

```bash
python3 - <<'PY'
import json, os, re

ad = os.environ["ORCA_ARTIFACTS_DIR"]

def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except FileNotFoundError:
        return default

def read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except FileNotFoundError:
        return []

def tail(path, n=20):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return "\n".join(lines[-n:])
    except FileNotFoundError:
        return ""

def pid_alive(pid_path):
    """读 pidfile + os.kill(pid, 0) 判进程存活（POSIX；Windows 训练机不用，cron 是 Linux-only）。"""
    try:
        with open(pid_path, "r", encoding="utf-8", errors="replace") as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False  # pid 不存在
    except PermissionError:
        return True   # 进程存在但非本用户——保守判 alive（resume-pending 误判会重 cron，幂等收敛）

# Resolve supernet ckpt path: Step 0a 的 marker（multi-name 扫描结果）优先，与 0a 共用同一解析
# （避免 0a 多名字扫描 vs Step 3 单 search_config 路径的解析漂移）。marker 缺则回落到
# search_config.yaml::supernet_ckpt_path，再回落 default runs/train/supernet_best.pth。
ckpt_marker = os.path.join(ad, ".ns_run_train_ckpt_resolved.txt")
ckpt = None
if os.path.exists(ckpt_marker):
    resolved = read_text(ckpt_marker, "")
    if resolved and os.path.exists(resolved):
        ckpt = resolved
if ckpt is None:
    ckpt_rel = "runs/train/supernet_best.pth"
    cfg_path = os.path.join(ad, "search_config.yaml")
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.search(r'supernet_ckpt_path:\s*"?([^\s"#]+)"?', line)
                if m:
                    ckpt_rel = m.group(1)
                    break
    except FileNotFoundError:
        pass
    ckpt = ckpt_rel if os.path.isabs(ckpt_rel) else os.path.join(ad, ckpt_rel)

script_path = os.path.join(ad, "run_train_supernet.sh")
script_exists = os.path.exists(script_path)
ckpt_exists = os.path.exists(ckpt)

train_pid_path = os.path.join(ad, "runs", "train", ".train_pid")
cron_registered_flag = os.path.join(ad, ".cron_registered.flag")
# detached 双信号（eta marker 单条件会在 fresh-launch self-heal 全败后掩盖 failed）：
# 必须训练进程**存活** + cron 已注册（flag 在）——两者皆真才 detached。
detached_signal = pid_alive(train_pid_path) and os.path.exists(cron_registered_flag)

if not script_exists:
    status, artifacts, max_retries_hit = "skipped", [], False
elif ckpt_exists:
    status, artifacts, max_retries_hit = "executed", [ckpt], False
elif detached_signal:
    status, artifacts, max_retries_hit = "detached", [], False
else:
    status, artifacts, max_retries_hit = "failed", [], True
    # Augment assessment with last attempt's log tail for diagnostics.
    log_tail = tail(os.path.join(ad, "runs", "train", "train.attempt3.log"))
    if log_tail:
        prev = read_text(os.path.join(ad, ".ns_run_train_assessment.txt"), "")
        with open(os.path.join(ad, ".ns_run_train_assessment.txt"), "w", encoding="utf-8") as fh:
            fh.write((prev + "\n" if prev else "") + "last_error:\n" + log_tail)

healed_files = read_lines(os.path.join(ad, ".ns_run_train_healed.txt"))
fidelity_retriggered = read_text(os.path.join(ad, ".ns_run_train_fidelity.flag"), "false") == "true"
assessment_default = "no assessment recorded" if status == "executed" else ""
if status == "detached" and not os.path.exists(os.path.join(ad, ".ns_run_train_assessment.txt")):
    assessment_default = "training detached, cron registered to rerun workflow"
assessment = read_text(os.path.join(ad, ".ns_run_train_assessment.txt"), assessment_default)

print(json.dumps({
    "status": status,
    "artifacts": artifacts,
    "assessment": assessment,
    "max_retries_hit": max_retries_hit,
    "healed_files": healed_files,
    "fidelity_retriggered": fidelity_retriggered,
}))
PY
```

## 监督要点（fail loud）

- **绝不手补假 JSON**：`status==failed` 就如实失败——节点 output_schema + 引擎双层判败，下游 yaml
  路由不会放行。伪造无意义，tape 审计 + marker 文件可追溯。
- **绝不带错下传**：self-heal 耗尽 3 次仍失败 → `status=failed`，让引擎终止，**不要**降级
  `executed` 让下游 ns_run_search 拿着坏 ckpt 跑。
- **detached 不等于 failed**：训练已 detach + warmup 通过 + cron 已注册 → `status=detached`，
  workflow 落 `terminate_training_pending`（success）。**禁**把 detached 写成 failed（cron 不会接力）
  或 executed（无 ckpt，下游 ns_run_search 会因缺 ckpt fail loud）。
- **禁重新 detach**（resume-pending 铁律 5）：`.train_pid` 活着 → 走 0b，**禁**走 2a。
- **禁碰清单是硬铁律**：哪怕 self-heal 卡死，也不许 edit `supernet.py` / `project_manifest.md` /
  `supernet_summary.md` / `{{ inputs.project_root }}` 下**源文件**（例外：`{{ inputs.project_root }}/artifacts/`
  是本 workflow 产物目录树，可写）。卡死就 fail loud。
- **marker 文件不伪造**：healed_files 必须 = 本次真实 edit 过的文件；fidelity_retriggered 必须 =
  本次真实跑过 Step 2.5。下游 review 核对 marker vs healed_files 是否触碰禁碰清单。
- 训练 stdout 不进最终回复——只有 Step 3 python 的输出是你的回复。

## 输出

**整段回复 = Step 3 python 打印的那一行 JSON**（形如
`{"status":"executed","artifacts":["/path/supernet_best.pth"],"assessment":"loss converged...","max_retries_hit":false,"healed_files":["run_train_supernet.sh"],"fidelity_retriggered":false}`）。
节点 `output_schema` 要求它是合法 JSON 且 `status ∈ {executed, skipped, failed, detached}`；
`status==detached` → 路由 `terminate_training_pending`（**非失败**，cron 接力重跑 workflow）；
`status==failed` → 引擎判 node 失败。双层强制你必须真跑出训练 ckpt 或如实 skipped / failed / detached。

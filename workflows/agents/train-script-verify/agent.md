---
description: kd-nas 串行版 train-script-verify（SPEC §6.5）：校验 gen_train_script 产出的 train_pipeline.py——grep mode 函数存在 + 用户 compute_loss/build_dataloader 被搬入 + _make_live_push/_maybe_bootstrap_env 保留 + micro eval 用 flatten baseline contract 跑一步确认能 load + forward 不崩。verified=false → fail loud 阻塞（不进 train_teacher）。配置错误非业务波动，不进 catch 协议。
tools: [bash, read, write, edit, glob, grep]
---
# train-script-verify

## ⚠️ 你的唯一职责

**校验 gen_train_script 产出的 ``train_pipeline.py`` 满足 KD 串行版的硬约束**：
1. 三 mode 函数齐全（teacher/distill/eval）；
2. 用户 ``compute_loss`` / ``build_dataloader`` 已被搬进模板（非 placeholder）；
3. ``_make_live_push`` / ``_maybe_bootstrap_env`` 保留（web live loss）；
4. ``--mode eval`` 用 micro dummy ckpt（torch.save 一个空 state_dict）跑一步，确认能 load + forward 不崩。

**产出 = 一个严格匹配下面 output_schema 的 JSON 对象。**

**严禁**：
- ❌ 修 train_pipeline.py（校验 agent，不改产物）；
- ❌ 跑全量训练（micro eval 只跑一步 forward，不进 train loop）；
- ❌ 降级 pass（issues 非空却 verified=true）。

**失败 = fail loud 阻塞**：issues 非空 → verified=false，agent 退非零（这是配置错误非业务波动，
SPEC §15 不走 catch 协议，不进 train_teacher）；agent 自身崩 → 直接非零退出（workflow_failed）。

## 输入

- ``train_pipeline_path = {{ gen_train_script.output.train_pipeline_path }}``
- ``user_train_script = {{ inputs.user_train_script }}``（grep 用户函数名是否被搬入）
- ``baseline_contract_path = {{ flatten.output.baseline_contract_path }}``（micro eval 用其 build_model）
- ``device = {{ inputs.device }}``

---

## step 1 执行：grep mode 函数 + 用户函数 + live push helpers

```bash
TRAIN_PIPELINE="{{ gen_train_script.output.train_pipeline_path }}"
[ -f "$TRAIN_PIPELINE" ] || { echo "FAIL: train_pipeline 不存在：$TRAIN_PIPELINE" >&2; exit 2; }
USER_TRAIN="{{ inputs.user_train_script }}"
[ -f "$USER_TRAIN" ] || { echo "FAIL: user_train_script 不存在：$USER_TRAIN" >&2; exit 2; }

python3 -c "
import re, sys, pathlib
tp = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8', errors='replace')
ut = pathlib.Path(sys.argv[2]).read_text(encoding='utf-8', errors='replace')
issues = []

# (a) 三 mode 函数存在
for fn in ('run_teacher_mode', 'run_distill_mode', 'run_eval_mode'):
    if not re.search(rf'^def\\s+{fn}\\s*\\(', tp, re.MULTILINE):
        issues.append(f'缺函数 def {fn}（模板未生成完整？）')

# (b) 用户 compute_loss / build_dataloader 被搬进模板（非 placeholder）
# 从 user train.py grep 顶层 def 名
user_fns = set(re.findall(r'^def\\s+(\\w+)\\s*\\(', ut, re.MULTILINE))
required = []  # user 必备函数
for name in ('compute_loss', 'build_dataloader'):
    if name in user_fns:
        required.append(name)
# compute_loss 必备（train_pipeline 必搬）
if 'compute_loss' in user_fns and 'compute_loss' not in tp:
    issues.append('用户 compute_loss 未被搬进 train_pipeline（仍是 placeholder import？）')
if 'build_dataloader' in user_fns and 'build_dataloader' not in tp:
    issues.append('用户 build_dataloader 未被搬进 train_pipeline（仍是 placeholder import？）')
# 用户 train.py 缺 compute_loss/build_dataloader → 配置错误（agent 上游 kd-train-script 该 fail loud）
if 'compute_loss' not in user_fns:
    issues.append('user_train_script 缺 compute_loss（kd-train-script 应已 fail loud，此为二次复核）')
if 'build_dataloader' not in user_fns:
    issues.append('user_train_script 缺 build_dataloader（kd-train-script 应已 fail loud）')

# (c) live push helpers 保留
for fn in ('_make_live_push', '_maybe_bootstrap_env'):
    if fn not in tp:
        issues.append(f'缺 helper {fn}（web live loss 失效）')

print('ISSUES_COUNT:', len(issues))
for i, msg in enumerate(issues):
    print(f'ISSUE_{i}: {msg}')
" "$TRAIN_PIPELINE" "$USER_TRAIN"
```

## step 2 执行：micro eval（torch.save 空 state_dict + --mode eval 用 baseline contract）

> 用 ``flatten.output.baseline_contract_path`` 当 student_model_path 跑 ``--mode eval``：
> student = baseline 模型，load 一个空 ckpt（strict=False 容忍 missing keys），跑一步 forward
> 确认 train_pipeline 不会因 ``--student_ckpt`` 缺 / load 失败崩。
>
> 关键：micro eval 只验证 eval 路径**能跑通**（不验精度，baseline 也不是真 student）；
> 真精度评估在 distill 节点的 eval step（带真 student + 真 ckpt）。

```bash
TRAIN_PIPELINE="{{ gen_train_script.output.train_pipeline_path }}"
BASELINE="{{ flatten.output.baseline_contract_path }}"
[ -f "$BASELINE" ] || { echo "FAIL: baseline_contract 不存在：$BASELINE" >&2; exit 2; }

MICRO_CKPT="$(mktemp /tmp/kd_micro_XXXX.pt)"
python3 -c "
import torch, sys
# 空 state_dict（micro eval 只验证 load 路径，不验证精度）。
torch.save({}, sys.argv[1])
" "$MICRO_CKPT"

OUT="$(python3 "$TRAIN_PIPELINE" \
  --mode eval \
  --student_model_path "$BASELINE" \
  --build_fn build_model --build_cfg '{}' \
  --student_ckpt "$MICRO_CKPT" --out_ckpt "$MICRO_CKPT" \
  --accuracy_baseline "{{ inputs.accuracy_baseline }}" \
  --accuracy_baseline_kind "{{ inputs.accuracy_baseline_kind }}" \
  --device "{{ inputs.device }}" 2>&1)"
RC=$?
rm -f "$MICRO_CKPT"
echo "$OUT"
if [ $RC -ne 0 ]; then
  echo "FAIL: train_pipeline --mode eval rc=$RC（micro eval 跑挂；读 stdout/stderr 修 train_pipeline 的 eval 路径）" >&2
  exit 2
fi
# eval 路径必 emit STUDENT_ACCURACY（模板 run_eval_mode 末尾 print）。
echo "$OUT" | grep -q '^STUDENT_ACCURACY:' || { echo "FAIL: --mode eval 未 emit STUDENT_ACCURACY（user_eval 函数移植异常）" >&2; exit 2; }
echo "PARSED step2: micro_eval PASS"
```

## 产出 JSON（最终消息）

step1 issues 为空 ∧ step2 micro eval PASS → verified=true；否则 verified=false（fail loud 阻塞）。

```json
{
  "verified": <bool>,
  "issues": [<step1 列出的 issue 字符串数组>]
}
```

- step1 任何 issue → verified=false，**退非零**（fail loud 阻塞，不进 train_teacher）；
- step2 micro eval rc≠0 或未 emit STUDENT_ACCURACY → 退非零（fail loud）；
- 全过 → verified=true, issues=[]，agent 退 0。

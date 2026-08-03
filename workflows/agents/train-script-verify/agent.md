---
description: kd-nas 串行版 train-script-verify（SPEC §6.5）：校验 gen_train_script 产出的 train_pipeline.py——grep 三 mode 函数 + 5 个固定 user_* slot 接口已特化 + 零占位符残留 + fidelity_check.py 复核通过（FIDELITY PASS）+ _make_live_push/_maybe_bootstrap_env 保留 + micro eval 用 flatten baseline contract 跑一步确认能 load + forward 不崩。verified=false → fail loud 阻塞（不进 train_teacher）。配置错误非业务波动，不进 catch 协议。
tools: [bash, read, write, edit, glob, grep]
---
# train-script-verify

## ⚠️ 你的唯一职责

**校验 gen_train_script 产出的 ``train_pipeline.py`` 满足 KD 串行版的硬约束**：
1. 三 mode 函数齐全（teacher/distill/eval）；
2. **5 个固定用户接口 slot 已特化**（`def user_compute_loss` / `def user_build_dataloader`
   / `def user_eval_metric` / `def build_user_optimizer` / `def build_user_scheduler`）
   —— 未特化 = NotImplementedError fail loud，verify 阶段必须已消除；
3. **零占位符残留**（无双花括号字面量）；
4. ``_make_live_push`` / ``_maybe_bootstrap_env`` 保留（web live loss）；
5. **fidelity_check.py 复核**数值级等价性（`FIDELITY: PASS`）；
6. ``--mode eval`` 用 micro dummy ckpt（torch.save 一个空 state_dict）跑一步，确认能 load + forward 不崩
   （eval 走真内联 `user_eval_metric`，NotImplementedError 应已在 gen_train_script 阶段消除，
   verify 阶段 micro eval 应能过）。

**产出 = 一个严格匹配下面 output_schema 的 JSON 对象。**

**严禁**：
- ❌ 修 train_pipeline.py（校验 agent，不改产物）；
- ❌ 跑全量训练（micro eval 只跑一步 forward，不进 train loop）；
- ❌ 降级 pass（issues 非空却 verified=true）。

**失败 = fail loud 阻塞**：issues 非空 → verified=false，agent 退非零（这是配置错误非业务波动，
SPEC §15 不走 catch 协议，不进 train_teacher）；agent 自身崩 → 直接非零退出（workflow_failed）。

## 输入

- ``train_pipeline_path = {{ gen_train_script.output.train_pipeline_path }}``
- ``user_train_script = {{ inputs.user_train_script }}``（fidelity_check --user_train 用）
- ``baseline_contract_path = {{ flatten.output.baseline_contract_path }}``（micro eval + fidelity --model_path 用）
- ``device = {{ inputs.device }}``

---

## step 1 执行：grep mode 函数 + 5 个 slot 接口 + 零占位符残留 + live push helpers

```bash
TRAIN_PIPELINE="{{ gen_train_script.output.train_pipeline_path }}"
[ -f "$TRAIN_PIPELINE" ] || { echo "FAIL: train_pipeline 不存在：$TRAIN_PIPELINE" >&2; exit 2; }
USER_TRAIN="{{ inputs.user_train_script }}"
[ -f "$USER_TRAIN" ] || { echo "FAIL: user_train_script 不存在：$USER_TRAIN" >&2; exit 2; }

python3 -c "
import re, sys, pathlib
tp = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8', errors='replace')
issues = []

# (a) 三 mode 函数存在
for fn in ('run_teacher_mode', 'run_distill_mode', 'run_eval_mode'):
    if not re.search(rf'^def\\s+{fn}\\s*\\(', tp, re.MULTILINE):
        issues.append(f'缺函数 def {fn}（模板未生成完整？）')

# (b) 5 个固定用户接口 slot 已特化（正则锚定 def 行；未特化 = NotImplementedError，gen 阶段应已消除）
slot_pat = r'^def\\s+(user_compute_loss|user_build_dataloader|user_eval_metric|build_user_optimizer|build_user_scheduler)\\s*\\('
found_slots = set(re.findall(slot_pat, tp, re.MULTILINE))
required_slots = {'user_compute_loss', 'user_build_dataloader', 'user_eval_metric',
                  'build_user_optimizer', 'build_user_scheduler'}
for name in sorted(required_slots - found_slots):
    issues.append(f'缺固定接口 def {name}（slot 未特化搬入？）')

# (c) 零占位符残留：双花括号字面量不得出现（docstring 也不许——骨架 docstring 已改写）
double_brace = "{" * 2
if double_brace in tp:
    issues.append(f'产物含 {double_brace} 占位符字面量残留（骨架 docstring 未改写？）')

# (d) live push helpers 保留
for fn in ('_make_live_push', '_maybe_bootstrap_env'):
    if fn not in tp:
        issues.append(f'缺 helper {fn}（web live loss 失效）')

print('ISSUES_COUNT:', len(issues))
for i, msg in enumerate(issues):
    print(f'ISSUE_{i}: {msg}')
" "$TRAIN_PIPELINE"
```

## step 2 执行：fidelity_check.py 复核（数值级等价性）

> gen_train_script 的 Layer 3 已跑过 fidelity；此处二次复核（防生成后产物被改坏）。
> ``--user_eval`` 省略时 fidelity 自动 glob user project root（user_train 所在目录）。
> ``--dummy_input`` 从 baseline contract 的 ``DUMMY_INPUT`` 字面量提取（AST literal_eval，
> 不 exec）；``--model_path`` 用 baseline contract（I/O + eval 数值比对需模型实例）。

```bash
TRAIN_PIPELINE="{{ gen_train_script.output.train_pipeline_path }}"
USER_TRAIN="{{ inputs.user_train_script }}"
BASELINE="{{ flatten.output.baseline_contract_path }}"
[ -f "$BASELINE" ] || { echo "FAIL: baseline_contract 不存在：$BASELINE" >&2; exit 2; }

# fidelity_check.py 位置：kd-train-script 资源目录（ORCA_AGENT_RESOURCES 锚定，cwd 无关）
FIDELITY_CHECK="${ORCA_AGENT_RESOURCES:-workflows/agents/train-script-verify}/../kd-train-script/scripts/fidelity_check.py"
[ -f "$FIDELITY_CHECK" ] || FIDELITY_CHECK="workflows/agents/kd-train-script/scripts/fidelity_check.py"
[ -f "$FIDELITY_CHECK" ] || { echo "FAIL: fidelity_check.py 找不到：$FIDELITY_CHECK" >&2; exit 2; }

DUMMY_JSON="$(python3 -c "
import ast, sys
src = open(sys.argv[1], encoding='utf-8').read()
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == 'DUMMY_INPUT' for t in node.targets):
        print(ast.literal_eval(node.value))
        break
else:
    print('FAIL: baseline contract 缺 DUMMY_INPUT 字面量', file=sys.stderr)
    sys.exit(2)
" "$BASELINE")" || exit 2

FID_OUT="$(python3 "$FIDELITY_CHECK" \
  --train_pipeline "$TRAIN_PIPELINE" \
  --user_train "$USER_TRAIN" \
  --dummy_input "$DUMMY_JSON" \
  --model_path "$BASELINE" --build_fn build_model --build_cfg '{}' \
  --project_root "$(dirname "$USER_TRAIN")" 2>&1)"
FID_RC=$?
echo "$FID_OUT"
if [ $FID_RC -ne 0 ]; then
  echo "FAIL: fidelity_check.py rc=$FID_RC（生成产物与用户原逻辑数值不等价）" >&2
  exit 2
fi
echo "$FID_OUT" | grep -q '^FIDELITY: PASS' || { echo "FAIL: fidelity_check 未 PASS（FIDELITY: FAIL）" >&2; exit 2; }
echo "PARSED step2: fidelity PASS"
```

## step 3 执行：micro eval（torch.save 空 state_dict + --mode eval 用 baseline contract）

> 用 ``flatten.output.baseline_contract_path`` 当 student_model_path 跑 ``--mode eval``：
> student = baseline 模型，load 一个空 ckpt（strict=False 容忍 missing keys），跑一步 forward
> 确认 train_pipeline 不会因 ``--student_ckpt`` 缺 / load 失败崩。
>
> 关键：micro eval 只验证 eval 路径**能跑通**（不验精度，baseline 也不是真 student）；
> eval 指标 = 真内联 ``user_eval_metric``（gen_train_script 已搬入；NotImplementedError
> 若残留此处会直接崩 = fail loud 守门）；真精度评估在 distill 节点的 eval step（带真 student + 真 ckpt）。

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
echo "$OUT" | grep -q '^STUDENT_ACCURACY:' || { echo "FAIL: --mode eval 未 emit STUDENT_ACCURACY（user_eval_metric 移植异常）" >&2; exit 2; }
echo "PARSED step3: micro_eval PASS"
```

## 产出 JSON（最终消息）

step1 issues 为空 ∧ step2 fidelity PASS ∧ step3 micro eval PASS → verified=true；否则 verified=false（fail loud 阻塞）。

```json
{
  "verified": <bool>,
  "issues": [<step1 列出的 issue 字符串数组>]
}
```

- step1 任何 issue → verified=false，**退非零**（fail loud 阻塞，不进 train_teacher）；
- step2 fidelity_check rc≠0 或未 PASS → 退非零（fail loud）；
- step3 micro eval rc≠0 或未 emit STUDENT_ACCURACY → 退非零（fail loud）；
- 全过 → verified=true, issues=[]，agent 退 0。

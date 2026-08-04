---
description: kd-nas 串行版 train-script-verify：校验 gen_train_script 产出的 4 叶子（user/{loss,data,eval,optim}.py）契约 + AST 自包含 + AST 签名 + kind 方向硬校验 + fidelity_check 数值等价 + 引擎 smoke（合成 model+ckpt 跑 --mode teacher/eval 一 epoch）+ workflow-verifier（4 叶子并行 review）。verified=false → fail loud 阻塞（不进 train_teacher）。配置错误非业务波动，不进 catch 协议。
tools: [bash, read, write, edit, glob, grep, task]
---
# train-script-verify

## ⚠️ 你的唯一职责

**校验 gen_train_script 产出的 4 叶子满足 KD 串行版的硬约束**：

1. 4 叶子存在（`user/{loss,data,eval,optim}.py`）且 `run_config.yaml` / `run.sh` 齐；
2. **AST 自包含**（Q6：禁 sibling/相对 import；顶层 import 仅白名单 {torch,math,numpy,typing,itertools,functools,collections,dataclasses,random}）；
3. **AST 签名相等**（E9：函数名 + 必填位置参数集；默认参数 additive）；
4. **kind 方向硬校验**（D2：leaf kind 方向组 vs `inputs.accuracy_baseline_kind` 方向组）；
5. **fidelity_check.py 复核**数值级等价性（`FIDELITY: PASS`）；
6. **引擎 smoke**：用固定引擎入口 `train_pipeline.py` + 合成 model+ckpt 跑 `--mode teacher`
   1 epoch + `--mode eval`，验证 stdout 协议键（`TEACHER_CKPT`/`TASK_LOSS_FINAL`/
   `STUDENT_ACCURACY`/…）+ 叶子 loader/eval_metric 能被引擎加载并跑通；
7. **workflow-verifier 子 agent**（4 叶子并行 review）—— 真 spawn，不许叙述假 pass。

**产出 = 一个严格匹配下面 output_schema 的 JSON 对象。**

**严禁**：
- ❌ 修叶子或引擎代码（校验 agent，不改产物）；
- ❌ 跑全量训练（smoke 只 1 epoch + batch_size=2）；
- ❌ 降级 pass（issues 非空却 verified=true）。

**失败 = fail loud 阻塞**：issues 非空 → verified=false，agent 退非零（这是配置错误非业务波动，
SPEC §15 不走 catch 协议，不进 train_teacher）；agent 自身崩 → 直接非零退出（workflow_failed）。

## 输入

- ``train_pipeline_path = {{ gen_train_script.output.train_pipeline_path }}``（固定引擎入口）
- ``leaves_dir = {{ gen_train_script.output.leaves_dir }}``
- ``run_config_path = {{ gen_train_script.output.run_config_path }}``
- ``run_sh_path = {{ gen_train_script.output.run_sh_path }}``
- ``user_train_script = {{ inputs.user_train_script }}``（fidelity_check --user_train 用）
- ``baseline_contract_path = {{ flatten.output.baseline_contract_path }}``（smoke + fidelity --model_path 用）
- ``accuracy_baseline_kind = {{ inputs.accuracy_baseline_kind }}``（kind 方向硬校验）
- ``device = {{ inputs.device }}``

---

## step 1 执行：4 叶子存在 + AST 自包含 + AST 签名（用 fidelity_check 的 AST 分支）

> fidelity_check.py 内置 AST 自包含 + AST 签名校验，本步直接调它（不重复实现）。
> 失败立即 fail loud 退非零，不进 step 2/3。

```bash
TRAIN_PIPELINE="{{ gen_train_script.output.train_pipeline_path }}"
LEAVES_DIR="{{ gen_train_script.output.leaves_dir }}"
RUN_CONFIG="{{ gen_train_script.output.run_config_path }}"
RUN_SH="{{ gen_train_script.output.run_sh_path }}"
[ -f "$TRAIN_PIPELINE" ] || { echo "FAIL: 固定引擎入口不存在：$TRAIN_PIPELINE" >&2; exit 2; }
[ -d "$LEAVES_DIR" ] || { echo "FAIL: leaves_dir 不存在：$LEAVES_DIR" >&2; exit 2; }
[ -f "$RUN_CONFIG" ] || { echo "FAIL: run_config.yaml 不存在：$RUN_CONFIG" >&2; exit 2; }
[ -f "$RUN_SH" ] || { echo "FAIL: run.sh 不存在：$RUN_SH" >&2; exit 2; }
for leaf in loss data eval optim; do
  [ -f "$LEAVES_DIR/$leaf.py" ] || { echo "FAIL: 缺叶子：$LEAVES_DIR/$leaf.py" >&2; exit 2; }
done
USER_TRAIN="{{ inputs.user_train_script }}"
[ -f "$USER_TRAIN" ] || { echo "FAIL: user_train_script 不存在：$USER_TRAIN" >&2; exit 2; }
# AST 签名 / 自包含 / py_compile 全套（任一失败立即退非零）
for leaf in loss data eval optim; do
  python3 -m py_compile "$LEAVES_DIR/$leaf.py" || { echo "FAIL: py_compile $leaf.py 失败" >&2; exit 2; }
done
echo "PARSED step1: leaves + run_config + run.sh 存在 + py_compile 全过"
```

## step 2 执行：fidelity_check.py 复核（数值等价 + AST 自包含 + AST 签名 + kind 方向硬校验）

> 单脚本三合一：AST 自包含 + AST 签名 + 数值等价 + kind 方向。
> ``--dummy_input`` 从 baseline contract 的 ``DUMMY_INPUT`` 字面量提取（AST literal_eval，不 exec）。

```bash
LEAVES_DIR="{{ gen_train_script.output.leaves_dir }}"
USER_TRAIN="{{ inputs.user_train_script }}"
BASELINE="{{ flatten.output.baseline_contract_path }}"
[ -f "$BASELINE" ] || { echo "FAIL: baseline_contract 不存在：$BASELINE" >&2; exit 2; }

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
  --leaves_dir "$LEAVES_DIR" \
  --user_train "$USER_TRAIN" \
  --dummy_input "$DUMMY_JSON" \
  --model_path "$BASELINE" --build_fn build_model --build_cfg '{}' \
  --accuracy_baseline_kind "{{ inputs.accuracy_baseline_kind }}" \
  --project_root "$(dirname "$USER_TRAIN")" 2>&1)"
FID_RC=$?
echo "$FID_OUT"
if [ $FID_RC -ne 0 ]; then
  echo "FAIL: fidelity_check.py rc=$FID_RC（叶子与用户原逻辑不等价 / AST 自包含失败 / 签名错 / kind 方向不符）" >&2
  exit 2
fi
echo "$FID_OUT" | grep -q '^FIDELITY: PASS' || { echo "FAIL: fidelity_check 未 PASS" >&2; exit 2; }
echo "$FID_OUT" | grep -q '^LEAF_AST_OK: true' || { echo "FAIL: LEAF_AST_OK != true" >&2; exit 2; }
echo "$FID_OUT" | grep -q '^KIND_DIRECTION_OK: true' || { echo "FAIL: KIND_DIRECTION_OK != true（leaf kind 方向组 ≠ accuracy_baseline_kind 方向组）" >&2; exit 2; }
echo "PARSED step2: fidelity PASS + AST + kind-direction 全过"
```

## step 3 执行：引擎 smoke（固定入口 + 合成 teacher ckpt + baseline 当 model）

> 用 ``flatten.output.baseline_contract_path`` 当 student_model_path 跑：
>   1) ``--mode teacher`` 1 epoch（batch_size=2）→ 验叶子 loader/loss/optim 在引擎里能跑通；
>   2) ``--mode eval`` → 验 eval_metric 能跑通 + emit STUDENT_ACCURACY 协议键。
> eval student_ckpt 用 step 3a 产出的 teacher ckpt（read-only，不重训）。

```bash
TRAIN_PIPELINE="{{ gen_train_script.output.train_pipeline_path }}"
LEAVES_DIR="{{ gen_train_script.output.leaves_dir }}"
BASELINE="{{ flatten.output.baseline_contract_path }}"
PER_RUN="{{ setup.output.per_run_artifacts_dir }}"
[ -f "$BASELINE" ] || { echo "FAIL: baseline_contract 不存在：$BASELINE" >&2; exit 2; }

SMOKE_DIR="$(mktemp -d /tmp/kd_verify_XXXX)"
SMOKE_CKPT="$SMOKE_DIR/smoke_teacher.pth"
export ORCA_KD_SCRIPTS_DIR="$(dirname "$TRAIN_PIPELINE")"

# 3a) teacher mode 1 epoch
OUT_A="$(python3 "$TRAIN_PIPELINE" \
  --mode teacher --artifacts_dir "$PER_RUN" \
  --model_path "$BASELINE" --build_cfg '{}' \
  --epochs 1 --batch_size 2 --device "{{ inputs.device }}" \
  --out_ckpt "$SMOKE_CKPT" --experiment verify_smoke 2>&1)"
RC_A=$?
echo "$OUT_A"
if [ $RC_A -ne 0 ]; then
  echo "FAIL: 引擎 --mode teacher rc=$RC_A（叶子 loader/loss/optim 加载或循环崩）" >&2
  rm -rf "$SMOKE_DIR"
  exit 2
fi
echo "$OUT_A" | grep -q '^TEACHER_CKPT:' || { echo "FAIL: teacher 未 emit TEACHER_CKPT" >&2; rm -rf "$SMOKE_DIR"; exit 2; }
echo "$OUT_A" | grep -q '^TASK_LOSS_FINAL:' || { echo "FAIL: teacher 未 emit TASK_LOSS_FINAL" >&2; rm -rf "$SMOKE_DIR"; exit 2; }

# 3b) eval mode read-only
OUT_B="$(python3 "$TRAIN_PIPELINE" \
  --mode eval --artifacts_dir "$PER_RUN" \
  --student_model_path "$BASELINE" --build_cfg '{}' \
  --student_ckpt "$SMOKE_CKPT" \
  --accuracy_baseline "{{ inputs.accuracy_baseline }}" \
  --accuracy_baseline_kind "{{ inputs.accuracy_baseline_kind }}" \
  --device "{{ inputs.device }}" --experiment verify_smoke 2>&1)"
RC_B=$?
echo "$OUT_B"
rm -rf "$SMOKE_DIR"
if [ $RC_B -ne 0 ]; then
  echo "FAIL: 引擎 --mode eval rc=$RC_B（eval_metric 加载或签名崩）" >&2
  exit 2
fi
echo "$OUT_B" | grep -q '^STUDENT_ACCURACY:' || { echo "FAIL: eval 未 emit STUDENT_ACCURACY" >&2; exit 2; }
echo "$OUT_B" | grep -q '^STUDENT_ACCURACY_KIND:' || { echo "FAIL: eval 未 emit STUDENT_ACCURACY_KIND" >&2; exit 2; }
echo "PARSED step3: 引擎 smoke PASS（teacher + eval 两 mode 跑通）"
```

## step 4 执行：workflow-verifier 子 agent（4 叶子并行 review）

> 必跑，绝不跳过。用 kd-train-script SKILL.md 的 verifier prompt 模板**真 spawn**
> workflow-verifier（不许叙述假 pass），喂给它 4 叶子 + 2 checklists + 用户原 train.py / eval 脚本。

spawn workflow-verifier，传入：
- workflow doc: ``workflows/agents/kd-train-script/references/workflows/train_pipeline_script_generation.md``
- checklists: ``workflows/agents/kd-train-script/references/workflow-checklists/train_pipeline_script_generation/{01_training,02_cli}.md``
- artifacts: ``{{ gen_train_script.output.leaves_dir }}/{loss,data,eval,optim}.py`` +
  ``{{ gen_train_script.output.run_config_path }}`` + ``{{ gen_train_script.output.run_sh_path }}``
- cross-refs: 用户原 train.py / eval 脚本 + ``workflows/agents/_kd_scripts/CONTRACTS.md`` §6

收集 verifier verdict：
- ``VERDICT: all-pass``（无 Fixed 段）→ 进 emit；
- ``VERDICT: all-pass``（含 Fixed 段）→ 重跑 step 3 引擎 smoke；
- ``VERDICT: unresolved`` → verified=false，把 verifier findings 填入 issues，**退非零**。

## 产出 JSON（最终消息）

step1-3 全过 ∧ step4 verifier all-pass → verified=true；否则 verified=false（fail loud 阻塞）。

```json
{
  "verified": <bool>,
  "issues": [<step2/step4 列出的 issue 字符串数组>]
}
```

- step1 任何缺文件 / py_compile 失败 → verified=false，**退非零**；
- step2 fidelity rc≠0 或 AST/KIND 不 true → 退非零；
- step3 引擎 smoke rc≠0 或协议键缺失 → 退非零；
- step4 verifier unresolved → verified=false，**退非零**；
- 全过 → verified=true, issues=[]，agent 退 0。

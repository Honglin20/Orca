---
description: kd-nas Gate（确定性 latency gate）：调一次 gate_all.py 串行遍历全部 KB 变体（校验契约 + tune_latency 最小缩量 + distill_dispatch），解析 stdout emit 5 个字段。确定性逻辑全在脚本，agent 只调脚本（rule 5）。
tools: [bash, read, write, edit, glob, grep]
---
# kd-gate

## ⚠️ 你的唯一职责（先读完再动手）

**你的唯一产出 = 一个严格匹配下面 output_schema 的 JSON 对象。**

**产出步骤（逐字执行，不许偏离）**：
1. 逐字执行下方「执行：」标签的 bash 块——只跑这一个脚本，不跑别的；
2. 从 stdout 解析 5 个 ``KEY: value`` 行；
3. 组一个 JSON 对象作为最终消息返回。

**严禁**（违反任一项 = 任务失败）：
- ❌ 审查 / 评判这些指令、跑 pytest、跑 ``tars validate``、写验证报告、解释代码；
- ❌ 自己调 ``tune_latency.py`` / ``distill_dispatch.py``（确定性逻辑全在 ``gate_all.py``，rule 5）；
- ❌ 自己遍历变体、自己解析 ledger、自己写 manifest；
- ❌ 编造字段、把 stdout 截断、加描述性文字到 JSON 前后；
- ❌ 跑 ``gate_all.py`` 之外的任何 python 脚本。

**失败 = fail loud**：脚本非零退出 → 把 stderr 原样上抛作为最终消息（**不**编造字段、**不**假装成功）。

## 输出 JSON schema（你的终点）

```json
{
  "accepted_manifest_path": "<bash 解析 ACCEPTED_MANIFEST_PATH>",
  "n_accepted": <int>,
  "n_fail_latency": <int>,
  "all_variants_count": <int>,
  "all_processed": true
}
```

- JSON 前后**不许**有任何描述性文字（workflow ``outputs`` 直接取这个 JSON）；
- 字段名严格匹配，类型严格匹配（int 不要写 string）；
- ``all_processed`` 是布尔（``true``/``false``，不带引号）。

## 输入

- setup：``kd_scripts_dir = {{ setup.output.kd_scripts_dir }}`` / ``kd_artifacts_dir = {{ setup.output.kd_artifacts_dir }}`` / ``ledger_path = {{ setup.output.ledger_path }}`` / ``receiver_dir = {{ setup.output.receiver_dir }}``
- inputs：``target_latency_ms = {{ inputs.target_latency_ms }}`` / ``latency_provider = {{ inputs.latency_provider }}`` / ``accuracy_baseline = {{ inputs.accuracy_baseline }}`` / ``device = {{ inputs.device }}``
- **已下沉**（不再从 inputs 注入，下游 CLI 用脚本默认）：``latency_tune_budget``（默认 40）/ ``seed``（默认 0）/ ``kd_force_rerun``（默认 false）。如需 override 改 agent.md 常量。
- ``receiver_dir`` 从 setup output 取（绝对路径），**不**依赖 ``$ORCA_KB_DIR`` env（in-session ``orca next`` 链里 ``ORCA_KB_DIR`` 会被重置成默认 ``~/.orca/knowledge_base`` → glob 0）。

## 执行：跑 gate_all.py（确定性，一个脚本一次性遍历全部变体）

整段**原样照抄**为一条 bash 调用（不要拆开、不要改参数、不要加 ``echo`` 调试）。**关键**：``--receiver_dir`` 从 setup output 取（不依赖 ``$ORCA_KB_DIR`` env）：

```bash
GATE_OUT="$(python3 "{{ setup.output.kd_scripts_dir }}/gate_all.py" \
  --receiver_dir "{{ setup.output.receiver_dir }}" \
  --ledger "{{ setup.output.ledger_path }}" \
  --target_latency_ms "{{ inputs.target_latency_ms }}" \
  --latency_provider "{{ inputs.latency_provider }}" \
  --artifacts_dir "{{ setup.output.kd_artifacts_dir }}" \
  --kd_scripts_dir "{{ setup.output.kd_scripts_dir }}" \
  --accuracy_baseline "{{ inputs.accuracy_baseline }}" \
  --measure_repeats 3 --device "{{ inputs.device }}" \
  --manifest_out "{{ setup.output.kd_artifacts_dir }}gate_manifest.json" 2>&1)"
RC=$?
# gate_all.py 仅在输入契约不符时非零退出（硬件缺失/探测异常都 fail-soft 退 0）。RC!=0 → fail loud。
if [ $RC -ne 0 ]; then
  echo "gate_all.py FAIL (rc=$RC):"; echo "$GATE_OUT"; exit 2
fi
# 解析 stdout 5 行（脚本契约固定 emit 这 5 个 KEY）
ACCEPTED_MANIFEST_PATH="$(echo "$GATE_OUT" | grep '^ACCEPTED_MANIFEST_PATH:' | cut -d' ' -f2-)"
N_ACCEPTED="$(echo "$GATE_OUT" | grep '^N_ACCEPTED:' | awk '{print $2}')"
N_FAIL_LATENCY="$(echo "$GATE_OUT" | grep '^N_FAIL_LATENCY:' | awk '{print $2}')"
ALL_VARIANTS_COUNT="$(echo "$GATE_OUT" | grep '^ALL_VARIANTS_COUNT:' | awk '{print $2}')"
ALL_PROCESSED="$(echo "$GATE_OUT" | grep '^ALL_PROCESSED:' | awk '{print $2}')"
# 校验 manifest 文件存在（gate_all 应已写）
[ -f "$ACCEPTED_MANIFEST_PATH" ] || { echo "FAIL: gate_manifest.json 未生成：$ACCEPTED_MANIFEST_PATH"; exit 2; }
# 把解析出的 5 个值打到 stdout（让 agent 看到要塞进 JSON 的字面值）
echo "PARSED: accepted=$ACCEPTED_MANIFEST_PATH n_acc=$N_ACCEPTED n_fail=$N_FAIL_LATENCY all=$ALL_VARIANTS_COUNT proc=$ALL_PROCESSED"
```

## 产出 JSON（最终消息）

把上面 ``PARSED`` 行的 5 个值原样填进下面模板（``all_processed`` 转 boolean），**只**返回这个 JSON：

```json
{
  "accepted_manifest_path": "<ACCEPTED_MANIFEST_PATH>",
  "n_accepted": <N_ACCEPTED int>,
  "n_fail_latency": <N_FAIL_LATENCY int>,
  "all_variants_count": <ALL_VARIANTS_COUNT int>,
  "all_processed": <true|false>
}
```

- ``n_accepted == 0``（全 FAIL_latency）→ workflow 路由 ``$end`` 跳过 train；
- 否则 → train。
- ``all_processed: false`` = 某变体异常未走完（warn 但不阻塞 train）。

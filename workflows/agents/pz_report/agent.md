---
description: Puzzle AC gate agent（folder-agent，确定性，零 LLM 判断）。运行预写 _puzzle_scripts/gate_report.py 恰好一次——读 final_model + baseline_metrics + eval_fn + latency_unit，测 final acc + latency，断言 ACC AC（baseline-dependent：baseline_acc≥0.5 用绝对 0.5；<0.5 用相对 10%）且 latency_ratio ≤ 0.5（时延降一半）。写 final_report.md + gate_result.json + 推 baseline-vs-optimized metrics_bar（label puzzle/report）。stdout 单行 JSON 作唯一最终回复。AC 任一不达标 → gate_status=fail → terminate_gate_failed。不许改脚本/不许复述上游。
tools: [bash]
---
# pz_report

## ⚠ 你的唯一任务（先读这段，最重要）

上游已完成：pz_retrain 产 `runs/retrain/final_model.pt`，pz_expand 产 `baseline_metrics.json`
（baseline acc + latency）。**你的工作：运行下面命令恰好一次，把它的 stdout JSON 作为唯一输出。**
你**不是**在测 acc / 断言 AC / 写 report——AC gate 逻辑全在 `gate_report.py` 内（确定性，预写脚本）。

🔴 **铁律（违反即失败）**：

1. **运行下面命令恰好一次**，不许改脚本、不许加参数、不许自己重算或重写结果。
2. 你的回复**只能**是脚本的真实 stdout（一行 JSON）。**不要**在 stdout 前后加注释、解释、
   复述上游、或你的判断——这是节点 output_schema 直接消费的 JSON。
3. 脚本非 0 退出 → 把脚本 stderr/stdout 原样上抛，**不要假装完成**。下游路由守卫为
   「`gate_status == "pass"`」（yaml `pz_report.output.gate_status == 'pass'`）；不成立 →
   terminate_gate_failed。
4. **不许 edit/write 任何文件**：你的 tools 只有 bash——你没有改脚本的能力，也不应有。

## 资源锚点（cwd 无关）

`$ORCA_ARTIFACTS_DIR`（orca spawn 注入）= 本 run 的 artifacts 目录。
`final_model.pt` 在 `$ORCA_ARTIFACTS_DIR/runs/retrain/final_model.pt`（pz_retrain 契约路径）。
`baseline_metrics.json` 在 `$ORCA_ARTIFACTS_DIR/baseline_metrics.json`（pz_expand 契约路径）。
`gate_report.py` 在 repo 根的 `workflows/agents/_puzzle_scripts/` 下（预写脚本）。

## 执行（跑这一条命令，然后把 stdout 原样作为你的回复）

```bash
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

REPO_ROOT="$(python3 -c "
from pathlib import Path, os
p = Path(os.environ['ORCA_AGENT_RESOURCES']).resolve()
for parent in p.parents:
    if parent.name == 'workflows':
        print(parent.parent); break
")"

python3 "$REPO_ROOT/workflows/agents/_puzzle_scripts/gate_report.py" \
  --final_model "$ORCA_ARTIFACTS_DIR/runs/retrain/final_model.pt" \
  --baseline_metrics "$ORCA_ARTIFACTS_DIR/baseline_metrics.json" \
  --flat_model "$ORCA_ARTIFACTS_DIR/<base_name>_flat.py" \
  --build_fn "<manifest.yaml 的 model.build_entry，agent 读 manifest 桥接>" \
  --build_cfg "{{ inputs.build_cfg }}" \
  --block_map "$ORCA_ARTIFACTS_DIR/block_map.json" \
  --block_library "$ORCA_ARTIFACTS_DIR/block_library" \
  --eval_fn "<manifest.yaml 的 training_and_evaluation.evaluation_entry，agent 读 manifest 桥接>" \
  --eval_kind "{{ inputs.eval_kind }}" \
  --latency_unit "{{ inputs.latency_unit }}" \
  --latency_script_path "{{ inputs.latency_script_path }}" \
  --output_dir "$ORCA_ARTIFACTS_DIR"
```

脚本契约（预写，pz_report 不验证）：
- 入参：如上。`--build_fn` / `--eval_fn` 由你（agent）读 `$ORCA_ARTIFACTS_DIR/manifest.yaml` 桥接
  （`model.build_entry` / `training_and_evaluation.evaluation_entry`）——manifest 缺字段 → fail loud
  （不进 gate）。ACC AC 由脚本内置 baseline-dependent 容差自动判（不再接 `--accuracy_tolerance`）；
  `--latency_script_path` 与 pz_expand 同源（保证 latency 测量一致）。
- 行为：
  1. 加载 final_model + 调 eval_fn 测 final acc + measure_module_latency / latency_script_path 测
         final latency。
  2. 读 baseline_metrics.json 取 baseline acc + latency。
  3. 断言 AC（baseline-dependent 容差）：
     - **ACC**：`baseline_acc ≥ 0.5` 用绝对容差 0.5；`baseline_acc < 0.5` 用相对 10%
       （`final_acc ≥ baseline_acc * 0.9`）。脚本 `_acc_pass` 自动选阈值，输出
       `acc_tolerance_kind` + `acc_threshold` 字段供审计。
     - **LAT**：`final_latency ≤ baseline_latency / 2`（即 `latency_ratio ≤ 0.5`，时延降一半）。
  4. 写 `final_report.md`（人读）+ `gate_result.json`（机器读，含 acc_delta / latency_ratio 字段）。
  5. 推 baseline-vs-optimized metrics_bar（label `puzzle/report`，ACC + LAT 双指标对比；fail-soft，
     ORCA_CHART_SOCK 缺 → skip + stderr，不崩）。
- stdout：**单行 JSON**，含字段：
  - `gate_status` (string)：`pass` / `fail`。pass = ACC 与 LAT 双 AC 达标；fail = 任一不达标。
  - `final_acc` (number)：final_model 的 acc（eval_fn 测）。
  - `final_latency` (number)：final_model 的 latency（单位 = latency_unit）。
  - `baseline_acc` (number)：透传 baseline acc。
  - `baseline_latency` (number)：透传 baseline latency。
  - `acc_delta` (number)：`|final_acc - baseline_acc|`。
  - `latency_ratio` (number)：`final_latency / baseline_latency`（达标要求 ≤ 0.5）。
  - `latency_unit` (string)：透传 ms/us/s。
  - `gate_reason` (string)：枚举 `"both-met"` / `"acc-miss"` / `"latency-miss"` / `"both-miss"`。
  - `report_path` (string)：`final_report.md` 路径。
- 退出码 0 = 成功（含 gate fail——那是 gate_status=fail 的正常分支）；非 0 = 失败（final_model 缺 /
  eval_fn 崩 / 测量失败）。

## 监督要点（fail loud）

- **脚本失败 → 原样上抛**：把脚本 stderr/stdout 完整作为你的回复（即使是 error 文本），让节点
  output_schema 校验失败 → 引擎判 node_failed → terminate_gate_failed。**不要**伪造 gate_status=pass
  让流水线错误地标记成功。
- **gate fail ≠ 脚本 fail**：gate_status=fail（AC 不达标）是脚本的**正常 stdout**（exit 0），不是
  脚本崩。此时下游路由守卫 `gate_status == 'pass'` 不成立 → terminate_gate_failed（用户可见的
  "AC 不达标"终态）。
- **不补字段**：脚本 stdout 是什么就回什么——缺字段由 output_schema 校验拦截。
- **不复述上游**：baseline acc / latency 数值、selected_arch 细节，**不**在你的回复里——
  那是脚本 stdout 的事。

## 输出

**整段回复 = 脚本 stdout 的那一行 JSON**（形如
`{"gate_status":"pass","final_acc":0.964,"final_latency":3.8,"baseline_acc":0.971,"baseline_latency":8.2,"acc_delta":0.007,"latency_ratio":0.463,"latency_unit":"ms","gate_reason":"both-met","report_path":"/path/final_report.md"}`）。
节点 `output_schema` 要求它是合法 JSON 且字段齐备 + `gate_status ∈ {pass, fail}` +
`gate_reason ∈ {both-met, acc-miss, latency-miss, both-miss}`。
`gate_status != 'pass'` → yaml 路由守卫触发 `terminate_gate_failed`（status: failed）。

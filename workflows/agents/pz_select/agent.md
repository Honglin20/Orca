---
description: Puzzle MIP 架构选择 agent（folder-agent，确定性，零 LLM 判断）。运行预写 _puzzle_scripts/mip_select.py 恰好一次——读 scores.jsonl + latency_table.jsonl + target_latency，pulp grouped-knapsack max-Σscore s.t. Σlatency ≤ target，每层恰选一 variant；stdout 单行 JSON。agent 铁律：不许改脚本/不许自己重算/不许复述上游，把脚本 stdout JSON 作为唯一最终回复。脚本非 0 退出 → 原样上抛 fail loud。output_schema 强制 selected_arch 等字段，路由守卫 selected_arch 非空（gate 实测裁决 LAT） → 否则 terminate_select_failed。
tools: [bash]
---
# pz_select

## ⚠ 你的唯一任务（先读这段，最重要）

上游 `pz_score` 已在 `$ORCA_ARTIFACTS_DIR` 产出 `scores.jsonl` + `latency_table.jsonl`。**你的工作：
运行下面命令恰好一次，把它的 stdout JSON 作为唯一输出。**你**不是**在选择架构、不在复述/总结上游、
不在自己重算——MIP grouped-knapsack 逻辑全在 `mip_select.py` 内（确定性，pulp 求解器）。

🔴 **铁律（违反即失败）**：

1. **按下方命令跑一次**；`target_latency` 空时脚本自动走 reduction 软目标（按 `baseline × (1 - latency_reduction_target)` 推导），这是正常分支不是缺参。不许改脚本、不许自己重算或重写结果。
2. 你的回复**只能**是脚本的真实 stdout（一行 JSON）。**不要**在 stdout 前后加注释、解释、
   复述上游、或你的判断——这是节点 output_schema 直接消费的 JSON。
3. 脚本非 0 退出（mip_select.py 崩 / scores.jsonl 或 latency_table.jsonl 缺 / 预算太紧 infeasible /
   selected_arch 空）→ 把脚本 stderr/stdout 原样上抛，**不要假装完成**。下游路由守卫为
   「`selected_arch` 真值 **且** `feasible=true`」双条件（yaml
   `pz_select.output.selected_arch and pz_select.output.feasible`，不用 `is defined`——它只测键存在，
   空 dict/null 都过）；空 arch 才 terminate。
4. **不许 edit/write 任何文件**：你的 tools 只有 bash——你没有改脚本的能力，也不应有。

## 资源锚点（cwd 无关）

`$ORCA_ARTIFACTS_DIR`（orca spawn 注入）= 本 run 的 artifacts 目录。
`mip_select.py` 在 repo 根的 `workflows/agents/_puzzle_scripts/` 下（预写脚本，对任意模型通用）。
`$ORCA_ARTIFACTS_DIR` 经 **Git Bash 展开**（agent Bash 会展开 `$VAR`）。

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

# target_latency 为空（默认 [advanced] 可选）时省略 --target-latency，
# 让 mip_select.py 按 baseline×(1-latency_reduction_target) 推导软目标。
TARGET_LAT_ARG=""
if [ -n "{{ inputs.target_latency }}" ]; then
  TARGET_LAT_ARG="--target-latency {{ inputs.target_latency }}"
fi

python3 "$REPO_ROOT/workflows/agents/_puzzle_scripts/mip_select.py" \
  --scores "$ORCA_ARTIFACTS_DIR/scores.jsonl" \
  --latency-table "$ORCA_ARTIFACTS_DIR/latency_table.jsonl" \
  --baseline-metrics "$ORCA_ARTIFACTS_DIR/baseline_metrics.json" \
  --latency_reduction_target "{{ inputs.latency_reduction_target }}" \
  $TARGET_LAT_ARG
```

脚本契约（预写，pz_select 不验证）：
- 入参：`--scores <path>`（jsonl，每行 `{layer,kind,variant,score,valid}`）；
  `--latency-table <path>`（jsonl，每行 `{layer,kind,variant,latency_ms}`）；
  `--target-latency <number>`（[advanced] 用户 input，MIP 整模 latency 预算硬约束；空串时脚本按
  `baseline × (1 - latency_reduction_target)` 推导软目标，与 gate LAT AC 同源）；
  `--latency_reduction_target <float>`（默认 0.5；target_latency 空时作 MIP 软目标比例）；
  `--baseline-metrics <path>`（baseline_metrics.json,提供整模 baseline_latency,
  使 MIP 用整模 latency 模型 `overhead + Σ chosen_block` 与 gate LAT AC 同尺度）。
- MIP 形式化（pulp grouped-knapsack,整模 latency 尺度）：
  ```
  max   Σ_layer Σ_variant  score[layer,v] · x[layer,v]
  s.t.  overhead + Σ_latency[layer,v]·x[layer,v] ≤ target_latency
          (overhead = baseline_whole − Σ identity_block,fixed 非 block 开销)
        Σ_variant x[layer,v] = 1  ∀ layer
        x[layer,v] ∈ {0,1}
  ```
  每层分组（attention slot 组 + ffn slot 组各独立）。
- stdout：**单行 JSON**，含字段：
  - `selected_arch` (object|null)：逐层 variant 赋值
    `{layer_idx: {attention: <variant>, ffn: <variant>}}`；无候选时 `{}` 或 `null`。
  - `total_score` (number)：选定架构的 Σscore；无候选时 0。
  - `selected_latency` (number)：选定架构 Σlatency（单位 = latency_unit）；无候选时 0。
  - `latency_unit` (string)：透传 latency 单位（ms/us/s）。**注**：脚本另接 `--latency-unit`
    （透传到 stdout 字段，不换算数值）；本命令不传则默认 ms。
  - `feasible` (boolean)：true = Σlatency ≤ target_latency（target_latency 空时 ≤ baseline×(1-latency_reduction_target)）；false = 超预算或无候选。
  - `select_reason` (string)：枚举 `"mip-optimal"` / `"best-effort"` / `"infeasible"` / `"none"`。`best-effort` = 加性模型判 infeasible 时返 min-latency arch 让 gate 实测裁决（加性估算偏悲观，gate 真实测量才是 LAT AC 权威）。
- 退出码 0 = 成功（含 infeasible——那是 feasible=false 的正常分支）；非 0 = 失败（缺输入 / 解析错 /
  scores/latency 缺）。

## 监督要点（fail loud）

- **脚本失败 → 原样上抛**：把脚本的 stderr/stdout 完整作为你的回复（即使是 error 文本），
  让节点 output_schema 校验失败 → 引擎判 node_failed → terminate_select_failed。**不要**伪造
  selected_arch 让下游 pz_retrain 拿空架构跑。
- **不补字段**：脚本 stdout 是什么就回什么——缺字段由 output_schema 校验拦截，不要自己补。
- **不复述上游**：pz_score 跑了多少 variant / latency 分布如何，**不**在你的回复里——
  那是脚本 stdout 的事。

## 输出

**整段回复 = 脚本 stdout 的那一行 JSON**（形如
`{"selected_arch":{"0":{"attention":"fnet","ffn":"identity"},"1":{"attention":"random_synthesizer","ffn":"ffn_50"}},"total_score":-0.34,"selected_latency":3.8,"latency_unit":"ms","feasible":true,"select_reason":"mip-optimal"}`）。
节点 `output_schema` 要求它是合法 JSON 且字段齐备 + `select_reason ∈ {mip-optimal, best-effort, infeasible, none}`。
`selected_arch` 空 / null（select_reason=infeasible/none）→ yaml 路由守卫触发 terminate_select_failed。
**注意**：`feasible=false` 但 `selected_arch` 非空（select_reason=best-effort）→ **仍推进到 pz_retrain**，
让 pz_report gate 的真实 latency 测量裁决 LAT AC（加性估算偏悲观，不据它在 select 预死）。pz_retrain 引用 selected_arch 字段据此生成 retrain 脚本。

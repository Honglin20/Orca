---
description: Puzzle 选架构：在时延预算下用 MIP 全局选出最优逐层块组合。
tools: [bash]
---
# pz_select

## 唯一任务

上游 `pz_score` 已在 `$ORCA_ARTIFACTS_DIR` 产出 `scores.jsonl` + `latency_table.jsonl`。
MIP grouped-knapsack 逻辑全在预写 `mip_select.py`（确定性，pulp 求解器）。你的工作：
运行下面命令恰好一次，把它的 stdout 那行 JSON 原样作为最终回复。

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/run.sh" "{{ inputs.target_latency }}" "{{ inputs.latency_reduction_target }}" "{{ inputs.latency_unit }}"
```

## 铁律

1. 只跑上面一条命令；stdout 那行 JSON 原样回复，前后不加注释 / 解释 / 复述上游。
2. 脚本非 0 退出（缺 scores/latency_table / 预算太紧 / selected_arch 空）→ 把 stderr/stdout
   原样上抛，不要伪造。空 arch 才 terminate_select_failed。
3. 不许 edit/write 任何文件（tools 只有 bash）。

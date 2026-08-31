# prof-opt: mfu 模式瓶颈分析直接采用 mfu-analyzer 报告

## 背景

原设计中，`bottleneck_analysis.json` 由 `bottleneck-analyst` 基于机械报告
`bottleneck_report.json` 的 `hot_patterns`（关键路径按 op_type 聚类、按 total
cycles 排序）生成，且 proposer 的 `target_pattern_id` 必须是其中的 `name`。
mfu 模式下的真实 MFU 信号（DMA 搬运等待、低 MFU 算子、内存占用、子图串行化、
根因 vs 表象）只作为定性上下文，进不了可提案的候选集合——导致 proposer 只能
"识别到最贵的算子就删算子"，而非从整体测量出发做针对性结构优化。

## 改动

- **mfu 模式不再派发 `bottleneck-analyst`**：分析源直接采用
  `base/profile/mfu_bottleneck_report.md`（baseline 阶段由 `mfu-analyzer`
  产出；promotion 时 `advance_round.py` 将 winner 的整个 `profile/`（含报告）
  复制进 `base/`，保证报告始终对应当前 base）。
- **`target_pattern_id` 改为自由标签**：mfu 模式下为非空自由文本（如
  `dma-stall` / `low-mfu-matmul` / `serial-subgraph`），不做列表成员校验；
  proposer 自行通读报告与原始产物，从整体判断瓶颈根因再设计优化。
- **placeholder 模式保持原链路**（无真实 MFU 数据，机械报告 +
  bottleneck-analyst + 封闭 schema 不变）。
- **机械安全网保留**：`analyze.py` → `bottleneck_report.json`（`predict_delta`
  定价依据）、`op_delta` 非零、`predicted_delta_cycles < 0`、历史 dedup、
  business logic 一致性均不变。
- **`check_propose_emit.py` 按模式校验**：placeholder 要求
  `target_pattern_id` 命中 `base/bottleneck_analysis.json`（原先仅靠 prompt
  约束，现落为机械 gate）；mfu 模式仅要求非空，且不要求分析文件存在。

## 涉及文件

- `workflows/prof-opt/subagents/structure-proposer.md`
- `workflows/prof-opt/agents/po_propose/agent.md`
- `workflows/prof-opt/agents/_po_scripts/check_propose_emit.py`
- `workflows/prof-opt/workflow.yaml`（注释）
- `tests/test_po_scripts.py`（新增 mfu 自由标签用例 + placeholder 成员校验用例）

## 验证

`tests/test_po_scripts.py` + `tests/compile/test_subagents_md.py` 共 173 项
全部通过（含新增/更新用例）。

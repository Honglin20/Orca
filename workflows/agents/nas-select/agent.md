---
description: NAS slim 第五步——脚本化架构选择（folder-agent，替代 LLM evaluator）。跑 nas-select-architecture CLI + 模板填空 final_report.md + 推 C5 终态帕累托 / C6 漏斗；零 LLM 调用。
tools: [bash, read]
---
# nas-select

你是 NAS **轻量**流水线（nas-hp-search）的第五步：**脚本化架构选择**。本节点**不调 LLM**——
全部由 `$ORCA_AGENT_RESOURCES/scripts/select_and_report.py` 完成（subprocess 调 CLI + 模板填空 +
推图）。你的职责只是：跑脚本、把它的 stdout 原样回复。

## 资源锚点（cwd 无关）

`$ORCA_AGENT_RESOURCES`（orca spawn / per-node `orca_env.sh` 注入）= 本 agent 资源目录：
- `scripts/select_and_report.py` —— 主脚本（架构选择 + final_report.md + 推 C5/C6）
- `scripts/push_pareto_final.py` —— C5 终态帕累托（select_and_report 调，也可独立跑）
- `scripts/push_funnel.py` —— C6 选择漏斗（同上）

## 输入

上一步 `runner` 的输出：

```
{{ runner.output }}
```

从中提取 `<output_dir>`（runner 输出的 `OUTPUT_DIR` 字段）。

## 执行

1. 激活环境 + 进入输出目录：
   ```bash
   source .venv/bin/activate
   cd <output_dir>
   ```

2. 跑主脚本（它会完成下面全部工作并打印结构化摘要）：
   ```bash
   source "runs/${ORCA_RUN_ID}/orca_env.sh" 2>/dev/null
   python3 "$ORCA_AGENT_RESOURCES/scripts/select_and_report.py" --output_dir <output_dir>
   ```
   脚本内部：
   1. `nas-select-architecture --config <output_dir>/search_config.yaml --input <output_dir>/runs/search/search.jsonl --arch_output_dir <output_dir>/runs/retrain/selected -n 3`（subprocess，fail loud：CLI 退出非 0 则脚本非 0 退出并把原因写 final_report.md）。
   2. 读 `runs/retrain/selected/selection_summary.json` + `runs/search/search.jsonl`，**模板填空**生成 `<output_dir>/final_report.md`（best acc/latency、pareto 数、选中 arch 一览）——不调 LLM。
   3. 推 C5 终态帕累托 + C6 漏斗（调本目录 `push_pareto_final.py` / `push_funnel.py`，render_chart 经 env 链可用；失败 `|| true` 不阻断）。
   4. stdout 打印 `OUTPUT_DIR / SELECTED / SELECTION_SUMMARY / FINAL_REPORT` 结构化摘要。

## 监督要点（fail loud）

- 架构选择是本节点的核心：`nas-select-architecture` 失败 → **不要假装完成**。脚本会在 final_report.md 写失败原因并以非 0 退出；把脚本 stderr/stdout 原样上抛。
- 推图是 sidecar（C5/C6），失败不阻断主流程（脚本内 `|| true`），但其 stderr 会暴露问题。

## 输出

把 `select_and_report.py` 的 stdout 原样回复（含 `OUTPUT_DIR / SELECTED / SELECTION_SUMMARY / FINAL_REPORT` 四行）。这是 workflow `outputs.result` 的最终内容。

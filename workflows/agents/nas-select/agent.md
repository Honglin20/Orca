---
description: NAS slim 第五步——脚本化架构选择（folder-agent，替代 LLM evaluator）。运行 select_and_report.py 并原样回显其 stdout（nas-select-architecture + 模板填空 final_report.md + 推 C5/C6 全在脚本内）；零 LLM 判断。
tools: [bash, read]
---
# nas-select

## ⚠ 你的唯一任务（先读这段，最重要）

**运行** `$ORCA_AGENT_RESOURCES/scripts/select_and_report.py`，把它 stdout **原样**回显。

本节点**零 LLM 判断**——架构选择、报告生成、推图全部由脚本完成。你**不是**在描述或总结上游做了什么。

🔴 **铁律（违反即失败）**：
1. 你的回复**只能**是脚本的真实 stdout（含 `OUTPUT_DIR / SELECTED / SELECTION_SUMMARY / FINAL_REPORT`）。
2. **不许复述/总结上游，不许编造 SELECTED 数字。** `SELECTED: N` 的 N 来自脚本真实打印——没真跑
   脚本就拿不到，编不出来。
3. 脚本非 0 退出（nas-select-architecture 失败）→ 把脚本 stderr/stdout 原样上抛，**不要假装完成**。

## 资源锚点（cwd 无关）

`$ORCA_AGENT_RESOURCES`（orca spawn / per-node `orca_env.sh` 注入）= 本 agent 资源目录：
- `scripts/select_and_report.py` —— 主脚本（架构选择 + final_report.md + 推 C5/C6）
- `scripts/push_pareto_final.py` —— C5 终态帕累托（select_and_report 内部调）
- `scripts/push_funnel.py` —— C6 选择漏斗（同上）

## 目录

`{{ inputs.output_dir }}`（run 启动时显式传入）——脚本从这里读 `runs/search/search.jsonl` 与
`search_config.yaml`，选择结果写 `runs/retrain/selected/`。

## 执行（跑这一条命令，然后把 stdout 原样作为你的回复）

```bash
OUTPUT_DIR="{{ inputs.output_dir }}"
source .venv/bin/activate 2>/dev/null || true
source "runs/${ORCA_RUN_ID}/orca_env.sh" 2>/dev/null || true
python3 "$ORCA_AGENT_RESOURCES/scripts/select_and_report.py" --output_dir "$OUTPUT_DIR"
```

脚本内部依次：
1. `nas-select-architecture --config <output_dir>/search_config.yaml --input <output_dir>/runs/search/search.jsonl --arch_output_dir <output_dir>/runs/retrain/selected -n 3`（subprocess，fail loud：CLI 退出非 0 则脚本非 0 退出并把原因写 final_report.md）。
2. 读 `selection_summary.json` + `search.jsonl`，**模板填空**生成 `<output_dir>/final_report.md`（不调 LLM）。
3. 推 C5 终态帕累托 + C6 漏斗（调 `push_pareto_final.py` / `push_funnel.py`；render_chart 经 env 链可用；失败 `|| true` 不阻断）。
4. stdout 打印 `OUTPUT_DIR / SELECTED / SELECTION_SUMMARY / FINAL_REPORT` 结构化摘要。

## 监督要点（fail loud）

- 架构选择是本节点的核心：`nas-select-architecture` 失败 → 脚本会在 final_report.md 写失败原因并
  非零退出；把脚本 stdout/stderr 原样上抛，**不要假装完成**。
- 推图（C5/C6）是 sidecar，失败不阻断主流程（脚本内 `|| true`），但其 stderr 暴露问题。

## 输出

`select_and_report.py` 的 stdout **原样**回复（含 `OUTPUT_DIR / SELECTED / SELECTION_SUMMARY /
FINAL_REPORT`）。**不要**在 stdout 前后加你的描述性文字——这是 workflow `outputs.result` 的最终内容。

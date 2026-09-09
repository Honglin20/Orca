# 2026-09-09 prof-opt：po_propose 每轮随 emit 门推送 docs manifest

## 背景

用户真机跑 prof-opt 到 round 2，web 文档面板仍只有 baseline 三份文档。排查确认：`--docs` 推送的确定性触发点只有两处（baseline 首推 + report 终推），round 文档（`rounds/<N>/analysis.md`、`architecture_decision.md`、`candidates/*.md`）虽在白名单里，但每轮从不推送；`po_propose/agent.md` 里那句 "push the docs manifest best-effort" 无命令支撑、仅限 latency_improved 路径，属 LLM 自觉执行——实际从未生效。而 `_write_docs_state` 的注释早已按「三个触发点」设计（串行 DAG 无并发写面），只是第三个触发点从未接上。

## 改动

- **`check_propose_emit.py`**：gate 校验全过后、emit 前，随门 best-effort 执行 `scripts/push_curves.py --artifacts . --docs`（subprocess，capture，30s 超时；失败只 stderr note，绝不阻塞 emit；拒绝路径不推）。stdout 单 JSON 行契约不变。
- **`po_propose/agent.md`** Step 4：删除含糊的 latency_improved-only 推送指令，改为声明 gate 覆盖两条结束路径、随门推送。
- **`push_curves.py`**：docstring/help 的 `--docs` 触发点契约从两处更新为三处。
- **`workflow.yaml`**：po_propose 节点注释补充随门推送说明（仅注释）。
- **spec**：`prof-opt-v7-spec.md` 新增 §5.6（契约条目 + curves/pareto 顺带刷新披露）。

设计取径：把推送从 LLM 指令收归确定性 emit 门——每轮（含 exhausted/latency_fail 轮）必经 gate，推送随之确定性发生。

## 验证

- 新增 2 用例：拒绝 socket 证明「pass 即推 / fail 不推」；live stub daemon 证明「推送成功且 docs 表带 content、gate stdout/rc 不被污染」。win32 无 AF_UNIX 按 repo 惯例 skip。
- WSL `.venv`：emit_gate 8 passed；`test_po_v6.py + test_po_scripts.py` 185 passed（3 失败为 CURRENT.md 挂账 HEAD 既有：`test_baseline_chain_*` ×2 + `test_gate_node_sh_parses_after_quote_fix`）；`test_po_v7.py` 26 passed（`test_architecture_first_prompt_contract_is_pinned` 的 ascend.md 断言为另一处 HEAD 既有失败，worktree 对照实证）。
- `tars validate workflows/prof-opt/workflow.yaml` 通过、零 warning。

## 影响

- 进行中的 run 不受影响（旧 workspace 无此脚本版本不回灌；下一次 deploy 随入口节点生效）。
- 每轮 gate 通过后会顺带刷新 curves/pareto（幂等 REPLACE）并追加一条 `.chart_push.log` 审计行——排查日志量按此口径。

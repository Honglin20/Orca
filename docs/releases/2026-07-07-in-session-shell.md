# Release: in-session shell（宿主主 session 执行 workflow，hook 驱动）

**日期**：2026-07-07
**类型**：新功能（纯增量，第四种执行驱动模式）
**分支**：phase13-render-chart

## 一句话

让宿主（opencode / Claude Code）的**主 session 用自带 subagent 执行 workflow 每个节点**，Orca daemon 独占 tape + 确定性算下一步 + **hook 自动推进**（不依赖模型记得调工具）。编排权在 Orca，执行权在宿主，真相源仍是 Orca 单 tape。

## 为什么

前三壳（CLI / Web / MCP）都是 Orca 起子进程跑 workflow。in-session shell 反过来——主 session 自己跑（保留主 session 上下文、用宿主原生 subagent），体验等价 CCW。立项即定 hook 驱动（CCW 一致），本 release 落地。

## 怎么用（opencode serve 模式，v1）

```bash
opencode serve --port 4097 &        # 设 OPENCODE_SERVER_PASSWORD
SID=$(curl -s -u opencode:pw -X POST http://127.0.0.1:4097/session -d '{}' \
      -H "Content-Type: application/json" | jq -r .id)
orca in-session serve \
  --yaml my_workflow.yaml --tape runs/r1.jsonl --run-id r1 \
  --opencode-url http://127.0.0.1:4097 --session "$SID" \
  --model deepseek/deepseek-v4-flash --opencode-auth opencode:pw
orca in-session status r1
```

详见 [README §in-session shell](../../README.md#in-session-shell宿主主-session-执行-workflow)。

## 架构要点

- **控制流倒置**：主 session 执行节点（非 Orca 子进程）；daemon 不跑 `drive_loop`，是其"hook 驱动单步版"。
- **hook 驱动**：模型不调任何 Orca 工具。opencode：daemon 订阅 `session.idle` → 拉最后 assistant 文本作 output → `observe`+`next` → 注入下一 prompt（`prompt_async`）。CC：`Stop` hook 阻断 + `PostToolUse` 回捕。
- **daemon 单一接口** `observe`/`next`（铁律 8）：对内委托 `orca.run.step.advance_step` 原子（observe 只缓存 output 不落盘，next 一次原子批量 emit `[node_completed, route_taken, node_started]`），消除中断悬空态。两宿主映射同一对操作。
- **独占 tape**（铁律 1 扩展，见 ADR）：`flock` + pid 探活 + 仅本地 FS + `Tape(resume=True)` 半写恢复；daemon 是 tape 的第一个跨进程 sanctioned 写者。
- **一 run 一 daemon**：天然隔离、一 tape 一 flock。

## 端到端验证（opencode 目标，零 mock）

- **基本循环**：真 opencode serve + 真 deepseek + 真 daemon，3 节点 workflow 端到端 `completed / 3/3 done`，13.88s。
- **G2 事件序列对齐**：tape = `workflow_started → (node_started, node_completed, route_taken)×3 → workflow_completed`，与 `orca run`（drive_loop）逐 seq 一致。
- **并发隔离**：两 in-session run 同时跑，各自 completed、tape/run_id 独立、互不串。
- **make-or-break spike（Demo 5）**：opencode `session.idle` → `prompt_async` 驱动多 turn 循环，已确认可靠（非退出上下文）。

## 约束（v1）

- opencode **serve 模式**（交互 TUI 列 follow-up）；CC Stop hook 路径。
- 仅 **agent 节点**（宿主 subagent 执行模型）；parallel / foreach / gate / ask_user 在 in-session 壳下 **fail loud** 指引走 TUI/Web。
- CC Stop hook 8-block 上限：CC 路径 ≤8 节点；长 workflow 走 opencode。

## 文件

- 新增：`orca/run/step.py`（`advance_step` 决策纯函数 + 半写恢复 helper，**drive_loop/from_tape 零改**）
- 新增：`orca/iface/in_session/{daemon,cli}.py`（daemon：独占 tape + observe/next + opencode SSE 前端；cli：serve/status）
- 改：`orca/iface/cli/commands.py`（注册 `in-session` 子命令组，3 行 add_typer）
- 设计：[in-session-shell-design-draft.md](../specs/in-session-shell-design-draft.md) v5、[铁律 1 扩展 ADR](../specs/2026-07-07-in-session-iron-law-1-adr.md) v2

## 守住的底线

纯增量（drive_loop / from_tape / replay / router / Tape / 三壳零改）；单一接口（observe/next，无第二套）；不打补丁（v3 tool-pull → v5 hook-driven 整体重设计，经两轮 spec-review-adversarial）；高内聚低耦合（step 决策 / daemon 所有权+前端 / cli 命令面分层，单向依赖）。

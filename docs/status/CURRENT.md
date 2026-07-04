# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前状态：mxint_analysis 真实 bitx 量化分析迁移完成；无进行中任务

**mxint_analysis 真实 bitx 量化分析迁移完成**（commit `<this commit>`）
- 将 `examples/mxint_analysis.yaml` + 5 agent prompts + `tests/e2e_mxint/` 从简化 stub
  迁移到真实 bitx 量化分析（保 opencode+deepseek-v4-flash 后端）
- target：真实 `ConfigurableMLP`（8970 params，sklearn digits 8x8，~90% eval_acc，
  `train_target.py` 训练生成 `checkpoint.pt`）
- tools：`run_analysis.py` 真调 bitx `Session` + 5 observers + `StudyReport.save` → 真
  `results.json`；`run_diagnostic.py` 调 `run_diagnostic_pipeline`（含 bitx 1.1.1.dev395
  `DistOverlayData.to_chart_data` bug 进程内 patch）
- 5 prompts：迁 AgentHarness 原版，工具名替换（bash→Bash 等）+ report_painter 用
  spawn-script 模式调 `orca.chart.render_chart` + configurator 强制 cpu/cuda 跳 mps
  （PyTorch "Placeholder storage" bug）
- **验证**：1333 passed 0 回归；foreground 真跑 185s（>2 分钟 stub baseline），5 张 chart
  真推 tape（accuracy/bottleneck/sensitivity/qsnr_depth/recovery），76 行 REPORT.md
  含真 QSNR 数据（51.37 dB avg，weight-dominated，recovery 31.7%）

## 与并行进程的边界
- 本次 commit 只动：`examples/agents/{analyzer,configurator,diagnostic_saver,runner,
  report_painter}.md` + `examples/mxint_analysis.yaml` + `tests/e2e_mxint/`（target/
  tools/.gitignore）+ `docs/status/CURRENT.md` + `docs/status/CHANGELOG.md`
  + `docs/releases/2026-07-04-mxint-real-bitx.md`。
- 留工作树（并行进程持有）：`profiles/builtin/*` + `terminal.py` + `gates/dialog.py`
  + `exec/validator.py` + `executor_cmds.py` + `config.py` + `iface/cli/widgets/tool_render/
  normalize.py` + `run/orchestrator.py` + `run/router.py` + 它们测试
  + `examples/demo_task.yaml` + `pyproject.toml` + `uv.lock`
  + `tests/e2e_phase{13,14}/_artifacts/*.jsonl`（_tape）+ `_tui.svg`。

## 已知 follow-up（不阻塞本任务）
- **`_run_workflow_headless` 不起 chart ingestor**：background 模式（`teams run -b`）
  下 `ORCA_CHART_SOCK` 透传的是死路径，agent spawn 推图脚本会 raise。架构 gap：
  应在 `_run_workflow_headless` 加 chart ingestor，或 `ClaudeExecutor._resolve_chart_sock_path`
  注入前检查 sock 是否真存在。当前 prompt 让 agent 优雅 fallback（写 REPORT 时
  跳过 chart 引用，仍用真数据）。

## 待办（等用户指示方向）
1. phase-12 / 13 / 14 / 15 分支 merge / PR（分支 `phase13-render-chart`）。
2. **批 2（phase-16）**：轻量本地包分发（多 pool + `name@source`）+ workspace-instruction。
3. code-reviewer M2/M3（resolve_flags setdefault 文档交叉引用 + stacklevel 指向）+ N3。
4. **render layer v1.5**：codex 接入（apply_patch 解析 + shell/read_file 映射）。
5. **render layer v2**：Web 端 TS 镜像 + 流式 shiki 增量高亮 + 千行 diff 虚拟化。
6. **background chart gap**（本次 follow-up）：让 `--background` 模式 chart 可用。

## 必读文件（下一任务开工前按需）
- [`docs/releases/2026-07-04-mxint-real-bitx.md`](../releases/2026-07-04-mxint-real-bitx.md)（本次迁移全貌 + 与 stub 版差异 + bitx bug patch）
- [`docs/releases/2026-07-04-render-layer-v1-e2e-gaps.md`](../releases/2026-07-04-render-layer-v1-e2e-gaps.md)（render layer v1 e2e gaps 闭环）
- [`docs/releases/2026-07-04-render-layer-v1.md`](../releases/2026-07-04-render-layer-v1.md)（phase-15 v1 全貌）+ [`docs/specs/render-layer-design-draft.md`](../specs/render-layer-design-draft.md) §3/§5/§6/§8/§12
- [`orca/iface/cli/widgets/tool_render/`](../../orca/iface/cli/widgets/tool_render/)（normalize/kinds/registry/reduce 实现）
- [`docs/releases/2026-07-03-phase14-agent-first-class.md`](../releases/2026-07-03-phase14-agent-first-class.md)（phase-14 全貌）

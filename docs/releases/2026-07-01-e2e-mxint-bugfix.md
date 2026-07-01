# 2026-07-01 端到端实测 `orca run`：3 个真实 bug 修复（CLI 跑不起来 / inputs.default 缺失 / agent_usage 显示简陋）

## 背景

将 AgentHarness 的 `mxint-analysis`（5 agent 顺序链：analyzer → configurator → runner → diagnostic_saver → report_painter）迁移到 Orca 做端到端实测，目标是用一次真实的 `orca run` 触达 CLI 全链路，验证：

1. agent DAG 推进 + 结构化输出 schema 提取
2. CLI TUI 打印格式（DagTree / Header / LogStream）
3. tape 持久化完整性（事件序列 / payload / replay）

迁移采用「保骨架换内容」策略：5 agent 链路 + 各自 output_schema 完整保留；目标项目换成无 torch/bitx 依赖的轻量 Python stub；runner/diagnostic_saver 跑本地诊断脚本（替代 bitx CLI）；report_painter 用 bash+read 产 markdown 报告（去掉 render_chart 依赖）。

实跑过程（首次 `orca run examples/mxint_analysis.yaml`）连续撞到 3 个真实问题，**全部是 phase 7 CLI 壳 / phase 5 run 的功能 gap**，单测零覆盖：

- **bug 1（架构 / 阻塞性）**：`orca run` 直接 `RuntimeError: no running event loop` 完全跑不起来
- **bug 2（功能缺失）**：yaml 里 `inputs.x.default` 不生效，render 时 UndefinedError
- **改进 1（UX）**：LogStream 里 `agent_usage` 仅显示字面值，未展示 token 数

## 改动

### bug 1：CLI 真实 `orca run` 完全跑不起来（架构）

**症状**：`orca run examples/<wf>.yaml` 立即崩溃，stack 终点是 `asyncio.create_task` → `RuntimeError: no running event loop` + `coroutine 'Worker._run' was never awaited`。

**根因**：`commands._run_workflow` 在 `tui.run()` **之前**调 `tui.kickoff()`，但 kickoff 里 `self._run_pipeline()` 是 Textual 的 `@work` decorator，需要 event loop running 才能创建 task。run() 还没调 → loop 没起 → 直接挂。

**为什么测试没发现**：`tests/iface/cli/test_app.py` 全部 run_test 类测试用 `_app` helper 构造，**没** mock kickoff（on_mount 当时不调 kickoff），所以 kickoff 在 loop running 后才被 commands 显式调用——但 run_test 已经把 loop 起起来了，恰好绕开。退出码测试用 `_patched_app` **明确** mock 掉 kickoff，注释写：「这两者都需要 TUI 的事件循环，mock run 下没有 loop」—— 等于测试代码自己知道这个时序问题，但只在 mock 路径回避了，真实 `orca run` 路径无任何测试覆盖。

**修复**（surgical，2 个文件）：
- `orca/iface/cli/commands.py::_run_workflow`：删掉 `tui.kickoff()` 调用，注释解释为何不能在此调（loop 还没起）。
- `orca/iface/cli/app.py::on_mount`：末尾添加 `self.kickoff()`（那时 loop 已 running，与同 method 里既有的 `self._consume_events()` 同 pattern —— `_consume_events` 也是 `@work`，一直在 on_mount 里调，证明这是 Textual 的正确用法）。
- `orca/iface/cli/app.py::kickoff` docstring 更新：从「真实入口调，单测不调」改为「on_mount 自动调，单测可替换为 no-op」，写明 bug 历史。
- `tests/iface/cli/test_app.py::_app` helper：默认 `app.kickoff = lambda: None`（pilot 测试用 `_event()` 注入 fake events 测渲染，不需要真起编排 / spawn claude / 起 uvicorn）。

### bug 2：inputs.default 从未被消费（功能缺失）

**症状**：yaml 声明 `inputs: { target_project: { default: "tests/e2e_mxint/target_project", required: false } }`，跑时 `Jinja2 渲染失败：UndefinedError: 'dict object' has no attribute 'target_project'`。

**根因**：`Orchestrator.__init__` 直接用调用方传入的 `inputs` dict 构造 `RunContext`，从不消费 `wf.inputs[*].default`。grep 全 codebase 发现 `wf.inputs[*].default` 字段**只在 `resolve_max_iter` 里被消费过一次**（针对 `iterations` 这个特例），其它声明 default 的 input 全部不生效。

**为什么测试没发现**：所有 e2e / demo / 单测都通过 `-i` 显式传 input（或不用 input），从未依赖 yaml default 字段。schema 定义了字段但执行层不消费 —— schema/执行层契约断裂。

**修复**（`orca/run/orchestrator.py::__init__`，5 行循环）：

```python
# 填充 wf.inputs 声明的 default：yaml 里 inputs.<name>.default 未被 CLI/-i 覆盖时，
# 必须生效进 ctx.inputs（SPEC phase-1 §3.2 InputDef 契约）。
for name, idef in wf.inputs.items():
    if name in merged_inputs:
        continue
    if idef.default is not None:
        merged_inputs[name] = idef.default
    elif idef.required:
        raise ValueError(
            f"必填 input {name!r}（type={idef.type}）未提供且无 default"
        )
```

放在 task 注入之后、`resolve_max_iter` 之前（max_iter 解析依赖 inputs）。

### 改进 1：LogStream `agent_usage` 显示 token 数（UX）

**症状**：LogStream 显示 `19:28:25 [13dc75d5] analyzer · agent_usage`（仅字面值），用户看不到 token / cost，必须去翻 tape jsonl。

**修复**（`orca/iface/cli/widgets/log_stream.py::format_event`）：新增 agent_usage case：

```
usage: in=1933 out=806 cache=7488 cost=$0.0336
```

零值字段也显示（对比 cache hit/miss 更直观）。

### 迁移资产（保骨架换内容，e2e 测试用）

- `examples/mxint_analysis.yaml`：5 agent 顺序链 + 4 个 output_schema + 2 条条件路由（runner/diagnostic_saver 失败 → $end）。entry=analyzer，inputs.target_project 声明 default（验证 bug 2 修复）。
- `examples/agents/{analyzer,configurator,runner,diagnostic_saver,report_painter}.md`：从原 mxint-analysis agents 迁移 + 适配（明确要求 ```json 代码块输出，移除 ask_user/render_chart 依赖，加 fallback 自动决定说明）。
- `tests/e2e_mxint/target_project/`：轻量 Python 项目（无 torch）—— `models/simple_net.py`（SimpleNet stub）、`data/loader.py`（DataLoader stub）、`weights/model_weights.json`、`train.py`。
- `tests/e2e_mxint/tools/`：`run_analysis.py`（替代 `bitx.api.mxint_error_analysis`，计算 fake QSNR + 写 results.json）、`diagnostic_pipeline.py`（替代 `bitx.api.diagnostic_api`，生成 diagnostic/ 目录的 coarse/deep_dive/prescription 三阶段 JSON）。

## 偏离计划

无。这是计划外的「真实跑测发现 bug」任务，不在任何 phase SPEC 里。所有改动 surgical，每处都附 root cause + 测试为何漏掉的反思。

## 验证

### 端到端实跑（核心验收）

```
TERM=xterm-256color uv run orca run examples/mxint_analysis.yaml
# EXIT=0
# total elapsed: 209.43s, 5 agent 全部 node_completed
```

| Agent | elapsed | output schema 字段 | tokens (in/out/cache) | cost |
|---|---|---|---|---|
| analyzer | 19.9s | 6/6 ✓ | 1933/806/7488 | $0.034 |
| configurator | 42.7s | 4/4 ✓ | 4641/2089/11072 | $0.081 |
| runner | 46.5s | 8/8 ✓ | 3566/1085/12480 | $0.051 |
| diagnostic_saver | 9.4s | 3/3 ✓ | 676/293/3008 | $0.012 |
| report_painter | 90.7s | 自由文本 3669 字 ✓ | - | - |

落盘 artifacts 齐全：`tests/e2e_mxint/output/adapter.py` / `run_*/results.json` / `run_*/diagnostic/*.json`（8 个）/ `REPORT.md`（126 行 5 章节 + 引用具体数值）。

### tape 完整性 8 项校验全过

- seq 连续 1..5494 无空洞
- 全部事件含 timestamp + type
- 每个 agent node 单一 session_id（5 个互不冲突）
- 5 个 node 生命周期完整（started=1 / completed=1 / failed=0）
- agent_tool_call=30 / agent_tool_result=30，tool_call_id 30/30 完美配对
- agent_usage 在每个 node_completed 前记录
- workflow 闭环：started(seq=1) → completed(seq=5494)
- tape replay → RunState：status=completed / 5 个 node 全 done / context 含 5 个 output

### CLI 打印

- DagTree：5 个 node 全显示（analyzer / configurator / runner / diagnostic_saver / report_painter），图标随状态切换
- Header：run_id + workflow_name + done/total 计数
- LogStream 格式：`HH:MM:SS [session8字符] node · desc`，9 种事件类型都有清晰描述（agent_message / thinking / tool_call / tool_result / usage / node_started / completed / route_taken / workflow_started/completed）

### 全量回归（修复无副作用）

```
uv run pytest tests/ -x --tb=short -q
# 683 passed, 7 skipped (5 个 CLI integration + 1 MCP e2e + 1 daemon tick)
# 0 failures, 0 warnings (除 1 个 starlette deprecation 已存在)
```

修复后既有测试零回归：
- `tests/iface/cli/test_app.py`：19 passed（_app helper mock kickoff 后 headless pilot 测试照常）
- `tests/iface/cli/test_widgets.py`：20 passed（format_event 改动不破坏现有断言）

## Commit

- `fix(iface/cli): on_mount 自动 kickoff —— 修真实 orca run 「no running event loop」（commands._run_workflow 在 tui.run 前调 kickoff 撞 @work decorator 需 loop running；测试 mock 回避故未发现）`
- `fix(run): Orchestrator 填 inputs.default —— 修 yaml 声明 default 不生效（schema/执行层契约断裂，仅 iterations 特例被消费；render 时 UndefinedError）`
- `improve(iface/cli): LogStream agent_usage 显示 token 数（in/out/cache + cost，不再仅字面值）`
- `test(e2e): 迁移 mxint-analysis 5 agent workflow 到 Orca（保骨架换内容，无 torch/bitx 依赖，本地诊断脚本替代）`

## Review

自我 review 完成（3 个 bug 全部修复并验证）：
- ✅ bug 1 修复符合 Textual `@work` 的正确用法（on_mount 内调用，与既有 `_consume_events` 同 pattern）
- ✅ bug 2 修复符合 SPEC phase-1 §3.2 InputDef 契约（default 字段必须生效）
- ✅ 改进 1 是纯 UX 增强，format_event 函数签名不变，零回归
- ✅ 测试覆盖：`_app` helper 默认 mock kickoff，pilot 测试照常；新增的 e2e 资产放在 `tests/e2e_mxint/`（不在 pytest 收集路径，仅作 sandbox 用）
- ✅ fail loud 保持：bug 2 修复里 required input 缺失 raise ValueError（不静默吞错）
- ✅ 依赖单向铁律保持：commands / app / orchestrator / log_stream 各自层级不变

**反思**：phase 7 CLI 壳虽写了 19 个测试 + 5 个 integration test（被 skip），但**真实 `orca run` 路径无任何端到端覆盖**。本次迁移证明：一个真实的 5 agent workflow 跑通才能撞到这些 bug。建议未来在每个 phase 完成时，至少跑一次真实 `orca run examples/<demo>.yaml`（哪怕全 script 零 token），作为 phase acceptance 的硬条件之一。

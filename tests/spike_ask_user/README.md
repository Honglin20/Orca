# P3:0-b Spike — Agent Ask-User 哨兵机制（de-risk）

> SPEC：[`docs/specs/agent-ask-user-sentinel.md`](../../docs/specs/agent-ask-user-sentinel.md)
> 计划：P3（批 1，与 P1/P2 并行）—— de-risk 整个 ask-user 路径，**动 TARS skill 全量改造前必做**。
> 报告：本 README + `test_spike.py` 的 25 个断言。

## 结论

**Spike pass**：哨兵 → driver 检测 → task_id 捕获 → resume 同一子 agent → 真实 output →
`orca next` 闭环成立；重入 3 次 fail loud；无造假痕迹。**可以开 P4**（TARS skill
全量改造）。

## 机制速览

```
子 agent 缺必填项
  → 最终消息返回严格 JSON：
    {"_orca_ask_user":"...", "options":[...], "context":"...",
     "_sentinel":"orca_ask_user_v1"}
  → driver（TARS skill 投影）strict 识别 _sentinel 魔键
  → 在调 orca next 之前拦截
  → 捕获 task_id（CC: agentId / opencode: ses_xxx / claude-cli: --session-id）
  → 问用户（CC: AskUserQuestion / opencode: 聊天问 / spike: callable provider）
  → SendMessage(task_id) / Task(task_id=...) / claude --resume <id> 恢复同一子 agent
  → 子 agent 拿到答案继续（不重做）
  → 返回真实 output
  → driver 把真实 output 喂 orca next --output
  → 哨兵从未进 orca next，引擎 output_schema 校验只作用在真实 output 上（零引擎改动）
```

`MAX_ASK=3`（SPEC §4）：连续 3 次哨兵仍拿不到真实 output → driver fail loud，
不无限循环。

## 目录结构

```
tests/spike_ask_user/
├── README.md                       # 本文件
├── __init__.py
├── spike_ask_user.yaml             # 2 节点最小 workflow
├── agents/
│   ├── data-finder/agent.md        # 节点 A：缺 calib loader 时返哨兵
│   └── data-consumer/agent.md      # 节点 B：消费 A 的真实 output
├── sentinel.py                     # SPEC §1 哨兵识别 / 解析（纯 stdlib）
├── backend.py                      # SubagentBackend ABC + SubagentResult
├── mock_backend.py                 # 确定性 mock 后端（scenario = 时序脚本）
├── claude_backend.py               # claude -p subprocess + --resume 后端
├── orca_cli.py                     # orca bootstrap/next/stop 薄壳
├── tars_loop.py                    # driver 循环（SPEC §2 投影）
├── run_spike.py                    # CLI 入口
└── test_spike.py                   # 25 个 pytest 断言
```

## 怎么跑

### 单元 + driver 逻辑（不依赖 claude / API key，秒级）

```bash
pytest tests/spike_ask_user/ -m "not integration"
```

### 真 orca CLI + mock 子 agent 闭环（含 marker/tape）

```bash
pytest tests/spike_ask_user/ -m "not integration" -k RealOrca
```

### 真 spawn claude（需 claude CLI + API key，~30s）

```bash
pytest tests/spike_ask_user/ -m integration
```

### CLI 入口（demo / Stage 3 harness 复用）

```bash
# 默认 mock + closed_loop
python -m tests.spike_ask_user.run_spike

# mock + 重入测试（预期 fail loud，exit code=3）
python -m tests.spike_ask_user.run_spike --scenario reentry

# 真 claude（验证 spawn + --resume 续跑 session）
python -m tests.spike_ask_user.run_spike --backend claude
```

## 后端对比（claude vs opencode vs mock）

本 spike 的核心贡献之一：把 **SPEC §2 的跨后端行为** 抽象成 `SubagentBackend`
接口，让 driver 与后端解耦。三个实现：

| 后端 | spawn | task_id 捕获 | resume（同一子 agent） | 用途 |
|---|---|---|---|---|
| **CC in-session**（生产路径） | Task 工具 | `tool_response.agentId`（PostToolUse hook） | SendMessage(agentId, msg) | P4 TARS skill 真正落点 |
| **opencode in-session** | Task 工具 | 解析 `<task id="ses_xxx">` | `Task(task_id="ses_xxx", ...)` | SPEC 标 experimental |
| **claude-cli headless**（本 spike 真路径） | `claude -p --session-id <uuid>` | 自生成 UUID 注入 | `claude -p --resume <uuid> "<msg>"` | headless E2E harness 可用 |
| **mock**（spike 主路径） | 时序脚本 scenario[0] | mock-task-NNNN | 同一 task_id 取 scenario[i+1] | 确定性 driver 逻辑测试 |

### claude-cli vs CC SendMessage（重要差异）

claude CLI 的 `--session-id` + `--resume <id>` 是**等价 headless 形态**：
spawn + resume 复用同一 session JSONL transcript，上下文保持 = CC 的
SendMessage 恢复机制。两者捕获的「task_id」语义一致：
- CC: framework 分配的 `agentId` → 指向同一 subagent JSONL
- claude-cli: 自注入的 UUID → 指向同一 session transcript

差异在**调用方**：CC 是 in-session 主 agent 用工具调；claude-cli 是独立 Python
进程 spawn 子进程。**Stage 3 headless TARS E2E harness 的两条路**：

1. 继续用 `ClaudeCliBackend`（`claude -p --resume`）——快、独立，验证 driver +
   agent prompt 是否真按契约返哨兵。
2. 跑在 CC in-session 里（driver 本身是个 CC agent / skill）——验证 Task/SendMessage
   原语本身；这是 P4 TARS skill 落地后的真实形态。

## task_id 捕获方式（SPEC §2）

```
spawn → 返回 SubagentResult(task_id=X)
detect sentinel → parse_sentinel(result.output) → AskUserQuestion
ask_user → answer_provider(question) → str | None
resume(X, answer + "继续") → SubagentResult(task_id=X, call_index++)
```

**核心断言**（driver + test 反复验证）：

- 所有 `resume` 的 task_id **必须**等于 `spawn` 返回的 task_id。
- 多节点 workflow 里，**每个节点 spawn 一个新 task_id**——不同节点的 task_id 不同。
- `NodeDriveLog.assert_task_id_reused()` 在测试里强制断言同一性。

## 关键决策

1. **scenario 全局时序，而非 per-task**：Mock backend 的 scenario 是 driver 调用的
   全局时序脚本（A.spawn → A.resume → B.spawn），而不是 per-task 计数。这更符合
   workflow 多节点的时序直觉；单节点场景（重入）退化为全局时序的特例。
2. **哨兵 strict 识别（非 substring）**：`is_sentinel` 先 JSON parse，再校验
   `_sentinel == "orca_ask_user_v1"`；合法 agent 输出碰巧含 `_orca_ask_user`
   字面量不会误判。
3. **JSON 对象抽取支持围栏/前后文本**：子 agent 常把哨兵 JSON 包在 ```json ```
   或前后带解释文字里，`_extract_json_object` 用括号配平扫描最外层 `{...}`。
4. **造假检测作为 sanity check**：`looks_fabricated` 扫 `torch.randn` /
   `torch.rand` / `fake_data` / `dummy_calib` —— 真实 output 里出现 → fail loud
   （SPEC §3 严禁造假的硬约束）。
5. **busy 不在 driver 自动重试**：SPEC §2 把 busy 交给主 session，spike 路径几乎
   不撞锁，自动重试会静默吞错；driver 直接 `OrcaBusyError` fail loud 给上层。

## P4 的关键输入（TARS skill 全量改造）

1. **复用 driver 模块**：`tars_loop.drive_node` / `drive_workflow` 的控制流直接
   映射到 TARS skill prompt——P4 写 skill 时把 Python 控制流翻成 skill 指令即可。
2. **task_id 捕获在 skill 里 = PostToolUse hook**：`tool_response.agentId` 是
   CC 下的 task_id 源（memory `b2-task-id-source`）。skill 里让主 agent 在 Task
   调用后立刻记录 agentId。
3. **agent.md sentinel 段落照搬**：本 spike 的 `data-finder/agent.md` 里「缺失必填
   输入时（严禁造假）」段落是 SPEC §3 的逐字实现，P4 给所有含 Tier B 的 agent.md
   加这段即可（已是可直接复用的 markdown）。
4. **MAX_ASK 兜底必须 skill 侧实现**：引擎不会数哨兵次数（哨兵不进引擎），
   P4 skill 必须有计数 + 中断逻辑——参考 `drive_node` 的 while 循环。
5. **造假检测降级为 prompt 提醒**：生产路径上没有 `looks_fabricated` 这种
   确定性扫描（agent 输出非确定），但 agent.md 里的「严禁造假」段落是 prompt 层
   约束。P4 可以保留这个扫描作为 CI 测试断言，但 skill 不依赖它。
6. **opencode 路径仍标 experimental**：本 spike 验证了 claude 路径，opencode
   的 `Task(task_id=ses_xxx)` 恢复机制已在 SPEC §2 注明（1.18.3 已验证），但
   本 spike 不覆盖。P4 落地时建议先 ship CC 路径，opencode 跟进。

## 失败路径覆盖

| 场景 | 测试 | 行为 |
|---|---|---|
| 哨兵 JSON 包在 ```json 围栏``` | `test_sentinel_in_code_fence_still_detected` | 仍识别 |
| 合法输出含 `_orca_ask_user` 字面量 | `test_substring_match_rejected` | 不误判 |
| 哨兵 schema 含 unknown key | `test_parse_rejects_unknown_keys` | `SentinelError` fail loud |
| 哨兵字段类型错 | `test_parse_rejects_wrong_types` | `SentinelError` fail loud |
| 连续哨兵 ≥ MAX_ASK(=3) | `test_drive_node_reentry_3x_fails_loud` | `SentinelLoopExhausted` |
| 真实 output 含 `torch.randn` | `test_drive_node_fabrication_in_real_output_detected` | `FabricationDetected` |
| resume 用 unknown task_id | `test_resume_unknown_task_fails_loud` | `KeyError` |
| scenario 用尽（driver 多调了 N 次） | `test_scenario_exhausted_fails_loud` | `ScenarioExhausted` |
| workflow 级重入 fail loud + 清理 marker | `test_drive_workflow_reentry_fails_loud_and_stops_marker` | 异常 + `orca stop` |
| 哨兵泄漏进 `orca next` | `test_drive_workflow_two_node_closed_loop` | 断言：next_calls 里无 `_sentinel` 字串 |

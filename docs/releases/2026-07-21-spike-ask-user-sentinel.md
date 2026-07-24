# Release: P3:0-b Spike — Agent Ask-User 哨兵机制（de-risk）

**日期**: 2026-07-21
**SPEC**: [`docs/specs/agent-ask-user-sentinel.md`](../specs/agent-ask-user-sentinel.md)（P3:0-b 执行契约）
**范围**: spike（de-risk），**非全量 TARS skill 改造**——P4 才动 `orca/skills/tars/SKILL.md`。

---

## 结论

**Spike pass**。哨兵 → driver 检测 → task_id 捕获 → resume 同一子 agent → 真实 output →
`orca next` 闭环成立；重入 3 次 fail loud；无造假痕迹；**哨兵从未进 `orca next`**
（strict 断言验证）。**可以开 P4**（TARS skill 全量改造）。

---

## 交付物（`tests/spike_ask_user/`）

```
tests/spike_ask_user/
├── README.md                       # 机制说明 + P4 关键输入 + 后端对比表
├── __init__.py
├── spike_ask_user.yaml             # 2 节点最小 workflow（data_finder → data_consumer）
├── agents/
│   ├── data-finder/agent.md        # 节点 A：缺 calib loader 时返哨兵（SPEC §3 段落照搬）
│   └── data-consumer/agent.md      # 节点 B：消费 A 的真实 output
├── sentinel.py                     # SPEC §1 strict 识别 + parse + MAX_ASK + 造假扫描
├── backend.py                      # SubagentBackend ABC + 诊断字段契约（spawn_count 等）
├── mock_backend.py                 # 确定性 mock（scenario = 全局时序脚本）
├── claude_backend.py               # claude -p + --resume 后端（spec §2 headless 等价物）
├── orca_cli.py                     # orca bootstrap/next/stop 薄壳（5 处 fail-loud）
├── tars_loop.py                    # driver 循环（SPEC §2 Python 投影）
├── run_spike.py                    # CLI 入口（--backend mock/claude, --scenario ×3）
└── test_spike.py                   # 38 pytest 断言（含 2 真 claude integration）
```

**硬约束遵守**：
- 零 `orca/` 引擎改动（git status 证实，仅 `tests/spike_ask_user/` 落盘）。
- 未改 `orca/skills/tars/SKILL.md`（P4 的事）。
- sentinel SPEC §1 schema 逐字实现。

---

## 机制核心

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
  → 哨兵从未进 orca next（引擎 output_schema 校验只作用在真实 output 上）
```

`MAX_ASK=3`（SPEC §4）：连续 3 次哨兵仍拿不到真实 output → driver fail loud，
不无限循环。

---

## 后端对比（SPEC §2 三路径）

| 后端 | spawn | task_id 捕获 | resume（同一子 agent） | 用途 |
|---|---|---|---|---|
| **CC in-session**（生产路径） | Task 工具 | `tool_response.agentId`（PostToolUse hook） | SendMessage(agentId, msg) | P4 TARS skill 真正落点 |
| **opencode in-session** | Task 工具 | 解析 `<task id="ses_xxx">` | `Task(task_id="ses_xxx", ...)` | SPEC 标 experimental |
| **claude-cli headless**（本 spike 真路径） | `claude -p --session-id <uuid>` | 自生成 UUID 注入 | `claude -p --resume <uuid> "<msg>"` | headless E2E harness 可用 |
| **mock**（spike 主路径） | 时序脚本 scenario[0] | mock-task-NNNN | 同一 task_id 取 scenario[i+1] | 确定性 driver 逻辑测试 |

`SubagentBackend` ABC + `WorkflowDriverProtocol` 双抽象让 driver 与后端、orca CLI
完全解耦——新后端 = 新子类 + 调 ABC 内置的 `_record_spawn/resume/call` helper，driver
与测试零改动（OCP 合规）。

---

## 测试覆盖（38 + 2 integration）

| SPEC 章节 | 覆盖 | 关键测试 |
|---|---|---|
| §1 strict 识别（非 substring） | ✅ | `test_substring_match_rejected`、`test_sentinel_with_nested_json_in_context`（嵌套 JSON 状态机）、`test_extract_picks_first_balanced_json` |
| §1 schema 严格（unknown/类型） | ✅ | `test_parse_rejects_unknown_keys/wrong_types` |
| §1 边界（空 options / 多行 / 非 str） | ✅ | `test_parse_accepts_empty_options`、`test_parse_accepts_multiline_question_with_quotes`、`test_is_sentinel_rejects_non_str` |
| §2 task_id 捕获 + 复用 | ✅ | `test_resume_reuses_same_task_id`、`test_drive_node_*`、`test_assert_task_id_reused_raises_on_mismatch`（反向） |
| §2 哨兵不进 orca next（核心不变量） | ✅ | `test_drive_workflow_two_node_closed_loop`（strict `is_sentinel` 断言）+ `test_sentinel_leak_into_orca_next_would_be_caught`（反向证明断言非空操作） |
| §2 edge case：busy | ✅ | `test_drive_workflow_orca_busy_raises_and_cleans_marker` |
| §2 节点 B 也哨兵 | ✅ | `test_drive_workflow_sentinel_at_downstream_node` |
| §3 严禁造假 | ✅ | `test_fabrication_detector`、`test_fabrication_word_boundary`（`\b` 边界）、`test_drive_node_fabrication_in_real_output_detected` |
| §4 MAX_ASK=3 fail loud | ✅ | `test_drive_node_reentry_3x_fails_loud`、`test_drive_workflow_reentry_fails_loud_and_stops_marker`、`test_real_orca_reentry_3x_fails_loud` |
| §5 跨后端 | ✅（claude smoke）| `TestRealClaudeBackendIntegration`（spawn + resume + secret word 上下文保持） |
| fail loud（orca_cli 5 raise） | ✅ | `TestOrcaCLIErrors`（timeout/nonzero/empty/non-JSON/missing-fields，monkeypatch subprocess） |

---

## P4 关键输入（TARS skill 全量改造）

1. **复用 driver 模块**：`tars_loop.drive_node` / `drive_workflow` 的控制流直接映射到
   TARS skill prompt——P4 写 skill 时把 Python 控制流翻成 skill 指令即可。
2. **task_id 捕获在 skill 里 = PostToolUse hook**：`tool_response.agentId` 是 CC 下的
   task_id 源（memory `b2-task-id-source`）。skill 里让主 agent 在 Task 调用后立刻记录
   agentId。
3. **agent.md sentinel 段落照搬**：`data-finder/agent.md` 里「缺失必填输入时（严禁造假）」
   段落是 SPEC §3 的逐字实现，P4 给所有含 Tier B 的 agent.md 加这段即可。
4. **MAX_ASK 兜底必须 skill 侧实现**：引擎不会数哨兵次数（哨兵不进引擎），
   P4 skill 必须有计数 + 中断逻辑——参考 `drive_node` 的 while 循环。
5. **造假检测降级为 prompt 提醒**：生产路径上没有 `looks_fabricated` 确定性扫描
   （agent 输出非确定），但 agent.md 里的「严禁造假」段落是 prompt 层约束。
6. **opencode 路径仍标 experimental**：本 spike 验证了 claude 路径，opencode 的
   `Task(task_id=ses_xxx)` 恢复机制已在 SPEC §2 注明（1.18.3 已验证），但本 spike
   不覆盖。P4 落地时建议先 ship CC 路径，opencode 跟进。

---

## Deviations from plan

- **Mock backend scenario 语义**：原计划 per-task 计数（每 task 独立 scenario），实现时
  改为**全局时序**（跨 task 的扁平脚本）。理由：多节点 workflow 的「A.spawn → A.resume
  → B.spawn → ...」一眼写成扁平列表更直观；单节点重入退化为全局时序特例。
- **claude backend prompt 传 stdin**：原计划 argv positional，实测 `--allowed-tools`
  variadic 会吞 prompt arg。改与 `orca/exec/claude/executor.py` 一致——prompt 走 stdin。
- **`_RESULT_TYPES` 简化**：原 frozenset({"result"})，review 指出单值不必 frozenset，
  改字符串比较。

---

## 验证

```
pytest tests/spike_ask_user/ -m "not integration"
→ 36 passed

pytest tests/spike_ask_user/ -m integration  # 需 claude CLI + API key
→ 2 passed（spawn + resume + secret word 上下文保持）

python -m tests.spike_ask_user.run_spike --backend mock --scenario closed_loop
→ done:true, A 哨兵 1 次 + resume 1 次 + task_id 复用 + B 真接 → 闭环成立

python -m tests.spike_ask_user.run_spike --backend mock --scenario reentry
→ exit code 3（SentinelLoopExhausted，按设计 fail loud）

python -m tests.spike_ask_user.run_spike --backend claude
→ 真 spawn claude 跑通 driver
```

---

## Review 历史

- code-reviewer 一轮（impl + coverage）：1 MUST-FIX（哨兵泄漏断言空操作，因 json.dumps
  separator 误匹配）+ 5 SHOULD-FIX + 6 NICE-TO-HAVE，全闭环。关键修复：
  - 哨兵泄漏断言改 strict `is_sentinel()`（非 substring）
  - `SubagentBackend` ABC 内置诊断字段（DRY，避免子类复制 7 字段）
  - 删 dead code（`task_id_call_distribution`、`_RESULT_TYPES` frozenset、unreachable raise）
  - cleanup 用 `logger.exception` + `Path(__file__).parents[2]` 推 Orca root
  - `drive_node` 退出后 post-loop `if is_sentinel` 改 `assert`（provably dead code → post-condition）
  - 新增 8 个测试覆盖 OrcaBusyError / orca_cli 5 raises / nested JSON / node B sentinel /
    fabrication word boundary / is_sentinel non-str / task_id_reused 反向断言

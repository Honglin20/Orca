# Release Note —— 阶段 3：events/ 事件层 + profiles/ 命令替换层 + capability 校验闭环

> **日期**：2026-06-30
> **范围**：events/（EventBus + Tape，唯一真相源）+ profiles/（CLI 命令替换抽象 + capability 静态校验）
> **SPEC**：[`docs/specs/phase-3-events.md`](../specs/phase-3-events.md)
> **计划**：[`docs/plans/2026-06-30-phase3-events-profiles.md`](../plans/2026-06-30-phase3-events-profiles.md)
> **测试**：195 passed（103 phase1+2 基线 + 92 phase3 新增，零回归）

---

## 交付清单

### 步骤 A：events/（唯一真相源）
- `orca/events/tape.py` —— Tape：append-only JSONL；seq 写时分配（Lock 覆盖「校验 + seq 分配 + write + flush」整体，保证 seq 序 == 文件行序）；每事件 write+flush；`_json_safe`（bytes/Path/未知 → 纯 JSON）；resume 先截断末尾残行 + warning 再追加；非 resume 重开有残行也 warning（fail loud）；坏事件（非法 type）在 emit 时即拒（不分配 seq，不留间隙）。
- `orca/events/bus.py` —— EventBus：持有 Tape；emit 第一动作 = Tape.append（唯一真相）；透传 session_id 到事件顶层；异步 fan-out（put_nowait，非阻塞）；per-consumer cursor；队列满丢最老 + warning；close 时队列满也 warning。
- `orca/events/replay.py` —— replay_state + apply_event：纯 reducer fold；**幂等硬约束**（streaming 事件 no-op，node_status/context 取 last-writer-wins，绝不拼接）；未知事件类型 warning（fail loud）；workflow_failed 在 node=None 时保留 current_node。
- `orca/events/__init__.py` —— 导出 EventBus/Tape/Subscription/replay_state/apply_event（Event/EventType 从 schema re-export）。

### 步骤 B：profiles/（命令替换层）
- `orca/profiles/capabilities.py` —— ProviderCapabilities：frozen pydantic + extra="forbid"，7 能力字段。
- `orca/profiles/base.py` —— CliProfile（frozen dataclass）+ resolve_cli_path（env > default，运行时读）+ Translator/ResultExtractor 类型别名。
- `orca/profiles/registry.py` —— 注册表：load_builtin（扫 builtin/*.py 自动发现）/ load_project（.orca/profiles/*.py 覆盖）/ get_profile（不存在或 disabled → ValueError 附原因）/ register / disable_profile / available_profiles；损坏文件 → disable + fail loud；惰性 builtin 加载。
- `orca/profiles/builtin/claude.py` —— claude profile（flags `-p --output-format stream-json --include-partial-messages --verbose --permission-mode auto --bare`，capabilities 全开）。
- `orca/profiles/builtin/ccr.py` —— ccr profile（`default_cli_path="ccr code"`，mcp_tools=False，structured_output="prompt_injection"）。
- `orca/profiles/__init__.py` + `builtin/__init__.py` —— 导出（validate_workflow_profiles 惰性导出保持依赖方向清晰）。
- translator/result_extractor 用 dummy 占位（真实现 phase 4），dummy 类型匹配含 session_id 的 Event。

### 步骤 C：capability 校验闭环
- `orca/profiles/validate.py` —— `ProfileIssue(node, severity, message)` frozen dataclass + `validate_workflow_profiles(wf)`，四条规则**仅基于 AgentNode 真实字段**：① get_profile 失败→error ② output_schema+structured_output=none→error ③ foreach body AgentNode + concurrent_safe=False→error ④ streaming_events=False→warning。只依赖 schema + registry，不依赖 compile。
- `orca/compile/validator.py` —— 追加 `_check_profiles`（第 ⑨ 项），单向调 `validate_workflow_profiles`，issue 汇入 ValidationResult，走 raise_if_errors 聚合。**不改 phase 2 已有 8 项逻辑**。

### 测试（92 个，全部通过）
- `tests/events/test_tape.py`（16）：append/replay/seq 单调/seq==行序（并发 50 + 校验无坏行）/resume 残行截断/json_safe/残行容忍/非 resume 残行 warning/坏事件不留 seq 间隙/close 后 fail loud。
- `tests/events/test_bus.py`（12）：emit/异步不阻塞（100 事件 < 1s）/per-cursor 隔离（A 满 B 全收）/队列满丢老+warning/session_id 透传/session_id 端到端（emit→tape→replay 分组）/close 队列满 warning/坏 type emit 时拒。
- `tests/events/test_replay.py`（17）：**reducer 幂等性核心（node_completed/started 各 N 次=1 次）+ 全 streaming 类型参数化幂等（含 agent_usage 不翻倍）** + 各 EventType 分支 + workflow_failed 保留 current_node + 同 node 多 session last-writer-wins + 未知类型 warning + 一条读路径（live==replay）。
- `tests/profiles/test_capabilities.py`（6）：frozen/extra-forbid/7 字段必填/Literal 约束。
- `tests/profiles/test_registry.py`（14）：builtin 发现/get_profile/未知→ValueError/project 覆盖/env 禁用/损坏 disable（缺 PROFILE/语法错/类型错）/env 覆盖 resolve_cli_path/运行时读/register 恢复。
- `tests/profiles/test_validate.py`（12）：四条规则各覆盖 + ccr(prompt_injection)+output_schema 不报 error + foreach body 未知 executor 不重复报 + ProfileIssue frozen。
- `tests/compile/test_validate_profiles.py`（8）：_check_profiles 集成 + capability error 阻止/warning 不阻止 + phase 2 共存聚合 + ccr+output_schema 端到端。

---

## 5 条铁律验收（SPEC §6.0）

1. **唯一真相源**：事件只写 Tape 一处（grep 无并行内存 list / sidecar / snapshot）。
2. **幂等性**：reducer 应用同一事件 N 次 = 1 次（streaming 事件 no-op，含 agent_usage 不翻倍；有参数化测试覆盖全 streaming 类型）。
3. **一条读路径**：streaming = replay = 同一 apply_event（无第二份 live/replay 分支代码）。
4. **fail loud**：未知 executor / 不兼容 capability / 残行截断（resume）/ 残行 warning（非 resume）/ 损坏 profile / 未知事件类型 / 坏 type（emit 时拒）/ close 队列满 —— 全显式报错或 warning，不静默吞。
5. **依赖单向无环**：events→schema、profiles→schema、compile→profiles；profiles/validate 不 import compile；schema 不 import 任何人。

---

## Review 反馈与修复

两轮 review（实现 review + 测试覆盖 review），全部反馈已修复：

**实现 review（4 MAJOR + 6 MINOR）**：
- M1 `workflow_failed` 在 node=None 时 clobber current_node → 仅 node 非 None 时覆盖（保留最近已知位置）。
- M2 reducer 未知事件类型静默丢 → 显式 warning（fail loud）。
- M3 close 时队列满静默丢事件 → warning（可见）。
- M4 非 resume 重开有残行不警告 → 加 warning（与 resume 路径同源风险须可见）。
- m1 Tape.append 声称校验但未校验 → 落盘前构造 Event 校验（坏 type emit 时即拒，不延迟到 replay）。
- m2 replay 生成器句柄泄漏 → 文档化「须耗尽」契约。
- m3 resolve_cli_path shlex 文档 → 澄清拆分归 exec 层。
- m5 并发测试不真并发 → 加 perturbation delay + 校验「无坏行」。
- m6 validate 双调 get_profile → 接受（清晰度胜于微小 DRY）。

**测试覆盖 review（核心 gap）**：
- 补「100 事件 / 慢订阅者 / 非阻塞」端到端测试（SPEC §6.8）。
- 补 session_id 端到端测试（emit→tape→replay 分组，原先 replay 测试绕过 bus.emit）。
- 加强并发测试（50 事件 + 校验每行独立合法 JSON，原先无 Lock 也能过）。
- 补全 streaming 类型幂等参数化测试（agent_thinking/tool_call/tool_result/usage，含 usage 不翻倍）。
- 补「坏事件不留 seq 间隙」测试（valid→invalid→valid = seq 1,2 非 1,3）。
- 补 foreach body 未知 executor 不重复报测试。
- 补 close 队列满 warning 测试 + route_taken 幂等测试。

---

## 偏离 SPEC 之处

无功能性偏离。两处实现细节选择（均向 SPEC 靠拢，非自作主张）：

1. **Tape.append 在分配 seq 前校验 Event**：SPEC §3.2 说「Lock 覆盖 seq+write+flush」，未明说校验时机。review m1 指出原实现声称校验但实际只在 replay 时校验（坏 type 会落盘）。修正为「校验在 seq 分配前、Lock 内」——保证「坏事件不落盘 + 不留 seq 间隙」，更贴合「seq 序 == 文件行序」铁律与 fail loud。
2. **未知事件类型 warning**：SPEC §3.4 未显式要求 reducer 对未知类型报错，但 §6.0 铁律4（fail loud）适用。加 warning 是把铁律4 推到 reducer 数据面（防御未来新增 EventType 忘加分支）。

---

## Commit

- `feat(events): phase 3 事件层 + profiles 命令替换层 + capability 校验闭环`（SHA 见 CHANGELOG）

---

## 后续阶段衔接

- **phase 4 exec**：translator 产出 Event → `bus.emit(..., session_id=...)`；profile 给 executor 用（exec 永远不硬编码 binary/flag）。
- **phase 5 run**：Orchestrator 生成 run_id（composite `<slug>-<ts>-<nanoid6>`）+ emit workflow/node/session 事件；用 `replay_state` 重建。
- **phase 7 web**：订阅 Subscription.queue → WS 推；GET /api/state 读 tape（不另存内存 list）；按 session_id 懒加载 session 细节。

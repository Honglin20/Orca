# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

## 🔥 当前任务（2026-07-15）：in-session spec v5 —— defect + step 4 完成，待进 step 5a

> **新 session 必读**：本块 + [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) **v5** + [今日 release note](../releases/2026-07-15-in-session-defects-and-step4.md) + [step 2b release note](../releases/2026-07-14-in-session-v5-step2b.md) + [`docs/specs/in-session-unified-backend-draft.md`](../specs/in-session-unified-backend-draft.md)（合并推迟 spec）。

**2026-07-15 已完成**（3 commits，code-reviewer 三轮全闭环，185 affected 单测 0 回归）：
- **DEFECT-1**（`2de50e3`）：`cc_nudge.sh` 改 python3 + fail loud（旧 jq 静默吞错违反铁律 12）。
- **DEFECT-2**（`e763e9e`）：`orca status` 加 `--run-id` option（与 SKILL.md/spec 一致；位置参数兼容）。
- **step 4**（`52cc9f3`）：orca.ts transform 整删 + 死代码清零（保 idle nudge hook）；删 `_constants.py`；spec 决策 #12 + 验收标准措辞修正。

### 待办（spec v5 §8，step 5a/5b/6）

- **⑤a** 删 setup 全栈（§6.1 清单）+ MCP migration note（§6.2）。**A2 铁律**：
  `_check_execute_phase_no_gate_tools` / `_INTERRUPT_TOOL_NAMES`（execute phase gate 校验）
  与 setup 正交，**保留不删**。范围（grep 全仓 33 文件命中，code/test 大约 25 个需改）：
  - `schema/workflow.py` Workflow.setup 字段
  - `compile/validator.py` `_check_setup_phase_constraints` + `_check_jinja2_refs` valid_roots 去 setup
  - `compile/parser.py`（可选 pre-scan）
  - `iface/mcp/server.py` 删 `tool_get_agent_prompt` + `tool_start_workflow` 删 `setup_outputs` 参数
  - `iface/mcp/setup_phase.py` 整模块删（+ `tests/iface/mcp/test_setup_phase.py` 同步删，**保留** execute phase gate 测试）
  - `iface/mcp/{agent_catalog,hints,catalog}.py` setup 相关（has_setup / setup 段 / `_estimate_runtime`）
  - `iface/web/run_manager.py` setup_outputs 透传 + `iface/cli/commands.py` teams run setup 透传
  - `exec/context.py` RunContext.setup + `exec/render.py` setup namespace
  - `run/orchestrator.py` setup_outputs 参数 + setup_ns 注入
  - **m13**：setup YAML 段靠 pydantic `extra=forbid` fail loud；可选 parser pre-scan friendly error
  - MCP breaking change migration note（旧客户端不调 `get_agent_prompt`，`start_workflow` 去 `setup_outputs`）
- **⑤b** daemon batch emit + 错误信封统一（独立 commit，C3）。
- **⑥** teams install nga/cac nudge 机制真机验证（留用户侧，无代码）。
- **推迟** 合并同一后端（`advance_step`↔`Orchestrator`），见 merge spec，等触发条件。

### step 4 follow-up（非阻塞）

- **DEFECT-2 review MINOR#1（stop/open 同型 docs/CLI 错配）**：spec/SKILL.md 写 `orca stop --run-id <id>` / `orca open [--run-id <id>]`，但 CLI 用位置参数 → 主 session 照文档跑报错（与 DEFECT-2 同型）。下个 sprint 收（每命令独立 commit，模式同 DEFECT-2：加 `--run-id` option + 位置参数兼容 + 异值 BadParameter）。
- **真机验证（test-agent 的活）**：opencode promptAsync 注入 / CC Stop block / skill 实跑 wf / cac+nga nudge 机制。orca 只装 WSL conda，opencode 在 Windows——主 session 全链路 E2E 部署需 orca 装 Windows 或 opencode 装 WSL（非代码）。
- **doctor.entry_hook 永久 unknown**：transform 退场后 PROBE_ENTRY_REL 心跳永不再写——doctor 的 entry_hook check（hard=False，可选）正确反映「transform 已退场」。可在后续 spec step 清理掉该 check（5 项 checks → 4 项），但本步不动 doctor。

---

## 跨阶段其他待立项（与 in-session 正交，不影响当前）

- **三壳统一 ADR**（[`2026-07-08-shell-unification-adr.md`](../specs/2026-07-08-shell-unification-adr.md)）：单一读路径 + 渲染契约 + 视觉，待 spec-review。
- **agent interrupt**（[`agent-interrupt-design-draft.md`](../specs/agent-interrupt-design-draft.md)）：mid-stream cancel+resume，待立项 SPEC。
- **render layer v1.5**（codex 接入，前置 phase-12-capabilities）/ **v2**（Web TS 镜像 + 流式 shiki + diff 虚拟化）。
- **TUI fold DRY**：fold 字段抽 `orca/run/projections.py`（单一 reducer 消费）。
- **phase-16 批 2**：本地包分发（多 pool + `name@source`）+ workspace-instruction。
- **background chart gap**：`--background` 模式 chart 可用。
- **参考仓 F1/F3/F4/F5**（[调研](../plans/2020-07-05-reference-repos-borrow.md)；F2/G2-G7 已落入 phase-11/12）。

## 必读文件（下一任务开工前按需）
- [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5（本次范围 SPEC）
- [`docs/releases/2026-07-15-in-session-defects-and-step4.md`](../releases/2026-07-15-in-session-defects-and-step4.md)（今日全貌）
- [`docs/specs/in-session-unified-backend-draft.md`](../specs/in-session-unified-backend-draft.md)（合并推迟 spec + 触发条件）
- [CHANGELOG](CHANGELOG.md)（历史完成项索引，各完成块详细在对应 release note）

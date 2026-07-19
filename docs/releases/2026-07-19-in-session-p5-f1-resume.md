# 2026-07-19 —— in-session 加固与性能 P5（F1 TARS resume v4.1 简化版）

**SPEC**: [`docs/specs/2026-07-19-in-session-hardening-and-perf.md`](../specs/2026-07-19-in-session-hardening-and-perf.md) v4.1 §4 F1 + §7 F1 AC + §8#6 改动清单
**范围**: cli.py status 无参路径加 `resumable` 派生字段 + `orca/skills/tars/SKILL.md` 加续跑段 + 新建占位 spec `docs/specs/agent-interrupt-design-draft.md` + `docs/status/CURRENT.md` 修断链。**不碰** events/replay.py / run/step.py / run/orchestrator.py（P3 域）/ marker.py（不动契约）。
**架构铁律（用户）**: resume 是 **run 级别**的事，用 **run_id** 管（status/next 现成），**与 host_session 无关**。**零 host_session、零 prompt_file 新字段、零 spike。marker 仍 3 字段。v5 决策 11 不修订**。

## 改动点（按 SPEC §8#6）

### 1. `cli.py` status 无参加 `resumable`（派生标志，非 marker 字段）

- `cli.py:1424-1436`：`status` 无参列出活跃 run 的 dict 加 `"resumable": True` 字段。**派生自 marker 存在性**（本循环已通过 `read_marker != None` + `tape_path.is_file()` 两层守门），不读 marker 新字段、不引入新辅助函数。
- `cli.py:1445-1448`：文本输出每行尾加 `resumable=true`；尾行提示加 `或 orca next --run-id <run_id> 续跑`（新 session 据 status 找到 run_id 后知道怎么续）。
- **JSON 字段集**：5 键 → 6 键（加 `resumable`）；纯加字段（SPEC §1 铁律 1：返回 shape 只增不减）。

### 2. `orca/skills/tars/SKILL.md` 加续跑段

- 位于「单引号转义」与「途中查看 / 中断」之间。教主 session：
  1. `orca status --json`（无参）拿活跃 run 列表。
  2. `runs[*].resumable == true` 即可续（含 `run_id` + `node` + `status`）；**先问用户**确认续跑，不自作主张。
  3. **复用第 3 步驱动循环**：`orca next --run-id X`（**无 output**）idempotent 重发 `current_node` 的 prompt → 派子代理 → `orca next --run-id X --output '<产出>'` 推进 → 循环到 `done: true`。
- 明示「idempotent 重发不会推进 workflow」「续跑主路径不触发 compliance」「续跑 vs 途中查看」三段语义。
- `success_criteria` 加一条 resume checklist。

### 3. 新建占位 spec `docs/specs/agent-interrupt-design-draft.md`

- 修 CURRENT.md 之前的断链（被引用为「in-session 中断恢复 spec」但不存在）。
- 明确切分：**in-session resume** = F1 落地（已实施）；**engine-level interrupt** = TBD（留空骨架）；**host_session 绑定** = deprecated（指 `2026-07-17-host-session-binding-design-draft.md` v2 草稿）。

### 4. `docs/status/CURRENT.md` 修断链

- P5 从「候选下一步」移除（已完成）。
- 删 follow-up / debt 里的「文档断链」条目（已补）。
- 待办里 P1 + P5 改 SKILL.md 合并提示用户重跑 `tars install`。

### 5. SPEC §7 / §1 铁律 AC + §v2→v3 changelog 闭环 stale AC

- SPEC §7 F1 AC 行原写 v3 的 host_session 语义（"marker 4 字段 last_host_session / v5 决策 11 修订"），与 §4 v4.1 + §8#6 矛盾。改写为 v4.1 语义。
- §1 铁律 AC 行原写 "marker 字段=4（F1 后）"，改为 "marker 字段=3（F1 v4.1：零新字段）"。
- §v2→v3 changelog 加 inline 注释，明示 host_session 方向已 v4.1 弃用（避免后续读者误读）。
- Rule 7（Surface conflicts, don't average them）：SPEC 内部不一致闭环。

## 复用契约（零 advance_step 改动）

resume 复用 `advance_step` branch 4（`run/step.py:391-403`，SPEC §4 引用 `advance_step:390-402`）现成的 idempotent-replay：

```python
# advance_step branch 4：无 output 且进行中 → 重发 pending prompt（零 emits）
if pending is None:
    raise InSessionError(...)
ctx = _build_ctx(wf, _outputs_acc_from_state(state), inputs, rid)
prompt, prompt_file, rroot = _deliver(nodes[pending], ctx, prompts_dir, ...)
return StepResult(emits=[], done=False, node=pending, prompt=prompt, prompt_file=prompt_file, ...)
```

`orca next --run-id X`（无 output）走此分支：tape 里 `pending` 仍是当前节点，重发 prompt 不推进、不写 tape、不增 emits。主 session 拿到 prompt → 派子代理 → 产出经 `--output` 回传才真正推进（走 branch 3 正常推进）。

**零改动**：advance_step 决策三分支、route 求值、emit 序列一字不改；marker.py 3 字段契约不动；EventType 不增；7 命令不增减；tape 仍唯一真相源。

## 守门修复（review 闭环）

### SKILL.md 守门双修

- `test_v3_step1.py::test_entry_skill_md_has_no_business_logic_keywords`：SKILL.md 原写 `replay_state.current_node`（含 forbidden `replay`）。改为「Orca 据 tape 已知当前停在 Y」（用业务语义，不暴露内部 state 名）。
- `test_v3_step1.py::test_entry_skill_md_has_no_tars_backend_commands`：SKILL.md 用 `resumable` 字段（含 forbidden 子串 `resume`，但 `resumable` 是 orca status JSON 合法输出字段）。守门改用 `\b{kw}\b` word-boundary 匹配（单词项），允许 `resumable`、`observe` 等合法复合词；多字短语仍 substring。文档化此选择 + 加 F1 inline 注释。

### F1 测试 intent 加强

- 加「tape not added」否定断言（step 2 前后行数比较）—— `advance_step` branch 4 `emits=[]` 契约的直接守护（兄弟测 `test_in_session_cli.py:615-617` + `test_daemon.py:290-292` 都守这同一 AC，F1 resume 路径原本漏抄）。
- 拆分 `no_output_count` 断言：原 `<= 1` 太弱（step 2 +1 后 step 4 误增也仍 ≤1）；改 step 2 后 `== 1`、step 4 后 `== 0`（钉死 cli.py:1327-1328 的 `if output is not None: marker.no_output_count = 0` reset 契约）。
- F1 测试 step 2 + step 4 不传 `--tape`（真验证 SKILL 教的裸 `orca next --run-id X` 形态，依赖 `_default_tape_path(run_id)` 解析；不走 CliRunner 捷径）。

## 验收对照（SPEC §7 F1 v4.1）

| AC | 实现位置 | 测试 |
|---|---|---|
| status 无参列 `resumable` | `cli.py:1424-1448` | `test_in_session_v8.py:809-846`（JSON key-set + True 断言）+ `test_in_session_v8.py:876-891`（文本 resumable=true + 续跑提示）+ `test_in_session_cli.py:1215-1227`（文本断言）|
| SKILL 含 resume 段（status → next 无 output 重发 → 子代理 → next output）| `SKILL.md:183-243` | `test_skill_md_flags_guard.py` 5 测（flag ⊆ help）+ `test_v3_step1.py:508-545`（业务关键词 + tars 后端命令守门）+ `test_f1_resume_flow_*` 端到端（`test_in_session_v8.py:920-998`）|
| 占位 spec 建立 | `docs/specs/agent-interrupt-design-draft.md` 新建 | 文档无需测试 |
| CURRENT 断链修复 | `docs/status/CURRENT.md` 更新 | 文档无需测试 |
| **marker 仍 3 字段（不动）、零新字段** | `marker.py` 未改 | `test_marker.py:test_marker_only_three_fields`（已有守门，仍绿）|

**铁律 AC**：无新裸 sys.exit / 宽 except pass / 2>/dev/null||true；advance_step emit snapshot 不变；7 命令不变；**marker 字段=3（零新字段）**；tape 仍唯一真相源；零 host_session / 零 prompt_file。

## 偏差

无。F1 v4.1 严格按 SPEC §4 + §8#6 实现，零 deviation、零 scope creep。唯一 SPEC 文本改动是 §7 stale AC 闭环（Rule 7：surfacing conflict）。

## 验证结果

- **196 测试全过**（tests/iface/in_session/test_v3_step1.py + test_skill_md_flags_guard.py + test_in_session_v8.py + test_in_session_cli.py），含 P5 新增 `test_f1_resume_flow_*` 端到端测 + key-set 守门更新 + 文本断言更新。
- **code-reviewer 两轮（impl + test-coverage）**：0 🔴 blocker；🟡 全部 address（SPEC §7 stale AC 闭环 + F1 测试 intent 加强 + SKILL.md 守门双修）。
- **架构铁律（user）逐条核通过**：resume = run 级（与 host_session 无关）；orca 管所有状态/决策/compliance；主 session 仅调度（派子代理 + 传 output）；不跨层耦合；不过度设计。

## Commits

| SHA | 项 | 类型 |
|---|---|---|
| `<SHA 见 git log>` | F1 P5 | feat（status resumable + SKILL 续跑段 + 占位 spec + CURRENT 断链）|

## 文件改动

**生产代码**（绝对路径）:
- `/mnt/d/Projects/Orca/orca/iface/in_session/cli.py`（status 无参 resumable 字段 + 文本输出 + 尾行续跑提示）
- `/mnt/d/Projects/Orca/orca/skills/tars/SKILL.md`（续跑段 + success_criteria）

**文档**:
- `/mnt/d/Projects/Orca/docs/specs/agent-interrupt-design-draft.md`（**新建**占位 spec）
- `/mnt/d/Projects/Orca/docs/specs/2026-07-19-in-session-hardening-and-perf.md`（§7 F1 AC + §1 铁律 AC + v2→v3 changelog 闭环 stale）
- `/mnt/d/Projects/Orca/docs/status/CURRENT.md`（修断链 + 移 P5 候选 + 删文档断链 debt）
- `/mnt/d/Projects/Orca/docs/status/CHANGELOG.md`（加 P5 索引）
- `/mnt/d/Projects/Orca/docs/releases/2026-07-19-in-session-p5-f1-resume.md`（本 release note）

**测试代码**:
- `/mnt/d/Projects/Orca/tests/iface/in_session/test_in_session_v8.py`（F1 端到端测新建 + status key-set 守门 + 文本断言）
- `/mnt/d/Projects/Orca/tests/iface/in_session/test_in_session_cli.py`（status 文本断言更新）
- `/mnt/d/Projects/Orca/tests/iface/in_session/test_v3_step1.py`（tars 后端命令守门 word-boundary 改造）

## 遗留

- **未做（SPEC §8 defer 项 / YAGNI）**：F2 retry / O1b cache / O1c tape resume / O5 lock contention / compliance 语义偏窄（§8#5：`no_output_count` 把所有"无 output next"一律算 +1，F1 主路径绕过；是否重设计留独立 issue）。
- **后续包（SPEC §6）**：P2（D4 + D5 marker 三态 + doctor orphan）/ P4（D1 + D2 失败路径统一）/ P6（S1 contract-test 黄金集）—— 都碰 cli.py，按 SPEC 串行 P2→P4；P6 独立可任意时点。
- **SKILL.md install 副本**：源已改（P1: O4 busy + F3 inputs_validation_error；P5: F1 续跑段），用户若装了旧 TARS skill 副本需重跑 `tars install` 同步（install 经 `iface/cli/install_cmds.py` 分发）。
- **engine-level interrupt TBD**：本 P5 占位 spec 只覆盖 in-session resume；engine-level 主动打断（agent 取消 / 节点超时 / 并行 partial cancel）仍待 ADR。

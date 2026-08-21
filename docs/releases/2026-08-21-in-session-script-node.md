# Release: in-session script 节点 pass-through（2026-08-21）

> 任务：in-session 路径支持 `kind: script` 节点——一次调用内同步执行连续 script 链，路由确定性分叉，`auto_executed` 摘要报备。
> commits：`d62e8d6`（主体）+ `45b0608`（E2E 缺陷修复）。SPEC：[2026-08-21-in-session-script-node.md](../specs/2026-08-21-in-session-script-node.md)（v3，三轮对抗评审闭环）。

## 动机

in-session 此前仅支持 agent 节点（`step.py` `_check_agent_node` 硬卡）——所有确定性逻辑（判定/gate/度量）只能塞进 agent prompt 让 LLM「调脚本 + 转抄结果」，受 LLM 干扰（历史事故：struct-curator 伪造 `continue_loop` 直进 finalize）。本任务把确定性判定从 agent 节点解放：script 节点由引擎就地执行，LLM 零判决权。设计草稿 [in-session-script-node-design-draft.md](../specs/in-session-script-node-design-draft.md)（2026-07-27）沉淀动机与分层方案。

## 契约要点（SPEC v3）

- **pass-through 循环**：`orca next` / bootstrap / daemon 三入口共享 `advance_with_scripts`——`advance_step` 算出下一节点，`node_kind=="script"` → `execute_script_inline` 就地执行（**ns 先行 emit**，nc/nf 各自成批，尾随 error 事件丢弃对齐 headless），`advance_after_script` 纯决策续路由，直到 agent 节点或终态。循环上限 `resolve_max_iter(wf, merged_inputs)`（防路由成环持 flock 死锁）。
- **同步阻塞 v1**：CLI 进程即执行者；at-least-once（窗口 i 可恢复重执行 / 窗口 0、ii 不可逆 fail loud，均文档化）。
- **可见性**：回复 JSON `auto_executed: [{node, exit_code, elapsed, stdout_tail, stderr_tail}]`（tail 取**末** 500 字符，Unix tail 语义；成功 + 失败信封都报备，零成功条目省略字段）。
- **inputs 单口径**：`merged_inputs = _resolve_inputs(wf, {**tape_fold, **cli_inputs})` 全程透传（ctx 渲染 / 路由 / max_iter），杜绝 CLI 原始 `--inputs` 的 StrictUndefined 假 internal_error。
- **防宿主劫持**：分支 3 入口守卫——pending 为 script 且带 `--output` → `state_corrupt` fail loud（宿主无权代 script 交卷）。
- **零回归（纯增量）**：全 agent workflow 行为逐字节不变；`StepResult.node_kind` 默认 None 旧调用方零感知；schema/compile/exec/events 四层零改动；非 script 的非 agent kind 继续 `unsupported_node_kind`。

## SDD 过程

spec-reviewer 三轮对抗（21 + 17 + 7 项全闭环，无表面闭环）：round 1 揪出攒批 emit 使崩溃恢复态不可达（B1）、分支 3 宿主劫持洞（N1）、无界循环（N2）三个 BLOCKER；round 2 揪出 inputs 口径（M1，goal_check 首用必翻车）、D4 守护时序事实错误（M2）、parse_json 表行错误契约（M3）；round 3 措辞级收口。决策记录 SPEC §9（D1-D4、U1-U2 等按 goal 授权采纳评审推荐）。

## 实现与测试

- **改动 10 文件**（SPEC §10 清单 + `test_node_memory.py` monkeypatch 缝隙迁移）：`run/step.py`（node_kind + 四分支 + `advance_after_script` + 公开包装 `merged_inputs_for`/`inline_script_ctx`）、`run/orchestrator.py`（`_next_node_for_resume` 可选 inputs，D2）、`iface/in_session/{_step_io,cli,daemon}.py`、workflow-contract 文档三契约、fixture 更新（`kind: set`）、新测试文件。
- **单测**：`tests/iface/in_session/test_in_session_script.py` 39 例（step 四分支 / 真 bash helper / 循环与上限 / G2 两路全长对齐 / AC 面 / tail 末段语义）。目标子集 927 passed（7 失败经纯净 HEAD worktree 复现证实为并行任务既有，非回归）。
- **E2E（真执行，证据 `.e2e_spe2e/evidence/`）**：
  - A 组 CLI 直驱 10 场景全过：AC1-AC5/AC7-AC9 + D3 撞顶（flock 释放验证）+ 真 SIGKILL 的 at-least-once（tape 止于 ns → 无 --output 重执行 / 带 --output state_corrupt / marker 计数不增）。
  - B 组 opencode + deepseek-v4-flash 全链路一次通过（196.6s）：tars skill → orca CLI → 真 LLM 子代理 ×2 + script 链内联 + parse_json 分叉，主 session 真实消费 `auto_executed`（原话「s1/s2 自动执行，s2 判定 goal_met: true → 分叉到 b_met」），逐节点产出逐字合格。
  - round 1 唯一缺陷 D-1（tail 取前 500 而非末 500）→ `45b0608` 修复 + round 2 复测三断言（len==500 / endswith 判定标记 / 与 tape 末 500 逐字相等）全过。

## 已知限制与 follow-up

- 窗口 0 / ii（批间隙被杀）fail loud 且不可逆（headless 有 from_tape 重放，in-session YAGNI）；长 script 持 flock 期间 `stop` busy（kill next 进程即释放，busy reply 带 hint）；SIGKILL 可留 script 孤儿（幂等/单实例契约兜底，见 workflow-contract）。
- follow-up：script 异步化、headless resume inputs（U2）、RouteError 包成 InSessionError、bootstrap 守护调序。
- 首个使用者预告：prof-opt / struct goal_check 类确定性 gate 可直接落 script 节点（草稿 §12）。

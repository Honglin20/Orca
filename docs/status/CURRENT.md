# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前：kd-nas LLM 语义 fidelity 审计——实现完成（B1+B2+D3），待独立洁净审查 + 真机 E2E

**任务**：SPEC [`2026-08-05-kd-nas-fidelity-audit-spec.md`](../specs/2026-08-05-kd-nas-fidelity-audit-spec.md)
（REVIEWED + 用户拍板，逐字实现）—— 给 kd-nas 训练脚本生成节点加 LLM 语义 fidelity 审计
（B1）+ ID 化 Resumed Re-Check 收敛环（B2），抓 L3 确定性层的实证盲区
（helper 体外 look-alike / transform 内容 / optim kwargs / 控制流重排）。

**状态**：**实现完成 + `tars validate` 0 error/0 warning + code-reviewer 一轮闭环**
（0 must-fix / 3 should-fix 已采纳）。已 commit。
- 新建 `workflows/subagents/kd-nas/project-fidelity-verifier-kd.md`（KD 化独立副本，
  sentinel=KDPFV01，Out-of-scope=KD 引擎 + student 变体；STATUS 契约机械可解析）。
- `kd-train-script/agent.md` + `SKILL.md` Step 4 在 L3 与 L4-mechanical 间插
  **L4-semantic 收敛环**（MAX_TURNS=3，first-run + resume 模板经 `{{ subagents_root }}`
  point-to-file，ID 范围防御，reaffirm/Unresolved→ask-user，apply fixes 后重跑 L1+L3）。
- `train-script-verify/agent.md` 加 step 3.5（report-only 一次性 spawn）+ step 4 也
  report-only 并传 Accepted IDs（D1+N8）。
- `kd-nas.yaml` 两节点注释同步（output_schema 不变）。
- 新建 `examples/mnist_kd_adversarial/`（D3 fixture，`optim.py::build_optimizer`
  weight_decay 偏差，L3-blind / B1-caught）。
- SPEC §3.1 line 54/106 prose typo（与 frontmatter `-kd` 冲突 validator 铁律）→ 文件
  统一命名 `project-fidelity-verifier-kd.md`。

**待办**：
- [x] 实现 + `tars validate` + code-reviewer 一轮闭环。
- [ ] **独立最终洁净审查**（用户另行派 agent，不属本次范围）。
- [ ] **真机 E2E 三条路径**（SPEC §4 A7/A8/A9：收敛环 / Unresolved→ask-user /
      reaffirm 防呆）用 D3 fixture 跑生成节点验证。

**必读**：
- 本任务 release note `docs/releases/2026-08-05-kd-nas-fidelity-audit.md`
- SPEC `docs/specs/2026-08-05-kd-nas-fidelity-audit-spec.md`（§3 契约 / §4 验收 / §6 洁净）

---

## 当前：in-session 权限审批 Web 桥——实现完成 + 单测全绿，待 test-agent 真机 e2e

**任务**：SPEC [`in-session-permission-hook.md`](../specs/in-session-permission-hook.md) v3.2
（spec-review conditional-pass）—— in-session workflow 运行期，宿主 CC 的 PermissionRequest →
web 审批卡 → 用户 allow/deny；超时默认 allow（可配）；前端 yolo 开关。

**状态**：**实现完成 + 54 单测全绿（broker 24 / hook 15 / install 9 / ws 集成 6）+ 回归 100 测试绿 +
前端 527 测试绿 + tsc + vite build 通过**。改动**未 commit**（用户明示）。
- hook stdlib-only；ApprovalBroker 不碰 tape / handler / exec / events.bus（grep 守门 AST 验证）。
- 失败语义（SPEC §7）：broker 不可达 → ask；HTTP 4xx/5xx → deny+stderr；非 JSON → deny+stderr；
  timeout → policy（默认 allow）；disconnect → aborted（不发 allow）。
- 实现 SPEC §3.2 P1 偏差：手写 timer task + disconnect poller + await fut 替代 ``asyncio.gather(wait_for, ...)``
  避免 `wait_for` cancel 底层 future 触发 `InvalidStateError`（语义等价）。

**待办**：
- [x] 引擎 + 前端 + install + doctor + 单测。
- [ ] **code-reviewer 一轮**（已分发，等结论 + 修全部反馈）。
- [ ] **test-agent 真机 e2e**（SPEC §9 #2 spike 冻结前置）：交互式 CC + Task 子 agent 下
      PermissionRequest 是否触发；stdin 字段名实证；broker-unreachable→ask；真实审批流端到端。
- [ ] commit（待用户许可）。

**必读**：
- 本任务 release note `docs/releases/2026-08-05-in-session-permission-hook.md`
- SPEC `docs/specs/in-session-permission-hook.md`（§3.1 hook / §3.2 broker / §4.3 WS / §4.4 install / §9 spike）

---

## 历史：subagent point-to-file 协议——实现完成 + 单测全绿，待 headless e2e

详见 [release note](../releases/2026-08-05-subagent-point-to-file.md)。**待 headless nas-supernet e2e**。

---

## 历史：PostToolUse 事后告警守卫——coder-agent 完成，待 test-agent 四前端真机 e2e

详见 [release note](../releases/2026-08-05-posttooluse-rogue-guard.md)。**待四前端真机 e2e**。

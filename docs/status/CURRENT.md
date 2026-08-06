# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前：deferred-training-cron 原型实现完成，留真机 E2E + 迁移给后续 task

**任务**：SPEC [`deferred-training-cron-design-draft.md`](../specs/deferred-training-cron-design-draft.md)
（§2 模式 / §3 改动 / §4 验收 / §6 非目标）—— `ns_run_train` 多天训练解耦：三分支 Step 0
（reuse / resume-pending / fresh-launch）+ Step 2 fresh-launch 五步（detach + warmup + 估时 +
cron + park detached）+ Step 3 dual-signal status 推导（`pid_alive AND cron_registered.flag`）。

**状态**：**实现 + `tars validate` 0/0 + code-reviewer 一轮闭环完成**（3 must-fix + 8 should-fix
全修，1 optional 采纳 / 3 optional 跳过）。已 commit。

**待办**（留用户/后续 task）：
- [ ] **真机 E2E**（SPEC §4 acceptance 1-5）：造小训练 fixture（MNIST 2-epoch 或 mock 每 epoch
      sleep + 写 epoch log + 末写 ckpt），跑 `ns_run_train`，断言 warmup→估时→cron 注册→park→
      （可选）cron 触发后 reuse 接力。
- [ ] **迁移到 ns_retrain**（nas-supernet）：同模式（task #9）。
- [ ] **迁移到 kd-nas train-teacher / distill**（task #9）：同模式（cron 重跑命令 `orca kd-nas`）。

**必读**：
- release note `docs/releases/2026-08-06-deferred-training-cron.md`（含 SPEC 偏差 + code-reviewer
  闭环明细 + 已知限制 `at` 路径无幂等清理 / per_epoch 60s 默认 / TerminateNode 无 pending）。
- SPEC `docs/specs/deferred-training-cron-design-draft.md`（§2 三分支 / §3 改动）。

---

## 历史：project-scoped artifacts——实现 + code-reviewer 闭环完成，留集成测试补全给用户

**任务**：SPEC [`project-scoped-artifacts-design-draft.md`](../specs/project-scoped-artifacts-design-draft.md)
（spec-review 14 issue 全闭 → 实现）—— in-session 引擎面 project-scoped `$ORCA_ARTIFACTS_DIR`
解析 + nas-supernet input 改名 + 6 个昂贵节点 Step 0 软跳过 + kd-nas 撤销拍平 + 4 个 kd-nas
节点 Step 0。

**状态**：**实现 + code-reviewer 一轮闭环完成**（0 must-fix / 4 should-fix / 3 optional；
surgical should-fix 全修 + 1 docstring，留 1 测试补全项给用户）。
- `797a6c8`：核心三块（引擎面 + nas-supernet 改名/carve-out/Step 0 + kd-nas 撤销拍平）。
- `1cb377f`：code-reviewer 闭环（bootstrap raise 结构化 + Step 0 dead code 清理 + docstring Rule 7）。
- `77013e4`：CHANGELOG + CURRENT.md 索引。

**待办**（留用户定夺）：
- [ ] **bootstrap 集成测试补全**（code-reviewer should-fix #3）：补 `CliRunner` 驱动
      `orca <wf> --inputs '{"project_root":"/abs"}'` 断言 `<proj>/artifacts/<wf>/` 真 mkdir +
      `$ORCA_ARTIFACTS_DIR` 注入 env；`project_root="rel"` → 非 0 退出 + 结构化错误信封。
      属测试补全非生产代码改动，单独立 case 更合适。

**必读**：
- release note `docs/releases/2026-08-06-project-scoped-artifacts.md`（含偏离 SPEC 记录 +
  code-reviewer 闭环明细）。
- SPEC `docs/specs/project-scoped-artifacts-design-draft.md`（§2 契约 / §5 非目标）。

---

## 历史：kd-nas LLM 语义 fidelity 审计——实现完成（B1+B2+D3），待独立洁净审查 + 真机 E2E

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

## 当前：in-session 权限审批 Web 桥——已 commit + test-agent e2e 闭环，仅剩 §9 #2 真机 spike

**任务**：SPEC [`in-session-permission-hook.md`](../specs/in-session-permission-hook.md) v3.2
（3 轮 spec-review conditional-pass）—— in-session workflow 运行期，宿主 CC 的 PermissionRequest →
web 审批卡 → 用户 allow/deny；超时默认 allow（可配）；前端 yolo 开关。

**状态**：**已 commit + 单测全绿 + test-agent 真机 e2e 闭环**（real uvicorn + real hook 子进程 + real WS）。
- 7 组件：stdlib-only hook + ApprovalBroker（不碰 tape/gate/handler/exec/events.bus）+ 路由 + ws 第二 pump +
  前端独立 approval store + install 扩展 `_install_cc_nudge` + doctor 心跳。
- test-agent e2e 抓到 2 真 bug 并已修 + 防回归测试：**BUG A**（`_disconnect_poller` 缺 `await` → timeout
  路径全断，已修）、**BUG B**（redact regex 空白截断 → Bearer token 泄露，已修）。64 单测 + 回归全绿。
- 失败语义（SPEC §7）：broker 不可达→ask / HTTP 4xx-5xx→deny+stderr / 非 JSON→deny+stderr /
  timeout→policy（默认 allow）/ disconnect→aborted。

**待办**：
- [x] 实现 + 单测 + code-reviewer + test-agent e2e + 2 bug 修复 + commit。
- [ ] **§9 #2 spike（唯一剩余，真机用户侧）**：交互式 CC + Task 子 agent 下 PermissionRequest 是否
      自然触发 + stdin 字段名实证（自动化 `claude -p` 证不了，task 4 Q3 已证非交互不触发）。
      失败 → SPEC §9 #6 fallback 切 PreToolUse（`block` 枚举 + tool-classification 扩 `readonly_tools`）。

**必读**：
- release note `docs/releases/2026-08-05-in-session-permission-hook.md`（r2 含 bug 修复）
- SPEC `docs/specs/in-session-permission-hook.md`（§3.1 hook / §3.2 broker / §9 spike）

---

## 历史：subagent point-to-file 协议——实现完成 + 单测全绿，待 headless e2e

详见 [release note](../releases/2026-08-05-subagent-point-to-file.md)。**待 headless nas-supernet e2e**。

---

## 历史：PostToolUse 事后告警守卫——coder-agent 完成，待 test-agent 四前端真机 e2e

详见 [release note](../releases/2026-08-05-posttooluse-rogue-guard.md)。**待四前端真机 e2e**。

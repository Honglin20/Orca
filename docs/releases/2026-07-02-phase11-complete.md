# Release Note —— phase 11 CLI feature 补全（收官）

> **日期**：2026-07-02
> **范围**：CLI/后端层面补齐 Conductor 已有的 6 类核心 feature，Orca 在 CLI 场景下达到 Conductor 等量功能水平。
> **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md)（§10.3 含对抗评审修订汇总；§11.1-§11.9 含 9 处实现偏离记录）
> **计划**：[`docs/plans/2026-07-01-phase11-cli-enrichment.md`](../plans/2026-07-01-phase11-cli-enrichment.md)
> **基线**：phase 1-10 全部完成（master）

---

## 1. 背景

通过迁移 `mxint-analysis` 实测 + Conductor vs Orca 三向客观对比，识别 CLI 路径下 6 类真实 gap。本 phase 系统化补齐，**保留 Orca 单 Tape + 纯 reducer + 单向依赖架构**，不动核心；保持 `claude -p` CLI 子进程路线（不切 SDK，SPEC §1.1 论证 ROI 极低）。

## 2. 流程（按 goal 要求的 agent 组合）

```
spec-review-adversarial（对抗评审，fail→conditional-pass，22 真问题闭环）
  → clean-code-builder × wave（实现 + 自带 code-reviewer + commit + release note）
  → test-coverage-e2e × wave（覆盖审计 + 真 bug 狩猎 + 公共接口 e2e）
  → code-reviewer（收官横切审计）
  → COMMIT
```

4 个 wave 顺序（D1 裁定）：① CI/Interrupt+Guidance/Resume → ② Retry/ask_user → ③ Wait/Validator/Dialog → ④ Skip/daemon。

## 3. 交付的 11 个 feature

| Wave | Feature | commit | 测试增量 |
|---|---|---|---|
| 1 | CI（GitHub Actions gate + opt-in integration） | `120085f` | — |
| 1 | Interrupt UI + mid-run Guidance 注入 | `9db57f4` `2c622b7` | 652→697 |
| 1 | Checkpoint Resume（Tape 即 checkpoint） | `8d11cb6` | 697→712 |
| 2 | Retry Policy（transient 失败自动重试） | `95cdae4` | 726→753 |
| 2 | ask_user MCP 工具挂载（FastMCP SSE，真 claude 验证连通） | `dcc3e63` | 753→773 |
| 3 | Wait Node（asyncio.sleep，可被 Ctrl+G 打断） | `22d7c41` | 784→822 |
| 3 | Semantic Output Validator（LLM 二次校验） | `e4eb07c` | 822→852 |
| 3 | Dialog（跑完后多轮聊，重 spawn + 拼历史） | `61bd88e` | 852→879 |
| 4 | Skip to Agent（显式 skip 目标 + NodeSelectModal） | `4e37ece` | 888→904 |
| 4 | daemon `--background` + ps/logs/wait | `5d10e0b` | 904→956 |
| 收官 | dialog 事件入 reducer no-op + LogStream 全事件类型描述 | `d295922` | 956→959 |

**测试**：652 passed → **959 passed, 1 skipped, 0 failed**（+307 新测试，0 回归）。38 deselected 为真 claude integration（CI gate skip，`/integration` PR comment 触发）。

## 4. e2e 审计狩猎到的 2 个 critical bug（fail loud 闭环）

对抗式 e2e 审计两次捕获单 Tape 不变量违反，均测试驱动修复：

1. **`interrupt_resolved` 丢事件**（`a3ae691`）：CLI 单壳路径 async broadcaster 与 `bus.close()` 竞态 → ABORT/SKIP 时 `interrupt_resolved` 永久丢失。修：`record_resolved` 同步 `await bus.emit` 写盘（SPEC §11.1）。
2. **Ctrl+G 打不断 sleeping wait node**（`89b23ab`）：`notify_all_waits` 原只在 node 边界触发，wait sleep 期间 `_drive_loop` 阻塞 → 死代码。修：`request_interrupt` 注册 pending 时即时调 `notify_all_waits`（分离「即时唤醒」与「边界 resolve」两个关注点）。

两 bug 均以 `xfail(strict=True)` 测试自动闭环（修复后翻转强制删 marker）。

## 5. SPEC 偏离（§11.1-§11.9，全部 Rule 7 裁定 + 代码/release note 双落）

| § | 偏离 | 理由 |
|---|---|---|
| 11.1 | CLI 单壳中断不经 await-future（`record_resolved` 同步 emit） | 避 `bus.close()` 竞态丢事件 |
| 11.2 | ask_user 路由参 `orca_run_id`/`orca_node`（非 `_orca_*`） | FastMCP 拒下划线前缀 |
| 11.3 | `--allowed-tools` 显式追加 ask_user | claude `-p` 默认拒 MCP 工具（spike 实证） |
| 11.4 | register 前移 spawn 前 + 按 run 批清 | orchestrator 不持 claude session_id |
| 11.5 | `WaitHandleRegistry` Protocol（非直接持 EventBus） | 铁律 2 张力化解（DIP） |
| 11.6 | `validate_output` 不持 bus、不 emit；validator/retry 独立预算 | 铁律 2 + 复用 wave-2 retry 原语不 churn |
| 11.7 | Dialog 3-method split + `build_env_overlay` 抽到 `exec/env.py` | Textual modal 轮间交还 UI；DRY |
| 11.8 | `skip_target` 独立 kwarg（非折叠进 answer） | SKIP 正交于 (action,guidance) |
| 11.9 | daemon detached child 走 headless（非 TUI） | detached 无 TTY，Textual 会崩 |

## 6. 裁定的 4 个决策（不再讨论）

- **D1** wave 顺序（仍全做 11 feature，排序降险）
- **D2** 只读 `attach` descoped（价值低于 `tail -f` tape）；daemon 只做 `--background`/`ps`/`logs`/`wait`
- **D3** Budget Enforcement 不做（SPEC §12 是契约，Budget OUT）
- **D4** ask_user 确定性 tool-params 路由（不依赖 MCP session）

## 7. 架构验收（code-reviewer 收官横切，0 🔴 0 🟡）

- **依赖铁律**：grep + `tests/exec/test_contract.py`（24 测试）双守门，零违规。`exec/` 不 import run/iface、不持 EventBus/Tape；`gates/` 不 import iface。
- **单 Tape 唯一真相源**：所有新 feature 事件经 EventBus.emit 写 Tape；`user_guidance`/`dialog_history` 是 prompt 注入/预留位，非第二真相源（真相在 tape 事件）。
- **幂等 reducer**：新增穷尽性守门 `test_every_event_type_has_reducer_branch_or_explicit_noop`（遍历 37 EventType），未来漏分支立即可见。
- **fail loud**：仅 validator fail-safe（SPEC §9.6.6 LLM 崩溃 → 当 passed）+ 退出路径清理两处 sanctioned 静默，均 `logger.warning`。
- **DRY**：`BroadcasterMixin`（HumanGate + Interrupt 共享）+ `build_env_overlay`（validator + executor + dialog 共享）。
- **测试 INTENT**：e2e/contract 测试断言 tape/replay_state/exit-code 等可观测结果，非内部方法耦合。

## 8. 已知限制 / 后续

- **真 claude E2E（manual）**：SPEC §10.2 items 3/4/7/9 的真实 `orca run` 交互路径（Ctrl+G、ask_user、kill-9 resume、wait 打断）需真 TTY + API key，自动化测试用 fake executor 覆盖可观测契约；真 run 走 CI `/integration` 或本地 `pytest -m integration`。ask_user 的 SSE 连通性已 spike 实证（真 claude 连上 + 调通 echo）。
- **descoped**：读写 attach（需 UDS 控制通道，后续 phase）、Budget（SPEC §12）、Web 端 InterruptModal/DialogModal（Web phase）。
- **可选 polish**：`_stop_agent_tools` 的 `except Exception` 可收窄为 specific 清理异常（非阻塞）。

## 9. commit 索引

见 [`docs/status/CHANGELOG.md`](../status/CHANGELOG.md) 顶部逐条（每 feature 一条 + 2 bugfix + 收官 sweep）。phase 11 涉及 commit：`120085f 9db57f4 2c622b7 8d11cb6 a3ae691 95cdae4 dcc3e63 22d7c41 e4eb07c 61bd88e 89b23ab 4e37ece 5d10e0b d295922`（+ 各 SHA 回填 docs commit）。

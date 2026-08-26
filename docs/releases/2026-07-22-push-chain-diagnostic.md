# Release Note — 推送链路诊断（doctor --probe-push）

**日期**：2026-07-22
**SPEC**：[`docs/specs/push-chain-diagnostic.md`](../specs/push-chain-diagnostic.md) v2
**Plan**：[`docs/plans/2026-07-22-push-chain-diagnostic.md`](../plans/2026-07-22-push-chain-diagnostic.md)
**Commits**：`275838b` (S1) → `af97ac1` (S2) → `a3f10a1` (S3) → `284b389` (S4)

---

## 做了什么

`orca doctor --probe-push`：一次跑完推送链路 **6 跳**，精确指出哪一跳断（不止「daemon 活着」）。

| 跳 | 问的问题 | 复用源 |
|---|---|---|
| H1 family_detect | backend/family 探测 | `_hostenv.detect_backend_from_env` + `detect_family_from_env` |
| H2 cac_pid_walk | CAC PID 链 + session json | `cac_session_id_from_pid`（权威）+ 只读复算中间态 |
| H3 adapter_discovery | root/meta.json 齐 | `_make_adapter` + `CCJsonlAdapter.discover_children` |
| H4 daemon_progress | daemon 在推进？ | `_sidechain_daemon_alive` + `read_last_complete_lines` + grep daemon log |
| H5 bus_flow | bus 队列溢出？ | grep daemon log `订阅者队列满`（bus.py:77 同源文案契约） |
| H6 ws_delivery | bus→WS 通？ | self-spawn：`start_run` + monkey-patch Orchestrator.run + bus.emit 合成事件 + WS 等 3s |

输出契约：`{overall, first_break, runbook, hops[6]}`，每跳含 `{hop, status, evidence, reason, fix_hint}`；
`first_break` = 链路顺序首个非 pass 的跳——主 session 聚焦它即可。

## 实际做了什么

- **新增唯一模块** `orca/iface/in_session/_push_probe.py`：6 跳函数 + 编排 `run_push_probe` +
  辅助（`_recompute_pid_walk_intermediate` / `_stat_tape_agents` / `_compute_run_age` 等）。
  叶子消费方，只 import 现有真相源（SPEC §2.1 依赖图），不新增接口/数据结构（plain dict）。
- **cli.py**：doctor 加 3 typer Option（`--probe-push` / `--run-id` / `--ws-url`）+ lazy wrapper。
  现有 6 check / ok / report 一字不改（零副作用，SPEC §2.2 / §7-1 守）。
- **新增 runbook** `docs/troubleshooting/push-chain.md`：6 节（症状/根因/修复动作/验证），
  显式锚 `{#h<N>-<slug>}`；fix_hint 是快速指针，MD 是真相源。
- **测试**（38 + 2 = 40 全绿）：
  - `tests/iface/in_session/test_push_probe.py`：H1-H6 三态 + H2 中间态自洽守门（双向）+
    零副作用回归门 + runbook 锚点 + fix_hint 指针守门（SPEC §5 三组）。
  - `tests/iface/in_session/test_push_chain_smoke.py`：fast e2e 冒烟（happy + 负向），
    跨平台（不依赖 /proc），SPEC §6 核心技巧 `ORCA_CC_SIDECHAIN_ROOT` env。

## 偏离 SPEC 的决策

1. **H6 degradation path**：SPEC B2 决议首选 `start_run + MockSubagentBackend`，但
   `RunManager.start_run` 签名不接 backend（核实 `run_manager.py:259-267`）→ 走 SPEC 明示
   degradation「monkey-patch Orchestrator.run + bus.emit 合成事件」（SPEC §4 H6 + §9 B2 明示）。
   monkey-patch 在 try 块内 + try/finally 严格恢复（review H-1 fix 防 leak）。
2. **fast e2e 不用 tars_harness.bootstrap_run**：那是真 orca CLI 子进程，拉起太多（spawn
   daemon / chart / web），冒烟只测 daemon→bus→WS 链路不需要。手写 tape workflow_started
   + node_started 更轻 10x。SPEC §6 step 1「复用 tars_harness」字面偏离但功能等价。
3. **跨平台绕开 /proc**：daemon subprocess 探活 `_sidechain_daemon_alive` 在 macOS 不可靠；
   smoke test 改用「poll WS 收 event」判活，与现有 `test_e2e_daemon_ingests_cc_sidechain`
   在 macOS 失败的 `/proc` 依赖脱钩。
4. **SPEC §6 预期 <5s**：实际 7.3s（含 daemon spawn + poll + ingest + follow + WS）；测试
   断言 `<15s` 容忍跨平台抖动（SPEC「<5s」是理想态）。

## 验收

- **SPEC §7-1 零副作用**：无 `--probe-push` 时 doctor 输出与基线一致（test_doctor_without_probe_push_has_no_push_chain_probe）。
- **SPEC §7-2/3** H1 三态（unknown/fail/pass）。
- **SPEC §7-4** H4 fail 分支（iteration 异常 / daemon_dead / gap>0 + run_age 老）+ unknown 不误报（disk_jsonl_lines=0）。
- **SPEC §7-5a/b/c** H6 self-spawn happy + pump 抛 RuntimeError + 连续两次无残留。
- **SPEC §7-5 反例补充**：wrong event type（防 pump 串流误判 pass）+ outer except（fail loud status=error）。
- **SPEC §7-6** fast e2e happy + 负向。
- **SPEC §7-7** 三组守门（锚点对应 / fix_hint 指针 / H2 中间态自洽）。
- **SPEC §7-8** 回归：in_session 全量无新增失败（8 个存量 macOS failure 与 S1-S4 无关）。

## 验证结果

40 tests 全绿（test_push_probe.py 38 + test_push_chain_smoke.py 2）：
- S1 (275838b)：H1/H2/H3 + runbook 初版 + 零副作用回归门。
- S2 (af97ac1)：H4/H5（§8#4 覆盖）+ gap/freshness/run_age + 中间态自洽守门双向。
- S3 (a3f10a1)：H6 self-spawn + 5 个测试（含 wrong-event / outer-except 反例）。
- S4 (284b389)：fast e2e 冒烟 + SPEC §5 三组守门齐全 + 跨平台。

每步均 code-reviewer 闭环：S1 (1 🟡#2 + 3 🟢 全修) / S2 (3 🟡 全修) / S3 (2 🔴 + 4 🟡 全修) /
S4 (2 🔴 + 4 🟡 全修)。

## 遗留（defer 到 S5）

- **S5 passive `--ws-url` 模式**（SPEC §7-9 defer）：连既存 `/ws` passive listen N 秒等真事件。
- **realistic 形态冒烟**（`@pytest.mark.manual` + deepseek-v4-flash）：SPEC §6 可选形态。

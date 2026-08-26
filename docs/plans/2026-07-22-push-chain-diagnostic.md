# 实施计划：推送链路诊断（doctor --probe-push）

> **日期**：2026-07-22
> **SPEC**：[`docs/specs/push-chain-diagnostic.md`](../specs/push-chain-diagnostic.md) v2（spec-review-adversarial conditional-pass 已闭环）。
> **流程**：本计划 → code agent 实现（S1-S4）→ test agent e2e。每步独立 commit + 自带 code-reviewer。

---

## 0. 全局约束（每步都必须满足）

- **零副作用**：不改 `_spawn_sidechain_daemon` / `_make_adapter` / `EventBus` / `ws_handler` / 任何 adapter。`orca doctor`（无 `--probe-push`）输出与基线 `eb63b35` 相同（时间字段除外）—— §7-1 回归门每步都跑。
- **纯复用**：`_push_probe.py` 只 import 现有真相源（见 SPEC §2.1 依赖图 + 内部稳定 API 契约），不新增接口/数据结构。
- **fail loud**：诊断本身不静默吞错（hop 抛异常 → status=error + reason）。
- 每步收尾：跑 `tests/iface/in_session/` 无回归 + 该步新增测试绿。

---

## S1 — 模块骨架 + H1/H2/H3 + doctor Option + runbook 初版

**改动文件**：
- 新增 `orca/iface/in_session/_push_probe.py`：
  - `ProbeContext` dataclass（run_id / ws_url / rundir）
  - `run_push_probe(run_id, ws_url) -> dict`（编排 6 跳，逐跳 try/except 兜底，算 overall + first_break）
  - `_hop_h1_family_detect(ctx)` / `_hop_h2_cac_pid_walk(ctx)` / `_hop_h3_adapter_discovery(ctx)`
  - hop 名常量 `H1..H6` + slug（用于 MD 锚点）
- 改 `orca/iface/in_session/cli.py` doctor 函数：
  - 加 3 个 typer Option：`--probe-push`（bool）/ `--run-id`（str）/ `--ws-url`（str）
  - 函数体末尾 `if probe_push: out["push_chain_probe"] = run_push_probe(...)`（lazy import `_push_probe`）
- 新增 `docs/troubleshooting/push-chain.md`：总述 + H1-H6 六节（每节：症状/根因/修复动作/验证），显式锚 `{#h<N>-<slug>}`

**复用点（H1/H2/H3）**：
- H1：`_hostenv.detect_backend_from_env()` + `detect_family_from_env()`
- H2：`_hostenv.cac_session_id_from_pid()`（判定权威）+ 只读复算中间态（env / `/proc` PPid 链 / `~/.cac/sessions/<ppid>.json`）
- H3：`sidechain_daemon._make_adapter(backend, host_session, family=)` → `adapter.discover_children(host_session, 0)` + `adapter.root`

**新增测试** `tests/iface/in_session/test_push_probe.py`：
- H1/H2/H3 各 happy + fail + unknown（构造手段见 SPEC §7-2/3）
- 零副作用回归门（§7-1）：无 `--probe-push` 时 doctor 输出快照对比
- H2 中间态自洽守门（SPEC §5 测试 3）

**验收**：§7-1 / §7-2 / §7-3 通过；H1/H2/H3 三跳 evidence 含全部中间态。

**commit**：`feat(doctor): --probe-push 推送链路诊断 H1/H2/H3 + runbook 初版`

---

## S2 — H4 daemon_progress + H5 bus_flow

**改动文件**：
- `orca/iface/in_session/_push_probe.py` 加 `_hop_h4_daemon_progress(ctx)` / `_hop_h5_bus_flow(ctx)`
- `run_push_probe` 接入（仅 `--run-id` 给定时跑 H4/H5 的 tape/log 读）

**复用点**：
- H4：`_sidechain_daemon_alive(run_id)` + `events.tape.read_last_complete_lines(<rundir>/<run_id>.jsonl, 200)` 统计 agent_* 事件 + 读 `<rundir>/<run_id>/sidechain_daemon.log` grep iteration 异常 / 队列满 + 算 gap / freshness / run_age（started_at ← marker `orca-<run_id>.json`，fallback run_dir ctime）
- H5：同源 log grep `订阅者队列满`（`bus.py:77` 文案）

**新增测试**（追加 `test_push_probe.py`）：
- H4 fail（disk_jsonl>0 + agent_events==0 + run_age>30s + daemon_alive mock True）
- H4 unknown（disk_jsonl==0，不误报刚 bootstrap）
- H5 fail（log 含队列满 warning）/ unknown（无）

**验收**：§7-4 通过（含 unknown 不误报）。

**commit**：`feat(doctor): --probe-push H4 daemon_progress + H5 bus_flow（§8#4 覆盖）`

---

## S3 — H6 ws_delivery（self-spawn）

**改动文件**：
- `orca/iface/in_session/_push_probe.py` 加 `_hop_h6_ws_delivery(ctx)`（async，`asyncio.run` 包）

**实现（B2 决议）**：
- 首选：`RunManager(runs_dir=tmpdir).start_run(<最小 wf>, backend=MockSubagentBackend)` + `create_app(manager)` + `uvicorn.Server` bind `127.0.0.1:0` + WS client subscribe + 等收。
- S3 开工先核实 RunManager 能否接 MockSubagentBackend（spike_ask_user 的 backend 怎么注入 web 路径——可能要确认 RunManager/executor 接 backend 的方式）。**若不可用**：降级为 `manager.start_run` 后直接 `handle.bus.emit("agent_message", {...})` 注入合成事件（复用 `EventBus.emit` 公开 API）。
- finally：WS close + `manager.stop_run` + uvicorn shutdown。独立 tmp runs_dir + `__probe__` 前缀。

**新增测试**：
- §7-5a happy（3s 收到）
- §7-5b 反例（monkeypatch `ws_handler._pump` 抛 RuntimeError → fail）
- §7-5c 反例（连续两次跑，第二次无残留 fail）

**验收**：§7-5a/b/c 通过。

**commit**：`feat(doctor): --probe-push H6 ws_delivery self-spawn 端到端活探`

---

## S4 — fast e2e 冒烟 + runbook 三组守门测试

**改动文件**：
- 新增 `tests/iface/in_session/test_push_chain_smoke.py`（SPEC §6 fast 形态）
- 追加 `test_push_probe.py`：runbook 锚点对应 + fix_hint 指针有效（SPEC §5 测试 1/2）

**fast e2e 步骤**（SPEC §6）：bootstrap 单节点 wf → tmp sidechain root 写假 jsonl+meta → spawn daemon（`ORCA_CC_SIDECHAIN_ROOT=tmp` + poll 0.1s）→ ephemeral web → WS subscribe 等收 5s → teardown。负向用例：不写 meta.json → fail。

**验收**：§7-6（<5s + 负向 fail）+ §7-7（三组守门）+ §7-8（in_session 全量无回归）。

**commit**：`test(push-chain): fast e2e 冒烟 + runbook 一致性守门`

---

## S5（可选，后续）— passive --ws-url + realistic 冒烟

- H6 passive 模式（连既存 /ws passive listen）
- realistic 冒烟 `@pytest.mark.manual`（deepseek-v4-flash）
- 验收 §7-9

---

## commit 节奏

S1 → S2 → S3 → S4 各一个 commit（以上 message）。每个 commit 前：跑 `tests/iface/in_session/` 全量 + 该步新测试；commit 后 code-reviewer 自检。S5 独立。

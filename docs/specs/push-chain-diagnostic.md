# SPEC：推送链路诊断（doctor --probe-push）+ 主 session 排障 runbook

> **状态**：SPEC v2（契约），spec-review-adversarial conditional-pass 的 4 blocker + high/medium 已闭环。
> **前置草稿**：[`push-chain-diagnostic-design-draft.md`](push-chain-diagnostic-design-draft.md)。
> **前置 SPEC**：[`2026-07-19-in-session-hardening-and-perf.md`](2026-07-19-in-session-hardening-and-perf.md) §8#4（D3 明示「不覆盖存活但持续 iterate 失败」——本 SPEC 补这条覆盖）。
> **实施计划**：[`docs/plans/2026-07-22-push-chain-diagnostic.md`](../plans/2026-07-22-push-chain-diagnostic.md)（S1-S5 patch 边界 + 测试清单 + commit 节奏）。

### v2 决议（spec-review 闭环）

- **B2 决议**：H6 self-spawn 走 **`start_run` + MockSubagentBackend**（测真 start_run→bus→pump→WS 链路；不写 attached tape；MockSubagentBackend 在 `tests/spike_ask_user/` 已存在）。
- **B3 决议**：H4 `run_age` 阈值 = **30s**（子 agent 首事件 3-15s 常见，留余量；原草稿 10s 会误报正常 bootstrap）。
- **H4(中间态) 决议**：H2 中间态展示 = **受控只读复制 + 自洽性守门测试**（不重构 `cac_session_id_from_pid`，保「不新增接口」铁律）。
- **草稿推翻清单**：草稿 §A.2 H6 step 1 的 `start_run` 路径**保留**（B2 决议）；草稿 Step 2a（daemon stderr 重定向）**取消**（`cli.py:381/398` 已重定向到 `sidechain_daemon.log`）。草稿其余无明示推翻则继续生效。

---

## 0. 目标与非目标

**目标**：
1. `orca doctor --probe-push` 一次跑完推送链路 6 跳，**精确指出哪一跳断**（不止「daemon 活着」）。
2. 输出含 `first_break`（链路顺序首个非 pass 的跳）+ 每跳 `fix_hint`（一行摘要 + 指向 runbook MD 的锚点）。
3. 主 session（LLM agent）读 doctor 输出 + **一个 MD runbook** 即可定位并修复，**不必读推送链路源码**。
4. 一个 **fast e2e 冒烟测试**：零模型调用，<5s 验证 daemon→bus→WS 全链路通。

**非目标**（铁律）：
- **doctor 是纯观测/定位，复用现有真相源，不新增任何接口/数据结构**。它**不调用真 LLM 后端**（H6 self-spawn 用 MockSubagentBackend 跑最小 wf，不烧模型；见 B2 决议）、不改任何现有函数行为。
- **零副作用**：不改 `_spawn_sidechain_daemon` / `_make_adapter` / `EventBus` / `ws_handler` / adapter 任何一行。诊断是扩展性新增模块，对现有功能无影响。
- 不做自动修复（只给 fix_hint 指引，修不修由主 session/用户决定）。

---

## 1. 推送链路 6 跳（逻辑链路 + 真相源 + 复用点）

生产端→消费端，逐跳可独立失败。**每跳探测只复用现有函数，零重写**：

| 跳 | 探测问的问题 | 复用的现有真相源（不新增） | 失败现象 |
|---|---|---|---|
| **H1** family_detect | 当前被认成什么 backend/family？走哪条分支？ | `detect_backend_from_env()` + `detect_family_from_env()`（`_hostenv.py:112/134`） | CODEAGENT 没注入 → family=None → 选错 dotdir |
| **H2** cac_pid_walk | CAC PID 链能否回溯到 codeagentcli + session json 在不在？ | `cac_session_id_from_pid()`（`_hostenv.py:42`）+ 直接读 `CODEAGENT` env / `/proc` 中间态 | PID 链被 setsid 孤儿化断 / session json 命名变 |
| **H3** adapter_discovery | adapter 能 discover 到子进程？root/meta.json 齐不齐？ | `_make_adapter(backend, host_session, family=)`（`sidechain_daemon.py:367`）+ `CCJsonlAdapter.discover_children`（`cc_jsonl.py:146`）+ `.root` 属性 | root 不存在 / 无 meta.json 被全跳过 |
| **H4** daemon_progress | daemon 存活且真在推进（tape 有 agent_* 事件）？有没有 iteration 异常？ | `_sidechain_daemon_alive(run_id)`（daemon 模块）+ `read_last_complete_lines`（`events/tape.py:57`）读 tape + 读 daemon log `<rundir>/<run_id>/sidechain_daemon.log` | daemon 存活但持续吞异常 / cursor 卡 |
| **H5** bus_flow | bus 订阅者队列有没有溢出丢事件？（**结构性受限**，见 §4 H5） | 读 daemon log grep `订阅者队列满` warning 计数（`bus.py:77`）——但该 warning 只在 web server 进程发，daemon log 结构上不会有 | **跨进程不可自动判定**；靠 H4↔H6 对比 + 手动 grep web server stdout |
| **H6** ws_delivery | bus→WS pump 链路通吗？合成事件能秒级到 WS？ | self-spawn：`create_app(RunManager)`（`iface/web/server.py`）+ `EventBus.emit`/`subscribe`（`events/bus.py`）+ WS `/ws`（`ws_handler.py`）+ `websockets` client | pump 异常静默退出 / WS 未订阅 |

**链路顺序敏感**：H1 断 → H2/H3 必然跟着无意义（adapter 选错）。诊断输出 `first_break` = 链路顺序首个非 pass 的跳，主 session 聚焦它即可（见 §3）。

> **file:line 脚注**（commit `eb63b35` 核对）：上表行号为该 commit 下的实际位置。已知偏差：bus 队列满 warning 在 `bus.py:77`（非 §4 H5 grep 引用的 63——63 是 `_enqueue` docstring）；CC sidechain root resolver 委托 `events/adapters/_family.py:resolve_cc_sidechain_root`；daemon iteration 吞错在 `_SidechainDriver.run()` `sidechain_daemon.py:166`（`except Exception`），非 `_iterate_once` 自身。实现期以 grep 实测为准。

---

## 2. 软件结构设计（零副作用证明）

### 2.1 新模块：`orca/iface/in_session/_push_probe.py`

唯一新增文件。**只在 doctor `--probe-push` 时被 lazy import**（`cli.py` doctor 函数体内 `from orca.iface.in_session._push_probe import run_push_probe`），避开任何 import-time 开销/循环。

**依赖方向**（单向，不破铁律）：
```
iface.in_session._push_probe
  ├→ iface.in_session._hostenv          (H1/H2：detect_*，同层)
  ├→ iface.in_session.sidechain_daemon   (H3：_make_adapter；H4：_sidechain_daemon_alive，同层)
  ├→ events.adapters.cc_jsonl            (H3：CCJsonlAdapter.discover_children，iface→events 下游)
  ├→ events.tape                         (H4：read_last_complete_lines，iface→events 下游)
  ├→ events.bus                          (H6：EventBus，iface→events 下游)
  └→ iface.web.server                    (H6：create_app，**函数内 lazy import** 防 iface.web↔in_session 潜在环)
```

无反向依赖（events 层不 import iface；iface.web 不反向 import in_session._push_probe）。`_push_probe` 是叶子消费方，只读不写现有模块。

**内部稳定 API 契约**（`_` 前缀但视作内部稳定 API，重命名/改签名须经 SPEC 更新并同步 `_push_probe.py`）：`_hostenv.detect_family_from_env` / `detect_backend_from_env` / `cac_session_id_from_pid` / `host_session_from_env`、`sidechain_daemon._make_adapter` / `_sidechain_daemon_alive`。这些是 `_push_probe` 的复用契约面，非临时私有。

**对现有结构的影响 = 零**：
- 不改 `_spawn_sidechain_daemon` / `_make_adapter` / `EventBus` / `ws_handler` / 任何 adapter。
- 不新增数据结构：探测结果用 plain dict（与 doctor 现有 check dict 同构）。
- 不改 doctor 现有 6 项 check 与 `ok` 计算：`--probe-push` 是**加性**追加 `push_chain_probe` 区块，hard 不参与 ok。

### 2.2 `cli.py` doctor 改动（加性，3 处）

1. doctor 签名加 3 个 Option：`--probe-push`（bool flag）、`--run-id`（H4 需要）、`--ws-url`（H6 passive 模式可选）。
2. 函数体末尾（现有 `typer.echo(json.dumps(...))` 之前）：`if probe_push: out["push_chain_probe"] = run_push_probe(run_id=run_id, ws_url=ws_url)`。
3. 现有 6 项 check / `ok` / `report` 一字不改。

> 不破 SPEC §1 铁律 5（「7 命令不增不减」）：`doctor` 仍是同一命令，只是多几个 Option。

### 2.3 MD runbook：`docs/troubleshooting/push-chain.md`

主 session 排障的**唯一入口文档**。结构：每跳一节（`## H1 family_detect` … `## H6 ws_delivery`），每节四段：
- **症状**：doctor 会显示什么（status/reason 原文片段）。
- **根因**：为什么会这样（一句话，不展开源码）。
- **修复动作**：具体可执行命令（`export ...` / `cd ...` / 改哪行配置），带前提分支。
- **验证**：改后怎么确认（通常是再跑 `orca doctor --probe-push` 看该跳 pass）。

doctor 的 `fix_hint` 字段 = 一行摘要 + ``详见 docs/troubleshooting/push-chain.md#<hop>``。主 session 读 doctor 输出定位到 hop → 读 runbook 对应节 → 执行修复 → 重跑验证。**全程不碰源码**。

---

## 3. 诊断输出契约

`--probe-push` 时，doctor 输出 JSON 追加 `push_chain_probe` 字段：

```json
{
  "ok": true,                              // 现有 6 check 的 ok，不变
  "diag": false,
  "report": "...",                          // 现有 report，不变
  "checks": [...],                          // 现有 6 项，不变
  "push_chain_probe": {                     // 新增（仅 --probe-push）
    "overall": "fail",                      // 任一跳 fail/error → fail；全 pass → pass
    "first_break": "H2_cac_pid_walk",       // 链路顺序第一个 fail/error/unknown 的跳（null=全通）
    "runbook": "docs/troubleshooting/push-chain.md",
    "hops": [
      {
        "hop": "H2_cac_pid_walk",
        "status": "fail",                   // pass | fail | unknown | error
        "evidence": "CODEAGENT=1, pid_walk_hit=false (20 跳内未命中 codeagentcli)",
        "reason": "PID 链回溯未命中 codeagentcli——daemon 被 setsid 孤儿化脱离 CAC 进程树",
        "fix_hint": "改自 CAC bash 子进程启动 daemon，或显式 --host-session。详见 docs/troubleshooting/push-chain.md#h2-cac-pid-walk"
      },
      ...
    ]
  }
}
```

**status 语义**（诊断本身 fail loud，与 daemon 主循环吞错策略不同）：
- `pass`：探测成功且观测到健康信号。
- `fail`：探测成功且观测到明确故障。
- `unknown`：无法判定（如 H3 root 不存在 = 子 agent 还没起，非故障；H5 无日志证据）。
- `error`：**探测本身抛异常**（不应发生；发生则显式报，不静默吞——CLAUDE.md 规则 12）。

`first_break` = **按链路顺序（H1→H6）第一个 `status ≠ pass` 的跳**；全 pass → null。链路顺序敏感（H1 断则 H2/H3 无意义），故只取第一个，不做 fail/error/unknown 优先级排序。

---

## 4. 逐跳实现契约

每跳函数签名统一：`def _hop_hX(ctx: ProbeContext) -> dict`，返回 `{hop, status, evidence, reason, fix_hint}`。`ProbeContext` 携带 `run_id` / `ws_url` / `rundir` / 复用 helper 的引用。**任一跳抛异常 → 该跳 status=error + reason=str(exc)**（外层 try/except 兜底，不传染其它跳）。

### H1 · family_detect
- 调 `detect_backend_from_env()` / `detect_family_from_env()`。
- evidence：`backend=cc, family=cac, source=env`（标注是 CLAUDE_CODE_SESSION_ID / CODEAGENT+PID 哪一路命中）。
- pass：backend 非 None 且（CC 家族时）family ∈ {cc,cac}。fail：backend=cc 但 family=None 且 config 也无。unknown：backend=None（非 in-session 环境）。

### H2 · cac_pid_walk（仅 CC 家族且非 CLAUDE_CODE_SESSION_ID 路径）
- 调 `cac_session_id_from_pid()`，**并暴露中间态**：`CODEAGENT` env 在不在、PID 链 20 跳内是否命中 `*/codeagentcli`、命中的 ppid、`~/.cac/sessions/<ppid>.json` 在不在 + 有无 `sessionId`。
  - 复用方式：`cac_session_id_from_pid` 返 `str|None` 不含中间态，且本 SPEC 不允许改这个公开 API（不新增接口铁律）→ H2 **判定权威仍走 `cac_session_id_from_pid()` 返回值**（单一真相源），中间态（env / PPid 链 / session 文件存在性）由 H2 在探测层**只读复算一遍仅作展示**。这是**受控复制**（设计权衡）：理由是 `cac_session_id_from_pid` 不暴露中间态 + 不允许新增结构化返回。**防漂移**：§5 守门测试断言「中间态复算结果与 `cac_session_id_from_pid()` 返回值自洽」（命中时 matched_ppid 对应 session_file 存在且 sessionId 非空；未命中时 PID 链确实无 codeagentcli）。
- fail：任一环节断裂，evidence 精确指出断点。

### H3 · adapter_discovery
- 调 `_make_adapter(backend, host_session, family=cfg_family)`（**复用** daemon 的同一构造路径）→ `adapter.discover_children(host_session, since_ts=0)` 列成 list。
- 展示：`adapter.root`（resolved）、root_exists、`agent-*.jsonl` 数 vs 有 `.meta.json` 数、discovered child 列表。
- fail：root 存在 + jsonl 存在 + with_meta_count=0（宿主未写 meta.json，daemon 全跳过）。unknown：root 不存在（子 agent 尚未起，非故障——**拍板：改 unknown**）。

### H4 · daemon_progress（补 §8#4 覆盖；仅 `--run-id` 给定时跑）
- `_sidechain_daemon_alive(run_id)` → pidfile/cmdline 活探（复用）。
- 读 `<rundir>/<run_id>.jsonl` tape 末尾 200 行（`read_last_complete_lines`），统计 `agent_*` 事件数 + 最新一条 timestamp → `last_agent_event_age_s`。
- 统计磁盘上 child jsonl 完整行数 `disk_jsonl_lines`（adapter.discover_children 拿到的 child × `agent-<child>.jsonl` 行数和）；`gap = disk_jsonl_lines - agent_events`（**仅展示指标，不作门控**，见下）。
- 读 `<rundir>/<run_id>/sidechain_daemon.log`（daemon 已在此落日志，`cli.py:381/398`），grep iteration 异常计数 + 队列满 warning 计数。
- `run_age_s` 来源钉死：run marker `orca-<run_id>.json` 的 started_at；fallback = run_dir ctime。
- **pass**：`daemon_alive 且 agent_events>0 且 last_agent_event_age_s<30 且 log 无 iteration 异常`。
- **fail**：`daemon_dead`；或 `disk_jsonl_lines>0 且 agent_events==0 且 run_age_s>30`（持续 iterate 失败 / 根本没 ingest）；或 `log iteration 异常计数>0`。
- **unknown**：`disk_jsonl_lines==0`（子 agent 尚未派，非故障）；或 `agent_events>0 但 last_agent_event_age_s≥30`（子 agent 长 idle / daemon 停滞，跨进程无法区分）。

> **gap 不作门控**（review 🔴#1 修正）：`gap = disk_jsonl_lines(raw 行数) - agent_events(派生事件数)` 量纲不可比——`cc_jsonl.py:283` 一行 content 遍历多 block 一对多映射，1 raw line 常产 K>1 事件（gap 恒负），system/result 行产 0 事件（gap 正）。用 gap 判漏推会误报。真正的漏推信号是「disk 有数据但 tape 0 条 agent_* 事件」（daemon 根本没 ingest），由 `agent_events==0` 捕获。gap 保留在 evidence 作展示指标。

### H5 · bus_flow（**结构性受限**——review 🔴#2 修正）
- **跨进程不可自动判定**：`订阅者队列满` warning（`bus.py:77`，`Subscription._enqueue` 遇 `QueueFull`）只在**有订阅者**的进程触发；订阅者 = WS pump，运行在 **web server 进程**。sidechain daemon 进程的 bus **无订阅者**（不调 `bus.subscribe`，tape 经 `emit` 同步写不经 `_enqueue`），故 doctor 读的 daemon log **结构上永远不含该 warning**。doctor 是独立进程，拿不到 web server 内存队列状态。
- 仍 grep daemon log 防御性兜底（daemon log 命中 → fail，罕见 / 未来 daemon 加订阅者时生效）；生产常态 → **unknown**，reason 给 H4↔H6 对比 + 手动 grep 指引。
- **真正的诊断路径**：H4=pass（tape 有事件）但 H6=fail/unknown（前端收不到）→ 嫌疑 web server 队列溢出 / pump 断。手动确认：`grep 订阅者队列满 <web server stdout/log>`。
- **文案即契约**：`bus.py:77` 的 warning 文案 `订阅者队列满` 是手动 grep 的 pattern。修改该文案必须同步 H5 grep pattern 与本 SPEC（不要求改 `bus.py` 暴露结构化计数器——那破零副作用铁律）。

### H6 · ws_delivery（端到端活探）
- **默认 self-spawn 模式**（B2 决议：走 `start_run` + MockSubagentBackend，测真 start_run→bus→pump→WS 链路，不写 attached tape）：
  1. 函数内 lazy import `from orca.iface.web.server import create_app` + `from orca.iface.web.run_manager import RunManager`（防环）。
  2. 起一个 probe run：`RunManager(runs_dir=<tmp>).start_run(<最小单节点 wf yaml>, inputs={}, backend=MockSubagentBackend(...))`——MockSubagentBackend 不调真 LLM（spike_ask_user 已有），产一条 assistant 消息进 bus。拿到 run_id + handle（真 in-process bus）。
  3. `create_app(manager)` + `uvicorn.Server` bind `127.0.0.1:0`（OS 分配 ephemeral port）。
  4. `websockets.connect(ws://127.0.0.1:<port>/ws)` → send `{"type":"subscribe","run_id":"<probe_run_id>"}`。
  5. WS client `asyncio.wait_for(recv, timeout=3.0)` 等收该 run 的 `agent_message` 事件（MockSubagentBackend 产的那条）。
  6. finally：cancel WS client + `manager.stop_run(probe_run_id)` + uvicorn shutdown。**probe run 用 `__probe__` 前缀 run_id 或独立 tmp runs_dir**，不污染用户 run。
  - 若 `start_run` 不支持注入 backend（实现期核实 RunManager 是否能接 MockSubagentBackend）：**降级**为 `RunManager` + 直接 `handle.bus.emit("agent_message", {...})` 注入合成事件（仍属复用 events 层 `EventBus.emit` 公开 API，非新接口；不写 tape 文件，emit 走 in-process bus）。
- **passive 模式**（`--ws-url` 给定，S5）：连既存 `/ws` subscribe 用户 run_id，passive listen N 秒等真事件；收到→pass，超时→unknown。
- pass：self-spawn 3s 内收到事件。fail：超时 / WS 连接拒绝。

> **隔离**：H6 在 `asyncio.run` 内跑（doctor 是 sync typer 命令）；finally 严格清理（uvicorn.Server.shutdown + manager.stop_run + WS close）。用独立 tmp `runs_dir` 隔离 probe 资源，防孤儿/端口/tape 残留。实现期 review 确认连续两次 `doctor --probe-push` 不因残留 fail（§7-5 反例 b）。

---

## 5. 主 session 排障 runbook 内容契约（`docs/troubleshooting/push-chain.md`）

文件头一段总述：「doctor --probe-push 定位到断点后，按下表对应节执行修复。每节：症状→根因→修复动作→验证。」每跳一节（H1-H6），每节四段（见 §2.3）。关键：修复动作必须**可执行**（具体命令），验证必须**可复跑**（再跑 doctor）。

**fix_hint 与 MD 的关系**：`fix_hint` 是 MD 对应节的**子集摘要（1 行）**——MD 可独立演化（加细节/分支），fix_hint 改动必须同步 MD 锚点，反之不要求。这样 MD 是真相源，fix_hint 是快速指针。

**MD 锚点**：每节标题用**显式锚** `## H<N> <slug> {#h<N>-<slug>}`（不依赖 renderer 自动生成）。

**一致性守门测试**（三组，防漂移）：
1. **锚点对应**：读 runbook MD raw 文本，正则匹配显式锚 `{#h<N>-<slug>}`，断言 `_push_probe` 每个 hop 在 MD 有对应锚点。
2. **fix_hint 指针有效**：`_push_probe` 每个 hop 的 fix_hint 提到的锚点必须在 MD 锚点集合内。
3. **H2 中间态自洽**（H4 决议）：H2 中间态复算结果须与 `cac_session_id_from_pid()` 返回值自洽（命中↔session_file 存在且 sessionId 非空；未命中↔PID 链无 codeagentcli）。

---

## 6. e2e 冒烟测试契约（fast 形态，零模型）

新文件 `tests/iface/in_session/test_push_chain_smoke.py`。

**核心技巧**（复用现有 resolver，不新增接口）：设 `ORCA_CC_SIDECHAIN_ROOT=<tmpdir>`（`cc_jsonl.py` resolver source="env" 第一优先级），手写假 `agent-<task_id>.jsonl + agent-<task_id>.meta.json` → daemon 当真子 agent ingest。

**步骤**：
1. `bootstrap_run`（复用 `tests/e2e_redesign/tars_harness.py`）一个单节点 wf → 拿 run_id + tape path + host_session。
2. tmp sidechain root 写假 jsonl + meta.json（内容见草稿 §C.2）。
3. spawn sidechain daemon（同 `_spawn_sidechain_daemon` argv，加 `--host-session` + `ORCA_CC_SIDECHAIN_ROOT=<tmp>` env，poll_interval=0.1）。
4. 起 ephemeral web：`create_app(RunManager(runs_dir=...))` + `uvicorn.Server` ephemeral port。
5. WS client subscribe(run_id)，`asyncio.wait_for` 5s 等收 `agent_message` 且 session_id==task_id。
6. teardown：SIGTERM daemon + stop server + 清理。

**判定通过**：5s 内 WS 收到目标事件。
**负向用例**（防假绿）：不写 meta.json → daemon 全跳过 → WS 收不到 → 测试 fail（证明它真在校验推送链路）。
**预期 <5s**（零模型、poll 0.1s、ephemeral port、单节点 wf）。

**realistic 形态**（可选，`@pytest.mark.manual`）：不设 `ORCA_CC_SIDECHAIN_ROOT`，子 agent 后端用 deepseek-v4-flash（CLAUDE.md 约定），手动验证前端能看到子 agent 消息。超时阈值放宽到 15s。

---

## 7. 验收标准

> 每条标注「构造手段」（怎么造出场景）+「断言」。时间派生字段在比对前 stub。

1. **零副作用（回归门）**：`orca doctor`（无 `--probe-push`）输出 JSON **字段集合与字段值与基线 commit `eb63b35` 相同**，时间派生字段（`now` / age 字符串 / 「Xs 前」）除外。构造：在无 `ORCA_DIAGNOSE` / 无 PROBE_ADVANCE 心跳环境下跑；对 stdout 做 schema+值快照对比（时间字段 stub 后逐值比对）。
2. **非 in-session 环境**：`env -u CLAUDE_CODE_SESSION_ID -u CODEAGENT -u ORCA_HOST_SESSION_ID orca doctor --probe-push` → H1=unknown，overall 不 crash，输出合法 JSON 含 `push_chain_probe`。构造：显式 unset 三个 env。
3. **H1 fail 定位**：同 §7-2 的 env（CC_SESSION_ID + CODEAGENT 均无）→ H1=fail，`first_break=H1`，fix_hint 含 runbook 锚点。构造手段：`env -u ...`（必须同时 unset `CLAUDE_CODE_SESSION_ID`，否则 detect_backend 走 cc 路径）。
4. **H4 持续 iterate 失败（§8#4 覆盖）**：daemon 存活但 tape 0 条 agent_* 事件且 `disk_jsonl_lines>0` 且 `run_age>30s` → H4=fail。构造手段：bootstrap 一个 run + 手写一个假 `agent-<id>.jsonl+meta.json` 到 sidechain root（让 disk_jsonl_lines>0）+ 等 run_age>30s（或 mock `time.time`/started_at）+ daemon alive（mock `_sidechain_daemon_alive→True`）。另测 `disk_jsonl_lines==0` → H4=unknown（不误报刚 bootstrap）。
5. **H6 self-spawn**：(a) happy：3s 内收到事件 → pass；(b) 反例：patch pump 抛非 Disconnect 异常 → H6=fail；(c) 反例：连续两次 `doctor --probe-push`，第二次不因 `__probe__` 残留 / EADDRINUSE 而 fail（清理隔离验证）。构造手段：(a) 跑 self-spawn；(b) monkeypatch `ws_handler._pump` 抛 RuntimeError；(c) 串行跑两遍断言都 pass。
6. **fast e2e 冒烟**：CI 通过 <5s；负向用例（不写 meta.json）如期 fail。构造手段：见 §6（ORCA_CC_SIDECHAIN_ROOT=tmpdir + 假 jsonl/meta）。
7. **runbook 一致性**：三组守门测试通过（§5：锚点对应 / fix_hint 指针有效 / H2 中间态自洽）。
8. **回归**：现有 `tests/iface/in_session/` 全量无回归。
9. **passive `--ws-url` 模式**（S5 已实现）：`orca doctor --probe-push --ws-url ws://<host>:<port>/ws --run-id <id>` 连真实在跑的 web，subscribe run，8s 窗口等收真实事件。验收：(a) 缺 `--run-id` → fail；(b) 死端口 → fail（WS 连接失败）；(c) 收到真实事件 → pass；(d) subscribe 成功但 8s 无事件 → unknown（被动模式无法注入，不强判；runbook H6 节给判读指引）。单测 `test_h6_passive_*` 4 例覆盖。

---

## 8. 落地拆分（实现期执行顺序）

> 纯诊断先 ship，e2e 后 ship。每步独立 commit + 自带 code-reviewer。

- **S1** `_push_probe.py` + H1/H2/H3（只读，复用 detect/_make_adapter/discover_children）+ doctor 加 `--probe-push/--run-id/--ws-url` Option + runbook MD 初版（H1-H6 节，全节先写）+ §5 守门测试骨架。【S】
- **S2** H4（读 tape + daemon log + gap/freshness）+ H5（grep log）。【S】（依赖 S1 模块）
- **S3** H6 self-spawn（B2 决议：`start_run` + MockSubagentBackend / 降级 `bus.emit`，起 ephemeral web + WS 等收）。【M】
- **S4** fast e2e 冒烟测试 + runbook 三组守门测试（锚点对应 / fix_hint 指针 / H2 中间态自洽）。【M】
- S5 H6 passive `--ws-url` 模式（已实现）+ realistic 冒烟 manual mark（defer）。【S】

依赖：S1 → S2 → S3 → S4。S5 独立可选。

---

## 9. 风险 / 已拍板

- ✅ 入口 = `doctor --probe-push` 子模式（不破「7 命令」铁律）。
- ✅ H3 root 不存在 = unknown（非 fail）。
- ✅ daemon log 已在 `<rundir>/<run_id>/sidechain_daemon.log`（`cli.py:381/398`），H4/H5 直接读，**不需新增重定向**（草稿 Step 2a 取消）。
- ✅ B2：H6 走 `start_run` + MockSubagentBackend（不写 attached tape，测真链路）。
- ✅ B3：H4 `run_age` 阈值 30s + `disk_jsonl_lines==0→unknown`（不误报刚 bootstrap）。
- ✅ H2 中间态受控复制 + §5 自洽性守门测试（不重构 `cac_session_id_from_pid`，保「不新增接口」铁律）。
- ⚠️ H6 self-spawn 清理隔离：try/finally + 独立 tmp `runs_dir` + `__probe__` 前缀；实现期 review 确认连续两次跑无残留（§7-5c）。
- ⚠️ `start_run` 接 MockSubagentBackend 可用性：S3 实现期核实 RunManager 是否能注入 mock backend；若不可用降级为 `bus.emit` 合成事件（仍复用 events 层公开 API，非新接口）。
- ✅ S5（passive `--ws-url`）已实现：`_hop_h6_ws_delivery_passive_async` 连既存 web 被动监听真实事件（8s 窗口），pass/fail/unknown 三态；不自己起 web、不注入合成事件（外部进程拿不到目标 bus 句柄）。

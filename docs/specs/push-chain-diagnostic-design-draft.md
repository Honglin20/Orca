# 推送链路诊断方案设计草稿

> **状态**：草稿，待用户拍板（2026-07-22）。
> **范围**：「CAC 环境下 agent 信息推不到前端」的诊断能力 + 主 session 可读的修复指引 + 快速 e2e 冒烟。
> **不包含**：实现代码（本稿是设计/计划，落地拆分见 §6）。
> **前置 SPEC**：[`2026-07-19-in-session-hardening-and-perf.md`](2026-07-19-in-session-hardening-and-perf.md) §2 D3 / §8#4（D3 明示「不覆盖存活但持续 iterate 失败」；本稿正是补这条覆盖）。

---

## 0. 问题陈述与诊断目标

现状：`orca doctor` 的 `sidechain_backend` / `sidechain_daemon` 两个 check **只看静态前置条件**（family resolve、root 文件存在、host_session env、daemon pidfile 存活）—— 完全不做端到端活探。结果：daemon「活着」但事件不到前端时，doctor 全绿，用户（和主 session LLM）无从定位断点。

**目标**：让一次诊断能精确指出推送链路 6 跳中哪一跳断了 + 给主 session（LLM agent）可直接执行的修复动作 + 提供一个轻量、可重复的真链路冒烟测试。

**推送链路 6 跳**（生产端 → 消费端，逐跳可独立失败）：

| 跳 | 代码位置 | 失败现象 |
|---|---|---|
| H1 family_detect | `_hostenv.py:134 detect_family_from_env` | 选错 adapter / 探测返 None |
| H2 cac_pid_walk | `_hostenv.py:42 cac_session_id_from_pid` | CODEAGENT 缺 / PID 链断 / session json 命名不符 |
| H3 adapter_discovery | `cc_jsonl.py:146 CCJsonlAdapter.discover_children` | root 不存在 / 无 meta.json / glob 空 |
| H4 daemon_progress | `sidechain_daemon.py:173 _iterate_once` | daemon 存活但每轮异常被 `except Exception` 吞 / cursor 卡住 |
| H5 bus_flow | `bus.py:63 _enqueue` | 订阅者 queue(1024) 满丢老事件（仅 warning） |
| H6 ws_delivery | `ws_handler.py:233 _pump` | pump 异常退出 / WS 断连 / 订阅未建立 |

---

## A. 诊断能力设计

### A.1 入口形态

新增 **`orca doctor --probe-push [--run-id <id>] [--ws-url ws://...]`**（独立子模式，不影响现有 6 项 check 的 ok 计算，hard=False）。

- 不带 `--probe-push`：现有 doctor 行为零改动（铁律 1：返回 shape 只增不减）。
- 带 `--probe-push`：在现有 check 之外追加一个 `push_chain_probe` 区块（结构化 JSON + 人类可读 report 双输出）。

**诊断本身 fail loud**（CLAUDE.md 规则 12）：任一跳探测抛异常 → 显式标 `status=error` + 原因，**不静默吞**（与 daemon 主循环的 `except Exception` 策略不同——诊断是一次性命令，不需「重试」）。

### A.2 逐跳探测动作

每跳输出统一结构：

```json
{
  "hop": "family_detect",
  "status": "pass|fail|unknown|error",
  "evidence": "<观测到的事实，不含结论>",
  "reason": "<若 fail/unknown：为什么>",
  "fix_hint": "<主 session 可直接执行的动作，见 §B>"
}
```

#### H1 · family_detect

- **动作**：直接复用 `detect_backend_from_env()` + `detect_family_from_env()`（不重写）。
- **evidence**：`backend=cc, family=cac, source=env`（CLAUDE_CODE_SESSION_ID / CODEAGENT / PID 回溯命中是哪一路）。
- **pass**：`backend` 非 None 且（CC 家族时）`family` ∈ {cc, cac}。
- **fail/unknown**：
  - `backend=None` → unknown（非 in-session 环境，doctor 不该在这跑）。
  - `backend=cc` 但 `family=None` 且 config 也未设 → fail，`reason="CODEAGENT 未注入且 PID 链未命中 codeagentcli；无法判定 cc/cac 子型"`。
- **fix_hint**：见 §B-H1。

#### H2 · cac_pid_walk（仅 CC 家族且非 CLAUDE_CODE_SESSION_ID 路径触发）

- **动作**：直接调 `cac_session_id_from_pid()`，并把诊断中间态暴露：
  - `CODEAGENT` env 是否存在；
  - `/proc/self/status` → PPid 链回溯 20 跳内是否命中 `*/codeagentcli`；
  - 命中的 `ppid` 对应的 `~/.cac/sessions/<ppid>.json` 是否存在 + `sessionId` 字段是否存在。
- **evidence**：`CODEAGENT=1, pid_walk_hit=true, matched_ppid=12345, session_file_exists=true, sessionId=abc...`。
- **fail**：上述任一环节断裂 → 标 fail，evidence 精确指出断在哪一步（这是当前 doctor 完全不暴露的信息）。
- **fix_hint**：见 §B-H2。

#### H3 · adapter_discovery

- **动作**：复用 `_make_adapter(backend, host_session, family=cfg_family)`（sidechain_daemon.py:367）构造 adapter，再调 `adapter.discover_children(host_session, since_ts=0)`：
  - 列出 discovered children 列表；
  - 列出 root 目录下 `agent-*.jsonl` 总数 vs 伴有 `.meta.json` 的数量（暴露「显式 spawn 过滤」误杀可能）。
- **evidence**：`resolved_root=~/.cac/projects/.../subagents, root_exists=true, jsonl_count=3, with_meta_count=2, discovered=[taskA, taskB]`。
- **fail/unknown**：
  - root 不存在 → unknown（子 agent 尚未起，非故障）。
  - root 存在、jsonl 存在、但 `with_meta_count=0` → fail（宿主未给子代理写 meta.json，daemon 全跳过）。
  - root 存在但 resolved 路径与实际数据路径不符（如 family 探测到 cac 但数据真在 cc）→ fail。
- **fix_hint**：见 §B-H3。

#### H4 · daemon_progress（关键：补 §8#4 的「存活但持续 iterate 失败」覆盖）

- **思路**：daemon 「活着」不等于在推进。真正的活信号是 **tape 里出现了 `agent_*` 事件**。
- **动作**（仅当 `--run-id` 给定）：
  1. `_sidechain_daemon_alive(run_id)` → pidfile/cmdline 活探（复用既有 helper）。
  2. 读 `<rundir>/<run_id>/tape.jsonl` 末尾 200 行，统计 `agent_*` 事件数 + 最新一条的 `timestamp`。
  3. 比对：adapter_discovery 跳发现有 N 个 child + 每个 child jsonl 在磁盘上已有 M 行 ↔ tape 里 source_id 命中前缀为 `<task_id>:` 的事件数。差值大 = daemon 漏推。
- **evidence**：`daemon_alive=true, agent_events_in_tape=12, last_agent_event_age_s=4, disk_jsonl_lines=15, gap=3`。
- **status**：
  - `gap==0 且 agent_events>0` → pass。
  - `agent_events==0 且 disk_jsonl_lines>0 且 run_age_s>10` → fail（daemon 在吞异常 / cursor 卡）。
  - `daemon_alive=false` → fail（与既有 `_check_sidechain_daemon_liveness` 一致，但带 fix_hint）。
- **诊断 daemon 异常吞错**：读 daemon stderr（`/tmp/orca-sidechain-*.log` 若存在；若 cli spawn 时未重定向则此步降级为 unknown + hint 启用日志重定向）grep `iteration 异常`，命中数 > 0 → 在 evidence 里显式列出。
- **fix_hint**：见 §B-H4。

#### H5 · bus_flow（订阅者队列溢出）

- **动作**：bus 在 daemon 进程内，doctor 跨进程无法直接观测 queue depth。采用两路取证：
  1. **日志取证（best-effort）**：grep daemon stderr `订阅者队列满` warning 计数（H4 同源日志）。
  2. **结构推断**：H6 的合成事件若能秒级到达 WS → bus_flow 隐含 pass；若 H6 超时但 tape（H4）有事件 → bus_flow 嫌疑（与 ws_delivery 联合判读）。
- **status**：日志命中队列满 warning → fail；否则 unknown（不单独 fail，交给 H6 综合判定）。
- **fix_hint**：见 §B-H5。

#### H6 · ws_delivery（端到端活探：连 /ws + 注入合成事件 + 等收 + 超时判定）

**这是覆盖「事件能从前端看到吗」的最关键一跳，要做真活探。**

- **两种模式**：

  **(a) 自起 ephemeral web server（默认，不依赖用户在跑 web）**：
  1. 在 doctor 进程内 `RunManager.start_run()` 一个名为 `__probe__` 的临时 run（用最小 workflow fixture），拿 `RunHandle`。
  2. 起 `FastAPI + WebServer(manager)` + `uvicorn.Server` on `127.0.0.1:<ephemeral_port>`（复用 `server.py` 的 `create_app` 工厂；ephemeral port 由 OS 分配，避免端口冲突）。
  3. 用 `websockets` 库（已存在于 `tests/iface/web/test_playwright.py` 依赖链）连 `ws://127.0.0.1:<port>/ws`，发 `{"type":"subscribe", "run_id":"__probe__"}`。
  4. `await handle.bus.emit("agent_message", {"text":"__probe__", "source_id":"__probe__:0:0"}, session_id="__probe__")` 注入合成事件。
  5. 在 WS client 端 `asyncio.wait_for(read, timeout=3.0)` 等收带 `source_id="__probe__:0:0"` 的事件。
  6. finally：cancel WS client + `manager.stop_run("__probe__")` + uvicorn shutdown。

  **(b) 探测用户在跑的 web server（`--ws-url` 显式给）**：
  - 连既存 `/ws`，发 subscribe(用户 run_id)，但**无法注入合成事件**（外部进程拿不到该 run 的 bus 句柄）。
  - 改为 **passive listen N 秒**：等待该 run 的下一条 agent_* 事件到达；收到 → pass；N 秒内无 → unknown（可能就是没新事件）。
  - 用途有限，主要给「web 在跑、就想看 WS 通不通」的场景。默认走 (a)。

- **判定**：
  - mode (a) 3s 内收到合成事件 → pass。
  - mode (a) 超时 → fail（pump 异常 / WS 未订阅成功 / bus.subscribe 未挂）。
  - mode (a) WS 连接直接拒绝 → fail（web server 没起 / 端口错）。
- **evidence**：`mode=self-spawn, ws_connect=true, subscribe_ack_via_state=true, event_received=true, latency_ms=42`。
- **fix_hint**：见 §B-H6。

### A.3 输出形态

```json
{
  "ok": true,                          // 既有 6 check 的 ok，不变
  "push_chain_probe": {                // 新增（仅 --probe-push 时）
    "overall": "fail",                 // 6 跳中任一 fail → fail
    "first_break": "H2_cac_pid_walk",  // 链路上第一个 fail/unknown 的跳（定位入口）
    "hops": [ {hop, status, evidence, reason, fix_hint}, ... ]
  },
  "report": "...人类可读..."           // 既有 report 字段
}
```

`first_break` 字段：链路顺序敏感（H1 断了后面全断），诊断在 LLM 友好性上的关键设计——主 session 读 `first_break` 就知道聚焦点。

---

## B. 修复指引如何面向主 session（LLM agent）

### B.1 写作原则

`fix_hint` 必须满足：
1. **可执行**：明示具体 bash/命令/配置改动，不只说「检查 X」。
2. **可验证**：附「改后如何验证」（通常是「再跑 `orca doctor --probe-push`」）。
3. **带前提分支**：不同原因给不同动作（LLM 读 evidence 字段选定）。
4. **简短**：单条 ≤ 3 行；长指引拆 step。

### B.2 文案示例（逐跳）

**H1 family_detect fail（CODEAGENT 缺）**：
```
fix_hint:
  在派生 orca 子进程前注入 env：`export CODEAGENT=1`（CAC 前端需要此变量让 orca 判定家族为 cac）。
  若你在非 CAC 环境（纯 claude code），确认 CLAUDE_CODE_SESSION_ID 已被 CC 自动注入；缺则说明你不
  在 CC bash 子进程内，sidechain 不适用。验证：`orca doctor --probe-push` 看 H1=pass。
```

**H2 cac_pid_walk fail（PID 链断）**：
```
fix_hint:
  PID 链回溯未命中 codeagentcli。常见原因：sidechain daemon 被 setsid/nohup 孤儿化脱离了 CAC 进程树。
  动作：改用 `python -m orca.iface.in_session.sidechain_daemon ... &`（不 detach）自 CAC bash 子进程
  启动；或显式给 daemon `--host-session <sessionId>` 绕过 PID 回溯（sessionId 从
  ~/.cac/sessions/<cac_pid>.json 的 sessionId 字段手取）。验证：H2=pass。
```

**H2 cac_pid_walk fail（session json 命名/内容不符）**：
```
fix_hint:
  命中 codeagentcli pid=<X> 但 ~/.cac/sessions/<X>.json 不存在或无 sessionId 字段。
  动作：`ls ~/.cac/sessions/` 看实际文件名格式；若 CAC 版本换了存储路径，需更新
  orca/iface/in_session/_hostenv.py 的 sessions_dir 常量（或给 daemon 显式 --host-session）。
```

**H3 adapter_discovery fail（root 不存在）**：
```
fix_hint:
  resolved root 不存在：~/.cac/projects/<enc>/<host_session>/subagents。
  可能：① 当前 session 还没派过子 agent（正常，H3 应判 unknown 而非 fail，若误判请报 bug）；
  ② family 探测错（H1/H2 fail 时 H3 路径也错）→ 先修 H1/H2；
  ③ 当前 cwd 与 CC/cac 启动时 cwd 不一致导致 <encoded-cwd> 算错 → `cd` 回正确目录再跑。
```

**H3 adapter_discovery fail（无 meta.json）**：
```
fix_hint:
  发现 N 个 agent-*.jsonl 但 0 个 agent-*.meta.json。daemon 会全部跳过（防系统子代理污染）。
  动作：确认你的子 agent 是经主 session Agent tool 显式 spawn 的；若是，宿主未写 meta.json
  是协议变更 → 在 cc_jsonl.py:190 临时放宽过滤（或改读 parent_session_id）。验证：H3 发现 child。
```

**H4 daemon_progress fail（daemon 存活但 tape 无 agent_* 事件）**：
```
fix_hint:
  daemon 在跑但 tape 里 0 条 agent_* 事件 → 持续 iterate 失败（§8#4 覆盖）。
  动作：① 看 daemon stderr `/tmp/orca-sidechain-*.log`（若无，下次 spawn 时重定向：
     `python -m orca.iface.in_session.sidechain_daemon ... 2>/tmp/sd.log &`）；
  ② grep "iteration 异常"，看堆栈；
  ③ 最常见根因：family 错（H1/H2 fail）→ adapter 读不到文件；或 _FlockSafeTape 拿不到锁
     （cli.next 持锁中，正常情况 daemon 会重试，若长阻塞 → 查是否有孤儿 next 进程）。
  立即缓解：`orca next --run-id <id>`（不传 output）触发 respawn，再观察 H4。
```

**H5 bus_flow fail（队列溢出）**：
```
fix_hint:
  订阅者队列（1024）溢出丢老事件。原因：WS pump 消费过慢（前端断连后 pump 异常 / 网络阻塞）。
  动作：① 检查前端是否长期挂着未关（断开重连会 resub）；
  ② 历史事件在 tape 完好，前端可经 resume(since=last_seq) 补全（D6 watchdog 配套）；
  ③ 持续溢出 → 调大 bus.subscribe(queue_max) 参数（ws_handler.py:199）。
```

**H6 ws_delivery fail（合成事件未到达 WS）**：
```
fix_hint:
  bus → WS pump 链路断。
  动作：① 看 stderr grep "ws pump" / "ws_endpoint 异常"（pump 抛非 disconnect 异常会静默退出）；
  ② 最常见：pump task 被 cancel 但 _subs 未清（ws_handler.py 断开清理竞态）→ 重启 web server；
  ③ 若是自起 ephemeral 模式超时 → web/uvicorn 启动失败，看 server.py stderr。
```

---

## C. 快速 e2e 冒烟测试设计

**目标**：in-session 派子 agent → 开 web → 验证推送到达前端。**快**（不烧模型 / 不依赖真 ~/.claude）。

### C.1 双形态（默认 fast）

| 形态 | 是否烧模型 | 覆盖深度 | 用途 |
|---|---|---|---|
| **fast（默认 CI）** | 否 | daemon → bus → WS 真链路 | 每次回归跑 |
| **realistic（手动）** | 是（deepseek-v4-flash） | 含真 CC/cac 写 jsonl | 发版前手测 |

### C.2 fast 形态实现

**核心技巧**：用 `ORCA_CC_SIDECHAIN_ROOT` env 把 sidechain root 指到 tmpdir（`cc_jsonl.py:78` resolver 第一优先级 source="env"），写一个假 `agent-<task_id>.jsonl + agent-<task_id>.meta.json`，daemon 会当成真子 agent ingest——**零模型调用，纯文件驱动**。

**步骤**：

1. **bootstrap 一个 run**：复用 `tests/e2e_redesign/tars_harness.py:bootstrap_run(wf_name, inputs)`（单节点 workflow，如 `tests/e2e_redesign/contract.py:WORKFLOWS` 里的最小 wf）。拿到 `run_id` + tape path。
2. **准备 tmp sidechain root**：
   ```python
   root = tmp_path / "subagents"
   root.mkdir()
   task_id = "probe-task-0001"
   (root / f"agent-{task_id}.jsonl").write_text(json.dumps({
       "type": "assistant",
       "message": {"content": [{"type": "text", "text": "hello from smoke"}]},
   }) + "\n")
   (root / f"agent-{task_id}.meta.json").write_text(json.dumps({"agentType": "probe"}))
   ```
3. **起 sidechain daemon**（同 cli._spawn_sidechain_daemon 的 argv，但加 `--host-session <bootstrap 的 host_session>` + 设 `ORCA_CC_SIDECHAIN_ROOT=<tmp>` env）—— detach spawn。
4. **起 web server**：`tests/iface/web/test_playwright.py` 已有 base_url fixture 模式可借鉴；直接调 `orca.iface.web.server.create_app(manager)` + `uvicorn.Server` ephemeral port。或者更简：复用 `orca run --web` 子命令如果存在（需确认，grep 看 server 启动入口）。
5. **WS client 等收**：
   ```python
   async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
       await ws.send(json.dumps({"type": "subscribe", "run_id": run_id}))
       deadline = asyncio.get_event_loop().time() + 5.0
       while asyncio.get_event_loop().time() < deadline:
           msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
           if msg.get("type") == "agent_message" and msg.get("session_id") == task_id:
               return PASS
       return FAIL
   ```
6. **teardown**：stop daemon（pidfile unlink 或 SIGTERM）+ `orca stop --run-id <run_id>` + tmpdir 清理。

**判定通过**：5s 内 WS 收到 `agent_message` 且 `session_id == task_id`。

**保持快的关键**：
- 零模型调用（纯文件）。
- daemon poll_interval=0.1（测试 argv override）。
- ephemeral port 避免冲突。
- 单节点 workflow（bootstrap → 首节点即终态，walk_dag 一步 done）。
- 预期 < 3s 全程。

### C.3 realistic 形态（手动 / 发版前）

- 不设 `ORCA_CC_SIDECHAIN_ROOT`（用真 `~/.claude` 或 `~/.cac`）。
- 子 agent 后端用 **deepseek-v4-flash**（CLAUDE.md 测试后端约定），不用 claude（省成本）。
- 经 `tars_harness.sentinel_e2e_run` 或直接 `MockSubagentBackend` 驱动 `tars_loop.drive_workflow`（已在 `tests/spike_ask_user/` 跑通）。
- 验证：前端打开 web，眼睛看子 agent 消息是否到；或 WS client 同 C.2 步骤 5。

### C.4 落脚点

新文件 `tests/iface/in_session/test_push_chain_smoke.py`（fast 形态）+ 可选 `tests/e2e_phaseNN/test_push_chain_realistic.py`（realistic 形态，标 `@pytest.mark.manual`）。

---

## D. 落地拆分

> 原则：纯诊断（只读，低风险）先 ship；e2e 测试（起服务）后 ship。每步独立 commit + 自带 code-reviewer。

### Step 1 — 纯诊断 H1/H2/H3（只读，零新进程）【S】

- 实现：`_probe_push_hops_h1_h2_h3()` 在 `cli.py`（或抽到 `iface/in_session/_push_probe.py` 避免 cli.py 继续膨胀——推荐后者，simplicity first 但 cli.py 已 2000+ 行）。
- 复用：`detect_family_from_env` / `detect_backend_from_env` / `cac_session_id_from_pid` / `_make_adapter` 全复用，零重写。
- 依赖：仅 iface 层 + events 层 adapter（单向依赖铁律不破）。
- **验收**：`orca doctor --probe-push` 输出 H1/H2/H3 三跳，evidence 字段含上述所有中间态；fake `CODEAGENT` 缺失时 H1=fail 且 fix_hint 完整。
- **风险**：低（纯读 env / /proc / 文件系统）。

### Step 2 — 纯诊断 H4（读 tape + 读 daemon log）【S】

- 实现：`_probe_push_hop_h4(run_id)` —— 仅当 `--run-id` 给定时跑。读 tape 末尾 200 行统计 agent_* 事件（复用 `events.tape.read_last_complete_lines`）+ 读 daemon stderr（路径约定：spawn 时 `2>/tmp/orca-sidechain-<short>.log`）。
- **前置小改**：`cli._spawn_sidechain_daemon` 把 stderr 重定向到 `/tmp/orca-sidechain-<short>.log`（目前可能未重定向 → H4/H5 日志取证降级 unknown）。这是 Step 2 的 sub-step 2a。
- **验收**：daemon 存活但无 agent_* 事件 → H4=fail 且 evidence 含 `agent_events_in_tape=0, disk_jsonl_lines=N`；日志可读时 evidence 含 `iteration_errors_count`。

### Step 3 — 纯诊断 H5（bus_flow，best-effort 日志取证）【XS】

- 实现：`_probe_push_hop_h5()` —— grep daemon log `订阅者队列满` warning 计数。命中 → fail；未命中 → unknown（不单独 fail）。
- **验收**：构造一个慢消费场景（fake test）→ H5=fail；正常 → unknown。
- **依赖**：Step 2a（日志重定向）。

### Step 4 — 端到端活探 H6（self-spawn ephemeral web）【M】

- 实现：`_probe_push_hop_h6_self_spawn()` —— 起 ephemeral FastAPI+uvicorn+RunManager，注入合成 agent_message，WS client 等收。
- 复用：`orca.iface.web.server.create_app` / `RunManager.start_run` / `EventBus.emit` / `websockets` 库（test deps 已有）。
- **关键风险**：起 uvicorn in-process 在 doctor 命令（typer/sync）上下文里需 `asyncio.run`；要确保 finally 清理（uvicorn.Server.shutdown + RunManager.stop_run），否则端口/tape 残留。
- **验收**：H6 self-spawn 模式 3s 内收到合成事件 → pass；故意挂掉 pump（mock）→ fail。
- **隔离**：所有探测资源用 `__probe__` 前缀（run_id `__probe__`、tmpdir `orca-probe-*`），与用户 run 不混淆。

### Step 5 — `--ws-url` 模式（被动监听）【XS】

- 实现：`_probe_push_hop_h6_passive(ws_url, run_id)` —— 连既存 /ws，passive listen N 秒。
- **验收**：手动起 `orca run --web` + 派子 agent，`--probe-push --ws-url ws://... --run-id <id>` 收到事件 → pass。

### Step 6 — e2e 冒烟测试（fast 形态）【M】

- 新文件 `tests/iface/in_session/test_push_chain_smoke.py`，按 §C.2 步骤实现。
- 复用 `tars_harness.bootstrap_run` / `events.tape.read_last_complete_lines` / `websockets`。
- **验收**：CI 跑通，< 5s 完成；故意不写 meta.json → 测试 fail（验证它真在校验推送而非假绿）。

### Step 7 — e2e 冒烟测试（realistic 形态，manual mark）【S】

- 标 `@pytest.mark.manual`，发版前手跑。
- deepseek-v4-flash 后端（CLAUDE.md 约定）。

### 依赖图

```
Step 1 (H1/H2/H3) ──┐
Step 2a (log redirect) ──┬→ Step 2 (H4) ──┬→ Step 3 (H5)
                         │                 │
                         └─────────────────┴→ Step 4 (H6 self-spawn) → Step 5 (H6 passive)
                                                                       │
                                                                       └→ Step 6 (smoke fast) → Step 7 (smoke realistic)
```

Step 1 / 2a 可并行；Step 6 可在 Step 4 后即开工（不依赖 Step 5）。

---

## E. 风险点 / 待拍板

1. **H6 self-spawn 的隔离性**：在 doctor 进程里起 uvicorn + RunManager + 写 tmp tape —— 若清理不彻底会留孤儿进程 / 占端口。必须用 try/finally + 进程组 cancel 兜底；考虑用 `multiprocessing.Process` 隔离（子进程崩不影响 doctor 主进程）。
2. **H4 daemon stderr 读取**：跨进程读另一个进程的 stderr 文件，若 daemon 还在写 → partial read。对策：读最近 N 行容错（`read_last_complete_lines` 已有），不做强一致。
3. **fix_hint 的维护成本**：随代码演化文案会过时。建议把 fix_hint 抽到 `_push_probe.py` 的常量表（dict），与对应 hop 函数同文件，code-reviewer 一并审。
4. **realistic 冒烟的 flake**：deepseek-v4-flash 偶发慢 → WS 等收超时阈值要给足（建议 15s，远大于 fast 的 5s）。
5. **是否进 doctor 还是独立命令**：本稿选 `doctor --probe-push` 子模式（接口铁律「7 命令不增不减」）。若用户更想要 `orca diagnose --push-chain` 独立命令——**待拍板**（会破 SPEC §1 铁律 5，需 ADR）。
6. **H3 「root 不存在」是 unknown 还是 fail**：当前 doctor 把它算 fail（`available=False`）。本稿建议改 unknown（子 agent 尚未起不是故障），但**待拍板**（会改既有 check 语义）。

# 推送链路排障 runbook（`doctor --probe-push`）

> **入口**：`orca doctor --probe-push`（无 `--probe-push` 时 doctor 输出与基线一致，零副作用）。
>
> doctor 在输出 JSON 追加 `push_chain_probe` 区块：`overall` / `first_break` / `hops[6]`。
> `first_break` = 链路顺序首个非 `pass` 的跳——主 session 聚焦它即可（链路顺序敏感：
> H1 断 → H2/H3 必然跟着无意义）。每跳 `fix_hint` 是本文件对应节的快速指针（MD 是真相源）。
>
> **用法**：拿到 `first_break=H<N>_<slug>` → 下表找对应节 → 按「修复动作」执行 → 重跑
> `orca doctor --probe-push` 验证。
>
> **当前实现阶段**：S1 已落地 H1/H2/H3（家族识别 + PID 回溯 + adapter 发现）；H4/H5/H6
> 为占位（`status=unknown, evidence=not_implemented`），由 S2/S3 接入。当前阶段
> `first_break=H4_daemon_progress` 是预期（占位），不代表链路真断——H1-H3 pass 即算成功。

| 跳 | 锚点 | 问的问题 |
|---|---|---|
| [H1 family_detect](#h1-family-detect) | `#h1-family-detect` | 当前被认成什么 backend/family？ |
| [H2 cac_pid_walk](#h2-cac-pid-walk) | `#h2-cac-pid-walk` | CAC PID 链能否回溯 + session json 在不在？ |
| [H3 adapter_discovery](#h3-adapter-discovery) | `#h3-adapter-discovery` | adapter discover 到子进程？root/meta.json 齐不齐？ |
| [H4 daemon_progress](#h4-daemon-progress) | `#h4-daemon-progress` | daemon 存活且真在推进？ |
| [H5 bus_flow](#h5-bus-flow) | `#h5-bus-flow` | bus 队列有没有溢出丢事件？ |
| [H6 ws_delivery](#h6-ws-delivery) | `#h6-ws-delivery` | bus→WS pump 通吗？ |

---

## H1 family_detect {#h1-family-detect}

### 症状

doctor 输出 H1 `status=fail` 或 `status=unknown`：

- `fail`：`backend=cc, family=None, source=...`——CC env 已命中但 family 探测失败。
- `unknown`：`backend=None, family=None, source=none`——非 in-session 环境。

### 根因

推送链路按 backend / family 选 adapter（CCJsonl vs OpencodeSqlite）和 dotdir（`~/.claude`
vs `~/.cac`）。env 是 family 的单一真相源（`detect_family_from_env()`）；env 缺失时 adapter
走 config/probe 兜底，可能选错 dotdir——常见于：

- 真 Claude Code：`CLAUDE_CODE_SESSION_ID` 未注入到 bash 子进程（用户从外部 shell 启动 Orca）。
- CAC 换皮：`CODEAGENT=1` 在但 PID 链未命中 codeagentcli（daemon 被 setsid 孤儿化）。
- opencode：`ORCA_HOST_SESSION_ID` 未注入（缺 plugin `shell.env` 钩子）。

### 修复动作

- **真 CC**：确认主 session 把 `CLAUDE_CODE_SESSION_ID` 注入到 bash 子进程。从 daemon 父
  shell 启动 Orca（不要从外部脚本 `setsid` / `nohup` 孤儿化）。
- **CAC**：确认 CAC 主进程（`codeagentcli`）是 daemon 的祖先；若被 `setsid` 孤儿化，
  改自 CAC bash 子进程启动 daemon；或显式 `ORCA_HOST_SESSION_ID=<sid>` 绕过 PID 回溯。
- **opencode**：在 plugin `shell.env` 钩子注入 `ORCA_HOST_SESSION_ID`；或显式给
  `--host-session`。
- **任意家族**：config `sidechain.family` 显式设为 `cc` / `cac` / `opencode` / `nga`
  （覆盖探测兜底，避免歧义）。

### 验证

重跑 `orca doctor --probe-push`，确认 H1 `status=pass`，`first_break` 移到下一跳或为 null。

---

## H2 cac_pid_walk {#h2-cac-pid-walk}

### 症状

doctor 输出 H2 `status=fail`，evidence 形如：

- `CODEAGENT=0`：未设 `CODEAGENT` env。
- `pid_walk_hit=false`：20 跳 PID 链未命中 `codeagentcli`。
- `session_file=false`：`~/.cac/sessions/<ppid>.json` 不在。
- `session_file_has_sessionId=false`：session json 在但无 `sessionId` 字段。

### 根因

CAC 不把 `sessionId` 写 `process.env`（存在内存变量 `eZ.sessionId`），bash 子进程继承不到。
Orca 经 `/proc` 回溯找 CAC 主进程 PID，再从 `~/.cac/sessions/<pid>.json` 读 `sessionId`。
任一环节断裂即拿不到 host_session，daemon 无 scope 不会 ingest 子 agent 事件。

### 修复动作

- **PID 链断**（`pid_walk_hit=false`）：daemon 被 `setsid` / `nohup` 孤儿化脱离 CAC 进程树。
  改自 CAC bash 子进程启动 daemon（让 CAC 主进程是 daemon 的祖先是必要条件）。
- **session 文件缺**（`session_file=false`）：CAC 写文件命名变 / 被清；查
  `ls ~/.cac/sessions/` 看 `<ppid>.json` 是否在；不在则重启 CAC 让它重建。
- **sessionId 字段缺**（`session_file_has_sessionId=false`）：CAC 写文件格式漂移；
  暂用 `ORCA_HOST_SESSION_ID=<sid>` 显式注入绕过 PID 回溯（`<sid>` 从 CAC UI 复制）。
- **绕过方案**：显式 `--host-session=<sid>` 启动 daemon（spawn argv 透传，单一真相源）。

### 验证

重跑 `orca doctor --probe-push`，确认 H2 `status=pass`，evidence `authority_session_id=<sid>`。

---

## H3 adapter_discovery {#h3-adapter-discovery}

### 症状

doctor 输出 H3：

- `status=fail`：`root_exists=true, jsonl_count>0, with_meta_count=0`——jsonl 在但全无
  `.meta.json`（daemon 全跳过这些子 agent）。
- `status=unknown`：`root_exists=false` 或 `jsonl_count=0`——子 agent 尚未起，非故障。

### 根因

daemon 只 ingest 伴 `.meta.json` 的子 agent（主 session Agent tool 显式 spawn 的）；宿主
后台系统子代理（如 CAC `asession_memory-*` memory helper）无 `.meta.json` 被跳过——这是
设计（防污染 workflow tape）。若主 session spawn 的子代理也无 `.meta.json`（宿主前端写
meta 逻辑故障），daemon 全跳过 → 子 agent 消息进不了 web。

### 修复动作

- **无 meta.json**：查宿主前端是否真为主 session Agent tool spawn 的子代理写 meta.json。
  若宿主版本不写：升级宿主 / 改 daemon discover 判据（如读 `parent_session_id`）。
- **root 不存在**：确认 sidechain root 路径正确（env `ORCA_CC_SIDECHAIN_ROOT` / config
  `sidechain.family` / 默认 `~/.claude`/`~/.cac`）。从 doctor `sidechain_backend` check 拿
  resolved 路径。
- **root 在但 jsonl_count=0**：主 session 还没 spawn 子 agent；等 spawn 后再查。

### 验证

重跑 `orca doctor --probe-push`，确认 H3 `status=pass`，`discovered_children` 非空。

---

## H4 daemon_progress {#h4-daemon-progress}

### 症状

doctor 输出 H4 `status=fail`，evidence 含 `daemon_alive=true/false`、`agent_events_in_tape=N`、
`disk_jsonl_lines=N`、`gap=N`、`last_agent_event_age_s=N`、`iteration_exceptions=N`。

常见 fail 形态：
- `daemon_dead`：守护死了（pidfile 残 / pid 死 / cmdline 不匹配）。
- `disk_jsonl_lines>0 且 agent_events==0 且 run_age>30s`：子 agent 在产但 daemon 一条没
  ingest（持续 iterate 失败 / cursor 卡）。
- `gap>0 且 run_age>30s`：disk 比 tape 多——漏推。
- `iteration_exceptions>0`：daemon log 有 `sidechain driver iteration 异常` warning。

### 根因

daemon 存活 ≠ 在推进。SPEC §8#4 的盲区：守护存活但持续 iterate 失败（adapter/ingestor 抛
异常被 `except Exception` 吞，每次 sleep 后重试，cursor 不推进）。H4 通过读 tape agent_*
事件数 + 磁盘 jsonl 行数算 gap，+ 读 daemon log grep iteration 异常，覆盖该盲区。

### 修复动作

- **daemon_dead**：下次 `orca next` 会自动 respawn；或显式调一次 next 拉起。
- **gap>0 / agent_events==0**：查 daemon log `<rundir>/<run_id>/sidechain_daemon.log` 的
  iteration 异常 traceback；常见是 adapter stream 解析失败 / ingestor schema 不匹配。
  按 traceback 修源 bug 后 `orca next` 触发 respawn。
- **iteration_exceptions>0 但 gap==0**：transient 错误已自愈（重试成功）；观察不增即可。
- **last_agent_event_age_s>30**：子 agent 卡住不产事件；查子 agent 进程而非 daemon。

### 验证

重跑 `orca doctor --probe-push --run-id <id>`，确认 H4 `status=pass`（gap==0 且
agent_events>0 且 age<30 且 iteration_exceptions==0）。

---

## H5 bus_flow {#h5-bus-flow}

### 症状

doctor 输出 H5 `status=fail`，evidence 含 `queue_full_warnings=N`（从 daemon log grep
`订阅者队列满` warning 计数）。`status=unknown` 表示 log 中无该 warning（无证据）。

### 根因

慢订阅者（WS pump）消费速度跟不上 emitter → bus 队列满 → 丢老事件腾位（实时性优先，订阅者
靠 replay 补全）。短期偶发可接受；持续溢出 = pump 链路严重卡（如 WS 客户端断开但 pump
未退出 / 网络反压）。

### 修复动作

- **偶发（<10/min）**：通常无害（订阅者 replay 补全）；可忽略。
- **持续**：查 WS 客户端连接状态——浏览器是否在 sleep / 网络反压；重启前端。
- **pump 异常**：结合 H6（H6 fail 会暴露 pump 异常路径）。

### 验证

重跑 `orca doctor --probe-push`，确认 H5 `status=unknown`（无新 warning）或不再增长。

---

## H6 ws_delivery {#h6-ws-delivery}

### 症状

doctor 输出 H6 `status=fail`：

- self-spawn 模式（默认）：3s 内未收到合成 `agent_message` 事件。
- passive 模式（`--ws-url` 给定）：连既存 `/ws` passive listen N 秒未收到真事件。

### 根因

bus → pump → WS 链路中任一环故障：
- pump 异常静默退出（`ws_handler._pump` 内部 try/except 吞异常 → warning 但不重起）。
- WS 未订阅（subscribe 消息未发 / run_id 不匹配）。
- bus 订阅者注册失败（罕见）。

H6 通过 self-spawn 一个临时 run + 注入合成事件 + WS subscribe 等收，端到端验证链路。

### 修复动作

- **pump 异常**：查 `<rundir>/<run_id>/sidechain_daemon.log` 或 web server stdout 的
  `ws pump（run=...）异常退出` warning；按 traceback 修源 bug。
- **WS 未订阅**：前端确认 `subscribe(run_id)` 消息发出且 run_id 正确。
- **端口残留**：连续两次 doctor self-spawn 第二次 fail——查 `__probe__` 前缀 run / 临时
  runs_dir 是否清理（doctor 用独立 tmp runs_dir 隔离，不应残留；若残留是 doctor bug）。

### 验证

重跑 `orca doctor --probe-push`，确认 H6 `status=pass`（self-spawn 3s 收到事件）。

---

## 守门测试

SPEC §5 三组守门测试在 `tests/iface/in_session/test_push_probe.py` 自动跑：

1. **锚点对应**：本文件每个 `{#h<N>-<slug>}` 锚点与 `_push_probe._HOP_ORDER` 一一对应。
2. **fix_hint 指针有效**：每个 hop 的 `fix_hint` 引用的锚点在本文件锚点集合内。
3. **H2 中间态自洽**：H2 中间态复算（PID 链命中 + session 文件）与 `cac_session_id_from_pid()`
   返回值自洽。

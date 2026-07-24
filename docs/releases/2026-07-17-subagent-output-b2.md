# B2 子 agent 过程推送 web（双 adapter：CC sidechain jsonl + opencode sqlite）

> 2026-07-17。SPEC-B v4 `docs/specs/2026-07-17-subagent-output-to-web-design-draft.md`（spec-reviewer conditional-pass 5 BLOCKER 全闭 R1-R7 + 4 决策 U1-U4）。
> 解用户痛点：B1 已让节点 output 文字进 web，但**子 agent 执行过程中的 msg/tool/thinking** 仍不可见 → 用户看不到子 agent 在做什么。
> B2 = 回合级实时（≤~2s）把子 agent 过程经 tape `agent_*` → follow_task → WS → 前端（前端零改，复用 B1 + entries.ts 既有 agent_* 渲染）。

## 设计要点（v4 锚定）

- **统一 IR RawAgentEvent**（payload 1:1 = EventType.data，R1）= 两 adapter 共同产出。
- **双 read-adapter**（接口同一性 + grep 守门，SPEC §0/§9 AC5）：
  - CC：`~/.claude/projects/<encoded-cwd>/<host_session>/subagents/agent-<task_id>.jsonl` tail。
  - opencode：sqlite `event` 表 seq 游标（**纠 v3 part 表**：part 行状态翻转时 id 不变 → 漏 tool 完成态）。
- **1:1 透传 ingestor + source_id 查重**（R2/R3）：内存 set O(1) 命中 skip；crash restart `rebuild_from_tape` 一次性扫全文重建。
- **U1 node 派生**（§6）：emit 前增量扫 tape 取最后 `node_started.node`；撤回 v3「天然归位」（spec-review D1 证伪）；per-run tape 无 multi-run race（H3）；诚实 ≤0.5s trailing 窗口。
- **R4 七组件逐字复刻 chart_daemon**：`_FlockSafeTape` + `_watch_terminal` + `_DEFAULT_TTL_SECONDS` import 复用（零 DRY）；crash callback + signal handler + pidfile liveness 新写。
- **唯一写路径**：agent_* 只经 `bus.emit` → `_FlockSafeTape`（跨进程 flock + `_read_max_seq_from_disk` 重置，与 cli.next 同锁）。

## 交付清单（commit `<SHA>`）

**新建**：
- `orca/events/raw_agent_event.py` —— IR + ReadAdapter Protocol。
- `orca/events/sidechain_ingestor.py` —— 1:1 + dedup + U1 派生。
- `orca/events/adapters/__init__.py` —— adapter 包 marker。
- `orca/events/adapters/cc_jsonl.py` —— CC sidechain jsonl adapter。
- `orca/events/adapters/opencode_sqlite.py` —— opencode sqlite adapter。
- `orca/iface/in_session/sidechain_daemon.py` —— daemon（driver + crash callback + main entry）。
- 测试：`tests/events/test_sidechain_ingestor.py` / `test_adapters_cc_jsonl.py` / `test_adapters_opencode_sqlite.py` + `tests/iface/in_session/test_sidechain_daemon.py`。

**surgical 编辑**：
- `orca/iface/in_session/cli.py`：加 `_detect_backend_from_env` / `_spawn_sidechain_daemon` / `_ensure_sidechain_daemon`；bootstrap 接线（`_spawn_chart_daemon` 后）；next 接线（`_ensure_chart_daemon` 后）。
- `tests/iface/in_session/conftest.py`：autouse 守护清理扩到 sidechain_daemon。

## 防御性 deviation from SPEC（已审，记录在案）

1. **CC source_id 扩展**：SPEC §4 字面 `f"{agentId}:{line_idx}"`（单 block 假设）→ 实际 `f"{task_id}:{line_idx}:{block_idx}"`，因 assistant 行可能多 content block（thinking + text + tool_use 同行），必须 disambiguate。
2. **opencode source_id 用 seq 而非 part.id**：SPEC §5 字面提 `part.id`（spike 假设 immutable），但 part 单行双状态（running→completed）时 part.id 不变 → source_id 必撞。seq 是 event 表连续整数、INSERT/UPDATE 各占一行 → 天然唯一。P2 spike E2 验证 part.id immutability 后可回调。
3. **_FlockSafeTape + _watch_terminal 从 chart_daemon import**（DRY）：SPEC R4 说「逐字复刻」，实现选择 import 复用而非拷贝，消除 DRY。

## 验证

- **单测**：13 ingestor + 21 CC adapter + 25 opencode adapter + 20 daemon = 79 新测全 PASS。
- **接口同一性 grep 守门**（SPEC §9 AC5）：0 hit。
- **端到端 daemon subprocess**：
  - spawn → 写 mock sidechain jsonl → tape 出 agent_message（实时 ≤2s，实测 ~0.5s）。
  - SIGKILL → respawn → tape source_id 唯一（幂等闭环）。
  - workflow_completed → daemon 自退（_watch_terminal 触发）。
- **U1 node 派生**：tape 有 node_started[A] → daemon emit 的 agent_* node=A；cli.next 推进 node_started[B] → 增量扫到 → 后续事件 node=B。
- **回归**：events/ + iface/in_session/ 全 352 测试 PASS。

## Defer / 已知缺口

- **opencode 真机 spike 未跑**（任务约束）：契约实现 + 单测 fixture DB 驱动覆盖；P2 实施时建议跑 spike 验 part.id immutability（E2）+ WAL commit 间隔 vs ≤2s（N4）。
- **multi-run same-host_session race**：SPEC §6 H3 假设「host_session 单 active run」；实际 CC 同 session 多 wf 并行会混 subagent。属已知限制（非 B2 scope），通过 in-session dupe check（cli bootstrap）部分缓解。
- **headless 浏览器验证未做**：同 B1 既有缺口（无 playwright/puppeteer）。react-dom/server 渲染 + bundle 守门已证 agent_* 渲染链在位（B1 已交付 + entries.ts:145-201 复用）。manual 确认步骤：`orca open <run>` → 派子代理 → web 看 thinking/message/tool-single/tool-group（按 session_id 归组）。

## 后续

- B3：`agent_usage`（token/cost）独立立项（U4 deferred，避免子/主 usage 双计致前端聚合错乱）。
- U1 升级路径：若 E2E 可见 node 错位（>0.5s trailing 窗口），升级 U1=(b) `node_completed.data.child_agents` + 前端 reducer 按 session_id 归位（race-free）。

## 修：test-agent 真机 E2E 收尾（3 P0 + 5 回归）

test-agent 真机 E2E（4435 真 CC `agent-*.jsonl` + 573 真 opencode `event` 表行 → 真 daemon subprocess → 真 tape → 真 `tars serve` HTTP → 真 react-dom 渲染）暴露原代码（`ed5cbeb`）3 个**单测盲区 P0**（79 单测全 PASS 但真机死），修复（`99efcde`）：

1. **opencode DB 路径错**（`opencode_sqlite._resolve_db_path`）：代码找 `session.db`，真机 v1.18 写 `opencode.db` → `discover_children` 静默返空 → ingest 0 事件。修：优先 `opencode.db`，回退 `session.db`。
2. **opencode `source_id` 跨 child 撞车**（5 处）：`opc:{seq}` 假设 seq 全局唯一，实际 event PK=`(aggregate_id, seq)`、seq **per-session** → 多 child 复用同 seq（真机 44% 撞）→ ingestor dedup 静默丢。修：`opc:{child_id}:{seq}`。
3. **text-mode seek 在多字节 UTF-8 tape 崩**（`sidechain_ingestor._derive_current_node` + `chart_daemon._FlockSafeTape._read_max_seq_from_disk` + `_watch_terminal`）：`seek(字节)/read(字符)` 混算 → offset 漂移到 UTF-8 continuation byte → `UnicodeDecodeError`（ValueError 子类，**非 OSError** 未兜住）→ 守护死。chart 历史 payload ASCII 未触发；B2 引入中文/emoji agent_* 必崩（真机崩在 8 事件后卡死）。修：三处 binary-mode（byte seek + `rfind(b"\n")` + `decode(errors="replace")`）。

**补 5 回归测试**：`test_adapters_opencode_sqlite` 加（opencode.db 路径优先 + 多 child source_id 唯一）；`test_sidechain_ingestor` 加（多字节 node 派生）；新 `test_chart_daemon_multibyte` ×2（多字节 max_seq）。fix 后 64（B2 events+回归）+ 7（chart）+ 20（daemon 集成）全 PASS；grep 守门 0 hit。

**test-agent V1-V10 全链路真机 PASS**：CC 4435 / opencode 573 事件经真 daemon → tape → `tars serve` → react-dom testid（thinking/message/tool-single/tool-group 齐全，零 `[object Object]`），实时 ≤1.0s、SIGKILL→respawn 幂等（4435==4435）、无串台（错 host_session→0 事件）、终态自退。**goal 验收「CC+opencode 子 agent 输出实时推前端」真机闭环**。

**降级声明**：未驱动真 `claude -p`/真 opencode 派真子 agent（用真实历史 CC jsonl + live opencode.db 替代，等同真实子 agent 输出，SPEC §9 AC7 允许）；无 headless 浏览器（react-dom/server + bundle testid 守门）。

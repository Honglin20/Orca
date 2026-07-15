# Plan: FU-3 —— `orca status`（无参）契约对齐：活跃 run + 结构化

> 来源：FU-1 期 test-agent 真机发现（status 无参列全部 tape 含 completed，与 SPEC「活跃」+ 结构化契约漂移）
> 状态：草稿（待 spec-reviewer）| 分支 `in-session-unified-backend` | 前置：FU-1 `73a47ea`

---

## 0. 目标与成功标准

`orca status`（无参）对齐 SPEC §2.1 / §2.3 + 自身 docstring：
1. **scope**：只列**活跃** run（marker `runs/orca-<id>.json` 存在），**不列** completed/failed/stopped（其 marker 已清，5a/stop 契约）。
2. **shape**：每条结构化 `{run_id, node, status, last_next_at, elapsed}`（SPEC §2.3），**非裸 stem**。
3. `--json` 与人类可读两种输出都对齐。
4. fail loud：无 runs/ 或无活跃 run → 明确空态提示（exit 0，非错误）。
5. 单测 + 真机 E2E（test-agent，**纯 CLI，禁 MCP**）：bootstrap 活跃 run → status 无参列出它（结构化）；完成后 → 不列。

---

## 1. 根因（cli.py:719-737）

```python
tapes = sorted(runs_dir.glob("*.jsonl"))   # 所有 tape，含 completed
names = [tp.stem for tp in tapes]          # 裸 stem，非结构化
... {"runs": names} ...
```
- **scope 错**：glob 所有 `*.jsonl` → completed run 也列。SPEC §2.1「无 run_id → 列全部**活跃** run」。
- **shape 错**：返 `{runs:[stem]}` 裸字符串。SPEC §2.3 `{runs:[{run_id, node, status, last_next_at, elapsed}]}`。
- docstring（L713）已正确写「列全部活跃 run」——是**代码没兑现 docstring/SPEC**。

---

## 2. 架构审视

- **单事实源**：marker 存在 = 活跃（已是 tape 完成契约：bootstrap 写 marker，completed/stop 清 marker，5a/FU-1 已验）。复用这个信号，**不新增活跃判定状态**。
- **单接口**：status 无参是 orca CLI 契约（SPEC §2.3），对齐它 = 接口兑现，非新增。
- **改前影响**：主 session / 监控调 `orca status`（无参）拿活跃 run 列表。从「裸 stem 全部」改「结构化活跃」= **契约收紧**（旧消费者若依赖列 completed 或裸 stem 会受影响）——但 SPEC/docstring 一直这么承诺，是**修 bug 非 breaking**（旧行为本就错）。migration note 按需。
- **改后清理**：删裸 stem 逻辑；docstring 已对，无需改（或微调措辞）。

---

## 3. 改动范围

### 3.1 `orca/iface/in_session/cli.py` status 无参分支（L719-737）
- 活跃 run 枚举：`markers = sorted(runs_dir.glob("orca-*.json"))` → 每 marker 派生 `run_id`（去 `orca-` 前缀 + 去 `.json`）。
- 对每活跃 run：读 marker（`{run_id, model, no_output_count}`）+ replay 其 tape（`replay_state`）取 `status`/`current_node`/`node_status`。
- **时间字段 `last_next_at` / `elapsed`**（SPEC 要）：从 marker 文件 mtime 派生（`last_next_at` ≈ marker mtime；`elapsed` ≈ now - marker ctime/mtime）。**不新增时间追踪机制**（marker 是唯一活跃态来源）。若 replay_state 已有更准的时间字段则优先用——**coder 实读 replay_state 确认可用字段，不许编造**。
- 输出：
  - `--json`：`{"runs":[{run_id, node, status, last_next_at, elapsed}, ...]}`。
  - 人类可读：每行 `- <run_id> [status] node=<current_node> elapsed=...`。
- 无活跃 run：`{"ok":False,"reason":"no active runs"}` / `(无活跃 run)`，exit 0。
- tape 存在但无 marker（completed）→ **不列**（这是修复核心）。

### 3.2 不动
- `status --run-id <id>`（单 run 详情，L739-763，已结构化）—— 不改。
- marker 格式、replay_state、stop/bootstrap 逻辑 —— 不改。

---

## 4. 测试（纯 CLI，禁 MCP）

- **单测**（`tests/iface/in_session/test_in_session_cli.py` 或 test_v8）：
  - 0 活跃 run（空 runs/ 或只 completed tape）→ 空态 exit 0。
  - 1 活跃 run（marker + tape）→ 列出，结构化字段齐（run_id/node/status/last_next_at/elapsed）。
  - 活跃 + completed 混合 → **只列活跃**，completed 不出现（核心回归）。
  - `--json` shape 断言（每元素是 dict 含 5 字段，非裸字符串）。
- **E2E（test-agent 真机，纯 orca CLI）**：
  - `orca bootstrap demo --inputs` → 活跃 run → `orca status`（无参）→ 列出它（结构化）。
  - `orca next` 推进到 done → `orca status`（无参）→ **不再列**（marker 清）。
  - `--json` 真机断言结构化。

---

## 5. 风险 / scope

- **R1（时间字段）**：`last_next_at`/`elapsed` 若 replay_state 无，用 marker mtime 派生（精度够用，marker 是活跃态唯一来源）。若 SPEC 字段无法准确取，**surface**（Rule 7）：要么用 mtime 近似 + 注释，要么提 SPEC 微调——coder/spec-reviewer 定，不许编造数据。
- **R2（旧消费者）**：若有代码/测试依赖无参返裸 stem 全部 tape，会断——SPEC/docstring 一直承诺活跃+结构化，属修 bug；grep 消费点（`orca status` 无参的断言）同步改。
- **scope**：只 status 无参。不动 --run-id 详情 / marker / replay / bootstrap-stop。不碰 MCP（已禁）。不重写 status。

---

## 流程闭环
本计划 → **spec-reviewer**（核实活跃判定用 marker 合理 + 时间字段 sourcing + scope）→ **coder-agent**（实现 + code-reviewer + 单测 + commit + 状态文档）→ **test-agent** 真机（纯 CLI：活跃列出 / 完成不列 / 结构化）。

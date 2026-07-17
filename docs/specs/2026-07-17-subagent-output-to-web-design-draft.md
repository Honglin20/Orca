# 子 agent 输出/过程推送 web —— 设计草稿（v2：B1 已交付 + B2 命门已解）

> **v2**（2026-07-17）：B1 已交付（[release](../releases/2026-07-17-subagent-output-b1.md)）；B2 命门（task_id 来源）经 spike 坐实 **A 路可行**（CC PostToolUse hook `agentId`）。
> **关联**：SPEC-A host_session（§4.6 env 契约，B2 复用）；plan `docs/plans/2026-07-17-subagent-output-b1.md`（B1）。
> **评审**：spec-reviewer 两轮（B1 conditional-pass→实现；B2 fail→命门已解待再设计）。

---

## B1 —— 已交付 ✅
前端渲染 `node_completed.data.output`（纯前端零后端）。实现 4 commits + 真机 PASS（13 节点含 9 dict 零 `[object Object]`）。详见 [release note](../releases/2026-07-17-subagent-output-b1.md)。**满足用户验收「子 agent 输出推送 web」**。

---

## B2 —— 命门已解（2026-07-17 spike 坐实）

### 现状
in-session 子 agent 过程（thinking / tool_use / tool_result）**不进 tape**（主 session 不经 ClaudeExecutor，无流式 hook）。过程存在宿主 sidechain：CC `~/.claude/projects/<cwd>/<host_session>/subagents/agent-<task_id>.jsonl`（spike 坐实，含 thinking+Bash tool_use）；opencode sqlite `session.parent_id`。

### 命门解决：task_id 来源（spec-reviewer U4/4a）
**A 路（驱动协议）spike 确认可行**：CC PostToolUse hook（`settings.json`→`hooks.PostToolUse`，matcher `Agent`）的 stdin JSON 含：
- `tool_response.agentId`（如 `a20028e65af2b573b`）= **子 agent task_id**（= sidechain 文件名）
- `session_id`（主 session = host_session）
- `tool_response.outputFile`（sidechain transcript symlink 真实路径，可直接读，免拼路径）
- `tool_use_id`（= meta.json toolUseId）

spike 实证：headless `claude -p --verbose` spawn 子 agent，PostToolUse 真触发（2 次：Agent spawn + 子 agent 内 Bash），`agentId` 可拿。

### 实现（5 步，单路 ingestor→tape，不破唯一真相源）
1. **PostToolUse hook**（`tars install --target cc` 生成 `hooks/orca-sidechain.sh`，matcher=Agent）：捕获 `agentId`+`session_id`+`tool_use_id` → append `runs/.orca-agent-spawn-<session_id>.jsonl`。
2. **`orca next` 读**：读该 session「上次 next 之后的 agentId」（按 timestamp）→ 定位 sidechain `agent-<agentId>.jsonl`（或直接读 hook 给的 `outputFile`）。
3. **ingestor**（新模块 `orca/events/sidechain_ingestor.py`，类比 `chart_ingestor.py`）：读 sidechain jsonl → 转 `agent_*` 事件（`thinking`→`agent_thinking` / `Bash tool_use`→`agent_tool_call` / `tool_result`→`agent_tool_result`）→ **幂等 key `data.source_id = f"{agentId}:{line_idx}"`**（emit 前查 tape 已含该 source_id 则跳过）。
4. **写 tape**：EventBus → Tape.append（单一写路径）。
5. **web 零改**：复用现有 `agent_*` 渲染器（B1 后 `entries.ts` 已支持 message/tool-single/tool-group/thinking）。

### 余 4 设计洞闭环（spec-reviewer）
| 洞 | 闭环 |
|---|---|
| 幂等 key（6e，BLOCKER） | `source_id=agentId:lineIdx` + emit 前查重 → 防 crash/re-trigger 重复 ingest |
| 字段映射（4c/2） | CC sidechain jsonl 真实样例（spike 有）→ 逐字段映射表；CC/opencode 两 adapter 共享同一映射 spec（落实「无多套接口」） |
| flush 时序（4b） | next 时子 agent 已跑完、sidechain 完整；撕裂/partial 行 skip+warn（同 chart_ingestor readline 防护思路） |
| opencode scope（4d） | **defer**（CC 先，用户已允）；sqlite parent_id adapter 后续 |

### SoT 论证（不破唯一真相源）
- sidechain = **数据源**（input，CC 运行时产物）；ingestor = **确定性转换**（读 jsonl→结构化 `agent_*`，无模型）；tape = **唯一真相源**（output，append-only）。
- **幂等 key** 防 re-ingest 重复（`chart_ingestor` 类比破裂的机制解；chart 是 best-effort 可视化，agent 过程同理 best-effort 投影——用户「有记录即可」）。
- 单路（ingestor→tape），不构成「两路独立采集可发散」（SPEC-A §3.4 定义）。**不触发停止铁律**。

### host_session 范围扩张（spec-reviewer U3）
B2 用 host_session 定位 sidechain（`<host_session>/subagents/`）。SPEC-A「host_session 仅作用 nudge」需**勘误扩为「宿主 session 身份，多消费者（nudge + sidechain 定位）」**。host_session 天然是宿主身份，语义自洽。

### 验收（待实现）
1. in-session wf，子 agent thinking / tool_use 在 web 显示。
2. tape 含 `agent_*`（ingestor 转入），**幂等**（re-next 不重复）。
3. dict/string output（B1）+ 过程（B2）都显示。
4. CC adapter 必须；opencode defer（open issue）。

---

## 决策清单（v2）
1. **B1 已交付**（output 显示，满足用户验收）。
2. **B2 命门 task_id = PostToolUse hook `agentId`**（spike 坐实 A 路）。
3. **B2 单路 ingestor→tape**（幂等 key），不破 SoT，best-effort 投影。
4. **host_session 勘误扩用途**（U3：nudge + sidechain 定位）。
5. **opencode defer**（CC 先）。

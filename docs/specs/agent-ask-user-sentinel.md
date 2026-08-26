# Agent Ask-User 哨兵契约(SPEC)

> Phase 0-b 的执行契约。让 IN-SESSION 子 agent 在缺必填输入(校准数据 / eval_fn / 等 Tier B 项)时,**问用户而非造假**,且**恢复同一子 agent 继续(不重头)**。
> 2026-07-21 冻结。配套:[[workflow-input-design-principle]](workflow-input-design-principle.md) Tier B。

## 0. 设计选择(为何是 Arch 1:TARS 层拦截,不进引擎)

- **不接 MCP server**(用户决策)、**不用自带 `ask_user` 工具**(compile validator `orca/compile/validator.py:723` 硬拦 `_INTERRUPT_TOOL_NAMES={ask_user,gate}`,铁律 7)。
- **哨兵在 TARS skill 层拦截,绝不喂给 `orca next`**:TARS 派子 agent → 子 agent 返回哨兵 → TARS **在调 `orca next` 之前**检测哨兵 → 问用户 → 恢复同一子 agent → 子 agent 返回**真实 output** → TARS 才 `orca next --output '<真实 output>'`。
- 因此引擎的 `output_schema` 校验(`orca/run/step.py:132-163`,`additionalProperties:false`)**只作用在真实 output 上,哨兵永远不触发 schema mismatch**。→ **`orca/` 引擎代码零改动**(0-b 的改动全在 TARS skill + agent.md + 本 SPEC)。

## 0.1 定位澄清:这是「跑在 SendMessage 之上的提问协议」,不是新通信通道

- **主→子(恢复、保上下文)** 用 CC 原生 `SendMessage(agentId, msg)` / opencode 原生 `Task(task_id=ses_xxx)`。**本机制不重新发明这条通道**,生产路径就是裸 SendMessage/Task(task_id)。
- **子→主/用户(子 agent 发起提问)** 没有原生通道(子 agent 无 `AskUserQuestion`,SendMessage 是主→子方向);子 agent 唯一可靠上行通道是**它的返回值**。sentinel 就是把「请帮我问用户 X」**编码进返回值**的薄约定。
- 合起来是半双工:子→主靠 sentinel 返回,主→子靠 SendMessage 恢复。**sentinel 补的是「提问信号」方向,不与 SendMessage 重复。**
- **sentinel 价值不在通信,在**:① TARS 对返回做**确定性 `is_sentinel()` 识别**(非 LLM 读自然语言),避免把提问误喂 `orca next` 撞 schema;② `MAX_ASK` 硬兜底防死循环;③ 可单测。
- **`tests/spike_ask_user/` 里的 `SubagentBackend`/mock/claude-cli backend 抽象是 test-only 脚手架**(为可单测 + 跨后端 mock),**不进生产路径**。生产 = 裸 SendMessage/Task(task_id) + 轻哨兵。

## 1. 哨兵 schema(子 agent 返回)

子 agent 缺 Tier B 必填项、且读代码无果时,以其**最终消息**返回如下 JSON(且仅此;**轻量**——只有两个必填键):

```json
{"_orca_ask_user": "<一句话问题,如 'calib loader 在你项目哪个 dotted-path?'>",
 "_sentinel": "orca_ask_user_v1"}
```

- **必填**:`_orca_ask_user`(问题串)+ `_sentinel: "orca_ask_user_v1"`(版本化魔键,TARS 用它做 **strict 识别**,非 substring match,避免合法输出碰巧含 `_orca_ask_user` 的 false positive)。
- **可选**:`options: list[str]`(候选答案,有就给用户做选项;opencode 无原生 AskUserQuestion,结构化由 TARS prompt 强制)、`context: str`(agent 已查过哪里/为何歧义,帮用户回答)。两者缺省时 TARS 用自由文本问。
- 一个节点可连续返回多次哨兵(先问 calib 再问 eval),但有**重入上限**(§4)。

## 2. TARS skill 行为(跨后端)

```
output = Task(prompt=node_prompt)            # 派子 agent
attempts = 0
while is_sentinel(output) and attempts < MAX_ASK(=3):
    q = parse_sentinel(output)
    answer = ask_user_host_native(q)          # CC: AskUserQuestion;opencode: 聊天问+读下一轮
    output = resume_same_subagent(answer)     # CC: SendMessage(task_id);opencode: Task(task_id=ses_xxx)
    attempts += 1
if is_sentinel(output):   # 重入上限仍哨兵 → fail loud
    fail_loud("node 未能获取必填输入,已问用户 %d 次" % attempts)
assert not is_sentinel(output)
orca_next_output(output)                      # 真实 output → orca next --output
```

### task_id 捕获(宿主特定)
- **CC**:Task 工具返回 `agentId`(亦见 PostToolUse hook `tool_response.agentId`,memory `b2-task-id-source`)。恢复 = `SendMessage(agentId, answer+"\n继续")`。
- **opencode**(1.18.3 已验证原生支持):Task 工具返回 `<task id="ses_xxx" ...>`,解析 `ses_xxx`。恢复 = `Task(task_id="ses_xxx", subagent_type=<同>, prompt=answer+"\n继续")`,sqlite session 行复用、上下文全保留。

### ask 机制(宿主特定)
- **CC**:原生 `AskUserQuestion`(结构化 options)。
- **opencode**:无原生问题工具 → 模型在主聊天问、读用户下一轮回复(自由文本)。结构化选项/默认/「≤2 问」风格由 TARS prompt 强制。

## 3. 子 agent prompt 契约(每个含 Tier B 的 agent.md 必加)

```markdown
## 缺失必填输入时(严禁造假)
若某 Tier B 必填项(见 inputs 原则)读用户代码无果:
1. **不要**造假(torch.randn / 复用 train 当 eval / 静默默认 0)。
2. 以最终消息返回哨兵:{"_orca_ask_user":"...","options":[...],"context":"...","_sentinel":"orca_ask_user_v1"}
3. 你**会被恢复**(不是重跑),收到用户答案后**继续,不要重做已完成的工作**。
4. 用户也答不出 → 返回 {"_status":"fail_loud","reason":"..."}。
```

## 4. 失败路径与边界

| 场景 | 处理 |
|---|---|
| 连续哨兵 ≥ `MAX_ASK=3` | TARS fail loud(节点放弃,不无限循环) |
| 主 session 上下文膨胀 | 每轮哨兵+答案+恢复累积;TARS 监控 budget,接近上限时提示用户简化 |
| 子 agent 中途崩溃(opencode #13910:task_id 可能丢) | 崩溃 ≠ 正常哨兵返回;走既有 fail loud,不影响哨兵路径 |
| 跨 session 续跑(v1 不支持) | 哨兵不落 tape;session 死 = 节点未推进、用户重跑即可(无 corruption)。v2 可选:引擎 emit `custom(kind=ask_user)` 事件入 tape 实现跨 session 续跑 |
| 哨兵 false positive | strict 识别 `_sentinel:"orca_ask_user_v1"` 魔键,非 substring |

## 5. 验收(headless TARS-SKILL E2E 判据)

- 构造一个缺 calib loader 的最小 workflow,headless 驱动 TARS(claude 后端)。
- 断言:子 agent 返回哨兵(而非 randn)→ SendMessage/Task(task_id) 被调用 ≥1 次 → 答案注入后子 agent 继续 → 最终 `orca next` 收到真实 output → tape 正常推进。
- 重入测试:故意让用户答「不知道」3 次 → 第 3 次后 fail loud,不无限循环。
- 跨后端:claude 路径必过;opencode 路径在 0-b E2E 通过前标 experimental。

## 6. 0-b spike(实现前必做,de-risk)

在动 TARS skill 全量改造前,先做一个**最小 claude-backend spike**:一个 2 节点 workflow,节点 1 的子 agent prompt 故意缺数据 → 返回哨兵 → TARS SendMessage 恢复 → 继续 → 完成。**spike pass 才开 Phase 1/2/3 的 ask-user 落地**;spike fail 则回滚重选 B 档(带答案重跑,丢上下文)。

## 7. 与既有 MCP ask_user 的关系

`orca run`(后端 CLI)路径的 `AgentToolsMcpServer` + `HumanGate` 机制保持不变、生产活跃。本哨兵机制**只用于 IN-SESSION 路径**(不经 ClaudeExecutor,无法注入 `--mcp-config`)。两套并存,不互相干扰。与 `docs/specs/in-session-unified-backend-draft.md` 的关系:本机制不触发 draft §0 的合并触发条件(in-session 仍走 `advance_step` + 宿主 Task,未合并决策核心)。

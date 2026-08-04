---
name: tars
description: >-
  TARS —— 在主 session 里把用户的一句话意图自动匹配到已注册的 workflow 并驱动完成。
  调 `orca list` 选 workflow → `orca <wf>` 拿 inputs_schema → 据此抽 inputs →
  `orca <wf> --inputs` 启动 → 派 Task 子代理逐节点执行 → `orca next --run-id --output`
  循环到 `done:true`。底层用 orca CLI 引擎。
allowed-tools: Bash, Read, Write
---

# TARS

<purpose>
你是 TARS，运行在主 session 里。底层引擎是 Orca——它把一个多 agent 工作流拆成一串节点，
每个节点是一段给子代理的指令。**你的职责是驱动**：启动 workflow、读每步指令、派 Task 子代理执行、
把产出回传推进到下一步，直到完成。

你不直接做节点里的工作（那是子代理的活），你只负责**调度 + 传递产出**。
</purpose>

## 唯一接口：7 个命令

```
orca list                          # 列出可用 workflow（只返 name + description）
orca <wf-name>                     # 不带 --inputs：返该 wf 的 inputs_schema
orca <wf-name> --inputs '{...}'    # 启动一个 workflow（返 run_id + 首节点指令 + 驱动协议）
orca next --run-id <id> --output '<产出>'   # 推进一步（把上一步子代理产出回传）
orca status [--run-id <id>]        # 看进度
orca stop --run-id <id>            # 中断一个 run
orca open [--run-id <id>]          # 打开 web 监控面板（看报告/图表/日志走这里，不在主 session 里看）
```

🔴 **铁律：绝不自己 Read 任何 workflow 的 YAML 文件**。选 workflow 经 `orca list`，
知道 inputs 经 `orca <wf>`（不带 `--inputs`）——这是单一信息源。

## 三步流程

### 第 1 步：选 workflow

```bash
orca list
```

读返回 JSON 的 `workflows` 数组（每项形如 `{"name": "...", "description": "..."}`），按 **description**
匹配用户意图，选定一个 `name`。

- 一眼能定 → 进第 2 步。
- 多个可能 / 意图模糊 → 用一两句话问用户关键区分点（最多 1-2 问，不把列表丢回去）。

### 第 2 步：拿 inputs_schema + 抽 inputs

选定 wf 后，**不带 `--inputs`** 调一次拿 inputs 清单（只查询，不启动）：

```bash
orca <wf-name>
```

返回 `inputs_schema` 数组，每项 `{"name","type","description"}`。**description 开头**可能带方括号标签，
告诉你这字段该怎么处理——严格按标签办，不要自己发挥：

| 标签 | 含义 | 你的行为 |
|---|---|---|
| `[ask]` | 业务决策，**必须用户给** | 用户意图有 → 用；**没有 → 必须问用户**（唯一该主动问的类别） |
| `[infer]` | 可从项目/文件系统推断 | **自己找**（glob、读目录、查项目根），别问；找不到再退化问 |
| `[default]` | 有合理默认 | **省掉**（让 workflow 用 default），别问、别瞎填 |
| `[advanced]` | 罕见 override | 同 `[default]`：**省掉** |
| （无标签） | 老字段 | 用户给了用、能推就推、缺且推不出再问 |

抽取流程：

1. 扫一遍分桶。`[ask]` 桶里用户没给的，**攒齐后集中问一轮**（一两句话，别一个个问）。
2. `[infer]` 桶用 Bash 自己找（glob `**/model.py`、查项目根等）；找不到的归并到上面那轮一起问。
3. `[default]`/`[advanced]` 桶**一律不放进 inputs JSON**。
4. 类型按 `type` 给（string/int/boolean/list）。

最终 inputs JSON 只含 `[ask]`（含用户答）+ `[infer]`（找到的）+ 无标签（决定填的），省掉所有
`[default]`/`[advanced]`。

> 不带 `--inputs` 调 `orca <wf>` **只返 schema，不启动**（也不产生 run）。真正启动在下一步。

### 第 3 步：启动 + 逐节点驱动

```bash
orca <wf-name> --inputs '<inputs JSON>'
```

读返回 JSON：

- `run_id`：本 run 句柄。**所有 `orca next` 都带它**，抄原样不改。
- `prompt`：首节点指令 + 驱动协议。

**驱动循环**（核心，严格照做）：

1. 用 **Task 工具派一个子代理**执行当前节点：子代理 Read 节点指令文件（prompt 里给了路径）做完，
   它的最终消息就是这步产出。🔴 **你自己不许 Read 节点指令文件**（撑爆上下文）——派子代理读。
2. 子代理返回后，**先检查它的最终消息是不是 ask-user 哨兵**（见下【哨兵处理】）：
   - 是哨兵 → 走哨兵小循环：问用户 → 恢复**同一**子代理 → 拿真实产出。
   - 不是哨兵 → **一律**把最终消息原样作 `--output` 喂 `next`（**包括子代理自报的失败**——
     引擎的哨兵检测 / schema 校验会判 ``recoverable``，见下【产出不合 schema / 自报失败】）：

     ```bash
     orca next --run-id <run_id> --output '<子代理最终消息>'
     ```
3. 读这条 stdout 的 JSON：
   - `"done": true` → workflow 完成，停。把最终结果总结给用户。
   - `"reason": "busy"`（撞锁，罕见）→ 不重派不重发，等返回的 `retry_after_ms` 毫秒后**原样重试同一条命令**。
   - 否则 JSON 里的 `prompt` 就是**下一个节点**的指令 → 回第 1 步继续。

一直循环到 `done: true`。

## 哨兵处理（子代理缺必填项时问用户而非造假）

子代理读用户代码找某必填项无果时，会以**最终消息**返回一个 ask-user 哨兵求助，而不是造假
（`torch.randn` / 复用 train 当 eval / 静默默认空值）。

**哨兵 schema**（子代理最终消息长这样，可能包在 ```json 围栏或前后带文字里）：

```json
{"_orca_ask_user": "<一句话问题>",
 "options": ["<候选1>", "<候选2>"],
 "context": "<已查过哪里、为什么歧义>",
 "_sentinel": "orca_ask_user_v1"}
```

处理：

1. **识别**：抽最外层 JSON 对象（从第一个 `{` 配平到匹配的 `}`），`json.loads` 成 dict 且
   `dict["_sentinel"] == "orca_ask_user_v1"` 才是哨兵；任一步失败 → 当真实产出。
2. **问用户**：CC 用原生 `AskUserQuestion`（question / options / context）；opencode 在主聊天问
   （带 options 编号），读下一轮用户回复作答。
3. **恢复同一子代理**（不重派，上下文不丢）：**Task 调用返回时立刻记下 task_id**
   （CC 的 `agentId` / opencode 的 `ses_xxx`）。拿不到 task_id（Task 早失败 / 返回格式异常）→
   走【失败处理】fail loud，**不重派新子代理假装续跑**（会丢上下文、违反「同一子代理」）。
   - CC：`SendMessage(task_id, "<用户答案>\n请基于此答案继续，不要重做已完成的工作。")`
   - opencode：`Task(task_id="ses_xxx", subagent_type=<spawn 时同一个>, prompt="<用户答案>\n请基于此答案继续，不要重做已完成的工作。")`
4. 子代理拿答案继续 → 返回新最终消息 → 回第 1 步**再判一次**（可能再问下一个缺项）。
5. **MAX_ASK = 3**：一个节点连续问 3 次仍哨兵 → 告诉用户放弃，`orca stop` 停 run。

🔴 **哨兵绝不喂 `orca next`**（带 `_sentinel` 等私有字段会被节点 `output_schema` 拒）。只有消化后的
真实产出才进 `--output`。

## 产出不合 schema / 子代理自报失败（recoverable）/ 连续未派活（warn）

`orca next` 回的信封可能带这两个字段（都是 run 存活，**不 stop、不重启**）：

- `"recoverable": true` → 节点产出不合 `output_schema`（非 JSON / 缺字段 / 类型错）**或**子代理自报失败
  哨兵（信封 ``error_kind == "agent_blocked"``，同走 recoverable 重派分支）。**重 arm 的 prompt 已由
  引擎注入历次失败原因（含本次）**——主 session 不再手动注入（见 2026-08-04 SPEC §4.3）。按你的判断
  重派（同 session 用 SendMessage 复用同一子代理；跨 session 续跑则派 fresh 子代理），拿产出再
  `orca next --output`。连续 3 次未过 engine 自动终态。
- `"warn": true` → 连续多次 next 没派子代理 / 没回传 output 的提醒。正常派 Task 推进即可，或主动 `orca stop`。

## 单引号转义

`--output` 的值用单引号包整段产出。产出含单引号 / 撇号（`it's`）时，每个单引号写 `'\''`：

```bash
orca next --run-id <run_id> --output 'it'\''s a good film'
```

含换行的产出：单引号本身跨行，直接把多行产出放在 `--output '...'` 里。

## 续跑（新 session 接手半完成的 run）

上一 session 中途断开（关终端 / 网络 / crash），后台仍保留半完成的 run。**新 session 启动时**：

1. 调一次无参 status 拿全部活跃 run：

   ```bash
   orca status --json
   ```
2. 读 `runs` 数组（每项含 `run_id` / `node` / `status` / `resumable`）。
   - 空 → 没有半完成 run，走三步流程开新工作。
   - 有 `resumable: true` → **问用户**续跑还是开新（列 `run_id` + `node` 让用户认领，**别自作主张**）。
3. 用户要续跑 `X`（停在节点 Y）：**不带 `--output`** 调一次重发当前节点 prompt（幂等重发，不重复推进）：

   ```bash
   orca next --run-id X
   ```

   拿到 `prompt` 后，按【第 3 步 驱动循环】照常派子代理 → 产出作 `--output` → 循环到 `done:true`。

## 失败处理（fail loud）

- `orca next` 非零退出 / JSON `reason` 含 `failed:` → workflow 终态出错。读 `reason` 告诉用户，
  **不要悄悄重跑** `orca <wf>`（会被「同 workflow 已有活跃 run」拒）。要重来先 `orca stop`。
- `inputs_validation_error`（bootstrap 期，run 未创建）→ 你抽的 inputs 不符 type / 缺必填。
  按 `reason` 指出的字段修 inputs 重发 `orca <wf> --inputs`（此错没有 run 可 stop，直接重发即可）。
- 看报告 / 图表 / 日志 → `orca open`（webui 做，不在主 session 里汇报）。

<success_criteria>
- [ ] 经 `orca list` 选定 workflow（只看 name + description，不读 YAML）
- [ ] `orca <wf>`（不带 --inputs）拿 inputs_schema
- [ ] 据标签抽 inputs：`[ask]` 没给才问（集中一轮）、`[infer]` 自己找、`[default]`/`[advanced]` 省略
- [ ] `orca <wf> --inputs` 启动拿到 run_id
- [ ] 每节点：派 Task 子代理读指令 → **检查最终消息是否哨兵** → 真实产出原样作 --output → `orca next --run-id`
- [ ] 哨兵：捕获 task_id → 问用户 → 恢复**同一**子代理 → 循环到真实产出；连续 ≥3 次（MAX_ASK）fail loud；**哨兵绝不进 next**
- [ ] `recoverable`：重 arm prompt 已含失败历史（引擎注入）→ 按判断重派（复用/fresh）→ `orca next --output`；连续 3 次自动终态
- [ ] `warn`：正常派 Task 推进或主动 `orca stop`
- [ ] 单引号产出正确转义（`'\''`）
- [ ] 循环到 `done:true` 后停止并总结
- [ ] 失败时读 reason 告知用户，不静默重启
- [ ] 新 session 先 `orca status` 找 `resumable:true`，问用户续跑，确认后 `orca next --run-id X`（无 output）→ 子代理 → --output 推进
</success_criteria>

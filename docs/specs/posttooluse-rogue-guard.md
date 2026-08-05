# SPEC: PostToolUse 事后告警守卫（防主 session 下场干活）

> 状态：**spec-review 闭环（conditional-pass，18 真问题已修订）** | 分支 `in-session-unified-backend` | 相关 SPEC [`in-session-entry-and-simplification.md`](in-session-entry-and-simplification.md) §4.4（nudge hook）
>
> 类型：**B 路径扩展**——纯提示（hint），不阻止、不推进、不捕 output。**不是**已退场的 A 路径 PostToolUse（§背景 §3 详述）。

---

## 0. 摘要

给四前端（cc / cac / opencode / nga）加一个**事后告警**钩子：主 session 在活跃 run 期间若自己用了「做节点活」的工具（Edit/Write/跑 train 等），在其执行后注入一条**纯文本提示**，提醒它「这是子代理的活，改派 Task 或调 `orca next`」。**仅当本 session 有活跃 run 时触发**；不阻止动作（动作已发生）；不调 `orca next`（B 路径铁律不变）。

## 1. 背景与问题

**失败模式（Q1）**：workflow 节点失败后，主 session（载 tars skill 的那个）不去调 `orca next`，而是「好心」自己下场——Edit/Write 改文件、Bash 跑 train/build。这是 turn **中途**的行为。

**现有 nudge 覆盖不到**：现有 §4.4 nudge（cc 家族 = Stop hook `orca-nudge.sh`；opencode 家族 = plugin `orca.ts` 的 `session.idle` 钩子）只在 **turn 结束 / session 空闲**时触发。子代理失败后主 session 在 turn 内连续调工具「下场」，Stop/idle 根本没机会触发——这是 Q1 的盲区。

**根因**：失败那一刻契约不够「唯一显然」（见 [`CLAUDE.md`](../../CLAUDE.md) 问题分类：跨模块、需 hack → 架构层）。本守卫是**症状层防御**（L2），与契约锐化（L1，另议）互补。**诚实声明**：本件不解决 Q1 根因（那是 L1 契约），仅提供可见信号；纯提示治本靠 L1。

## 2. 目标 / 非目标

**目标**
- 主 session 在**本 session 有活跃 run** 期间，自己用了「做节点活」的工具 → 事后注入一条纯提示。
- 覆盖四前端：cc 家族（cc + cac）+ opencode 家族（opencode + nga）。
- 复用现有活跃 run 检测 + host_session 归属判定（DRY）。

**非目标（明确不做）**
- ❌ **不阻止**动作（用户决策：纯提示，`permissionDecision: deny` 太严）。动作已执行，提示在后。**接受无法阻止 rogue 动作**——这是用户明确选择的代价。
- ❌ **不调 `orca next`**（B 路径铁律；自动推进 = 退化 A 路径）。
- ❌ **不捕 Task output**（不复活已删的 A 路径 PostToolUse output cache）。
- ❌ 不改现有 Stop/idle nudge 行为（L3 续推现状保留，仅回归保护）。

## 3. 与已退场 A 路径 PostToolUse 的区别（必须讲清，否则 review 必打回）

`templates/__init__.py` 记：v5 §8 step 2b 删了 `cc_hooks.py`（CC 路 A 的 Stop/PostToolUse 脚本生成）。**本件不是它的复活**：

| 维度 | 已删 A 路径 PostToolUse | **本件（B 路径守卫）** |
|---|---|---|
| 目的 | 捕 Task tool 的 output → 写 cache → Stop 读 cache 驱动 `next`（**自动推进**） | 检测「下场干活」→ 注入提示（**不推进**） |
| 触发工具 | `Task\|Agent`（捕子代理产出） | `Edit/Write/NotebookEdit/Bash`（下场干活） |
| output | 捕获并跨 hook 传递 | 不捕、不传 |
| 路径 | A（hook 驱动编排） | B（主 session 自调 `next`） |

> **注脚 P1**：上表「A 路径 matcher = `Task\|Agent`」据 v5 step 2b 回忆，未留 commit-level 证据；本件**不依赖**该 matcher 的精确性（新 guard 触发工具集在 §5 独立定义）。
>
> **注脚 P2（D-v7-1 边界判例）**：§5 工具白名单分类属于**传输层分类**（区分 hint 投递目标），**非**编排状态机判断；分类输出仅决定「是否注入一段文本」，不调 advance/router/replay/tape 路径，故 D-v7-1（模板零业务逻辑）不禁。判例依据：现有 `cc_nudge.sh` 已读 marker + tape host_session 做「是否 block」判定，本件同性质（读状态 → 决定是否注入文本）。

**结论**：本件是 §4.4 nudge 的第二个事件载体（Stop/idle 之外加 PostToolUse），与 A 路径退场决策不冲突。

---

## 4. 触发条件（两家族统一语义）

满足**全部**才注入提示：

1. **本 session 有活跃 run**：`runs/orca-<run_id>.json` marker 存在 **且** tape 首条 `workflow_started.data.host_session` == 当前 session（复用 §4.4 host-session-binding v2 判定）。无活跃 run / 归属他 session → 静默放行。
2. **工具属「下场干活」类**（分类表见 §5，规则单一真相源 `templates/tool-classification.json`，§12）。Read/Glob/Grep/Task(派子代理)/AskUserQuestion 等 → 不触发。
3. **节流窗口外**：per-session 独立节流，**键名按家族分**（P3 修订）——`runs/.orca-guard-cc-<host_session>`（cc 家族）/ `runs/.orca-guard-<sessionID>`（opencode 家族），**30s**，与 §4.4 nudge 的 60s 节流（`runs/.orca-nudge-*`）分键，互不影响。窗口内重复 → 静默。

## 5. 工具分类表（单一真相源：`templates/tool-classification.json`）

为防 cc_nudge.sh（Python heredoc）与 orca.ts（TypeScript）双实现漂移（P5/E2），分类规则集中于 **`orca/iface/in_session/templates/tool-classification.json`**，两处启动时 read。CI grep 守门：白名单字面量在 `*.sh` / `*.ts` 各出现 ≤1 次（即只读资产引用，非硬编码副本）。

| 工具（CC / opencode） | 判定 | 说明 |
|---|---|---|
| `Edit` / `Write` / `NotebookEdit`（CC）；`edit` / `write`（opencode） | 🔴 下场 | 编排期主 session 几乎没理由写文件 |
| `Bash` / `PowerShell`（CC）；`bash`（opencode） | 🟡 看命令 | 见下「Bash 分类」 |
| `Read`/`Glob`/`Grep`（CC）；`read`/`glob`/`grep`（opencode） | 🟢 放行 | 调试读 |
| `Agent`/Task（CC）；`task`（opencode） | 🟢 放行 | 正确委派，鼓励 |
| `AskUserQuestion`（CC） | 🟢 放行 | 问用户 |

**Bash 分类**（命令串解析）：
1. **复合命令（E1 修订）**：命令含任一 shell 分隔符（`;`、`&&`、`||`、`|`、`&`、换行）→ **直接 🔴 下场**（保守规则；符合 §10 R2「误分类后果可控」精神；不做精细分词——`git log; python train.py` 这种「先看一眼再跑 train」正是 Q1 失败模式，必须拦）。
2. **非复合**：取首个命令词，按 **word-boundary 前缀匹配**（E6 修订：`<cmd>` 后须接空白或 EOL，禁止 `ls` 命中 `lsof`/`lsblk`）对照只读白名单：
   - 命中白名单（`orca`、`cat`、`head`、`tail`、`grep`、`rg`、`find`、`ls`、`git status`、`git log`、`git diff`、`git show`，大小写不敏感）→ 🟢 放行；
   - 未命中 → 🔴 下场。

白名单本身存于 `tool-classification.json` 的 `readonly_bash_prefixes` 数组（word-boundary 前缀集）。

## 6. 提示文本（事后口吻，承认动作已发生）

```
【Orca 守卫·事后提醒】检测到你在活跃 run（<run_id>）期间自己用了 <tool>。编排期主 session
不该下场做节点工作——那是子代理的活。建议：改派 Task 子代理完成此步，或把已有产出作为
--output 调 `orca next --run-id <run_id>` 推进。本提醒不阻止（动作已执行）；若这是必要的
调试/解锁操作，忽略即可。
```

> **注脚 E9**：`<tool>` 直接取 `tool_name` 原值（不友好化），避免引入映射表 = 又一份 DRY 包袱。

---

## 7. CC 家族实现（cc + cac）

### 7.1 单脚本双事件（DRY）

扩展现有 `orca/iface/in_session/templates/cc_nudge.sh`（已装为 `<root>/hooks/orca-nudge.sh`）使其**按 `hook_event_name` 分支**，单一脚本服务 Stop + PostToolUse，共享活跃 run + host_session 扫描逻辑：

- 读 stdin JSON 的 `hook_event_name`：
  - **`Stop`**：现有行为**字节级不变**（`decision:block` + reason，60s 节流）。
  - **`PostToolUse`**：读 `tool_name` + `tool_input`（Bash 的 `command`）+ `session_id`（CC hooks doc 声明字段，§10 R5 fallback 用）→ §5 分类（读 `tool-classification.json`）→ §4 触发条件 → 命中则 stdout 输出 `additionalContext`（**无 `decision`**），否则 exit 0 静默。

活跃 run 扫描 / `_host_session_from_env` / `_host_session_from_tape` / marker fail-loud 等 python 逻辑**两事件共用**。

### 7.2 settings.json 注册（`install_cmds._install_cc_nudge` 扩展）

除现有 `hooks.Stop` 条目外，**新增 `hooks.PostToolUse` 条目**，指向同一脚本，matcher 锚定（E7 修订）限定关心的工具：

```jsonc
{
  "hooks": {
    "Stop": [{ "hooks": [{ "type": "command", "command": "bash <abs>/orca-nudge.sh" }] }],
    "PostToolUse": [{
      "matcher": "^(Write|Edit|NotebookEdit|Bash|PowerShell)$",
      "hooks": [{ "type": "command", "command": "bash <abs>/orca-nudge.sh" }]
    }]
  }
}
```

- matcher 让 CC 只在相关工具后调用脚本（减少无谓 spawn）；脚本内再做 §5 精分类（readonly Bash 放行）。
- 去重：PostToolUse 条目里 command 含 `orca-nudge` 即视为已声明。
- cac 落点 `.claude`→`.cac`，其余同（§4.3 家族对称）。

### 7.3 PostToolUse 输出契约

命中（活跃 run + 下场工具 + 节流窗外）→ exit 0 + stdout：
```json
{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "<§6 文本>"}}
```
未命中 → exit 0 无 stdout（静默）。**绝不 `decision:block`，绝不 exit 2**（纯提示）。

> **注脚 E12**：不选 `decision:block` 的理由——PostToolUse 语义下 `decision:block` = 强制继续 turn + 把 reason 当下一轮指令，与「提醒不阻止」冲突，且会让模型在 turn 内反复触发。选 `additionalContext`（纯上下文注入，不改变控制流）。详见 §2 用户决策。

节流文件 `runs/.orca-guard-cc-<host_session>`（30s；与 §4.4 `runs/.orca-nudge-cc-<host_session>` 分键）。

## 8. opencode 家族实现（opencode + nga）

### 8.1 `orca.ts` 加 `tool.execute.after` 钩子

opencode plugin 无 CC 式 PostToolUse，但有等价的 `tool.execute.after` 事件（[opencode plugin docs](https://opencode.ai/docs/plugins) 工具事件清单确认；`tool.execute.before` 示例显示 input 形状 `input.tool` / `output.args`）。在 `OrcaPlugin` 返回对象里新增：

```ts
"tool.execute.after": async (input: any) => {
  // step 0（P10 修订）：取 in-flight mutex，与 idle nudge 共用，防 turn 中工具调用与 idle 并发注入交错
  const sessionID = <见 step 1 取法>
  if (sessionID && injecting.has(sessionID)) return
  if (sessionID) injecting.add(sessionID)
  try {
    // step 1（P4 修订 + fallback）：取 sessionID。
    //   首选 input.sessionID；取不到 → 写 runs/.orca-guard-unbound.json 心跳 + return（fail-safe 降级）。
    // step 2：复用 listActiveRuns(sessionID) —— 已存在！空 → return
    // step 3：§5 分类 input.tool / 参数（读 tool-classification.json）→ 非下场 → return
    // step 4：guard 节流（nudgeAllowed 同款内核，独立文件 runs/.orca-guard-<sessionID>，30s）→ 窗内 return
    // step 5：client.session.promptAsync({ path:{id:sessionID}, body:{ parts:[{type:"text",text:<§6>}], model:{...} }})
    //        （与 idle nudge 同款注入；markGuard 注节流时间戳，注入失败不计）
  } catch (e) { console.error("[orca] guard promptAsync failed:", e) }
  finally { if (sessionID) injecting.delete(sessionID) }
}
```

**复用**（DRY）：`listActiveRuns` / `hostSessionOfRun` / `nudgeAllowed` / `markNudged` / `client.session.promptAsync` / `injecting` mutex 全部已在 `orca.ts`。节流文件名参数化为 `throttleFile(scope, sessionID)`（idle 与 guard 共用 `nudgeAllowed`/`markNudged` 内核，仅文件名不同）。

### 8.2 idle nudge 不变

现有 `event` → `session.idle` 钩子**字节级不动**（回归保护）。新增 `tool.execute.after` 与之并列。

---

## 9. Issue #34692 处理（已记录约束）

[`docs/REFERENCES.md:92`](../../docs/REFERENCES.md)（Issue #34692）：CC 的 PreToolUse/PostToolUse 在工具调用**委派给 subagent 时被静默跳过**。

**对本件影响：良性，需文档化**：
- 我们关心的触发工具是 Edit/Write/Bash（下场干活）——**不是 subagent 委派**，故不被跳过。
- 主 session 正确委派 Task/Agent 时，PostToolUse 被跳过——**这正是我们想要的**（委派是正确行为，不该告警）。
- 故 #34692 不构成本件的盲区，但 SPEC 显式记录：**本守卫不覆盖「主 session 通过 Task 委派给子代理、子代理内下场干活」的链路**（子代理内部工具调用不在主 session hook 视野；那是子代理自己的 SKILL/约束范畴）。

opencode 侧：`tool.execute.after` 对 `task` 工具是否同样跳过未实证。我们放行 `task`，故无影响。

---

## 10. 风险 / 未决

- **R1（opencode `tool.execute.after` 输入形状 + mid-turn 可调性，spike）**：官方文档未给完整 input 字段（`input.tool`、`input.sessionID`、`input.args?` / `output` 形态、是否能在 turn 进行中成功调 `promptAsync`）。
  - **处置（采纳 spec-reviewer 推荐方案 b）**：SPEC 内声明 fallback + spike 作为实施第 0 步。
  - **fallback**：取不到 `sessionID` → 写 `runs/.orca-guard-unbound.json` 心跳 + `return`（fail-safe 降级，该次不告警，不抛错）；`promptAsync` 在 turn 中失败 → `console.error` + 不计节流，下个工具调用重试。
  - **可测断言（E10）**：活跃 run 中触发 write 工具 → session 消息历史新增 user 角色消息（promptAsync 成功证据）；失败 → `runs/.orca-guard-unbound.json` 或 doctor 报 `guard_hook_unbound`。结果写入 release note。
- **R2（只读 Bash 白名单维护）**：白名单分类是 best-effort（CC hook `if` 过滤器本就 fails open）。本件为提示非阻断，误分类（把干活命令当只读）只是漏提示，无安全后果。复合命令规则（§5 Bash 分类 1）已覆盖主要绕过路径。
- **R3（CC 单脚本双事件回归）**：扩展 `cc_nudge.sh` 须保证 Stop 行为字节不变。靠现有 nudge 单测 + Stop 分支 golden 断言（§11.2）+ 新增 guard 单测 + e2e 回归保护。
- **R4（四前端真机加载）**：CAC/NGA 真机是否读 `.cac`/`.nga` 的 settings.json/plugin 仍属 §9#1 跨平台用户侧验证（本件不解决，沿用现状假设）。
- **R5（CC PostToolUse env 链，spike；E5 修订）**：现有 `cc_nudge.sh` 的 `_host_session_from_env` spike 只覆盖 **Stop** hook（`CLAUDE_CODE_SESSION_ID` 注入实证）。PostToolUse 子进程是否继承同 env 链**未实证**——Stop 的 spike 不能继承。
  - **fallback**：env 未注入 `CLAUDE_CODE_SESSION_ID` 时，从 stdin JSON 的 `session_id` 字段（CC hooks common input 声明）取 host_session；两处都取不到 → fail-safe 放行（写 `runs/.orca-guard-unbound.json` 心跳）。
  - 与 R1 同等处理（实施第 0 步 spike，结果入 release note）。

## 11. 验收标准

### 11.1 安装（单测，四前端对称）
- [ ] `tars install --target cc` 在 `~/.claude/settings.json` 注册 `hooks.Stop`（去重）**和** `hooks.PostToolUse`（matcher `^(Write|Edit|NotebookEdit|Bash|PowerShell)$`，command 含 `orca-nudge`）。
- [ ] `--target cac` 同 cc，落点 `.cac`。
- [ ] `--target opencode` / `nga` 的 `orca.ts` 含 `tool.execute.after` 钩子（grep 守门）；idle `event` 钩子不变。
- [ ] **四前端触发工具集字面相等（E11）**：cc/cac settings.json matcher 工具集 ≡ opencode/nga orca.ts 分类工具集（单测断言集合一致）。
- [ ] 幂等：重跑不重复注册、内容相同跳过。

### 11.2 CC 家族（单测 + 真机 e2e）
**单测**（mock stdin JSON，覆盖各 case）——**前置 setup（E8）**：每条清空 `runs/orca-*.json`、`runs/.orca-guard-*`、`runs/.orca-nudge-*`，并 tape-derive 验证无活跃 run（除非 case 需要）：
- [ ] **PostToolUse + 有活跃 run（本 session）+ Write/Edit** → stdout JSON 含 `additionalContext` 字段且文本含 `<run_id>`；stdout 不含 `decision`/`permissionDecision`；exit 0（非 2）。
- [ ] **PostToolUse + 有活跃 run + 只读 Bash（`ls`/`cat`/`git log`）** → 无 stdout（静默）。
- [ ] **PostToolUse + 有活跃 run + `orca next` Bash** → 无 stdout。
- [ ] **PostToolUse + 有活跃 run + 非只读 Bash（`python train.py`）** → stdout 含 `additionalContext`。
- [ ] **PostToolUse + 复合命令（`git log; python train.py`）** → stdout 含 `additionalContext`（E1 验收）。
- [ ] **PostToolUse + word-boundary（`lsof`）** → stdout 含 `additionalContext`（E6 验收；`ls` 误命中已堵）。
- [ ] **PostToolUse + 无活跃 run + Write** → 无 stdout。
- [ ] **PostToolUse + run 归属他 session + Write** → 无 stdout（host_session 隔离）。
- [ ] **PostToolUse + 30s 内重复 Write** → 仅第一次有 stdout（节流）。
- [ ] **Stop 分支回归**：Stop mock stdin → stdout 字节级 == pre-change golden（snapshot）；60s 节流文件名/窗口不变。

**真机 e2e**（test-agent，真 `claude -p` 驱动 mini workflow）：
- [ ] 活跃 run 中模型 Write → 模型**下一 turn 回应引用 §6 文本**（行为证据，P8/E4）。
- [ ] Stop nudge 行为不变（回归：turn 结束 + 活跃 run 仍 `decision:block` 提醒调 next）。

### 11.3 opencode 家族（真机 e2e，test-agent，真 opencode/nga 驱动 mini workflow）
- [ ] **有活跃 run + write/edit** → `promptAsync` 注入 §6 提示（session 消息历史新增 user 角色消息）。
- [ ] **有活跃 run + 只读 bash** → 无注入。
- [ ] **有活跃 run + task（委派）** → 无注入。
- [ ] **无活跃 run + write** → 无注入。
- [ ] **节流生效**。
- [ ] **idle nudge 不变**（回归）。
- [ ] **R1 spike 闭环（E10）**：`tool.execute.after` 的 `input.tool` / sessionID 取法 / bash 参数字段 / mid-turn `promptAsync` 可调性在真机确认；取不到 sessionID 时 `runs/.orca-guard-unbound.json` 心跳写出。写入 release note。

### 11.4 守门（CI grep / 架构，P7/E3 修订为结构化断言）
- [ ] **模板/plugin 零 Orca 业务逻辑**（D-v7-1）：不调 advance/router/replay/tape 路径，不做状态机判断。
- [ ] **纯提示（结构化单测，非裸 grep）**：对 `cc_nudge.sh` 喂 PostToolUse mock stdin（覆盖 §11.2 各 case），断言 stdout 不含 `decision`/`permissionDecision`、exit ≠ 2；对 Stop mock stdin 断言 stdout 字节级 == golden。grep 守门降级为「模板内 `decision:block` 总出现次数 ≤ 基线」（Stop 分支合法含 1 处）。
- [ ] **不调 `orca next`**：grep 模板/plugin 内无自调 `next`（B 路径铁律）。
- [ ] **白名单单一真相源（P5/E2）**：白名单字面量在 `*.sh`/`*.ts` 各出现 ≤1 次（只读 `tool-classification.json` 引用）；CI grep 守门。

---

## 12. 实现改动清单（coder-agent）

**第 0 步（spike，实施最先做；R1 + R5）**：
0. 真机 spike：(a) opencode `tool.execute.after` input 形状 + sessionID 取法 + mid-turn `promptAsync` 可调性；(b) CC PostToolUse 子进程 env 链（`CLAUDE_CODE_SESSION_ID` 是否注入）。两 spike 结果 + fallback 触发条件写入 release note。若某家族 spike 彻底失败 → 该家族降级为「不覆盖」（写心跳文件 + warn），不阻塞另一家族。

**正式改动**：
1. `orca/iface/in_session/templates/tool-classification.json`（新增）：单一真相源——`readonly_bash_prefixes`（word-boundary 前缀集）+ 下场工具集 + 复合命令分隔符集。
2. `orca/iface/in_session/templates/cc_nudge.sh`：加 `hook_event_name` 分支（Stop 原样 / PostToolUse 新增 §5 分类读 JSON + §7.3 输出 + session_id fallback + 30s guard 节流）。
3. `orca/iface/cli/install_cmds.py::_install_cc_nudge`：settings.json 合并新增 `hooks.PostToolUse` 条目（去重 + 锚定 matcher）。
4. `orca/iface/in_session/templates/opencode/orca.ts`：新增 `tool.execute.after` 钩子（step 0 mutex / step 1 sessionID fallback / 读 tool-classification.json / 独立 guard 节流键）；节流文件名参数化。
5. 单测：install 四前端对称 + cc_nudge.sh PostToolUse 分支（mock stdin，覆盖 §11.2）+ Stop golden + orca.ts guard 逻辑（同款）+ tool-classification.json 单一真相源 grep 守门。
6. `docs/specs/in-session-entry-and-simplification.md` §4.4 加交叉引用本件。
7. release note + CHANGELOG + CURRENT.md（任务完成强制流程）。

## 13. 流程

spec-review 闭环（conditional-pass，18 真问题已按本件修订）→ **coder-agent**（第 0 步 spike → 实现 + 自 review + commit）→ **test-agent**（四前端真机 e2e + 回归）。

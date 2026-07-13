# In-Session 统一后端 Design Draft

> **状态**：Draft（2026-07-13），待 `spec-review-adversarial` 审视，**不立即实施**。
> **前置依赖**：批 B（opencode in-session 主链路跑通）收口后开工。本 draft 建立在 in-session model-driven advance（CURRENT.md 2026-07-09 补丁）之上。
> **关联**：收口 2026-07-13 讨论——同一后端 + 命令分家 + skill 入口 + 删 setup + CAC 适配 + 输出契约。
> **替换**：本 draft 推翻 v7/v8/批 B 的「in-session CLI 自管 tape」路线，改为「in-session 与 teams 共享同一后端 core」。

---

## 0. 背景与目标

### 0.1 现状裂缝（结构性问题）

in-session（`orca in-session bootstrap/next`）与后端（`orca run`）当前是**两套代码**：

| | in-session（`orca in-session`） | 后端（`orca run`） |
|---|---|---|
| 决策核心 | `advance_step`（纯决策）+ CLI 自管 | `Orchestrator` 循环 |
| 写 tape | CLI 调 `emit_batch`（自己写） | Orchestrator 经 EventBus |
| 节点执行 | 主 session 派 task 子代理（core 进程外） | `Executor.exec` spawn subprocess（core 进程内） |

两套决策路径产出 tape 要**人工对齐**（CURRENT.md 反复纠结的「G2 序列对齐 vs `orca run` 同 wf tape」）。这是违反「单一真相源 + 一条读路径」底线（CLAUDE.md 架构铁律）的结构性裂缝。

### 0.2 目标

**同一后端 core，两个入口壳（`teams` / `orca`）**。in-session 为主要战场。命令分家顺带根治「LLM 误调后端 run」。

### 0.3 非目标

- 动态构建 workflow（本期不做，后续再议）。
- 重写已稳定的子模块（reducer / tape / router / translator 复用）。

---

## 1. 架构铁律：同一后端 core

### 1.1 单一决策核心

合并 `advance_step`（in-session 纯决策）与 `Orchestrator`（teams 循环）为**唯一决策核心**。两壳调同一套决策逻辑，产出同一形态的 tape 事件序列——G2 对齐问题自动消失（同一代码写 tape）。

> **合并差异面（2026-07-13 源码 diff）**——`advance_step`（step.py，in-session 单步纯决策）↔ `Orchestrator`（orchestrator.py，teams 内部循环）。逐条对齐清单：
>
> | # | 差异点 | advance_step | Orchestrator | 合并动作 |
> |---|---|---|---|---|
> | 1 | 节点类型 | 仅 agent（validator 拒 parallel/foreach/gate/ask_user）| 全类型 | **决策 A**（见下）|
> | 2 | 驱动模型 | 单步外部驱动 | 内部循环 drive_loop | **决策 B**：core 支持两种驱动（§1.2 start/submit_output_and_advance）|
> | 3 | inputs default | `_resolve_inputs`（DRY 债）| `__init__` 内联 | 抽共享纯函数 |
> | 4 | outputs 渲染 | `_final_outputs`（DRY 债）| `_evaluate_outputs` | 抽共享纯函数 |
> | 5 | resume/next_node | ✅ 已复用 `Orchestrator._inputs_from_tape`/`_next_node_for_resume` | 原始 | 已共享，无需动 |
> | 6 | 中断/gate/ask_user | 不处理 | InterruptHandler/AgentToolsMcpServer/HumanGate | 随决策 A |
> | 7 | max_iter/错误 taxonomy | 终态+InSessionError | max_iter+四类错误 | phase-11 ErrorKind 已部分统一 |
>
> **好消息**：#5 已共享（advance_step 已调 Orchestrator 类方法），非"两套独立代码硬拼"；#3/#4 是注释自标的 DRY 债，本就计划抽。
>
> **决策 A（最大，已定 a）**：in-session 节点范围 = **a = 线性 MVP**。
> - **MVP 支持**：agent 节点 + **routes 条件路由（含回边/循环）** + max_iter 兜底。⚠️「线性」≠ 只能直线——route 的 `to` 指向任意节点（含上游），`router.resolve` 无回边约束（源码实证），故「校验不通过回上游」这类循环 MVP 即支持。
> - **MVP 拒绝**（编译期 validator 拦）：parallel（并行扇出）/ foreach（集合展开）/ gate / ask_user（in-session 下语义要重定义：主 session 怎么并发派子代理？弹窗走宿主 elicitation 不可靠）。
> - **扩展路径（a→b）**：ORCA 已有 parallel 完整实现（Orchestrator 扇出/聚合），将来升级 = 把 parallel 决策搬进 core + InSessionExecutor 加「主 session 并发派 task 子代理」语义。
>
> **决策 B（中等）**：驱动统一——core 同时支持 teams 内部循环 / orca 外部单步。路径清晰（§1.2）。
>
> **扩展性预留（OCP，现在做、零代码成本，避免将来重写）**：
> 1. **core 决策按 `node.kind` 开放分派**（`match` 结构，现仅 `case "agent"`）——加 parallel = 加分支，不改骨架。避免：agent 逻辑硬编码主流程。
> 2. **executor 接口 = 单执行单元**（`exec(node)`）——扇出归 core 决策层，executor 不背并发。加 parallel 时 executor 接口不变。
> 3. **tape seq 兼容并发**（append 时单调分配，已是）——parallel 并发事件交错写 tape 天然有序。

### 1.2 core API 契约（两壳共调）

```
core.start(wf, inputs, *, executor) -> {run_id, entry_prompt}
core.submit_output_and_advance(run_id, output, *, output_file=None) -> {done, next_prompt, next_prompt_file}
core.status(run_id) -> RunState
core.stop(run_id) -> {}
```

- **`teams` 壳**：`core.start(executor=SubprocessExecutor)` → core 内部驱动循环（executor 跑节点 → 自动 `submit_output_and_advance`）。
- **`orca` 壳**（in-session）：`core.start(executor=InSessionExecutor)` → core 返回 `entry_prompt` 给主 session → 主 session 派子代理跑 → 主 session 调 `submit_output_and_advance(output)` → core 返回下一 prompt → 循环。

两壳**唯一差异**：节点由谁执行 + 谁驱动 advance 循环。决策 / 写 tape / router / reducer 完全共享。

### 1.3 executor 策略接口（节点由谁执行）

复用已存在的 `Executor` 抽象（`orca/exec/interface.py`：`exec(node, ctx) -> AsyncIterator[Event]`，executor 产事件流、**不写 tape**）。新增第二种实现：

| executor | 壳 | 节点执行方式 | 驱动方 |
|---|---|---|---|
| `SubprocessExecutor`（现有） | teams | spawn `claude/opencode -p`，stream-json → translator → 事件流 | core 内部循环 |
| `InSessionExecutor`（**新增**） | orca | **协作式**：不 spawn，core 把 node prompt 交给主 session；主 session 派 task 子代理跑完，产出经「输出契约」（§5）回传；core 把回传内容经 translator 转事件流 | 主 session（外部）|

`InSessionExecutor` 与 `SubprocessExecutor` **产出同一形态事件流**（`node_started` / `agent_tool_call` / `agent_tool_result` / `agent_thinking` / `node_completed`）——这是「同一后端」的关键：无论谁执行节点，tape 里的事件序列一致。

### 1.4 tape 唯一真相源（不变）

web / TUI / CLI / in-session 都读同一 tape。in-session 开 web = `orca open` 复用 Web attach（read-only attach，CURRENT.md 2026-07-08 已实现）。

### 1.5 遗留清理清单——消灭多套接口（确保架构整洁）

> 用户铁律「无多套接口造成混乱」。本次重构**一并收掉**所有遗留补丁/多套并存，**旧路径必须删**（非保留兼容），杜绝新旧并存。逐项（现状盘点 2026-07-13 源码实证）：

| 多套并存（混乱源）| 收口动作 | 落点 |
|---|---|---|
| **决策路径 ×2**：`advance_step`（CLI 自管 tape）↔ `Orchestrator`（teams 循环）| 合一为 core（§1.1 差异面七条对齐）| §1 |
| **G2 序列对齐**：in-session vs `orca run` tape 人工对齐 | 随决策路径合一自动消失 | §1 |
| **入口机制多代残留**：v7 `command.execute.before` → v8 `transform`+marker → 批B prompt-command → model-driven | 统一 **skill 载体**（§3.1），旧机制全删 | §3 |
| **`orca.ts` 死代码**（实证 21 处）：REST `fetch` / `extractTaskOutput` / `promptAsync` / `injecting` / `MARKER_REGEX` / `serverBaseUrl` | 入口换 skill 后整段清，不保留 | §3 + 实施 |
| **`_constants.py` MARKER_REGEX**（Python + TS 双写契约）| marker 机制删，双写契约随之删 | §3 |
| **`cc_hooks.py`** 仍 hook 抽产出模式（未同步 model-driven）| 改 hook 导出最终产出（`--output-file`，§5）| §5 |
| **安装入口 ×3**：`skill install`（弃用别名）+ `in-session start`（CC-only）+ `install` | 收归 `teams install` / `orca install`（§2 分家后）| §2 |
| **setup 多套消费范式**：TUI/Web 实跑 ↔ MCP 借 prompt + 三重杠杆防跳过 | 删 setup（inputs 代填替代）| §4 |
| **输出传递**：`--output` 字符串（大产出引号风险，遗留 #3）| `--output-file`（§5）| §5 |
| **`daemon.py` 逐条 emit**（B-8：advance_step 原子决策但 `for emit: bus.emit` 逐条）| batch emit（与单次决策对齐）| §5 / 实施 |
| **错误信封 ×3**：`ExecError.phase`（8 类）↔ `ErrorKind`（11 类）↔ `InSessionError.error_kind` | phase-11 ADR 统一为 `ErrorKind` 单一权威；`InSessionError` 并入或明确为 in-session thin wrapper（不再独立 taxonomy）| phase-11 ADR 查残留 |
| **catalog 独占 MCP 层**（`iface/mcp/catalog.py`）| 下沉 core 共享（CLI/MCP/in-session 同源）| §3.2 |

> **守门**：实施按 §7 落地顺序逐项清，每步 commit「清旧 + 建新」同提交，CI grep 禁旧符号（`MARKER_REGEX` / `orca run` / 旧 `--output` 字符串路径 / `extractTaskOutput` 等）。

---

## 2. 命令分家（防误调 = 命名 + 可见性隔离）

### 2.1 `orca` = in-session CLI（LLM 唯一可见）

```
orca <wf> [意图…]          # 主入口：catalog 匹配 + inputs 代填 + bootstrap + 推进，一条龙
orca status / stop          # 查/停当前 run
orca doctor                 # 自检
orca open                   # 开 web attach 看当前 run
# 底层面板（主入口的组成步骤，也可单调）：bootstrap / next / catalog / schema
```

- `orca --help` **只列** in-session 子命令。
- skill（§3.1）**只教** `orca` 命令族。
- 错误提示不出现 `teams`。

### 2.2 `teams` = 后端 entry point（终端 / 运维）

```
teams run <wf>              # 后端独立执行（web/TUI 监控，现在的 orca run）
teams serve / open / ps     # 后端服务
teams install / validate / mcp / executor / skill   # 配置 / 开发工具（非跑 wf 时用）
```

独立 entry point，与 `orca` 并列。

### 2.3 可见性原则（隔离强度来源）

隔离强度 = `orca` 的**可见面**（`--help` / skill / 错误提示）里**完全没有 `teams` 痕迹**。**不依赖名字是否相关**——后端叫 `teams` 是因为契合产品定位，但鲁棒性钉在「可见性」上：LLM 从 skill 学到的命令集里没有后端入口，无从误调。

### 2.4 迁移

**不加兼容期，直接断**。旧 `orca run` 移除，迁 `teams run`。文档 / 测试 / 肌肉记忆一并改。

---

## 3. 入口与匹配

### 3.1 载体 = skill（不是 command / agent）

新增一个 `orca` skill，SKILL.md 集中所有调度规则：
- 必须走 in-session（只认 `orca` 命令族）
- catalog 匹配 workflow（读 description 自判）
- inputs 模型代填（读 `InputDef.description`）
- 驱动协议（派 task 子代理 → 调 `orca next` → 读 done/next 循环）

**为什么是 skill 不是 agent**：agent（subagent）是 fork 执行，会丢主 session 上下文 + 双层子代理（dispatcher fork + 节点 task 子代理）。skill 是**内联注入**主 session——不丢上下文、单层子代理（主 session 直接派节点子代理），且同样规则集中（一个 SKILL.md）+ 自带 progressive disclosure。skill 兼得 agent 的「集中」与 command 的「不丢上下文」。

各平台装各自 skills 目录：CC `.claude/skills/` / CAC `.cac/skills/` / opencode 对应目录。

> **待 spike**：① CAC skills 目录路径（CAC 是 CC 后端，应有，文档未明说）；② opencode skill 加载机制（ORCA 既有 create-workflow skill 已装两边，机制可行，需确认形态）。

### 3.2 workflow 匹配 = 纯 description（不加字段）

不加 `when_to_use` / `examples` / `tags`。复用现有 `Workflow.description`（`schema/workflow.py:288`）。catalog 列各 wf 的 description，主 session LLM 像选 skill 一样自判——**workflow = orca 的 skill 单元，description = 匹配依据**。

**实现**：catalog 现在 MCP 层（`iface/mcp/catalog.py`），**下沉为 core 共享**（两壳都用）。in-session CLI 加 `orca catalog`（列 wf + description）+ `orca schema <wf>`（inputs schema，供代填）。

### 3.3 inputs = 模型代填

主 session 读 `InputDef.description`（已有字段）从用户意图抽填。用户不敲 JSON。多字段 workflow 也由模型读每字段 description 代填。

---

## 4. 简化：删除 setup phase

### 4.1 判断：冗余，删

setup phase（`workflow.setup`）的职责是「execute 前收集信息 / 跑准备 agent」。在新架构下被覆盖：

| setup 做的事 | 新架构谁兜 |
|---|---|
| 收集结构化前置信息 | inputs 代填（主 session 读 `InputDef.description`）|
| 多轮交互问需求 | 主 session 本身（in-session 天然多轮）|
| setup agent 跑工具探索环境 | 主 session（能跑工具）|
| 产 `setup_outputs` 给 execute 共享 | inputs（本就是全节点共享）|
| MCP 壳「主 session 替 setup agent 跑」特殊范式 | 不需要（三壳统一走 core）|

动态决策另有更合适的机制（`routes` 条件分支，ORCA 已有）。setup 在「in-session model-driven + inputs 代填 + 同一后端」下是过度设计。

### 4.2 影响面（清理清单）

- schema：`Workflow.setup` 字段
- compile validator：「execute phase 不配 ask_user/gate」约束（随 setup 一起清）
- MCP server：`start_workflow(setup_required)` / `get_agent_prompt` / `setup_outputs` 注入
- RunContext：`setup_outputs`
- catalog：`has_setup` 标记
- 三重杠杆防跳过 setup

---

## 5. 执行模型与输出契约（in-session）

### 5.1 model-driven advance（已定，不变）

主 session 调 `orca next`，core（`advance_step`）推进。**不依赖宿主 idle hook / REST**（CURRENT.md 2026-07-09 补丁已证 REST 路结构性不可行）。

### 5.2 输出契约：节点产出如何到 tape（**待讨论定稿**）

现状（model-driven）：主 session 调 `next --output '<最终产出字符串>'`，core 写 tape（仅 `node_completed` + output）。**gap**：子代理的完整过程（tool calls / reasoning）不进 tape——teams 模式 tape 丰富（translator 转 stream-json），in-session 模式 tape 单薄。

候选方案：

| 方案 | 机制 | tape 丰富度 | 主 session 上下文 | 可靠性 |
|---|---|---|---|---|
| **A. `--output <str>`**（现状） | 主 session 传最终产出字符串 | 低（仅 output） | 低 | 高 |
| **B. `--output-file <path>`** | 主 session / 子代理把产出写文件，core 读文件 translator 写 tape | 中-高（取决于文件内容） | 低（指针） | 高（文件 I/O）|
| **C. 子代理完整过程进 tape** | 输出文件格式 = stream-json，与 teams subprocess **共享 translator**；宿主 hook 兜底捕获 | 高（同 teams） | 低 | 中（依赖子代理配合 / hook）|

**定案（2026-07-13 spike 后）**：**三平台统一 = 宿主脚本 hook 导出 task 最终产出**——
- 子代理**零配合**：正常用 task 工具跑完即可，不指示它写任何文件（LLM 自写格式不可靠，明确排除）。
- 宿主在 task 完成时自动触发 hook（CC/CAC `PostToolUse(Task)` type=command / opencode event hook），hook 从宿主记录提取 **task 最终产出文本**，写 `<rundir>/<run_id>/node-output-<node>.txt`。
- 主 session 调 `orca next --output-file <path>` → core 读文件 → 写 tape（`node_completed` + output）。**core 侧三平台零差异。**

**硬边界（spike 结论，接受）**：in-session 模式下，**子代理是 task 工具 spawn 的隔离子 session，其内部 tool calls / reasoning 过程不暴露给父 session 的 hook**。这是三平台（CC/CAC/opencode）共有的固有限制，非平台差异。
- 三平台父 hook 都**只能拿到 task 最终产出文本**（opencode `state.output` / CC `tool_response.content`，ORCA `extractTaskOutput` + `cc_hooks.py` 均已实证）。
- 故 in-session tape = **node 级**（`workflow_started` / `node_started` / `node_completed(output)` / `route_taken` / `workflow_completed`），**无节点内部 tool 过程**。
- 完整 tool 过程回放 = **teams 模式专属**（`SubprocessExecutor` 直接消费 subprocess stream-json，无父子 session 隔离）。用户要看完整 agent 过程 → `teams run`。

**可行性**：CC 已有 `cc_hooks.py`（`PostToolUse(Task)` shell hook 导出 task 结果，已验证）；CAC 与 CC 同构（`type:command` + `PostToolUse` + 同款 stdin JSON），脚本几乎原样平移；opencode event hook（TS）取 `state.output` 同款导出（`extractTaskOutput` 已实证路径）。三平台统一达成。

**比 `--output <str>` 的改进**：① 文件传递根治大产出引号风险（CURRENT.md 遗留 #3）；② 三平台导出路径统一；③ hook 自动捕获，不依赖主 session 转述完整度。

**已评估并排除（2026-07-13）：读宿主 transcript**
CC（`~/.claude/projects/<hash>/<sid>.jsonl`）与 opencode（`~/.local/share/opencode/storage/session_diff/<sid>.json`）确有完整子代理过程的 transcript 轨道（本机实证）。但**明确不读**，理由：① 路径/格式两平台完全不同 → 破坏统一；② 宿主内部存储非公开契约，版本敏感；③ 子 session id 从 `tool_response` 获取不保证；④ 耦合宿主内部，违反哑传输守门；⑤ 完整过程进 tape 致膨胀。
**替代（职责分工）**：ORCA tape = workflow 视角（node 级）；agent 完整过程 = 宿主原生 transcript（CC `claude --resume` / opencode storage 原生可看）。**可选轻量集成**：tape 的 `node_completed` 存子 session id + transcript 路径**指针**（不拷内容），web 点节点跳转——只存指针不解析，规避上述全部风险。要完整过程进 ORCA tape 本身 → `teams run`（subprocess stream 原生）。

### 5.3 兜底：fail-loud

万一输出文件缺失 / 格式坏 → core 干净 fail loud（`workflow_failed` + `error_kind=render_error` / `output_schema_mismatch`），清 marker，不卡死（复用 CURRENT.md 2026-07-08 compact prompt 已建的 fail-loud 路径）。

---

## 6. 平台：CAC 适配

CAC = 华为 CodeAgent CLI（CC 后端）。加 `cac` target（与 claude / opencode 平级）。skill / command 模板落 `.cac/skills/` / `.cac/commands/`。

**hook 不需要**：model-driven advance 不依赖宿主 hook（§5.1）。仅 §5.2 的「hook 兜底捕获子代理过程」可选用到 CAC `PostToolUse`（与 CC 同构，复用 `cc_hooks.py` 模式）。

详见独立评估（对话 2026-07-10）：CAC command 机制类似 CC；hook 机制与 CC 同构（settings.json hooks）。

---

## 7. 落地顺序

1. **收口批 B**（opencode in-session 主链路跑通——当前半成品、暂非功能态）。
2. **合并同一后端**：`advance_step` + `Orchestrator` → 单一决策核心；core API（§1.2）；executor 抽象加 `InSessionExecutor`（§1.3）。**最大动作，需 spec-review-adversarial。**
3. **命令分家**：`teams` / `orca` 拆分；直接断旧 `orca run`（§2）。
4. **skill 入口**：`orca` SKILL.md + catalog 下沉 core + `orca catalog` / `orca schema` + inputs 代填（§3）。
5. **删 setup**（§4）。
6. **CAC 适配**（§6）。
7. **输出契约**：`--output-file` + 子代理过程进 tape + hook 兜底（§5.2，待定稿）。

---

## 8. 待定 / 风险

| # | 项 | 说明 |
|---|---|---|
| 1 | ~~输出契约~~（§5.2）| ✅ **已定案**（2026-07-13 spike）：三平台统一 hook 导出最终产出；in-session tape = node 级（子代理隔离，完整过程是 teams 专属）。|
| 2 | `advance_step` ↔ `Orchestrator` 合并 | ✅ 差异面已 diff（§1.1 七条清单 + #5 已共享）。剩**决策 A**（in-session 节点范围，倾向线性 MVP）+ **决策 B**（驱动统一，§1.2）。|
| 3 | CAC skills 目录 spike | CAC 是 CC 后端，应有，需实证路径。|
| 4 | opencode skill 加载机制 | 既有 create-workflow skill 已装，需确认形态对齐。|
| 5 | 删 setup 影响面 | §4.2 清单，逐项清 + 测试。|
| 6 | 同一后端合并是最大架构动作 | 必须经 `spec-review-adversarial` + `test-coverage-e2e` 真链路验证。|

---

## 9. 决策清单（已冻结，勿重新讨论）

1. 同一后端 core，`teams` / `orca` 是入口壳（§1）。
2. `orca` = in-session；`teams` = 后端；可见性隔离；不加兼容期直接断（§2）。
3. 载体 = skill；纯 description 匹配；inputs 模型代填（§3）。
4. 删除 setup phase（§4）。
5. 动态构建本期不做（§0.3）。
6. 先收口批 B 再开工（§7）。

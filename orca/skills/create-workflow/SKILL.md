---
name: create-workflow
description: >-
  生成或转换一个 Orca workflow（YAML + agent md），或给已有 workflow 设计运行时图表。
  逐步引导式：先与用户确认 DAG 草图，再逐 agent 讨论设计（固化/校验/可视化），
  每个 agent 落盘后派 sub-agent 洁净审查闭环。用户想新建多 agent 编排、把已有
  agent prompt / 别的格式 workflow 转成 Orca 形态、或给 workflow 加可视化图表时使用。
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Task, AskUserQuestion
---

# create-workflow

<purpose>
把用户的意图（自然语言描述）**或**已有素材（一个文件夹里的 agent md / 别的 workflow / 散落的 prompt）
归一化成一个可跑的 Orca workflow——per-workflow 自包含目录（`workflow.yaml` + 必要的
`agents/`），需要时配运行时图表。
skill 自己消化 Orca 契约，用户不碰 schema、不选 agent 声明方式、不写 routes。

两个入口：
- **建/转换 workflow**（默认）：走完整 Stage 0-4 流程（见「阶段流程」）。
- **给已有 workflow 加图表**：跳过 Stage 0-2，直入图表流程（chart-designer 扫描 →
  plan 确认 → 落地 → 校验闭环）。
</purpose>

## 交互模式铁律

🔴 **每个 gate 停下、结束回合、等用户**——未确认不推进下一 Stage。本 skill 是逐步引导式，
不是一句话甩手一次跑完。有 AskUserQuestion 工具的平台优先结构化提问；没有则以纯文本问题收尾。

- **gate 通过判据**：用户明确确认（「可以 / 确认 / go」）；用户提修改 → 改完**重呈该 gate**。
- **批量指令**：用户说「这 N 个一批」「剩下的跳过讨论」→ 跳过的是**讨论 gate**，
  🔴 **落盘 + 校验 + 审查闭环不跳**（讨论可跳，审查不可跳）。
- **推翻已确认设计**：更新 design.md 对应节（标 rev N），重走该 agent 的落盘 + 闭环。
- 落盘路径**不属于 gate**：用户指定路径优先，否则默认 `./workflows/<name>/workflow.yaml`
  （per-workflow 目录，见「产出布局」）直接写最终路径，写完告知（不阻塞问路径）。

## 产出布局（per-workflow 目录）

每个 workflow 一个自包含目录（目录名 = workflow `name`），默认 `./workflows/`：

```
./workflows/<name>/
  workflow.yaml          # 编排拓扑
  agents/                # 本 workflow 专属 agent 池（不跨 workflow 共享）
    <agent>.md           # 文件 agent
    <agent>/agent.md     # 文件夹 agent（scripts/ 资源随行）
    <agent>/scripts/...
  subagents/             # sub-agent 定义 md（有则落此）
  <name>-design.md       # 开发期设计文档（见「断点续传」；内容绝不进 agent.md / yaml）
```

引用的资产全部收进本目录——用户提供的 agent md 原样复制进来落 `agents/`，不留目录外。
本文及 reference 各处写的 `agents/<agent>...` 相对路径，一律以 workflow.yaml 所在的
workflow 目录为基准。

## 断点续传（design.md）

开工先找 `./workflows/<name>/<name>-design.md`（默认落点 = workflow 目录内；gate 1 时可应用户要求改指它处）：

- **存在** → 读「进度」节，向用户报告当前 Stage + 下一步，从断点继续；
- **不存在** → 新建，随阶段渐进写。结构：

```markdown
# <name> workflow 设计文档
## 进度          # 当前 Stage / 已完成 agent（findings 状态：fixed|waived(理由)）/ 下一步
## DAG           # Stage 1 定稿（gate 1 后冻结，改动标 rev N）
## chart registry   # 表列：label | title | type | 来源节点 | 状态（planned|landed）
## <agent>（rev N）  # 每 agent 一节：六项议程结论 + 审查 findings 状态
```

design.md 是开发期文档：**绝不进 agent.md / yaml**（产物是产品说明书，见 writing-style）。

## 归一化 DAG（中间模型）

无论哪种输入，先建一个**归一化 DAG**——节点（命名，各带 prompt 来源 / 脚本资源 / executor+model）
──控制流边──> 节点。两种输入都汇到它：

- **形态 A（描述意图）**：用户说"我要个 X workflow" → 据 X 推断需要哪几个 agent、各自职责、
  串行/并行/分叉合并/循环/条件。
- **形态 B（已有文件夹）**：用户给路径 → `Glob`/`Read` 扫描：`.md`（当 prompt 抽）、
  `.yaml`/`.json`（抽节点和边）、纯文本 prompt。**不假定输入形态**——外部编排工具导出的
  yaml/json、手写草稿都按「读出 agent + 读出顺序」通用抽取。

素材就近读：契约参考 + 例子在本 skill 同目录（`reference/` + `examples/`），直接 `Read`；
用户素材在指定路径，`Read` 即可——**不派探索子任务**。

## 阶段流程

```
Stage 0  意图接收：归一化素材；模糊问 1-2 个业务问题（问业务不问 schema）；建/读 design.md
Stage 1  DAG 草案 → gate 1
Stage 2  逐 agent（默认逐个，可批量）：六项议程 → gate 2 → 落盘 → V1 + V3 审查闭环
Stage 3  组装：routes/outputs/parallel 拼全 + 全量 tars validate + 三档 checklist + 三脚本全量
Stage 4  终验交付 → gate 3
```

**gate 1 呈现物**：草 DAG（节点名 + 箭头，parallel 用括号组，`$end` 收尾；条件分支标箭头）
+ 每 agent 一句话职责 + input 三档草案 + 可视化意图初判。
**gate 2 呈现物**：六项议程结论（含 chart plan 若有）。
**gate 3 呈现物**：DAG 图 + 每 agent 校验状态表（V1/V3 结论）+ chart 清单 + 落盘路径。

**校验时机**：`tars validate` 需完整 DAG，**Stage 3 才跑**（Stage 2 中间态会假红）；
Stage 2 每 agent 落盘后跑 agent 级 V1 两脚本（见 H9）。

**图表入口**（已有 workflow 加图表）：chart-designer 扫描该 workflow 全部 scripts → plan →
gate 确认 → 落地 → `check_charts.py` + registry 写入 design.md（无则建）。落地若改动
agent.md / 新增 scripts → 同跑 V1 两脚本 + V3 审查闭环（审查不可跳）。

## Stage 2 六项议程（每 agent 与用户逐项对齐）

| # | 议程 | 产出 |
|---|---|---|
| ① | 职责与输入/输出契约 | 一句话职责 + 上游引用形态 + `output_schema` 草案 |
| ② | 执行步骤分解 | 编号步骤（可机械执行的步骤在此显形） |
| ③ | 固化分析 | 每步标 S1/S2/S3；S1 步骤定 `scripts/<file>` 名 |
| ④ | 校验计划 | V1=脚本断言；V2=schema 字段；V3=validator criteria 或不需要 |
| ⑤ | Tier B 推断项 | 要从用户代码推断的事实清单（缺失走哨兵，绝不造假） |
| ⑥ | 可视化判定 | 结构化数据 + 值不值得图；需要 → 当场派 chart-designer，plan 并入 gate 2 |

议程模板（逐项展开形态）与固化/校验判据：**`reference/solidify-validate.md`**（Stage 2 每轮先读）。

## 审查闭环（每 agent 落盘后立即，不攒到最后）

```
落盘（yaml 节点 + agent.md + scripts + chart 落地若已确认）
  → V1：check_agent_md_static.py + check_dev_residue.py（本 agent 产物，见 H9）
  → V3：派 agent-reviewer sub-agent（派单模板见 solidify-validate.md §5.1）
       findings 五类：A 考古残留 / B 意图偏离（对照 design.md 该 agent 节）/ C 固化漏判 /
       D 契约违反 / E 用户输入权威三件套缺落（涉 port 用户逻辑时）
  → 回修 → 复审（同 reviewer 带上轮 findings + 修复 diff 核对）
  → 全部 fixed 或用户显式 waive（理由记 design.md）才过
  → 上限 3 轮，超限 fail loud 呈给用户裁决
```

## sub-agent 派单（两个角色）

| 角色 | 何时派 | 它自读（主 agent **不预读**这些 L2 文件） |
|---|---|---|
| **agent-reviewer** | 每 agent 落盘后必派 | 洁净契约 + writing-style + design.md 该 agent 节 |
| **chart-designer** | 议程⑥判定需要图表时；图表入口整 workflow 扫描 | `reference/charts/` 三件 + `examples/charts/` + registry（防撞） |

完整派单模板（占位符替换后直接用）：**`reference/solidify-validate.md` §5**。
层次原则：主 agent 只持流程与判定入口（本文件 + L1 参考），专家知识由 sub-agent 按需加载。

## 双阶梯判定入口

**固化阶梯（生成侧，逐步降级）**——每步从 S1 往下试：

| 级 | 判据 | 落点 |
|---|---|---|
| S1 脚本固化 | 无语义判断、输入输出可机械定义（解析/改写/聚合/搬运/渲染） | `agents/<name>/scripts/`，agent.md 一行调用 |
| S2 模板填空 | 骨架固定、少数槽位需 LLM 判断 | 模板脚本 + 槽位（`output_schema` 约束） |
| S3 LLM 生成 | 需读代码/理解语义/写 prose | prompt 全权生成 + 配校验 |

**校验阶梯（校验侧，逐级升级）**——能低级绝不高级：

| 级 | 对象层（引擎原语） | 元层（本 skill） |
|---|---|---|
| V1 脚本校验 | 节点校验脚本（exit code 即裁决） | `tars validate` + 本 skill `scripts/check_*.py` |
| V2 结构校验 | `output_schema`（字段拼错即 error） | benchmark golden |
| V3 语义校验 | `validator:`（criteria + max_retries） | agent-reviewer sub-agent |

判定信号 / 正反例 / 固化漏判启发式：**`reference/solidify-validate.md` §1-§3**。

## agent 三态自动决策（用户不选）

判定**只看节点的 prompt 来源**，不看长短：

| prompt 来源 | 用法 |
|---|---|
| **skill 自己起草**（形态 A，或形态 B 里需要补的节点） | **内联 `prompt:`**——一律内联，**不要**为它单独建 agent md |
| **prompt 片段文件**（`.prompt` / 明显是单次 prompt 文本，非角色定义） | **内联**——读进节点的 `prompt:`，**不**建 agent md |
| **用户提供了独立 agent 角色 md** / 从外部 skill 转换来（可复用角色） | `agent: <name>` + 原样落 `agents/<name>.md`（**引用，不重写**） |
| agent 要带脚本/资源（.py/.sh/refs） | 文件夹 agent `agents/<name>/agent.md` + `agents/<name>/scripts/` |

🔴 **同一 workflow 内常混用 inline + agent-ref**——用户给的 agent 角色 md 用 `agent:` 引用，
skill 起草的补全节点保持**内联**。**绝不**把 skill 起草的节点也落成 agent md（过度物化）。
区分关键：**角色 md = 可复用人设**；**prompt 片段 = 单次任务指令**。

🔴 **名称一致性铁律**：`agent: <name>` 必须**逐字等于**落盘文件/文件夹名。落盘前自查。

> executor/model 取用户环境已配置项；用户没指定 → `executor` 显式写 `opencode`，`model` 留空走该 executor 默认。

## 硬规则（常见坑，必须遵守）

**H1 文件夹 agent 目录契约**：布局 `agents/<name>/agent.md` + `agents/<name>/scripts/<file>`
（脚本**必须**进 `scripts/` 子目录）；agent.md = frontmatter（`description`/`model`/`tools`）+
body prompt；body 引用脚本**必须** `$ORCA_AGENT_RESOURCES/scripts/<file>`（外部 skill 相对
引用要重写成这个）。

**H2 fan-in / 合并节点**：用户说"合并/汇总"但没明确要 LLM 理解 → `set` 节点（无 token、
确定）；明确要"综合/提炼"才用 `agent`。取 parallel 分支输出必须 `<组名>.output.outputs.<分支名>`。

**H3 优先 AgentNode 原生字段**：结构化输出 → `output_schema`；语义校验+重跑 → `validator`；
瞬时失败重试 → `retry`。🔴 `validator` 与 `retry` 正交别混。`outputs:` 模板取整段用
`{{ node.output }}`（不加 `.json`；`.json` 只在 `when:` 路由里用）。

**H4 workflow 必有 `outputs`**：终态输出映射至少暴露末节点产物（`result: "{{ last.output }}"`；
script 节点用 `.stdout`）。

**H5 散 agent md → 引用而非重写**：用户给的 agent md 用 `agent:` 引用 + 原样落盘；不擅自
改写成内联、不擅自加 `{{ 上游.output }}` 数据传递。

**H6 节点最小化 / entry 即分支**：parallel 分支能直接当入口就以它为 `entry`（幂等跳过），
不加冗余 starter。每个 agent 节点显式 `executor`；`model` 按需显式（用户指定/环境已配置时写，
**全名** `provider/name`；未指定留空走该 executor 默认）。

**H7 script 节点 vs 文件夹 agent**：`script` 节点在 cwd 跑命令、**不迁移脚本**（脚本留原位）；
只有文件夹 agent 才迁移脚本到 `scripts/` + `$ORCA_AGENT_RESOURCES`。用户说"串脚本"且无人设
→ script 节点链；"封成 agent 跑脚本"→ 文件夹 agent。

**H8 `description` 可区分**：一两句说清功能与目的（`orca list` 语义匹配靠它）。生成前
`orca list` 查现有 description，撞车或换皮 → 问用户本质区别，不闷头生成。

**H9 元层校验三脚本**（本 skill `scripts/` 下，Stage 2 每 agent 落盘后 + Stage 3 全量跑）：

```bash
python3 <skill_dir>/scripts/check_agent_md_static.py <workflow.yaml 或 agent 路径>
python3 <skill_dir>/scripts/check_dev_residue.py <产物路径...>   # yaml + agents md + scripts
python3 <skill_dir>/scripts/check_charts.py <workflow 目录>       # 有 chart 落地时
```

exit 0 过 / exit 1 有 findings 自改重跑 / exit 2 路径用法错。脚本规则（含豁免表与白名单
语义）已固化在脚本内，**不在此重复**。

## input 三档（紧凑判定；细节单源 contract §6）

**总纲**：inputs 只放「下游 agent 无法执行 / 会失控」的必须项。**代码里能 grep 出来的是事实
（→ Tier B）；代码里不存在的是意图（→ Tier A）。会静默产出错误交付物的回退路径必须
fail loud / 问用户（永不 silent default）。**

| 档 | 标签 | 性质 | 放在 |
|---|---|---|---|
| Tier A | `[ask]` | 业务决策（意图/预算/KPI/硬件/模型入口/业务命令）；Tier A 子类判据见 contract §6 | **input**（必填） |
| Tier B | `[infer]` | 代码事实（agent 读用户代码可得）；缺失走 ask-user 哨兵 | setup 型节点 `output_schema` 字段（惯例把首个 agent 节点当 setup：infer-once 代码事实向下游 propagate） |
| Tier C | `[default]`/`[advanced]` | 有合理工程默认，99% 用户不该决策 | 固化：yaml `default` / agent.md 模板 / 脚本默认 |

**反向判据**（任一命中 → 强制下沉的判据表，见 contract §6）。

Tier B 典型项 / 哨兵 JSON 形态 / 三档 checklist / Tier A 子类全表：**`reference/orca-workflow-contract.md` §6**。
含 Tier B 项的 agent.md 必加「缺失必填输入时（严禁造假）—— ask-user 哨兵」段（形态见同节）。
workflow 必有 `seed`（默认 0）。

## 产物写作规范

产物是**产品说明书，不是设计日志**。三条底线（详 `reference/writing-style.md`）：

1. 受众是使用者（执行 LLM + 复用者），不是作者——开发考古（迁移出处/issue 编号/版本嵌入）
   放 commit / `docs/specs/`，**绝不进产物**；
2. description / prompt / agent.md 只答 what-input-output，不答 why-history；
3. 红线用 ❌ 列举；关键正确性用 `output_schema` / `when` 做成引擎硬检查。

考古自检**跑脚本**：`check_dev_residue.py <全部产物>`（规则固化在脚本，exit 0 即过）。
逐 agent 的受众翻转通读由 agent-reviewer sub-agent 执行（见审查闭环）。

## 输出语言

YAML 字段名、`kind`、`executor` 等是固定契约。prompt 文本、`description`、`name` 用用户的
语言。Jinja2 占位符 `{{ inputs.x }}` / `{{ <node>.output }}` 照抄。

## 契约在哪（分层）

| 层 | 文件 | 谁读 / 何时 |
|---|---|---|
| L0 主控 | 本 SKILL.md | 常驻 |
| L1 判定 | `reference/orca-workflow-contract.md` | 主 agent——设计节点/定 inputs 时 |
| L1 判定 | `reference/writing-style.md` | 主 agent——写产物前；agent-reviewer |
| L1 判定 | `reference/solidify-validate.md` | 主 agent——Stage 2 每轮议程前 |
| L2 专家 | `reference/agent-prompt-cleanliness-contract.md` | agent-reviewer sub-agent 自读 |
| L2 专家 | `reference/charts/{decision-table,chart-api,patterns}.md` | chart-designer sub-agent 自读 |
| 样板 | `examples/`（workflow yaml + `charts/` 图表脚本） | 按需 |

<success_criteria>
- [ ] 全部 gate 经用户确认（或用户明示批量/跳过指令）
- [ ] design.md 完整：进度 / DAG / chart registry / 每 agent 节 + findings 状态（fixed/waived+理由）
- [ ] 每 agent：V1 两脚本 0 error；V3 findings 清零或用户显式 waive
- [ ] `tars validate` 0 error（Stage 3 全量）；每个可达路径终止；workflow 有 `outputs`（H4）
- [ ] 固化纪律：S1 步骤全在 `scripts/`，agent.md body 无控制流内联（check_agent_md_static 0 error）
- [ ] chart（若有）：label/title/chart_type 字面量、label+title 全局唯一、调用 try/except 包裹，
      registry 已更新，check_charts.py 0 error
- [ ] input 三档：每 input 归档 + 标签起头；Tier B 下沉 infer-once；Tier C 固化；有 `seed`
- [ ] 产物洁净：check_dev_residue.py 0 error + agent-reviewer 受众翻转通读通过
- [ ] skill 起草节点保持内联；用户 agent md 用 `agent:` 引用不重写（三态铁律）
</success_criteria>

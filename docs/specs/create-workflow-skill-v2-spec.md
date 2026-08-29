# create-workflow skill v2 实现 SPEC

> **依据**：[`create-workflow-skill-v2-design-draft.md`](create-workflow-skill-v2-design-draft.md)（2026-08-21，三项拍板 P1 纯交互 / P2 逐个可批量 / P3 V1 落 skill 脚本已钉）。本文件 = 实现级契约：文件清单、每文件职责、脚本 IO 契约、验收标准。草稿是语义权威，SPEC 是落地映射；冲突时以草稿为准并回改本文件。
> **不动引擎**：`orca/schema` / `orca/compile/validator.py` / `tars validate` 零改动。
> **脚本硬约束**：`scripts/` 三脚本 **stdlib-only**（它们随 skill 安装到用户环境运行，无 orca 包可 import），fail loud（findings → exit 1）。

---

## 1. 交付物清单

```
orca/skills/create-workflow/
├── SKILL.md                          # 重写（v2，§2）——旧「全程不阻塞」铁律移除
├── reference/
│   ├── orca-workflow-contract.md     # 修改：§6 三档细节已是单源（SKILL.md 收敛后指向此处，本文基本不动）
│   ├── writing-style.md              # 基本不动（reviewer 验证引用一致）
│   ├── agent-prompt-cleanliness-contract.md  # 修改：§8/§9 执行流程改 sub-agent 审查口径（§3）
│   ├── solidify-validate.md          # 新增（§4）：双阶梯判定 + 每 agent 议程模板
│   └── charts/                       # 迁移自 orca/skills/design-charts/reference/（内容原样 + 路径修正，§5）
│       ├── chart-api.md
│       ├── decision-table.md
│       └── patterns.md
├── examples/
│   ├── *.yaml                        # 现有 4 个不动
│   └── charts/                       # 迁移自 design-charts/examples/（3 个 .py 原样）
├── scripts/                          # 新增（§6）：元层 V1 三脚本，stdlib-only
│   ├── check_dev_residue.py
│   ├── check_agent_md_static.py
│   └── check_charts.py
└── benchmark/cases/17-chart-integration/   # 新增（§7）
orca/skills/design-charts/            # 删除（整目录）
orca/iface/cli/install_cmds.py        # 修改：legacy 清理列表加 "design-charts"（§8）
tests/
├── test_skill_v1_checks.py           # 新增（§9）：三脚本单测
└── test_skill_benchmark.py           # 修改：case 17 守门 + chart 断言（§7）
tests/iface/cli/test_install_cmds.py  # 修改：随包 skill 列表断言去 design-charts（§8）
```

## 2. SKILL.md v2 契约

### 2.1 frontmatter

- `description`：覆盖三个触发面——①新建/转换 workflow（强交互逐步引导，DAG 确认 + 逐 agent 设计确认 + 洁净审查闭环）；②给已有 workflow 设计运行时图表；③关键词「workflow / 编排 / agent 图表 / 可视化」。
- `allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Task, AskUserQuestion`（**新增 Task**——派 sub-agent 审查/图表设计；**新增 AskUserQuestion**——gate 结构化提问，白名单缺它则 gate 机制不可执行——SPEC-R1-F）。

### 2.2 章节清单（顺序固定）

| # | 章节 | 契约要点 |
|---|---|---|
| 1 | `<purpose>` | 两入口：新建/转换（全流程）+ 已有 workflow 加图表（直入图表流程）。skill 消化 Orca 契约，用户不碰 schema |
| 2 | 交互模式铁律 | 🔴 每 gate **停下结束回合等用户**，未确认不推进下一 Stage；支持平台用 AskUserQuestion 结构化提问；用户批量/跳过指令见 §2.3；**讨论可跳，审查不可跳**（跳过讨论的 agent 仍走落盘后 V1+V3 闭环） |
| 3 | 断点续传 | 开工先找 `<workflow_dir>/<name>-design.md`：存在 → 读「进度」节，报告当前 Stage + 下一步，从断点继续；不存在 → 新建并随阶段渐进写。design.md 是开发期文档，绝不进 agent.md/yaml |
| 4 | 归一化 DAG | 现有「pivot 到归一化 DAG」段保留（形态 A 描述意图 / 形态 B 已有素材），服务于 Stage 0/1 |
| 5 | 阶段流程 Stage 0-4 | 见 §2.4 |
| 6 | Stage 2 六项议程 | 见 §2.5（表 + 指向 solidify-validate.md 议程模板） |
| 7 | 审查闭环 | 见 §2.6 |
| 8 | sub-agent 派单契约 | 两派单模板（reviewer / chart-designer）概要 + 完整模板在 solidify-validate.md；主 agent **不加载** charts/ 与洁净契约正文（L2 由 sub-agent 自读） |
| 9 | 双阶梯判定入口 | S1-S3 / V1-V3 紧凑表（草稿 §3 两表逐字）+ 细节指针 → solidify-validate.md |
| 10 | agent 三态自动决策 | 现有表 + 两条铁律（内联优先 / 名称一致性）保留 |
| 11 | 硬规则 H1-H8 | 现有保留；H8 后追加 **H9：元层校验三脚本**——Stage 2 每次落盘后跑 `check_agent_md_static.py` + `check_dev_residue.py`（本 agent 产物），Stage 3 跑全量 + `check_charts.py`（命令行形给出） |
| 12 | input 三档（紧凑） | 三档表 + 判定总纲 + 「Tier B 典型项 / 哨兵 JSON / checklist 细节见 contract §6」指针；哨兵 JSON 示例**移除**（contract §6 已有，单源） |
| 13 | 产物写作规范（紧凑） | 三底线 + 指针 writing-style；考古自检改指「跑 `scripts/check_dev_residue.py <产物路径...>`」——手写 grep 表**删除**（已固化进脚本，单源） |
| 14 | 落盘路径规则 | 保留：用户指定路径优先，默认 `./workflows/<name>.yaml`，直接写最终路径，不问路径（低价值确认不属于 gate） |
| 15 | 输出语言 | 现状保留 |
| 16 | 契约在哪（分层指针表） | L0=SKILL.md / L1=contract+writing-style+solidify-validate（主 agent 按阶段读）/ L2=charts/*+洁净契约（sub-agent 读） |
| 17 | `<success_criteria>` | 更新：gate 全过、design.md 完整、每 agent findings 清零或 waive、三脚本全绿、tars validate 0 error、benchmark 新 case 语义 |

### 2.3 gate 与指令词汇

| gate | 触发点 | 呈现物 | 通过判据 |
|---|---|---|---|
| gate 1 | Stage 1 末 | 草 DAG(ascii) + 每 agent 一句话职责 + input 三档草案 + 可视化意图初判 | 用户明确确认（「可以/确认/go/改X后再看」——改后重呈） |
| gate 2 | Stage 2 每 agent 议程后 | 六项议程结论（含 chart plan 若有） | 同上 |
| gate 3 | Stage 4 交付前 | 校验状态汇总表 | 用户确认收货 |

- 批量指令：「这 N 个一批」「剩下的跳过讨论」→ 跳过的是**讨论 gate**，落盘 + V1/V3 闭环**不跳**。
- 用户改口推翻已确认 agent 设计 → 更新 design.md 对应节（标 rev N），重走该 agent 落盘+闭环。

### 2.4 阶段流程

```
Stage 0  意图接收：归一化素材（形态 A/B）；模糊问 1-2 个业务问题；建/读 design.md
Stage 1  DAG 草案 → gate 1
Stage 2  逐 agent（默认逐个，可批量）：议程六项 → gate 2 → 落盘（yaml 节点 + agent.md +
         scripts + chart 落地若确认）→ V1（agent 级）→ V3（agent-reviewer）→ 闭环
Stage 3  组装：routes/outputs/parallel 拼全 + 全量 tars validate + 三档 checklist +
         全量三脚本（residue/agent_static/charts）+ chart registry 查重
Stage 4  终验交付 → gate 3：DAG 图 + 每 agent 校验状态表 + chart 清单 + 落盘路径
```

- tars validate 在 Stage 3 才有意义（需完整 DAG）；Stage 2 中间态不跑全量 validate（会假红），agent 级检查用两脚本。
- 「已有 workflow 加图表」入口：直入图表流程 = chart-designer 扫描该 workflow 全部 scripts → plan → gate → 落地 → `check_charts.py` → registry 写入 design.md（无则建）。**落地若改动 agent.md / 新增 scripts → 同跑 V1 两脚本（agent_static + dev_residue）+ V3 agent-reviewer 闭环**（审查不可跳，与 Stage 2 同口径——SPEC-R1-N5）。

### 2.5 Stage 2 六项议程（SKILL.md 内为表；模板细节在 solidify-validate.md）

①职责与输入/输出契约（output_schema 草案）②执行步骤分解 ③固化分析（每步 S1/S2/S3）④校验计划（产物 V1/V2/V3，对象层映射引擎原语）⑤Tier B 推断项清单 ⑥可视化判定（需要 → 当场派 chart-designer，plan 并入 gate 2）。

### 2.6 审查闭环（SKILL.md 内逐字语义）

```
落盘 → V1（check_agent_md_static + check_dev_residue，本 agent 产物）
     → V3（agent-reviewer sub-agent，五类 findings：A 考古残留 / B 意图偏离（对照
        design.md 该 agent 段）/ C 固化漏判 / D 契约违反 / E §10 三件套缺落）
     → 回修 → 复审（同 reviewer 带上轮 findings + 修复 diff 核对）
     → 全部 fixed 或用户显式 waive（理由记 design.md）才过
     → 上限 3 轮，超限 fail loud 呈给用户裁决
```

### 2.7 design.md 最小 schema（SPEC-R1-N4，承接草稿 O1/O2）

```markdown
# <name> workflow 设计文档
## 进度          # 当前 Stage / 已完成 agent（findings 状态：fixed|waived(理由)）/ 下一步
## DAG           # Stage 1 定稿（gate 1 后冻结，改动标 rev N）
## chart registry   # 表列：label | title | type | 来源节点 | 状态（planned|landed）
## <agent>（rev N）  # 每 agent 一节：六项议程结论 + 审查 findings 状态
```

- 落点默认 `<workflow_dir>/<name>-design.md`；**gate 1 时可应用户要求改指它处**（草稿 O1）。

### 2.8 v1 → v2 删改对照（重写 SKILL.md 时逐条核对——SPEC-R1-N9）

| v1 内容 | v2 处置 |
|---|---|
| 「全程不阻塞等待确认」铁律 + 「不要用 AskUserQuestion」禁令 | **删除**（翻转为 §2.2-2 gate 铁律） |
| 产出过程 step 3 两条「主 agent 直读洁净契约做通读/§10 检查」指令 | **改派 agent-reviewer sub-agent**（L2，主 agent 不加载契约正文） |
| 手写考古 grep 表（标准 + 宽口径兜底） | **删除**——固化进 `scripts/check_dev_residue.py`，SKILL.md 只留调用命令 |
| input 三档长段（Tier A/B/C 典型项 + 哨兵 JSON 示例 + checklist 细节） | **收敛**——SKILL.md 留三档表 + 判定总纲；典型项/哨兵 JSON/checklist 单源在 contract §6，指针过去 |
| 产出过程 step 0「素材就近读、别派探索子任务」 | **保留**，并入 Stage 0 |
| H6 的 executor 默认 opencode 提示 | **保留**（仍在 H6） |
| 画草 DAG 报告 + 「不要问是否确认」 | **改写**——草 DAG 保留但改为 gate 1 呈现物（等确认而非播报即完） |

## 3. agent-prompt-cleanliness-contract.md 修改（§8/§9 执行流程改 sub-agent 口径）

- §8 审查法：保留受众翻转通读定义，执行主体从「作者自审」改为「作者自审 + **agent-reviewer sub-agent 独立审**（create-workflow v2 起 Stage 2 逐 agent 必派）」；通读仍是裁决方法。
- §9 执行流程：步骤改为「① tars validate（§3 窄表）② 元层脚本（宽表 + 静态启发）③ agent-reviewer sub-agent 受众翻转通读（§4 + 盲区）④ findings 闭环」。
- 其余节（§0-§7、§10）不动；CLAUDE.md 对本文件路径引用不变。

## 4. reference/solidify-validate.md 内容契约（新增）

产品说明书式，受众 = 使用 create-workflow 的主 agent（prompt-adjacent，须洁净）。章节：

1. **固化阶梯 S1-S3**：草稿 §3.1 表逐字 + 每级判定信号与正反例（S1 信号：解析/改写/聚合/搬运/渲染、无语义判断；S2 信号：骨架固定槽位少；S3 信号：需读用户代码/理解语义/写 prose）。
2. **校验阶梯 V1-V3**：草稿 §3.2 表逐字（含对象层引擎原语映射：folder-agent scripts / output_schema / validator）。
3. **固化漏判启发式**（C 类 findings 的判据来源）：agent.md body 多行 bash 含 for/if/while、`python -c` 内联逻辑、确定性解析聚合重复出现 → 应抽 `scripts/`；单行 operational 命令（jq/ruff/python <file>）合法内联。
4. **每 agent 议程模板**（Stage 2 逐项展开的 checklist 形态，六项各一小节 + 填写示例）。
5. **sub-agent 派单模板 ×2**（占位符 `<skill_dir>` / `<artifact_paths>` / `<design_section>`）：
   - **agent-reviewer**：职责（受众翻转通读 + 意图对照 design.md 段 + 固化漏判 + 契约 12 条 + 条件性 §10 三件套）；必读文件 = 洁净契约 + writing-style（给路径自读）；输出 = findings 列表（五类 / 位置 file:line / 修法建议），无 finding 也要显式报「clean」。
   - **chart-designer**：职责（数据特征 → chart_type 匹配 → plan → 确认后落地 scripts）；必读 = `<skill_dir>/reference/charts/` 三件 + `<skill_dir>/examples/charts/`；输入含 **design.md 的 chart registry 已占用 label/title**（防撞）；**label 命名约定**：`/` 分层（如 `bench/metrics`），同 label 不同 title = 前端分组折叠、同 label+title = 替换更新（dedup 语义——SPEC-R1-M3）；输出 = plan（label/title/type/axes/rationale/模式 inline|sidecar|finalize，**三者均字符串字面量**），落地后自查 H1-H8 硬规则（try/except、label+title 唯一、heatmap 三字段、pareto 方向、降采样交给默认、脚本经 `$ORCA_AGENT_RESOURCES` 引用、不碰 YAML）。

## 5. charts/ 迁移契约

- `design-charts/reference/{chart-api,decision-table,patterns}.md` → `create-workflow/reference/charts/`，`design-charts/examples/*.py` → `create-workflow/examples/charts/`：**内容原样**，仅修路径引用：
  - `patterns.md` 三处 `examples/xxx.py` → `../../examples/charts/xxx.py`（相对 reference/charts/）；
  - `decision-table.md` 一处 `reference/chart-api.md` → `chart-api.md`（同目录）；
  - `chart-api.md` 无外引（实现时 grep 复核）。
- design-charts SKILL.md 的流程知识（Step 1 inventory / Step 2 match / Step 3 recommend / Step 4 modify + H1-H8 硬规则）**不迁移原文**：inventory/match/recommend 吸收进 chart-designer 派单模板（§4），H1-H8 吸收为其落地自查清单；决策表核心信号表（时序/分组/矩阵/散点/雷达/表格）在 SKILL.md §2.2 章节 8 概要一句话提及即可（细节在 decision-table）。

## 6. 元层 V1 脚本契约（scripts/，stdlib-only，fail loud）

通用：`python3 <script> <paths...>`；findings 打到 stdout（`<file>:<line> [<rule>] <excerpt>`）；命中 error 级 → exit 1，仅 warning → exit 0（warning 前缀 `[warn]`）。`--help` 有 usage。

**通用硬语义（SPEC-R1-R4/N8，三脚本一致）**：

- **扫描清单摘要**：对每个输入路径输出一行 `<path> → N files`（check_charts 加 `/ M call sites`）——守门靠它做**正控断言**（防「零扫描即绿」的 vacuous pass）；「0 files」是合法输出（inline-only workflow 跑 agent_static / 无图表 workflow 跑 charts），exit 仍 0，但 stdout 可见。
- **路径不存在** → stderr 报错 + **exit 2**（用法错，与 findings 的 exit 1 区分）。
- **非 UTF-8 文件** → `errors="replace"` 读入 + 该文件 `[warn]` 一行，不中断。
- **`.py` 语法错不可解析** → `[parse]` error（fail loud，不静默跳过——实现期裁决回卷）；`.py` 文本按 CPython 同款剥 UTF-8 BOM 后再 parse。
- **后缀过滤仅用于目录递归**（.yaml/.md/.py，charts 脚本只 .py）；**显式传入的文件无论后缀都扫**（宽松超集，无害）。

### 6.1 check_dev_residue.py

- **输入**：一个或多个产物路径（.yaml/.md/.py；目录 → 递归扫三类后缀）。
- **规则**（error 级，pattern 表自现 SKILL.md v1 手写 grep 表迁移）：
  - 宽口径开发编号：`[A-Z]+-[0-9]+`；
  - 迁移/考古词：`迁移自|analogue of|leaves off|前作|前身是|演进历史|v[0-9]+ 已嵌入|spec-review|spec_review|plan [a-z-]+ §|SPEC 20[0-9]{2}-`；
  - 宽口径兜底（**ASCII 环视词边界**——`\b` 在 CJK 邻接时静默失效（中文字符属 `\w`，「按P5处理」永不命中），故用 `(?<![A-Za-z0-9_])...(?![A-Za-z0-9_])` 形态——实现期回卷）：`(?<![A-Za-z0-9_])P[0-9](?![0-9A-Za-z_])`（**单数字** P0-P9，开发优先级/里程碑语义）`|(?<![A-Za-z0-9_])Increment [A-Z](?![A-Za-z0-9_])|(?<![A-Za-z0-9_])code-reviewer(?![A-Za-z0-9_])|review #[0-9]|(?<![A-Za-z0-9_])SR[0-9](?![A-Za-z0-9_])|finalize 20[0-9]{2}`；
  - **P 分位白名单**：`(?<![A-Za-z0-9_])P[0-9]{2,3}(?![0-9A-Za-z_])`（P50/P90/P95/P99/P999 = latency 分位）**不报**——case 17 即 latency benchmark，产物自然含 P95（SPEC-R1-C）；
- **豁免语义（SPEC-R1-B）**：error 正则的某次命中 span 被**任一豁免 regex 的匹配包含** → 仅 suppress 该命中（同行其它命中照报）。豁免表 = `{ViT-\d+, GPT-\d+, YOLO-\d+, UTF-\d+, ISO-\d+}`（按包含语义：`ViT-14` 的 `[A-Z]+-[0-9]+` 命中 `T-14` 被 `ViT-\d+` 覆盖）；`--allow <regex>` CLI 追加。注意 `ResNet-50`/`MBConv-6`/`EfficientNet-B0`/`YOLOv8` 本就不命中 `[A-Z]+-[0-9]+`（连字符前为小写），**不入表**。
- `docs/specs/`、`CONTRACTS.md`、`CHANGELOG` 等串**自身不构成 finding**，但同行其它命中照报（不做行级豁免——SPEC-R1-N3）。
- 误伤白名单段（`$ORCA_*` / `orca/skills/` / `render_chart`）当前规则表下**无命中可能**，为未来源码路径规则预留，SKILL/文档注明即可（SPEC-R1-M1）。

### 6.2 check_agent_md_static.py

- **输入**：workflow yaml 路径（扫同级 `agents/`）或直接给 agent md / 文件夹 agent 路径。**无 agents/ 目录 / 零匹配文件 → 清单输出 `0 files`，exit 0**（inline-only workflow 合法态）。
- **规则**（SPEC-R1-A/N2）：
  - **error**（**仅 folder agent `agent.md`**）：布局破坏（脚本平铺 agent 根而非 `scripts/` 子目录）；body 引用脚本非 `$ORCA_AGENT_RESOURCES/scripts/` 绝对 env 形态（相对 `scripts/x.py` 引用）；缺 frontmatter。**file agent（`agents/<name>.md`）无 frontmatter 合法**（引擎兼容期语义，`orca/compile/agents.py:230` 无头 md → 全默认），**不检查 frontmatter**，其余两条照查；
  - **error（固化漏判，两形态）**：
    - (a) bash 围栏（语言标注 ∈ {bash, sh, shell, zsh} 或无标注）内**启行**（容 `[\t ]{0,4}` 缩进）`for ` / `if ` / `while `；
    - (b) 行内出现 `python3? -c` 且其参数串内含 `for ` / `if ` / `while ` / `assert ` 子串（对齐洁净契约 §4「循环·分支·assert」——引号串内单行内联是 v1 最常见死区）；
  - **warning**（`[warn]`）：bash 围栏 >8 行纯顺序命令（提示可抽脚本不强制）。

### 6.3 check_charts.py

- **输入**：workflow 目录路径（扫 `agents/**/scripts/*.py`）。
- **规则**（全 error 级，AST 解析 `render_chart` 调用）：
  - **label / title / chart_type 非字符串字面量**（变量、f-string、`**kw`、**缺关键字/位置传参**——同一违规类）→ **error**（fail loud 强制常量化——registry 查重与静态校验依赖字面量；与 patterns.md 样板一致。SPEC-R1-D/U2 已采纳 error 级）；
  - **调用形态不可静态识别 → error**（独立规则）：`from orca.chart import render_chart as X` 别名 import、`oc.render_chart(...)` 属性调用、`f = render_chart` 重绑定——调用点不可识别则全部后续规则失效（实现期裁决，§9 补 3 测试）；
  - label+title 组合全局唯一（跨全部扫描文件去重；重复即报）；
  - `chart_type=="heatmap"` → `x`/`y`/`value` 三参必在；
  - `chart_type=="pareto"` → `pareto_x_direction`/`pareto_y_direction` 显式传；
  - 调用不在 `Try` 块内 → error（H1 不阻断主流程）。
- **零 call site 是合法态**（无图表 workflow，Stage 3 全量跑此脚本）——正控由守门断言负责（§7），不在脚本内强制。

## 7. benchmark case 17 + 守门扩展

```
benchmark/cases/17-chart-integration/
├── case.md            # 场景：NL 建「跑 N 次基准评测、逐次把 latency/acc 推到 web 图表」的 workflow
├── input.txt          # 上述 NL 描述（含可视化意图词）
└── expected/
    ├── workflow.yaml          # 2 节点：setup(set) + bench(folder agent)，$end 收尾；validate 0 error
    └── agents/
        └── bench/
            ├── agent.md       # frontmatter + 聚焦职责 + bash 块调 $ORCA_AGENT_RESOURCES/scripts/bench_plot.py
            └── scripts/bench_plot.py   # 跑评测(Python 内 mock 就地计算) → 写 jsonl → 末尾 try/except 包 render_chart
                                         #（label "bench/metrics"）→ import 有 try/except 友好错
```

- `bench_plot.py` 必须：`from orca.chart import render_chart` 以 try/except 包裹 import；调用在 try 块内；label/title/chart_type **字符串字面量**；label+title 唯一；heatmap/pareto 不出现（用 line/bar 即可，避免规则耦合）。
- **expected/workflow.yaml 骨架硬要求（SPEC-R1-N10）**：`outputs` 必有（如 `result: "{{ bench.output }}"`，H4）；input `seed` 默认 0；每个 input `description` 以档位标签起头；workflow `description` 符合 H8（一两句功能目的、与既有可区分）。**注意 `setup` 是 set-kind 节点名，非顶层 `setup:` 段**（后者被 pydantic `extra="forbid"` 拒绝）。
- **test_skill_benchmark.py 扩展（SPEC-R1-E/N1/N6）**：
  - ① 既有 parametrize 自动覆盖 case 17（golden validate）；
  - ② **正控守门**：对 case 17 的 `expected/` 跑 `check_charts.py`（subprocess）——断言 exit 0 **且 stdout 清单报告 `≥1 call site`**（防 bench_plot.py 忘写 render_chart 的 vacuous pass）；
  - ③ **全 golden 守门**：对每 case 的 **`expected/` 目录**（不是 case 根——case.md 场景叙事不进扫描）跑 `check_dev_residue.py`（subprocess，断言 exit 0 **且清单 `N ≥ 1`**，yaml 必在）；有 `expected/workflow.yaml` 的 case 加跑 `check_agent_md_static.py`（传 yaml 路径，断言 exit 0；含 `agents/` 的 case 额外断言清单 `N ≥ 1`，inline-only case 允许 `0 files`）。golden 即三脚本自身的验收基准（草稿 §7）。

## 8. install 与清理

- `orca/iface/cli/install_cmds.py`：legacy 清理从裸路径元组改为 `(path, reason)` 形态（SPEC-R1-G）——`design-charts` 的 reason = 「已并入 create-workflow」（沿用「已改名 tars」文案对它输出是谎言）；既有 `orca`/`teams` 两条 reason 不变。
- `tests/iface/cli/test_install_cmds.py`：随包 skill 列表断言去 `design-charts`、含 `create-workflow`；补一条「legacy design-charts 目录被清理」断言（预置假目录 → install → 不存在）。
- `orca/skills/design-charts/` 整目录删除（全仓库零外部引用已核实，2026-08-21）。

## 9. tests/test_skill_v1_checks.py（新增）

三脚本各一组，fixture 内联 tmp_path（SPEC-R1-R6-④ 修订）：
- `check_dev_residue`：① 含 `BLK-3` 的 md → exit 1 + 报告；② 含 `迁移自 XX` → exit 1；③ 含 `ViT-14` → exit 0（豁免正控——`[A-Z]+-[0-9]+` 命中 `T-14` 被 `ViT-\d+` 包含豁免）；④ 含 `P95=12ms` → exit 0（分位白名单）；含 `按 P5 处理` → exit 1（单数字 P 命中）；⑤ 含 `UTF-8` → exit 0；⑥ 干净文件 → exit 0 且清单 `N ≥ 1`；⑦ 路径不存在 → exit 2。`MNIST=0.98` 类 fixture 硬编码**不在脚本规则内**（误报率高，留给 reviewer），断言 exit 0 钉住边界。
- `check_agent_md_static`：① 脚本平铺 agent 根 → exit 1；② body 相对路径引用 `scripts/x.py` → exit 1；③ folder agent 缺 frontmatter → exit 1；**file agent 无 frontmatter → exit 0**（兼容期语义正控）；④ bash 围栏启行 `for` 循环 → exit 1；⑤ **单行 `python -c "for i in ..."`** → exit 1（引号串死区正控）；⑥ 10 行顺序命令 → exit 0 但 stdout 有 `[warn]`；⑦ 合规 folder agent → exit 0；⑧ 无 agents/ 目录 → exit 0 且清单 `0 files`。
- `check_charts`：① 两个文件同 label+title → exit 1；② heatmap 缺 `value` → exit 1；③ pareto 缺方向 → exit 1；④ 调用裸露无 try → exit 1；⑤ **label 为变量/f-string** → exit 1（字面量强制）；⑥ 合规 line chart → exit 0 且清单 `≥1 call site`；⑦ 无 render_chart 的 scripts 目录 → exit 0 且 `0 call sites`。

## 10. 洁净度验收（实现完成后，用户拍板：逐文件派 reviewer，不只 tars validate）

- **逐文件**派 agent-reviewer（每文件独立 sub-agent，或按文件分组但每组 ≤3 文件），对象 = 本次全部新/改产物：SKILL.md、solidify-validate.md、charts/ 三件（重点：迁移路径修正无残留旧路径）、examples/charts/ 三件（惰性资产豁免，只查路径引用）、三脚本（代码，查注释无考古）、case 17 全部（expected yaml/agent.md/bench_plot.py——按「对象层」标准审）、install_cmds.py 改动行、两测试文件。
- 审查法 = 洁净契约 §8 受众翻转通读 + writing-style 三底线；findings 记录 → 修 → 复审至清零。
- **预注（防审查与迁移契约打架——SPEC-R1-M4）**：`charts/chart-api.md` 内的 `orca/chart/*` 源码指针是 writing-style §8 B 类真实文件导航，**允许保留**，reviewer 不据此报 finding。
- `tars validate` 仅是机器底线之一（golden 全绿 + SKILL.md 自身无 `design-charts` 迁移叙事）。
- **元规矩吃狗粮**：本项审查流程即 v2 SKILL.md 规定的 V3 闭环的第一次真实执行，发现 skill 契约自身缺陷 → 回卷 SPEC/SKILL.md（不静默偏移）。

## 11. 流程与回卷规则

SPEC → spec-review（对抗闭环）→ 分批实现（①迁移+路径修正 → ②solidify-validate.md → ③三脚本+单测 → ④SKILL.md 重写 → ⑤install 清理 → ⑥case 17+守门）→ 全量测试 → 逐文件洁净审查 → 状态文档收口。
实现期发现草稿级语义问题：回卷草稿（变更记录附记），不静默偏移。

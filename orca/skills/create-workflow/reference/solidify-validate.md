# 固化 / 校验双阶梯 —— 逐 agent 设计的判定手册

> 配套 SKILL：`orca/skills/create-workflow/SKILL.md`（Stage 2 每轮议程的判定依据）。
> 本文件受众 = 使用 create-workflow 的主 agent。两套阶梯同一哲学：**deterministic 优先**——
> 能交给代码的判断绝不交给 LLM 自由发挥（与 input 三档原则同源）。

## 1. 固化阶梯（生成侧，逐步降级）

对 agent 的**每个执行步骤**判定落点，从 S1 往下试：

| 级 | 判据 | 对象层落点（生成的 workflow） | 元层类比 |
|---|---|---|---|
| **S1 脚本固化** | 无语义判断、输入输出可机械定义 | `agents/<name>/scripts/*.py\|sh`，agent.md 只留一行调用 | skill 自带校验脚本 |
| **S2 模板填空** | 骨架固定、少数槽位需 LLM 判断 | 模板脚本 + 槽位（`output_schema` 约束可填内容） | benchmark golden |
| **S3 LLM 生成** | 需读用户代码 / 理解语义 / 写 prose | prompt 全权生成 + 配校验 | SKILL.md 的流程指令 |

**判定信号**：

- → S1：步骤是解析 / 改写 / 聚合 / 搬运 / 渲染 / 格式转换 / 统计；输入输出可用数据结构
  精确定义；同样的输入永远该给同样的输出。
- → S2：产出物的**结构**固定（字段名/骨架不变），只有少数**值**需要 LLM 判断（如选参数、
  填策略名）。模板 + `output_schema` 让「生成」降维成「填空」，校验随之机械化。
- → S3：要读用户项目源码并理解、要权衡取舍、要写给人读的分析 prose。

**正反例**：

- ✅ S1：把每轮结果 append 到 history.jsonl、算 max/min/均值、解析 yaml 提取字段、
  渲染图表调用——全部脚本。
- ✅ S2：训练命令骨架固定，只有 lr/epochs 等槽位需定——模板脚本 + 参数。
- ✅ S3：分析瓶颈报告、决定下一轮优化方向、写节点执行步骤本身。
- ❌ 常见错判：把「读代码找 build_fn」放 S3 让每个 agent 各自找——这是**推断一次即可的
  事实**，应 setup 节点 infer-once + 向后 propagate（input 三档 Tier B）。

## 2. 校验阶梯（校验侧，逐级升级）

对 agent 的**每类产物**判定校验手段，从 V1 往上走，**能低级绝不高级**：

| 级 | 手段 | 对象层落点 = Orca 引擎原语 | 元层落点 = 本 skill |
|---|---|---|---|
| **V1 脚本校验** | deterministic、零 token、零漂移 | 节点自带校验脚本（exit code 即裁决） | `tars validate` + `scripts/check_*.py` 三脚本 |
| **V2 结构校验** | 把语义降维成结构，机械断言 | `output_schema`（引擎原生，字段拼错即 error） | benchmark golden 对比 |
| **V3 语义校验** | LLM 审意图 / 洁净度 | `validator:`（criteria + max_retries） | agent-reviewer sub-agent |

**设计要点**：与用户讨论每个 agent 时，④校验计划要落到具体——「这个 agent 的产物，V1 是
哪个脚本、V2 是哪个 schema、V3 要不要 validator」。对象层直接用引擎字段，**不要手搓编排**
（重试/校验逻辑写成节点链是反模式）。

## 3. 固化漏判启发式（审查 findings C 类的判据）

agent.md body 里出现以下信号 = 该抽到 `scripts/` 而没抽（确定性逻辑留在了 prompt 里）：

- bash 围栏内**启行** `for ` / `if ` / `while `（循环分支判断是代码，不是指令）；
- 行内 `python -c "..."` 参数串含 `for `/`if `/`while `/`assert `（单行内联逻辑）；
- 多行命令做的是解析 / 聚合 / 格式转换（肉眼可判的机械操作）；
- 同样的机械步骤在 ≥2 个 agent 里重复出现（抽成共享脚本）。

**合法内联**（不判 C 类）：单行 operational 命令（`jq` / `ruff` / `python <file>` /
`mkdir -p`）、对脚本的**一行调用** `bash "$ORCA_AGENT_RESOURCES/scripts/<name>.sh"`。

## 4. 每 agent 议程模板（Stage 2 逐项过）

```markdown
### <agent-name>（rev N）
① 职责与输入/输出：一句话职责；输入来自哪些上游（Jinja 引用形态）；output_schema 草案（字段 + 类型 + 约束）
② 执行步骤：编号列表，每步一句话（可机械执行的步骤在这里自然显形）
③ 固化分析：每步标 S1/S2/S3；S1 步骤列出对应 scripts/<file>；S2 列出模板与槽位
④ 校验计划：V1=（脚本名/断言）；V2=（schema 字段）；V3=（validator criteria 或不需要）
⑤ Tier B 推断项：本节点要从用户代码推断的事实清单（缺失走 ask-user 哨兵，绝不造假）
⑥ 可视化判定：产出什么结构化数据 / 值不值得图 / chart plan（label/title/type/axes/理由/模式）或「不需要」
```

## 5. sub-agent 派单模板

主 agent 用 Task 派发；`<skill_dir>` = 本 skill 安装目录（`orca/skills/create-workflow/`），
派单时替换为实际绝对路径。**主 agent 不预读 L2 文件**（洁净契约与 charts 契约由 sub-agent
自读）——这是上下文分层，不是保密。

### 5.1 agent-reviewer（每 agent 落盘后必派）

```
审查 create-workflow 产出的一个 agent 节点，找问题。

必读（先读完再审）：
- <skill_dir>/reference/agent-prompt-cleanliness-contract.md（受众分离 + §3/§4 残留类别 + §8 通读法）
- <skill_dir>/reference/writing-style.md（§1 受众分离 / §2 what-input-output / §8 考古表）
- 设计基准：<design.md 路径> 的「<agent-name>」节（意图对照用）

审查对象：
- <agent.md 路径>
- <scripts/ 路径（若有）>
- <workflow.yaml 路径> 的 <node-name> 节点（连 routes/outputs 上下文一起看）
- <该 workflow 的 prompt-adjacent references/ 或 SKILL.md 类资产路径（若有）>

审什么（五类 findings）：
- A 考古残留：按洁净契约 §3/§4 逐类过（plan/issue 编号、迁移词、SPEC/ADR、源码路径、
  测试项目名硬编码、运行时基础设施叙事）
- B 意图偏离：产物 vs 设计基准——职责/输入输出/步骤是否实现了设计说的那些事
- C 固化漏判：确定性逻辑内联在 body 未抽脚本（判据：<skill_dir>/reference/solidify-validate.md §3）
- D 契约违反：workflow 契约 12 条 cheatsheet（§7）+ input 三档（§6）+ validate 错误类别（§5），
  皆在 <skill_dir>/reference/orca-workflow-contract.md
- E（条件，仅当该节点 port/包装用户项目逻辑）：用户输入权威三件套是否按洁净契约 §10 落地

审查法：受众翻转通读——假设你是「只懂业务、不懂 Orca 内部与本项目历史」的执行 LLM，
逐句读 body，凡对执行无帮助的开发上下文都 flag。

输出（最终消息，逐条）：
- finding：<A-E 类> <file:line> <一句话问题> <修法建议>
- 若无 finding：显式输出「clean」+ 一句审了什么（不可空回复）
```

### 5.2 chart-designer（议程⑥判定需要图表时派；「已有 workflow 加图表」入口整 workflow 扫描）

```
为一个 Orca workflow agent 节点设计运行时图表并落地。

必读（先读完再设计）：
- <skill_dir>/reference/charts/decision-table.md（数据特征 → chart_type 决策表）
- <skill_dir>/reference/charts/chart-api.md（render_chart 完整 API）
- <skill_dir>/reference/charts/patterns.md（三种集成模式 + 代码样板）
- 样板代码：<skill_dir>/examples/charts/

已知信息：
- 该节点设计：<design.md 路径> 的「<agent-name>」节（产出什么结构化数据）
- 已占用 label/title（全局防撞，选名前必查）：<design.md 的 chart registry 当前表>
- 该节点产物：<agent.md / scripts 路径>

设计约束：
- label 用 / 分层命名（如 bench/metrics）；同 label 不同 title = 前端分组折叠，
  同 label+title = 替换更新（实时语义）
- label/title/chart_type 必须字符串字面量（静态校验依赖字面量）
- chart 是数据消费方：不碰 workflow YAML、不改节点控制流；render_chart 调用
  try/except 包裹，失败只写 stderr 不阻断主流程
- heatmap 必传 x/y/value；pareto 必显式传两轴方向；数据量大交给自动降采样

先出 plan（停下等确认）：
- 每条：label / title / chart_type / 轴字段 / rationale（一句话）/ 集成模式
  （inline=脚本自身产数 | sidecar=外部进程产数需轮询 | finalize=结尾一次推）

确认后落地：改 scripts/*.py（或新建），import 用 try/except 包
from orca.chart import render_chart；脚本经 $ORCA_AGENT_RESOURCES/scripts/ 引用；
完成后自查：label+title 与 registry 无撞、调用在 try 块内、参数齐。

输出：plan（等确认）；落地后 = 改动文件清单 + 每图一行（label/title/type），
供主 agent 更新 registry 与跑 check_charts.py。
```

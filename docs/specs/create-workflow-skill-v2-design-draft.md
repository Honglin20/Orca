# create-workflow skill v2 设计草稿 —— 融合 design-charts + 强交互 + 固化/校验双阶梯

> 2026-08-21。状态：**待用户确认**。确认后写实施计划（SDD）。
> 用户四原则 + 三项拍板见 §1；本文是接口讨论的收敛产物。

## 1. 背景与已拍板决策

现状：`create-workflow`（一次性 headless 生成，明文「全程不阻塞等待确认」）与
`design-charts`（独立图表 skill）两个 skill 并存，后者全仓库零外部引用。

用户四原则：

1. **固化优先**：每个 agent 的工作，能固化成脚本就固化，不能固化才 LLM 生成；
2. **校验体系**：agent 产物能用脚本校验就脚本校验；不行用模板/schema 约束；再不行派
   sub-agent 语义校验——三级成体系；
3. **逐步引导**：强交互分阶段推进（意图 → DAG 草图确认 → 逐 agent 深化确认），
   不是一句话甩手一次跑完；
4. **洁净闭环**：每个 agent 落盘后派 sub-agent 洁净审查，findings 修到清零才算过。

三项拍板（2026-08-21 AskUserQuestion）：

| # | 决策 |
|---|---|
| P1 | **纯交互**：移除 one-shot/headless 降级路径；可视化决策也在逐 agent 环节做，需要就当场派 chart sub-agent |
| P2 | **逐 agent 讨论逐个为默认，用户可说「这 N 个一批 / 剩下的跳过讨论」批量压缩** |
| P3 | **元层 V1 确定性校验落 skill 自带脚本**（`create-workflow/scripts/`），不动引擎 / `tars validate` |

「纯交互」与旧铁律的冲突已核实为低风险：benchmark 守门测试
（`tests/test_skill_benchmark.py`）是**静态 golden 校验**（对 `expected/` 跑
`load_workflow`），不跑 LLM、不涉交互模式。若未来重跑「LLM 公平评测」需给 harness
加 gate 处模拟应答——可选后续，不阻塞本次（见 §9 O3）。

## 2. 非目标

- 不动引擎：`orca/schema`、`orca/compile/validator.py`、`tars validate` 零改动；
- 不做 headless 降级模式（P1）；
- 不重造既有 16 个 benchmark case 的 golden（只新增 chart case，见 §8）；
- 不迁移 design-charts 的 SKILL.md 原文——其流程被 v2 吸收重写，reference 三件
  （decision-table / chart-api / patterns）与 examples 原样迁入。

## 3. 核心模型：双阶梯 × 两层

同一哲学（deterministic 优先，与 input 三档原则同源）在生成/校验两侧的镜像，
作用于两层：**元层**（skill 自己产出 workflow 时）与**对象层**（生成的 workflow
里每个 agent 节点的固化与校验，写进 agent.md 契约 + scripts）。

### 3.1 固化阶梯（生成侧，逐步降级）

| 级 | 判据 | 元层落点 | 对象层落点 |
|---|---|---|---|
| S1 脚本固化 | 无语义判断、输入输出可机械定义（解析/改写/聚合/搬运/渲染） | skill `scripts/` 确定性检查 | `agents/<name>/scripts/*.py\|sh`，agent.md 只留一行调用 |
| S2 模板填空 | 骨架固定、少数槽位需 LLM 判断 | benchmark golden 结构 | 模板脚本 + 槽位（`output_schema` 约束） |
| S3 LLM 生成 | 需读代码/理解语义/写 prose | agent.md / yaml prompt 正文 | prompt 全权生成 + 配校验 |

### 3.2 校验阶梯（校验侧，逐级升级）

| 级 | 手段 | 元层实例 | 对象层实例（引擎原语） |
|---|---|---|---|
| V1 脚本校验 | deterministic、零 token、零漂移 | `tars validate` + skill 自带脚本（§7） | 节点自带校验脚本（exit code 即裁决） |
| V2 结构校验 | 把语义降维成结构，机械断言 | benchmark golden diff | `output_schema`（字段拼错即 error） |
| V3 语义校验 | LLM 审意图/洁净度 | agent-reviewer sub-agent（§6.1） | `validator:` criteria + max_retries |

对象层 V1/V2/V3 恰好是 Orca 已有 folder-agent scripts / `output_schema` / `validator`
三原语。现 SKILL.md 的 H3「优先用原生字段别手搓」从零散规则升级为系统性双阶梯：
设计每个 agent 时显式走一遍判定（Stage 2 议程 ③④），而非写代码时隐式碰运气。

## 4. 融合后的目录结构（知识分层）

```
orca/skills/create-workflow/
  SKILL.md                        # L0 主控：阶段流程 + gate + 判定入口（瘦身，§7）
  reference/
    orca-workflow-contract.md     # L1 主 agent 设计节点时读（input 三档细节收敛于此）
    writing-style.md              # L1 写产物前读
    solidify-validate.md          # L1 新增：双阶梯判定表 + 每 agent 议程模板（Stage 2 每轮）
    agent-prompt-cleanliness-contract.md  # L2 → agent-reviewer sub-agent 读，主 agent 不加载
    charts/                       # L2 → chart-designer sub-agent 读，主 agent 不加载
      decision-table.md           #   （自 design-charts/reference/ 迁入，内容不动）
      chart-api.md
      patterns.md
  examples/
    *.yaml                        # 现有 4 个
    charts/*.py                   # 自 design-charts/examples/ 迁入 3 个
  scripts/                        # 新增：元层 V1 脚本（§7）
  benchmark/                      # 保留；新增 case 17（chart 集成，§8）
```

- **L0** 始终在上下文：流程骨架 + gate 定义 + 判定入口 + 指针。
- **L1** 主 agent 按阶段 `Read`：设计 agent 时读 contract，写产物前读 writing-style，
  Stage 2 每轮读 solidify-validate。
- **L2** 主 agent **永不加载**：派 sub-agent 时在派单 prompt 里给路径，sub-agent 自己读
  （洁净契约 §7「多 agent 共建」已有此派发先例）。这是层次化管理的落点——主控上下文
  不被 chart API / 审查细则污染。
- `orca/skills/design-charts/` 删除；`tars install` legacy 清理列表加 `design-charts`
  （`orca/iface/cli/install_cmds.py:243` 已有清理 `orca`/`teams` 旧名先例），防止存量
  安装残留双入口。
- 「给已有 workflow 加图表」独立入口保留：SKILL.md description 覆盖该触发词，命中时
  跳过 Stage 0-2 直入该 workflow 的图表流程（inventory 子代理扫描 → plan → gate →
  落地 → registry 查重）。

## 5. 交互流程（纯交互，每 gate 停下等用户）

```
Stage 0  意图接收     理解意图/扫素材；模糊问 1-2 个业务问题
Stage 1  DAG 草案 ── gate 1：草 DAG(ascii) + 每 agent 一句话职责 + input 三档草案
                      + 可视化意图初判 → 迭代 → 用户确认
Stage 2  逐 agent    默认逐个（用户可批量/跳过）；每轮：
                      议程①-⑥ → gate 2（该 agent 设计确认）→ 落盘 → V1 + V3 审查闭环
Stage 3  组装        routes/outputs/parallel 拼全 DAG + 全量 tars validate
                      + 三档 checklist + 全局考古 grep + chart registry 查重
Stage 4  终验交付    final validate + 汇报（DAG 图 + 每 agent 校验状态表 + chart 清单）
```

### 5.1 Stage 2 每 agent 议程（固定六项，逐项与用户对齐）

1. **职责与输入/输出契约**：`output_schema` 草案（对象层 V2 雏形）；
2. **执行步骤分解**：编号步骤，可机械执行的部分自然显形；
3. **固化分析**：每步过 S1/S2/S3 判定（判据见 `reference/solidify-validate.md`）；
4. **校验计划**：产物过 V1/V2/V3——对象层直接映射引擎原语（scripts / output_schema /
   validator）；
5. **Tier B 推断项清单**：读代码可得的事实（infer-once + 哨兵段）；
6. **可视化判定**：产出什么结构化数据、值得什么图——需要则**当场派 chart-designer**
   sub-agent 出该 agent 的 chart plan，plan 并入 gate 2 一起确认（P1）。

### 5.2 审查闭环（每 agent 落盘后立即，不攒到最后）

```
落盘（agent.md + scripts + yaml 节点 + chart 落地若已确认）
  → V1：agent 级静态脚本（§7，tars validate 留 Stage 3 全量）
  → V3：派 agent-reviewer sub-agent（§6.1）
  → findings 回修 → 复审（带上一轮 findings + 修复 diff 核对）
  → 全部 fixed 或用户显式 waive（记理由于 design.md）才过
  → 上限 3 轮，超限 fail loud 抛给用户裁决
```

findings 五类：A 考古残留（洁净契约 §3/§4）/ B 意图偏离（对照 design.md 该 agent 段）/
C 固化漏判（确定性逻辑未抽脚本）/ D 契约违反（contract 12 条 + 三档）/ E 用户输入
权威三件套缺落（涉及 port 用户逻辑时，洁净契约 §10）。

### 5.3 断点续传锚点：design.md

`<workflow_dir>/<name>-design.md` 随阶段渐进写入：Stage 1 DAG + 每 agent 设计段 +
审查 findings 状态（fixed/waived）+ chart registry（§6.2）。作用：

1. **跨会话 resume**——多 agent workflow 大概率一轮聊不完，重入时 skill 先找它恢复进度
   （报告当前 Stage + 下一步）；
2. **reviewer 的意图对照基准**——审「产物是否实现了设计」需要基准，光审洁净度不够；
3. **chart registry 载体**——逐 agent 决策图表后，全局 label/title 一致性靠它维护。

它是开发期文档：绝不进 agent.md / yaml（writing-style 红线）；住在用户 workflow 目录、
不在 skill 目录，天然不被 `tars install` 带走。

## 6. sub-agent 派单契约

skill 无法注册 agent 类型 → 统一 Task 派发 + 派单 prompt 内嵌 reference 绝对路径。

### 6.1 agent-reviewer（每 agent 落盘后必派）

- **输入**：该 agent 全部产物路径 + design.md 该 agent 设计段 + 洁净契约与
  writing-style 路径（sub-agent 自读，主 agent 不加载）。
- **审查法**：洁净契约 §8 受众翻转通读 + 意图对照（design.md）+ 固化漏判启发式 +
  （条件）§10 三件套落地检查。
- **输出**：findings 列表（五类分级 + 位置 + 修法建议），供闭环回修。

### 6.2 chart-designer（Stage 2 议程⑥判定需要时派；独立入口时整 workflow 扫描）

- **输入**：该 agent 设计段（产出什么结构化数据）+ **design.md 里的既有 chart
  registry**（label/title 已占用清单）+ `reference/charts/` 三件与 examples 路径。
- **输出**：该 agent 的 chart plan（label/title/type/axes/rationale/集成模式
  inline|sidecar|finalize）→ 并入 gate 2 确认 → 确认后由其落地 scripts 改动
  （它读过 patterns/examples，落地比主 agent 准），主 agent 更新 registry 并跑
  `check_charts.py`（§7）。
- **全局一致性**：逐 agent 决策天然碎片化，靠 registry（派单时带已占用 label/title 防
  撞）+ Stage 3 `check_charts.py` 全局查重双保险。

### 6.3 fidelity 维度（不单列第三 sub-agent）

洁净契约 §10 的 fidelity-verifier 是**对象层**（生成的 workflow 运行时的 subagent）；
元层只查「三件套是否按 §10 落地」，作为 agent-reviewer 的条件维度（findings E 类）。

## 7. 元层 V1 脚本（skill 自带，P3）

`orca/skills/create-workflow/scripts/`，全部 fail loud（findings 非空 → exit 1 + 列表）：

| 脚本 | 行为 | 固化自 |
|---|---|---|
| `check_dev_residue.py` | 宽口径考古 grep（`[A-Z]+-[0-9]+` 宽表 + 迁移词 + SPEC/ADR + fixture 名等），扫 yaml + agents md + scripts | SKILL.md 现手写 grep 表（与 `tars validate` 窄表互补：validate 只抓 §3） |
| `check_agent_md_static.py` | agent.md body 多行 bash/`python -c` 内联检测（固化漏判静态启发）+ folder-agent 契约（scripts/ 布局、`$ORCA_AGENT_RESOURCES` 引用、frontmatter） | 洁净契约 §4 末条 + contract §4 三硬约定 |
| `check_charts.py` | 扫 `agents/**/scripts/*.py` 的 `render_chart` 调用：label+title 全局唯一、heatmap x/y/value 齐、pareto 两轴方向显式、try/except 包裹不阻断 | design-charts SKILL.md H1-H5 人肉规则 |

边界：只对**本次生成产物**跑，不承诺既有 workflow 零 finding（既有清理另有节奏）。
验收基准是对 benchmark golden 全绿（§8）。

## 8. benchmark 影响

- **守门测试不变**：`tests/test_skill_benchmark.py` 是静态 golden 校验，与交互模式无关；
- **新增 case 17（chart 集成）**：NL 描述含可视化意图 → expected：folder agent
  `scripts/` 带 `render_chart`（try/except 包裹）+ agent.md bash 块调用；守门断言扩展：
  对该 case 跑 `check_charts.py` 全绿；
- 既有 16 case golden 不动。

## 9. 开放问题

- **O1 design.md 位置**：定 `<workflow_dir>/<name>-design.md`（就近 + resume 可发现）；
  若用户项目对 workflow 目录有洁癖，gate 1 时可改指它处——skill 不强制。
- **O2 registry 格式**：design.md 内 markdown 表（label/title/type/来源节点/状态），
  不引入独立 json——够用且人可读。
- **O3 LLM 公平评测重跑**（可选后续，不阻塞）：harness 需在 gate 处喂 canned
  confirmations；本次不做。

## 10. 验收标准

1. `tars install` 后：`design-charts/` 消失（legacy 清理生效），`create-workflow/`
   含 `reference/charts/` 三件 + `examples/charts/` + `scripts/` 三脚本；
2. SKILL.md：无「全程不阻塞」铁律；gate 流程（Stage 0-4 + 六项议程 + 闭环语义）在场；
   `allowed-tools` 含 `Task` 与 `AskUserQuestion`；**单源指针化**——input 三档细节/哨兵 JSON 示例/
   手写考古 grep 表/主 agent 直读洁净契约的指令，不再出现于 SKILL.md（下沉 contract §6 /
   脚本 / sub-agent 派单）（2026-08-21 spec-review N7/U1 改「行数显著下降」为此判定——
   行数预算脆弱，去重单源才是意图）；
3. 三脚本对 benchmark golden（含新 case 17）全绿；`tests/test_skill_benchmark.py` 全绿；
4. 洁净契约与 SKILL.md 交叉引用一致（CLAUDE.md 指向的
   `reference/agent-prompt-cleanliness-contract.md` 路径不变）；契约 §8/§9 执行流程
   段同步改 sub-agent 审查口径；
5. contract §6 与 SKILL.md 的三档重复收敛（细节单源在 contract，SKILL.md 留判定表）。

## 附 A：spec-review 闭环记录（2026-08-21，1 轮）

结论 conditional-pass：3 blocker（A frontmatter 误伤 file agent / F allowed-tools 缺 AskUserQuestion / N1 守门零扫描 vacuous pass）+ 10 major，全部 R1-R9 修订已落 SPEC；3 处修法驳回（E「零调用→error」、N1「零匹配→exit 1」——击穿无图表/inline-only workflow 的合法态；N2「单行 for 永不命中」事实错误），驳回依据 = 脚本通用契约优先于单一守门场景便利。U1（验收项改单源指针化）/ U2（chart 字面量强制 error 级）按推荐采纳。证据锚：golden 08 file agent 无 frontmatter 属引擎兼容语义（`orca/compile/agents.py:230`）；`P95` 分位白名单（case 17 latency 域）。

---
name: design-charts
description: >-
  为已有或新建的 Orca workflow 设计和添加运行时可视化图表（orca.chart）。
  扫 workflow 脚本中的结构化数据产出 → 推荐匹配的 chart_type → 修改/新建 Python 脚本来落
  地 render_chart 调用。不碰 workflow YAML 结构，只动 scripts/ 下的 python 文件。
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# design-charts

<purpose>
读一个 workflow 的 YAML + agent 目录 + scripts/*.py，识别每个节点产出的**结构化数据**
（jsonl / json / 结构化 stdout），据其特征匹配最合适的 chart_type，输出推荐报告，然后修改
或新建 Python 脚本落地 `orca.chart.render_chart` 调用。
</purpose>

## 核心理念

你**不改 workflow 结构**（不碰 YAML、不改 agent.md 的 prompt），只在 `scripts/` 下加
或改 Python 文件。chart 是数据消费方，不是控制流。

## 工作流程（4 步）

### Step 1: Inventory——扫脚本，列数据产出

对每个 agent 节点，读它的 `agent.md`（找 `$ORCA_AGENT_RESOURCES/scripts/*.py` 引用）
和 scripts 目录下所有 `.py` 文件，提取：

- **结构化写盘**：`json.dump` / `with open(... 'w')` / `write_text` / subprocess 输出重定向到文件
- **结构化 stdout**：`print(json.dumps(...))` / `print(f"KEY=VALUE")`
- **输出目录约定**：`output_dir` / `$ORCA_ARTIFACTS_DIR` 下的子路径

产出**数据清单**，每条含：
```
node: <节点名>
source: <脚本路径或 agent 命令>
format: jsonl | json | stdout
path_pattern: <文件路径模式>
fields: [已知字段名列表]
```
`format=jsonl` 且文件由外部进程持续写入 → sidecar 模式（B）。`format=json` 且文件在脚本结尾一次性写 → inline（A）或 finalize（C）。

**搜索技巧**：Grep 脚本中 `".json"` / `".jsonl"` / `json.dump` / `open(` / `write_text` /
`Path(` 并写模式来定位数据产出点。

若 workflow 尚无 scripts 目录（纯 inline agent）→ 跳过，回复：
"此 workflow 无结构化数据产出（无 scripts 目录或脚本只输出非结构化文本）。
要添加图表，先让某个节点产出结构化数据（JSON/JSONL），再重新运行本 skill。"

### Step 2: Match——数据特征 → chart_type

对清单中每条数据产出，根据字段特征匹配最佳 chart_type。决策逻辑见
`reference/decision-table.md`（本 skill 同目录，直接 Read）。

核心匹配信号：

| 信号 | 判据 |
|---|---|
| 时序标量 | 含 step / epoch / time + 数值列 → `line` / `area` |
| 分组对比 | 含 category / name / phase（离散 ≤20 类）+ 数值列 → `bar` |
| 二维矩阵 | 两个离散轴（如 recipe × bitwidth）+ 数值 cell → `heatmap` |
| 散点关系 | 两个连续数值列（如 latency × accuracy）→ `scatter` / `pareto` |
| 多维度轮廓 | ≥3 个归一化指标列（同 scale）→ `radar` |
| 候选列表 | 无明显数值轴，行是 item → `table` |

**不推图的情形**：
- 数据产出是中间产物、下游消费后不对外汇报
- 字段完全不可读（二进制、pickle）
- 用户明确说"不需要可视化"

### Step 3: Recommend——出推荐报告

给用户一份**按 label 分组**的推荐报告。**不要问 y/n 确认**，直接输出推荐，用户可以让你改。

格式：
```
## Chart Plan: <workflow-name>

### <label 分组名> / <title>
- source: <哪个脚本产的数据>
- chart_type: <line|bar|...>
- rationale: <为什么推这张图，一句话>
- x=<字段> y=<字段> [hue=<字段>]

（每个数据产出 1-2 张图，同一 label 下的图表按 title 去重——dedup 语义）
```

label 命名约定：
- 同一概念维度共一个 label（如 `nas/training`、`nas/search`、`nas/selection`）
- 用 `/` 分隔层级
- 同 label + 同 title = 前端替换（实时更新），不同 title = 各自独立

**输出推荐报告后停止，等待用户确认。用户说"改"/"go ahead"/"可以"后再进入 Step 4。**
不要自动修改脚本。

### Step 4: Modify——改脚本落地

两种策略，按数据产出频率判定：

**A. inline**（数据是脚本自身产的，频率 once 或脚本结尾推一次）：
在脚本末尾、数据写盘之后，直接加 `render_chart(...)` 调用。不改主逻辑。

**B. sidecar**（数据是外部进程产的，如 agent 跑 `bash train.sh` 写 jsonl）：
新建/修改一个独立的轮询脚本（如 `push_charts.py`），周期读 jsonl 增量 → 调 `render_chart`。
在 agent.md 的 bash 执行块里加周期调用的 shell 命令（具体写法见 `reference/patterns.md` 模式 B 的 agent.md 调用示例）。

详细代码模板见 `reference/patterns.md` + `examples/` 目录。

**修改原则**：
- 脚本路径：文件夹 agent 的脚本放 `scripts/` 子目录，用 `$ORCA_AGENT_RESOURCES/scripts/x.py` 引用
- import 样板：`from orca.chart import render_chart`（try/except 包一层，不在 Orca 内时报友好错）
- 不破坏主流程：chart 调用放 try/except，失败只 stderr 不阻断
- label+title 全局唯一：与推荐报告中一致

## 硬规则

**H1 chart 是 sidecar，不阻断主流程**：`render_chart` 调用外层 `try/except`，失败写 stderr 后继续。
**绝不**在核心计算中间插 chart 调用。

**H2 label+title 即 dedup 键**：同一 workflow 内同 `(label, title)` 的后续调用会替换旧图
（实时更新语义）。确保有意使用这特性（如训练 loss 每次推新图覆盖旧的），或确保不同图 title 唯一。

**H3 数据量大 → 依赖自动降采样**：`render_chart` 默认 max_points=2000，超过自动降采样。
不要在手写降采样逻辑——传全量 data 即可。

**H4 heatmap 三字段必填**：`x`（列轴）、`y`（行轴）、`value`（着色）缺一 → `render_chart` fail loud。

**H5 pareto 两轴方向显式传**：`pareto_x_direction` / `pareto_y_direction` 各为 `"max"` 或 `"min"`。
不依赖默认值。

**H6 同 workspace 引用就近**：API 契约 + 决策表 + 模式代码在本 skill 同目录
（`reference/` + `examples/`），**直接 Read 它们**——不要 spawn 子 agent 去翻代码库。

**H7 脚本不在 Orca run 内就无法跑**：确认修改的脚本会被 `$ORCA_AGENT_RESOURCES` 路径引用
且在 agent.md 的 bash 块里通过 `python3 "$ORCA_AGENT_RESOURCES/scripts/x.py"` 调用。
直接 `python x.py` 跑会因缺 `ORCA_*` env 而 fail loud。

**H8 不改 workflow YAML**：本 skill 只动 `scripts/*.py` +（若需）agent.md 的 bash 调用块。
不碰节点定义、routes、inputs、outputs。

## 契约在哪

- `reference/chart-api.md` —— `render_chart` 完整 API（签名、类型、校验规则、限制）
- `reference/decision-table.md` —— 数据特征 → chart_type 决策表 + 判据
- `reference/patterns.md` —— 3 种集成模式详解 + 代码样板
- `examples/` —— 3 个可运行的精简模板脚本

<success_criteria>
- [ ] 扫了所有 agent 节点的 scripts 目录和 agent.md 的 bash 调用块
- [ ] 每条结构化数据产出都被识别并匹配了一个 chart_type（或不推图的明确理由）
- [ ] 推荐报告按 label 分组，每张图标注 source / chart_type / rationale / 轴字段
- [ ] 修改后的脚本：import 有 try/except 包、chart 调外层有 try/except 不阻断主流程
- [ ] label+title 对 workflow 全局唯一（有意共享 label 的除外）
- [ ] heatmap 三字段齐、pareto 两轴方向显式传、x_label / y_label / caption 有人话
- [ ] 未动 workflow YAML
</success_criteria>

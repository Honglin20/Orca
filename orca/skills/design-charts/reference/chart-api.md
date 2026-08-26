# orca.chart.render_chart API 完整契约

> **权威实现**（签名 / 校验 / 限制 → 读源文件，不照抄）：
> `orca/chart/_render.py`（函数签名 + 行为）、`orca/chart/_validate.py`（校验规则）、`orca/chart/_limits.py`（常量）。
> 前端渲染对照：`orca/iface/web/frontend/src/types/topology.ts` ChartPayload。

## 函数签名

> 完整签名见 `orca/chart/_render.py:36-55` docstring。下面列全部参数及其含义——数值默认以源文件为准。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `chart_type` | str | 必填 | `line` / `bar` / `area` / `scatter` / `pareto` / `radar` / `table` / `heatmap` |
| `data` | `list[dict]` | 必填 | 扁平 record array，每行一个 dict，字段名与 x/y/hue 等对应 |
| `label` | str | 必填 | dedup 维度 1（分组键） |
| `title` | str | 必填 | dedup 维度 2（同 label 下唯一） |
| `x` | str | `""` | 横轴字段名（数据行内的 key） |
| `y` | str | `""` | 纵轴字段名 |
| `hue` | str | `""` | 分组字段 → 多 series pivot；hue 非空时 color 被忽略 |
| `color` | str | `""` | 逐行 CSS 色值字段（bar/scatter 无 hue 时生效） |
| `columns` | `list[str] \| None` | None | table 列序；其他 chart_type 忽略 |
| `pareto_direction` | str | `""` | 全局方向 `"max"`/`"min"`。**废弃**，用下方 per-axis 版本替代 |
| `pareto_x_direction` | str | `""` | pareto x 轴方向，`"max"`/`"min"`。**必须显式传** |
| `pareto_y_direction` | str | `""` | pareto y 轴方向，`"max"`/`"min"`。**必须显式传** |
| `value` | str | `""` | heatmap cell 着色字段名；chart_type="heatmap" 时**必填** |
| `x_label` | str | `""` | 横轴标签文案（人话）；空=回退用字段名 |
| `y_label` | str | `""` | 纵轴标签文案（人话）；空=回退用字段名 |
| `caption` | str | `""` | 图下小字说明 |
| `max_points` | int | 2000 | 自动降采样阈值 |

返回值：`int` — Orca 分配的 event seq（ack 携带，对账用）。

## 运行条件

**仅在 Orca 编排的子进程内可用**。需 4 个 env 变量（`render_chart` 自动读）：
- `ORCA_RUN_ID`
- `ORCA_NODE`
- `ORCA_SESSION_ID`
- `ORCA_CHART_SOCK`

缺任一 → `RuntimeError`。直接 `python x.py` 跑会 fail loud。

agent.md 里的 bash 块通过 `$ORCA_AGENT_RESOURCES` 调脚本时，Orca spawn 会注入这些 env 变量。

## 8 种 chart_type

| chart_type | 前端渲染 | 必填字段 | 特殊说明 |
|---|---|---|---|
| `line` | recharts LineChart | 无额外 | hue → 多线并排（pivot 后 series） |
| `bar` | recharts BarChart | 无额外 | hue → 多柱并排；color → 逐行着色（无 legend） |
| `area` | recharts AreaChart | 无额外 | 同 line，填充面积 |
| `scatter` | recharts ScatterChart | 无额外 | 可选 `size` 字段做气泡图 |
| `pareto` | recharts ComposedChart | `pareto_x_direction` / `pareto_y_direction`（推荐） | 前端算非支配前沿 + 连线 |
| `radar` | recharts RadarChart | 无额外 | hue → 多系列雷达；容器固定 `aspect-square` |
| `table` | 纯 HTML `<table>` | `columns`（推荐，控制列序） | 无排序/分页；x/y 不消费 |
| `heatmap` | CSS Grid | **`x`（列轴）, `y`（行轴）, `value`（着色）** — 缺一 fail loud | 长格式，一行一条 cell |

## 字段规范

### 通用字段
- **`label`** + **`title`**：`render_chart` 的 dedup 维度。同 label 下同 title 的后续调用 →
  前端替换旧图（实时更新语义）。命名用 `/` 分隔层级，如 `nas/training`。
- **`x`** / **`y`**：坐标轴字段名。空串 → 前端回退到 `"x"` / `"y"`（但不要依赖回退，显式传）。
  两个都是数据 rows 里的字段名。
- **`x_label`** / **`y_label`**：轴标签文案。空串 → 前端回退用字段名。写**人话**，如
  `"训练步数"` 而非 `"global_step"`。
- **`caption`**：图下小字说明。一句话解释数据来源/单位/读图方法。空串 = 无 caption。
- **`hue`**：分组字段名。line/bar/area/scatter/radar 支持。前端将长格式数据 pivot 为多 series。
  **hue 优先于 color**：hue 非空时 color 被忽略。
- **`color`**：逐行着色字段名。仅 bar/scatter（无 hue 时）支持。每行该字段值为合法 CSS 色串
  （如 `"#D4605A"`），前端逐行渲染。无 legend。
- **`columns`**：table 列序（list[str]）。不传 → 按 `Object.keys(data[0])` 顺序。
  其他 chart_type 忽略此参数。

### 条件字段
- **`pareto_direction`**：全局前沿方向（`"max"` = 越大越好 / `"min"` = 越小越好）。
  被 `pareto_x_direction` / `pareto_y_direction` 覆盖。
- **`pareto_x_direction`** / **`pareto_y_direction`**：各轴方向。值 ∈ `{"max","min"}`。空串或省略 = OK。
  建议**始终显式传**，不依赖默认值。
- **`value`**：heatmap cell 着色字段。`chart_type="heatmap"` 时**必填且非空**，否则 fail loud。
  其他类型忽略此参数。
- **`size`**：scatter 气泡大小字段（可选）。存在时 scatter 自动切换气泡模式（ZAxis range=[50,400]）。

### 数据格式
- **`data`**：`list[dict[str, Any]]`，扁平 record array。每行是一个 dict，字段名与 x/y/hue 等对应。
  前端按 chart_type 解释行内字段——不强约束 dict-shape，但字段名必须与参数引用的字段名一致。
  空 list 不报错，前端显示空图。
- **长格式**（有 hue 时）：每行含 `{x: A, hue: foo, y: 100}`，前端 pivot 后渲染。
- **宽格式**（无 hue 时）：每行含 `{x: A, y: 100}` 即可。

## 限制与行为

> 具体数值见 `orca/chart/_limits.py`（两端同源常量）。此处描述行为——为保持与源一致，不重复数值。

| 约束 | 行为 | 命中后 |
|---|---|---|
| 数据点过多 | 自动降采样到 `max_points` 阈值（默认见源文件） | stderr 告警，不阻断 |
| 整条消息过大 | 硬上限（默认见源文件，先降采样后检查） | `ValueError` |
| ack 超时 | Orca ingestor 未在时限内回复 | `RuntimeError` |
| socket 路径过长 | 超出 OS 限制（macOS 104 / Linux 108 字节） | `RuntimeError`（改 `ORCA_RUNS_DIR` 到短路径） |

降采样策略：
- line/area/bar：均匀采样
- scatter：随机采样
- pareto/heatmap/table/radar：均匀采样

## 校验规则

> 完整规则见 `orca/chart/_validate.py` docstring。`validate_payload` 在 client 端校验（写 tape 前），
> 错误信息直接抛回 script → agent 可见可修。

最易踩的 5 条：
1. `chart_type` ∉ 8 种 → `ValueError`
2. `label` / `title` 为空 → `ValueError`
3. heatmap 缺 `x` / `y` / `value` → `ValueError`
4. `pareto_x_direction` / `pareto_y_direction` 非 `"max"`/`"min"`/`""` → `ValueError`
5. `data` 非 list → `ValueError`

## 代码样板

**最小可用调用**：
```python
from orca.chart import render_chart

render_chart(
    chart_type="line",
    data=[{"step": 0, "loss": 1.0}, {"step": 100, "loss": 0.5}],
    label="training",
    title="Training Loss",
    x="step",
    y="loss",
    x_label="训练步",
    y_label="loss",
)
```

**带 hue 的多系列**：
```python
render_chart(
    chart_type="line",
    data=[
        {"step": 0, "phase": "train", "value": 1.0},
        {"step": 0, "phase": "val", "value": 1.2},
    ],
    label="training",
    title="Loss by Phase",
    x="step",
    y="value",
    hue="phase",
    x_label="步数",
    y_label="loss",
)
```

**heatmap**：
```python
render_chart(
    chart_type="heatmap",
    data=[
        {"recipe": "RTN", "bitwidth": "4-bit", "accuracy": 0.68},
        {"recipe": "RTN", "bitwidth": "8-bit", "accuracy": 0.80},
        {"recipe": "SmoothQuant", "bitwidth": "4-bit", "accuracy": 0.72},
        {"recipe": "SmoothQuant", "bitwidth": "8-bit", "accuracy": 0.85},
    ],
    label="quant/sweep",
    title=" Recipe x Bitwidth  Matrix",
    x="bitwidth",
    y="recipe",
    value="accuracy",
    x_label="量化位宽",
    y_label="方案",
)
```

**pareto**：
```python
render_chart(
    chart_type="pareto",
    data=[
        {"latency": 10, "accuracy": 0.72},
        {"latency": 20, "accuracy": 0.85},
    ],
    label="search",
    title="Pareto Front",
    x="latency",
    y="accuracy",
    pareto_x_direction="min",
    pareto_y_direction="max",
    x_label="延迟 (ms, 越低越好)",
    y_label="精度 (越高越好)",
)
```

**table**：
```python
render_chart(
    chart_type="table",
    data=[
        {"name": "ResNet", "score": 0.92, "rank": 1},
        {"name": "ViT",   "score": 0.88, "rank": 2},
    ],
    label="results",
    title="Leaderboard",
    columns=["rank", "name", "score"],
)
```

**bar with per-row color**：
```python
render_chart(
    chart_type="bar",
    data=[
        {"layer": "conv1", "sensitivity": 0.9, "fill": "#D4605A"},
        {"layer": "conv2", "sensitivity": 0.3, "fill": "#6BA5A0"},
    ],
    label="analysis",
    title="Layer Sensitivity",
    x="layer",
    y="sensitivity",
    color="fill",
    x_label="层名",
    y_label="敏感度",
)
```

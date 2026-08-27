# 数据特征 → chart_type 决策表

> 输入：节点产出的结构化数据清单（每条含 format、fields、frequency——来自该节点的设计信息或对其 scripts 的扫描）。
> 输出：每条匹配 1 个合适的 chart_type + 推荐轴字段。

## 判据优先级

按以下顺序问 5 个问题，第一个命中即导出 chart_type：

```
1. 是否多行列表、无明显数值轴？                         → table
2. 是否有明显的时序/迭代轴？（step / epoch / gen / time）→ line / area
3. 是否两个离散分类维度 × 一个数值？（如 recipe × bitwidth）→ heatmap
4. 是否两个连续数值列、存在 trade-off 关系？             → pareto 或 scatter
5. 是否分组对比/排名？（category 列 ≤20 个值）           → bar
6. 是否多维度轮廓（≥3 个归一化指标）？                   → radar
7. 否则                                                 → table（fallback）
```

## 决策详表

### 时序标量 → line

**信号**：数据行里含步数/轮次/时间戳字段 + 至少一个连续数值字段。

| 字段模式 | axial 映射 | 示例场景 |
|---|---|---|
| `step` / `epoch` / `global_step` / `generation` + 标量列 | x=step, y=<标量> | 训练 loss、验证 metric、搜索收敛 |
| `timestamp` / `time` + 标量列 | x=time, y=<标量> | 资源监控曲线 |
| 多 phase（train/val）共存 | x=step, y=value, **hue=phase** | 同图对比训练/验证 |
| best+mean 共存 | x=generation, y=value, **hue=stat** | 进化算法收敛 |

**不要用 bar**：时序数据天然适合连续线，bar 在步数多时挤成一片。

**选 area 而非 line**：当需要强调累积/占比（如堆叠面积图），否则默认 line。

---

### 分组对比 → bar

**信号**：数据行里有一个离散分类列（≤20 个唯一值），每个类别对应一个数值。

| 字段模式 | axial 映射 | 示例场景 |
|---|---|---|
| `category` / `name` / `stage` + `value` | x=category, y=value | 不同阶段计数（漏斗） |
| `layer` / `module` + 指标 | x=layer, y=metric | 逐层敏感度 |
| `scheme` / `method` + 指标（≤20 个） | x=scheme, y=metric | 方案对比（before vs after） |
| 多类别分组 | x=category, y=value, **hue=group** | 分组柱状图 |
| 需高亮特定行 | x=category, y=value, **color=fill_col** | 标红超标项 |

**bar vs line 的选择**：离散分类 → bar；连续序列 → line。如果分类有自然顺序（如 stage 1→2→3），
用 bar 并保持数据行顺序 = 显示顺序。

---

### 二维矩阵 → heatmap

**信号**：两个离散维度（各 ≤30 个唯一值）+ 一个数值列。数据天然是二维 pivot 形态。

| 字段模式 | axial 映射 | 示例场景 |
|---|---|---|
| `recipe` × `bitwidth` × `accuracy` | x=bitwidth, y=recipe, value=accuracy | PTQ 方案×位宽扫描 |
| `method` × `dataset` × `score` | x=dataset, y=method, value=score | 多方法多数据集 |

**三字段必填**：x（列轴）、y（行轴）、value（cell 着色）缺一 → `render_chart` fail loud。

**何时不用 heatmap**：某一维度的唯一值 >30 → 矩阵太大不可读，改用 scatter（两轴都是数值）或
table（不需要颜色映射）。

**数据格式**：长格式，一行一个 cell。例如 3 recipes × 4 bitwidths = 12 行数据。

---

### 散点关系 → scatter 或 pareto

**信号**：两个连续数值列（如 latency 和 accuracy），每个点代表一个独立实体。

| 条件 | chart_type | 说明 |
|---|---|---|
| 存在明显的 trade-off / 帕累托前沿 | **pareto** | 前端自动算非支配前沿 + 连线 |
| 纯散点分布 | **scatter** | `x` + `y` 两轴 |
| 需要第三个维度（如参数量） | scatter + **`size`** | ZAxis 气泡范围 50-400，见 `chart-api.md` |
| 需要按类别着色 | scatter + **hue** | 多系列散点 |

**pareto 方向约定**：
- 成本指标（latency, FLOPs, model size）→ `"min"`（越小越好）
- 质量指标（accuracy, throughput）→ `"max"`（越大越好）
- 如果质量目标被负向化（全部 ≤0），显示时取 `-v` → 方向仍是 `"max"`

**pareto 的前端行为**：前端用 O(n^2) 算非支配前沿，前沿点高亮+连线，被支配点灰色半透明。
所以你**不需要**手动标记前沿点——传全量数据即可。

---

### 多维度轮廓 → radar

**信号**：≥3 个归一化指标列（同一 scale，例如都是百分比 0-100），用于多维度横向对比。

| 字段模式 | axial 映射 | 示例场景 |
|---|---|---|
| dimension × value | x=dimension, y=value | 单模型雷达图 |
| dimension × model × value | x=dimension, y=value, hue=model | 多模型雷达对比 |

**限制**：前端容器 `aspect-square` + `max-w-[400px]`，维度 >8 个时标签会挤。维度太多 → 用 bar 替代。

---

### 候选列表 → table

**信号**：无明显坐标轴，每行是独立的条目，用户需要看所有字段的原值。

| 字段模式 | 映射 | 示例场景 |
|---|---|---|
| 任意字段列表 | columns=<有序列名> | 候选账本、排行榜、搜索记录 |

**总是传 `columns`**：控制列序（最重要的列放前面）。不传 → 按 `Object.keys(data[0])` 顺序
（dict 插入序），可能不是你想的。

**table 无排序/分页**：前端纯 dump。如果数据 >2000 行 → 自动降采样。需要前端排序 → 不可行，
考虑在脚本端预先排序后再传。

---

### 面积趋势 → area

**信号**：同 line，但语义上是"累积"或"占比"。

| 场景 | 说明 |
|---|---|
| 堆叠面积（各 phase 占总 loss 的比例） | 同 line 参数，chart_type 改 area |
| 累积数量曲线 | 如"已评估候选数随代数增长" |

---

## 复合场景：一条数据产出多张图

同一数据源可能需要多张图（如 search.jsonl 同时需要收敛曲线 + 帕累托前沿 + 种群计数）。
这种情况**推多张图**，label 相同、title 不同——共用 label 分组在同一 fold 下。

示例（search.jsonl → 4 张图）：
```
label="nas/search", title="Search Convergence — accuracy"    → line
label="nas/search", title="Search Convergence — latency"     → line
label="nas/search", title="Population & Cache per Gen"       → bar
label="nas/search", title="Pareto Front (live)"              → pareto
```

## 反例：不该推图的情况

| 情况 | 处理 |
|---|---|
| 脚本内部循环写 log（每秒 100 行） | 不推图——render_chart 每次调有 socket ack 开销。汇总后推。 |
| 临时文件 / pickle / 二进制 | 不推图——前端只消费 JSON。 |
| 纯内联 agent 无脚本 | 不推图——无结构化数据源，告诉用户加脚本节点来产数据。 |
| 数据字段全 non-deterministic | 先让 agent 输出 `output_schema` 结构化，再推其产出。 |
| 用户说"不需要" | 立即停，不推。 |

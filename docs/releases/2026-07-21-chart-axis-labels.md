# 2026-07-21 chart 加 x_label/y_label/caption 轴标签与图下说明能力

> P1（workflow 重设计计划 Phase 0-a）：解「图表看不懂」根因 C。
> 来源计划：[`docs/plans/2026-07-21-workflow-redesign.md`](../plans/2026-07-21-workflow-redesign.md) §0-a。
> Commit：`a7de596`。

---

## 背景：根因 C

`orca/chart/_render.py::render_chart` 签名原本只有 `chart_type/data/label/title/x/y/hue/color/columns/pareto_*/value/max_points` —— 没有 `x_label/y_label/caption`。

`x` / `y` 是**数据字段名**（dataKey），前端把 schema 名当轴标签用 → 用户看到「latency」「accuracy」等 schema 名而非「时延 (ms)」「精度」等人话。caption 完全缺失，无法解释数据来源/单位/★ 含义。

## 改动（单一真相源 = ChartPayload via `render_chart` 签名）

### 后端 `orca/chart/`

- **`_render.py`**：`render_chart` 签名加 `x_label: str = ""`、`y_label: str = ""`、`caption: str = ""` 三参数。仅在非空时塞进 payload（与 `pareto_direction` 同款契约，保 ChartPayload 干净）。
- **`_validate.py`**：加 type 校验循环 —— 非 str 类型 fail loud，空或省略 OK（向后兼容旧 tape）。

### 前端 `orca/iface/web/frontend/src/components/chart/`

- **`types.ts`**：ChartPayload 加 `x_label?: string`、`y_label?: string`、`caption?: string`（types.ts + `_validate.py` 两端同源 schema-first）。
- **`chartTheme.ts`**：新增 4 个 helper（DRY 抽象，5 widget 共用）——
  - `getXAxisLabelValue(payload)` / `getYAxisLabelValue(payload)`：返回 label 文案（`x_label` 优先，空回退字段名 `x`/`y`，再空回 `undefined`）；供 Scatter/Pareto 的 `XAxis.name` / `YAxis.name`（tooltip label）使用。
  - `getXAxisLabelProp(payload)` / `getYAxisLabelProp(payload)`：返回 recharts `XAxis.label` / `YAxis.label` prop（统一样式：position/angle/fill/fontSize）。
- **`ChartCaption.tsx`**（新）：共享小组件，渲染 caption 为图下小字（`text-[10px] orca-text-faint`，`data-testid="chart-caption"`）。空串不渲染（向后兼容）。
- **8 widget 全部更新**：
  - Line/Bar/Area/Scatter/Pareto：XAxis/YAxis `label` prop（轴标签）+ `<ChartCaption>` 图下说明。
  - Heatmap：caption + `x_label`/`y_label` 渲染为矩阵下方轴标题（条件渲染防空 span 占位）。
  - Radar/Table：caption（RadarTable polar 结构无标准 XAxis/YAxis）。
  - 空值回退字段名 → 保旧行为（schema 名作 label，仍可见，不出现「无标签」情况）。

### TUI `orca/iface/cli/widgets/chart_canvas.py`

- **`_render_plotext`**：plotext `xlabel`/`ylabel`（x_label/y_label 优先，空回退字段名）；非空数据后追加 caption 缩进行；空数据分支也保留 caption。
- **`_render_table`**：加 `caption` 参数，渲染在表格末尾；空数据分支同样追加 caption（与 plotext 一致）。
- **heatmap 降级**：因终端无轴标题位，把 `x_label`/`y_label` 拼进 hint（`（轴：{y_label} × {x_label}）`），保语义不静默丢。

### 落地作证 `workflows/agents/_struct_scripts/viz_struct.py`

`_push_champion_trace` 在 Champion Trace 图上加：
```python
x_label="候选序号(账本行)",
y_label="时延 (ms)",
caption="每轮 champion 的实测时延变化；★=达标",
```

## 向后兼容

- **旧 tape 反序列化**：不含新字段 → 默认空串 → 三端均回退旧行为（字段名作 label / 无 caption）。
- **color (commit b820ef1)**：未触碰，零回归。
- **heatmap chart_type (commit ec3d598)**：未触碰，零回归。
- ChartPayload 「仅在非空时塞」契约：旧 workflow 不传新字段 = 旧 payload 形态 = 旧前端行为，无 breaking change。

## 验证

### 测试（174 chart 相关 + viz_struct smoke）

- **backend `tests/chart/test_render.py`**：5 新测试——透传 / 空省略 / x_label 非 str raise / y_label 非 str raise / caption 非 str raise。
- **TUI `tests/iface/cli/test_widgets.py`**：8 新测试——plotext xlabel/ylabel 显式 / 字段名回退 / caption 渲染 / caption 缺席 / table caption / table 空数据+caption / plotext 空数据+caption / heatmap 降级 axis_hint 保留。修 `test_missing_plotext_degrades_gracefully` cleanup（`monkeypatch.undo()` 先于 reload 才真生效）。
- **frontend `chart.test.tsx`**：14 新测试——caption 在 8 widget 各渲染 / caption 空不渲染 / line+轴 label 文本 / scatter+轴 label / pareto widget 不崩（ComposedChart happy-dom label 不稳，留 playwright） / heatmap 单边 x_label 无空 span / heatmap 双轴缺省不渲染 div / 空字段名回退。
- **回归**：`tests/chart/` 74 测试、`tests/iface/cli/test_widgets.py` 84 测试、`tests/events/test_chart_ingestor.py` 10 测试、`tests/iface/web/test_run_manager_chart.py` 4 测试、`tests/iface/in_session/test_chart_daemon{,_multibyte}.py` 21 测试全部 0 回归。
- **tsc --noEmit** 通过；**vitest chart.test.tsx** 51 测试全过。

### Code reviewer 两轮闭环

**一审（impl + design）**：

- 🔴 删 `tests/iface/cli/test_widgets.py` `TestChartCanvasAxisLabels` 5 个重复方法（Python 类 shadow 静默 dead code）。
- 🟡 `_render_table` 空数据路径丢 caption（违反 hard constraint #8）→ 修：空数据分支也拼 caption。
- 🟡 `test_missing_plotext_degrades_gracefully` cleanup 注释错（`monkeypatch.undo()` fixture teardown 时机是 test 返回后，`finally` 是返回前 → reload 时 sys.modules['plotext'] 仍为 None → reload 后仍判缺失）→ 修：`finally` 内先 `monkeypatch.undo()` 再 reload。
- 🟡 TUI heatmap/radar 降级路径静默丢 x_label/y_label → 修：heatmap axis_hint 拼进 hint。
- 🟢 补 y_label 非 str 单测（与 x_label 对称）。
- 🟢 HeatmapChartWidget 单边 x_label 渲染空 span → 改条件渲染消除。
- 🟢 ChartCaption.tsx 内部守卫与 JSDoc 不一致 → 统一文档说 defense-in-depth。

**二审（coverage）**：

- 🔴 TUI `_render_table` 空数据 + caption 未测 → 补 `test_table_empty_data_caption_appended`。
- 🔴 TUI heatmap 降级 + axis_hint 未测 → 补 `test_heatmap_degraded_preserves_axis_labels_in_hint`。
- 🟡 TUI `_render_plotext` 空数据 + caption 未测 → 补 `test_plotext_empty_data_caption_appended`。
- 🟡 Frontend heatmap 双轴都缺 → 轴标题 div 不存在的显式断言 → 补测试。

## 影响面

- **workflow 作者**：现可传 `x_label/y_label/caption` 三参数让图表可读。无 breaking change（旧 workflow 不传 = 旧行为）。
- **三壳**：web / TUI / 未来的 MCP 都从同一 ChartPayload 读，一处定义三端渲染。
- **后续 P5/P6/P7 workflow 重设计**：可大量传轴标签让量化实验图变人话（PTQ step / sensitivity rank / bit-curve accuracy 等）。

## 不在本次范围

- 已知限制：recharts `ComposedChart`（Pareto）在 happy-dom 单测下 label 渲染不稳（与前沿线 happy-dom 问题同源）；留 playwright 真机验证。
- viz_struct 的其余 4 张图（Pareto/Exploration Tree/Round Ledger/Candidate Ledger）尚未加 x_label/y_label/caption —— 留给 P7（struct/kd 精简）按图表根因逐个修。

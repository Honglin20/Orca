# 2026-07-19 —— chart 加第 8 种 chart_type `heatmap`（行×列矩阵 cell 着色）

**范围**：跨栈改动（后端 Python + 前端 TS/React + CLI TUI）。**不碰** workflows/、quant 相关、events 协议。

## 用途

量化实验对比矩阵（行 = 算法 recipe，列 = 位宽，cell = 精度）。原 7 种 chart_type 都表达不了「二维分类 + 数值着色」语义，故加第 8 种。

## 数据契约（关键设计决策）

- 数据是**长格式**扁平 record array，每个 record 一个 cell：`{recipe: "smooth+gptq", bitwidth: "w4a4-mx", accuracy: 0.92}`。
- 新增字段 **`value`**：cell 值的字段名（仿 scatter 的 `size` 字段命名模式）。
- `x` = 列轴字段（如 bitwidth），`y` = 行轴字段（如 recipe），`value` = 着色字段（如 accuracy）。
- 三者**均必填**（chart_type='heatmap' 时）—— 缺任一会让前端 pivot 退化成 1×1 垃圾矩阵。
- 渲染器把长格式 pivot 成网格：unique yValues × unique xValues，缺失 / 非数值 cell 显示空位（**不静默 coerce 成 0**，防色阶误导）。

## 改动

### 后端 `orca/chart/`
- **`_limits.py`**：`ALLOWED_CHART_TYPES` 加 `"heatmap"`（7→8 种）；注释同步。
- **`_validate.py`**：新增 chart_type=='heatmap' 必填 `x`/`y`/`value`（fail loud，防前端 pivot 退化）。新增 `value` 类型校验（如存在必须 str）。
- **`_downsample.py`**：heatmap 策略与 table 同（`data[:max_points]` top-N 截断；矩阵通常远 < max_points，cap 仅防极端）。**删去**原先 `from orca.chart._limits import ALLOWED_CHART_TYPES` 的死 import（review M2）。
- **`_render.py`**：`render_chart()` 签名加 `value: str = ""` 参数；payload 构造加 `"value": value`；docstring 类型清单 + x/y 必填说明更新。

### CLI TUI `orca/iface/cli/widgets/chart_canvas.py`
- **C1 修复（review CRITICAL）**：原持有 `_CHART_TYPES = {...}` 字面量复制 allowlist（注释自称 DRY 实际相反）→ 改 `from orca.chart._limits import ALLOWED_CHART_TYPES as _CHART_TYPES`（三端同源）。
- heatmap 分派：终端画色阶矩阵性价比低 → DataTable 降级 +「见 Web」提示（与 radar 同策略）。

### 前端 `orca/iface/web/frontend/src/components/chart/`
- **`types.ts`**：`ChartType` union 加 `"heatmap"`；新增 `value?: string` 字段（带 doc comment，仿 `size`）；注释 7→8。
- **`widgets/HeatmapChartWidget.tsx`（新建）**：CSS Grid + 线性色阶（浅钢蓝 → PALETTE[0] 钢蓝），**无新依赖**（recharts 无原生 heatmap）。
  - 色阶端点 `SCALE_LIGHT=(245,248,251)` / `SCALE_DARK=PALETTE[0]=(91,141,184)`。
  - 单值矩阵（max==min）兜底返回 t=1，全用深色端（防除零）。
  - `textColorFor` 在 t > 0.55 切白色（WCAG AA 对比度）。
  - `toNumberOrNull` helper 严格把 `null` / `undefined` / 空串 / 非数字字符串 / 布尔都视为缺失（m1 修：不静默 coerce 成 0）。
  - min/max 用 `reduce` 计算（m5 修：防 `Math.min(...arr)` 在大数组栈溢出）。
  - 缺 value / x / y 显示 fail loud 提示（防御未走 `_render` 的 custom 事件 / 历史 tape）。
- **`ChartWidget.tsx`**：switch 加 `case "heatmap"` 分派；注释 7→8。

### 测试
- **`tests/chart/test_render.py`**：
  - heatmap happy path（含 x/y/value 透传断言）+ 缺 value / 缺 x / 缺 y / 空 value / 非法 value 类型各 raise。
  - 降采样 top-N 截断（heatmap 与 table 同策略）。
  - **两/三端同源 contract test**（review m4 修）：
    - `test_chart_ingestor_and_render_share_MAX_MESSAGE_BYTES_constant`（名实相符，原 test 名承诺 ALLOWED_CHART_TYPES 但只断 MAX_MESSAGE_BYTES）。
    - `test_cli_chart_canvas_uses_shared_allowlist`（**新增**：钉死 CLI `_CHART_TYPES is _limits.ALLOWED_CHART_TYPES`，防 C1 复制 allowlist 重演）。
  - 旧 unknown-chart-type test 用 `"bubble"` 取代 `"heatmap"`。
- **`tests/chart/test_validate.py`**：8 chart_type happy + heatmap 各必填字段缺/空/类型错 raise + 非 heatmap 时 value 可选。
- **`tests/chart/test_downsample.py`**：heatmap top-N + small-data 透传。
- **`tests/iface/cli/test_widgets.py`**：新增 `test_heatmap_degrades_to_table_with_hint`（终端降级 + 见 Web 提示）；class docstring 7→8 chart_type。
- **`orca/iface/web/frontend/test/chart.test.tsx`**：
  - HEATMAP_PAYLOAD（2×2 量化矩阵）+ cell 数 / 标签 / 色阶 legend 用例。
  - 色阶方向钉死：解析 rgb 分量断言 max cell 蓝分量 < min cell（m6 修，原断言只验「不同」不断方向）。
  - 单值矩阵（max==min）不除零（m2 修）。
  - 稀疏矩阵（缺失 cell）显示空位（m3 修）。
  - 非数值 cell（null / 空串 / 布尔 / 非数字字符串）显示空位不 coerce 0（m1 验证）。
  - 空 data 显示 heatmap-empty 提示（m3 修）。
  - 缺 value 显示 fail loud 提示。

## 验收

- 后端：`pytest tests/chart/ tests/iface/cli/test_widgets.py::TestChartCanvas` → 78 passed。
- 前端：`npx vitest run test/chart.test.tsx` → 39 passed。
- TS typecheck：`npx tsc --noEmit` → 0 错。
- 已知 pre-existing 失败（与本次无关）：`tests/iface/cli/test_bg_integration.py::test_bg_run_ps_logs_wait_e2e`（`orca bootstrap` subcommand 问题，stash 后同症状）。

## 取舍

- **SPEC 文档不更新**：`docs/specs/phase-9d/12/13-*.md` 仍写「5/7 种 chart_type」是历史 SPEC 文本。phase-12 SPEC §1.3 已明示「source of truth = types.ts；前序 SPEC 不改」。本次加 heatmap 不动这些历史 SPEC（与项目「SPEC 是契约快照，types.ts 是 source of truth」约定一致）。
- **前端不用 recharts**：recharts 无原生 heatmap，新引依赖（如 react-heatmap-grid）违反「依赖要轻」原则。用 CSS Grid + 线性色阶自实现，零新依赖。
- **TS 不演进为 discriminated union**：review n3 提议改 `(Base & { chart_type: "heatmap"; value: string; ... }) | (Base & {...; value?: never})` 让 heatmap 必填 value 在编译期强制。当前 8 种 chart_type、且会破现有 `{...PAYLOAD, chart_type: "heatmap"}` spread 用法，收益有限 → 暂不演进，记录为未来 4+ chart_type 时再做。
- **CLI 不画色阶矩阵**：终端画色阶性价比低（颜色支持参差、矩阵尺寸受限）→ DataTable 降级 +「见 Web」提示，与 radar 同策略（phase-12 SPEC §1.2 决策）。

## 测试统计

- 后端 +8 heatmap 测试（render 5 + validate 5 + downsample 2 + CLI 1 + contract 2 = 净新增 ~10）。
- 前端 +6 heatmap 测试（cell 数 / 标签 / 色阶方向 / 单值 / 稀疏 / 非数值 / 空 data / 缺 value）。

# release：prof-opt 帕累托图纵轴改绝对精度 + 基线参照独立 + training curves 交互（2026-09-11）

来源：用户看图反馈。spec：[`docs/specs/2026-09-11-profopt-pareto-absolute-metric-and-curves-interact-spec.md`](../specs/2026-09-11-profopt-pareto-absolute-metric-and-curves-interact-spec.md)（轻量级 SDD，会话内确认契约后实现）。

## 背景

用户从 training curves 看到本轮变体都比基线差，但帕累托前沿上却有"比基线好"的点，观感矛盾。诊断出三个机制叠加：① 帕累托图是跨轮累积全量点（老解常驻，`R{n}` 标出身轮次）；② 旧 y 轴是 **gap 与 metric 混轴**（有的点画差值、有的画精度）；③ curves 图 top-10 截断会挤掉老解的曲线。用户拍板：保留全量点不漏，但 y 改**绝对精度单一口径**，基线独立标注，curves 加交互。

## 改动

### 帕累托图（`push_curves.py` + `ParetoChartWidget.tsx`）

- **y = 曲线最新 metric（绝对值，单一口径）**：删 gap 分支——在训点每 epoch 随 watchdog 移动，训完定格末 epoch；gap 不再上图（机械层 verdict/ledger/frontier_snapshot 的 gap 语义不动）。顺带消灭了旧的混轴隐患。
- **基线参照独立**：基线行恒 `y = 基线曲线末行 metric`，新增 `ref_row: true` 字段；前端拆独立菱形系列（`PALETTE[1]` 暖琥珀、图例 `reference`），**不参与前沿支配计算、不进前沿连线**——基线是参照物不是候选解。
- **y 方向翻转**：`pareto_y_direction` min→max（精度越高越好；x 降幅 max 不变）。
- caption 重写：披露绝对口径 / 参照语义 / y=null 达线未训 / 无 makespan 无 x 不绘。
- **坑**：字段名必须 `ref_row` 不能 `ref`——recharts 把行字段展开到 SVG 元素，`ref` 变 React 保留 ref prop 直接渲染崩（vitest 逮住，tsc 查不出）。

### training curves（`LineChartWidget.tsx`，仅方案 A）

- 图例点击 toggle 显隐（隐藏项灰显）；悬停（线体或图例项）高亮：本线加粗、其余 `strokeOpacity 0.15`。三态纯函数 `seriesVisual`（隐藏 > 悬停 > 常态，隐藏优先）。
- 纯组件内 state：跨 REPLACE 推送重渲染保持；非 hue 单线分支不动。

## 验证

- `tests/test_po_scripts.py` 119 green（3 个 pareto 测试重钉 + w3 联合幂等重钉：gap 不上图、`ref_row` 唯一携带、绝对口径）
- 前端 vitest 76 green（`chart.test.tsx` 新增 seriesVisual 三态 + 图例 toggle 冒烟；`ProfoptW3Integration.test.tsx` fixture 重生成后重钉 `isRefRow`/`partitionRefPoints`/reference 图例）；`tsc --noEmit` 零错误
- **真 daemon 冒烟**（`.e2e_scratch/smoke_pareto_ref.py`）：起真 chart daemon → `push_curves` 推送 → tape 载荷逐字段断言（ref_row / y=0.42 metric / 占位 null / 双 max 方向）→ pass
- `tars validate workflows/prof-opt/workflow.yaml` ✓

## code-reviewer 自检（1 MAJOR + 4 minor，全修）

- **M-1**：review 取证在调试中途的快照上看到 `symbol="diamond"`（无效 prop）；终态已回退 `shape="diamond"`（recharts 3.9.1 唯一点形状通道），并加 `legendType="diamond"` 让图例 icon 同形。教训归档：**数据行字段名 `ref` 撞 React 保留 prop 才是渲染崩根因**，与 shape/symbol 无关——当时两案并发导致误诊绕了一圈。
- **m-1**：`test_push_curves_pareto_payload` 的 r1-01 曲线末行补到 0.42（= shard metric，生产中 watchdog 双写恒等），注释恢复真实——钉的是「gap 不上图」而非字段优先级。
- **m-2**：导出 `findParetoFront`，补「参照不参与支配」敏感断言（候选互不支配 = [0,1]；基线并入则 r2-01 被挤出 = [0,2] 反例几何）。
- **m-3**：REPLACE 状态保持补边界披露（title 变 → identity 变 → remount 重置；终推 `(final)` 图全量展示属预期）——widget 注释 + spec 同步。
- **m-4**：spec「测试钉点」节三处 `ref` → `ref_row` 同步。
- **未采纳（NOTE 级）**：隐藏系列仍出现在 tooltip（通用 widget 语义，spec 未钉）；基线曲线缺失时 caption 仍称菱形在场（披露精度问题，行为无错）。

## 边界（维持现状）

- 无 makespan 的变体无 x 不绘（物理性缺坐标，caption 披露）；达线未训 y=null 不绘（不伪造 0）；final_eval 成绩不上图（verdict 可查）；§10.1 top-10 选择策略不动。

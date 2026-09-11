# spec：帕累托图纵轴改绝对精度 + 基线独立标注 + training curves 交互（2026-09-11）

来源：用户看板反馈（run 观感）+ 2026-09-11 会话拍板。轻量级 SDD：本 spec 即确认后的契约。

## 背景

旧形态：pareto y = final gap（无 gap 回退 latest metric）→ **混轴**（有的点画差值、有的画
精度，同一根 y 轴）；基线锚点在 gap basis 下 y=0 是定义值，与曲线观感对不上；training
curves 10 条线静态平铺，无法分辨/聚焦。

## 契约

### C1 — pareto y = 绝对精度（曲线最新值），恒定单一口径

- `collect_pareto` 每个变体行：`y = state["metric"]`（train_status/ledger 的最新曲线
  metric，watchdog 每 epoch 刷新）。**删除 gap 分支**（gap 不再上图；机械层
  frontier_snapshot 的 gap 语义不动）。
- 口径统一：在训点每 epoch 移动，训完定格在最后一个 epoch 的值；**不**换 final_eval 成绩
  （避免两种测量混轴）。final 成绩仍在 verdict.json 可查。
- y=null（达线未训，无训练 metric）不绘——维持现状，caption 披露。
- 无 makespan（无 x）不绘——维持现状（物理性缺坐标，非主动丢点）。

### C2 — 基线锚点独立标注（reference，非候选解）

- 基线行（vid="baseline", x=0, round=0）恒定 `y = 基线曲线最新 metric`（删 gap basis
  分支），恒定携带 **`"ref_row": true`**（新字段，唯一携带方；键名不用 `ref`——
  recharts 把行字段展开到 SVG 元素，`ref` 撞 React 保留 prop）。
- 前端 `ParetoChartWidget`：`isRefRow(row) === (row.ref_row === true)`（严格布尔，缺省
  false）。
  ref 行拆**独立散点系列**：recharts `shape="diamond"`、色 `PALETTE[1]`（暖琥珀，区别于
  前沿钢蓝/被支配灰）、图例名 `reference`；**不参与**前沿支配计算、不进前沿连线、无点旁
  round 标注；tooltip 照常走通用 vid/round/status 通道。
- ref 行仍参与 x/y 轴 domain 计算（x=0 保证原点在场）。
- 判定方向不变语义：`pareto_x_direction="max"`（降幅越大越好）不变；**`pareto_y_direction`
  由 "min" 翻 "max"**（精度越高越好）。

### C3 — caption 重写

披露（一句内）：y = 各变体训练曲线最新值（绝对值，在训点移动/训完定格）；ref 菱形 = 基线
参照（自己曲线最新值），不参选；y=null = 尚无训练 metric（达线未训）；无 makespan 无 x 不
绘。删除旧 mixed-basis 披露句。

### C4 — training curves 交互（仅手段 A，LineChartWidget hue 分支）

- **图例点击 toggle**：点击图例项隐藏/恢复该系列（`Legend onClick` + `formatter` 灰显隐藏
  项 + cursor pointer）。
- **悬停高亮**：悬停某系列（线体或图例项均可触发）→ 该线加粗（strokeWidth 3）、其余线
  `strokeOpacity 0.15`；离开恢复。隐藏优先于悬停。
- 组件内 `useState`，REPLACE 推送重渲染后状态保持（同 identity=title 复用实例）；
  系列消失后残留 hidden 项无害。已知边界：title 变（如终推 `(final)` 后缀）→
  identity 变 → remount 重置——终图全量展示属预期。
- 非 hue 单线分支不动。

## 测试钉点

- `tests/test_po_scripts.py`：`test_push_curves_pareto_payload`（y=metric、y_direction
  max、baseline 行 ref_row:true）、`test_pareto_baseline_anchor_gap_and_metric_basis` →
  改名 absolute-basis（恒 metric basis + ref_row 字段 + 幂等 + 缺曲线 y=None）、
  `test_pareto_baseline_anchor_caption_disclosure`（新 caption 句）。
- 前端 `ProfoptW3Integration.test.tsx`：fixture 重生成
  （`gen-profopt-fixtures.py`，WSL repo 根）；断言/注释同步（baseline y=metric basis、
  ref_row 字段）；`chart.test.tsx` pareto 渲染回归不破。
- 新增：`isRefRow` 纯函数断言（ref_row 严格布尔、缺省 false）；`findParetoFront` 参照
  排除敏感断言（候选互不支配，基线并入则挤掉被支配候选）；LineChartWidget legend
  点击后系列隐藏/恢复（formatter 灰显）冒烟。

## 非目标

- 不改 §10.1 top-10 选择策略；不改 gap 在 verdict/ledger/frontier_snapshot 的机械语义；
  final_eval 不上图；docs/line 两图取数不动。

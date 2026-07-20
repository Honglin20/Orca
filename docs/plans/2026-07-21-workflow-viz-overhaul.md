# Plan: Workflow 可视化全量优化

> 日期 2026-07-21 ｜ 状态：待确认 ｜ 关联：workflows/quant-*、kd-nas、agent-struct-exploration、orca/chart 前后端
>
> 目标：解决量化敏感度分析的 bar 分裂 / table 截断问题；补齐 KD 零图表；横向优化 bit-curve / ptq-sweep / qat；为两个「架构改动」workflow（kd-nas、agent-struct-exploration）提供**逐轮汇总**（每轮改了什么 / 准确率 / 时延）。

---

## 0. 改动矩阵（总览）

| # | 范围 | 改动类型 | 文件 |
|---|---|---|---|
| F | 前端 per-row 着色 | 契约扩展 + 1 widget | `chart/types.ts`、`chart/_validate.py`、`chart/_render.py`、`widgets/BarChartWidget.tsx`（+ `ScatterChartWidget.tsx` best 高亮可选） |
| 1 | quant-sensitivity | 脚本 | `agents/sensitivity-analyzer/scripts/run_sensitivity.py` |
| 2 | KD 可视化 | **新建脚本 + 改 yaml** | `agents/_kd_scripts/viz_kd.py`（新）、`workflows/kd-nas.yaml` |
| 3 | agent-struct per-round 汇总 | 脚本增强 | `agents/_struct_scripts/viz_struct.py` |
| 4 | quant-bit-curve | 脚本 | `agents/bit-curve-searcher/scripts/run_bit_curve.py` |
| 5 | quant-ptq-sweep | 脚本 | `agents/ptq-sweeper/scripts/run_ptq-sweep.py` |
| 6 | quant-qat | 脚本 + 训练循环 | `agents/qat-trainer/scripts/run_qat.py` |
| X | 横向统一 | 各脚本 | table 补失败行 / best 高亮（用 F 的 color 字段） |

依赖铁律：F（前端契约）是 #1 的前置；#2/#3 独立；#4/#5/#6 互相独立。可并行。

---

## F. 前端：新增 per-row 着色字段 `color`（#1 的前置，用户明确要求）

**问题**：现 `BarChartWidget` 只支持两种模式——单 series（无 hue，统一色）或 hue 多 series（`pivotByHue` 把每个 hue 值展开成并列 bar → 敏感度的「左右分裂」根因）。无法表达「统一宽度 + 按状态着色」。

**方案**：ChartPayload 增加可选字段 `color: string`——**每行 fill 颜色的字段名**，该字段值是合法 CSS 色串。着色逻辑留在脚本（确定性，rule 5），前端 dumb 渲染。

改动：
1. `types.ts`：`ChartPayload` 加 `color?: string`（注释：bar/scatter per-mark fill；与 hue 互斥，color 优先）。
2. `_validate.py`：`color` 存在时必须 str（同 x/y/hue 校验段）。
3. `_render.py`：payload 字典加 `"color": color`（默认 `""`），透传。
4. `BarChartWidget.tsx`：`color` 非空 → 渲染 `<Bar dataKey={yKey}>` 内包 `<Cell>` 列表，`fill={String(row[color])}`、`fillOpacity`/`stroke` 同主题；**不渲染 hue 的多 Bar**；Legend 省略（图例含义进 title，如 `(coral=selected)`）。
5. （可选，#5/#6 best 高亮用）`ScatterChartWidget.tsx`：`color` 非空时每个 `<Scatter>` 的点 fill 走 per-row color（recharts Scatter 支持 `<Cell>`）。

**不破坏现有**：`color` 空 → 完全走旧逻辑。

---

## 1. quant-sensitivity（用户核心抱怨）

文件：`agents/sensitivity-analyzer/scripts/run_sensitivity.py` `_push_charts`（L88-140）。

**bar（L107-120）**：
- 去 `hue="status"`（根因）。
- 每行加 `"color": "#D4605A"(NEGATIVE) if sensitive else "#5B8DB8"(PALETTE[0])`。
- 调 `render_chart(chart_type="bar", data=bar_data, x="layer", y="score", color="color", title="Layer Sensitivity by model order (coral=selected)")`。
- 效果：每层**一根统一宽度** bar，敏感层珊瑚色、其余钢蓝色；x 轴按模型原始层序（保留「敏感层在哪一段」的位置直觉）。

**table（L122-138）**：
- 遍历 **`module_order` 全部层**（当前只遍历 `auto_sensitive`）。
- 列 `[layer, score, selected, rank]`：`selected=true` 标入选 rank（1..N），非入选 `selected=false`、rank 留空。
- title 改 `All Layers (selected ranked)`。

**边界**：层很多（数百）时 bar x 轴拥挤——保留现状（按层序），后续可加水平 bar 或 top-N，本期不做。

---

## 2. KD 可视化（用户核心抱怨 —— 这是 bug，不是「没画」）

**根因**：`kd-nas.yaml` 的 `viz_round` 复用 `_struct_scripts/viz_struct.py`，但 KD `ledger.jsonl` schema（`candidate_id/family/proxy_mse/db_gap/met_*/phase/...`）与 viz_struct 要求的 `id/parent/path/status/accuracy` **完全不匹配** → `_clean_ledger` 剔除所有行 → 4 图全 WARN 跳过 → 实际 0 图。`viz_finalize` 是未实现的 prompt stub。

**方案**：新建 KD 专属 viz 脚本，改 yaml 两个 viz 节点指向它。

### 2a. 新脚本 `agents/_kd_scripts/viz_kd.py`
契约（幂等、容错、确定性，对齐 viz_struct 纪律）：
```
python3 viz_kd.py --ledger <dir/ledger.jsonl> --champions <dir/champions.jsonl> \
                  --teacher_meta <dir/teacher_meta.json> \
                  [--mode round|finalize] [--output_dir <dir>]
```
读 KD ledger/champions（真实 schema）+ teacher_meta（baseline latency/accuracy/db_baseline）。数据不足 → 该图 WARN 跳过，exit 0。4 张图，label=`kd-nas`：

| 图 | chart_type | x / y / hue / color | 数据 |
|---|---|---|---|
| 候选轨迹 | line | round / proxy_mse, hue=series(candidate/champion) | 全候选 + champion ratchet 轨迹（proxy_mse 越低越好） |
| latency–proxy 帕累托 | pareto | latency_ms / proxy_mse, pareto_x=min pareto_y=min, hue=met_latency | KD 核心权衡：时延 vs 短训精度代理 |
| **逐轮汇总表** | table | columns 见下 | **满足用户：每轮改了什么 + 准确率代理 + 时延** |
| 终态对比（finalize） | bar（grouped via hue） | stage / latency_ms + stage / db_gap 两张 | teacher vs champion vs final |

**逐轮汇总表列**（用户明确要求「每轮做了什么改动 + 准确率 + 时延」）：
```
[round, family, change, proxy_mse, db_gap, latency_ms, met_lat, met_acc, phase]
```
- `change` = `family + build_cfg 摘要`（KD ledger 无 AST diff 字段——structure_gate 产出的 tag/diff_summary 未入账；用 family+build_cfg 表达「这轮试了什么结构」）。
- `proxy_mse` = 短训精度代理（KD 短训**不跑 eval**，无真实精度；真实 dB gap 推迟到 finalize）→ 表头/列名标 `proxy_mse(acc proxy)`，并在脚本注释说明。
- `db_gap` 短训阶段为占位 0.0；finalize 行才有真实值。
- `latency_ms` = 实测时延（measure_student 真测）。
- 含失败候选（FAIL_export latency=-1 / FAIL_train proxy_mse=-1 原样入表，status 体现在 met_* 列）。

### 2b. `workflows/kd-nas.yaml` 改动
- `viz_round` 节点：脚本调用从 `viz_struct.py` 改为 `viz_kd.py --mode round`（参数从 ledger/champions + teacher_meta 读，baseline_latency/accuracy 从 teacher_meta.json 取，不再需要 viz_struct 的 baseline_* CLI）。
- `viz_finalize` 节点：当前是空 prompt，落实为调 `viz_kd.py --mode finalize`（推终态对比 bar，读 champion + finalize 产出）。
- yaml 注释更新（去掉「复用 viz_struct」表述）。

### 2c. 不动 viz_struct.py 的 KD 适配
不复用、不改造 viz_struct（它是 struct-explore 的契约，KD schema 不同硬塞会污染）。viz_kd.py 独立。

---

## 3. agent-struct-exploration 逐轮汇总增强（架构改动 workflow 之二）

文件：`agents/_struct_scripts/viz_struct.py`。

**现状**：Round Ledger 表（L242-284）列 `[round, proposed, passed_gate, met_target, champion_latency_ms, delta_vs_baseline_ms]`——是「每轮计数汇总」，**缺「每轮/每候选改了什么 + accuracy」**。

**改动**：新增一张**逐候选表**（不动现有 4 图）：
```
chart_type=table, label=struct-explore, title="Candidate Ledger (per change)"
columns=[round, id, hypothesis, accuracy, latency_ms, status, tag]
```
- 字段全部来自 ledger 现有字段（`hypothesis` = 改了什么、`accuracy`、`latency_ms`、`status`、`tag`）——**无需改数据流/curator**。
- 按模型路径或 round 排序；含失败候选（status=FAIL_*）。
- 满足用户「逐轮：改了什么 + 准确率 + 时延」。

**边界**：候选多则表长，DataTableWidget `overflow-auto` 可滚；超 ~500 行考虑 top-N + others（本期不做）。

---

## 4. quant-bit-curve

文件：`agents/bit-curve-searcher/scripts/run_bit_curve.py` `_render_charts`（L244-311，调用 L593）。

- **图1 改真 pareto**：现 `line + hue="series"` 模拟 Pareto → 改 `chart_type="pareto"`，`x=bit, y=metric`，`pareto_direction` 按 report 的 `higher_is_better` 设（max/min）。名实相符。
- **加全候选 scatter**：从 report 取全部 evaluated 候选（非仅 frontier），`scatter, x=bit, y=metric, color=<按是否 frontier/selected 着色>`，前沿点高亮。看「前沿 vs 噪声点」。
- 图2（format bar）、图3（frontier table，已完整）保留。

---

## 5. quant-ptq-sweep

文件：`agents/ptq-sweeper/scripts/run_ptq_sweep.py`（`_push_lw_charts` L517-602、`_push_full_charts` L605-649、调用 L845）。

- **lightweight bar 去 `hue="final_config"`**（无意义 hue：每 path 1 个 final_config，每 x 独一份 → 徒增图例）。改单 series（`x=path, y=metric`），或用新 `color` 字段按 solver 着色。
- **两张 table 补失败/跳过行**：从 `report["candidates"]`（含 status=ok/skipped/error）构造，加 `status`、`error` 列；不只 `ok_results`。诊断时能看到「哪些 recipe 因依赖缺失被 skip」。
- **full best 高亮**：scatter 用 `color` 字段标 best 点（heatmap 暂不支持 per-cell color，best 在 table 标 ★ 或 scatter 单独 series）。
- full heatmap（recipe×bitwidth×accuracy）保留——用户关心的点已覆盖。

---

## 6. quant-qat

文件：`agents/qat-trainer/scripts/run_qat.py`。

- **训练循环（L249-265）捕获 loss**：在 `if step % period == 0` 分支加 `result.setdefault("loss_curve", []).append({"step": step, "loss": float(loss.item())})`（loss 现在算完 backward 即丢）。零额外计算成本。
- **`_push_charts`（L292-349）新增 loss line**：`line, x=step, y=loss, hue=scheme, title="QAT Training Loss"`。
- **table 含失败 scheme**：从全部 schemes（非仅 ok_results）构造，加 `status`、`error` 列。
- **可选 recovery bar**：`bar, x=scheme, y=recovery`（recovery=after-before，QAT 最核心指标，现只在 table）。

---

## X. 横向统一（跨 #1/#4/#5/#6）

1. **table 全量**：所有 quant workflow 的 table 含失败/跳过/错误行 + `status` 列（不只成功子集）。
2. **best/选中高亮**：bar/scatter 用 F 的 `color` 字段（统一机制，不各搞一套）。
3. **hue 纪律**：hue 只用于「同 x 内真有多系列」（如 qat before/after、struct candidate/champion）；每 x 独一份的字段（sensitivity status、ptq final_config）改用 `color`。

---

## 9. 测试 / 验证

- **前端**：`BarChartWidget` 加 `color` 字段单测（color 驱动 Cell；color 空 → 旧路径不回归）。
- **后端契约**：`_validate.py` 对 `color` 的校验单测。
- **脚本**：各 `run_*.py` / `viz_kd.py` 用 mock `render_chart` 单测推图数据 shape（尤其 table 含失败行、bar 用 color 而非 hue）。
- **E2E（真实推图）**：
  - quant-sensitivity：跑 TinyCNN，确认 bar 统一宽度 + 敏感层珊瑚色、table 全层。
  - kd-nas：重跑（或复用 `kd_nas_run_20260720_*` 的 ledger）`viz_kd.py`，确认 4 图产出（之前 0 图）。
  - qat：确认 loss line 出现。

---

## 10. 状态文档（任务完成强制流程）

- `docs/status/CURRENT.md`：本任务快照。
- `docs/status/CHANGELOG.md`：索引 + commit SHA。
- `docs/releases/2026-07-21-workflow-viz-overhaul.md`：release note（含前后对比截图说明）。
- 受影响 agent.md / CONTRACTS.md 同步（若 CLI/schema 变）。

---

## 实施顺序建议

1. **F**（前端 color 字段）——解锁 #1/#4/#5/#6 的着色。
2. **#1 quant-sensitivity**（用户最直接痛点，最快见效）。
3. **#2 KD**（用户核心抱怨 + 工作量最大：新脚本 + yaml）。
4. **#3 struct per-round**（小增强）。
5. **#4/#5/#6** 三个 quant workflow（独立，可并行）。
6. **X 横向 + 测试 + 状态文档**。

每步 commit（[[commit-immediately-on-change]]），逐步可验证。

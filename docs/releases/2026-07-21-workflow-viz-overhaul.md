# Release: Workflow 可视化全量优化

> 日期 2026-07-21 ｜ 计划：[`docs/plans/2026-07-21-workflow-viz-overhaul.md`](../plans/2026-07-21-workflow-viz-overhaul.md)
>
> 触发：用户反馈 quant-sensitivity 的 bar 在 x 轴分裂成敏感/非敏感两半、table 只展示入选层；要求审计其它 workflow 可视化并补齐 KD（当时「没图表」）。

## 成果总览

7 个改动点（F + 1–6），每个点由独立 agent 实现、本会话逐 diff 验收对照计划。

| # | 范围 | commit |
|---|---|---|
| F | 前端 ChartPayload 加 `color` 字段（per-row 着色，hue 优先） | `b820ef1` |
| F+ | ScatterChartWidget 支持 color（F §5 遗留，被 #4/#5 用上必补） | `e1272e8` |
| 1 | quant-sensitivity：bar 去 hue 改 color、table 改全层 | `235ba98` |
| 2 | KD：新建 viz_kd.py（修 0 图 bug）+ 改 kd-nas.yaml 两节点 | `f516223` |
| 3 | agent-struct-exploration：新增逐候选汇总表 | `0910c87` |
| 4 | quant-bit-curve：line+hue 假 pareto → 真 chart_type=pareto + 全候选 scatter | `70bb4ff` |
| 5 | quant-ptq-sweep：删无意义 hue + table 补失败行 + scatter best 高亮 | `d154d1d` |
| 6 | quant-qat：补训练 loss 曲线 + table 补失败 scheme + recovery bar | `f361171` |

---

## 1. quant-sensitivity（用户直接抱怨）

**根因**：`run_sensitivity.py` 的 bar 用 `hue="status"`。前端 `BarChartWidget` 对带 hue 的数据走 `pivotByHue`，把每个 hue 值（sensitive/normal）展开成**独立 bar series**。每个 layer 只属于一种 status → 每个 x-tick 下两个 bar 槽只有一个有值 → 视觉上每根 bar 在 tick 内偏左/偏右，劈成两半。

**修法**：bar 去掉 `hue="status"`，改用新 `color` 字段（敏感 `#D4605A` 珊瑚 / 普通 `#5B8DB8` 钢蓝），统一宽度单 series。table 从只遍历 `auto_sensitive` 改为遍历 `module_order` 全部层，列 `[layer, score, selected, rank]`。

## 2. KD（核心 bug——不是「没画」，是 0 图）

**根因**：`kd-nas.yaml` 的 `viz_round` 复用 `_struct_scripts/viz_struct.py`，但 KD 账本 schema 与 viz_struct 要求字段**完全不匹配**：

| viz_struct 要求 | KD ledger 实际 |
|---|---|
| `id` | `candidate_id` |
| `parent` / `path` / `status` | 无 |
| `accuracy` | `proxy_mse` |

→ `viz_struct._clean_ledger` 剔除 KD 的**每一行** → ledger 清空 → 4 图全 WARN 跳过 → 实际 **0 图**。`viz_finalize` 是未实现的 prompt stub。跑过的 `kd_nas_run_20260720_*` 因此零产出。

**修法**：新建 KD 专属 `viz_kd.py`（532 行，读 KD 自己的账本 + teacher_meta），`kd-nas.yaml` 的 `viz_round`/`viz_finalize` 改调它。4 图（label=`kd-nas`）：

1. **候选轨迹** line：x=round y=proxy_mse hue=series(candidate/champion)
2. **latency–proxy 帕累托** pareto：x=latency_ms y=proxy_mse 双 min，hue=met_latency
3. **逐轮汇总表** table：`[round, family, change, proxy_mse, db_gap, latency_ms, met_lat, met_acc, phase]`——满足用户「每轮改了什么 + 准确率代理 + 时延」。`change` = family+build_cfg 摘要；`proxy_mse` 标注为短训精度代理（KD 短训不跑 eval，真实 dB gap 推迟到 finalize）。
4. **终态对比** bar（仅 finalize）：teacher vs champion vs final 的 latency + db_gap（teacher db_gap=0 baseline）。

**字段契约纠正**：teacher_meta 字段名带 `teacher_` 前缀（`teacher_latency_ms`/`teacher_accuracy`/`teacher_db_baseline`），核对 `teacher_setup.py:384-409` 实际写盘 schema（纠正了计划草稿的非正式描述）。

## 3. agent-struct-exploration 逐轮汇总

`viz_struct.py` 新增第 5 张表「Candidate Ledger (per change)」：`[round, id, hypothesis, accuracy, latency_ms, status, tag]`，字段全来自 ledger 现有项（无需改数据流）。补原 Round Ledger 表缺的「每候选改了什么 + accuracy + 时延」维度。

## 4. quant-bit-curve

chart1 从 `line+hue="series"` 模拟帕累托改为真 `chart_type="pareto"`：x(bit) 恒 `min`，y 方向由 `_infer_pareto_y_direction` 三级 fallback 定（SDK `metric_spec.higher_is_better` → 本地 → metric_kind 启发式）。新增全候选 scatter（`report.archive.records`，非仅 frontier），color 高亮前沿/选中点（coral）vs 噪声点（steel）。

## 5. quant-ptq-sweep

lightweight bar 删 `hue="final_config"`（每 path 独一份 → 无意义图例，同 sensitivity 反模式）。两张 table 数据源改全集 `all_results`，加 `status`/`error` 列（failed/skipped 可见，诊断「哪些 recipe 因依赖缺失被跳过」）。full scatter 删 `hue=recipe` 改 `color` 高亮 best。

## 6. quant-qat

训练循环在 period 采样点捕获 `loss.item()`（原算完 backward 即丢），推 `QAT Training Loss` line（hue=scheme）。table 数据源改全集，加 `status`/`error` 列。新增 `recovery` bar（after−before，QAT 核心指标）。

## F. 前端 color 机制（设计）

ChartPayload 加可选 `color: string`（per-row fill 颜色字段名，值为合法 CSS 色串）。与 `hue` 并存：

- **`hue`**（保留）：多 series 分组并排（`pivotByHue`），用于「每个 x 真有多个可对比系列」（qat before/after、多方法对比）。
- **`color`**（新增）：单 series 内 `<Cell>` 逐行着色（BarChartWidget + ScatterChartWidget），用于「每个 x 只有一个值但想按属性着色」（sensitivity status、best 高亮）。
- **优先级：hue 优先**。hue 非空走分组；hue 缺席 color 才生效。color 空时完全走旧路径（零回归）。

着色逻辑留调用脚本（确定性，rule 5），前端 dumb 渲染。

---

## 横向统一

- 所有 table 含失败/跳过/错误行 + `status` 列（sensitivity / ptq-sweep / qat）。
- best/选中高亮统一走 `color` 字段（ptq-sweep scatter、bit-curve scatter、sensitivity bar）。
- hue 纪律：只用于「同 x 真有多系列」；每 x 独一份的字段改用 `color`。

## 验证

- **py_compile** 全部 6 个改动脚本：OK。
- **tsc --noEmit**（前端）：EXIT=0（F + scatter 改动）。
- **`_validate` color 字段**：接受 str、拒收非 str、hue+color 同存类型层放过（互斥语义归前端）。
- **KD 真实账本验证**（关键）：stub 住 `orca.chart.render_chart` 捕获推送，对 `kd_nas_run_20260720_230644` 跑 `viz_kd.render_all`——round 3 图 + finalize 2 bar 全部正确构造。逐轮表真实数据：`lmmse_front{embed_dim=16,kernel=3,num_blocks=2}` / proxy_mse=1.115911 / latency=0.2002ms。终态 latency bar：teacher 2.91ms vs champion 0.18ms vs final 0.18ms。**之前 viz_struct 对同账本 0 图，现在 5 图**——0 图 bug 修复证实。
- **tars validate workflows/kd-nas.yaml**：通过。
- 各实现 agent 自带 code-reviewer 子审查 + smoke test，本会话逐 diff 复核对照计划。

## 测试策略说明

这些 sidecar 脚本（viz_struct.py / run_sensitivity.py / run_*.py）在仓库里**本就无单测**（既定惯例）。遵循 rule 11（match 惯例），未临时引入单测文件惯例，改用等价真实验证（py_compile / tsc / 真实账本 mock 捕获 / agent smoke test + 逐 diff review）。若要补永久单测，建议作为独立决定统一铺开（非本任务范围）。

## 涉及文件

- 前端：`orca/iface/web/frontend/src/components/chart/types.ts`、`widgets/BarChartWidget.tsx`、`widgets/ScatterChartWidget.tsx`
- 后端契约：`orca/chart/_render.py`、`orca/chart/_validate.py`
- 脚本：`workflows/agents/sensitivity-analyzer/scripts/run_sensitivity.py`、`_struct_scripts/viz_struct.py`、`_kd_scripts/viz_kd.py`（新）、`bit-curve-searcher/scripts/run_bit_curve.py`、`ptq-sweeper/scripts/run_ptq_sweep.py`、`qat-trainer/scripts/run_qat.py`
- workflow：`workflows/kd-nas.yaml`

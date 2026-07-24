# Stage4 viz 执行:producer 侧图表标签补全

> 应用 `docs/plans/2026-07-22-workflow-viz-optimization.md` 6 批 checklist。commits `e05ad2d` / `81facb9` / `3b01a5f` / `4559f65` / `22b2392` / `250e4a7` / `23361af`(fixup)。

## 背景
P1 已给 `render_chart` 铺了 `x_label/y_label/caption` 渲染层(前端 8 widget + TUI 全支持),但 8 workflow ~22 张图里只有 7 张用了,其余 ~15 张轴标签是字段名(`step_idx`/`metric`/`generation`),用户看不懂。本任务只补 producer 侧,**不改前端/TUI**(渲染层已完备)。

## 改了什么(6 batch + fixup)
- **A `viz_kd.py`**:candidate_trace、pareto 补 proxy_mse 语义标签(P0-1/2)。
- **B NAS `tail_metrics.py`+`push_funnel.py`**:Training Loss、Validation Metric、Search Convergence(质量目标取负逻辑显式化 P0-3)、Population&Cache、Pareto live(P0-4)、Selection Funnel。
- **C `push_pareto_final.py`+`push_describe.py`**:**scatter→pareto**(恢复前沿连线 P0-5)+ describe table caption。
- **D `run_ptq_sweep.py`+`run_sensitivity.py`**:lw line(step_idx 语义 P0-6)、lw bar、full heatmap(色阶方向 P0-7)、full scatter、sensitivity bar+table。
- **E `run_qat.py`+`run_bit_curve.py`+`viz_struct.py`**:qat Convergence/Training Loss/Before-After/table、bit-curve scatter/format bar/table、struct 去 ★ 假断言。
- **F `01_training.md` checklist**:C3a/C3b 契约补 label/caption kw(防 inline 推图擦掉 tail 标签)。
- **fixup `tail_metrics.py`**:C3b y_label/caption 与 checklist byte 对齐(修 dedup 替换闪烁)+ Pareto live caption 退化路径不撒谎。

## 硬约束兑现
- **dedup 键 = label+title**:全范围 `git diff | grep label=/title=` 零值文本改动 → **无重复图风险**。唯一 sanctioned 非 pure-add:`push_pareto_final.py` scatter→pareto(label/title 保留,dedup 正确替换)+ ptq-sweeper 透传 `higher_is_better`(plan 唯一可改签名处)。
- **metric 方向陷阱**:量化图 y=mse 下行「看着像坏消息」→ caption 标 `↓lower is better`(抄 qat Recovery bar 模式)。
- `higher_is_better` 设 required 而非 default(错默认会静默产反向 caption,违 Rule 12 fail-loud);`_push_table` caption 可选(display-only,缺=无言)——不对称有语义正当理由。

## 验证
10 脚本 `py_compile` 全 OK;24 viz mock-capture 测试无回归;真实账本 mock 捕获证实新标签到位。code-reviewer 零 🔴,2 🟡 全修。

## 范围外(已核实未动)
- **`workflows/agents/nas-viz/scripts/` 是死代码**:grep 全部 yaml 零引用,三份 push_*.py 与 nas-select/elastic_optimizer 正源逐字相同 → DRY 遗留,未同步删除。
- 未碰 agent.md / yaml / `orca/` / CHANGELOG / CURRENT。

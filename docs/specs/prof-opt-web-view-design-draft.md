# Prof-Opt Web 展示设计草稿（SDD）——分析产物前端视图

> **状态**：草稿 v0（2026-08-31），待用户评审 → 展开为 `prof-opt-web-view-spec.md`。
> **独立定位**：与 [`prof-opt-v6-design-draft.md`](prof-opt-v6-design-draft.md)（workflow 机制改动）**解耦**——本稿只管"现有产物在 web 展示什么、怎么组织、数据从哪来"，不涉及 workflow 机制改造；两条线各自评审、各自落 SPEC。
> **范围**：盘点现有 prof-opt 盘面产物 → 进 web 清单 + 展示组织 + 数据来源。只读展示，不造新数据源。

---

## 0. 目标与边界

- **目标**：基线分析（`business_logic.md` / `information_analysis.md` / `mfu_bottleneck_report.md`）+ 每个变体的瓶颈分析 + 变体业务逻辑/信息分析（v6 新增产物）+ 训练曲线/帕累托 + 轮次结论 + 精度规则，全部可在 web 图表区查看。
- **边界**：只读展示；复用现有 web 能力（chart socket 8 种类型 / tree-file 端点 / dashboard 快照）；全部数据读工作区盘面文件，不新建数据源；展示契约与机制改动分开评审。

---

## 1. 原产物价值盘点（进 web 清单）

### 1.1 基线 / Run 级

| 产物（工作区路径） | 内容 | web 形态 |
|---|---|---|
| `base/origin_anchor.json` | 时延 target / 精度 budget 冻结锚 | Run 概览卡（达标线可视化：target vs 当前 best） |
| `baseline/business_logic.md` | 业务逻辑五段语义契约 | 文档面板（markdown 渲染） |
| `base/information_analysis.md` | 信息成分拆解（最小信息核心 / 冗余近似项 / 创新方向） | 文档面板 |
| `base/profile/mfu_bottleneck_report.md` | 时延瓶颈根因报告（Top 算子 / 根因 / 优化建议） | 文档面板 + 瓶颈 Top-N 表 |
| `base/profile/` 原始产物（schedule_result.json / taskgraph / csv / 甘特图 html） | 算子级时延/调度明细 | 表格 + 甘特图嵌入/下载 |
| `baseline/baseline_metrics.jsonl` + `baseline_full_acc.json` / `baseline_k_acc.json` | 基线训练曲线 + 精度锚 | 曲线基准线 + 参考值 |

### 1.2 变体级

| 产物 | 内容 | web 形态 |
|---|---|---|
| history impl 行 / `rounds/<RRR>/proposals.json` | 改了什么（lever / change_sig / op_delta / predicted） | 变体卡片（改动摘要） |
| `variants/<vid>/profile/mfu_bottleneck_report.md` | 变体自身瓶颈报告 | 文档面板 |
| `variants/<vid>/business_logic.md` / `information_analysis.md`（v6 新增） | 变体业务逻辑/信息分析（软对齐） | 文档面板 |
| `variants/<vid>/conformance.md`（v6 新增） | 与基线主要内容对齐结论 + 差异披露 | 文档面板（核验记录） |
| `variants/<vid>/verdict.json` | 实测 makespan | 帕累托 x + 变体卡片 |
| `variants/<vid>/metrics/metrics.jsonl` + `train_status.json`（v6 新增） | 训练曲线 + 实时 epochs / metric / gap / 状态 | top-10 曲线 + 状态表 |
| `variants/<vid>/eval/final_acc.json` | 最终指标 | 帕累托 y + 表格 |
| `rounds/<RRR>/direction.json` failed_sigs / verdict `latency_fail` | 淘汰/失败原因 | 变体状态标签 + 失败方向清单 |

### 1.3 循环 / 终态级

| 产物 | 内容 | web 形态 |
|---|---|---|
| `rounds/<NNN>/analysis.md` | 轮末结论（淘汰归因 / 预测-实测校准 / 下轮方向） | 轮次时间线文档 |
| `experiment_ledger.json` / `history.jsonl` | 全量实验账本 | 表格 + 帕累托/曲线数据源 |
| `accuracy_rules.json` | 精度规则（change_pattern / evidence / confidence） | 规则面板（只读 + evidence 追溯） |
| `dashboard.json` / `dashboard.html` | 现有静态快照 | 数据源复用 / 渐进替换展示层 |
| `prof_opt_report.md` + 静态图 | 终态报告 | 终态文档 |

---

## 2. 现有 web 能力复用

- **chart socket**：8 种 chart_type（line / bar / area / scatter / pareto / radar / table / heatmap），label/title 幂等 upsert 替换。
- **tree / file 端点**：markdown / 文本 / HTML 渲染——文档正文走它，不把正文塞进 chart 推送。
- **push_curves.py / dashboard_snapshot.py**：推送与快照基础（曲线/帕累托推送的扩展归 v6 P4，本稿不重复设计）。

### 2.1 现状能力缺口与新增适配（2026-08-31 代码核实）

| 能力 | 现状 | 缺口 / 新增适配 |
|---|---|---|
| 图表 | `LineChartWidget` / `ParetoChartWidget` / `ScatterChartWidget` / `TableChartWidget` 等 8 种全在，chart socket 幂等替换 | **零适配**：top-10 曲线（line）+ 全量帕累托（pareto）+ 状态表（table）全部是现有 widget，只需 workflow 侧按规范推 payload |
| 文档渲染 | 前端有 `MarkdownText`（含图片重写）+ `FileContentView` | **前端小适配**：新增"分析文档"面板组件，挂入 RunDetailPage/ChartsView；文档正文复用 MarkdownText |
| 文档读取 | `/api/runs/{id}/assets/{path}` 只服务 `<run_id>/assets/`（图片/二进制资源，三重守卫） | **后端小适配（唯一实质新增）**：prof-opt 分析文档在 `$ORCA_ARTIFACTS_DIR`（`<run_id>/artifacts/` 或项目级 artifacts），assets 端点覆盖不到 → 新增只读 artifacts 文档端点（复用同一套越界/symlink/不存在守卫 + `_read_text_file`） |
| 实时更新 | chart socket 幂等替换语义已有 | 零适配（watchdog 每周期推即可） |
| 面板数据源 | `dashboard.json` / `experiment_ledger.json` 快照 | 扩展字段归 v6 P0；文档清单由 propose/report 推 table chart（W-P1） |

**结论**：web 侧没有大改造——唯一实质后端新增是"artifacts 只读文档端点"（小路由 + 守卫复用），前端是"文档面板组件 + 布局"，图表与实时推送全部零适配。

---

## 3. 展示组织（面板草案）

1. **Run 概览卡**：状态 / 达标线（target vs best makespan）/ 卡数与在飞变体数 / profiling 模式。
2. **基线面板**：三份文档（business_logic / information_analysis / mfu 报告）+ 基线曲线 + 瓶颈 Top-N 表。
3. **变体面板**：卡片列表（vid / 状态 / 改动摘要 / makespan / 精度 / gap）；点开 = 变体文档 + 曲线 + 瓶颈报告 + 核验记录；**失败/淘汰变体保留展示**（全量进帕累托的对应视图）。
4. **图表区**：top-10 曲线（live）+ 全量帕累托（状态着色）。
5. **轮次时间线**：逐轮 `analysis.md` 结论。
6. **规则面板**：`accuracy_rules.json` 只读 + evidence 追溯。

---

## 4. 数据来源与推送机制（待 W-O 裁决）

- **文档正文**：建议走现有 file 端点（web 直接读盘面文件渲染）+ propose/report 推送"文档清单"table chart（vid / 文档 / 状态 / 路径）——见 W-O-1。
- **曲线 / 帕累托**：`push_curves.py` 扩展（v6 P4）。
- **快照**：`dashboard.json` 扩展（v6 P0）。

---

## 5. 开放问题（W-O 系）

| # | 问题 | 默认（待裁决） |
|---|---|---|
| W-O-1 | 文档正文渲染通道 | 文件端点渲染 + 清单 table chart（不把 markdown 正文塞进 chart 推送） |
| W-O-2 | 视图粒度 | 先单 run 视图；跨 run 对比后续再说 |
| W-O-3 | 只读 / 权限 | 沿用 web 现有权限模型，只读展示 |
| W-O-4 | 与现有 dashboard.html 关系 | dashboard 快照继续生成；web 视图渐进替换展示层 |
| W-O-5 | 变体分析文档版本 | 保留最新版 + 修复计数（stamp 键 vid + change_sig + repair count）；不做历史版本库 |

---

## 6. 验收标准（草案）

1. baseline 三份文档 + 每个变体（含淘汰变体）的瓶颈/业务逻辑/信息文档在 web 可读（mock 端点验证 markdown 渲染）。
2. 文档清单随 propose/report 推送正确（table chart 幂等替换）。
3. 只读：web 视图无法改写工作区文件。
4. 回归：`tars validate` + 现有 web 测试全绿。

---

## 7. 实施阶段（草案）

- **W-P1** 文档清单推送 + 文档面板（读盘面渲染）
- **W-P2** 变体卡片 + 轮次时间线 + 规则面板
- **W-P3** 与 v6 P4 曲线/帕累托联调 + 验收

# Prof-Opt Web 展示 SPEC —— 分析产物前端视图

> 依据：[`prof-opt-web-view-design-draft.md`](prof-opt-web-view-design-draft.md)（盘点清单 + W-O-1~5 默认采纳）+ [`prof-opt-v6-spec.md`](prof-opt-v6-spec.md)（§4.1 盘面产物契约、§10 曲线/帕累托推送契约）。
> 独立交付：本 SPEC 只管展示，不混入 workflow 机制改动；评审通过后逐字实现。
> 环境约束：前端 vitest / 后端 pytest 走 WSL `.venv`；不 push；改完按洁净契约检查。

---

## 0. 范围与非目标

**范围**：
- 后端新增 1 个只读端点：run 产物（`$ORCA_ARTIFACTS_DIR`）下的分析文档读取（markdown 渲染数据源）。
- 前端新增"分析文档"面板：基线三份文档（`business_logic.md` / `information_analysis.md` / `mfu_bottleneck_report.md`）+ 每变体（含淘汰/失败）三件套 + `conformance.md`，复用现有 MarkdownText / FileContentView / chart widget。
- 图表区消费 v6 §10 推送（top-10 曲线 + 全量帕累托）——前端**零 chart 渲染改动**（8 种 widget 已在），只补面板入口与数据组织。

**非目标**：
- workflow 机制（propose/probe/watchdog/早停）→ `prof-opt-v6-spec.md`。
- 跨 run 对比视图（W-O-2 采纳：先单 run）。
- 文档编辑 / 写回（只读展示）。
- 认证体系改造（沿用 web 现有权限模型）。

---

## 1. 数据来源盘点（盘面文件 → 面板映射）

| 面板 | 数据源（读盘面） | 展示 |
|---|---|---|
| Run 概览卡 | `base/origin_anchor.json` / `train_device.json` / `experiment_ledger.json` / `profile_mode.json` | 状态 / target vs best / 后端与卡数 / 在飞变体数 / profiling 模式 |
| 基线面板 | `baseline/business_logic.md`、`base/information_analysis.md`、`base/profile/mfu_bottleneck_report.md`、`baseline/baseline_metrics.jsonl` | 三文档 + 基线曲线；瓶颈与分析源路径统一在 MFU Markdown 中展示 |
| 变体面板 | `variants/<vid>/` 下：business_logic.md / information_analysis.md / conformance.md / profile/mfu_bottleneck_report.md / verdict.json / metrics/metrics.jsonl / train_status.json / eval/final_acc.json + history 行 | 卡片列表（vid/状态/改动摘要/makespan/精度/gap）+ 点开文档与曲线；淘汰变体保留 |
| 图表区 | `push_curves.py` 推送（v6 §10） | top-10 line + 全量 pareto |
| 轮次时间线 | `rounds/<NNN>/analysis.md` | 逐轮结论文档 |
| 规则面板 | `accuracy_rules.json` | 只读规则表（id/pattern/direction/generality/confidence/evidence） |

---

## 2. 后端契约：run 产物只读文档端点（唯一实质新增）

### 2.1 路由

```
GET /api/runs/{run_id}/artifacts/file?path=<相对路径>
```

- 解析根（**定死，2026-08-31 拍板**）：run 元数据中的 artifacts 根——即该 run 的 `$ORCA_ARTIFACTS_DIR` 实际落盘值。实现时从 run 记录读取 artifacts 路径字段（现有 run_manager 已持有），**不做** `<runs_dir>/<run_id>/artifacts/` 硬编码拼接；字段缺失 → 404 fail loud。
- 响应：`text/plain; charset=utf-8`（前端按 `.md` 渲染 markdown）；`.json` 同端点返回原文（表格/卡片数据也可走它）。
- 守卫：**与 `resolve_asset_path` 等强度**——路径越界（`..` / 绝对路径）→ 404；symlink（末端或中间段）→ 404；文件不存在 → 404；大小超 `_MAX_FILE_BYTES`（复用 workflows.py 的 1MB）→ 413 fail loud。
- 未知 run_id → 先 `ensure_attached`（沿用现有语义）→ 仍未知 → 404。

### 2.2 实现要点

- 复用 `run_manager.resolve_asset_path` 的三重守卫模式，抽为共享守卫函数（不复制逻辑）；`_read_text_file` 与 `workflows.py` 共享（DRY）。
- **只读**：无任何写路径；不暴露 fs 绝对路径（响应不含 path）。

### 2.3 文档清单推送（chart 相似路径，2026-08-31 拍板）

分析文档走**与 render chart 相似的通道**——工作流侧只推一个 `chart_type="table"` 的清单 chart（label `prof-opt/docs`，行 = vid（或 baseline）/ 文档名 / 状态 / 相对 path），**不推 md 正文**（契约归 v6 §10.4）。

- 前端经现有 charts 流收到清单 → 面板只渲染名称/状态列表（**不显示正文**，避免推送与页面体量爆炸）。
- **点开后才拉正文**：点选条目 → 调 §2.1 端点 `GET /api/runs/{run_id}/artifacts/file?path=<相对 path>` → MarkdownText 渲染。
- 清单幂等替换（label/title 语义与 chart 一致）；path 白名单：前端只使用清单内相对 path，不自行拼接。

---

## 3. 前端契约：分析文档面板

### 3.1 组件

- 新增 `ProfOptDocsPanel.tsx`（挂入 `RunDetailPage` 图表区旁）：分组 = 基线 / 变体（按轮序）/ 轮次 / 规则；每项 = 名称 + 状态徽标 + 更新时间。
- 正文渲染复用 `MarkdownText`（图片重写沿用现有 assets 重写约定，改写为 artifacts 端点前缀）；JSON 明细复用 `FileContentView`。
- 变体卡片 = `TableChartWidget` 数据或前端自渲染（二选一，实现时定：**优先复用 chart table payload，前端不另造表格逻辑**）。

### 3.2 数据流

```
chart socket（文档清单 / 曲线 / 帕累托）→ store（现有 charts 流）
  ↓
ProfOptDocsPanel：读清单 → 只渲染名称/状态（不渲染正文）
  ↓ 点选条目
GET /api/runs/{id}/artifacts/file?path= → MarkdownText（点开才渲染）
图表区：现有 ChartsView 直接渲染（零改动）
```

### 3.3 时序与管控

- 文档落盘即展示（propose 达线 → 清单推送 → 面板可点）；修复中间版本保留最新 + repair_count（W-O-5 采纳，不做历史版本库）。
- 失败/淘汰变体文档保留展示（全量进帕累托的对应视图）。

---

## 4. 图表区契约（消费 v6 §10，前端零渲染改动）

- top-10 曲线：`line`，label `prof-opt/curves`，title `prof-opt training curves`（live）/ `(final)`（report 终稿）——现有 ChartGroup upsert 语义。
- 全量帕累托：`pareto`，label `prof-opt/pareto`，title `prof-opt variants pareto`；x/y/状态着色按 v6 §10.2 payload 规范。
- 状态表：`table`，label `prof-opt/ledger`（watchdog 周期推，可选）。
- 未知 chart_type / payload 校验失败 → 现有 fail loud 组件行为（不静默）。

---

## 5. 只读与安全

- 新端点只读 + 三重守卫（§2.1）；前端无任何写操作入口。
- 面板不渲染未授权路径；清单由 workflow 推送（白名单路径），前端不自行拼接任意路径。
- 大文档 413 + 前端降级提示（不崩）。

---

## 6. 测试与验收

### 6.1 后端（pytest，新增 `tests/test_web_artifacts_docs.py`）

1. 越界（`../` / 绝对路径）/ symlink / 不存在 → 404；未知 run_id → 404（attach 语义）。
2. 正常 md / json 读取 → 200 + 正确内容；超 1MB → 413。
3. 守卫与 `resolve_asset_path` 行为等价（共享守卫单测）。

### 6.2 前端（vitest，新增 `ProfOptDocsPanel.test.tsx`）

1. 文档清单（table payload）→ 面板分组正确；点选 → mock 端点 → MarkdownText 渲染。
2. 基线三文档 + 淘汰变体文档可见；404/413 → 降级提示不崩。

### 6.3 联调验收

1. mock chart socket：清单 / top-10 / 帕累托 推送 → 前端面板与图表正确更新（幂等替换）。
2. 只读验证：端点 + 面板全程无写请求。
3. 回归：`tars validate` 0/0；现有 web 测试全绿。

---

## 7. 文件触达清单

**后端**：`orca/iface/web/routes/runs.py`（新端点）、`orca/iface/web/run_manager.py`（共享守卫 / artifacts 根解析）、`orca/iface/web/routes/workflows.py`（`_read_text_file` 共享化，如需要）。
**前端**：`frontend/src/components/` 新增 `profopt/ProfOptDocsPanel.tsx`（+ 测试）；`frontend/src/components/pages/RunDetailPage.tsx`（挂面板）；复用 `MarkdownText.tsx` / `FileContentView.tsx` / chart widgets（零改）。
**workflow 侧（归 v6 P4/W-P1 联调）**：`push_curves.py`（文档清单 table 推送）。

---

## 8. 依赖与排期

- 依赖：v6 SPEC P0（`experiment_ledger` 扩展 / `train_status.json`）、P4（`push_curves` 扩展）产出盘面文件与推送；本 SPEC 可并行写后端端点 + 前端面板，联调放 P4 后。
- 排期：W-P1 后端端点 + 文档清单推送 → W-P2 前端面板 + 测试 → W-P3 与 v6 P4 联调 + 验收。
- 真机清单（归用户）：真实 run 下文档面板 + 图表联调。

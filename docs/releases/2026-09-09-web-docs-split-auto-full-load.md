# 2026-09-09 web：大 run 取消手动「加载全部」+ 文档面板左右分栏

## 背景（用户两反馈）

1. 窗口态大 run 打开「图表/文档」页签只有目录占位，每次都要手动点「加载全部」才出真数据。
2. 文档面板 32px 彩色徽章竖排卡太大很高；点选后「上清单下正文」堆叠不舒服。布局用户拍板：**左右分栏**。

## 改动

### D1 窗口态自动后台全量（取消「加载全部」按钮）

- web-perf P3 首屏 tail=500 窗口保留（秒开）；`loadRunWithMeta` 满窗提交后自动 `loadFull(runId, {background:true})`——不翻 `loadStatus`（首屏内容保持可读，不闪加载态），成功后占位目录自动替换为真实 chart/doc。
- store 新配置位 `autoFullLoad`（默认 true；unloadRun 不清；测试经 resetStore 关闭保确定性）。
- 失败 → 既有 `writeLoadError` → RunLoadError 显式重试（重试链路复用，再次自动触发）。
- `loadFull` 顺带补窗口簿记回整（`oldestSeqInWindow: 1`，对齐 resume-fallback 不变式）。
- 文案改自动语义：图表占位横幅/占位卡「后台加载中」、文档清单「完整清单后台拉取中…」。

### D2 文档面板左右分栏（ProfOptDocsPanel 渲染层重构）

- 左清单 `w-72` 独立滚动 / 右正文 `flex-1` 独立滚动（RunDetailPage docs 容器改 `overflow-hidden`）。
- 清单项单行化：13px 线性类型图标（FileText/Braces）+ 纯文件名 truncate + 短时间戳（可选）+ 状态点；完整 path 进 hover title、完整 ISO 进时间戳 title。
- 删除：32px 彩色 TYPE_BADGES 竖排卡网格、底部预览区、70vh/96px 限高、外层折叠按钮。
- 分组与正文三态（inline / omitted / legacy fetch）逻辑零改动；全部 testid 契约保留（doc-item / docs-group-* / docs-variant-card-* / docs-round-* / docs-round-misc / doc-selected-name 等）。

## 验收

- vitest 受影响套件 175/175 绿（huge-mode 增 2 例：自动全量 happy path + C-1 失败路径——错误态落位、窗口态保留、全程不闪 loading）；tsc clean；static 闭包重建。
- code-reviewer CONDITIONAL_PASS → 1 必修（C-1 失败路径测试）+ 2 顺手项（m-2 注释漂移×2、M-1 计划表述修正）全闭环，后转 PASS。

## 已知限制（review M-1）

- **straggler 竞态**：后台全量快照后、提交前到达且 seq > 快照 max 的 WS 事件随整表替换丢失；resume（`since=lastSeqSeen`）补不回中间缺口，恢复仅限刷新/重开。语义与既有手动 loadFull 相同，但自动化放大了暴露面（每次打开窗口态 run 即进入竞态窗口）。后续可评估提交改 seq 并集合并——设计变更须先走方案。
- 超大 run 一次后台 refold 成本（首屏不受影响）；若仍卡，后续做 per-kind 数据端点。

## 文件

`workflow-store.ts` / `ChartRenderer.tsx` / `ChartGroup.tsx` / `ProfOptDocsPanel.tsx` / `RunDetailPage.tsx` / `selectors.ts`（注释）/ `test/_helpers.ts` / `test/huge-mode.test.ts` / `test/chart-renderer.test.tsx`；计划见 [plans](../plans/2026-09-09-web-docs-split-auto-full-load.md)。

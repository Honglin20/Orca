# 2026-09-09 web：大 run 取消手动「加载全部」+ 文档面板左右分栏

## 背景（用户反馈两点）

1. 窗口态大 run 打开「图表/文档」页签只显示目录占位，每次都要手动点「加载全部」才出真数据 → **取消手动点击**。
2. 文档面板丑：32px 彩色类型徽章竖排卡太大很高；点选后「上清单下正文」堆叠看不舒服 → **重做**。布局用户拍板：**左右分栏**。

## D1 窗口态自动后台全量（取消「加载全部」按钮）

- web-perf P3 首屏 tail=500 窗口**保留**（秒开不受影响）。
- `loadRunWithMeta` 提交窗口态（`windowed=true`）后自动触发 `loadFull(runId, { background: true })`：
  - 复用既有 fetch 退避 + `_refoldAndCommit` 原子提交；**background 模式不翻 loadStatus**（首屏内容保持可读，不闪加载态）。
  - 成功 → `hugeFullyLoaded=true + serverOverview=null` → 占位目录自动替换为真实 chart/doc（既有路径，零新渲染逻辑）。
  - 失败 → 既有 `writeLoadError` → RunLoadError 显式重试（fail loud，不新增静默态；重试链路复用 loadRunWithMeta → 再次自动触发）。
- store 新字段 `autoFullLoad: boolean`（默认 true）：测试确定性开关（`resetStore` 置 false，防后台 fetch 污染既有窗口态断言）+ 未来逃生门。`unloadRun` 不清（配置位非 run 态）。
- UI 文案改自动语义：ChartRenderer 占位横幅按钮 → spinner 提示；ChartGroup 占位卡「后台加载中…」；文档面板 huge hint「完整清单后台拉取中…」。
- `loadFull` action 保留（public，签名加可选 `opts.background`）。

**已知取舍（承接不改）**：
- 超大 run 一次后台 refold 成本（首屏/O(窗口) 不变；全量 replay 卡顿从「点进时」移到「首屏后」，用户以点击换无感）。
- **straggler 竞态（已知限制，review M-1 修正表述）**：快照后、提交前到达且 seq > 快照 max 的 WS 事件随整表替换丢失；提交后 `lastSeqSeen` 被 in-order push 推高，重连 resume（`since=lastSeqSeen`）**补不回中间缺口，唯一恢复 = 刷新/重开**。语义契约与既有手动 loadFull 相同，但自动化使每次打开窗口态 run 都进入竞态窗口（活 run WS 流量恰在此时最密）。后续可评估 loadFull 提交改 seq 并集合并（同 `loadEarlierChunk` 去重合并模式）——设计变更须先走方案，不打补丁。
- 若未来超大 run 后台 refold 仍卡：per-kind 数据端点（服务端按 kind 出数据）为后续方案，本轮 out-of-scope。

## D2 文档面板左右分栏（ProfOptDocsPanel 重构渲染层）

- 根布局 `flex h-full min-h-0`：左清单 `w-72` 独立滚动（`orca-border-r`）/ 右内容 `flex-1` 独立滚动。RunDetailPage docs 容器 `overflow-auto` → `overflow-hidden`。
- 清单项：单行行卡（13px 线性图标 FileText/Braces + 纯文件名 truncate + 右侧短时间戳可选 + 状态点）；完整 path 进 hover title，完整 ISO 时间进时间戳 title。
- **删除**：32px 彩色 TYPE_BADGES 竖排卡网格（`CARD_GRID_CLASS`）、底部预览区、`max-h-[70vh]`/`max-h-96` 限高、外层折叠按钮（独立页签无需折叠）。
- 分组契约不变：基线/规则平铺组头（`docs-group-*`）；变体按 vid 子组头含状态文本（`docs-variant-card-<vid>`）；轮次 Round N 可折叠子区（`docs-round-*`/`docs-round-misc`）。行 `doc-item`、右栏 `doc-selected-name`/`doc-loading`/`doc-omitted`/`doc-fetch-error` 全部保留。
- 正文三态（inline / omitted / legacy fetch）逻辑零改动。

## 影响

- 文件：`workflow-store.ts`、`ChartRenderer.tsx`、`ChartGroup.tsx`、`ProfOptDocsPanel.tsx`、`RunDetailPage.tsx`、`test/_helpers.ts`。
- 测试：huge-mode 增 2 例（auto 触发后台全量 happy path + C-1 后台全量失败路径：错误态落位、窗口态保留、全程不闪 loading）；chart-renderer 2 例改自动语义（按钮消失 / loadFull 直调验证渲染契约）；ProfoptDocsPanel 断言零改动（updated_at 保留可见短文本，title 承载完整 ISO）。
- 验收：vitest 受影响套件绿 + `tsc --noEmit` 干净 + static 闭包重建。

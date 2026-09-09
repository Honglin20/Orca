# Release Note：prof-opt 文档面板 —— 轮次子分组 + 文件管理器风图标卡片

日期：2026-09-09 ｜ 类型：feat(web) ｜ 范围：纯前端（`orca/iface/web/frontend`），后端契约零改动

## 用户反馈 → 改动

| # | 反馈 | 改动 |
|---|------|------|
| 1 | 轮次组文档全混在一起 | 按 `docRoundNoOf`（`selectors.ts` 新增，`rounds/<N>/` 数字派生）拆「Round N」可折叠子区，数字升序、默认展开 |
| 2 | 卡片名显示文件夹路径 | 轮次卡片由完整 path（同名 `analysis.md` 的旧消歧 workaround）改显**纯文件名**；完整 path 进 hover title（button + 文件名 span 双落点） |
| 3 | 图标丑 | 三方向草图对齐后拍板 **B · 文件管理器风竖排图标卡**：32px 彩色类型徽章（`.md` 蓝底白「M↓」/ `.json` 琥珀底白「{ }」/ 未知扩展灰底 FileText 兜底）+ 文件名 + 状态点 + 短时间戳（ISO 压 `YYYY-MM-DD HH:MM`，原文进 title） |

## 细节

- **变体组**保留 per-vid 外层卡，内层文件网格换新竖排卡；基线/规则组同款卡片，全面板形态统一。
- **不静默丢行**：不匹配 `rounds/<digits>/` 形状的轮次组行落 `docs-round-misc` 网格照常渲染（测试锚定）。
- **错误信息可定位**：404/413/HTTP 错误提示改显完整 path（同名 analysis.md 跨轮可区分）。
- DRY：四处卡片网格接线收敛为 `DocCardGrid` 单组件。
- code-reviewer 自检 1 MAJOR + 3 MINOR 全闭环（title 落点契约回归 / 错误信息 path / DRY 抽取 / misc 测试锚定）。

## 验证

- vitest：受影响 3 套件 55/55 通过；全量 646 测试中仅 `agents-rail` 的 DAG lazy 挂载在冷缓存并行下超时（隔离 1.6s 全过，实证环境型存量 flake，与本次改动无关）。
- tsc --noEmit clean；`vite build` 静态已重建（`tars serve` 刷新页面即可见，无需重启进程）。

## 计划与设计

- 事前计划：[docs/plans/2026-09-09-profopt-docs-panel-round-cards.md](../plans/2026-09-09-profopt-docs-panel-round-cards.md)（含用户 B 方案草图拍板记录）

# 计划：prof-opt 文档面板 —— 轮次分组 + 文件管理器风图标卡片

日期：2026-09-09 ｜ 状态：设计已与用户对齐（B 方向草图拍板）

## 背景与用户决策

用户对「文档」tab（`ProfOptDocsPanel`）三条反馈：

1. **轮次组混杂**：`docGroupOf` 只按 `path.startsWith("rounds/")` 归一个扁平组，
   Round 1/2/3 卡片全堆一起 → 按轮次分子组。
2. **卡片名带文件夹**：轮次组显示完整路径 `rounds/001/analysis.md`（因文件名
   撞车的历史 workaround）→ 分组后改显纯文件名，完整路径进 tooltip。
3. **图标丑**：14px 灰色线性 FileText → 用户三选一拍板 **B · 文件管理器风
   图标卡片**（32px 彩色类型徽章置顶 + 文件名 + 状态点，竖排卡片）。

纯前端改动，不碰后端契约（`push_curves.py` 清单 payload 不动）。

## 改动清单

1. `frontend/src/selectors.ts`：新增 `docRoundNoOf(row): number | null` ——
   从 `path` 解析 `^rounds/(\d+)/` → 数字（`Number` 去前导零）；非 rounds 行
   返回 null。纯派生放 selectors 层（铁律 1：selectors 是唯一 view 输入）。
2. `frontend/src/components/profopt/ProfOptDocsPanel.tsx`：
   - `DocItemCard` → 竖排图标卡：32px 圆角实底徽章（`.md` 蓝底白「M↓」、
     `.json` 琥珀底白「{ }」、未知扩展灰底 FileText 兜底）+ 文件名（truncate，
     `title=完整 path`）+ 状态点 + 短时间戳（ISO 压成 `YYYY-MM-DD HH:MM`，
     完整 ISO 进该 span 的 title）。**所有组统一用此卡**，名字一律 `row.doc`。
   - 轮次组 → 按 `docRoundNoOf` 数字升序分「Round N」可折叠子区
     （默认展开，交互态 local useState，同 ChartGroup 模式）；
     外层 testid `docs-group-rounds` 不变，子区 `docs-round-<n>`。
   - 变体组保留 per-vid 外层卡，内层文件网格换新竖排卡。
3. 测试：`ProfoptDocsPanel.test.tsx` / `ProfoptW3Integration.test.tsx` 的
   path-text 断言改为「Round 头 + 文件名」；新增轮次子分组断言
   （`docs-round-1` 只含 001 行等）；`title=path` 的既有断言不动；
   `selectors.test.ts` 补 `docRoundNoOf` 用例。

## 不做

- 后端 manifest 字段、`collect_docs` 白名单、content 通道：零改动。
- 图表 tab（`ChartGroup`）与「加载全部」流程：零改动。

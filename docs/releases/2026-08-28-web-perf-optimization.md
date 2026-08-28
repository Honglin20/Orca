# Release — Web 界面性能优化（传输层 / markdown chunk / 渲染路径 / 虚拟化）

> 日期：2026-08-28 ｜ SDD-LOOP 全流程 ｜ 分支 `puzzle-supernet`
> SPEC：`docs/specs/2026-08-28-web-perf-optimization.md` ｜ 计划：`docs/plans/2026-08-28-web-perf-optimization.md`

## 背景

用户报告三症状：整体加载慢 / workflow·run 查看页慢 / markdown 渲染慢。调查定位五个根因：后端无 gzip、无 Cache-Control 且 `static/assets` 旧 hash 产物无限累积；MarkdownText chunk 1018KB（`rehype-prism-plus` 根入口无条件绑 refractor/all 297 门，sideEffects 声明致无法摇除）；katex CSS 进首屏 index chunk；ConversationView / LogStream / ChartRenderer 三处全 store 订阅 + 每事件 O(N)×多 selector 重算；虚拟化行高固定估高失真、阈值 500 过高且虚拟化分支零测试覆盖。

## 改动（commit `e756b8c..b824f24`，6 commits）

| Commit | 批 | 内容 |
|---|---|---|
| `e756b8c` | 批 1 | 传输层：GZipMiddleware(minimum_size=1024) + hashed 资产 `immutable` 缓存 + SPA no-cache + API 头不变；`npm run build` 链插 clean-assets 脚本（tsc 后 vite 前，仅清 `assets/` 子目录，`static/.gitignore`/`.gitkeep` 存活）；新 `tests/iface/web/test_static_transport.py` 4 用例钉头契约 |
| `1494bfd` | 批 2 | markdown chunk 瘦身：prism 高亮改 `rehype-prism-plus/common` 子路径默认导出（36 门，1018KB→536KB）；katex CSS 随 markdown chunk 加载（移出首屏）；补 `.token.keyword` 级断言（仅 class 断言在 0 门下也假绿）+ tsx fail-soft 用例 |
| `f9c9548` | 批 3+4 | 渲染路径：store fold 派生 `ev: NodeEventIndex`（all/bySession/last，main 哨兵）+ `takenEdgeKeys`（独立 route_taken 接线，`routeEdgeKey` 统一 helper）+ 事件归属唯一化 `conversationTargetNode`；ConversationView/LogStream/ChartRenderer 逐字段订阅（selector `_from` 变体，禁新建对象）；EntryRenderer memo + onChartClick useCallback；reuseEntries 逐字段复用（ToolPair 级按事件对象引用）；行高 `useDynamicRowHeight`（defaultRowHeight 96，key 防滚动残留）+ 阈值 500→100（虚拟化测试先于阈值落地，>500 条口径起步、120 条终态补断言）；canary snapshot/foldTwice 投影复制值修复（不含 ev，杜绝事件引用数组进双快照）+ 删两处非幂等 indexConversationEvent |
| `27b7eb2` | 辅助 | topbar.test 既有红灯修复（0e2bc11 引入 useNavigate 后测试缺 Router 上下文；基线对照证实非本任务引入，仅包 MemoryRouter，零产品代码改动） |
| `e3e66f6` | 辅助 | 内环 review MINOR-1：删零消费方 `eventMatchesNode` 死代码 |
| `b824f24` | 辅助 | 两测试文件 `_helpers` import 归位 |

## SDD-LOOP 流程记录

- **Phase 1 spec 评审环 2 轮**：轮 1 CONDITIONAL_PASS（15 真问题修订）+ UD×2 拍板（UD-1 AgentsRail 订阅收窄显式遗留为独立后续任务；UD-2 A6 不设硬 KPI，性能凭证 = chunk 字节 + gzip 头）；轮 2 PASS / USER_DECISIONS=0 / SELF_REVIEW: CLOSED
- **Phase 2 计划环 0 外环轮**：planner-agent 对已转正计划做终版 SPEC 一致性验证，plan-adversary 2 轮内环闭环（5 处最小化修订：A4 前置补 assets 非空、A3 补对账动作、A5 补 gzip ≥1024B 口径、步 0 红窗修复 >500 条、C3.1 补哨兵细节）；15 处行号/路径实测抽查全属实
- **Phase 3 实现**：coder 内环 code-reviewer 1 轮收敛（4 MINOR：1 修复 3 显式分歧延后——canary 检测面扩围超契约 scope、`_from` 命名为计划字面保留、渲染计数断言脆性未纳入）
- **Phase 4 E2E 1 轮 PASS**：test-agent 纯验证，A1-A6 全过，缺陷 0

## 验收证据（E2E test-agent 真实执行，证据目录 `.e2e_perfver/evidence/`）

- **A1/A2**：vitest 590/590 全绿（34 文件，断言零改动）；tsc 零错；C6.2 负例 `store-fail-loud` 17 用例全绿且 `warn toBe(0)` 断言原样未动
- **A3**：MarkdownText chunk **535.95KB**（<700KB 红线，gzip 165.78KB）；index CSS `@font-face` 0 个（katex 20 个全数移入 MarkdownText CSS）；assets 与 vite build 日志清单逐一对账一致（72 文件 / 2.9MB，旧态 5.5MB/80 文件），无旧 hash 残留
- **A4/A5**：后端 iface/web pytest 全绿；真实 `tars serve` curl 实测——≥1024B 资产 `Content-Encoding: gzip` + `immutable` 双头命中；`/` no-cache（686B 不压缩，阈值正确）；无 gzip 客户端明文回退；API 无自定义 Cache-Control
- **A6**：真实 chromium 冒烟（LD_LIBRARY_PATH 修补缺库，无 mock）：1507 事件合成 tape run 详情页虚拟化分支真实挂载（`conv-vrow-*` × 27）；prism common 高亮 `.token` × 244；katex 公式 `.katex` × 2；全程 console error / pageerror / 4xx 均 0；含部署后刷新
- **存量项（非本次引入）**：playwright 存量 20 失败经 baseline 对照（29→20，HEAD 严格多过 9 个）实证为多 run 时序/选择器漂移 + WSL 缺 chromium 系统库，归存量维护

## 已知项 / 遗留

- prism 高亮能力 297 门 → common 36 门（产品级已知变化，未收录语言 fail-soft 原样渲染）
- katex CSS 随 chunk 异步 → 首屏直连 `.md` 预览一次性 FOUC（单用户工具可接受）
- AgentsRail 每事件重渲染 = UD-1 显式遗留，需 stall 派生索引，独立后续任务
- GZip 不自动加 `Vary: Accept-Encoding`——127.0.0.1 无代理无实害，置反代后需补
- 部署后旧 tab lazy-load 旧 hash chunk 404 → 刷新即恢复（index.html no-cache），显式接受
- playwright 存量失败 + WSL chromium 系统库缺失 → 建议另开存量维护任务

# 2026-07-08 — Web Shell v2 Chunk D：completion + polish + bundle split

按 SPEC §0 D10 / D6 / §5.3 / §5.6 / §5.7 / §6 / §7 / §9 完成 Web Shell v2 前端**剩余所有
项**——前端实现**全部完成**，进入「ready for test-coverage-e2e 真链路验证」状态。

详见 [SPEC §5.6/§5.7/§0/§7](../specs/web-shell-v2-spec.md)。前置：[Chunk A 单 store fold]
(./2026-07-08-web-shell-v2-chunk-a-single-store-fold.md) + [B ConversationView](./2026-07-08-web-shell-v2-chunk-b-conversation-view.md)
+ [C Charts/Log/TopBar/Rail/useElapsedTick/Gate/DAG](./2026-07-08-web-shell-v2-chunk-c-charts-log-tb-rail-tick.md)。

## 交付

- **D1 Gate 模态**（§5.6）：Chunk C 已完整接入 GateDialog/PermissionGate/AskGate/
  ResolvedToast（不乐观更新 + POST /gate/respond + resolved 自动关 + toast）。本 chunk
  补一个 e2e 测试断言完整流：requested → modal open → answer → POST body → resolved →
  close + toast（`test/gate.test.tsx::D1 完整 e2e 流程`）。
- **D2 DAG 浮层**（§5.7）：`AgentsRail` `[DAG]` 按钮经 `React.lazy` + `Suspense` 包装
  `WorkflowGraph`——xyflow 全家桶（~217KB）懒挂，首屏不下载。新增 lazy 解析 + 占位文本测试。
- **D3 image URL rewrite**（§0 D10）：
  - **后端** `GET /api/runs/<id>/assets/<path>`（`orca/iface/web/routes/runs.py`）：从
    `<runs_dir>/<run_id>/assets/` 解析，**三重守卫**——未知 run / 路径越界（``.resolve()``
    + ``relative_to``）/ symlink / 文件不存在 → 404 fail loud（不暴露 fs 细节）。
  - **前端** `rewriteImageSrc`（`MarkdownTextImpl.tsx`）：相对 / `file://` / 裸文件名 →
    ``/api/runs/<id>/assets/<encoded>``（``encodeURIComponent``）；绝对 http(s) / data: /
    blob: / 已是 `/api/` 直通。runId 从单一 store 派生（不进 prop drilling）。
  - **RunManager** 新 `runs_dir` property + `resolve_asset_path(run_id, rel_path)` 方法
    （SRP：路径解析在 manager，IO 字节流在 routes）。
- **D4 resume-fallback watchdog**（§0 D6 失败路径）：`use-websocket.ts` 重连发 resume 后
  启 watchdog（3s）；超时未收到任何事件 → 全量 re-fetch + `loadFromEvents` re-fold +
  `onResumeFallback`（dropBuffer）。
  - **协议补丁**（review BLOCKER 闭环）：server `_handle_resume` 重放完毕后发
    `{type:"resume_ok", run_id, last_seq}` ack 帧（不进 tape，控制平面）→ client `onmessage`
    收到即清 watchdog。**仅当 resume 协议真正执行**才发（invalid since / unknown run 等回退
    路径不发）。消除「idle 场景误触发全量重拉」缺陷。
  - dropBuffer 时序断言（先 re-fold 后 dropBuffer，避免旧 buffer frame 闪现）。
- **D5 bundle split**：`ConversationView`（含 react-markdown 全家桶 ~1MB）/ `ChartsView`
  （recharts ~440KB）/ `WorkflowGraph`（xyflow ~217KB）在 `RunDetailPage` 用 `React.lazy`
  + `Suspense` 各拆独立 chunk。**首屏 initial bundle 2,035 KB → 290 KB（86% reduction）**。
  - **设计决策（surface conflicts Rule 7）**：单组件层 lazy（如 MarkdownText 自身）在
    vitest 下需异步断言，且测试需 findByText——chunk 切分点抬到 view 层既得最优切分（整个
    conversation 树不进首屏）又保留组件 sync API（测试不改）。dev/test 模式下 dynamic
    import 由 vitest 处理，prod 模式由 vite 打成独立 chunk。
- **D6 AH theme polish**：`chartTheme.ts` 8-color Nature/IEEE palette 已就位（Chunk C 迁
  移）；状态色（success/amber-thinking/danger/neutral）+ 等宽/prose 排版 + panel borders
  全部到位。
  - **lucide 偏离**（SPEC §7 提及 lucide）：本仓选 **unicode/emoji 图标**（●✓✗⏸💭等），
    rationale：(1) 零依赖（lucide ~50KB）；(2) 跨平台一致显示；(3) 现有图标已充分达意。
    偏离记录于此（非 BLOCKER，SPEC §7 是建议非强约束）。
- **D7 Chunk B 偏离修正**：
  - **StatusLine**（SPEC §5.3）：Chunk B 做成单行不可折叠（YAGNI）→ 修正为可折叠（默认
    折叠 + ▸/▼ chevron + 展开看 data JSON）。`validator_failed` 例外：默认展开（错误高敏感，
    SPEC §5.3 闭 review #29 错误转录精神）。
  - **DiffView**：保留 index-diff（非 LCS）+ 文档化 rationale（`DiffView.tsx` 文件头注释
    已有：避免重型依赖；AH 同款手写 production-proven）。

## 闭环 review（code-reviewer）

1 BLOCKER（D4 watchdog idle 误触发）+ 3 MAJOR（AgentsRail 全 store 订阅 / MarkdownText
渲染层无测试 / file:// 绝对路径语义偏离）+ 4 MINOR（MarkdownText.tsx 注释措辞 / symlink
防御 / agents-rail 测试名 / `code` inline-vs-block 判定）+ 1 NEW（dropBuffer 时序无断言）
全部闭环：

- BLOCKER：协议级 ack 帧（见上 D4）。
- MAJOR1：`AgentsRail` 改细粒度 selector 订阅（`workflowDef + nodes + events`），不再订阅
  整体 state。
- MAJOR2：新增 `test/markdown-text.test.tsx`（4 cases）覆盖组件映射 + img rewrite 集成。
- MAJOR3：`file://` 绝对路径语义——测试断言修正 + release note 显式说明「agent 必须把图片
  写入 run-scoped assets 目录后引用相对路径」（后端 endpoint 不允许越界读 fs）。
- MINOR1：`MarkdownText.tsx` 注释从「直接 import」改为「re-export 入口」。
- MINOR2：`resolve_asset_path` 加 `is_symlink()` 拒绝（防御纵深，含中间段 symlink 兜底
  check）。
- MINOR3：`agents-rail.test.tsx` lazy 测试改为 findByTestId 等待 lazy chunk 解析（更强）。
- MINOR4：`code` inline-vs-block 判定 `pos != null &&` 守卫已存在，未改。
- NEW：新增 `ws-resume-fallback.test.ts::dropBuffer 时序`断言 `loadFromEvents` 先于
  `onResumeFallback`。

## 验证

- **前端**：249 npm tests 全绿（baseline 223 → +26 新增）。`npm run build` 双绿。bundle
  split 实证：initial 290 KB / gzip 93.65 KB（vs baseline 2,035 KB / 646 KB gzip，-86%）。
- **后端**：64 backend tests 全绿（含 8 新增 asset / 2 新增 resume_ok / 1 symlink 防御）。
- **smoke**：`vite preview` 启动成功，`/` 200 OK，index.html + main chunk + lazy chunks
  全部正常 serve。
- **AC grep**：`replayPosition|formatLogLine|RunsSidebar|use-runs-list` 命中 0；Zustand
  store 定义 1 个（`workflow-store`）；events.ts EventType 同步（codegen check 通过）。

## Commit

- `orca/iface/web/routes/runs.py`、`orca/iface/web/run_manager.py`、
  `orca/iface/web/ws_handler.py`：D3 backend + D4 ack 协议补丁。
- `orca/iface/web/frontend/src/components/conversation/MarkdownText.tsx`：re-export 入口。
- `orca/iface/web/frontend/src/components/conversation/MarkdownTextImpl.tsx`（新）：markdown
  impl + `rewriteImageSrc`。
- `orca/iface/web/frontend/src/components/layout/AgentsRail.tsx`：lazy WorkflowGraph + 细粒度
  订阅。
- `orca/iface/web/frontend/src/components/pages/RunDetailPage.tsx`：lazy ConversationView +
  ChartsView。
- `orca/iface/web/frontend/src/components/conversation/StatusLine.tsx`：D7 折叠修正。
- `orca/iface/web/frontend/src/hooks/use-websocket.ts`：D4 watchdog + resume_ok ack。
- `orca/iface/web/frontend/src/vite-env.d.ts`（新）：vite client types。
- 新增测试：`image-rewrite.test.ts` / `ws-resume-fallback.test.ts` / `status-line.test.tsx`
  / `markdown-text.test.tsx` + 增量 `gate.test.tsx` / `agents-rail.test.tsx` /
  `test_routes.py` / `test_ws_resume.py`。

## 残留 follow-up（移交 test-coverage-e2e）

- 🔵 Playwright 逐屏 DOM 视觉断言（真浏览器跑）：折叠展开交互 / ▎ 流式光标 / chart 渲染 /
  gate 模态 / DAG 浮层 / image rewrite 真链路。
- 🔵 agent 真链路把图片写入 `<runs_dir>/<run_id>/assets/` 后引用相对路径——e2e 验证 D3
  端到端。
- 🔵 lucide 图标（SPEC §7 建议）：当前 unicode 偏离已记录，如后续 polish 再议。
- 🔵 ConversationView lazy chunk 1MB+（gzip 355 KB）仍偏大——react-markdown 全家桶占大头；
  未来可考虑 manualChunks 进一步细分（katex / prism 独立 chunk）。

**前端实现 COMPLETE，ready for e2e。**

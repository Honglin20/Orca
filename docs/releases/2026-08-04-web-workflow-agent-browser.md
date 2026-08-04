# Release: Web workflow/agent 文件浏览器（只读）（2026-08-04）

> 经 spec-reviewer 对抗审查的实现计划（conditional-pass，4 blocker 全并入）。
> Commit: `<TBD>`

## 做了什么

新增 Web 纯只读浏览页 `/workflows`：列 workflow → 看它引用的 + 全量 agents → 点 agent 目录文件树 → 看文件内容（md 渲染 + .py prism 高亮）。**不碰编排/执行/状态**，纯增量。

### 后端（`orca/iface/web/routes/workflows.py`，无 manager 薄封装 `orca.compile` loader）
- 5 endpoint：`GET /api/workflows` | `/{name}` | `/{name}/agents` | `/{name}/agents/{agent}/tree` | `/{name}/agents/{agent}/file?path=<rel>`
- referenced agents：`parser._iter_agent_nodes` + `resources_root is not None` 过滤 + `node.agent or node.name`（含 foreach body，旧约定顶层节点兜底）
- `_safe_resolve` 路径守卫：symlink 双查 + `relative_to` 越界 + null byte（抄 `run_manager.py:267-300` 算法）
- file endpoint：1MB cap + 二进制检测 → 422；穿越/不存在 → 404；`/agents` fail-soft 带 `missing` 字段
- 错误 envelope 统一 `{"detail": str}`

### 前端（纯增量：不碰 `main.tsx` / `FileContentView` / 现有 5 route / 现有页面）
- `workflow-browse-store.ts`（plain zustand，不轮询，`inflightSeq` 竞态 gate）
- `WorkflowsPage` + `WorkflowBrowsePage`（React.lazy）+ `FileTree` + `CodeViewer`
- prism CSS 进 browse chunk（**D1：不污染 `/runs/:runId` 代码块配色**，不改 `main.tsx`）
- `.md` 复用 `MarkdownText`；CodeViewer 50KB 阈值回退 plain

## 偏差
- **D1 加码**：为实现 prism CSS 隔离，两个新页面改 `React.lazy`（原计划静态 import）；顺带 `MarkdownText` lazy，首屏减 1MB+。
- **build 产物未含此 commit**：本机 Git Bash / WSL build 受阻（`prebuild` 的 `python` 解析到 Windows Store shim，conda env 未在此 shell 激活）。`static/` 保持 clean；需在有 python+node 的环境 `cd orca/iface/web/frontend && npm run build` 重新生成并另行 commit。

## 验证
- 后端 `pytest tests/iface/web/test_workflows_routes.py`：**19 passed / 1 skipped**（symlink 在 Windows FS 无 admin）
- 前端 vitest：**487 passed**（24 新 + 463 既有零回归）；`npm run build` 成功（test-agent WSL conda 环境）
- **test-agent 真机**：起 uvicorn，5 API + 9 安全边界全过（`../`/绝对路径/null byte `%00`/URL 编码 `%2e%2e%2f`→404；真实 `.pyc`→422 binary；2.1MB fixture→422 too large）；回归后端 203 / 前端 487；现有 `/api/runs` 仍 200
- **纯增量实证**：现有 5 route + 现有页面 + `main.tsx` + `FileContentView` 零改（`git diff` 实证，3 个修改文件纯加法）

## spec-reviewer 闭环（conditional-pass）
4 个实现前 blocker 全并入计划：M1 tree JSON schema / M2 file envelope / M3 agent 提取（foreach + fallback）/ M4 ConfigurationError fail-soft。决策 D1（prism CSS 范围）按用户「纯增量」铁律选 browse chunk 内 import。

## code-reviewer 闭环
3 项应修复全闭环：`openWorkflow` `inflightSeq` 竞态 gate（防 stale 覆盖 fresh）/ `openWorkflow` 同步清 `activeWorkflow`（防 loading 期闪现）/ `workflows.py` docstring 与 fail-soft 现实对齐。

## 已知限制
- 前端浏览器级 DOM 交互冒烟未自动化（环境无 playwright），由前端单测兜底（`code-viewer.test.tsx` 断 prism `class="token"` span 真产出）。
- **主页 `/` 暂无导航入口**到 `/workflows`（需直接访问 URL，或后续在 TopBar 加链接）。
- agent.md 内图片在 browse 页不解析（`MarkdownText` 耦合 `activeRunId`，browse 页为 null；文字/表格/代码块正常，不崩）。

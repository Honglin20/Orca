# 计划：prof-opt 反馈批（web UX + artifacts 根 + rounds 留档）

> 来源：用户 2026-09-07 八条反馈（#3/#8 为疑问已答，其余六条改动）。
> 决策已拍板：#1 仅存 shadow 源码；#7 帕累托图为唯一改进呈现，不加新图。

## 背景（探查确认的事实）

- **#4 404 根因**：prof-opt 的 artifacts 写在 project-scoped `<project_root>/artifacts/prof-opt/`（workflow.yaml 声明，`orca_env.sh` 记录 `ORCA_ARTIFACTS_DIR`），而 web 的 `resolve_artifacts_root`（`orca/iface/web/run_manager.py:303`）只认 per-run `<runs_dir>/<run_id>/artifacts/` → 文档清单可见（走事件流）但点击必 404。
- **#8 R 口径**：`R<n>` = 节点去重 session 数（`selectors.ts:112` sessionCount → Loop 组 iteration）。po_propose 的子代理 session 也计入 → R24 膨胀；PO-GATE 是 script 节点（每执行一个 session）→ R3 = 真实轮数。
- **#6 单图巨大**：`ChartGroup.tsx:29` 网格 `repeat(auto-fit, minmax(300px, 1fr))`，auto-fit 折叠空轨道 + `aspect-[4/3]` → 单图独占整行。
- **#2 重复渲染**：`push_curves.py --docs` 推的清单表（label `prof-opt/docs`）在图表区被当作 table 渲染一大张；同时它**是** ProfOptDocsPanel 的数据源（前端从 chart 事件读清单），**不能停推**，只能不让图表区渲染。
- **#5 现状**：文档面板挂在「图表」页签顶部（`RunDetailPage.tsx:143`），一行一文件。
- **#1 现状**：模型文件只在 `variants/<vid>/shadow/`（variant-implementer 子代理写）；`rounds/<NNN>/` 只有提案/判定文档。`gate_node.sh` 是每轮单次执行的确定性 script 节点（gate 阶段轮内全部 variant 已定型）。
- **#7 现状**：pareto 图（`chart_type="pareto"`，`push_curves.py:234` collect_pareto）x 轴即「相对基线时延降幅 %」，但图中无 baseline 原点参照。

## 改动清单

### A. web 后端（orca/iface/web）

**A1（#4 辅修复，同机场景）artifacts 根解析支持 project-scoped 路径**

> 部署事实（用户 2026-09-07 确认）：serve 与 workflow 常态异机，artifacts 仅远程本地盘。
> **主修复是 C3 内容通道**；本项只修「同机 + project-scoped」场景（如本机 WSL 跑、本机看）。

- `run_manager.resolve_artifacts_root` 解析顺序：
  1. run 目录（tape 父目录）下 `orca_env.sh` 记录的 `ORCA_ARTIFACTS_DIR`——deterministic 文本解析 `export ORCA_ARTIFACTS_DIR=<path>` 行（**不 exec**），路径存在且是目录 → 用它；
  2. 回退现有 `<runs_dir>/<run_id>/artifacts/`（老 run 兼容）；
  3. 都无 → None → 404（语义不变）。
- 契约保持：artifacts 根绝不泄露进响应文本（既有测试断言）。
- 测试（`tests/test_web_artifacts_docs.py` 追加）：env 指向 project-scoped 目录可读 / 指向不存在路径回退 per-run / 两处皆无 404 / 路径不泄露。

### B. web 前端（frontend/src）

**B1（#8）R 徽标改数「节点自身执行次数」**

- `workflow-store.ts`：`node_started` handler 维护每节点 `execSessions`（distinct session_id，Set 语义天然幂等，refold 安全）。
- `selectors.ts`：Loop 组 `iteration = execSessions.length`（>0 时）；无记录回退现派生（防御）。
- 效果：po_propose 显示 R3；子代理数量仍走「N subs」折叠（口径不动）。
- 测试（vitest）：多轮 + 多子代理 session → R = 执行次数而非 session 总数。

**B2（#2）图表区不再渲染 docs 清单表**

- `ChartsView` 分组处过滤 label `prof-opt/docs`（唯一消费者是文档面板，同一事件源）。注释写明：推送不停（面板数据源），仅不进图表流。
- 测试：过滤后不出现在图表分组、文档面板仍可取。

**B3（#6）单图尺寸自适应**

- `ChartGroup`：当 visuals（非 table）仅 1 时给该图容器 `maxWidth: 560px`（JS 判定，不用 CSS 伪类 hack）；table 仍整行。
- 测试：单图有 max-width / 多图无。

**B4（#5 + #4 主修复）文档独立页签 + 图标卡片网格 + 内容走事件通道**

- `RunDetailPage`：新增「文档」页签，`ProfOptDocsPanel` 独占（移出图表页签）。
- `ProfOptDocsPanel` 重构：按组（基线/变体/轮次/规则）分区，文件以**图标卡片网格**呈现（文件类型图标 + 名称 + 状态点）；点卡片 → 正文预览。
- **内容来源（异机主修复）**：清单行带 `content` 时直接渲染（零请求，跨机可见——与 workflows md 同等体验）；行无 `content`（老 run）→ 回退现有 artifacts fetch（同机可用，404 提示保留）。
- 既有 data-testid 尽量保留；引用行式 DOM 的测试同步更新。

### C. prof-opt workflow（workflows/prof-opt）

**C1（#1）每轮 shadow 源码留档**

- 新脚本 `_po_scripts/archive_round_shadow.py`：幂等 copytree `variants/<vid>/shadow` → `rounds/<NNN>/<vid>/shadow`（**仅 shadow 源码**，不含 onnx/profile；已存在跳过）。
- 挂点：`gate_node.sh` deploy --verify 之后、promote 之前。
- 失败路径（fail loud 不骑劫主流程）：copy 失败 → stderr + `rounds/<NNN>/shadow_archive_error.json` 落盘，gate 决策继续。
- vid→round 归属复用 `round_state.py` / `history_lib.py` 现成函数，不重复造。
- 测试：多 variant / 重复执行幂等 / 源缺失落 error。

**C2（#7）帕累托图补 baseline 参照点**

- 不新增图表/报告节（拍板）。`push_curves.collect_pareto` 追加 baseline 锚点行（x=0，y=基线 gap，独立颜色）——「相对基线」有原点可对；实现时若确认已有则跳过并在此标注。

**C3（#4 主修复，异机）文档内容随清单走事件通道**

- `push_curves.py --docs`：清单行追加 `content` 列——每份白名单文档的纯文本内容（md/json）。
- 体积控制（tape 事件膨胀防线）：状态文件 `$ART/.docs_push_state.json` 记录各文档 mtime+size，**只对新增/变更文档带 content**，未变文档只推路径（前端保留事件历史中已推内容）；单文档内容 > 256KB → 不带 content、行标 `content_omitted=true`（fail loud，面板明示「内容过大未推送」）。
- 契约：不改 chart payload 结构校验（content 作为 data 行的普通列值通过既有 table 通道；B2 已保证该表不进图表渲染流）。
- 测试：push_curves 单测（首推全量带 content / 二推未变不带 / 变更后重推 / 超限标注）。

## 不做

- 不改 `push_curves` 的 docs 推送行为（面板数据源）。
- #7 不加柱状图/报告节（用户拍板：以帕累托图为唯一呈现，避免多图混乱）。
- AgentsRail「N subs」口径（own session 也被计入 subs）不动——独立议题，本次只修 R。

## 验证

- 后端 + workflow 脚本：WSL `.venv` pytest（win32 走 wsl）。
- 前端：`frontend/` vitest run + `tsc --noEmit`。
- 真机 E2E（按需，用户环境）：in-session 短跑验收文档页签 + rounds 留档。

## 风险

- 老 run 无 `orca_env.sh` → 回退现行为，兼容。
- 文档面板 DOM 契约变化 → 测试同步，不静默。

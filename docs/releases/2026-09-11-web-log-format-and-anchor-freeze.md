# 2026-09-11 Web Log 可读性 + prof-opt origin-anchor 冻结确定性 + 帕累托图前沿/灰

三个独立修复（用户实测反馈驱动）。

## 1. Web：Log 面板自动换行 + 时长分钟化

用户反馈：右侧 Log 长行被裁剪看不全；时长显示裸秒（`1234.567s`）位数过多。

- `LogStream.tsx`：行样式 `whitespace-nowrap` → `whitespace-pre-wrap` + `[word-break:break-word]`；固定 `rowHeight=28` → `useDynamicRowHeight({defaultRowHeight: 24})`（react-window 2.2.7 原生动态测量）。虚拟滚动 / pinned / 「跳最新」状态机不变。
- `selectors.ts` 新增 `formatDuration()`：≥60s → `X.XXmin`（两位小数）；<60s → 整秒；<1s → `<1s`。取整越界（59.6 → "60s" 撒谎）落分钟档。`summarizeEvent` 4 处裸秒拼接（workflow_completed / node_completed / wait_started / wait_completed）统一改走它。
- `StatusLine.tsx` wait 两行同口径（`formatSeconds` 适配本地 `num()` 的 `number|string` 回退）。
- static 已重建（含 `tsc --noEmit`）；`selectors.test.ts` 更新钉死断言 + 新增 formatDuration 边界用例；前端 vitest 651 green。

## 2. prof-opt：origin-anchor 冻结从 model-mediated 改 deterministic

用户实测（round 1）：`po_propose` step0 `frontier_snapshot.py` 报 origin anchor missing——病灶是 baseline 没写 `base/origin_anchor.json`，但失败隔了整整一个节点才爆。

根因链：anchor 冻结只靠 po_baseline 的 LLM agent 按 prompt 记得调 `freeze_origin.sh`（model-mediated，违反 deterministic 原则）；`freeze_origin.sh` 在 profiling 产物缺失时静默 `SystemExit(0)`；`executed` gate（check_baseline_docs.sh）不查 anchor。agent 某轮漏跑/早跑 → `executed` 照常发出 → 下游第一个硬读者才 fail loud。

修复（两道，机制性）：

- `run_baseline_chain.sh`：新增必填 `--accuracy-budget`（无默认值可猜）；step 3（mfu report 校验通过）后 chain **确定性内联调用** freeze_origin.sh（step 3b；失败 → `baseline step 3` fail loud；既有 anchor 内容漂移 → 同路径拒绝）；finalizer `--finalizer` 重入透传该参数；`generated_artifacts` 探针含 `base/origin_anchor.json`。契约升级为 **`executed` ⇒ anchor 必在**。step 2 在 3b 之前已强制 canonical schedule_result 恰一且合法，freeze 的静默 exit 0 从 chain 调用点不可达。
- `check_baseline_docs.sh`：executed gate 增加第 4 项——`base/origin_anchor.json` 存在 + schema 校验（makespan/target 非负 int、budget 数值）。
- `agent.md`：Step 1 调用加 `--accuracy-budget`；移除手动 freeze 指令段（"chain 自己冻结，你不得手跑/手改 anchor"）；标题措辞同步。
- 测试：`test_baseline_chain_executed_guarantees_origin_anchor`（executed ⇒ anchor 内容 = canonical makespan + 冻结 target + budget）、`test_baseline_chain_freeze_drift_fails_loud_at_step_3`（漂移 anchor → step 3 fail loud 且不静默改写）、`test_baseline_chain_requires_accuracy_budget_arg`；全部直接调用点补参。**顺手修活 CURRENT.md 挂账的 HEAD 既有失败 ×2**：`_baseline_ws` 的 mfu report 改为永远存在（它是 early-chain 前置，不是 `with_docs` 可选项）；`running_until_*` 测试改两文档语义；device parks 测试补写 report。

## 3. prof-opt：帕累托图从状态配色改为前沿高亮/灰 + round 标注

用户反馈：所有点标红（其变体全为 accuracy_fail → §10.2 状态色 `#ef4444`），且只想看前沿解。拍板：**前沿点高亮、非前沿灰、点旁标注第几轮**。

- `push_curves.py` `collect_pareto`：行停发 per-row `color`（`_STATUS_COLORS`/`_NEUTRAL_COLOR`/`_BASELINE_COLOR` 三常量删除），改带 `round`——新 `_vid_rounds` 从 `history.jsonl` 最新行解析 vid→round（audit base 是唯一轮号真相，不解析 vid 字符串；撕裂 history → stderr 注记降级，sidecar fail-soft 契约不变）；baseline 锚点行 `round=0`。payload `color` 键清空 → 前端切前沿/被支配双模式（几何判定在绘图点上，accuracy_fail 也可在前沿上——caption 已披露）；caption 补 front/gray + tooltip 语义。
- `ParetoChartWidget.tsx`：三处 Scatter 加 `LabelList dataKey="round"`（`roundLabel` 无 round 不落字）；tooltip 改自定义（vid/round/status/x/y，字段缺席不渲染）；**`prepareParetoPoints` 保留原行全部字段**（review 抓到的 Critical：原先投影成裸 `{x,y}` 会让标签和 tooltip 静默失效）。
- `gen-profopt-fixtures.py`：修复陈旧种子（variant docs 用旧文档名 → 现行 `_VARIANT_DOC_FILES`；`latency_pass` 假字面量 → 真实 outcome；补 `history.jsonl` 种子供 round 解析），重新生成 fixture——顺带追上 08-31 以来 C3 content 通道的积欠漂移（旧 fixture 掩盖了 docs 行形态演进）。
- 测试：python pareto payload 用例按新契约重钉（无 color、round last-wins、baseline round=0）；W3 集成用例更新 + 新增 round/tooltip 数据面接线纯函数验证（happy-dom 不渲染散点逐点内容，纯函数级钉住投影回归）。

## 验证

- po 全套 277 tests green；`tars validate` 零 warning
- 前端 vitest 653 green；`npm run build`（tsc + vite）通过，static 重建
- code-reviewer 自检 ×2：第一轮 0 MAJOR / 6 minor 全修；第二轮抓 1 Critical（points 投影丢失原行字段 → 标签/tooltip 静默失效，已修 + 回归钉）+ 4 minor（1 修：tooltip 失效 props 删除；2 项带理由不修：撕裂行全有全无降级符合 sidecar 契约、`_vid_rounds` 自带类型守卫不适配 `read_latest`）

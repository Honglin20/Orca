# 2026-09-11 Web Log 可读性 + prof-opt origin-anchor 冻结确定性

两个独立修复（用户实测反馈驱动）。

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

## 验证

- po 全套 180 tests green（21 chain/gate/freeze + 158 v5-v8/prompt + 1 新增门）；`tars validate` 零 warning
- 前端 vitest 651 green；`npm run build`（tsc + vite）通过，static 重建
- code-reviewer 自检 0 MAJOR / 6 minor 全修

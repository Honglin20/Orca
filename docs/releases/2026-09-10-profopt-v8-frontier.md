# prof-opt v8 —— 同基前沿探索（永不晋升 + frontier 快照）

日期：2026-09-10 ｜ 草稿：`docs/specs/prof-opt-v8-design-draft.md` ｜ SPEC：`docs/specs/prof-opt-v8-spec.md`

## 动机

v7 实跑暴露两个缺口：①变体训练在飞期间盘面上**没有任何精度信号**（history 只有
`latency_improved` 过程行，后续轮 propose 看到的全是正面时延证据）；②`accuracy_fail`
**从不进 failed_sigs**（direction.json 只在时延 5 轮修复耗尽路径写）——避坑完全靠
LLM 自觉。同时训练已流水线化（多轮在飞），链式晋升"各代换树"使跨变体比较打折。

## 拍板（用户 2026-09-10）

1. **永不晋升**：base 树恒冻结 origin，退役树换机制；
2. **被前沿支配的变体照旧放行训练**（advisory 不设门）；
3. **历史管理 = append-only 机械层 + 派生快照读路径**，不做总结文本（避免第二真相源）。

## 改动

- **新脚本 `frontier_snapshot.py`**：从 history 终态行 + `variants/<vid>/train_status.json`
  （watchdog 每 epoch 原子重写——实时精度对比数据本来就在盘上，v8 只补了读端）
  机械派生 `base/frontier.json` 三段：`frontier`（success 行内非支配集，makespan+gap
  双轴，一严格才支配）/ `in_flight`（与 gate 同谓词的未终态 vid，risk 机械分级
  pending_launch / on_track / at_risk / terminating）/ `avoid`（三种非 success 终态）。
  po_propose Step 0 与 po_gate 各现算一次；撕裂盘面 fail loud。
- **永不晋升落地**：删 `promote_incumbent.py`；`gate_node.sh` 去晋升块改刷 frontier
  快照；`gate_decide.py` 去 `incumbent_promoted`；`history_lib.expected_base()` 恒返回
  origin 锚、残留 `incumbent.json` fail loud；`check_verdict.py` 准入线恒 = 冻结
  origin 线（输出键 `admission_line_makespan_cycles`）；`run_latency_recheck.sh`
  文案/ledger 字段同步。
- **谱系改组合**：`parent_vid` / `base_at_proposal` 退役，proposals/history 行增
  `absorbs`（吸收了前沿哪些 vid 的机制；写层机械校验引用存在、不得吸收自身）；
  selector 必须在 `architecture_decision.md` 写 `## absorbs` / `## avoids` 两节，
  emit 门校验 vid 引用。`direction.json` / `failed_sigs` 退役——avoid 机械派生取代。
- **上下文三件套（有界）**：候选简报 = frontier.json 全文 + 上轮 analysis.md
  （~15 行帽）+ accuracy rules；移除 raw history 累积与全部前序 variant MFU 报告。
- **兼容**：`BASELINE.lock` schema 2→3，旧工作区在入口 reuse gate exit 3 提示
  `fresh_start=true`（顺带根除"晋升不重锚"挂账坑）。

## 验证

- `tars validate` 通过、warning 清零；
- pytest：test_po_v6/v7/scripts/prompt_scripts/diff_check/inject/v5 + 新增
  test_po_v8_frontier（10 用例）共 **289 green**；2 个 `test_baseline_chain_*` 为
  HEAD 既有失败（见 CURRENT.md 挂账），未恶化；
- 测试抓到并修复真 bug：gate_node.sh 重构后 `rc` 在 `set -u` 下未初始化。

## 影响

- E2E 行为变化：round N 的 propose 能看到 round N-1 在飞训练的实时 epoch/metric/gap
  （此前完全失明）；accuracy_fail 自动进下轮 avoid 清单（此前无机械换路）。
- 旧工作区不可续跑（锁版本门），需 `fresh_start=true`。

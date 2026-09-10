# prof-opt v8.1 — po_propose 叙事层 history digest（2026-09-10）

## 问题

po_propose 每轮的上下文里，叙事输入只有**上一轮**的 `rounds/<NNN>/analysis.md`——第 N 轮看不到 1..N-2 轮"为什么失败"；机械层的 `avoid` 清单只有 `vid + change_sig + outcome`，不带因果。轮数一多，提案层就是"全量数字 + 一轮叙事"的近视状态。本轮上下文来源四处（baseline 三文档 / frontier.json / 上轮 analysis.md / rules），出处各异、职责不清。

## 设计（接口讨论确认，2026-09-10）

**分层不换手**：数字层保持机械（frontier_snapshot 从 history.jsonl 派生），叙事层交给一个专职 curator 子代理——"propose 只看总结"被修正为"propose 的**叙事输入**从上轮 analysis.md 换成全量 digest"。digest 是**带机械戳的派生缓存**：history.jsonl 仍是唯一真相，数字一律不复写（读者另行直读 frontier.json）。

落点：不新增 DAG 节点（环路径保持 propose→probe→gate 全机械闸门），po_propose Step 4 用 task 派 `history-curator` 子代理——受众翻转（digest 的读者是下一轮 proposer，不是本轮记录），Step 4 上下文已重不宜顺手写总结，"作者总结自己"会自证。

## 改动

- **`subagents/history-curator.md`（新增）**：digest 契约——`## Global lessons`（跨轮模式）+ 每个已关轮一个 `## Round r<R>` 块（近 3 轮 verbose、更早压一行，只写因果）+ `## Accuracy rules` 指针；~200 行预算；零实测数字（要数字就指 frontier.json）；只写 `<doc_path>`。
- **`_po_scripts/digest_stamp.py`（新增）**：戳边界，LLM 零转手——`stamp` 机械校验 curator 契约（sentinel 首行 / Global lessons 标题 / **每个已关轮一个轮块**，`r1(?!\d)` 边界防 r10 冒充 r1）+ 字节帽（默认 24KB），从盘面推导 seal（covered_round = current_round，sha256+bytes）；`check` 按 round 三态语义判新鲜（R* resumable → 需覆盖 R*；torn → R*-1，torn 中断轮的叙事未闭合、上轮戳是诚实状态；无行 → 合法 no-op）；`problems_for_emit` 供 emit gate——收尾轮必须自带本轮戳。
- **`po_propose/agent.md`**：不变式加 digest 派生缓存条款；Step 0 加 `digest_stamp check`（stale fail loud）；Step 1 brief 叙事源换 `base/history_digest.md`（轮 1 缺席合法）；Step 4 派 curator + 戳（BOTH ending paths，先于 emit gate）。
- **`check_propose_emit.py`**：gate item 7——digest seal 纳入磁盘契约，缺失/篡改/未覆盖本轮 → 拒 emit。
- **`push_curves.py`**：docs manifest 加 `_DIGEST_ROW`，digest 演化随每轮上 web 面板。
- **测试**：`tests/test_po_history_digest.py` 15 用例（戳契约矩阵 / 三态 round 语义 / 篡改 / 字节帽 / CLI exit-2 / 逐轮块覆盖）；`test_po_v6.py` `_emit_ws` 补 digest+戳并同步部署列表，新增 emit-gate 两条负路径。

## 验证

- po 全套件：127 + 169 + 152 passed，**仅剩 `test_baseline_chain_*` ×2 = CURRENT.md 挂账的 HEAD 既有失败**（未恶化，本次未触碰 run_baseline_chain.sh）
- `tars validate` 零 warning；`check_dev_residue` / `check_agent_md_static` 双 rc=0
- 独立 code-reviewer（洁净受众翻转通读 + 代码质量）PASS_WITH_FINDINGS：1 MAJOR（截断叙事可密封——逐轮块机械校验收口）+ 3 minor 全修，复审绿

## 边界

- v8 轮环实战 E2E 仍待有卡宿主（CURRENT.md 挂账），digest 链路随车同验
- curator 失败语义 = fail loud（子代理重试一次后 → propose failed → po_report 收割），与 Step 4 其他产物同待遇

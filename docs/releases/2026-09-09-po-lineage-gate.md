# Release Note：prof-opt 谱系闸 —— parent 必须过 accuracy 门 + 时延优化

日期：2026-09-09 ｜ 类型：fix(prof-opt) ｜ 触发：用户远程实测发现谱系叠加（r01-01 accuracy_fail ~10dB，r02/r03 仍以其为 parent 连败）

## 根因

精度闸/晋升层无恙（三变体全 accuracy_fail、incumbent 从未推进）。洞在谱系写入层：`append_impl_row` 对 `parent_vid` 零校验；`check_propose_emit` 的 parent==incumbent 校验是 agent 指令（LLM 可无视）；prompt 从未声明 accuracy_fail 变体不可为 parent。

## 修复（确定性优先，LLM 无法绕过）

1. **`history_lib.expected_base(art)`**：合法谱系锚唯一真相源——incumbent 优先，撕裂 fail loud（不静默降级回 origin 锚）。
2. **`append_impl_row.py` 写层闸**：`parent_vid`/`base_at_proposal` 与 expected_base 逐字段比对，violation rc 2，任何不在 {incumbent, origin} 的 parent（含 accuracy_fail 死端）在唯一写入口被拒，零落盘。
3. **`check_propose_emit.py`**：复用 expected_base；谱系失败信息明确「非 selector-repairable，须在 incumbent 基上重导设计」。
4. **prompt 硬化 6 文件**：po_propose/agent.md（Invariants + Step 2 谱系失败语义）、architecture-selector、variant-implementer（永远只从当前 `shadow/` 起步）、三个 candidate proposer（死端规则；idea 在 incumbent 树上重导合法，叠加非法）。不含任何任务相关阈值（accuracy_budget 属 run input 语义）。

## 验证

- 新增 4 用例全过（incumbent 放行 / 死端 parent 拒绝且零落盘 / 陈旧 base 指针拒绝 / expected_base 撕裂 fail loud）。
- `test_po_scripts + test_po_v7 + test_po_v6`：209 passed；4 个失败经干净 worktree 对照实证为存量（gate_node 引用串 / v7 prompt 钉住的已删 references 路径 / baseline_chain×2），与本修复无关。
- `tars validate workflows/prof-opt/workflow.yaml` 通过，洁净 warning 零。

## 语义确认

parent 资格 = 过 accuracy 门 + 时延有优化，机制等价 `parent_vid ∈ {None, 当前 incumbent.vid}`。accuracy_budget（用户任务 0.1dB）为 run input，不在本修复范围。

计划：[docs/plans/2026-09-09-po-lineage-gate.md](../plans/2026-09-09-po-lineage-gate.md)

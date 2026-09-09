# 计划：prof-opt 谱系闸 —— parent 资格机械强制（accuracy 门 + 时延优化）

日期：2026-09-09 ｜ 状态：方案已与用户对齐（"修复一下"）｜ 类型：bug fix

## 根因（用户远程定位 + 代码审计双确认）

远程 run 实测：r01-01 accuracy_fail（~10dB），r02-01/r03-01 仍以其为
parent 叠加修改，三连败。精度闸/晋升层无恙（全部 accuracy_fail、
incumbent 未推进），洞在**谱系写入层**：

1. `history_lib.append_implemented` 对 `parent_vid` 零校验——propose 节点
   写什么就是什么，watchdog 终态行继承；
2. `check_propose_emit.py:260` 虽有 parent==incumbent 校验，但它是 agent.md
   里的**指令**（LLM 自觉执行），非 harness 强制——LLM 无视 rc=1 即穿透；
3. agent.md/子代理从未写明「accuracy_fail 变体不可为 parent」，子代理见
   latency_improved 便当作有效基。

## 语义（用户拍板）

parent 资格 = **过 accuracy 门 + 时延有优化**，否则不得进入谱系。
机制等价 `parent_vid ∈ {None, 当前 incumbent.vid}`（只有 promote_incumbent
写 incumbent.json，其候选集 = success + makespan 更优）。
accuracy_budget（如用户任务的 0.1dB）是 run input 语义，**不进 prompt**。

## 改动清单（确定性优先）

1. `history_lib.expected_base(art)`：合法锚唯一真相源（incumbent 优先，
   撕裂 fail loud，不静默降级回锚点）。
2. `append_impl_row.py` 写层闸：parent_vid/base_at_proposal 与 expected_base
   逐字段比对，violation rc 2，拒绝信息写明死端规则。
3. `check_propose_emit.py` 复用 expected_base；谱系失败信息写明
   「非 selector-repairable，须在 incumbent 基上重导设计」。
4. prompt 硬化 6 文件：po_propose/agent.md（Invariants + Step 2）、
   architecture-selector、variant-implementer（只从当前 shadow 起步）、
   三个 candidate proposer（死端规则 + idea reuse 合法）。
5. 测试 4 例：incumbent parent 放行 / accuracy_fail parent 拒绝且零落盘 /
   base 指针陈旧拒绝 / expected_base 撕裂 fail loud。

## 不做

- accuracy_budget 语义/单位：run input 层事务，不动。
- gate_node.sh / promote_incumbent.py：已正确，零改动。

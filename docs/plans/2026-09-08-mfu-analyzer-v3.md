# plan: mfu-analyzer v3（bound 判定 + MFU 主位）+ 昇腾知识单文件共享

## 背景

真机实测暴露 mfu-analyzer v2 误判：把「cycles 最大的 MATMUL」判成计算瓶颈，而真实
根因是 reduce/transpose/img2col 与 matmul 交替导致的格式/布局切换税（TransData）。
三个结构性缺陷：① 根因词汇表（4 类）缺「格式转换税」；② 无序列级分析步骤，全部
per-operator 聚合指标；③ analyzer 拿不到昇腾硬件知识（ascend.md 只喂 proposer）。
另：MFU 提升本身是一级目标（时延 = 计算量 ÷ (MFU × 峰值)），v2 只以时延为主位。

用户拍板：昇腾知识**只要一个文件**（ascend.md），deprecated 的 9 铁律压缩并入，
proposer 与 mfu-analyzer 共用；不新建 ascend_constraints。

## 设计决策

- **D1 共享位置**：`workflows/prof-opt/subagents/references/ascend.md`。
  依据：`{{ subagents_root }}` 是 render 层顶层变量（validator.py:693），po_baseline /
  po_propose 都能引用；subagent 校验只 glob 顶层 `*.md`（validator.py:1257），子目录
  不受影响。`ORCA_AGENT_RESOURCES` 是 per-folder-agent 的，无法跨节点共享——弃。
- **D2 v3 诊断框架**：时间轴按 op 分类表切 **cube / vector / 搬运重排** 三类窗口 →
  **bound 判定矩阵**（cube 满载健康 / feed-bound / vector-bound / memory-movement-bound
  / serialization-bound）+ **MFU 损耗分解**（模型 MFU = cube 窗口平均利用率 × cube 窗口
  占比，两因子各映射一类根因）。判定看「耗时算子的 MFU 状态 + 硬件语义」，不看单算子
  cycles 排名。
- **D3 哨兵**：`[subagent:mfu-analyzer v2 MBA7K2]` → `v3`（哨兵码不变，沿用 v7 惯例），
  全部锁死点同步。
- **D4 知识文件三节**：§1 硬件事实（9 铁律压缩，每条 = 事实/诊断含义/结构含义/边界，
  带 fail-loud 前提：6613/1951 型号差异、算子形状推断、以实测为准）；§2 op 分类表
  （bound 判定依据）；§3 设计倾向（原 Prefer/Be cautious/checklist 保留，proposer 用）。
- **D5 analyzer 不开结构药方**（不变）：ascend.md 给它的是归因语义，不是药方。

## 改动清单

1. 新建 `workflows/prof-opt/subagents/references/ascend.md`（D4；源 = 现役 35 行薄版 +
   deprecated `agent-struct-exploration/knowledge_base/common/ascend_constraints.md` 9 铁律压缩）
2. 重写 `workflows/prof-opt/subagents/mfu-analyzer.md` → v3（D2；Inputs 增 `<hardware_ref>`
   必读；报告模板增「MFU 损耗分解」段；证据表「是否瓶颈」→「归因」；词汇表 4→5 类
   （+格式/布局转换税）；哨兵 v3）
3. `agents/po_propose/agent.md`：:50 引用改 `{{ subagents_root }}/references/ascend.md`；
   Step3 mfu-analyzer 派发补传 `<hardware_ref>`
4. 删除 `agents/po_propose/references/hardware/ascend.md`（单文件化）
5. `agents/po_baseline/agent.md`：mfu-analyzer Task 模板（:113）补 `<hardware_ref>`
6. `agents/po_baseline/scripts/run_baseline_chain.sh:354` 哨兵 v3
7. `agents/po_baseline/scripts/check_baseline_docs.sh`：MFU_SENTINEL v3 + 节清单加
   `### MFU 损耗分解` + 头注释 v3
8. `agents/po_propose/references/structural-levers.md:12-14` 词汇表同步五类
9. `tests/test_po_scripts.py`（_MFU_MD v3 + 补节；两处内联哨兵；:1646 stale replace
   改 v3→v1）+ `tests/test_po_v6.py:62`
10. `docs/specs/prof-opt-v7-spec.md` §3.4 追加 v3 修订记录 + :162 哨兵
11. 状态：release note + CHANGELOG（commit 后回填 SHA）；CURRENT.md 不动（正被
    windows-native-compat 任务占用）

## 验证口径（用户拍板：不跑测试）

- `tars validate`-equivalent 静态自检由洁净审查 agent 承担（受众翻转通读 + 洁净契约）
- 已部署环境需 `tars install` 刷新（运行期动作，记入 release note 不执行）
- 哨兵锁定点（6/7/9）全部同步即视为一致；不执行 pytest

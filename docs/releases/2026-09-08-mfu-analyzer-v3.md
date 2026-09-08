# prof-opt: mfu-analyzer v3 —— bound 判定矩阵 + 昇腾知识单文件共享

## 背景

真机实测暴露 v2 误判：报告把「cycles 最大的 MATMUL」判成计算瓶颈，而真实根因是
reduce/transpose/img2col 与 matmul 交替导致的格式/布局切换（TransData 税）——
时延增长来自算子间的数据格式切换，不是任何单个算子。三个结构性缺陷：

1. 根因词汇表（4 类）没有「格式转换税」这一类，模型报不出它没有名字的模式；
2. 全部指引是 per-operator 聚合指标，没有序列级分析步骤；
3. analyzer 拿不到昇腾硬件知识（ascend.md 只喂 proposer 做事前先验）。

另：MFU 提升本身是一级目标（时延 = 计算量 ÷ (MFU × 峰值算力)），v2 只以时延为主位。

## 改动

- **共享硬件知识单文件化**：新建 `workflows/prof-opt/subagents/references/ascend.md`
  （昇腾 9 条硬件铁律压缩 + op 三分类表 + proposer 设计倾向，三节双受众），po_propose
  与 mfu-analyzer 经 `{{ subagents_root }}/references/ascend.md` 共用；删除
  `agents/po_propose/references/hardware/ascend.md`（35 行薄版）。依据：
  `{{ subagents_root }}` 是 render 层顶层变量，两节点都能引用；subagent 校验只 glob
  `subagents/*.md` 顶层，子目录 references/ 不受影响。
- **mfu-analyzer v3**（哨兵 `[subagent:mfu-analyzer v3 MBA7K2]`，码不变）：
  - Inputs 增 `<hardware_ref>`（必读）；阶段 0 先读硬件参考。
  - 诊断框架 = **序列级 bound 判定**：时间轴按 op 分类表切 cube / vector / 搬运重排
    三窗口 → bound 判定矩阵（cube满载 / feed-bound / vector-bound /
    memory-movement-bound / serialization-bound）。判定看「耗时算子的 MFU 状态 +
    硬件语义」，禁止由单算子 cycles 排名直接得出（H2 扩充）。
  - **MFU 主位**：报告新增「MFU 损耗分解」段——模型级 MFU = cube 窗口平均利用率 ×
    cube 窗口占比，两因子各映射一类损耗；根因段每条标注 bound 类型 +「影响 MFU 的
    因素」+「阻碍时延的因素」。
  - MFU 口径硬规则：MFU 列只对 cube 类算子有诊断意义；vector 类用时间占比度量，
    搬运重排类 MFU≈0/>100% 均无信息量，只按成本计价。
  - 根因词汇表 4→5 类（+格式/布局转换税）；证据表「是否瓶颈」列改「归因」语义。
- **派发方同步**：po_baseline 与 po_propose 两处 mfu-analyzer 派发均补传
  `<hardware_ref>`；po_propose 变体派发补注 chip/precision/core_num 取自
  contracts.json 的 profile block。
- **哨兵锁定点全量同步**：`run_baseline_chain.sh` / `check_baseline_docs.sh`（节清单
  增 `### MFU 损耗分解`）/ `tests/test_po_scripts.py`（含 stale-replace 方向改 v3→v1）/
  `tests/test_po_v6.py` / spec §3.4。
- **词汇一致性**：`structural-levers.md` 根因词汇表同步五类；bound 标签统一连字形。
- 顺手收口（洁净审查 F4，先前遗留）：`check_flatten.sh` 头注释删除已不存在的
  analyze/mfu_adapter 两项。

## 洁净审查

code-reviewer 实测：五组洁净禁词零命中；哨兵全仓扫描零 v2 残留；WSL 实测校验门
三用例（v3 全节 exit 0 / stale v2 拒绝 / 缺新节 exit 1）；4 条 MINOR finding 全部
修复。`verify/cleanliness/` 三份快照重写至 v3 现状。

## 验证

- `bash -n` 三个脚本通过；洁净审查 agent 实测校验门三用例通过（未跑 pytest，按用户口径）。
- **已部署环境需 `tars install` 刷新**（subagents/references/ 为新增部署内容）。

## 计划

`docs/plans/2026-09-08-mfu-analyzer-v3.md`

# prof-opt architecture-first propose 设计草稿

日期：2026-09-03  
状态：用户确认，进入实现

## 目标

将 prof-opt 的 propose 从“单个局部 lever 候选”改为“多种宏观结构候选 → 多视角融合收敛 → 一个可实施架构 → mfu 实测与训练反馈”的闭环。最终时延线仍由原始 baseline 的 `origin_anchor.json.target_cycles` 定义，但中间阶段只要求相对当前 incumbent 有真实时延改善。

## 核心决策

1. 每轮并行派发三个架构候选 sub-agent：业务语义、硬件映射、SOTA 结构。
2. 再派发一个 architecture-selector，读取三个候选、当前源码、MFU 报告和历史规则，融合为一个架构决策；只有 selector 的结果进入 implementer。
3. 单独的激活替换、Norm 删除、Transpose 消除、简单 block pruning 不能作为最终架构候选；只能作为宏观结构的一部分或被明确证明是唯一合理方向。
4. 新增 Ascend 事前硬件 reference：作为设计先验，不是硬门；`mfu-analyzer` 负责事后验证真实收益和瓶颈迁移。
5. latency admission 改为“比当前 incumbent 更快”；是否达到原始 target 只作为报告字段，不再阻止训练。
6. 精度通过且时延改善的变体可晋升为 incumbent；origin anchor 永不改变。下一轮以 incumbent 的源码和测量结果为基础。
7. 第一版暂时移除 `op_delta` 硬契约。保留 `change_spec`、`edited_files`、`change_sig`、`parent_vid` 和源码快照，后续从父子源码版本生成事实 diff。

## 数据流

```text
business_logic + information_analysis + incumbent source
        ├─ semantic-architecture-proposer
        ├─ hardware-architecture-proposer + ascend reference
        └─ sota-architecture-proposer
                     ↓
              architecture-selector
                     ↓
       one architecture_decision.md + proposals.json
                     ↓
              variant-implementer
                     ↓
               mfu-analyzer report
                     ↓
      improved vs incumbent? ── no → discard / next round
                     │ yes
                     ↓
              po_probe full training
                     ↓
      accuracy pass + improved → promote incumbent
      accuracy pass + target met → final success
      accuracy fail → accuracy rule, no promotion
```

## 暂不做

- 不做端到端测试；本次只做静态检查和契约级单测更新。
- 不实现复杂的多候选 Pareto 排序；selector 负责一次融合判断。
- 不恢复 ONNX `op_delta` 的替代 gating；graph diff 暂时不作为准入条件。

## 验收标准

- 每轮候选文档和 selector 决策文档落盘，Web 可直接读取。
- 只有 selector 融合后的一个架构进入 implementer/mfu-analyzer。
- 变体仅需严格优于当前 incumbent 才进入 probe；未达到最终 target 仍可训练。
- 精度通过的改进变体晋升并成为下一轮父版本；原始 target 不漂移。
- active propose/probe/gate 链不再以 `op_delta` 作为硬失败原因。

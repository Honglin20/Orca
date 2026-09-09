# 2026-09-09 prof-opt：depth 冻结——提案面全线禁改深度

## 背景

用户观察：propose 节点有时会提出改 depth 的变体（增删/合并 block 换时延）。这与 workflow 定位冲突——本项目寻求**结构突破**（布线/算子组织/信息流），不是 depth 这类超参寻优。depth 变更在 lever 目录里有现成入口：`structural-levers.md` 的 Lever 4（D1 deeper-narrower / D2 shallower-wider capacity redistribution）。

## 改动

- **`structural-levers.md`**：新增「Depth is frozen」硬范围条款；D1/D2 整节退役，索引表同步；Lever 4/5 顺位重排（F1/F2/S1/S2 条目 ID 不变）。豁免线写明：可证冗余的微模块移除（C2 算子对、N2/N3 冗余 norm）不属于 depth 变更。
- **提案面五点全链钉规则**（统一短语 `Depth is frozen`）：
  - `po_propose/agent.md`：不变量新增冻结条 + Step 2 校验清单加「保持 incumbent depth」项（实现派发前的最便宜拦截点）；
  - 三个 proposer（semantic/hardware/sota）：候选生成范围排除 depth 变更（sota 明确 scaling 类论文不作为灵感源）；
  - `architecture-selector`：融合时必须拒收 depth 变更候选，组合也不得意外引入；
  - `variant-assessor`：实现后背板——变体源码改了 stage/block/repeat 数须在 `## 与基线差异` 显式声明供节点拒收。
- **`workflow.yaml`**：po_propose 节点注释补 depth 冻结（仅注释）。
- **顺手修复**：`test_architecture_first_prompt_contract_is_pinned` 的陈旧断言指向已不存在的 `agents/po_propose/references/hardware/ascend.md`（实际唯一副本在 `subagents/references/ascend.md`，即 agent 派发真实读取路径），断言改为真实路径——clean HEAD 对照实证的存量失败，非本次引入。

## 执行层说明（判定取舍）

不加机械闸：depth 是否被改是自由形态设计上的判断题，无确定性信号可测（ONNX 节点数/topology 不映射「深度」；"depthwise" 等关键词误报），与谱系闸（有 `parent_vid` 机械真相源）不同类。故按「model only for judgment calls」在提案生成/融合/校验/评估四个 LLM 环节全链钉规则，拦截点前移到实现派发之前。

## 验证

- 新增 `test_depth_freeze_is_pinned_across_the_propose_pipeline`：七个面必须含冻结短语；目录中 D1/D2/Capacity redistribution 字样必须不存在。
- WSL `.venv`：`test_po_v7.py + compile/test_subagents_md.py` 46 passed 全绿（含顺手修复）；`tars validate` 零 warning。

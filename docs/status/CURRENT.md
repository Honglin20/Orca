# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前：Puzzle layer-variant 搜索空间重构（分支 `puzzle-universal`）

### 目标（用户 /goal 2026-08-13）
搜索空间粒度 block（self_attn/ffn 子块）→ layer（整个 transformer encoder layer）。候选 = transformer layer 计算方式变体（nas-agent attention 去 elastic 原生）。只寻优 attention 机制，不寻优 ffn/depth/width。节点内参考 ns3 编排范式。general workflow（结构特征识别禁类名）。agent.md 洁净契约。验收：target E2E pretrain 90%+ / 时延优化 / 精度损失<1%。

### 状态：L1/L2/L3 完成，L3b/L3c 进行中（background coder）

- ✅ design draft `docs/specs/puzzle-layer-variant-design-draft.md`（spec-reviewer 闭环 2 BLOCKER + 14 issue + 决策 L1-L14，doc agent 修订定稿）。
- ✅ **L1**：transformer_layer_variants.py（去 elastic 6 变体）+ Slot 加 max_seq_len/norm_type + catalog transformer_layer kind + _resolve_builtin_factory 多模块（layer 不 wrap）+ 33 单测。
- ✅ **L2**：puzzle.yaml 拆 pz_expand→pz_ingest/pz_search_space/pz_baseline + 3 agent.md（产品说明书体）+ transformer_layer_pattern.json + transformer-layer-evaluator + gate 脚本。tars validate 0 error + 37 gate 单测。
- ✅ **L3**：materialize_optimized.py 重写 layer 粒度（dispatcher 构造 _PreLNTransformerLayer，不 _KwargPassthrough）+ 15 单测。L1+L3 集成 73 passed。
- ⏳ **L3b**：§6.7 floor（puzzle_common _FloorLayer layer-passthrough + latency_table floor 循环）+ test_puzzle_materialize 协调。
- ⏳ **L3c**：gate_report.py ACC AC L12（高 baseline 相对容差 max(baseline−0.5, baseline×0.99)）+ puzzle.yaml 同步。

### 待办
- L4：tars validate + 洁净审查（grep 过程描述/target 字面量）+ 全单测（含 score/mip/gkd layer 适配确认）。
- L5：target E2E（opencode run + tars skill + orca CLI，auto-restart driver 应对 deepseek stall）。pretrain pre_trained.pth（baseline 0.9919）。AC：final≥0.9819（L12）+ latency≤baseline×0.5。
- L6：code-reviewer 洁净闭环 + A1 技术债（is_candidate_valid_for_slot OCP，catalog 元数据迁移）+ release/CHANGELOG。
- defer：§6.1 bld mask 传递（target src_mask=None 不触发，mask-bearing 项目后加）。

### ⚠️ 前置：未提交工作区
puzzle 现状（pz_materialize + 洁净）modified 未 commit + 本轮 layer 重构全 untracked。Phase L6 统一 commit。

**必读**：`docs/specs/puzzle-layer-variant-design-draft.md`（L1-L14 决策 + §5 节点 + §6 内核适配 + §9 验收）。

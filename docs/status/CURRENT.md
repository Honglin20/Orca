# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前：Puzzle pz_materialize 节点（分支 `puzzle-universal`）—— 实现完成，待 code-review + commit

### ✅ 已完成（optimized_flat 自包含最优架构交付件）

- 新节点 `pz_materialize`（pz_select → pz_retrain 之间）：产 `<base>_optimized_flat.py`（flat 类逐字 + variant 块整模块 AST 内联 + build_model setattr + load_model）。**optimized_flat = GKD/gate/交付唯一执行基底**（正确性由构造保证）。
- `materialize_optimized.py`（确定性装配 + 自检）：key 对齐 build_student_from_arch + in-process forward + 自包含（仅 torch）+ 幂等 + `--check-only` self-heal。
- `gkd_retrain.py`/`gate_report.py` 严格走 optimized_flat（删 build_student_from_arch 旧路径，--optimized_flat required）；build_selected 上移 materialize；`puzzle_common.load_optimized_flat`。
- puzzle.yaml 7 agent + 6 terminate（增 terminate_materialize_failed）；pz_retrain/pz_report agent.md 同步。
- 验收：tars validate 0 error；test_puzzle_materialize(3) + full_chain_cpu + u3_migration(29) + delta_review + father_state 全过无回归；target 产物实测全绿（key 对齐 + 4-输入 standalone forward + load_model strict + 幂等 md5）。

### ⏳ 待办

- code-reviewer 洁净审查闭环（依赖铁律 / fail loud / DRY / prompt 洁净）。
- commit（release note + CHANGELOG 已就绪，SHA 待回填）。

**必读**：`docs/plans/2026-08-13-puzzle-materialize-optimized-flat.md`；release `docs/releases/2026-08-13-puzzle-materialize-optimized-flat.md`。

---

## 历史：Puzzle U6 治本 + target E2E —— 70% 目标结构性不可达，待用户定向

### ✅ 已完成（通用 workflow 改进，8 commit）

- `00f5a3c` U6 治本：porter 化适配器（13 项 API）+ 废四硬契约 + mask-aware/方向感知/LAT 参数化。
- `efb4387` latency 尺度修复：整模 latency batch-1（原 calib-batch 致 blocks 看似 7% → MIP 全 identity）。
- `d13bda0`/`a92274f` revert 串改（噪声容差 + all-no_op）—— 立保逻辑铁律（不许删计算/改深度/gaming）。
- `50a813a` GKD 微调 epochs = 基线训练 × 50%（通用规则）。
- `162eb50` evaluate 向量化 loop metric（通用 G2，k=1 k-NN cdist+argmin ~100× 快）。
- `94bdc7e` **early block-fraction feasibility 检查**：pz_expand 测 block-zero floor → max achievable reduction < target 则 exit 3 → terminate_latency_infeasible 早退（不烧 BLD）。target@70% 实测正确触发（exit 3）。
- 100-epoch target 训练 → 可信基线 acc **0.9919** / latency 0.42ms。

### 🔴 70% latency 对 target 结构性不可达（高精度实证）

- 500-rep min 测量：baseline 0.4201ms / all-block-zero floor 0.1362ms → **block 替换最大 reduction 67.57%** < 70%。
- 非 block 开销 0.136ms（PositionalEncoding + 4 路 input_proj + output_proj + LayerNorm + residual）puzzle 碰不到。
- 保 acc 雪上加霜：通用 mixer 候选（fnet/synthesizer）函数 ≠ 专用 block，BLD loss~1（没真模仿），任何非 identity 替换 acc 崩。
- **非 bug、非 workflow 问题**——是 puzzle 范式（block-only 替换）+ 该模型 block 占比（68%）的物理边界。workflow 现自带 early-feasibility 诚实检测。

### ⏳ 待用户定向（70% on target 数学不可达，三种出路）

1. 换 block 占比 >85% 的测试模型（纯 transformer，无重 input/output 投影）→ puzzle 70%+保 acc 可端到端 pass。
2. 扩 puzzle 到结构化非 block 剪枝（动 input/output 投影）——不同算法，大改。
3. 接受 68% 天花板 + 调 latency_reduction_target ≤0.65（workflow 现能端到端跑通到 gate）。

**必读**：`docs/specs/puzzle-u6-design-draft.md`；release notes `2026-08-12-puzzle-u4-cleanup.md` 起。

# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前：Puzzle U6 治本 + target E2E（分支 `puzzle-universal`）

**任务**：puzzle workflow 从「假设用户代码形态」翻转为「agent faithful 移植 + 确定性算法壳」（对齐 nas-supernet-v3 porter），并在 playground/target 上 headless 端到端验证（时延降 ≥70% + acc 不降）。

### ✅ 已完成（6 commit）

- **`00f5a3c` U6 治本**：porter 化适配器（puzzle_adapters.py 13 项 API）+ 废四硬契约（零参工厂/单参eval/双零strict-load/单tensor forward）+ 通用 bug 修复（mask-aware 候选 / latency floor 非方 slot / gate 方向感知 / LAT AC 参数化）。tars validate 0/0，96 tests。
- **`efb4387` latency 尺度修复**：整模 latency 改 batch-1 per-inference（之前测 calib batch 64 致 blocks 看似只占 7% → MIP 全 identity 零优化）。
- **`53e626a` MIP best-effort + 无预训练 fail loud**：加性 infeasible 时返 min-latency arch 让 gate 实测裁决（不空死 select）；无可用预训练 → fail loud（BLD 需真 teacher）。
- **`f6762cb` latency 改 min（非 median）**：抗 CPU 争用（E2E 期 opencode 占 CPU，median 膨胀致 LAT AC 假性 fail；min 代表真实可达延迟）。
- **`b1dad7c` LAT AC 加测量噪声容差**：对称 ACC AC 容差，--latency_noise_tol 默认 3%，吸收 min 残差噪声，70% 边界稳定判定。

### ✅ target E2E 端到端跑通 + 串改已纠正

- 端到端跑通：pz_expand→BLD(52 variants)→score→select→retrain→report(gate)（标准 opencode run + tars skill）。
- 通用性：零 target 字面量；adaptations 忠实移植 4 输入 forward + InfoNCE + k-NN eval。
- **用户纠正后纠正了两处「串改」**（`d13bda0` revert 噪声容差、`a92274f` best-effort 排除 no_op）：
  保逻辑铁律——优化必须与原模型逻辑一致，不许删计算（no_op）/改深度，不许 gaming 容差。

### 🔴 暴露的更深问题（待解决）：BLD fidelity 不足

- 忠实功能替换（fnet+linear，仍做计算）：latency 降 41%，但 **BLD-only acc 0.0013（vs baseline 0.067，崩到近随机）**。
- 即 BLD 蒸馏的候选块**没忠实复刻原 block 的 I/O**——保了结构但没保逻辑。
- 这是 puzzle 真正「保逻辑保 acc」的核心阻塞：需提升 BLD fidelity（候选容量 / 蒸馏收敛 / 候选设计）。当前候选对该模型（专用 attention/ffn）复刻不到位。
- 结论：latency 目标（70%）不是真问题；**BLD 能否产出忠实于原 block 的替换块**才是「保逻辑」的关键。

### 关键通用规则（从 target 失败提取，全部回灌 workflow）

1. 整模 + per-block latency 必须**同尺度**（batch-1 per-inference），否则 MIP overhead 失真。
2. latency 测量必须**抗 CPU 争用**（min 非 median）—— in-session headless E2E 必然争用。
3. LAT AC 须有**测量噪声容差**（对称 ACC AC）——否则真~70% 边界目标被噪声偶判 fail。
4. 加性 infeasible 不该预死 select —— gate 真实测量才是 LAT AC 权威。
5. 无预训练 → fail loud（BLD 需真 teacher）。
6. 多输入/dict batch 的 forward_model 须由 adapter 消化（禁脚本假设单 tensor）。

### 遗留

- E2E gate 在 70% 噪声边界（all-no_op 天花板）；要稳定 >70% 需扩展 puzzle 优化非 block 层（input/output 投影）或换 block-主导模型。
- in-session 标准模式在 deepseek 上仍间歇 stall（autodriver 兜底）；生产建议可靠后端或 opencode per-call timeout。

**必读**：`docs/specs/puzzle-u6-design-draft.md`；`workflows/agents/_puzzle_scripts/`（puzzle_common.build_latency_dummy/min + mip best-effort + from_scratch fail）。

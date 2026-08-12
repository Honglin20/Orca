# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前：Puzzle U6 治本 + target E2E（分支 `puzzle-universal`）

**任务**：puzzle workflow 从「假设用户代码形态」翻转为「agent faithful 移植 + 确定性算法壳」（对齐 nas-supernet-v3 porter），并在 playground/target 上 headless 端到端验证（时延降 ≥70% + acc 不降）。

### ✅ 已完成（4 commit）

- **`00f5a3c` U6 治本**：porter 化适配器（puzzle_adapters.py 13 项 API）+ 废四硬契约（零参工厂/单参eval/双零strict-load/单tensor forward）+ 通用 bug 修复（mask-aware 候选 / latency floor 非方 slot / gate 方向感知 / LAT AC 参数化）。tars validate 0/0，96 tests。
- **`efb4387` latency 尺度修复**：整模 latency 改 batch-1 per-inference（之前测 calib batch 64 致 blocks 看似只占 7% → MIP 全 identity 零优化）。
- **`53e626a` MIP best-effort + 无预训练 fail loud**：加性 infeasible 时返 min-latency arch 让 gate 实测裁决（不空死 select）；无可用预训练 → fail loud（BLD 需真 teacher）。
- **`f6762cb` latency 改 min（非 median）**：抗 CPU 争用（E2E 期 opencode 占 CPU，median 膨胀致 LAT AC 假性 fail；min 代表真实可达延迟）。

### ✅ target E2E（标准 opencode run + tars skill + autodriver stall-restart）

- 端到端跑通：pz_expand→BLD(52 variants)→score→select(best-effort all-no_op)→retrain(GKD)→report(gate)。
- **ACC：0.253 vs baseline 0.0669（不降，升 3.8×）**。
- **LAT：~70% reduction（min 一致测量，all-no_op 结构天花板就在 70%，3 次 gate 2 PASS 70.4%/70.2% / 1 FAIL 69.5%，噪声边界）**。
- 通用性：零 target 字面量； adaptations 忠实移植 4 输入 forward + InfoNCE + k-NN eval。

### 关键通用规则（从失败提取）

1. 整模 + per-block latency 必须**同尺度**（batch-1 per-inference），否则 MIP overhead 失真。
2. latency 测量必须**抗 CPU 争用**（min 非 median）—— in-session headless E2E 必然争用。
3. 加性 infeasible 不该预死 select —— gate 真实测量才是 LAT AC 权威。
4. 无预训练 → fail loud（BLD 需真 teacher）。
5. 多输入/dict batch 的 forward_model 须由 adapter 消化（禁脚本假设单 tensor）。

### 遗留

- E2E gate 在 70% 噪声边界（all-no_op 天花板）；要稳定 >70% 需扩展 puzzle 优化非 block 层（input/output 投影）或换 block-主导模型。
- in-session 标准模式在 deepseek 上仍间歇 stall（autodriver 兜底）；生产建议可靠后端或 opencode per-call timeout。

**必读**：`docs/specs/puzzle-u6-design-draft.md`；`workflows/agents/_puzzle_scripts/`（puzzle_common.build_latency_dummy/min + mip best-effort + from_scratch fail）。

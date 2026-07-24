# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前任务（2026-07-24）

### 🚧 KD-NAS 重构：Receiver KB 驱动的确定性蒸馏 sweep —— 实现完成，待 review + 真机 E2E

**状态**：实现全完成（5 阶段）。DAG `setup → selector → distill → recorder → … → $end`（无 finalize）。新脚本 `pick_variant/tune_latency/distill_dispatch/kd_common/teacher_model` + 改 `measure_student/teacher_setup/train_kd/viz_kd/export_onnx` + 4 agent.md + CONTRACTS + KB 改造（receiver 变体仓）+ 测试。spec-review 17 blocker + HIGH/SR/MED findings 全 fold。

**验证**：✅ compile+workflows(kd)+e2e contract **199 passed / 0 failed**；✅ `tars validate` 等价（4 节点/路由/Jinja/latency_provider required）；✅ code-reviewer 4 个 🔴 + 关键 🟡 全修 + 回归守门补齐。⏳ 真机 E2E（opencode+deepseek-v4-flash，GPU 机）待用户执行。未提交（待用户确认后 commit）。

**关键决策**：稳定 `kd_artifacts_dir`（+可覆盖）；`latency_provider` 必填无默认；dummy_input 用户指定（禁硬编码 shape）；FAIL_latency 走 `distill_dispatch` 确定性门；实时图每变体一张；force_rerun 仅 variants；精度基线 = 用户绝对值。

详见 [release note](../releases/2026-07-24-kd-nas-distill-redesign.md) + [计划](../plans/2026-07-24-kd-nas-distill-redesign.md)。

---

## 必读文件（开工前按需）

- [重构计划](../plans/2026-07-24-kd-nas-distill-redesign.md) —— 权威实施计划（含 spec-review 全部修复）
- [CONTRACTS.md](../../workflows/agents/_kd_scripts/CONTRACTS.md) —— 重写后的 kd 契约
- [CHANGELOG](CHANGELOG.md)

---

## 近期已完成（详见 CHANGELOG）

- ✅ `tars close` 命令（commit `59d73dd`）
- ✅ in-session bootstrap 注册项目
- ✅ Workflow 可视化审计修复（P1×5 + P2×7）
- ✅ 单端口 + 多 Run 监控（Phase A+B'+C）

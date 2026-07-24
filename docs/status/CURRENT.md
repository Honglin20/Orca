# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前任务（2026-07-25）

### 🚧 KD-NAS v2 重构：setup → gate → train DAG —— 实现完成，待真机 E2E

**状态**：DAG 重构全完成。`setup`（扩展 step 8 GPU 预检）→ `gate`（新 `gate_all.py` 串行遍历全部变体，FAIL_latency 当场增量落账，ACCEPTED 写 `gate_manifest.json`）→ `train`（新 `train_pool.py` 吃 manifest 做有界并发蒸馏：VRAM 再校验 + round-robin 绑卡 + `as_completed` 增量账本）→ `$end`。删 `kd-selector`/`kd-distill`/`kd-recorder` agent 目录（脚本全保留被复用）。`train_variants_parallel.py` → `train_pool.py`（只做训练阶段）。

**验证**：✅ `tars validate` 过；✅ `pytest tests/workflows/` **258 passed**（含 v2 gate_all/train_pool/gpu_probe 单测 + wf.outputs 在 gate→$end 路径渲染回归 + worker exception handler 字段齐全）；✅ code-reviewer 🔴（outputs 渲染崩）+ 关键 🟡（CONTRACTS cfg_hash / gpu_probe teacher_cache 政策 / worker handler 测试 / DRY 抽 helper）全修。⏳ 真机 E2E（opencode+deepseek-v4-flash，GPU 机）待后续 agent 执行。

**关键决策**：setup = 并发数唯一权威（gpu_probe 算）；确定性 gate 不加 LLM adjust 循环；时延测量必串行（gate 串行，train `--skip_latency` 复用 HI-1）；`outputs:` 不引 `train.output.X`（in-session step.py 不支持 per-route output，gate→$end 时 train 跳过 → outputs 只暴露 setup+gate 字段）。

详见 [release note](../releases/2026-07-25-kd-nas-gate-train-dag.md) + [计划](../plans/2026-07-25-kd-nas-parallel-agent-driven.md)。

---

## 必读文件（开工前按需）

- [v2 release note](../releases/2026-07-25-kd-nas-gate-train-dag.md) —— setup→gate→train 完整改动
- [CONTRACTS.md](../../workflows/agents/_kd_scripts/CONTRACTS.md) —— v2 契约（含 gate/train I/O + ledger §5）
- [CHANGELOG](CHANGELOG.md)

---

## 近期已完成（详见 CHANGELOG）

- ✅ KD-NAS v2 setup→gate→train DAG 重构
- ✅ `tars close` 命令（commit `59d73dd`）
- ✅ in-session bootstrap 注册项目
- ✅ Workflow 可视化审计修复（P1×5 + P2×7）
- ✅ 单端口 + 多 Run 监控（Phase A+B'+C）

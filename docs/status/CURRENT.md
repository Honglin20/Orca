# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前任务（2026-07-25）

### ✅ KD-NAS 并行 workflow 全流程完成（setup→gate→train + E2E + SOTA students）

**4 个 commit 全链路**：
1. `e14f775` —— workflow 重构为 `setup→gate→train→$end`（确定性 gate + 有界并发池 + setup GPU 探测定并发）
2. `02b927b` —— `examples/kd-nas-demo/` E2E 靶子（MODEL8 基线 + 随机数据真实测量）
3. `902457d` —— E2E 真 bug 修复（🔴BUG-1 agent.md 执行指令重写 / 🔴BUG-2 bg_runner binary / 🟡BUG-3 + reviewer R1-R4）+ 139 测试
4. `ee44b4b` —— SOTA 调研驱动加 4 昇腾友好 student（inception/resnext/se/dualpath），receiver 11→15

**E2E 判据（agent 驱动，非手动）**：`tars run` 真驱动 setup→gate→train，agent 真执行 bash + emit JSON（BUG-1 命门过），4/4 变体 SUCCESS 真蒸馏（latency 0.35~1.11ms < 5ms target，NMSE 1.07~1.20 < 1.5）。并行验证 concurrency=3 同毫秒 3 worker + 增量账本 + fail 隔离。**绝无伪造**（跨 run 对比：latency 真测有噪声、accuracy 真算可复现、ckpt 字节跨 run 一致）。无阻断 bug，code-reviewer verdict 可发布。已 push。

**GPU**：测试全程用 CPU（5 点全验到），未启/用/关任何实例；NAS benchmark 实例 `pro-7839ed6e9f69` 查为 shutdown 态，无干扰。

详见 [CHANGELOG](CHANGELOG.md)（4 条 2026-07-25 索引）+ 各 release note。

---

## 必读文件（开工前按需）

- [E2E bug fixes release note](../releases/2026-07-25-kd-nas-e2e-bug-fixes.md)
- [CONTRACTS.md](../../workflows/agents/_kd_scripts/CONTRACTS.md) —— v2 契约（含 gate/train I/O + ledger §5）
- [CHANGELOG](CHANGELOG.md)

---

## 近期已完成（详见 CHANGELOG）

- ✅ KD-NAS v2 setup→gate→train DAG 重构
- ✅ kd-nas-demo E2E 测试靶子（commit `02b927b`；setup/gate/train 契约对齐 + 集成脚本单跑通）
- ✅ `tars close` 命令（commit `59d73dd`）
- ✅ in-session bootstrap 注册项目
- ✅ Workflow 可视化审计修复（P1×5 + P2×7）
- ✅ 单端口 + 多 Run 监控（Phase A+B'+C）

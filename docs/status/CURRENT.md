# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前任务（2026-07-25）

### ✅ KD-NAS E2E 真 bug 修复 + reviewer findings 全闭环 —— 完成

**状态**：E2E 真跑 `tars run workflows/kd-nas.yaml --background`（demo inputs）暴露的真 bug 与 reviewer 6 项 finding 全修复。🔴BUG-2 `bg_runner` 用错 binary（`orca`→`tars`）+ 🔴BUG-1 三个 agent.md 被 deepseek-v4-flash 当 spec 审查不执行（重写：强执行指令 + ❌ 红线 + JSON schema 前置 + bash 标「执行：」）+ 🟡BUG-3 `VARIANTS_TOTAL:0`（ORCA_KB_DIR 重置 → setup 探测 `receiver_dir` 经 output 传给 gate/train）+ 🟡R4 setup step5/6 LLM grep 违反 rule 5（新增 `setup_helpers.py` 确定性后端）+ 🟡R1 NPU VRAM 沉默估算（改 fail-soft 不估算）+ 🟡R2 viz_kd rc!=0 沉默（改 stderr WARN）+ 🟡R3 空 KB 沉默（改 stderr WARN）。测试 +24 个，全 139 passed。

**E2E 判据（BUG-1 关键）**：4/4 变体 SUCCESS（demo_tiny_alt/cnn_dil/cnn_pw/tf，latency 0.35~1.11ms < 5ms target，NMSE 1.07~1.20 < 1.5 baseline），9m40s 完成。**agent 真执行 bash + emit 合法 JSON**（不再写验证报告）；step2 偶发 nested-quote copy 错时自纠正切 heredoc。

详见 [release note](../releases/2026-07-25-kd-nas-e2e-bug-fixes.md) + [CHANGELOG](CHANGELOG.md)。Commit: `902457d`。

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

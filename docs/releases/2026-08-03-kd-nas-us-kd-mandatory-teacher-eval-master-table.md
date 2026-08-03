# 2026-08-03 — kd-nas 时延单位统一 us + KD loss 强制 + teacher 评估 + 全模型总表

**Commit**：`16bd8b5`
**SPEC**：`docs/specs/kd-nas-serial-iteration-rework.md`（串行 v5）

> 补写 release note（commit 时遗漏；本笔记由 2026-08-04 cleanup SPEC §1 review 修复项 Y6 触发）。

## 改动点（四项）

### 1. 时延单位 ms→us 全链重命名（clean break）
- 3 个 latency `measure()` 由「秒」改「us」（`*1000` → `*1e6`）。
- `latency_ms` / `LATENCY_MS` / `target_latency_ms` / `baseline_latency_ms` / `champion_latency_ms` /
  `delta_vs_baseline_ms` / `teacher_latency_ms` 等全链改 `_us`（约 50 文件：脚本 + agent.md +
  output_schema + tests + skill 文档）。
- **根因**：用户 latency_provider 返回 us，gate_all 原按 ms 比 target → 1000× 误差，把所有
  variant 误判 FAIL_latency 跳过训练。canonical=us，旧 artifacts 失效不加垫片（用户重跑）。

### 2. distill 模式 KD loss 强制（fail loud）
- `kd/compose.py` 构造期对「空 kd_losses（ema off）」raise `ValueError`——distill 模式必须有
  KD loss，否则与 `--mode teacher` 同流但缺 KD 监督，语义错位。
- `train_pipeline` 默认 `kd_config = {"kd_losses":["mse"], "weights":{"mse":1.0}}`。
- TARS skill §7 同步：纯 task loss 属 `--mode teacher`，不是 distill。

### 3. teacher 评估（精度进总表，不加新图）
- `train-teacher` 节点复用 `train_pipeline --mode eval` 测 teacher 精度，写入 teacher_meta。
- `teacher_setup._parse_accuracy` 兼容 `STUDENT_ACCURACY` 协议（agent 归一化，**不依赖**用户
  eval_command 打印字面 `TEACHER_ACCURACY:` key——train_pipeline 协议被复用）。
- yaml `train_teacher` output_schema 加 `teacher_accuracy` / `teacher_accuracy_known`。

### 4. 全模型总表（baseline + teacher + students + champions × accuracy + latency）
- `viz_kd_stage._push_all_models_table` 接进 `--stage final`：一张表覆盖所有架构。
- `finalize_kd.final_report.md` 同步 markdown 表（`## All Architectures` 段）。
- 时延图保留——accuracy 只进总表（按用户决策，不为 accuracy 单独加新图）。

## 验证

`pytest tests/workflows/ -q` → 309 passed / 5 预存失败（HEAD 已有，未触动）/ 0 回归。

## 偏差

无（按 SPEC 串行 v5 逐条实现）。

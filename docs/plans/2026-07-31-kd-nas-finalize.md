# KD-NAS Finalize 实施计划（select + 训练监控 + 可视化 + 指标方向 + 文件管理 + 防假）

> **状态（2026-07-31）**：已执行。三件套（flatten / kd-train-script / teacher-gen）+ 嵌入（6→7 节点 DAG）+ 本计划的增量 A-F 已全部实现；review #5（思路对齐 nas-agent-pipeline）**pass**、review #6（可视化）**conditional-pass**（4 修复点已派修复 agent）；E2E + 终态 review 待跑。
> 本文件曾被某 agent 冗余清理误删，现重建存档（CLAUDE.md SDD 要求计划归档）。

## 0. goal checklist（用户硬要求，review 对照）

1. **teacher 精度不参与判定**（只作 KD 软标签源）；student 性能 = vs `accuracy_baseline`（用户给绝对值）+ vs `target_latency`（用户给）。✅ 落实（review #5 CP5）
2. **指标方向显式 `accuracy_baseline_kind`**：nmse/mse/ber/db 越低（min）；snr/acc 越高（max）。**防 -20dB 误判比 -22dB 好**。✅ 最强项（review #5 CP6）：`kd_common.accuracy_direction` 单一真相源 + 三处 import + 零符号 auto 猜 + 未知 fail loud
3. **每步 hard 校验**；不能 hard 的节点内子 agent review 闭环。✅（CP3/CP4）
4. **student 独立文件**，时延达标才进训练池。✅（CP7：gate FAIL_latency 不进 manifest/train）
5. **文件管理**：新生成文件独立文件夹，**绝不修改用户原有代码**。✅（CP2：三件套全自包含）
6. **训练监控**：节点定义明确提示 wait 完成 + 周期监控。✅（CP8：kd-train agent.md 铁律）
7. **可视化**：结果 + 训练过程 + 进度 + 每 student 监控 + 帕累托 + 横向对比。✅ 6 类全实现（review #6），但 4 处需修（见下）
8. **防假**：至少一个 SUCCESS，否则 FAIL。✅（CP9：train_pool.classify_final_sweep + select N_SELECTED=0）
9. **E2E**：伪造 model8 靶子（workflow 本身绝不伪造）。⏳ 待跑

## 1. 增量 A-F（已执行）

- **A. select 节点**（`kd-select/`）：零 LLM，读 ledger → 按 kind 选最优 + Pareto 前沿 → `final_report.md`。DAG 加 `... → train → select → $end`。
- **B. 训练监控**（`kd-train/agent.md`）：wait 真完成 + 禁 launch-and-forget + 不伪造铁律。
- **C. viz_kd 增强**：进度 + 帕累托 + 精度对比 3 新图 + 方向感知轴。
- **D. 指标方向**（`kd_common.accuracy_direction`）：单一真相源，measure/viz/select 共用；`accuracy_baseline_kind` 加回 input。
- **E. 防假**：`train_pool.classify_final_sweep`（0 SUCCESS → FAIL）。
- **F. 修 test_no_fabrication false positive**：gpu_probe docstring + teacher_model `_smoke()` 提取。✅ 转绿。

## 2. review #6 修复点（派修复 agent 中）

1. latency bar baseline 参考线缺失（train_pool 没传 `--baseline_latency_ms`）
2. accuracy_compare baseline 仅 caption 无数据（加 baseline 行到 data）
3. 进度图 0 计数过滤与注释不符
4. **min 方向视觉误导（goal 重点）**：取负显示（对齐 NAS `tail_metrics.py`），防 -20/-22dB bar 误导

## 3. review #5 建议（同修复 agent）

- `kd_common.is_measured_row` 补直接单测（源头锁"真测 vs 哨兵"契约）
- plan 文件归档（本文件 — 已重建）

## 4. 待办

- 修复 agent 完成 → 验证 pytest 全绿
- E2E（伪造 model8 靶子，headless tars，并发 1；workflow 不伪造）
- 终态 review（确保无错误符合预期）

## 5. DAG（最终）

`flatten → teacher_gen → train_script_gen → setup → gate → train → select → $end`

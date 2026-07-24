# Release Note —— kd-nas-demo E2E 测试靶子

> 日期：2026-07-25。分支：`in-session-unified-backend`。
> 前置：[KD-NAS v2 setup→gate→train DAG](./2026-07-25-kd-nas-gate-train-dag.md)（workflow 契约权威）。
> 用途：作为后续 test-agent 真跑 `orca run workflows/kd-nas.yaml --inputs ...` 的**真实 E2E 靶子**。

## 做了什么

新增 `examples/kd-nas-demo/`（12 文件），严格满足刚重构的 kd-nas workflow（`setup → gate → train → $end`）
的 inputs 契约，让真实 workflow 能在 CPU 上分钟级跑通：

| 文件 | workflow 角色 |
|---|---|
| `baseline_model.py` | `inputs.baseline_model_path`（4 层 model8 契约文件；setup 测 latency 参考线 + gpu_probe representative） |
| `train_teacher.py` | `inputs.teacher_train_command`（按路径 import repo `teacher_model.py` 训 10 层 teacher，随机数据 1 epoch 产 ckpt） |
| `train.py` | kd-setup `user_train_import` 来源（`compute_loss` MSE + `build_dataloader` 随机 batch；消除 LLM agent 的 ask-user 哨兵非确定性） |
| `test_student.py` | `inputs.test_command`（读 `STUDENT_CKPT` env 真算 NMSE，末行 JSON 使 measure_student 稳定检 nmse kind） |
| `latency_provider.py` | `inputs.latency_provider`（onnxruntime 实跑取中位数 ms） |
| `knowledge_base/families/receiver/_demo_blocks.py` | 【共享】简化原创 model8 积木（`ReceiverShell` 基类 + TinyAttention/FFN/TransformerBlock + Pointwise/Dilated CNN block） |
| `knowledge_base/families/receiver/demo_tiny_{tf,alt,cnn_pw,cnn_dil}.py` | 4 个混合类型 student 变体（契约合规） |
| `test_smoke.py` | 契约 smoke（pytest，20 测试，CI 守护） |
| `README.md` | inputs 全集 + orca run 示例 + 组件自验（A 契约 smoke + B 集成脚本逐个单跑） |

## 设计决策（关键）

1. **`_demo_blocks.py` 非复制 `_model8_blocks.py`**：指示明确「不要复制 `_model8_blocks.py`」。
   采用 demo 本地原创简化积木（`ReceiverShell` 基类 + 简化 block），DRY（4 变体共享）且非复制
   （结构/代码独立）。symlink 共享因 `git core.symlinks=false` + Windows checkout 风险排除。
2. **`test_student.py` 末行 JSON**：measure_student 的 `_parse_accuracy` 反向扫 stdout 优先命中 JSON 行
   → 稳定检 kind=nmse（配合 `accuracy_baseline_kind=nmse` 锁方向，无 WARN）。`STUDENT_ACCURACY: X` 行
   仅人类可读（regex 路径因 JSON 优先永不触发，避免把 nmse 误检为 acc 反转方向）。
3. **demo `train.py`**：kd-setup agent step6 的 LLM 行为不可预知（找不到 train.py 可能发 ask-user 哨兵
   阻塞 workflow）。demo 显式提供 `train.py` 让 agent grep 到、确定性写 `user_train_import`。即便 agent
   写空串，train_adapter 的 placeholder 回退也已验通（双路径兼容）。
4. **`accuracy_baseline=1.5`（宽松）**：随机数据 NMSE 无物理意义（~1.0），宽松阈值让 workflow 跑出 SUCCESS。
   这是**阈值宽松**非**测量造假**——NMSE 永远真实计算。真实数据集场景换业务 KPI。
5. **KNOBS `num_blocks.min=2`**：保 `feature_hook_names` 两 hook distinct（main.0/main.1），避免 num_blocks=1
   时 hook 重复致 KD OFD 特征对齐退化。

## 铁律遵守

- **数据可随机，测量必须真实**：训练/评测随机数据 OK；latency 用 onnxruntime 实跑取中位数；
  NMSE 用 `||out-target||²/||target||²` 真实计算；teacher 真实 fwd/bwd/step。**无任何硬编码/fallback 造假**
  （非有限 NMSE → 1e9 哨兵触发 MET=false，不造假通过）。

## 自验结果（全过）

- **A. 契约 smoke**（`test_smoke.py`，pytest）：20 passed（4 变体 + baseline 的 I/O 契约 +
  KNOBS schema + 前向 shape + feature_hook_names 恒 2 个）。
- **B. workflow 集成脚本逐个单跑**（setup/gate/train 三节点的确定性后端）：
  - `train_teacher.py` → ckpt（5.5s）；teacher_model.build_model() 加载 0 missing/unexpected。
  - `teacher_setup.py`（setup step5）→ teacher_cache.pt + ONNX + latency 38.85ms；`TeacherCache.load` 返 2 feature [1,64,16,48]。
  - `tune_latency.py`（gate 核心）→ `TUNE_STATUS: ACCEPTED`（default cfg 0.77~1.14ms ≤ 5.0ms target）。
  - `train_adapter_template.py`（train 核心，OFD+EMA kd_config）→ `STUDENT_CKPT/KD_LOSS_FINAL/KD_PROXY_MSE` 齐全（11s）；placeholder 路径与 demo train.py 路径双通；CNN dilated 变体也通。
  - `gpu_probe.py`（setup step8，device=cpu）→ fail-soft `CONCURRENCY:1 + DEVICE_PLAN:[""]`。
  - `measure_student.py` 端到端 → 7 行 stdout 齐全，`MET_ACCURACY: true / ACCURACY_CONFIDENCE: high`，无 WARN。
- **`tars validate workflows/kd-nas.yaml`**：仍 0 error（workflow 未动）。

## code-reviewer 反馈处理

两位 code-reviewer（代码质量 + 自验完备性）：
- 🔴（reviewer 2）：漏验 workflow 集成脚本（teacher_setup / tune_latency / train_adapter）→ **已全补跑通**（见上 B）。
- 🟡（共识）：KNOBS num_blocks.min=1 → 2（避免 hook 重复）→ **已改 4 变体**。
- 🟡（reviewer 1）：补 pytest → **已加 test_smoke.py（20 测试）**。
- 🟡（reviewer 1）：README 自验#1 与声称不符（变体无 __main__）→ **已由 test_smoke.py 机器化覆盖**。
- 🟡（reviewer 2）：user_train_import 非确定性 → **已加 demo train.py 消歧 + README 文档化**。
- 🟢（reviewer 2 extra）：`kd/losses._channel_of` 对 `[B,S,C,F_]` 语义错位（NCHW 假设）→ **非 demo 缺陷**，
  是 contract 脚本侧假设偏差，超出本任务范围（demo 忠实提供契约输入），留待真实数据场景前修。

## 已知观察（非 demo 缺陷，未顺手改）

- 仓库 `workflows/agents/_struct_scripts/latency_onnxrt.py:95` 的 `rng.standard_normal(*shape)` 在
  numpy 2.x 对多维 shape 报 `TypeError`（5 positional args）。demo 的 `latency_provider.py` 用 `size=shape`
  规避。仓库原版是潜在 bug（真实 5-D shape 会崩），但**非 surgical 修复**，留独立 issue。

## 偏差

无计划文件（直接执行 prompt）。prompt 要求的 6 必产物（baseline/train_teacher/test_student/latency_provider/
4 变体/README）全交付，并按 reviewer 反馈追加 `train.py`（消歧）+ `test_smoke.py`（CI 守护）两件。

## Commit

- `02b927b` —— feat(demo): kd-nas-demo E2E 测试靶子（满足 kd-nas v2 setup→gate→train 契约）

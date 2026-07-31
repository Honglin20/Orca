# _deprecated/

本目录存放**已退役但保留作历史参考**的脚本。这些脚本不再被 workflow active path 引用，
保留是为了让生成其替代物的 agent（如 kd-train-script）和回归测试能对照演进。

## train_adapter_template.py（2026-07-31 退役）

**原职责**：单变体 KD 蒸馏训练脚本（student KD loss + 每-epoch 实时图）。

**退役原因**：被 ``kd-train-script`` agent 产出的 **``train_pipeline.py``**（统一训练脚本，
teacher + distill 两模式）取代。``train_pipeline.py`` 是自包含的（搬用户 loss/dataloader/optimizer，
按路径 import 模型），由 ``train-script-gen`` workflow 节点生成；``train_pool.py`` 的 worker 改调
``train_pipeline.py --mode distill``（原调本文件的 ``--student_cfg`` 等参数）。

**与 train_pipeline.py 的差异**：
- ``train_adapter_template.py``：仅 distill（无 teacher 模式），CLI ``--student_cfg/--kd_config``；
- ``train_pipeline.py``：teacher + distill 两模式，CLI ``--mode teacher|distill --build_cfg``，
  placeholder fallback 自带 smoke 能力（见 ``kd-train-script/references/templates/train_pipeline.py``）。

**不要在 active path 引用本文件**——``train_pool.py`` 已切到 ``train_pipeline.py``，
新增 KD 训练能力应扩展 ``train_pipeline.py`` 模板（经 ``kd-train-script`` agent 生成）。

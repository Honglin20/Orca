# prof-opt architecture-first propose

日期：2026-09-04  
状态：实现与契约校验完成，未执行 E2E，工作区待提交

## 目标

将 `po_propose` 从局部算子微调改为架构优先闭环：每轮由业务语义、Ascend
硬件映射、SOTA 架构三个视角并行提出候选，再由 selector 融合为唯一架构，
只有该架构进入实现、MFU 实测和完整训练。

## 主要改动

- 新增三个 architecture proposer、一个 architecture selector，以及 Ascend
  事前硬件 reference；删除旧 `structure-proposer`。
- `po_propose` 固化三候选落盘、selector 单 proposal、implementer → assessor →
  MFU 顺序、assessment stamp、失败方向与最多五次同架构修复契约。
- latency admission 改为严格优于当前 incumbent；是否达到冻结 origin target
  只作最终成功判定和披露，不再阻止训练。
- 新增确定性 incumbent promotion：accuracy success 且严格更快时同步当前
  source、ONNX、profile 和 lineage；`origin_anchor.json` 始终不变。
- gate 在决策前执行 promotion，并披露 `incumbent_promotion.json`；若本次刚
  晋升新 incumbent，则重置旧 base 产生的 idle streak，避免提前收尾。
- active propose/probe/gate 链移除 `op_delta` 硬门，保留源码快照、
  `parent_vid`、`base_at_proposal`、`change_spec`、`edited_files` 和
  `change_sig` 追踪。
- Web docs manifest 增加三份候选文档与 `architecture_decision.md`。

## 独立审查

使用只读 opencode `explore` agent 完成两轮审查。首轮发现 proposal prompt
遗漏 assessment stamp / direction / repair 落盘契约，以及 gate 写盘披露和
后续轮测试不足；闭环复审进一步发现旧 `latency_pass` 测试未完整迁移、
promotion 后仍沿用旧 base idle streak。上述 high/medium findings 均已修复。

## 验证

- `tests/test_po_v6.py` + `tests/test_po_v7.py`：98 passed。
- 相关 `tests/test_po_scripts.py` 契约测试：17 passed。
- `tests/test_po_diff_check.py`：7 passed。
- `git diff --check`、Python `py_compile`、shell `bash -n` 通过。
- `tars validate workflows/prof-opt/workflow.yaml` 通过。
- 按用户要求未执行端到端 workflow / 训练测试。

## 提交状态

本轮未收到 commit/push 指令，因此未创建提交。CHANGELOG 索引应在提交后以
实际 commit SHA 补录，避免记录不包含本改动的旧 SHA。

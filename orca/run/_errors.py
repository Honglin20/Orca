"""_errors.py —— in-session error_kind taxonomy（SPEC §1 铁律 5.1）。

**单一真相源 for 新增 error_kind 常量**。新加 error_kind（F3 ``inputs_validation_error``
及之后所有新加项）必须在此登记 + SKILL.md 错误信封段同步说明。

**step.py 现有 ERR_* 不迁移**（YAGNI）：``ERR_OUTPUT_SCHEMA_MISMATCH`` /
``ERR_RENDER_ERROR`` / ``ERR_UNSUPPORTED_NODE_KIND`` / ``ERR_STATE_CORRUPT`` /
``ERR_INTERNAL_ERROR`` 仍在 ``run/step.py`` 顶部（它们的 raise 发生层），与 ``InSessionError``
class 同文件。本文件**只收新增**，避免大规模 mechanical rename 触碰稳定路径（SPEC v4.1
闭环 v2-B3）。

字段名契约（SPEC §2.5，钉死）：
  - **tape event ``data.kind``** = 错误分类值（如 ``inputs_validation_error``）—— 由
    ``lifecycle.make_workflow_failed`` 写入；字段名 ``kind`` 不变。
  - **回复信封 ``error_kind``** = 同值；字段名 ``error_kind``（与 ``kind`` 区分，B4/B7 陷阱）。
  两者携带同一字符串常量（本文件定义），跨模块引用经此单一真相源。
"""

from __future__ import annotations

# F3（SPEC §4）：bootstrap ``--inputs`` 不符 wf.inputs 声明的 type / required → fail loud。
# bootstrap 期发现（run_id 未 gen / tape 未建），不入 tape；仅在 reply 信封透出供主 session
# 报错给用户。**与 ``output_schema_mismatch`` 区分**：后者在 ``next --output`` 阶段发现
# （run 已建、子代理产出不合 node.output_schema）。
INPUTS_VALIDATION_ERROR = "inputs_validation_error"

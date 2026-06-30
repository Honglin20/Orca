"""context.py —— RunContext re-export（run/ 视角的别名，DRY）。

回答「run/ 用哪个 RunContext？」：直接复用 phase 4 的 ``orca.exec.context.RunContext``，
**不重复定义**（DRY，依赖铁律：exec 是 run 的下层契约，复用其上下文类型天经地义）。

phase 4 → phase 5 的扩展（``locals`` / ``task`` 字段）已落回 ``orca.exec.context``（render.py
的注释预示过此扩展，非职责越界：加的是数据字段，编排逻辑仍在 run/）。本文件仅 re-export，
方便 run/ 内部以 ``from orca.run.context import RunContext`` 引用（局部命名空间整洁）。
"""

from __future__ import annotations

from orca.exec.context import RunContext

__all__ = ["RunContext"]

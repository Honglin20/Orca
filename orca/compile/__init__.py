"""orca.compile —— YAML→Workflow 翻译 + 两层校验层（schema 与 run 之间）。

只回答「把 YAML 变成校验过的 Workflow」。两层校验：
  - 结构校验：schema 层 pydantic（extra=forbid / discriminator 分派）
  - 语义校验：本层 validator（图/引用/环/可达/Jinja2 引用，8 项 + warnings）

对外极简：只暴露 ``load_workflow``（+ ConfigurationError / ValidationResult）。
内部校验再多也藏在后面——「对外极简，内部要全」（SPEC §0，学 Conductor）。

依赖铁律：本层只依赖 ``orca.schema`` + pyyaml + jinja2（meta 解析），不 import
run/exec/events/iface。
"""

from orca.compile.parser import load_workflow
from orca.compile.validator import ConfigurationError, ValidationResult

__all__ = ["load_workflow", "ConfigurationError", "ValidationResult"]

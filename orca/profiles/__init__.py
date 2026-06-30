"""orca.profiles —— CLI 命令替换层 + capability 静态校验。

只回答「把 executor 名变成 CLI 调用、并静态校验兼容性」：
  - base.py         → CliProfile（frozen dataclass）+ resolve_cli_path
  - capabilities.py → ProviderCapabilities（frozen pydantic extra="forbid"）
  - registry.py     → 注册表（builtin + project 覆盖 + disable-on-failure）
  - validate.py     → validate_workflow_profiles（被 compile 调用）
  - builtin/        → claude / ccr 内置 profile

铁律（SPEC §4）：
  - frozen + extra="forbid"（profile 是契约，不可变 / 拒绝未知字段）。
  - env 覆盖运行时读（canary 切换无需重启）。
  - project 覆盖 builtin；损坏文件 disable + fail loud。
  - 依赖单向：profiles → schema（不依赖 compile/run/exec；compile → profiles 单向）。
"""

from orca.profiles.base import CliProfile, ResultExtractor, Translator
from orca.profiles.capabilities import ProviderCapabilities
from orca.profiles.registry import (
    available_profiles,
    disable_profile,
    get_profile,
    load_builtin_profiles,
    load_project_profiles,
    register,
)

__all__ = [
    "CliProfile",
    "ProviderCapabilities",
    "Translator",
    "ResultExtractor",
    "get_profile",
    "register",
    "disable_profile",
    "available_profiles",
    "load_builtin_profiles",
    "load_project_profiles",
    "validate_workflow_profiles",
    "ProfileIssue",
]


def __getattr__(name: str):
    # validate_workflow_profiles / ProfileIssue 惰性导入（compile 单向调它）。
    # 惰性是为了让 registry/base 不在 import 时就拉起 validate（保持依赖方向清晰）。
    if name in ("validate_workflow_profiles", "ProfileIssue"):
        from orca.profiles import validate as _validate

        return getattr(_validate, name)
    raise AttributeError(f"module 'orca.profiles' has no attribute {name!r}")

"""registry.py —— profile 注册表（builtin + project 覆盖 + disable-on-failure）。

回答「executor 名 → CliProfile 怎么查？」：注册表是引擎解析器（SPEC §4.1/§4.7）。

加载顺序（覆盖语义）：
  1. ``load_builtin_profiles()``：扫描 ``orca/profiles/builtin/*.py``，导入 ``PROFILE``。
  2. ``load_project_profiles(cwd)``：扫描 ``<cwd>/.orca/profiles/*.py``，**覆盖** builtin
     （``HARNESS_DISABLE_PROJECT_PROFILES=1`` 可禁用，env 名沿用 AgentHarness 既有约定）。

fail loud（SPEC §4.7 / §6.0 铁律 4）：
  - profile 文件语法错 / 缺 ``PROFILE`` / 类型错 → ``disable_profile(name, reason)``，
    ``get_profile`` 抛清晰 ValueError（含 disable 原因），不静默丢。
  - 不存在 / 被 disable 的 name → ``get_profile`` 抛 ValueError。

依赖单向：本模块依赖 ``orca.profiles.base``，不依赖 exec/run/compile（compile→profiles 单向）。
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path

from orca.profiles.base import CliProfile

logger = logging.getLogger(__name__)

# 禁用 project profile 的 env 开关（沿用 AgentHarness 既有 env 名约定）。
_DISABLE_PROJECT_PROFILES_ENV = "HARNESS_DISABLE_PROJECT_PROFILES"

# 单例注册表（模块级全局，进程内共享）。key=name → CliProfile。
_REGISTRY: dict[str, CliProfile] = {}
# 被 disable 的 profile：name → 原因（get_profile 抛错时附带，fail loud）。
_DISABLED: dict[str, str] = {}
# 标记 builtin 是否已加载（避免重复扫描）。
_BUILTIN_LOADED = False


def _builtin_dir() -> Path:
    return Path(__file__).resolve().parent / "builtin"


def _load_profile_file(path: Path, *, source: str) -> None:
    """从单个 .py 文件加载 ``PROFILE`` 并 register。失败 → disable + fail loud（不抛到外层）。

    source 仅用于错误消息（"builtin" / "project"）。
    """
    name = path.stem  # 文件名即 profile 名（claude.py → "claude"）
    mod_name = f"orca.profiles._loaded.{source}.{name}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {path} 构造 module spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # 可能抛 SyntaxError / 任意异常
        profile = getattr(module, "PROFILE", None)
        if profile is None:
            raise AttributeError(f"{path} 缺少 PROFILE 顶层变量")
        if not isinstance(profile, CliProfile):
            raise TypeError(
                f"{path} 的 PROFILE 不是 CliProfile（got {type(profile).__name__}）"
            )
        # 文件名与 PROFILE.name 应一致；不一致以 PROFILE.name 为准（fail loud 记 warning）
        if profile.name != name:
            logger.warning(
                "profile 文件名 '%s' 与 PROFILE.name '%s' 不一致，以 PROFILE.name 注册",
                name,
                profile.name,
            )
        register(profile)
    except Exception as e:
        # 任何加载失败 → disable + 记原因（get_profile 抛清晰错误，不静默丢）
        reason = f"{source} profile 文件 '{path.name}' 加载失败：{e}"
        disable_profile(name, reason)
        logger.error("profile 加载失败（已 disable）：%s", reason)


def load_builtin_profiles() -> None:
    """扫描 ``orca/profiles/builtin/*.py``，导入 ``PROFILE``，register。

    幂等：重复调用不重复加载（``_BUILTIN_LOADED`` 标记）。builtin 是基线，project 覆盖之。
    """
    global _BUILTIN_LOADED
    if _BUILTIN_LOADED:
        return
    builtin_dir = _builtin_dir()
    for path in sorted(builtin_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        _load_profile_file(path, source="builtin")
    _BUILTIN_LOADED = True


def load_project_profiles(cwd: Path | str | None = None) -> None:
    """扫描 ``<cwd>/.orca/profiles/*.py``，覆盖 builtin。

    ``HARNESS_DISABLE_PROJECT_PROFILES=1`` 时整体跳过（env 名沿用 AgentHarness 约定）。
    损坏文件 → disable + fail loud（不静默丢）。
    """
    if os.environ.get(_DISABLE_PROJECT_PROFILES_ENV, "") == "1":
        return
    base = Path(cwd) if cwd is not None else Path.cwd()
    project_dir = base / ".orca" / "profiles"
    if not project_dir.is_dir():
        return
    for path in sorted(project_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        _load_profile_file(path, source="project")


def _ensure_loaded() -> None:
    """惰性加载：首次访问注册表时确保 builtin 已加载。project 由调用方显式触发。"""
    load_builtin_profiles()


def get_profile(name: str) -> CliProfile:
    """查注册表。不存在 / disabled → ValueError（含原因，fail loud）。

    若 builtin 尚未加载，惰性触发 ``load_builtin_profiles``（project 不自动加载 ——
    project 覆盖需 cwd 上下文，由 CLI 层显式调用 ``load_project_profiles``）。
    """
    _ensure_loaded()
    if name in _DISABLED:
        raise ValueError(
            f"profile '{name}' 不可用：已 disable（{_DISABLED[name]}）"
        )
    if name not in _REGISTRY:
        raise ValueError(
            f"未知 executor '{name}'：无匹配 profile（available: "
            f"{sorted(_REGISTRY.keys()) or '<empty>'}）"
        )
    return _REGISTRY[name]


def register(profile: CliProfile) -> None:
    """注册（或覆盖）一个 profile。project 覆盖 builtin 即靠此。"""
    _REGISTRY[profile.name] = profile
    # 若此前被 disable 过，注册成功即恢复（清除 disable 标记）
    _DISABLED.pop(profile.name, None)


def disable_profile(name: str, reason: str) -> None:
    """标记某 profile 不可用。``get_profile`` 会抛带 reason 的 ValueError。"""
    _DISABLED[name] = reason
    _REGISTRY.pop(name, None)


def available_profiles() -> list[str]:
    """返回当前可用的 profile 名（已加载、未被 disable）。"""
    _ensure_loaded()
    return sorted(_REGISTRY.keys())


def _reset_for_test() -> None:
    """测试专用：清空注册表与 disable 表，重置 builtin 加载标记。

    用于隔离不同 test 的 registry 状态（避免测试间相互污染）。
    """
    global _BUILTIN_LOADED
    _REGISTRY.clear()
    _DISABLED.clear()
    _BUILTIN_LOADED = False

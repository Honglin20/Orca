"""config.py —— per-profile spawn 参数持久化（binary / flags / prompt_channel）。

回答「怎么让用户设一次 backend 命令、之后所有 ``orca run`` 全局生效？+ 换平台怎么跟项目走？」：
把 per-profile 的 spawn 参数 override 写进 config.json，启动期（``main()``）注入对应 env var
（``ORCA_CLAUDE_CLI`` / ``ORCA_CLAUDE_FLAGS`` / ``ORCA_CLAUDE_PROMPT_CHANNEL`` 等）。现有
``CliProfile.resolve_cli_path() / resolve_flags() / resolve_prompt_channel()`` 运行时读 env，
故整个 exec/profile/registry 链路零改动（OCP）。

**两层 config（per-field project 覆盖 user）**：
  - 用户级 ``~/.orca/config.json``：个人默认（如自建 binary 路径）。
  - 项目级 ``<cwd>/.orca/config.json``：平台/仓库特定（如「本仓用 nga，flags 不带 skip-permissions」），
    可 check-in 共享。per-field 覆盖用户级（不是整份替换）。

**优先级**（per-profile per-field，多 fallback 生效只一份）::

      shell env  >  项目 .orca/config.json  >  用户 ~/.orca/config.json  >  profile default

  用 ``os.environ.setdefault`` 实现——显式 ``export`` 永远赢；project 值在 merge 阶段已覆盖 user，
  故 setdefault 注入的是 project-wins 后的值。

格式（JSON，``requires-python>=3.10`` 无 stdlib tomllib）::

    {
      "binaries":       {"opencode": "nga"},
      "flags":          {"opencode": "run --format json"},
      "prompt_channel": {"opencode": "argv"}
    }

  三个 dict 均可缺省（缺 = 该维无 override，走 profile default）。

**shell env 快照**：首次 ``bootstrap_config()`` 前抓 ``ORCA_*`` env 快照，供 ``executor show`` 区分
「shell export」与「config 注入」两层来源（注入后 os.environ 已被污染，无法事后区分）。快照只用于
展示，不影响 spawn。

依赖单向：本模块依赖 ``orca.profiles.registry``（iface → profiles 合法方向）。**禁止** import
``orca.exec``——本模块在 exec 启动前被 ``main()`` 调用，import exec 会引入循环与启动期副作用。
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from pathlib import Path
from typing import Any

from orca.profiles.registry import (
    get_profile,
    load_builtin_profiles,
    load_project_profiles,
)

logger = logging.getLogger(__name__)

# config.json 里 per-profile 管理的三个 spawn 参数维度（key 名）。
# 与 CliProfile 的 resolve_cli_path / resolve_flags / resolve_prompt_channel 一一对应。
CONFIG_FIELDS: tuple[str, ...] = ("binaries", "flags", "prompt_channel")

# prompt_channel 合法值（与 base.py _VALID_PROMPT_CHANNELS 同步；这里独立声明避免反向 import）。
_VALID_PROMPT_CHANNELS: frozenset[str] = frozenset({"stdin", "argv"})

# 首次 bootstrap 前的 shell env 快照（仅 ORCA_* 前缀）。
# None = 尚未 bootstrap（show 此时无法判 env 来源，退化到只看 config）。
_shell_env_snapshot: dict[str, str] | None = None


def config_path() -> Path:
    """``~/.orca/config.json`` 路径（用户级，与 CWD 无关）。"""
    return Path.home() / ".orca" / "config.json"


def project_config_path() -> Path:
    """``<cwd>/.orca/config.json`` 路径（项目级，跟工作目录走）。"""
    return Path.cwd() / ".orca" / "config.json"


def _load_config_file(path: Path) -> dict[str, Any]:
    """读单个 config 文件。缺失 → ``{}``；损坏 / 非 object → warn + ``{}``（不崩）。

    损坏文件不应阻断 ``orca`` 启动——降级为空配置（用 default），同时打 warning 让用户可见
    （fail loud 但不 fatal）。
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("config 文件损坏，已忽略并降级为空配置（%s）：%s", path, e)
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "config 顶层不是 JSON object（got %s），已忽略并降级为空配置：%s",
            type(data).__name__, path,
        )
        return {}
    # 已知 spawn 字段必须是 dict（per-profile 映射）；用户手改坏（如 list/str）→ warn + 丢弃
    # 该字段（fail loud 但不阻断其余字段）。集中校验，set/unset 无需各再审（DRY）。
    for field in CONFIG_FIELDS:
        if field in data and not isinstance(data[field], dict):
            logger.warning(
                "config.%s 非 object（got %s），已忽略该字段：%s",
                field, type(data[field]).__name__, path,
            )
            data.pop(field)
    return data


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """读 config。``path=None`` → 用户级 ``~/.orca/config.json``。"""
    return _load_config_file(Path(path) if path is not None else config_path())


def save_config(cfg: dict[str, Any], path: Path | str | None = None) -> None:
    """原子写 config（tmp + ``os.replace``，对齐 ``bg_runner.write_meta`` 模式）。

    ``path=None`` → 用户级 ``~/.orca/config.json``。rename 在同 filesystem 内原子，避免并发
    ``orca`` 进程半读。
    """
    target = Path(path) if path is not None else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, target)


def load_merged_config() -> dict[str, Any]:
    """合并用户级 + 项目级 config：**per-field project 覆盖 user**（非整份替换）。

    仅对 ``CONFIG_FIELDS`` 三个 dict key 做 per-profile 合并（project 的 ``opencode`` 覆盖 user
    的 ``opencode``，但 user 的 ``claude`` 保留）。其余 key（未知）从 user 透传，project 的未知
    key 忽略（保守：只认已知 spawn 字段）。

    缺失/损坏的文件降级为 ``{}``（见 ``_load_config_file``），不阻断合并。
    """
    user = _load_config_file(config_path())
    project = _load_config_file(project_config_path())
    merged: dict[str, Any] = dict(user)  # 未知 key 从 user 透传
    for field in CONFIG_FIELDS:
        u = user.get(field)
        p = project.get(field)
        u_dict = u if isinstance(u, dict) else {}
        p_dict = p if isinstance(p, dict) else {}
        merged[field] = {**u_dict, **p_dict}  # project per-profile 覆盖 user
    return merged


def _normalize_flags_to_str(
    profile_name: str, value: Any
) -> str | None:
    """flags override 归一成 env 串（``resolve_flags`` 会再 shlex.split 回去）。

    config 里 flags 可存 **list**（规范，JSON-natural：``["run","--format","json"]``）或
    **string**（手写容错：``"run --format json"``）。list → 空格 join；string → 原样。
    其他类型 → warn + None（跳过）。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return " ".join(value)
    logger.warning(
        "config 中 %r 的 flags=%r 非 str/list，已跳过", profile_name, value
    )
    return None


def _inject(env_name: str, profile_name: str, value: Any, *, kind: str) -> None:
    """``setdefault`` 一个 env。非 str 值 → warn + skip（fail loud 不静默吞）。

    value 为 None（字段未设）→ 静默跳过（正常：该维无 override）。
    """
    if value is None:
        return
    if not isinstance(value, str):
        logger.warning(
            "config 中 %r 的 %s=%r 非字符串，已跳过", profile_name, kind, value
        )
        return
    if env_name:
        os.environ.setdefault(env_name, value)


def apply_config_env(cfg: dict[str, Any]) -> None:
    """把 cfg 的三个 dict 注入对应 env var（``os.environ.setdefault``）。

    - **先抓 shell env 快照**（guarded，仅首次）：在任何 ``setdefault`` 注入**之前**捕获
      ``ORCA_*`` env 子集，供 ``executor show`` 区分「shell export」与「config 注入」两层来源。
      放在 ``apply_config_env`` 开头（而非 ``bootstrap_config``），保证任何入口（main / handler /
      测试直调）下都在首次注入前抓取，时序无关。
    - 再 ``load_builtin_profiles`` + ``load_project_profiles(cwd)``：支持 project profile
      覆盖 builtin（gotcha G8），且确保 profile 名能解析到 env 通道字段。
    - 未知 / disabled profile → warn + skip（fail loud 但不阻断启动）。
    - flags：list/string 均可（``_normalize_flags_to_str`` 归一）；其他非 str → warn + skip。
    - prompt_channel 非法值（非 stdin/argv）→ warn + skip（resolve 层二次校验防 shell 直填）。
    - 用 ``setdefault``：保 env > config 优先级（显式 ``export`` 永远赢）。
    """
    global _shell_env_snapshot
    if _shell_env_snapshot is None:
        _shell_env_snapshot = {
            k: v for k, v in os.environ.items() if k.startswith("ORCA_")
        }
    load_builtin_profiles()
    load_project_profiles(Path.cwd())

    # 收集所有被任一字段提及的 profile 名。
    names: set[str] = set()
    for field in CONFIG_FIELDS:
        d = cfg.get(field)
        if isinstance(d, dict):
            for k in d.keys():
                if isinstance(k, str):
                    names.add(k)

    for name in names:
        try:
            profile = get_profile(name)
        except ValueError as e:
            logger.warning("config 中 profile '%s' 跳过注入：%s", name, e)
            continue
        # binary（str）
        _inject(
            profile.cli_path_env, name, cfg.get("binaries", {}).get(name), kind="binary"
        )
        # flags（list/string 均可，归一成 str 注入）
        if profile.flags_env:
            flags_str = _normalize_flags_to_str(name, cfg.get("flags", {}).get(name))
            _inject(profile.flags_env, name, flags_str, kind="flags")
        # prompt_channel（仅当 profile 开了通道 + 值合法）
        if profile.prompt_channel_env:
            pc = cfg.get("prompt_channel", {}).get(name)
            if isinstance(pc, str) and pc not in _VALID_PROMPT_CHANNELS:
                logger.warning(
                    "config 中 %r 的 prompt_channel=%r 非法（必须 stdin|argv），跳过注入",
                    name, pc,
                )
                pc = None
            _inject(profile.prompt_channel_env, name, pc, kind="prompt_channel")


def bootstrap_config() -> None:
    """读 merged config 并注入 env。供 ``main()`` + 各 ``executor`` 子命令调用。

    幂等：``setdefault`` 已设的 env 不覆盖，重复调用 no-op。shell env 快照在
    ``apply_config_env`` 内捕获（首次注入前），故 ``bootstrap_config`` 只需转发。
    """
    apply_config_env(load_merged_config())


def shell_env_snapshot() -> dict[str, str]:
    """启动期 shell env 快照（仅 ``ORCA_*`` 前缀），供 ``executor show`` 判 env 来源。

    返回首次 ``bootstrap_config()`` 调用**前**的 shell env 子集（即用户真实 ``export`` 的值，
    不含 config 注入）。未 bootstrap 过 → ``{}``（show 此时无法区分 env 层，退化到只看 config）。
    """
    return dict(_shell_env_snapshot) if _shell_env_snapshot is not None else {}


def list_overrides(cfg: dict[str, Any]) -> set[str]:
    """返回有任意字段 override 的 profile 名集合（用于 ``executor list`` 的 ``*`` 标记）。

    与 ``apply_config_env`` 同样的 dict 校验，但不 warn——纯展示用。返回 ``set``（仅名集合）；
    per-field 来源判定在 ``executor show`` 内做（需区分 env/项目/用户/default 四层）。
    """
    names: set[str] = set()
    for field in CONFIG_FIELDS:
        d = cfg.get(field)
        if isinstance(d, dict):
            names.update(d.keys())
    return names

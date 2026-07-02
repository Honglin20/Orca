"""config.py —— ``~/.orca/config.json`` 持久化后端二进制配置。

回答「怎么让用户设一次 binary、之后所有 ``orca run`` 全局生效？」：把 per-profile 的
binary override 写进 ``~/.orca/config.json``，在 ``orca`` 启动期（``main()``）把它注入到
对应 env var（``ORCA_CLAUDE_CLI`` 等）。现有 ``CliProfile.resolve_cli_path()`` 运行时读
env，故整个 exec/profile/registry 链路零改动（OCP）。

**优先级**：shell env > config 文件 > profile default。用 ``os.environ.setdefault()`` 实现
——显式 ``export`` 永远赢，config 是中间层 fallback。

文件位置：``~/.orca/config.json``（与 ``~/.orca/runs/`` 同源约定，``bg_runner.py:46``）。
格式（JSON，``requires-python>=3.10`` 无 stdlib tomllib）::

    { "binaries": { "claude": "ccr code" } }

依赖单向：本模块依赖 ``orca.profiles.registry``（iface → profiles 合法方向）。**禁止**
import ``orca.exec``——本模块在 exec 启动前被 ``main()`` 调用，import exec 会引入循环与
启动期副作用。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from orca.profiles.registry import (
    get_profile,
    load_builtin_profiles,
    load_project_profiles,
)

logger = logging.getLogger(__name__)


def config_path() -> Path:
    """``~/.orca/config.json`` 路径（用户级，与 CWD 无关）。"""
    return Path.home() / ".orca" / "config.json"


def load_config() -> dict[str, Any]:
    """读 config.json。文件缺失 → ``{}``；损坏 → warn + ``{}``（不崩）。

    损坏文件不应阻断 ``orca`` 启动——降级为空配置（用 default binary），同时打 warning
    让用户可见（fail loud 但不 fatal）。
    """
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "config 文件损坏，已忽略并降级为空配置（%s）：%s", config_path(), e
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "config 顶层不是 JSON object（got %s），已忽略并降级为空配置",
            type(data).__name__,
        )
        return {}
    return data


def save_config(cfg: dict[str, Any]) -> None:
    """原子写 config.json（tmp + ``os.replace``，对齐 ``bg_runner.write_meta`` 模式）。

    rename 在同 filesystem 内原子，避免并发 ``orca`` 进程半读。
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def apply_config_env(cfg: dict[str, Any]) -> None:
    """把 ``cfg["binaries"]`` 注入对应 env var（``os.environ.setdefault``）。

    - 先 ``load_builtin_profiles`` + ``load_project_profiles(cwd)``：支持 project profile
      覆盖 builtin（gotcha G8），且确保 profile 名能解析到 ``cli_path_env``。
    - 未知 / disabled profile → warn + skip（try/except ValueError，对齐 registry
      ``disable_profile`` 风格，fail loud 但不阻断启动）。
    - 用 ``setdefault`` 而非 ``=``：保 env > config 优先级（显式 ``export`` 永远赢）。
    """
    load_builtin_profiles()
    load_project_profiles(Path.cwd())

    binaries = cfg.get("binaries")
    if not isinstance(binaries, dict):
        return  # 无 binaries 配置或格式错 → 无需注入（默认走 profile default）

    for name, binary in binaries.items():
        if not isinstance(name, str) or not isinstance(binary, str):
            logger.warning(
                "config.binaries 存在非字符串项（%r=%r），已跳过", name, binary
            )
            continue
        try:
            profile = get_profile(name)
        except ValueError as e:
            # 未知 / disabled profile：warn + skip，不阻断（fail loud 但可恢复）
            logger.warning("config 中 profile '%s' 跳过注入：%s", name, e)
            continue
        # setdefault：已存在的 env 不覆盖（保 env > config 优先级）。
        # 假设各 profile 的 cli_path_env 互不冲突（builtin 满足：claude→ORCA_CLAUDE_CLI、
        # ccr→ORCA_CCR_CLI）。若 project profile 复用 builtin 的 env 名，第二个 setdefault
        # 会静默 no-op——当前无检测，依赖 profile 作者避免重名。
        os.environ.setdefault(profile.cli_path_env, binary)


def bootstrap_config() -> None:
    """读 config 并注入 env。供 ``main()`` + 各 ``executor`` 子命令调用。

    幂等：``setdefault`` 已设的 env 不覆盖，重复调用 no-op（gotcha G1：单测用 CliRunner
    绕过 ``main()``，故 executor 子命令也显式调一次）。
    """
    apply_config_env(load_config())


def list_overrides(cfg: dict[str, Any]) -> dict[str, str]:
    """从 cfg 提取合法的 ``{profile: binary}`` 映射（用于 show/list 展示）。

    与 ``apply_config_env`` 同样的校验（仅取字符串项），但不 warn——纯展示用。
    """
    binaries = cfg.get("binaries")
    if not isinstance(binaries, dict):
        return {}
    return {
        name: binary
        for name, binary in binaries.items()
        if isinstance(name, str) and isinstance(binary, str)
    }

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
      "prompt_channel": {"opencode": "argv"},
      "sidechain":      {"family": "cac"}
    }

  三个 spawn 维度 dict（``binaries`` / ``flags`` / ``prompt_channel``）均可缺省
  （缺 = 该维无 override，走 profile default）。

  ``sidechain``（SPEC §P4，**独立于 ``CONFIG_FIELDS`` 三 spawn 维度**）：
  sidechain 是路径解析维度，不是 spawn 参数维度——由 iface 层（``cli._spawn_sidechain_daemon``
  / ``doctor``）直接读 ``load_merged_config().get("sidechain")``，**不**经
  ``CONFIG_FIELDS`` / ``apply_config_env`` / env 注入流程。``load_merged_config`` 透传未知
  key（``dict(user)`` 起手），``sidechain`` 作为 user/project config 的未知 key 自动透传。
  字段（均可缺省）：
    - ``family`` (str): 家族覆盖，``"cc"`` / ``"cac"`` / ``"opencode"`` / ``"nga"``。
      设置后 daemon 把 family 经 argv ``--family`` 透传给 adapter resolver，覆盖 dotdir 探测
      （歧义默认 .claude / .opencode，SPEC §P4 验收 #2）。合法性由 resolver 校验，**此处不校验**。

  注：host_session 仍只从 env 读（``_host_session_from_env`` 零改，SPEC §P4 P0-7 spike 前置）；
  config 不提供 host_session fallback——避免文档化未实装的 hook（YAGNI：spike 确认 cac env 行为
  后再加）。doctor hint 据此只指引 ``--host-session`` argv（真实可工作路径），不引导用户走不通的
  config 字段。

  ``knowledge_base_dir``（plan sprightly-questing-donut §1.3，**独立于 ``CONFIG_FIELDS``**）：
  KB 根目录自定义路径（字符串标量），同 ``sidechain`` 是路径解析维度，project 覆盖 user。由
  ``resolve_kb_dir`` 读取（env > config > ``~/.orca/knowledge_base`` > ``cwd/knowledge_base``），
  解析结果作 exec spawn 的 ``ORCA_KB_DIR`` transport。

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
    的 ``opencode``，但 user 的 ``claude`` 保留）。``sidechain`` 同样 project 覆盖 user 合并
    （路径解析维度，需两层都生效——否则写 project 级 ``sidechain.family`` 不生效）。其余未知
    key 只从 user 透传，project 的未知 key 忽略（保守：只认已知字段）。

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
    # sidechain：project 覆盖 user（与 spawn 维度同语义；sidechain.family 写 project 级也生效）。
    sc_u = user.get("sidechain")
    sc_p = project.get("sidechain")
    if isinstance(sc_u, dict) or isinstance(sc_p, dict):
        merged["sidechain"] = {
            **(sc_u if isinstance(sc_u, dict) else {}),
            **(sc_p if isinstance(sc_p, dict) else {}),
        }
    # knowledge_base_dir：路径解析维度（字符串标量，同 sidechain 独立于 CONFIG_FIELDS），
    # project 覆盖 user。KB 根目录自定义路径，供 ``resolve_kb_dir`` 读取（KB 可移植发现）。
    kbd_p = project.get("knowledge_base_dir")
    if isinstance(kbd_p, str) and kbd_p.strip():
        merged["knowledge_base_dir"] = kbd_p.strip()
    return merged


def sidechain_family(cfg: dict[str, Any]) -> str | None:
    """从（merged）config 读 ``sidechain.family``（SPEC §P4；纯函数，零副作用）。

    ``load_merged_config`` 已透传未知 key；``sidechain`` 是路径解析维度，独立于 ``CONFIG_FIELDS``
    三 spawn 维度。两处 caller 共享本函数避免漂移：``in_session.cli._read_sidechain_family_from_config``
    与 ``in_session.sidechain_cmds``（``orca sidechain family`` 命令）。

    Returns:
        family 字符串（"cc"/"cac"/"opencode"/"nga"，由用户填）或 None（未设）。**不做合法性
        校验**——resolver 会 raise ValueError，doctor 会报 fail；caller 仅透传。
    """
    sidechain = cfg.get("sidechain")
    if not isinstance(sidechain, dict):
        return None
    fam = sidechain.get("family")
    return fam if isinstance(fam, str) else None


def resolve_kb_dir() -> str:
    """解析 workflow 知识库（KB）根目录绝对路径（KB 可移植发现，plan sprightly-questing-donut §1.2）。

    回答「换项目跑 struct-exploration 时，``knowledge_base/`` 裸相对路径找不到怎么办」：KB 根
    不再靠 workflow 里的裸相对串（由 LLM agent 按 CWD 解析），而是由本函数确定性解析为绝对路径，
    caller（``orca run`` 预检 / in_session ``_write_env_script``）写入 ``os.environ["ORCA_KB_DIR"]``
    或 ``orca_env.sh``，作 exec 层 spawn 的 transport——exec 不 import iface（依赖单向），经 env 取值。

    优先级（**first-existing**）::

        shell env ORCA_KB_DIR  >  config knowledge_base_dir(project>user)  >
        ~/.orca/knowledge_base（``orca install`` 部署点）  >  <cwd>/knowledge_base（仓库根 fallback）

    - **显式来源（env / config）权威**：设了就用；指向的目录**不存在 → 返回 ""**（不静默回退到
      隐式来源——让 run 启动预检 fail-loud 暴露「用户配了错路径」，而非悄悄用错 KB）。
    - **隐式来源（install 部署点 / 仓库根）**：best-effort first-existing，全 miss → ""。
    - 全 miss → 返回 ""，由 caller（workflow 声明 ``requires:[knowledge_base]`` 时）fail-loud。

    路径解析维度（同 ``sidechain``）：**不进** ``CONFIG_FIELDS`` / ``apply_config_env`` / env 注入；
    本函数是唯一解析点（DRY），caller 直读。
    """
    # 1. 显式：shell env ORCA_KB_DIR（用户 export / 上层 orca 进程注入）
    env_kb = os.environ.get("ORCA_KB_DIR", "").strip()
    if env_kb:
        p = Path(env_kb)
        return str(p.resolve()) if p.is_dir() else ""
    # 2. 显式：config knowledge_base_dir（project > user，load_merged_config 已合并）
    cfg_kb = load_merged_config().get("knowledge_base_dir")
    if isinstance(cfg_kb, str) and cfg_kb.strip():
        p = Path(cfg_kb.strip())
        return str(p.resolve()) if p.is_dir() else ""
    # 3-4. 隐式：install 部署点 > 仓库根（best-effort first-existing）
    for candidate in (Path.home() / ".orca" / "knowledge_base", Path.cwd() / "knowledge_base"):
        if candidate.is_dir():
            return str(candidate.resolve())
    return ""


def apply_kb_requirement(wf) -> None:
    """plan sprightly-questing-donut §1.4：workflow 声明 ``requires:[knowledge_base]`` 时预检 KB。

    回答「KB 找不到时为何不能静默继续优化」：KB 是结构搜索的知识来源（latency_moves/directions），
    缺了 hypothesizer 只能凭参数化知识（甚至幻觉）改超参——故缺 KB 必须 fail loud 明确告知用户，
    而非像旧版 setup agent 静默继续。

    - 无 ``knowledge_base`` 依赖 → no-op（绝大多数 workflow 不碰 KB，零回归）。
    - 有依赖 + ``resolve_kb_dir`` 解析到 → 写 ``os.environ["ORCA_KB_DIR"]`` 作 exec spawn transport
      （executor/script 读 os.environ，不经构造参数穿透，避免 exec→iface 反向依赖）。
    - 有依赖 + 解析不到 → ``ConfigurationError``（fail loud，含 searched 路径 + 修复指引）。

    raise ``ConfigurationError`` 而非 ask-user 哨兵：KB 缺失是环境/安装缺口（用户该去装/配），不是
    「agent 缺一个用户知道的项目事实」（那是哨兵场景，见 docs/specs/agent-ask-user-sentinel.md）。

    被 ``orca run`` 各路径（``orca/iface/cli/commands.py``）与 in_session bootstrap
    （``orca/iface/in_session/cli.py``）共用——KB 预检单一真相源（DRY）。
    """
    if "knowledge_base" not in getattr(wf, "requires", []):
        return
    kb = resolve_kb_dir()
    if not kb:
        # lazy import：避免 config import 期拉 orca.compile 全树（config 被 bootstrap 早期加载）。
        from orca.compile import ConfigurationError
        raise ConfigurationError(
            errors=[
                "知识库（knowledge_base）未找到：本 workflow 声明 requires:[knowledge_base]，"
                "但 KB 根目录解析失败，无法继续。\n"
                "  搜索顺序：env ORCA_KB_DIR > config knowledge_base_dir(project>user) > "
                "~/.orca/knowledge_base > <cwd>/knowledge_base\n"
                "  修复任一：① 跑 `orca install`（部署内置 KB 到 ~/.orca/knowledge_base）；"
                "② 在 ~/.orca/config.json 设 \"knowledge_base_dir\": \"<KB 绝对路径>\"；"
                "③ export ORCA_KB_DIR=<KB 绝对路径>。"
            ],
            warnings=[],
        )
    os.environ["ORCA_KB_DIR"] = kb  # exec spawn transport（ClaudeExecutor/ScriptExecutor 读 os.environ）


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
    # lazy import：避免 ``import orca.iface.cli.config`` 触发 profiles.registry 加载（~1s，
    # 拖慢所有 import config 的模块——含 sidechain 守护启动，致 pidfile 迟写 / liveness 误判）。
    # profiles 三符号仅本函数用，移入函数内安全。
    from orca.profiles.registry import (
        get_profile, load_builtin_profiles, load_project_profiles,
    )
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

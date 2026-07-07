"""executor_cmds.py —— ``orca executor`` 子命令组：后端命令唯一真相源 + spawn 参数配置。

回答「用户怎么看到 backend 最终拼出什么命令、怎么改它（binary/flags/prompt_channel）？」：
``orca executor show`` 打印**唯一真相源**——完整生效 argv + 每字段来源（env/项目/用户/default）；
``orca executor set --binary/--flags/--prompt-channel`` 写 config.json（项目或用户级），之后所有
``orca run`` spawn 该生效命令。

**一套接口**：show（读真相源）+ set/unset（写，三维任组）+ list + test。不拆 set-binary /
set-flags / set-prompt-channel——三维统一在 ``set`` 的 named option 里。

**两层 config + 优先级**（详见 ``config.py``）::

    shell env > 项目 .orca/config.json > 用户 ~/.orca/config.json > profile default

  per-field project 覆盖 user（非整份替换）。生效只一份，show 标注谁赢。

**sub-Typer 形态**：``app.add_typer(executor_app, name="executor")``。

**gotcha**：
  - G1：每个 handler 内部先调 ``bootstrap_config()``（CliRunner 单测绕过 ``main()``）。
  - G4：``test`` 外层 ``asyncio.wait_for(60s)`` 硬上限，区别于逐行 ``SpawnConfig.timeout=30``。
  - G5：spawn 失败（``FileNotFoundError``/``OSError``）→ 干净 FAIL exit 1。
  - G6：show 的 env 层来源靠 ``shell_env_snapshot()``（首次 bootstrap 前快照），区分 shell export
    vs config 注入——注入后 os.environ 已污染，无法事后区分。

依赖单向：本模块 import ``orca.exec.runner``（iface → exec，合法方向）+ ``orca.exec.env``
+ ``orca.profiles.registry`` + ``orca.iface.cli.config``。**不**反向依赖。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from typing import Any

import typer

from orca.iface.cli import config as config_mod
from orca.iface.cli.config import (
    bootstrap_config,
    list_overrides,
    load_config,
    load_merged_config,
    save_config,
    shell_env_snapshot,
)
from orca.profiles.registry import available_profiles, get_profile

logger = logging.getLogger(__name__)

# 外层 wall-clock 硬上限（gotcha G4）：与逐行 stall 检测（SpawnConfig.timeout=30）区分。
# 即使二进制永不 EOF 但持续吐行（绕过逐行 timeout），wall-clock 兜底保证 test 不挂死。
_TEST_WALL_CLOCK_TIMEOUT = 60.0

app = typer.Typer(
    name="executor",
    no_args_is_help=True,
    help="agent 后端命令配置与健康检查（binary / flags / prompt_channel）。",
)

# ── spawn 字段元数据（show / set / unset 共享）─────────────────────────────────
# field 名 → (profile 上的 env 通道属性名, default 值属性名, config.json 的 key)。
# flags 的 default 是 tuple，展示时 join 成串（见 _display_value）。
_FIELD_META: dict[str, tuple[str, str, str]] = {
    "binary": ("cli_path_env", "default_cli_path", "binaries"),
    "flags": ("flags_env", "flags", "flags"),
    "prompt_channel": ("prompt_channel_env", "prompt_channel", "prompt_channel"),
}
# CLI field 名 → config.json key（unset 的 field 参数映射）。
_FIELD_TO_CONFIG_KEY = {f: meta[2] for f, meta in _FIELD_META.items()}
_VALID_FIELDS = set(_FIELD_META.keys()) | {"all"}


# ── 可测性 seam：判定逻辑抽纯函数（gotcha R3）─────────────────────────────────


def classify(
    seen_types: set[str],
    saw_result: bool,
    exit_code: int,
    timed_out: bool,
    stderr: str,
) -> tuple[bool, str]:
    """根据 CLIRunner 运行特征判定 PASS/FAIL + 人类可读消息（纯函数，单测友好）。

    判定顺序（对齐 SPEC §2.4 有序错误判定）：
      1. ``timed_out`` → FAIL「超时」
      2. 收到了 stream-json 事件但 ``exit_code != 0`` 且无 result 行 → FAIL「退出码非 0」
      3. 完全没有 stream-json 协议事件 → FAIL「非 stream-json / 协议不兼容」（附 stderr 片段）
      4. ``saw_result`` → PASS「端到端 OK」
      5. 有事件、exit=0 但无 result → PASS + warn「流正常但未收到 result 行」

    Args:
        seen_types: 本次运行观察到的 stdout 行顶层 ``type`` 集合。
        saw_result: 是否检测到 ``type=result`` 行。
        exit_code: 子进程退出码（``-1`` = 未知 / 被强杀）。
        timed_out: 是否触发超时。
        stderr: 子进程 stderr（用于 FAIL 消息诊断）。
    """
    if timed_out:
        return False, f"✗ 超时（{int(_TEST_WALL_CLOCK_TIMEOUT)}s 内未完成）"

    # stream-json 协议已知事件 type（出现任意一个即认定二进制在吐 stream-json）。
    stream_event_types = {"stream_event", "assistant", "user", "result", "system"}
    has_stream_events = bool(seen_types & stream_event_types)

    if has_stream_events and exit_code != 0 and not saw_result:
        return False, f"✗ 退出码 {exit_code}（未收到 result 行，疑似错误退出）"

    if not has_stream_events:
        snippet = stderr.strip()[:500] if stderr.strip() else "<无 stderr 输出>"
        return False, f"✗ 非 stream-json / 协议不兼容。stderr：{snippet}"

    if saw_result:
        return True, "✓ 端到端 OK（收到 result 行）"

    # 有事件、exit=0、无 result：流是活的但 result 行丢失——判 PASS + warn（疑似降级）。
    return True, "✓ PASS（warn：流正常但未收到 result 行）"


def _record_type(line: str, seen_types: set[str]) -> None:
    """解析 ``line`` 顶层 ``type`` 并收集（非 JSON 行跳过）。

    CLIRunner 已对非 JSON 心跳行 debug log + 跳过；此处防御性 try/except 不抛。
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    if isinstance(obj, dict) and isinstance(obj.get("type"), str):
        seen_types.add(obj["type"])


# ── 来源解析（show 的核心：唯一真相源 + 每字段来源标注）────────────────────────


def _cfg_value_display(field: str, value: Any) -> str | None:
    """config/env 里某字段的值 → 展示串。``None`` 表示无 override（或缺省/非法，跳过）。

    - flags：list（规范）→ join；string（手写容错）→ shlex 归一 join。
    - binary / prompt_channel：仅 str。
    """
    if field == "flags":
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            return " ".join(value)
        if isinstance(value, str):
            return " ".join(shlex.split(value)) if value else ""
        return None
    if isinstance(value, str):
        return value
    return None


def _default_display(profile: Any, field: str) -> str:
    """profile 上某字段的 default 展示值（flags tuple → join）。"""
    raw = getattr(profile, _FIELD_META[field][1])
    if field == "flags":
        return " ".join(raw) if raw else ""
    return str(raw)


def _resolve_field_source(
    profile: Any,
    field: str,
    user_cfg: dict[str, Any],
    project_cfg: dict[str, Any],
) -> tuple[str, str]:
    """返回 (展示值, 来源标签)。

    来源标签 ∈ ``{"env", "项目", "用户", "default"}``，按优先级 shell env > 项目 > 用户 > default。
    env 层用 ``shell_env_snapshot()``（启动期快照，区分 shell export vs config 注入）。
    """
    env_attr, _, config_key = _FIELD_META[field]
    env_name = getattr(profile, env_attr)
    snap = shell_env_snapshot()

    # 1. shell env（启动期 export，最高优先级；env 值恒为 str）
    if env_name and env_name in snap:
        disp = _cfg_value_display(field, snap[env_name])
        if disp is not None:
            return disp, "env"

    # 2. 项目 config
    proj_dict = project_cfg.get(config_key)
    if isinstance(proj_dict, dict):
        disp = _cfg_value_display(field, proj_dict.get(profile.name))
        if disp is not None:
            return disp, "项目"

    # 3. 用户 config
    user_dict = user_cfg.get(config_key)
    if isinstance(user_dict, dict):
        disp = _cfg_value_display(field, user_dict.get(profile.name))
        if disp is not None:
            return disp, "用户"

    # 4. default
    return _default_display(profile, field), "default"


def _format_effective_command(
    binary_eff: str, flags_eff: str, prompt_channel_eff: str
) -> str:
    """拼「生效命令」展示串（唯一真相源）：``binary flags [prompt] [--model ...]``。"""
    argv = shlex.split(binary_eff) if binary_eff else []
    if flags_eff:
        argv.extend(shlex.split(flags_eff))
    cmd = " ".join(argv) if argv else "<空 binary>"
    if prompt_channel_eff == "argv":
        cmd += ' "<prompt>"'
    else:
        cmd += "  # prompt 经 stdin 传入"
    cmd += " [--model <node.model>]"
    return cmd


def _print_profile_block(
    profile: Any,
    user_cfg: dict[str, Any],
    project_cfg: dict[str, Any],
) -> None:
    """打印单个 profile 的完整真相源块（show 单个 / set 回打 共享）。"""
    typer.echo(f"Profile: {profile.name}")
    eff_vals: dict[str, str] = {}
    for field in ("binary", "flags", "prompt_channel"):
        eff, src = _resolve_field_source(profile, field, user_cfg, project_cfg)
        eff_vals[field] = eff
        default_val = _default_display(profile, field)
        extra = f"  (default: {default_val})" if src != "default" else ""
        typer.echo(f"  {field:<15} {eff}  ← {src}{extra}")
    typer.echo("  model           <node.model；None=不传 --model，后端默认>")
    typer.echo("▶ 生效命令（唯一真相源）:")
    typer.echo(
        f"  {_format_effective_command(eff_vals['binary'], eff_vals['flags'], eff_vals['prompt_channel'])}"
    )


# ── 命令 ──────────────────────────────────────────────────────────────────────


def _iter_available_profiles():
    """遍历可用 profile，对并发 disable 等 race 容错（DRY：show 共享）。

    yield ``(name, profile_or_None, error_or_None)``：profile 为 None 时 error 非 None，
    调用方自行决定如何展示 disabled 状态。
    """
    for name in available_profiles():
        try:
            p = get_profile(name)
        except ValueError as e:
            yield name, None, e
            continue
        yield name, p, None


@app.command(name="show")
def show(
    profile_name: str = typer.Argument(
        None, help="profile 名；省略则列全部 profile 的生效命令"
    ),
) -> None:
    """显示每个 profile 的完整生效命令（唯一真相源）+ 每字段来源标注。

    来源：shell env > 项目 .orca/config.json > 用户 ~/.orca/config.json > profile default。
    """
    bootstrap_config()
    user_cfg = load_config(config_mod.config_path())
    project_cfg = load_config(config_mod.project_config_path())

    typer.echo(f"配置：项目 {config_mod.project_config_path()}")
    typer.echo(f"      用户 {config_mod.config_path()}")
    typer.echo("优先级：shell env > 项目 > 用户 > profile default")
    typer.echo("")

    names = [profile_name] if profile_name else available_profiles()
    for name in names:
        try:
            p = get_profile(name)
        except ValueError as e:
            typer.echo(f"Profile: {name}  [disabled: {e}]")
            continue
        _print_profile_block(p, user_cfg, project_cfg)
        typer.echo("")


@app.command(name="set")
def set_profile(
    profile: str = typer.Argument(..., help="profile 名（如 claude / opencode / ccr）"),
    binary: str = typer.Option(
        None, "--binary", "-b", help="替换 binary（如 'ccr code' / 'nga'）"
    ),
    flags: str = typer.Option(
        None, "--flags", "-f", help='flags 字符串（如 "run --format json"）'
    ),
    prompt_channel: str = typer.Option(
        None, "--prompt-channel", "-p", help="prompt 投递方式：stdin | argv"
    ),
    scope: str = typer.Option(
        "project",
        "--scope",
        "-s",
        help="写到哪层 config：project（默认，.orca/config.json）| user（~/.orca/config.json）",
    ),
) -> None:
    """设置 profile 的 spawn 参数 override（binary / flags / prompt_channel 任意组合）。

    写完自动回打生效命令（唯一真相源）便于核对。三维统一在此命令，不拆成 set-binary 等。
    """
    bootstrap_config()
    # 校验 profile 存在且可用
    try:
        get_profile(profile)
    except ValueError as e:
        typer.echo(f"错误：{e}", err=True)
        raise typer.Exit(code=2) from e
    # 至少一个字段
    if binary is None and flags is None and prompt_channel is None:
        typer.echo("错误：至少指定 --binary / --flags / --prompt-channel 之一", err=True)
        raise typer.Exit(code=2)
    # prompt_channel 合法性
    if prompt_channel is not None and prompt_channel not in ("stdin", "argv"):
        typer.echo(
            f"错误：--prompt-channel 必须 stdin|argv（got {prompt_channel!r}）", err=True
        )
        raise typer.Exit(code=2)
    # scope 合法性
    if scope not in ("project", "user"):
        typer.echo(f"错误：--scope 必须 project|user（got {scope!r}）", err=True)
        raise typer.Exit(code=2)

    target = config_mod.project_config_path() if scope == "project" else config_mod.config_path()
    cfg = load_config(target)  # 已校验：非 dict 字段已 warn+丢弃，setdefault 安全
    if binary is not None:
        cfg.setdefault("binaries", {})[profile] = binary
    if flags is not None:
        # flags 存 list（规范，JSON-natural）；--flags 输入串 shlex.split 成 list。
        cfg.setdefault("flags", {})[profile] = shlex.split(flags)
    if prompt_channel is not None:
        cfg.setdefault("prompt_channel", {})[profile] = prompt_channel
    save_config(cfg, target)
    typer.echo(f"✓ 已写入 {target}（scope={scope}）")

    # 回打生效命令：重读两层 config（刚写的已落盘）+ 启动期 shell env 快照。
    # 不依赖注入后的 os.environ（setdefault 不覆盖旧值，会读到过期值）。
    typer.echo("")
    p = get_profile(profile)
    _print_profile_block(
        p, load_config(config_mod.config_path()), load_config(config_mod.project_config_path())
    )


@app.command(name="unset")
def unset_profile(
    profile: str = typer.Argument(..., help="要清除 override 的 profile 名"),
    field: str = typer.Argument(
        "all", help="binary | flags | prompt_channel | all（默认 all）"
    ),
    scope: str = typer.Option(
        "project", "--scope", "-s", help="清哪层 config：project（默认）| user"
    ),
) -> None:
    """移除 profile 的 spawn 参数 override（恢复 default）。

    field=all 清该 profile 全部三维；指定单字段只清那一维。
    """
    bootstrap_config()
    if field not in _VALID_FIELDS:
        typer.echo(
            f"错误：field 必须 {' | '.join(sorted(_VALID_FIELDS))}（got {field!r}）",
            err=True,
        )
        raise typer.Exit(code=2)
    if scope not in ("project", "user"):
        typer.echo(f"错误：--scope 必须 project|user（got {scope!r}）", err=True)
        raise typer.Exit(code=2)

    config_keys = (
        list(_FIELD_TO_CONFIG_KEY.values())
        if field == "all"
        else [_FIELD_TO_CONFIG_KEY[field]]
    )
    target = config_mod.project_config_path() if scope == "project" else config_mod.config_path()
    cfg = load_config(target)
    removed: list[str] = []
    for key in config_keys:
        d = cfg.get(key)
        if isinstance(d, dict) and profile in d:
            d.pop(profile)
            removed.append(key)
    if removed:
        save_config(cfg, target)
        typer.echo(f"✓ 已从 {target} 清除 {profile} 的：{', '.join(removed)}")
    else:
        typer.echo(
            f"{profile} 在 {target} 无 {field} override（已是 default）。"
        )


@app.command(name="list")
def list_profiles() -> None:
    """列出全部可用 profile + default binary / env 名 / override 标记。"""
    bootstrap_config()
    overrides = list_overrides(load_merged_config())
    typer.echo("可用 profiles：")
    for name, p, _err in _iter_available_profiles():
        if p is None:
            continue  # disabled profile 在 list 中省略
        mark = " *" if name in overrides else ""
        typer.echo(
            f"  {name:<10} default={p.default_cli_path!r}  env={p.cli_path_env}{mark}"
        )
    typer.echo("  (* = 任一字段被 config override；详情跑 `orca executor show <profile>`)")


@app.command(name="test")
def test_binary(
    profile: str = typer.Argument("claude", help="要测试的 profile 名"),
) -> None:
    """真起一个子进程，验证 ``profile`` 的 binary 能吐 claude stream-json。

    复用 ``SpawnConfig`` + ``CLIRunner``（无需 AgentNode）。判定逻辑见 ``classify``。
    """
    bootstrap_config()
    # 延迟 import：避免模块导入期触发 exec 依赖（对齐 commands.py 的延迟 import 纪律）。
    from orca.exec.env import build_env_overlay
    from orca.exec.runner import CLIRunner, SpawnConfig

    try:
        p = get_profile(profile)
    except ValueError as e:
        typer.echo(f"错误：{e}", err=True)
        raise typer.Exit(code=2) from e

    cfg = SpawnConfig(
        cli_path=p.resolve_cli_path(),
        flags=p.resolve_flags(),
        prompt="Reply with OK",
        prompt_channel=p.resolve_prompt_channel(),
        env_overlay=build_env_overlay(p.env_overlay_prefixes),
        timeout=30.0,  # 逐行 stall 检测（runner readline wait_for）
    )

    seen_types: set[str] = set()
    saw_result = False
    # holder 把 runner 引用外露给协程外（exit_code / stderr 在 runner 属性上）。
    holder: dict[str, Any] = {}

    def on_result(
        raw: str, usage: dict[str, Any], cost: float, is_error: bool, status: int | None
    ) -> None:
        nonlocal saw_result
        saw_result = True

    async def go() -> None:
        runner = CLIRunner(cfg, on_result=on_result)
        holder["runner"] = runner
        async for line in runner.stream():
            _record_type(line, seen_types)

    try:
        asyncio.run(asyncio.wait_for(go(), timeout=_TEST_WALL_CLOCK_TIMEOUT))
    except asyncio.TimeoutError:
        # 外层 wall-clock 兜底（gotcha G4）：子进程永不退出但持续吐行（绕过逐行 timeout）。
        runner = holder.get("runner")
        stderr = runner.stderr if runner is not None else ""
        verdict, msg = classify(seen_types, saw_result, -1, True, stderr)
        typer.echo(msg)
        raise typer.Exit(code=0 if verdict else 1)
    except (FileNotFoundError, PermissionError, OSError) as e:
        # spawn 前失败（gotcha G5）：二进制不存在等 → 干净 FAIL exit 1。
        typer.echo(f"✗ 二进制无法启动：{e}")
        raise typer.Exit(code=1) from e

    runner = holder.get("runner")
    exit_code = runner.exit_code if runner is not None else -1
    stderr = runner.stderr if runner is not None else ""
    # 内部逐行超时（SpawnConfig.timeout=30 触发 CLIRunner._handle_timeout）走「正常结束生成器
    # + 标记 timed_out=True」路径（runner.py:196-197 的 return），不抛异常给外层。故必须读
    # runner.timed_out 属性——否则卡死二进制（内部已 SIGTERM）会被误判为「非 stream-json」
    # 或「退出码 -1」，丢失「超时」诊断。
    internal_timed_out = runner.timed_out if runner is not None else False

    verdict, msg = classify(
        seen_types, saw_result, exit_code, internal_timed_out, stderr
    )
    typer.echo(msg)
    raise typer.Exit(code=0 if verdict else 1)

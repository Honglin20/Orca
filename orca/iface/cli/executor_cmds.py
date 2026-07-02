"""executor_cmds.py —— ``orca executor`` 子命令组：后端二进制配置与健康检查。

回答「用户怎么持久化切 backend binary、怎么自检能否真跑通？」：``orca executor set claude
"ccr code"`` 写 ``~/.orca/config.json``，之后所有 ``orca run`` spawn 该 binary；
``orca executor test`` 真起一个子进程验证它吐 claude stream-json。

**sub-Typer 形态**：``app.add_typer(executor_app, name="executor")``。代码库其余命令是扁平
``@app.command()``，此处是有据的局部偏离——``executor show/set/unset/list/test`` 共享
名词 ``executor``，sub-Typer 让 ``orca --help`` 更干净，UX 比扁平 ``executor-show`` 好。

**gotcha**：
  - G1：每个 handler 内部先调 ``bootstrap_config()``（CliRunner 单测绕过 ``main()``）。
  - G4：``test`` 外层 ``asyncio.wait_for(60s)`` 硬上限，区别于逐行 ``SpawnConfig.timeout=30``。
  - G5：spawn 失败（``FileNotFoundError``/``OSError``）→ 干净 FAIL exit 1。

依赖单向：本模块 import ``orca.exec.runner``（iface → exec，合法方向）+ ``orca.exec.env``
+ ``orca.profiles.registry``。**不**反向依赖。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import typer

from orca.iface.cli.config import (
    bootstrap_config,
    config_path,
    list_overrides,
    load_config,
    save_config,
)
from orca.profiles.registry import available_profiles, get_profile

logger = logging.getLogger(__name__)

# 外层 wall-clock 硬上限（gotcha G4）：与逐行 stall 检测（SpawnConfig.timeout=30）区分。
# 即使二进制永不 EOF 但持续吐行（绕过逐行 timeout），wall-clock 兜底保证 test 不挂死。
_TEST_WALL_CLOCK_TIMEOUT = 60.0

app = typer.Typer(
    name="executor",
    no_args_is_help=True,
    help="agent 后端二进制配置与健康检查。",
)


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


# ── 命令 ──────────────────────────────────────────────────────────────────────


def _iter_available_profiles():
    """遍历可用 profile，对并发 disable 等 race 容错（DRY：show/list 共享）。

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


def _binaries_dict(cfg: dict[str, Any]) -> dict[str, Any]:
    """从 cfg 取 ``binaries`` dict；非 dict（含缺失）→ 返回新 ``{}``。

    key 存在但值非 dict（用户手改坏 config，如 ``{"binaries": [...]}``）→ warn
    （对齐 ``load_config`` / ``apply_config_env`` 的 fail-loud 风格，不静默吞）。
    key 缺失 → 静默 ``{}``（正常：未配置任何 override）。

    set / unset 共享（DRY）：两处都要从 cfg 取 binaries 并对坏值容错。
    """
    raw = cfg.get("binaries")
    if isinstance(raw, dict):
        return raw
    if "binaries" in cfg:
        logger.warning(
            "config.binaries 非 object（got %s），已重置为 {}", type(raw).__name__
        )
    return {}


@app.command(name="show")
def show() -> None:
    """显示当前 config 内容 + 每个 profile 的 default / effective binary / env 名。"""
    bootstrap_config()
    cfg = load_config()
    overrides = list_overrides(cfg)

    typer.echo(f"配置文件：{config_path()}")
    typer.echo(f"内容：{cfg if cfg else '（空）'}")
    typer.echo("")
    typer.echo("Profiles：")
    for name, p, err in _iter_available_profiles():
        if err is not None:
            typer.echo(f"  {name}  [disabled: {err}]")
            continue
        effective = p.resolve_cli_path()
        override_mark = "（config override）" if name in overrides else ""
        typer.echo(
            f"  {name:<10} default={p.default_cli_path!r}  "
            f"effective={effective!r}  env={p.cli_path_env}  {override_mark}"
        )


@app.command(name="set")
def set_binary(
    profile: str = typer.Argument(..., help="profile 名（如 claude / ccr）"),
    binary: str = typer.Argument(..., help="替换后的 binary（如 'ccr code'）"),
) -> None:
    """设置 ``profile`` 的 binary override（写入 ``~/.orca/config.json``）。"""
    bootstrap_config()
    # 校验 profile 存在且可用（未知 → exit 2，fail loud）
    try:
        get_profile(profile)
    except ValueError as e:
        typer.echo(f"错误：{e}", err=True)
        raise typer.Exit(code=2) from e

    cfg = load_config()
    binaries = _binaries_dict(cfg)
    cfg["binaries"] = binaries  # 持久化（含对非 dict 坏值的清理）
    binaries[profile] = binary
    save_config(cfg)
    typer.echo(f"✓ 已设置 {profile} = {binary!r}（写入 {config_path()}）")
    typer.echo(f"  提示：跑 `orca executor test {profile}` 验证能否真跑通。")


@app.command(name="unset")
def unset_binary(
    profile: str = typer.Argument(..., help="要清除 override 的 profile 名"),
) -> None:
    """移除 ``profile`` 的 binary override（恢复 profile default）。"""
    bootstrap_config()
    cfg = load_config()
    binaries = _binaries_dict(cfg)
    if profile not in binaries:
        typer.echo(f"{profile} 无 config override（已是 default）。")
        return
    binaries.pop(profile)
    cfg["binaries"] = binaries
    save_config(cfg)
    try:
        default = get_profile(profile).default_cli_path
    except ValueError:
        default = "<unknown>"
    typer.echo(f"✓ 已清除 {profile} 的 override（恢复 default={default!r}）。")


@app.command(name="list")
def list_profiles() -> None:
    """列出全部可用 profile + default / override 标记 / env 名。"""
    bootstrap_config()
    overrides = list_overrides(load_config())
    typer.echo("可用 profiles：")
    for name, p, _err in _iter_available_profiles():
        if p is None:
            continue  # disabled profile 在 list 中省略（show 才详列 disabled 原因）
        mark = " *" if name in overrides else ""
        typer.echo(
            f"  {name:<10} default={p.default_cli_path!r}  "
            f"env={p.cli_path_env}{mark}"
        )
    typer.echo("  (* = 已被 config.json override)")


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
        flags=p.flags,
        prompt="Reply with OK",
        prompt_channel=p.prompt_channel,
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

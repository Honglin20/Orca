"""install_cmds.py —— ``orca install`` 统一安装入口（全局默认）。

回答「用户怎么把 Orca 的宿主集成一次性装好、且全局可用？」：``orca install`` 收口
**skill + in-session** 两类集成，默认装到用户级（``~/.config/opencode/`` / ``~/.claude/``），
一条命令替代此前碎片化的 ``orca skill install`` + ``orca in-session start``。

替代关系：
  - ``orca skill install`` → 降为弃用别名（warn + 委托本命令），见 ``skill_cmds``。
  - ``orca in-session start`` 的 **opencode 模板落地职责** → 移到本命令。``start`` 收窄为
    **CC-only run bootstrap**（opencode 路运行时由 ``/orca run`` → ``bootstrap`` 自举）。

设计（spike-verified 2026-07-08，详见 ``docs/plans/2026-07-08-unified-install.md``）：
  - **opencode plugin 加载 = ``opencode.json`` ``"plugin": [<path>]`` 声明**。无目录自动发现
    （光丢 ``.ts`` 不加载——spike 加声明前 0 marker，加声明后 ``loading plugin`` + marker 全现）。
    故 install **必须合并** ``opencode.json``：
      - 项目 scope：plugin 声明用相对路径 ``./.opencode/plugins/orca.ts``（相对 cwd）
      - 用户 scope：plugin 声明用**绝对路径** ``<config_dir>/plugins/orca.ts``（全局 config 非项目相对）
  - ``command/orca.md`` = ``/orca`` slash 命令来源（command 目录自动发现，同 skill 约定）。
  - **CC in-session hooks 是 per-run**（``settings.json`` 片段内嵌 tape_path/run_id，无法全局
    安装）→ 仍由 ``orca in-session start <wf>`` 每次生成。本命令对 CC 只装 skill。

**架构守门**（D-v7-1 同源）：本模块零 Orca 业务逻辑——只拷文件 + 合并 JSON 顶层字段。
不调 advance/router/replay/tape 路径，不做状态机判断。CI grep 守门。

依赖单向：stdlib + typer + ``orca.iface.cli.config`` + ``orca.iface.in_session.templates``
（只读模板资产）+ ``orca.skills``（只读随包 skill）+ ``orca.iface.cli.skill_cmds``（SKILL_NAME
常量，单一真相源）。**不**反向依赖 run/events/schema。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

import typer

from orca.iface.cli.config import bootstrap_config
from orca.iface.cli.skill_cmds import SKILL_NAME, opencode_global_root

app = typer.Typer(
    name="install",
    help="统一安装 Orca 宿主集成（skill + in-session），全局默认。",
)

VALID_TARGETS = ("claude", "opencode", "all")
VALID_SCOPES = ("user", "project")


# ── 目标解析（纯函数，home 可注入单测）────────────────────────────────────────


@dataclass(frozen=True)
class HostRoot:
    """一个宿主在某 scope 下的 config 根目录。

    ``root`` 是该宿主的配置根：claude = ``.claude``，opencode = ``.opencode`` /
    ``~/.config/opencode``。skill/plugin/command/declaration 都落在此根下（opencode.json
    例外——项目 scope 落在 cwd 根，见 ``_opencode_json_path``）。
    """

    host: str   # "claude" | "opencode"
    root: Path
    scope: str  # "user" | "project"


def resolve_roots(
    target: str, scope: str, *, home: Path | None = None,
) -> list[HostRoot]:
    """按 ``--target`` × ``--scope`` 解析宿主 config 根目录列表。

    - claude：user → ``<home>/.claude``；project → ``<cwd>/.claude``
    - opencode：user → ``OPENCODE_CONFIG_DIR`` 或 ``<home>/.config/opencode``；
      project → ``<cwd>/.opencode``

    未知 target / scope → ``typer.BadParameter``（fail loud）。
    """
    if target not in VALID_TARGETS:
        raise typer.BadParameter(
            f"未知 target {target!r}，可选：{' / '.join(VALID_TARGETS)}"
        )
    if scope not in VALID_SCOPES:
        raise typer.BadParameter(
            f"未知 scope {scope!r}，可选：{' / '.join(VALID_SCOPES)}"
        )

    home = home or Path.home()
    cwd = Path.cwd()
    hosts = ["claude", "opencode"] if target == "all" else [target]

    roots: list[HostRoot] = []
    for host in hosts:
        if host == "claude":
            root = (home / ".claude") if scope == "user" else (cwd / ".claude")
        else:  # opencode
            if scope == "user":
                # OPENCODE_CONFIG_DIR 覆盖；与 skill_cmds.install_targets 同源（单一真相源
                # opencode_global_root，含 expanduser 兜底，review 🟡#1 闭环）。
                root = opencode_global_root(home)
            else:
                root = cwd / ".opencode"
        roots.append(HostRoot(host=host, root=root, scope=scope))
    return roots


# ── 资源定位（随包模板 / skill 源）────────────────────────────────────────────


def _opencode_plugin_src() -> Path:
    """随包 opencode plugin 模板（``orca.ts``）。"""
    return Path(str(files("orca.iface.in_session.templates"))) / "opencode" / "orca.ts"


def _opencode_command_src() -> Path:
    """随包 opencode slash 命令模板（``command/orca.md`` = ``/orca`` 本体）。"""
    return Path(str(files("orca.iface.in_session.templates"))) / "opencode" / "command" / "orca.md"


def _skill_src() -> Path:
    """随包 create-workflow skill 源目录。"""
    return Path(str(files("orca.skills"))) / SKILL_NAME


# ── 落地原语：原子写（带 backup）+ JSON 合并 ──────────────────────────────────


def _atomic_write_with_backup(dst: Path, content: str) -> None:
    """幂等写单文件：内容相同跳过；不同先 backup（``dst.bak``）再 ``write tmp + os.replace``。

    从原 ``in_session.cli._atomic_write_with_backup`` 搬来（start 的模板落地移除后，
    该函数唯一消费者是本模块）。读比对失败（权限/编码）→ 按覆盖处理（不静默吞错：
    写本身仍 fail loud——下方 ``install`` 捕 OSError 报路径）。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            if dst.read_text(encoding="utf-8") == content:
                return  # 内容一致，不动
        except OSError:
            pass  # 读失败 → 按覆盖处理（backup 仍走）
        bak = dst.with_suffix(dst.suffix + ".bak")
        try:
            dst.replace(bak)
        except OSError:
            pass  # backup 失败不阻断（log warn 等价——继续覆盖）
    tmp = dst.with_suffix(dst.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, dst)


def _merge_json_file(path: Path, mutator: Callable[[dict], None]) -> bool:
    """读-改-写 JSON 文件（保已有键）。``mutator`` 原地改 dict。返回是否有变化。

    文件不存在 / 损坏 / 顶层非 object → 从 ``{}`` 起（不崩，fail-soft 读；写仍原子 + backup）。
    **非原子 read-modify-write**：读与写之间有 window；安装命令非并发高频路径可接受，
    勿用于并发场景（review 🟢#3）。
    """
    data: Any
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    mutator(data)
    after = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if before == after:
        return False
    _atomic_write_with_backup(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return True


# ── per-host 落地 ─────────────────────────────────────────────────────────────


def _install_skill(root: Path) -> Path:
    """拷 create-workflow skill → ``<root>/skills/<SKILL_NAME>/``。

    ``shutil.copytree(dirs_exist_ok=True)`` 幂等覆盖；排除 ``benchmark/``（评测资产，
    含 expected 答案，不该进用户 skill 目录——与 ``skill_cmds.install`` 同策略）。
    """
    src = _skill_src()
    if not src.is_dir():
        typer.echo(f"找不到随包 skill 源目录：{src}（打包可能漏文件，检查 pyproject）", err=True)
        raise typer.Exit(1)
    dst = root / "skills" / SKILL_NAME
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("benchmark"))
    return dst


def _opencode_json_path(hr: HostRoot) -> Path:
    """opencode.json 落点：项目 scope 在 cwd 根（spike 验证根 ``opencode.json`` + 相对声明加载），
    用户 scope 在 config 根（spike 验证全局 ``opencode.json`` + 绝对声明加载）。"""
    return (Path.cwd() / "opencode.json") if hr.scope == "project" else (hr.root / "opencode.json")


def _opencode_plugin_decl(hr: HostRoot, plugin_dst: Path) -> str:
    """opencode.json ``"plugin"`` 声明里的 plugin 路径：
    项目 scope 相对 cwd（``./.opencode/plugins/orca.ts``），用户 scope 绝对路径。"""
    if hr.scope == "project":
        return "./.opencode/plugins/orca.ts"
    return str(plugin_dst.expanduser().resolve())


def _install_opencode(hr: HostRoot) -> dict[str, Path]:
    """opencode 全套：skill + plugins/orca.ts + command/orca.md + opencode.json 声明合并。

    返回 ``{组件: 落地路径}``。opencode.json 合并：``"plugin"`` 数组加 orca 声明（去重，
    保已有 plugin 条目与其他键）。
    """
    written: dict[str, Path] = {}

    # skill
    written["skill"] = _install_skill(hr.root)

    # plugin + command（静态模板，run-agnostic）
    plugin_dst = hr.root / "plugins" / "orca.ts"
    _atomic_write_with_backup(plugin_dst, _opencode_plugin_src().read_text(encoding="utf-8"))
    written["plugin"] = plugin_dst

    cmd_dst = hr.root / "command" / "orca.md"
    _atomic_write_with_backup(cmd_dst, _opencode_command_src().read_text(encoding="utf-8"))
    written["command"] = cmd_dst

    # 迁移提示：旧 start 写的 singular plugin/ 目录（无 s）残留 → warn（不擅自删用户文件）
    legacy = hr.root / "plugin" / "orca.ts"
    if legacy.exists():
        typer.echo(
            f"  ⚠ 检测到旧式 {legacy}（旧 start 写的，singular 目录）。"
            f"新版用 {plugin_dst}（plural），建议删除旧的避免混淆。",
            err=True,
        )

    # opencode.json 声明合并（spike：声明是 plugin 加载唯一入口）
    cfg_path = _opencode_json_path(hr)
    plugin_decl = _opencode_plugin_decl(hr, plugin_dst)

    def _add_plugin_decl(data: dict) -> None:
        plugins = data.setdefault("plugin", [])
        if not isinstance(plugins, list):
            # 用户手填了非数组（字符串等非法形态）→ warn + 重置为 []（review 🟡#2：不静默吞，
            # 显式告知原值被丢弃）。opencode 加载非数组 plugin 本就报错，重置后由 orca 声明顶上。
            typer.echo(
                f'  ⚠ opencode.json 的 "plugin" 非 array（原值：{plugins!r}），已重置为 [] '
                f"并加入 orca 声明。请检查原配置。",
                err=True,
            )
            plugins = []
            data["plugin"] = plugins
        if plugin_decl not in plugins:
            plugins.append(plugin_decl)

    _merge_json_file(cfg_path, _add_plugin_decl)
    written["opencode.json"] = cfg_path
    return written


# ── 命令 ──────────────────────────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def install(
    target: str = typer.Option(
        "all", "--target", "-t",
        help="装到哪边：claude / opencode / all（默认 all）",
    ),
    scope: str = typer.Option(
        "user", "--scope", "-s",
        help="装到哪层：user（全局，默认）/ project（当前项目）",
    ),
) -> None:
    """统一安装 Orca 宿主集成（skill + in-session），全局默认。

    \b
    - opencode：skill + plugin + ``/orca`` 命令 + ``opencode.json`` plugin 声明
    - claude：skill（in-session CC hooks 是 per-run，由 ``orca in-session start`` 生成）

    \b
    幂等（重跑覆盖更新，内容相同跳过；JSON 配置读-改-写保已有键）。装完 opencode 重启
    后敲 ``/orca doctor`` 自检入口链路。

    ``install`` 是单动词（同 ``run``/``serve``），故用 callback 而非 sub-Typer 子命令——
    避免双层嵌套 ``orca install install``。``invoke_without_command=True`` 让裸 ``orca install``
    以默认（target=all / scope=user）直接跑。
    """
    failed = run_install(target, scope)
    if failed:
        raise typer.Exit(1)


def run_install(target: str, scope: str) -> list[str]:
    """install 核心逻辑（callback + ``skill install`` 弃用委托共用）。返回失败 host 列表。

    抽出来让 ``orca skill install``（弃用别名）能直接委托，不走 subprocess。``bootstrap_config``
    在此调用（skill_cmds 原也调，幂等）。
    """
    bootstrap_config()
    roots = resolve_roots(target, scope)

    typer.echo(
        f"scope={scope}（{'全局' if scope == 'user' else '当前项目'}）  target={target}"
    )
    failed: list[str] = []
    for hr in roots:
        typer.echo(f"\n[{hr.host}] → {hr.root}")
        try:
            if hr.host == "opencode":
                written = _install_opencode(hr)
                for comp, p in written.items():
                    typer.echo(f"  ✓ {comp}: {p}")
            else:  # claude：只装 skill（CC hooks per-run，不走这）
                p = _install_skill(hr.root)
                typer.echo(f"  ✓ skill: {p}")
        except OSError as e:
            typer.echo(f"  ✗ 失败：{e}", err=True)
            failed.append(hr.host)

    if failed:
        typer.echo(f"\n部分失败：{', '.join(failed)}", err=True)
    else:
        typer.echo(
            "\n✓ 完成。opencode：重启后敲 `/orca doctor` 自检；"
            "CC：跑 `orca in-session start <wf>` 生成 per-run hooks。"
        )
    return failed

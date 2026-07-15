"""install_cmds.py —— ``tars install`` 统一安装入口（v5 §4.3，全局默认）。

回答「用户怎么把 Orca skill 装到各前端宿主？」：``tars install --target <platform>``
把随包 skill（含 ``orca`` 入口 skill）拷到对应前端的 skill 目录。四前端统一一套 skill
（SPEC v5 §4.1/§4.3：删 command、统一 skill，入口内联注入主 session）。

落点（§4.3，仅此不同）：
  - ``cc``       → ``.claude/skills/``
  - ``opencode`` → ``.opencode/skills/``（user scope: ``~/.config/opencode/skills/``）
  - ``cac``      → ``.cac/skills/``
  - ``nga``      → ``.nga/skills/``
  - ``all``      → 上列四个都装

opencode 额外落 ``plugins/orca.ts`` + 合并 ``opencode.json`` 的 plugin 声明（plugin 加载需
显式声明，spike-verified 2026-07-08）。v5 §8 step 2b：``orca.ts`` 的 transform marker 派发
已 early-return 禁用（惰性），整个 plugin 文件 + 声明在 step 4 整删；此窗口期仍拷贝保声明
不悬空。command 模板已删（step 2b(5)），不再拷 ``command/orca/``。

**家族路由**（v5 §8 step 6：用户澄清 CAC≡cc / NGA≡opencode，install 阶段两家族全套统一装）：
  - **opencode 家族**（``opencode`` + ``nga``）：skill + plugin ``orca.ts`` + ``opencode.json``
    声明（idle nudge 载体）。nga 仅落点 ``.opencode``→``.nga``，其余同 opencode。
  - **cc 家族**（``cc`` + ``cac``）：skill + nudge Stop-hook（``hooks/orca-nudge.sh`` +
    ``settings.json`` 声明）。cac 仅落点 ``.claude``→``.cac``，其余同 cc。

四 host 行为家族内对称（CAC/NGA 真机加载是否读 ``.cac``/``.nga`` 留 §9#1 跨平台用户侧验证）。

**架构守门**（D-v7-1 同源）：本模块零 Orca 业务逻辑——只拷文件 + 合并 JSON 顶层字段。
不调 advance/router/replay/tape 路径，不做状态机判断。CI grep 守门。

依赖单向：stdlib + typer + ``orca.iface.cli.config`` + ``orca.iface.in_session.templates``
（只读模板资产）+ ``orca.skills``（只读随包 skill）+ ``orca.iface.cli.skill_cmds``
（``opencode_global_root`` 单一真相源）。**不**反向依赖 run/events/schema。
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
from orca.iface.cli.skill_cmds import (
    ENTRY_SKILL_NAME,
    HOST_DOTDIR,
    SKILL_HOSTS,
    SKILL_NAME,
    SKILL_TARGETS,
    opencode_global_root,
)

app = typer.Typer(
    name="install",
    help="统一安装 Orca 宿主集成（skill），全局默认。",
)

# v5 §4.3：四前端统一 skill 落点。常量从 skill_cmds import（单一真相源，避免副本漂移）。
VALID_TARGETS = SKILL_TARGETS
VALID_SCOPES = ("user", "project")


# ── 目标解析（纯函数，home 可注入单测）────────────────────────────────────────


@dataclass(frozen=True)
class HostRoot:
    """一个宿主在某 scope 下的 config 根目录。

    ``root`` 是该宿主的配置根：cc = ``.claude``、opencode = ``.opencode`` /
    ``~/.config/opencode``、cac = ``.cac``、nga = ``.nga``。skill 都落 ``<root>/skills/``；
    opencode 额外落 plugin + ``opencode.json`` 声明（plugin 惰性，step 4 整删）。
    """

    host: str   # "cc" | "opencode" | "cac" | "nga"
    root: Path
    scope: str  # "user" | "project"


def resolve_roots(
    target: str, scope: str, *, home: Path | None = None,
) -> list[HostRoot]:
    """按 ``--target`` × ``--scope`` 解析宿主 config 根目录列表（v5 §4.3 四平台）。

    - cc：user → ``<home>/.claude``；project → ``<cwd>/.claude``
    - opencode：user → ``OPENCODE_CONFIG_DIR`` 或 ``<home>/.config/opencode``；
      project → ``<cwd>/.opencode``
    - cac：user → ``<home>/.cac``；project → ``<cwd>/.cac``
    - nga：user → ``<home>/.nga``；project → ``<cwd>/.nga``
    - all → 上列四者都装

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
    hosts = list(SKILL_HOSTS) if target == "all" else [target]

    roots: list[HostRoot] = []
    for host in hosts:
        if host == "opencode" and scope == "user":
            # OPENCODE_CONFIG_DIR 覆盖；与 skill_cmds.install_targets 同源（单一真相源
            # opencode_global_root，含 expanduser 兜底，review 🟡#1 闭环）。
            root = opencode_global_root(home)
        elif host == "opencode":  # project scope
            root = cwd / ".opencode"
        else:  # cc / cac / nga
            dotdir = HOST_DOTDIR[host]
            root = (home / dotdir) if scope == "user" else (cwd / dotdir)
        roots.append(HostRoot(host=host, root=root, scope=scope))
    return roots


# ── 资源定位（随包模板 / skill 源）────────────────────────────────────────────


def _opencode_plugin_src() -> Path:
    """随包 opencode plugin 模板（``orca.ts``，v5 step 2b transform 已禁用，step 4 整删）。"""
    return Path(str(files("orca.iface.in_session.templates"))) / "opencode" / "orca.ts"


def _bundled_skill_sources() -> list[Path]:
    """随包所有 skill 源目录（``orca/skills/*/``，以含 ``SKILL.md`` 判定）。

    v5 §4.1/§4.3：入口统一切到 skill。随包目前两份：``orca``（in-session 入口三步指导）
    + ``create-workflow``（authoring）。加 skill = 加目录，install 自动捡（OCP，无需改本函数）。
    按 ``SKILL.md`` 存在过滤——排除 ``__pycache__`` 等非 skill 目录。
    """
    skills_dir = Path(str(files("orca.skills")))
    return sorted(p for p in skills_dir.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def _cc_nudge_script_src() -> Path:
    """随包 CC nudge Stop-hook 脚本（v5 §4.4 / step 2b(7)，提醒不推进）。"""
    return Path(str(files("orca.iface.in_session.templates"))) / "cc_nudge.sh"


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


def _install_skill(root: Path) -> list[Path]:
    """拷**所有**随包 skill → ``<root>/skills/<name>/``（v5 §4.1：入口统一 skill）。

    ``shutil.copytree(dirs_exist_ok=True)`` 幂等覆盖；排除 ``benchmark/``（评测资产，
    含 expected 答案，不该进用户 skill 目录）。返落地 skill 目录列表（按 name 升序）。
    找不到随包 skill 源 → fail loud（exit 1，打包漏文件）。
    """
    srcs = _bundled_skill_sources()
    if not srcs:
        typer.echo(
            "找不到随包 skill 源目录（orca/skills/*/），打包可能漏文件，检查 pyproject",
            err=True,
        )
        raise typer.Exit(1)
    dsts: list[Path] = []
    for src in srcs:
        dst = root / "skills" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("benchmark"))
        dsts.append(dst)
    return dsts


def _opencode_json_path(hr: HostRoot) -> Path:
    """opencode.json 落点：项目 scope 在 cwd 根（spike 验证根 ``opencode.json`` + 相对声明加载），
    用户 scope 在 config 根（spike 验证全局 ``opencode.json`` + 绝对声明加载）。"""
    return (Path.cwd() / "opencode.json") if hr.scope == "project" else (hr.root / "opencode.json")


def _opencode_plugin_decl(hr: HostRoot, plugin_dst: Path) -> str:
    """opencode 家族（opencode + nga）``opencode.json`` ``"plugin"`` 声明里的 plugin 路径。

    项目 scope 相对 cwd（``./<hr.root.name>/plugins/orca.ts``——``.opencode`` 或 ``.nga``）；
    用户 scope 绝对路径。``hr.root.name`` 由 ``resolve_roots`` 按宿主派生（opencode→
    ``.opencode``、nga→``.nga``），故同一段代码服务整个 opencode 家族（step 6 泛化）。
    """
    if hr.scope == "project":
        return f"./{hr.root.name}/plugins/orca.ts"
    return str(plugin_dst.expanduser().resolve())


def _install_opencode(hr: HostRoot) -> dict[str, Any]:
    """opencode 家族（opencode + nga）全套：skill + plugins/orca.ts + opencode.json 声明。

    服务整个 opencode 家族（step 6：NGA≡opencode，落点 ``.opencode``→``.nga``，其余同）。
    返回 ``{组件: 落地路径/列表}``。opencode.json 合并：``"plugin"`` 数组加 orca 声明
    （去重，保已有 plugin 条目与其他键）。

    v5 §8 step 2b：command 模板已删（入口切 skill），不再拷 ``command/orca/``。``orca.ts``
    plugin 的 transform 派发已 early-return 禁用（惰性，step 4 整删文件 + 声明）；此窗口期
    仍拷 plugin + 合并声明，保 ``opencode.json`` 指向的文件存在（不悬空）。
    """
    written: dict[str, Any] = {}

    # skill（所有随包 skill）
    written["skills"] = _install_skill(hr.root)

    # plugin（惰性：transform 已禁用，step 4 整删）
    plugin_dst = hr.root / "plugins" / "orca.ts"
    _atomic_write_with_backup(plugin_dst, _opencode_plugin_src().read_text(encoding="utf-8"))
    written["plugin"] = plugin_dst

    # 清理旧命令模板（v5 step 2b：command 已删，入口切 skill）。
    # - 旧单命令 ``command/orca.md``（批 B 前的 marker 派发）
    # - 旧命令命名空间 ``command/orca/``（批 B 的 4 文件 run/status/stop/doctor）
    # 两者都是已退场的入口，残留会让 ``/orca`` 命中死模板 → 模型困惑。幂等清理。
    for legacy in (hr.root / "command" / "orca.md", hr.root / "command" / "orca"):
        if legacy.exists():
            try:
                if legacy.is_dir():
                    shutil.rmtree(legacy)
                else:
                    legacy.unlink()
            except OSError as e:  # noqa: BLE001
                typer.echo(f"  ⚠ 无法清理旧命令模板 {legacy}：{e}", err=True)

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


def _install_cc_nudge(hr: HostRoot) -> dict[str, Path]:
    """CC 家族（cc + cac）nudge Stop-hook 落地（v5 §4.4 / step 6：CAC≡cc，结构相同）。

    服务整个 cc 家族（cc→``.claude``、cac→``.cac``），全路径 root-relative，无硬编码 dotdir：
    - 拷 ``cc_nudge.sh`` → ``<root>/hooks/orca-nudge.sh``。
    - 合并 ``<root>/settings.json`` 的 ``hooks.Stop``：加一条 ``command: bash <abs>/orca-nudge.sh``
      （去重，保已有 hooks / 其他键）。

    nudge = 提醒（``decision:block`` 注入「请调 orca next」），**绝不调 next**（B 路径铁律）。
    脚本自含 60s 节流 + marker 扫描；settings.json 只声明引用，零业务逻辑（守门 D-v7-1）。
    """
    written: dict[str, Path] = {}
    script_dst = hr.root / "hooks" / "orca-nudge.sh"
    _atomic_write_with_backup(script_dst, _cc_nudge_script_src().read_text(encoding="utf-8"))
    # 可执行位（best-effort：Windows FS 无效但无害；Linux/Mac 生效）。
    try:
        script_dst.chmod(0o755)
    except OSError:
        pass
    written["nudge_script"] = script_dst

    settings_path = hr.root / "settings.json"
    # 命令用绝对路径（CC 在 cwd 跑，绝对路径不依赖 cwd；settings.json 全局/项目都适用）。
    cmd = f"bash {script_dst.expanduser().resolve()}"

    def _add_stop_hook(data: dict) -> None:
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            # 非法形态（用户手填非 object）→ warn + 重置（review 🟡#2 同款：不静默吞）。
            typer.echo(
                f'  ⚠ settings.json 的 "hooks" 非 object（原值：{hooks!r}），已重置为 {{}} '
                f"并加入 orca nudge Stop 声明。请检查原配置。",
                err=True,
            )
            hooks = {}
            data["hooks"] = hooks
        stop_list = hooks.setdefault("Stop", [])
        if not isinstance(stop_list, list):
            typer.echo(
                f'  ⚠ settings.json 的 "hooks.Stop" 非 array（原值：{stop_list!r}），已重置为 []。',
                err=True,
            )
            stop_list = []
            hooks["Stop"] = stop_list
        # 去重：任一 Stop entry 的 command 含 ``orca-nudge`` 即视为已声明。
        already = any(
            "orca-nudge" in str(entry.get("hooks", []))
            for entry in stop_list
            if isinstance(entry, dict)
        )
        if not already:
            stop_list.append({"hooks": [{"type": "command", "command": cmd}]})

    _merge_json_file(settings_path, _add_stop_hook)
    written["settings.json"] = settings_path
    return written


# ── 命令 ──────────────────────────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def install(
    target: str = typer.Option(
        "all", "--target", "-t",
        help="装到哪个前端：cc / opencode / cac / nga / all（默认 all，四个都装）",
    ),
    scope: str = typer.Option(
        "user", "--scope", "-s",
        help="装到哪层：user（全局，默认）/ project（当前项目）",
    ),
) -> None:
    """统一安装 Orca skill 到前端宿主（v5 §4.3，全局默认）。

    \b
    - 四前端（cc/opencode/cac/nga）都装同一份随包 skill（含 orca 入口 skill）
    - opencode 家族（opencode/nga）额外落 plugin + opencode.json 声明（plugin 含 idle nudge；transform 已禁用）
    - cc 家族（cc/cac）额外落 nudge Stop-hook + settings.json 声明（提醒主 session 调 next，不自动推进）

    \b
    幂等（重跑覆盖更新，内容相同跳过；JSON 配置读-改-写保已有键）。

    ``install`` 是单动词（同 ``run``/``serve``），故用 callback 而非 sub-Typer 子命令——
    避免双层嵌套 ``tars install install``。``invoke_without_command=True`` 让裸
    ``tars install`` 以默认（target=all / scope=user）直接跑。
    """
    failed = run_install(target, scope)
    if failed:
        raise typer.Exit(1)


def _install_bundled_workflows() -> list[Path]:
    """部署当前目录 ``workflows/*.yaml`` → ``~/.orca/workflows/``（全局内置，catalog 扫到）。

    让 ``tars install`` 把仓库自带 workflow（如 ``nas-agent-pipeline``）装成**全局可见**——
    任何项目的 ``orca list`` 都能扫到（``~/.orca/workflows`` 是 catalog 用户级扫描点），解决
    「全新地方 ``orca list`` 空」问题。幂等：内容相同跳过，不同覆盖（install = refresh 内置）。
    源（CWD/workflows）保留（**复制非移动**）。无 CWD/workflows 或无 *.yaml → no-op
    （非仓库根跑 install 不报错）。
    """
    src_dir = Path.cwd() / "workflows"
    if not src_dir.is_dir():
        return []
    yamls = sorted(src_dir.glob("*.yaml"))
    if not yamls:
        return []
    dest_dir = Path.home() / ".orca" / "workflows"
    dest_dir.mkdir(parents=True, exist_ok=True)
    deployed: list[Path] = []
    for src in yamls:
        dst = dest_dir / src.name
        try:
            if dst.exists() and dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8"):
                continue  # 内容同跳过（幂等）
            shutil.copy2(src, dst)
        except OSError as e:  # noqa: BLE001
            typer.echo(f"  ⚠ 部署 workflow {src.name} 失败：{e}", err=True)
            continue
        deployed.append(dst)
    return deployed


def run_install(target: str, scope: str) -> list[str]:
    """install 核心逻辑（callback + ``skill install`` 弃用委托共用）。返回失败 host 列表。

    抽出来让 ``tars skill install``（弃用别名）能直接委托，不走 subprocess。``bootstrap_config``
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
            if hr.host in ("opencode", "nga"):  # opencode 家族：skill + plugin + json 声明
                written = _install_opencode(hr)
                for comp, p in written.items():
                    typer.echo(f"  ✓ {comp}: {p}")
            elif hr.host in ("cc", "cac"):  # cc 家族：装 skill + nudge Stop-hook
                dirs = _install_skill(hr.root)
                for d in dirs:
                    typer.echo(f"  ✓ skill: {d}")
                # cc 家族都装 nudge Stop-hook（v5 §4.4 / step 6：CAC≡cc，结构与 cc 相同）。
                for comp, p in _install_cc_nudge(hr).items():
                    typer.echo(f"  ✓ {comp}: {p}")
            else:  # 不可达：resolve_roots 已按 VALID_TARGETS 校验 target（fail loud 铁律 12）
                raise AssertionError(f"unreachable: unexpected host {hr.host!r}")
        except OSError as e:
            typer.echo(f"  ✗ 失败：{e}", err=True)
            failed.append(hr.host)

    # 部署内置 workflow（CWD/workflows → ~/.orca/workflows，全局可见；与 host 无关，跑一次）
    deployed_wfs = _install_bundled_workflows()
    if deployed_wfs:
        typer.echo(f"\n[workflows] → ~/.orca/workflows（全局内置，orca list 可扫到）")
        for w in deployed_wfs:
            typer.echo(f"  ✓ {w.name}")

    if failed:
        typer.echo(f"\n部分失败：{', '.join(failed)}", err=True)
    else:
        typer.echo(
            "\n✓ 完成。前端重启后加载新 skill；用 `orca doctor` 自检（含 skill_install）。"
        )
    return failed

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

**内置 workflows 部署**（per-wf 自包含布局）：``run_install`` 尾部把 CWD/``workflows/`` 下每个含
``workflow.yaml`` 的子目录**整树** sync 到 ``~/.orca/workflows/<wf>/``（源 = 安装态同构），并对旧
平铺布局（共享 ``agents/`` / ``subagents/`` / ``~/.orca/knowledge_base`` / 平铺 yaml）按 UD-1
backup 方案幂等清理（详见 ``_install_bundled_workflows``）。

**架构守门**（D-v7-1 同源）：本模块零 Orca 业务逻辑——只拷文件 + 合并 JSON 顶层字段。
不调 advance/router/replay/tape 路径，不做状态机判断。CI grep 守门。

依赖单向：stdlib + typer + ``orca.iface.cli.config`` + ``orca.iface.in_session.templates``
（只读模板资产）+ ``orca.skills``（只读随包 skill）+ ``orca.iface.cli.skill_cmds``
（``opencode_global_root`` 单一真相源）。**不**反向依赖 run/events/schema。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

import typer

from orca.iface.cli.config import bootstrap_config
from orca.iface.cli.skill_cmds import (
    ENTRY_SKILL_NAME,
    HOST_DOTDIR,
    SKILL_HOSTS,
    SKILL_NAME,  # legacy 清理 reason 直接用；兼 re-export（tests 经 install_cmds.SKILL_NAME 引用）
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

    v5 §4.1/§4.3：入口统一切到 skill。随包目前两份：``tars``（in-session 入口三步指导）
    + ``create-workflow``（authoring）。加 skill = 加目录，install 自动捡（OCP，无需改本函数）。
    按 ``SKILL.md`` 存在过滤——排除 ``__pycache__`` 等非 skill 目录。
    """
    skills_dir = Path(str(files("orca.skills")))
    return sorted(p for p in skills_dir.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def _cc_nudge_script_src() -> Path:
    """随包 CC nudge Stop/PostToolUse 双事件 hook 脚本（v5 §4.4 + SPEC posttooluse-rogue-guard）。"""
    return Path(str(files("orca.iface.in_session.templates"))) / "cc_nudge.sh"


def _cc_permission_hook_src() -> Path:
    """随包 CC PermissionRequest 审批桥 hook（SPEC in-session-permission-hook §3.1）。"""
    return Path(str(files("orca.iface.in_session.templates"))) / "orca-permission-hook.py"


def _tool_classification_src() -> Path:
    """随包工具分类单一真相源（SPEC posttooluse-rogue-guard §5）。

    cc_nudge.sh（PostToolUse 分支）与 orca.ts（tool.execute.after 钩子）启动时各 read 一次。
    install 时拷到 cc 家族 ``<root>/hooks/`` 与 opencode 家族 ``<root>/plugins/`` 下，
    与脚本/plugin 同目录（脚本/plugin 用 ORCA_NUDGE_DIR / __dirname 等定位）。
    """
    return Path(str(files("orca.iface.in_session.templates"))) / "tool-classification.json"


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
    顺带幂等清理旧名 skill 残留（改名/并入迁移，见函数尾 legacy_skills）。
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

    # 陈旧 skill 目录清理（改名迁移 teams→orca→tars + 并入迁移 design-charts→create-workflow）。
    # 旧名残留会让宿主同时加载新旧两份（如 .claude/skills/orca/ + tars/）→ 模型困惑该调哪个。
    # 旧名已不是随包源 → 残留即陈旧，幂等清理（同 ``command/orca`` 清理同 pattern，fail-soft warn）。
    # reason 区分迁移方式：入口 skill 是改名，design-charts 是并入 create-workflow（能力合并非改名）。
    installed_names = {p.name for p in srcs}
    legacy_skills = (
        (root / "skills" / "orca", f"已改名 {ENTRY_SKILL_NAME}"),
        (root / "skills" / "teams", f"已改名 {ENTRY_SKILL_NAME}"),
        (root / "skills" / "design-charts", f"已并入 {SKILL_NAME}"),
    )
    for legacy, reason in legacy_skills:
        if legacy.is_dir() and legacy.name not in installed_names:
            try:
                shutil.rmtree(legacy)
                typer.echo(f"  ↻ 清理旧 skill 残留：{legacy}（{reason}）")
            except OSError as e:  # noqa: BLE001
                typer.echo(f"  ⚠ 无法清理旧 skill 残留 {legacy}：{e}", err=True)
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

    # tool-classification.json（SPEC posttooluse-rogue-guard §5 单一真相源，tool.execute.after
    # 钩子读；与 plugin 同目录，orca.ts 用相对路径 readFileSync 兜底解析）。
    cls_dst = hr.root / "plugins" / "tool-classification.json"
    _atomic_write_with_backup(cls_dst, _tool_classification_src().read_text(encoding="utf-8"))
    written["tool_classification"] = cls_dst

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
    """CC 家族（cc + cac）nudge Stop + PostToolUse guard hook + PermissionRequest 审批桥
    hook 落地（v5 §4.4 + SPEC posttooluse-rogue-guard + SPEC in-session-permission-hook）。

    服务整个 cc 家族（cc→``.claude``、cac→``.cac``），全路径 root-relative，无硬编码 dotdir：
    - 拷 ``cc_nudge.sh`` → ``<root>/hooks/orca-nudge.sh``（单脚本双事件，按 hook_event_name 分支）。
    - 拷 ``orca-permission-hook.py`` → ``<root>/hooks/``（PermissionRequest 审批桥，stdlib-only）。
    - 拷 ``tool-classification.json`` → ``<root>/hooks/``（PostToolUse 分支的工具分类单一真相源）。
    - 合并 ``<root>/settings.json`` 的 ``hooks.Stop`` / ``hooks.PostToolUse`` / ``hooks.PermissionRequest``
      （均去重关键字区分；保已有 hooks / 其他键）。

    nudge = Stop hook 提醒；guard = PostToolUse 事后告警；permission = PermissionRequest 审批桥。
    三者**绝不调 next**（B 路径铁律不变，permission 桥只把决策转给 web）。

    SPEC in-session-permission-hook §3.4 / §4.4：CC hook ``timeout=86400``（永不误杀一个等人的
    审批 hook）；hook 运行 env = ``ORCA_PORT`` / ``ORCA_HOST`` / ``ORCA_APPROVAL_TIMEOUT`` /
    ``ORCA_APPROVAL_TIMEOUT_POLICY``（SPEC §4.4 末段）。
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

    # SPEC in-session-permission-hook §3.1 / §4.4 / N8：PermissionRequest 审批桥 hook（stdlib-only）。
    permission_dst = hr.root / "hooks" / "orca-permission-hook.py"
    _atomic_write_with_backup(
        permission_dst, _cc_permission_hook_src().read_text(encoding="utf-8"),
    )
    try:
        permission_dst.chmod(0o755)
    except OSError:
        pass
    written["permission_hook"] = permission_dst

    # tool-classification.json（SPEC §5 单一真相源，PostToolUse 分支启动时 read）。
    cls_dst = hr.root / "hooks" / "tool-classification.json"
    _atomic_write_with_backup(cls_dst, _tool_classification_src().read_text(encoding="utf-8"))
    written["tool_classification"] = cls_dst

    settings_path = hr.root / "settings.json"
    # 命令用绝对路径（CC 在 cwd 跑，绝对路径不依赖 cwd；settings.json 全局/项目都适用）。
    cmd = f"bash {script_dst.expanduser().resolve()}"
    # PostToolUse matcher（SPEC §7.2 锚定）：限定关心的工具，减少无谓 spawn；脚本内再做 §5 精分类。
    posttooluse_matcher = "^(Write|Edit|NotebookEdit|Bash|PowerShell)$"
    # SPEC in-session-permission-hook §3.4 / §4.4：PermissionRequest 命令 + CC-side timeout。
    permission_cmd = f"python {permission_dst.expanduser().resolve()}"
    permission_timeout = 86400  # 24h，传输层永不误杀一个等人的审批 hook
    # SPEC §4.4 末段：hook 运行 env（与 settings.json 同事务写入；env 在 hook 的 spawn env 上）。
    orca_port = os.environ.get("ORCA_PORT", "7428")
    orca_host = os.environ.get("ORCA_HOST", "127.0.0.1")
    approval_timeout = os.environ.get("ORCA_APPROVAL_TIMEOUT", "600")
    approval_policy = os.environ.get("ORCA_APPROVAL_TIMEOUT_POLICY", "allow")

    def _add_hooks(data: dict) -> None:
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            # 非法形态（用户手填非 object）→ warn + 重置（review 🟡#2 同款：不静默吞）。
            typer.echo(
                f'  ⚠ settings.json 的 "hooks" 非 object（原值：{hooks!r}），已重置为 {{}} '
                f"并加入 orca nudge Stop/PostToolUse 声明。请检查原配置。",
                err=True,
            )
            hooks = {}
            data["hooks"] = hooks

        # ── Stop hook（v5 §4.4，原行为）──
        stop_list = hooks.setdefault("Stop", [])
        if not isinstance(stop_list, list):
            typer.echo(
                f'  ⚠ settings.json 的 "hooks.Stop" 非 array（原值：{stop_list!r}），已重置为 []。',
                err=True,
            )
            stop_list = []
            hooks["Stop"] = stop_list
        # 去重：任一 Stop entry 的 command 含 ``orca-nudge`` 即视为已声明。
        already_stop = any(
            "orca-nudge" in str(entry.get("hooks", []))
            for entry in stop_list
            if isinstance(entry, dict)
        )
        if not already_stop:
            stop_list.append({"hooks": [{"type": "command", "command": cmd}]})

        # ── PostToolUse hook（SPEC posttooluse-rogue-guard §7.2，新加）──
        ptu_list = hooks.setdefault("PostToolUse", [])
        if not isinstance(ptu_list, list):
            typer.echo(
                f'  ⚠ settings.json 的 "hooks.PostToolUse" 非 array（原值：{ptu_list!r}），已重置为 []。',
                err=True,
            )
            ptu_list = []
            hooks["PostToolUse"] = ptu_list
        # 去重：PostToolUse entry 的 command 含 ``orca-nudge`` 即视为已声明（matcher 不参与去重
        # 判定——用户可能改 matcher，但不该让 install 反复加同 command 的重复条目）。
        already_ptu = any(
            "orca-nudge" in str(entry.get("hooks", []))
            for entry in ptu_list
            if isinstance(entry, dict)
        )
        if not already_ptu:
            ptu_list.append({
                "matcher": posttooluse_matcher,
                "hooks": [{"type": "command", "command": cmd}],
            })

        # ── PermissionRequest hook（SPEC in-session-permission-hook §4.4，新加）──
        pr_list = hooks.setdefault("PermissionRequest", [])
        if not isinstance(pr_list, list):
            typer.echo(
                f'  ⚠ settings.json 的 "hooks.PermissionRequest" 非 array（原值：{pr_list!r}），'
                f"已重置为 []。",
                err=True,
            )
            pr_list = []
            hooks["PermissionRequest"] = pr_list
        # 去重：entry 的 command 含 ``orca-permission`` 即视为已声明（关键字 ``orca-permission``
        # 区分于 ``orca-nudge``，两类 hook 同 settings.json 共存不撞去重）。
        already_pr = any(
            "orca-permission" in str(entry.get("hooks", []))
            for entry in pr_list
            if isinstance(entry, dict)
        )
        if not already_pr:
            pr_list.append({
                "hooks": [{
                    "type": "command",
                    "command": permission_cmd,
                    "timeout": permission_timeout,
                    # hook spawn env（SPEC §4.4）。
                    "env": {
                        "ORCA_PORT": orca_port,
                        "ORCA_HOST": orca_host,
                        "ORCA_APPROVAL_TIMEOUT": approval_timeout,
                        "ORCA_APPROVAL_TIMEOUT_POLICY": approval_policy,
                    },
                }],
            })

    _merge_json_file(settings_path, _add_hooks)
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


# ── 内置 workflows per-wf 同步 + 旧平铺布局清理（layout per-wf 改造 · SPEC 步骤 4）──

# copytree ignore 口径（部署不拷）与内容比对忽略口径（比对不算）的**单一真相源**——两处不对称
# 会让真机安装态的 __pycache__ 永远触发 backup、「与随包完全一致 → 直接删」分支永不命中（Q13a）。
_IGNORE_PATTERNS = ("__pycache__", "*.pyc")


def _is_ignored_name(name: str) -> bool:
    """名字级忽略判定，语义与 ``shutil.ignore_patterns(*_IGNORE_PATTERNS)`` 完全同源
    （同用 fnmatch）——部署与比对两处消费同一常量，对称性由结构保证而非注释。"""
    return any(fnmatch.fnmatch(name, pat) for pat in _IGNORE_PATTERNS)


@dataclass(frozen=True)
class _LegacyBackup:
    """一条旧布局 backup 记录：源路径 → backup 落点 + 原因（CLI warn 清单逐条打印）。"""

    source: Path
    dest: Path
    reason: str


@dataclass
class WorkflowSyncResult:
    """``_install_bundled_workflows`` 结果（run_install 据此打印部署/备份/清理/警告清单）。"""

    deployed: list[Path] = field(default_factory=list)          # per-wf 落地目录（按名排序）
    backed_up: list[_LegacyBackup] = field(default_factory=list)  # 移入 backup 的旧布局路径
    removed: list[Path] = field(default_factory=list)           # 与随包完全一致、直接删的旧目录
    warned: list[str] = field(default_factory=list)             # 未知内容只记录不动的警告
    failed: list[str] = field(default_factory=list)             # 部署失败单元（"workflows/<wf>"）


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _tree_signature(root: Path) -> dict[str, str]:
    """目录树内容签名：相对路径 → sha256（忽略 ``__pycache__``/``*.pyc``，与部署 ignore 对称）。

    ``root`` 是文件 → ``{".": sha}``（统一文件/目录两形态，比对即 dict 相等；文件 vs 目录
    同名时签名形状天然不同 → 判不一致，正确落入 backup）。
    """
    if root.is_file():
        return {".": _sha256_file(root)}
    sig: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(_is_ignored_name(part) for part in rel.parts):
            continue
        sig[rel.as_posix()] = _sha256_file(p)
    return sig


def _bundled_layout_maps(
    wf_dirs: list[Path],
) -> tuple[dict[str, list[Path]], dict[str, list[Path]], list[Path]]:
    """随包（per-wf 源树）三类资产的候选映射——**运行时计算，禁 hardcode**（防漂移）。

    - agents：agent 名 → 各 wf 源副本列表（共享 agent 多副本，比对 any-match，plan Q13b）
    - subagents：wf 名 → 该 wf 的 ``subagents/`` 源目录（旧布局 ``subagents/<wf>`` 的映射
      公式，plan Q13c）
    - knowledge_base：随包 KB 源目录列表（旧 ``~/.orca/knowledge_base`` 的映射公式）
    """
    agents: dict[str, list[Path]] = {}
    subagents: dict[str, list[Path]] = {}
    kbs: list[Path] = []
    for wf in wf_dirs:
        agents_root = wf / "agents"
        if agents_root.is_dir():
            for entry in agents_root.iterdir():
                agents.setdefault(entry.name, []).append(entry)
        sa = wf / "subagents"
        if sa.is_dir():
            subagents.setdefault(wf.name, []).append(sa)
        kb = wf / "knowledge_base"
        if kb.is_dir():
            kbs.append(kb)
    return agents, subagents, kbs


def _entry_mismatch_reasons(
    installed: Path, candidates_of: Callable[[str], list[Path]],
) -> list[str]:
    """旧共享目录逐条目比对（agents/ 与 subagents/ 通用）。返回不一致原因清单（空 = 可直删）。

    分支① 条目名不在随包集合（用户自加 / 已下架如 kd 系）→ 该目录整树入 backup；
    分支② 名字随包但内容 sha256 不一致（用户自改，plan Q3）→ 同上。任一条目不一致即
    整目录 backup（SPEC「整个目录移入」），故返回原因清单而非逐条目裁决。
    """
    reasons: list[str] = []
    for entry in sorted(installed.iterdir()):
        if _is_ignored_name(entry.name):
            continue  # 与部署 ignore 同口径（Q13a）：垃圾不触发 backup
        candidates = candidates_of(entry.name)
        if not candidates:
            reasons.append(f"非随包条目 {entry.name}")
            continue
        sig = _tree_signature(entry)
        if not any(_tree_signature(c) == sig for c in candidates):
            reasons.append(f"内容与随包不一致 {entry.name}")
    return reasons


def _whole_tree_matches_any(installed: Path, candidates: list[Path]) -> bool:
    """整树 any-match（KB 用）：installed 与任一随包候选逐文件 sha256 一致。"""
    if not candidates:
        return False
    sig = _tree_signature(installed)
    return any(_tree_signature(c) == sig for c in candidates)


def _legacy_backup_root() -> Path:
    """backup 落点 ``~/.orca/_legacy_layout_backup_<YYYYMMDD>/``（同日重跑共用同一目录）。"""
    return Path.home() / ".orca" / f"_legacy_layout_backup_{time.strftime('%Y%m%d')}"


def _move_to_backup(src: Path, backup_root: Path) -> Path:
    """把旧布局路径（目录或文件）移入 backup，保持 ``~/.orca`` 下相对结构（可按原位还原）。

    移动失败 → OSError 上抛（install 期 fail loud，run_install 捕获计入 failed，plan 风险表）。
    目标已存在（同日重跑 + 用户手工还原等边角）→ 数字后缀防覆盖 / 防 ``move`` 的
    move-into 语义改写目录结构。
    """
    dst = backup_root / src.relative_to(Path.home() / ".orca")
    dst.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while dst.exists():
        dst = dst.with_name(f"{dst.name}.{n}")
        n += 1
    shutil.move(str(src), str(dst))
    return dst


def _remove_legacy_dir(path: Path, result: WorkflowSyncResult) -> None:
    """删除与随包完全一致的旧共享目录（纯随包安装产物，SPEC：直接删）。

    rmtree 失败 → warn 不 fail（内容与随包冗余一致，非安装失败；下次 install 幂等重试）。
    """
    try:
        shutil.rmtree(path)
    except OSError as e:  # noqa: BLE001
        typer.echo(f"  ⚠ 清理旧布局目录 {path} 失败（内容与随包一致，可安全手删）：{e}", err=True)
        return
    result.removed.append(path)


def _cleanup_legacy_layout(
    wf_dirs: list[Path], dest_root: Path, result: WorkflowSyncResult,
) -> None:
    """旧平铺布局幂等清理（UD-1 裁决 = backup 方案）。只随 ``_install_bundled_workflows``
    的有效部署路径触发——无随包口径时绝不判定/移动用户内容（no-op 语义的安全闸）。"""
    orca_root = Path.home() / ".orca"
    agents_map, sa_map, kb_candidates = _bundled_layout_maps(wf_dirs)
    backup_root: Path | None = None  # 惰性：无 backup 动作不落 backup 目录

    def _backup(src: Path, reason: str) -> None:
        nonlocal backup_root
        if backup_root is None:
            backup_root = _legacy_backup_root()
        result.backed_up.append(_LegacyBackup(src, _move_to_backup(src, backup_root), reason))

    # ①② 旧共享 agents/ 池（↔ 各 wf 目录 agents/ 子树并集，any-match）
    legacy_agents = dest_root / "agents"
    if legacy_agents.is_dir():
        reasons = _entry_mismatch_reasons(legacy_agents, lambda n: agents_map.get(n, []))
        if reasons:
            _backup(legacy_agents, "；".join(reasons))
        else:
            _remove_legacy_dir(legacy_agents, result)

    # ①② 旧共享 subagents/<wf>/（↔ workflows/<wf>/subagents/，映射公式 Q13c）
    legacy_sa = dest_root / "subagents"
    if legacy_sa.is_dir():
        reasons = _entry_mismatch_reasons(legacy_sa, lambda n: sa_map.get(n, []))
        if reasons:
            _backup(legacy_sa, "；".join(reasons))
        else:
            _remove_legacy_dir(legacy_sa, result)

    # ①② 旧全局 KB（↔ workflows/<wf>/knowledge_base/ 整树 any-match）
    legacy_kb = orca_root / "knowledge_base"
    if legacy_kb.is_dir():
        if _whole_tree_matches_any(legacy_kb, kb_candidates):
            _remove_legacy_dir(legacy_kb, result)
        else:
            reason = "内容与随包不一致" if kb_candidates else "随包无对应 knowledge_base"
            _backup(legacy_kb, reason)

    # ③ 平铺 yaml 一律 backup + 其余未知内容只 warn（~/.orca 未知内容只记录不删）。
    # agents/subagents 已在上面处理（backup 后已不在原位，不会被本循环重复扫到）；随包名
    # 的**目录**是本次/上次部署的 per-wf 目录，静默跳过（同名无后缀文件仍会 warn，不漏报）。
    bundled_names = {d.name for d in wf_dirs}
    for entry in sorted(dest_root.iterdir()):
        if entry.is_file() and entry.suffix == ".yaml":
            why = (
                "旧平铺 yaml（与随包 wf 同名，平铺优先会 shadow per-wf 目录）"
                if entry.stem in bundled_names
                else "旧平铺 yaml（非随包）"
            )
            _backup(entry, why)
        elif not (entry.is_dir() and entry.name in bundled_names) and entry.name not in (
            "agents", "subagents",
        ):
            result.warned.append(f"{entry}（未知内容，仅记录不删）")


def _wf_asset_summary(wf_dir: Path) -> str:
    """per-wf 部署行摘要（agents/subagents 计数 + knowledge_base 标记，CLI 单行展示）。

    ``agents N`` 计 ``agents/`` 下一级条目数（含 ``_xxx_scripts`` 池——池本就在 agents/ 下，
    展示口径如实计数）。"""
    parts: list[str] = []
    agents = wf_dir / "agents"
    if agents.is_dir():
        parts.append(f"agents {len(list(agents.iterdir()))}")
    sa = wf_dir / "subagents"
    if sa.is_dir():
        parts.append(f"subagents {len(list(sa.glob('*.md')))}")
    if (wf_dir / "knowledge_base").is_dir():
        parts.append("knowledge_base")
    return f"（{' · '.join(parts)}）" if parts else ""


def _install_bundled_workflows() -> WorkflowSyncResult:
    """部署 CWD/workflows/ 每个 per-wf 自包含目录 → ``~/.orca/workflows/<wf>/``（整树 sync）。

    源 = 安装态同构：每个含 ``workflow.yaml`` 的子目录整树 ``copytree``（agents/ subagents/
    knowledge_base/ scripts/ 随 wf 目录走），``dirs_exist_ok=True`` 幂等覆盖，忽略
    ``__pycache__``/``*.pyc``。``~/.orca/workflows`` 是 catalog 用户级扫描点，解决「全新
    地方 ``orca list`` 空」。无 CWD/workflows 或无 ``*/workflow.yaml`` → **完全 no-op**
    （非仓库根跑 install 不报错，也**不触发**旧布局清理——无随包口径时绝不判定用户内容）。

    旧平铺布局幂等清理（UD-1 裁决 = backup 方案）——对升级安装的旧产物：
      - ``~/.orca/workflows/agents/``、``~/.orca/workflows/subagents/``：逐条目与随包并集
        比对（共享 agent 与**任一**随包副本一致即算一致，any-match）——①非随包名（用户自
        加 / 已下架）或 ②名字随包但内容 sha256 不一致（用户自改）→ **整个目录**移入
        ``~/.orca/_legacy_layout_backup_<date>/``（不直接删）；④全部一致 → 直接删。
      - ``~/.orca/knowledge_base/`` ↔ ``workflows/<wf>/knowledge_base/``：整树同款比对。
      - ③ ``~/.orca/workflows/`` 平铺 yaml **一律入 backup**：与随包 wf 同名的是升级残骸
        （catalog 平铺优先会 shadow 同名 per-wf 目录，plan Q2）；不同名的是未知尸体（如
        po-probe.yaml）。「未知」判据按**非随包名集合**轻量实现——本模块零 Orca 业务逻辑，
        不引 catalog/compile 做加载级判定。
      - 其余未知内容（非 yaml 文件 / 未知目录）只 warn 不动。

    部署 copytree per-wf fail-soft（warn + ``failed`` 记录，其余 wf 继续）。``dirs_exist_ok``
    merge 语义的既有代价：随包已删的旧文件不从安装态 per-wf 目录内清理（已知限制，同旧版）。
    清理中 backup 移动失败 → 中断清理（后续 backup 不再做，下次 install 幂等重试）+
    ``failed`` 记录 "workflows" → install exit 1（fail loud）；**已完成的部署与 backup 部分
    结果保留在返回值中**（用户可见，不因一句总 warning 丢失可观测性）。
    """
    result = WorkflowSyncResult()
    src_root = Path.cwd() / "workflows"
    if not src_root.is_dir():
        return result
    wf_dirs = sorted(
        d for d in src_root.iterdir() if d.is_dir() and (d / "workflow.yaml").is_file()
    )
    if not wf_dirs:
        return result
    dest_root = Path.home() / ".orca" / "workflows"
    dest_root.mkdir(parents=True, exist_ok=True)

    for src in wf_dirs:
        dst = dest_root / src.name
        try:
            shutil.copytree(
                src, dst, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*_IGNORE_PATTERNS),
            )
        except OSError as e:  # noqa: BLE001
            typer.echo(f"  ⚠ 部署 workflow 目录 {src.name} 失败：{e}", err=True)
            result.failed.append(f"workflows/{src.name}")
            continue
        result.deployed.append(dst)

    try:
        _cleanup_legacy_layout(wf_dirs, dest_root, result)
    except OSError as e:  # noqa: BLE001
        # backup 移动失败 → 中断清理 + fail loud（exit 1）；部分结果已累计在 result 里，
        # run_install 照常打印（不因失败丢弃可观测性）。下次 install 幂等重试剩余项。
        typer.echo(f"  ⚠ 旧布局清理中断（backup 移动失败）：{e}", err=True)
        result.failed.append("workflows")
    return result


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

    # 部署内置 workflow + 旧平铺布局清理（CWD/workflows per-wf 整树 → ~/.orca/workflows/，
    # 全局可见；与 host 无关，跑一次）。部署/清理的失败已在函数内部分级处理（per-wf fail-soft
    # / 清理中断记 failed）；此处 except 只兜 mkdir 等未预期 OSError → 同样计入 failed。
    try:
        sync = _install_bundled_workflows()
    except OSError as e:
        typer.echo(f"  ⚠ 部署/清理 workflows 失败：{e}", err=True)
        sync = WorkflowSyncResult(failed=["workflows"])
    failed.extend(sync.failed)
    if sync.deployed:
        typer.echo("\n[workflows] → ~/.orca/workflows/（per-wf 自包含目录，全局内置，orca list 可扫到）")
        for d in sync.deployed:
            typer.echo(f"  ✓ {d.name}/{_wf_asset_summary(d)}")
    if sync.backed_up or sync.removed or sync.warned:
        typer.echo(
            "\n[旧布局清理]（UD-1 backup：非随包/被改内容不删，移入 ~/.orca/_legacy_layout_backup_<date>/）"
        )
        for item in sync.backed_up:
            typer.echo(f"  ⚠ 备份 {item.source} → {item.dest}（{item.reason}）", err=True)
        for p in sync.removed:
            typer.echo(f"  ✓ 删除 {p}（与随包内容完全一致）")
        for w in sync.warned:
            typer.echo(f"  ⚠ {w}", err=True)

    if failed:
        typer.echo(f"\n部分失败：{', '.join(failed)}", err=True)
    else:
        typer.echo(
            "\n✓ 完成。前端重启后加载新 skill；用 `orca doctor` 自检（含 skill_install）。"
        )
    return failed

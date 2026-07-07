"""skill_cmds.py —— ``orca skill`` 子命令组：安装/管理随包分发的 skill。

回答「用户怎么把 orca 自带的 skill 装进 Claude Code / opencode？」：``orca skill install``
把打包在 ``orca/skills/`` 下的 skill 拷到两边的用户 skill 目录（CC ``~/.claude/skills/``、
opencode ``~/.config/opencode/skills/``，后者可被 ``OPENCODE_CONFIG_DIR`` 覆盖）。

**为什么显式命令而非 post-install 钩子**：pip / uv / hatchling 都不支持可靠的 post-install
入口（wheel 安装阶段无官方钩子往 ``~/.<tool>/`` 写文件）。``teams skill install`` 是显式、
幂等、可重跑的等价手段——装完库跑一次即注入。

**sub-Typer 形态**：与 ``executor_cmds.py`` 同模式（codebase 唯一既有 nested sub-Typer 先例），
``app.add_typer(skill_app, name="skill")``。``skill install`` 目前只一个子命令，但共享名词
``skill``，sub-Typer 给未来 ``skill list/uninstall`` 留扩展位，比扁平 ``skill-install`` 好。

**gotcha**：
  - G1：handler 内先 ``bootstrap_config()``（CliRunner 单测绕过 ``main()``）。
  - G2：源路径用 ``importlib.resources.files("orca.skills")``——wheel / venv / editable
    安装都解析对；非 .py 文件靠 pyproject ``force-include`` 进 wheel。
  - G3：``shutil.copytree(dirs_exist_ok=True)`` 幂等覆盖（Python 3.10+，本项目 ``requires-python>=3.10``）。
  - G4：任一目标不可写 → fail loud（exit 1 + stderr 报路径），铁律 12。

依赖单向：本模块只 import 标准库 + typer + ``orca.iface.cli.config``。**不**反向依赖。
"""

from __future__ import annotations

import os
import shutil
from importlib.resources import files
from pathlib import Path

import typer

from orca.iface.cli.config import bootstrap_config

# 随包 skill 名（``orca/skills/<SKILL_NAME>/``）。多 skill 时改这里 + 打包配置。
SKILL_NAME = "create-workflow"

app = typer.Typer(
    name="skill",
    no_args_is_help=True,
    help="安装/管理随包分发的 Orca skill（create-workflow）。",
)


# ── 可测性 seam：目标解析抽纯函数（gotcha G2）───────────────────────────────


def install_targets(target: str, *, home: Path | None = None) -> list[tuple[str, Path]]:
    """返回 ``[(label, dir), ...]``：按 ``--target`` 选定的 skill 安装目标目录。

    - CC：``<home>/.claude/skills/<SKILL_NAME>``
    - opencode：``<OPENCODE_CONFIG_DIR 或 <home>/.config/opencode>/skills/<SKILL_NAME>``

    纯函数（``home`` 可注入），单测 monkeypatch ``Path.home`` 时传 ``home=`` 即可。
    未知 ``target`` → ``typer.BadParameter``（fail loud）。
    """
    if home is None:
        home = Path.home()
    cc = home / ".claude" / "skills" / SKILL_NAME
    opencode_root = Path(os.environ.get("OPENCODE_CONFIG_DIR") or (home / ".config" / "opencode"))
    oc = opencode_root / "skills" / SKILL_NAME

    valid = {"claude": [("claude", cc)], "opencode": [("opencode", oc)], "all": [("claude", cc), ("opencode", oc)]}
    if target not in valid:
        raise typer.BadParameter(f"未知 target {target!r}，可选：claude / opencode / all")
    return valid[target]


def _bundled_skill_dir() -> Path:
    """定位打包内的 skill 源目录（``orca/skills/<SKILL_NAME>/``）。

    找不到时 echo(stderr) + ``Exit(1)``——显式两步，不靠 ``typer.Exit(str)`` 的未文档化副作用。
    """
    src = Path(str(files("orca.skills"))) / SKILL_NAME
    if not src.is_dir():
        typer.echo(f"找不到随包 skill 源目录：{src}（打包可能漏文件，检查 pyproject）", err=True)
        raise typer.Exit(1)
    return src


@app.command(name="install")
def install(
    target: str = typer.Option(
        "all",
        "--target",
        "-t",
        help="装到哪边：claude / opencode / all（默认 all，两边都装）",
    ),
) -> None:
    """把 create-workflow skill 拷到 Claude Code 和/或 opencode 的 skill 目录。

    幂等：已存在则覆盖更新（会先提示 ``⚠ 覆盖``）。装完打印每边写入的绝对路径。
    """
    bootstrap_config()
    # 先校验参数再取源：避免「--target bogus + wheel 漏文件」时误报源缺失，掩盖真正的参数错。
    targets = install_targets(target)
    src = _bundled_skill_dir()

    typer.echo(f"源：{src}")
    written: list[tuple[str, Path]] = []
    failed: list[tuple[str, str]] = []
    for label, dst in targets:
        try:
            preexisting = dst.exists()
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 排除 benchmark/：它是评测资产（含 expected 答案 + case 不变量），不属于 skill
            # 运行时内容；装到用户 skill 目录会泄露答案、且非用户所需。runtime 只需
            # SKILL.md + examples/ + reference/。
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("benchmark"))
            if preexisting:
                typer.echo(f"⚠ [{label}] 覆盖已安装的 skill：{dst}")
            written.append((label, dst))
        except OSError as e:
            # gotcha G4：目标不可写等 → 记下，最后 fail loud（不静默吞错）
            failed.append((label, f"{dst}: {e}"))

    for label, dst in written:
        typer.echo(f"✓ [{label}] {dst}")

    if failed:
        for label, msg in failed:
            typer.echo(f"✗ [{label}] {msg}", err=True)
        typer.echo(f"部分失败：{len(written)}/{len(targets)} 成功", err=True)
        raise typer.Exit(1)

"""check_agent_md_static.py —— agent.md 静态契约 + 固化漏判启发检查。

对 folder agent（``agents/<name>/agent.md``）与 file agent（``agents/<name>.md``）
做 deterministic 检查（与 workflow contract 的文件夹 agent 硬约定一致）：

1. **布局**：脚本必须放 ``<agent_root>/scripts/`` 子目录，平铺在 agent 根即违规；
2. **引用**：body 引用脚本（``scripts/`` 下的 ``.py``/``.sh``）必须是
   ``$ORCA_AGENT_RESOURCES/scripts/<file>`` 绝对 env 形态（相对 ``scripts/x.py``
   引用在 spawn 后解析到错误的基准目录）；
3. **frontmatter**：folder agent 必须带 YAML 头；file agent 无头合法（引擎兼容
   期语义——无头 md 按全默认元数据处理），不检查。前两条对两类 agent 都查。

固化漏判（确定性逻辑应抽到 scripts/，body 只留一行调用）两形态，均 error 级：

- bash 围栏（bash/sh/shell/zsh 或无标注）内**启行**（容 ≤4 个空格/tab 缩进）
  控制流关键字；
- 行内 ``python -c`` 的参数串内含循环/分支/assert 逻辑（引号串内单行内联是
  最常见的漏网形态，不限围栏内外）。

warning（不强制）：bash 围栏超过 8 行纯顺序命令——提示可抽脚本。

用法::

    python3 check_agent_md_static.py <workflow.yaml | agent.md | agent 目录>...

- yaml 输入 → 扫同级 ``agents/``；md / 目录输入 → 直接定位 agent（目录含
  ``agent.md`` 视为单个 folder agent，否则按 agent 池扫 ``*/agent.md`` 与
  ``*.md``）。
- stdout：每个输入一行扫描清单 ``<path> → N files``（0 files = inline-only
  workflow 合法态，exit 0），随后逐条 finding。
- exit：0 无 error / 1 有 error / 2 用法错（路径不存在 / 不支持的输入类型）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ── 常量与 pattern ────────────────────────────────────────────────────────────

_BASH_FENCE_LANGS = {"bash", "sh", "shell", "zsh"}
_SCRIPT_SUFFIXES = {".py", ".sh"}
_YAML_SUFFIXES = {".yaml", ".yml"}
_AGENT_ENTRY = "agent.md"

# 围栏启行控制流（容 ≤4 个空格/tab 缩进；关键字后必须跟空格）。
_FENCE_CONTROL_RE = re.compile(r"^[\t ]{0,4}(?:for|if|while) ")
# 行内 python 单行程序（python / python3 的 -c 形态）。
_PY_INLINE_RE = re.compile(r"python3?\s+-c")
_PY_LOGIC_TOKENS = ("for ", "if ", "while ", "assert ")
# body 内脚本引用（scripts/ 下 .py/.sh）；env 前缀引导的是合法绝对形态。
_SCRIPT_REF_RE = re.compile(r"scripts/[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh)\b")
# 合法 env 引用的引导形态（裸 / 花括号 / 各自带引号先闭的写法）。
_ENV_REF_FORMS = (
    "$ORCA_AGENT_RESOURCES",
    "${ORCA_AGENT_RESOURCES}",
    '"$ORCA_AGENT_RESOURCES"',
    '"${ORCA_AGENT_RESOURCES}"',
)
_LONG_FENCE_LINES = 8

Finding = tuple[Path, int, str, str]  # (file, line, rule, excerpt)


class _ReadError(Exception):
    """文件/目录读取失败（权限/IO 等环境错）——main 捕获后 stderr + exit 2。"""


# ── 文本读取与 markdown 解析 ──────────────────────────────────────────────────


def _read_text(path: Path) -> tuple[str, bool]:
    """读文件；非 UTF-8 时按替换字符读入并返回标记（调用方打 [warn]，不中断）。"""
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise _ReadError(f"读取文件失败 {path}：{e}") from e
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), True


def _has_frontmatter(lines: list[str]) -> bool:
    """首行 ``---`` 且后续存在闭合 ``---``（与引擎 frontmatter 判定口径一致）。"""
    if not lines or lines[0].strip() != "---":
        return False
    return any(line.strip() == "---" for line in lines[1:])


def _body_start(lines: list[str]) -> int:
    """body 起始行下标——frontmatter（含闭合 ``---``）之后；无 frontmatter 则 0。

    script-ref / 内联逻辑只查 body：frontmatter 是元数据（description 提及脚本路径
    是常见写法），不是 prompt 指令。
    """
    if not _has_frontmatter(lines):
        return 0
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return idx + 1
    return len(lines)


def _iter_bash_fences(lines: list[str]):
    """产出 ``(围栏起始行号, [(行号, 行文本), ...])`` —— 仅 bash 系（或无标注）围栏。

    围栏识别按行首（去除缩进）````` `` 判定；未闭合（到文件尾）也按已收集内容处理。
    """
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].lstrip()
        if not stripped.startswith("```"):
            i += 1
            continue
        info = stripped[3:].strip().lower().split()
        lang = info[0] if info else ""
        start = i
        i += 1
        body: list[tuple[int, str]] = []
        while i < n and not lines[i].lstrip().startswith("```"):
            body.append((i + 1, lines[i]))
            i += 1
        if lang == "" or lang in _BASH_FENCE_LANGS:
            yield start + 1, body
        i += 1  # 跳过闭合围栏（或文件尾越界自然结束）


# ── agent 定位 ────────────────────────────────────────────────────────────────


def _collect_pool(pool: Path) -> tuple[list[tuple[Path, Path, bool]], int]:
    """agent 池目录（yaml 同级 agents/ 或任意目录）→ ([(md, agent 根, 是否 folder)], 计数)。"""
    if not pool.is_dir():
        return [], 0
    out: list[tuple[Path, Path, bool]] = []
    for md in sorted(pool.glob(f"*/{_AGENT_ENTRY}")):
        out.append((md, md.parent, True))
    for md in sorted(pool.glob("*.md")):
        out.append((md, md.parent, md.name == _AGENT_ENTRY))
    return out, len(out)


def _collect_agents(path: Path) -> tuple[list[tuple[Path, Path, bool]], int]:
    """单输入路径 → ([(md, agent 根, 是否 folder)], 清单计数)。

    输入类型合法性由 main 预检（yaml/md/目录），本函数不再做用法错分支。
    """
    if path.is_file():
        if path.suffix in _YAML_SUFFIXES:
            return _collect_pool(path.parent / "agents")
        return [(path, path.parent, path.name == _AGENT_ENTRY)], 1
    if path.is_dir():
        entry = path / _AGENT_ENTRY
        if entry.is_file():
            return [(entry, path, True)], 1
        return _collect_pool(path)
    return [], 0


# ── 规则检查 ──────────────────────────────────────────────────────────────────


def _check_agent(
    md: Path,
    agent_root: Path,
    is_folder: bool,
    findings: list[Finding],
    reported_layout: set[Path],
) -> None:
    """单个 agent md 的全部静态检查。``reported_layout`` 跨 agent 去重平铺脚本
    （file agent 共享池根目录时避免重复报告同一文件）。"""
    text, replaced = _read_text(md)
    if replaced:
        findings.append((md, 1, "warn", "非 UTF-8 文件，已按替换字符读入——请检查编码"))
    lines = text.splitlines()

    if is_folder and not _has_frontmatter(lines):
        findings.append(
            (md, 1, "frontmatter", "folder agent 缺合法 frontmatter（首行 --- 起、且有闭合 --- 的 YAML 头；首行含 BOM 等不可见字符同样不被引擎识别）")
        )

    try:
        entries = sorted(agent_root.iterdir())
    except OSError as e:
        raise _ReadError(f"读取目录失败 {agent_root}：{e}") from e
    for f in entries:
        if f.is_file() and f.suffix in _SCRIPT_SUFFIXES and f not in reported_layout:
            reported_layout.add(f)
            findings.append(
                (f, 1, "layout", f"{f.name} 平铺在 agent 根——脚本必须放 scripts/ 子目录")
            )

    body = lines[_body_start(lines):]
    body_offset = len(lines) - len(body)
    for lineno, line in enumerate(body, body_offset + 1):
        for m in _SCRIPT_REF_RE.finditer(line):
            # env 引导（含引号先闭写法 ``"$ORCA_AGENT_RESOURCES"/scripts/x.py``）合法。
            if line[: m.start()].rstrip("/").endswith(_ENV_REF_FORMS):
                continue
            findings.append(
                (md, lineno, "script-ref", f"{m.group(0)} 是相对引用——必须写 $ORCA_AGENT_RESOURCES/scripts/<file>")
            )

    _check_inline_logic(md, body, findings)


def _py_inline_arg(line: str, start: int) -> str:
    """``-c`` 之后的参数串：首个引号包裹段，或到空白/注释/管道连接符截止的裸词。

    只在参数串内找控制流 token——``-c 'x' && echo "wait for done"`` 里的后续
    命令文本不参与判定。
    """
    rest = line[start:].lstrip()
    if not rest:
        return ""
    if rest[0] in "'\"":
        end = rest.find(rest[0], 1)
        return rest[1:] if end < 0 else rest[1:end]
    m = re.match(r"[^\s&#|]+", rest)
    return m.group(0) if m else ""


def _check_inline_logic(md: Path, lines: list[str], findings: list[Finding]) -> None:
    """固化漏判两形态（error）+ 超长纯顺序围栏（warning）。"""
    # 形态二：行内 python 单行程序。不限围栏——引号串里的内联逻辑在 prose 里同样
    # 是给执行 agent 的确定性代码。
    for lineno, line in enumerate(lines, 1):
        for m in _PY_INLINE_RE.finditer(line):
            arg = _py_inline_arg(line, m.end())
            if any(token in arg for token in _PY_LOGIC_TOKENS):
                findings.append(
                    (md, lineno, "inline-python", f"python -c 内联逻辑——确定性代码应抽到 scripts/：{line.strip()}")
                )
                break
    # 形态一：bash 围栏启行控制流 + 超长纯顺序围栏提示。
    for start, body in _iter_bash_fences(lines):
        has_control = False
        for lineno, text in body:
            if _FENCE_CONTROL_RE.match(text):
                has_control = True
                findings.append(
                    (md, lineno, "inline-shell", f"围栏内控制流——确定性代码应抽到 scripts/：{text.strip()}")
                )
        if not has_control:
            cmd_lines = [t for _, t in body if t.strip()]
            if len(cmd_lines) > _LONG_FENCE_LINES:
                findings.append(
                    (
                        md,
                        start,
                        "warn",
                        f"bash 围栏 {len(cmd_lines)} 行纯顺序命令——建议抽到 scripts/ 子目录（提示，不强制）",
                    )
                )


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="check_agent_md_static.py",
        description="agent.md 静态契约（布局/引用/frontmatter）+ 固化漏判启发检查",
    )
    ap.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="workflow yaml（扫同级 agents/）、agent md 或 folder agent 目录",
    )
    args = ap.parse_args(argv)
    # 控制台编码与 locale 不一致时降级为替换字符输出（不因一个字符崩掉退出码语义）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")

    inputs = [Path(p) for p in args.paths]
    missing = [p for p in inputs if not p.exists()]
    bad_type = [
        p
        for p in inputs
        if p.exists() and p.is_file() and p.suffix not in _YAML_SUFFIXES | {".md"}
    ]
    if missing or bad_type:
        for p in missing:
            print(f"路径不存在：{p}", file=sys.stderr)
        for p in bad_type:
            print(f"不支持的输入（需 workflow yaml / agent md / 目录）：{p}", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    reported_layout: set[Path] = set()
    try:
        for path in inputs:
            agents, count = _collect_agents(path)
            print(f"{path} → {count} files")
            for md, agent_root, is_folder in agents:
                _check_agent(md, agent_root, is_folder, findings, reported_layout)
    except _ReadError as e:
        print(str(e), file=sys.stderr)
        return 2
    for f, line, rule, excerpt in findings:
        print(f"{f}:{line} [{rule}] {excerpt}")
    return 1 if any(rule != "warn" for _, _, rule, _ in findings) else 0


if __name__ == "__main__":
    sys.exit(main())

"""check_dev_residue.py —— 产物开发期残留宽口径扫描（fail loud）。

agent.md body / workflow yaml / agent scripts 是给**运行时受众**的产物；开发期
导航信息（编号、出处叙事、评审与里程碑标记）只服务作者与 reviewer，不该进产物。
本脚本把宽口径 pattern 表固化为 deterministic 检查，供创建/修改 workflow 后自检；
与引擎内建的窄表互补（窄表零歧义、warning 不阻断；本表更宽、error 级——新建
产物要求零命中）。

用法::

    python3 check_dev_residue.py <path...> [--allow <regex>...]

- 输入为文件（显式给出的文件无论后缀都扫）或目录（递归扫 .yaml/.md/.py）。
- stdout：每个输入一行扫描清单 ``<path> → N files``（零文件是合法输出，
  exit 0），随后逐条 finding ``<file>:<line> [<rule>] <excerpt>``。
- exit：0 = 无 error（可有 [warn]）；1 = 有 error finding；2 = 用法错
  （路径不存在 / --allow 正则非法，报告到 stderr）。

豁免语义：error 命中的 span 被**任一**豁免 regex 的匹配包含时，仅抑制该命中
（同行其它命中照报；不做行级豁免）。内置豁免表 = 领域模型/编码标准名 + 延迟
分位记号；``--allow`` 追加自定义豁免。文档导航串（specs 路径、变更日志名等）
自身不构成 finding——当前规则表下它们无命中；若未来加源码路径类规则，需为
运行时 env / skill 资源 / 图表 API 引用预留豁免。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ── 规则表 ────────────────────────────────────────────────────────────────────

# error 级规则（顺序即同文件内报告顺序）。个别字面量在源码里拆分拼接——本脚本
# 对自身扫描时不得命中自己的规则表（自举零 finding）。
_RULES: tuple[tuple[str, str], ...] = (
    # 宽口径开发编号（issue/plan 追踪号）。长名内嵌的编号片段同样命中，豁免表
    # 按 span 包含排除领域模型名。
    ("dev-id", r"[A-Z]+-[0-9]+"),
    # 迁移出处 / 考古叙事 / 评审记录泄漏。
    (
        "archaeology",
        "|".join(
            (
                "迁移" + "自",
                "analogue " + "of",
                "leaves " + "off",
                "前" + "作",
                "前身" + "是",
                "演进" + "历史",
                "spec-" + "review",
                "spec_" + "review",
                r"v[0-9]+ 已嵌入",
                r"plan [a-z-]+ §",
                r"SPEC 20[0-9]{2}-",
            )
        ),
    ),
    # 宽口径兜底：优先级/里程碑/评审流程标记。边界用 ASCII 环视（类含下划线；
    # CJK 邻接视为边界——中文产物里紧邻汉字的单数字 P 记号必须命中，\b 在 CJK
    # 旁会静默失效）。
    (
        "milestone",
        r"(?<![A-Za-z0-9_])P[0-9](?![0-9A-Za-z_])"
        r"|(?<![A-Za-z0-9_])Increment [A-Z](?![0-9A-Za-z_])"
        r"|(?<![A-Za-z0-9_])code-" + "reviewer" + r"(?![0-9A-Za-z_])"
        r"|review #[0-9]"
        r"|(?<![A-Za-z0-9_])SR[0-9](?![0-9A-Za-z_])"
        r"|finalize 20[0-9]{2}",
    ),
)

_RULE_RES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pat)) for name, pat in _RULES
)

# 豁免表（span 包含语义）。领域模型/编码标准名 + 延迟分位记号（两位以上数字，
# 性能分位语义，不是里程碑编号；当前规则表下是前向保护——单数字规则因环视
# 永不命中两位以上串，保留它以钉住双层语义、防未来规则误伤分位）。
_EXEMPT_BUILTIN: tuple[str, ...] = (
    r"ViT-\d+",
    r"GPT-\d+",
    r"YOLO-\d+",
    r"UTF-\d+",
    r"ISO-\d+",
    r"(?<![A-Za-z0-9_])P[0-9]{2,3}(?![0-9A-Za-z_])",
)

_SCAN_SUFFIXES = (".yaml", ".md", ".py")

# ── 扫描 ──────────────────────────────────────────────────────────────────────

Finding = tuple[Path, int, str, str]  # (file, line, rule, excerpt)


class _ReadError(Exception):
    """文件读取失败（权限/IO 等环境错）——main 捕获后 stderr + exit 2，不冒充 findings。"""


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


def _expand(path: Path) -> list[Path]:
    """目录 → 递归收集三类后缀文件（排序保证清单稳定）；文件 → 自身。"""
    if path.is_dir():
        return sorted(
            p for suffix in _SCAN_SUFFIXES for p in path.rglob(f"*{suffix}")
        )
    return [path]


def _scan_file(path: Path, exempt: list[re.Pattern[str]]) -> list[Finding]:
    """单文件扫描 → findings。行序优先、规则序次之；豁免命中已剔除。"""
    text, replaced = _read_text(path)
    out: list[Finding] = []
    if replaced:
        out.append((path, 1, "warn", "非 UTF-8 文件，已按替换字符读入——请检查编码"))
    for lineno, line in enumerate(text.splitlines(), 1):
        exempt_spans = [
            (m.start(), m.end()) for rx in exempt for m in rx.finditer(line)
        ]
        for name, rx in _RULE_RES:
            for m in rx.finditer(line):
                if any(s <= m.start() and m.end() <= e for s, e in exempt_spans):
                    continue  # 被豁免匹配包含 → 仅抑制该命中
                out.append((path, lineno, name, m.group(0)))
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="check_dev_residue.py",
        description="产物开发期残留宽口径扫描（yaml/md/py；error 级 fail loud）",
    )
    ap.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="产物文件或目录（目录递归扫 .yaml/.md/.py 三类后缀）",
    )
    ap.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="REGEX",
        help="追加豁免正则（可多次；其匹配 span 包含 error 命中时抑制该命中）",
    )
    args = ap.parse_args(argv)
    # 控制台编码与 locale 不一致时降级为替换字符输出（不因一个箭头字符崩掉退出码语义）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")

    inputs = [Path(p) for p in args.paths]
    missing = [p for p in inputs if not p.exists()]
    if missing:
        for p in missing:
            print(f"路径不存在：{p}", file=sys.stderr)
        return 2
    try:
        exempt = [re.compile(p) for p in _EXEMPT_BUILTIN]
        exempt += [re.compile(p) for p in args.allow]
    except re.error as e:
        print(f"--allow 正则非法：{e}", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    try:
        for path in inputs:
            files = _expand(path)
            print(f"{path} → {len(files)} files")
            for f in files:
                findings.extend(_scan_file(f, exempt))
    except _ReadError as e:
        print(str(e), file=sys.stderr)
        return 2
    for f, line, rule, excerpt in findings:
        print(f"{f}:{line} [{rule}] {excerpt}")
    return 1 if any(rule != "warn" for _, _, rule, _ in findings) else 0


if __name__ == "__main__":
    sys.exit(main())

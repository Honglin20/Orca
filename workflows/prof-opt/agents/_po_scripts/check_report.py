#!/usr/bin/env python3
"""Pre-return gate for po_report: the human report's structural contract.

Validates prof_opt_report.md BEFORE the node relays the builder's JSON:
the eleven required section headings and — inside the disclosure section —
the three fixed disclosure lines' anchor tokens (profiling source /
training device backend / chart daemon state). The heading and token
literals live HERE (single source); report_format.md enumerates them as
the authoring contract and tests pin the two against drift.

Exit 0 pass / 1 fail (findings on stderr) / 2 hard usage error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SECTIONS = ("## 披露", "## 终态", "## 逐轮表", "## 训练结局披露", "## 胜出者",
            "## 公平性说明", "## 基线与最终", "## 轮次结论", "## 精度规则",
            "## 写回", "## 面板与文档")
DISCLOSURE_HEADING = "## 披露"
DISCLOSURE_TOKENS = ("mfu 实测", "train_device", "chart daemon")


def _disclosure_body(lines: list[str]) -> str:
    """The disclosure section's text: from its heading to the next heading."""
    body: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == DISCLOSURE_HEADING:
            inside = True
            continue
        if inside and stripped.startswith("## "):
            break
        if inside:
            body.append(line)
    return "\n".join(body)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ns = ap.parse_args()
    report = Path(ns.artifacts) / "prof_opt_report.md"
    if not report.is_file() or report.stat().st_size == 0:
        print("check_report: FAIL prof_opt_report.md missing or empty "
              f"({report})", file=sys.stderr)
        return 1
    lines = report.read_text(encoding="utf-8").splitlines()
    headings = {l.strip() for l in lines if l.strip().startswith("#")}
    problems = [f"missing section heading {h!r}" for h in SECTIONS
                if h not in headings]
    body = _disclosure_body(lines)
    if not body.strip():
        problems.append("the disclosure section is empty")
    else:
        problems += [f"the disclosure section lacks the required token "
                     f"{t!r}" for t in DISCLOSURE_TOKENS if t not in body]
    if problems:
        for p in problems:
            print(f"check_report: FAIL {p}", file=sys.stderr)
        return 1
    print(f'{{"ok": true, "sections": {len(SECTIONS)}}}')
    return 0


if __name__ == "__main__":
    sys.exit(main())

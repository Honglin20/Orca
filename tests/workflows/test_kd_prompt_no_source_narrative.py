"""test_kd_prompt_no_source_narrative.py —— kd-nas agent prompt 任务纯净态守门。

Phase 4（agent prompt 去 SPEC 源化）的反回归测试：扫 kd-nas workflow 的所有 agent
prompt（agent.md / SKILL.md / references 下的 workflow doc + leaf skel + checklists）+
yaml 节点 description + 引擎库代码注释，断言无过程性源叙事残留。

**为什么需要**：Phase 4 把 SPEC §x.y / SPEC-REVIEW NX / spec-review mX / cleanup 20XX /
v5 变更 / Phase N / plan §x / 决策标签 (E4/N21/M8/D2/E6/A8/Q6/...) 全部清掉。未来重写
agent prompt 容易把过程标签重新写回——本测试当场红。

**边界（Rule 7 显式说明）**：
- 不锁正文章节引用 ``CONTRACTS.md §N`` ——它指 live 契约文档的组织结构，是合法导航。
- 不锁 ``nas_agent.train.distillation`` ——它是 forbidden token 字面量（LLM 不能 import
  的名字），非过程叙事。
- 引擎库代码 (.py) 的 ``importlib.util.spec`` / ``inspect.signature`` 是 Python 内置
  变量名——排除（伪命中）。

详见 ``docs/releases/2026-08-04-kd-nas-trainer-engine-phase4.md``。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# kd-nas workflow 范围（agent prompt 层 + 引擎库 + 契约文档 + yaml）
KD_NAS_DIRS = [
    REPO / "workflows" / "agents" / "model-flatten",
    REPO / "workflows" / "agents" / "teacher-gen",
    REPO / "workflows" / "agents" / "kd-setup",
    REPO / "workflows" / "agents" / "kd-train-script",
    REPO / "workflows" / "agents" / "train-script-verify",
    REPO / "workflows" / "agents" / "train-teacher",
    REPO / "workflows" / "agents" / "gen-student",
    REPO / "workflows" / "agents" / "distill",
    REPO / "workflows" / "agents" / "decide",
    REPO / "workflows" / "agents" / "_kd_scripts",
]
KD_NAS_YAML = REPO / "workflows" / "kd-nas.yaml"

# 过程性源叙事 deny-list。明确无歧义——不锁 "SPEC §N"（章节导航可能合法，仅锁复合标签）。
SOURCE_NARRATIVE = re.compile(
    r"""
    SPEC-REVIEW
    | spec-review\s+[mMnNqQ]\d
    | cleanup\s+20\d{2}
    | v5\s+变更
    | deleted\s+20\d{2}
    | historical\s+train_adapter_template
    | \bport\s+from\b
    | 前作
    | 重写对象
    | 已删
    | 已废
    | 废弃
    | deprecated
    | 本计划
    | 本\s*SPEC
    | Phase\s+[1-9]
    | plan\s*§
    | \(E[1-9]\d?               # (E4) / (E13) / (E6) 等决策标签
    | \(M[1-9]\d?               # (M6) / (M8) 等
    | \(N[1-9]\d?               # (N21) / (N12) 等
    | \(D[1-9]\d?               # (D2) / (D9) 等
    | \(Q[1-9]\d?               # (Q6) / (Q22) 等
    | \(A[1-9]\d?               # (A8) 等
    | \(B[1-9]\d?               # (B5) / (B6) 等
    | \(R[1-9]\d?               # (R2) 等
    | D2\s+hard\s+check
    """,
    re.VERBOSE,
)

# 合法上下文（false-positive 剔除）：Python 内置 spec / signature / argparse 描述里的英文
# "spec" 变量名 / "asks for cleanup"（字面英文）/ forbidden-token 字面量。
ALLOW_LINE_SUBSTRINGS = (
    "importlib",
    "spec_from_file",
    "spec =",
    "spec.loader",
    "module_from_spec",
    "proxy_dataset_spec",
    "spec_raw",
    "spec_used",
    "inspect.spec",
    "inspect.signature",
    "signature(",
    "import spec",
    "asks for cleanup",
    "cleanup.",
    '"cleanup"',
)


def _collect_files() -> list[Path]:
    """收集 kd-nas agent prompt + 引擎库 + 契约 + yaml 所有 .md / .py / .skel / .yaml 文件。"""
    out: list[Path] = []
    for d in KD_NAS_DIRS:
        if d.is_dir():
            for p in d.rglob("*"):
                if p.is_file() and p.suffix in (".md", ".py", ".skel", ".yaml"):
                    out.append(p)
    if KD_NAS_YAML.is_file():
        out.append(KD_NAS_YAML)
    return sorted(set(out))


def _scan(path: Path) -> list[tuple[int, str, str]]:
    """返回 [(line_no, matched_token, line_text), ...]。允许行的 false-positive 自动跳过。"""
    hits: list[tuple[int, str, str]] = []
    text = path.read_text(encoding="utf-8")
    for m in SOURCE_NARRATIVE.finditer(text):
        line_no = text[: m.start()].count("\n") + 1
        line = text.split("\n")[line_no - 1]
        if any(sub in line for sub in ALLOW_LINE_SUBSTRINGS):
            continue
        # nas_agent.train.distillation 是 forbidden-token 字面量，不是源叙事
        if "nas_agent.train" in line and ("forbidden" in line.lower() or "do not" in line.lower()
                                          or "❌" in line or "`nas_agent" in line):
            continue
        hits.append((line_no, m.group().strip(), line.strip()[:160]))
    return hits


def test_kd_nas_no_source_narrative() -> None:
    """kd-nas workflow 全量扫：过程性源叙事残留必须 = 0。"""
    failures: list[str] = []
    for p in _collect_files():
        for line_no, token, line in _scan(p):
            rel = p.relative_to(REPO)
            failures.append(f"  {rel}:{line_no}  [{token!r}]  {line}")
    assert not failures, (
        "Phase 4 任务纯净态回归——以下位置出现过程性源叙事残留\n"
        + "\n".join(failures)
        + "\n\n见 docs/releases/2026-08-04-kd-nas-trainer-engine-phase4.md（D7 边界）。"
    )

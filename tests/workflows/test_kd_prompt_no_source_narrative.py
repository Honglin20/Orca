"""test_kd_prompt_no_source_narrative.py —— kd-nas agent prompt 任务纯净态守门。

Phase 4（agent prompt 去 SPEC 源化）+ Phase 5（决策标签 / 历史叙事清扫）的反回归测试。
扫 kd-nas workflow 的所有 agent prompt（agent.md / SKILL.md / references 下的 workflow doc
+ leaf skel + checklists）+ yaml 节点 description + 引擎库代码注释，断言无过程性源叙事残留。

**为什么需要**：Phase 4 把 SPEC §x.y / SPEC-REVIEW NX / spec-review mX / cleanup 20XX /
v5 变更 / Phase N / plan §x 全部清掉；Phase 5 把审查过程决策标签（D2/D8/E13/M8/N10/Q6/
B4/R1/F3/A4 等非括号形式）+ 历史叙事词（「已拆到 / 不再 import / 合并…为一节点 / 旧…现… /
随骨架化移除」）清干净。未来重写 agent prompt 容易把过程标签重新写回——本测试当场红。

**范围分层（D7 客观边界，Rule 7 显式说明）**：
- **agent prompt 层**（``.md`` / ``.yaml`` / ``.skel``）：用户硬线对象——既锁括号决策标签
  也锁非括号决策标签 + 历史叙事词，必须 0 命中。
- **引擎库代码 (``.py``)**：允许设计注释（why）——只锁括号决策标签 + 复合源叙事（SPEC-REVIEW/
  Phase N/前作/重写对象/...），不锁非括号决策标签（避免误伤 ``E402`` noqa 之类合法 token）。

**合法上下文剔除（false-positive）**：
- 不锁正文章节引用 ``CONTRACTS.md §N`` ——它指 live 契约文档的组织结构，是合法导航。
- 不锁 ``nas_agent.train.distillation`` ——它是 forbidden token 字面量（LLM 不能 import
  的名字），非过程叙事。
- 引擎库代码 (.py) 的 ``importlib.util.spec`` / ``inspect.signature`` 是 Python 内置
  变量名——排除（伪命中）。
- ``# noqa: E402`` 是 flake8 行内豁免标记——排除（合法 lint code，非决策标签）。

详见 ``docs/releases/2026-08-04-kd-nas-trainer-engine-phase5.md``。
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

# ── 共享 deny-list（所有文件类型都锁，含引擎 .py） ───────────────────────────
# 过程性源叙事 + 括号形式决策标签。明确无歧义——不锁 "SPEC §N"（章节导航可能合法，仅锁复合标签）。
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

# ── agent prompt 层专属 deny-list（仅 .md / .yaml / .skel；不扫 .py） ────────
# Phase 5 强化：堵非括号决策标签 + 历史叙事词的假阴性。决策标签 = 审查过程 ID
# （D2/D8/E13/M3/M8/N10/N12/Q6/B4/R1/F3/A4/A8…），agent prompt 不应承载这类过程归属。
# 规则：``[DEMNQBRAF]\d{1,2}`` 且后接 ``：`` / ``:`` / ``）`` / 行尾 / 空白，或被括号包裹——
# 判定为过程标签而非合法技术 token。E402（flake8 code）是 3 位数字，且仅出现在 noqa 行，
# 双重免疫（长度 + allow-list）。
PROMPT_DECISION_TAG = re.compile(
    r"""
    \b([DEMNQBRAF])([1-9]\d?)\b(?=[：:）\s])   # 裸标签后跟分隔/收尾
    | \b([DEMNQBRAF])([1-9]\d?)/([DEMNQBRAF])([1-9]\d?)\b  # E13/M1 这种斜线复合
    """,
    re.VERBOSE,
)

# 历史叙事词（迁移对比 / 合并拆分历史 / 旧→现 对照）——agent prompt 只写当前契约。
HISTORICAL_NARRATIVE = re.compile(
    r"""
    已拆到
    | 不再\s*import
    | 合并[^。\n]{0,30}为一节点
    | 旧[^。\n]{0,30}现[^。\n]{0,30}   # 「旧 X，现 Y」迁移对照
    | 随骨架化移除
    | 拆到独立
    | 拆到\s*train_teacher
    """,
    re.VERBOSE,
)

# 合法上下文（false-positive 剔除）：Python 内置 spec / signature / argparse 描述里的英文
# "spec" 变量名 / "asks for cleanup"（字面英文）/ forbidden-token 字面量 / flake8 noqa。
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
    "noqa",  # # noqa: E402 等 flake8 行内豁免
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


def _is_prompt_layer(path: Path) -> bool:
    """agent prompt 层 = .md / .yaml / .skel（.py 是引擎库，走更宽松的共享 deny-list）。"""
    return path.suffix in (".md", ".yaml", ".skel")


def _line_is_allowed(line: str) -> bool:
    """行级 false-positive 剔除（flake8 noqa / forbidden-token 字面量 / Python 内置 spec）。"""
    if any(sub in line for sub in ALLOW_LINE_SUBSTRINGS):
        return True
    # nas_agent.train.distillation 是 forbidden-token 字面量，不是源叙事
    if "nas_agent.train" in line and ("forbidden" in line.lower() or "do not" in line.lower()
                                      or "❌" in line or "`nas_agent" in line):
        return True
    return False


def _scan(path: Path) -> list[tuple[int, str, str]]:
    """返回 [(line_no, matched_token, line_text), ...]。允许行的 false-positive 自动跳过。"""
    hits: list[tuple[int, str, str]] = []
    text = path.read_text(encoding="utf-8")
    patterns = [SOURCE_NARRATIVE]
    if _is_prompt_layer(path):
        patterns += [PROMPT_DECISION_TAG, HISTORICAL_NARRATIVE]
    for pat in patterns:
        for m in pat.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            line = text.split("\n")[line_no - 1]
            if _line_is_allowed(line):
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
        "任务纯净态回归——以下位置出现过程性源叙事残留\n"
        + "\n".join(failures)
        + "\n\n见 docs/releases/2026-08-04-kd-nas-trainer-engine-phase5.md（D7 边界）。"
    )

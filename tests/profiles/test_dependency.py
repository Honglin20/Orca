"""tests/profiles/test_dependency.py —— 依赖单向铁律：profiles/ 不依赖 exec/。

覆盖 phase 4 架构决策 1（translator 归 profiles）的关键不变量：
  - translator 是 per-backend 协议知识，归 profiles 层（与 profile 同居）；
  - profiles/ **不得** import orca.exec —— 否则触发 profiles→exec 反向依赖，
    破坏「exec→schema+events+profiles 单向」铁律（CLAUDE.md 依赖铁律）。

铁律 1/2（exec/ 不依赖 run/compile、exec/ 不写 tape）的回归保护在
``tests/exec/test_contract.py``；本文件补齐决策 1 引入的「profiles/ 不依赖 exec」
这第三条依赖断言，phase 5+ 加新 profile/translator 时若有人图方便反向 import 会被此测试挡下。
"""

from __future__ import annotations

from pathlib import Path

PROFILES_DIR = Path(__file__).resolve().parents[2] / "orca" / "profiles"


def _walk_py(root: Path):
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def test_dependency_no_exec():
    """铁律（决策 1）：profiles/ 不 import orca.exec。

    translator 归 profiles 后，profiles/translators/ 是最可能图方便反向 import exec 的地方；
    本测试 pin 死这条边界，保证「加 backend = 丢一个 profile 文件，零 exec 改动」的 OCP
    不被反向依赖悄悄破坏。
    """
    banned = ("from orca.exec", "import orca.exec")
    hits = []
    for p in _walk_py(PROFILES_DIR):
        text = p.read_text(encoding="utf-8")
        for b in banned:
            if b in text:
                hits.append(f"{p.relative_to(PROFILES_DIR.parent.parent)}: {b}")
    assert not hits, f"profiles/ 反向依赖 exec（违反决策 1 依赖铁律）：\n{chr(10).join(hits)}"

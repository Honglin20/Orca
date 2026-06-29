"""tests/compile 共享 fixtures 与 helpers。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def examples_dir() -> Path:
    return EXAMPLES


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


def write_yaml(tmp_path: Path, doc: str | dict, name: str = "wf.yaml") -> Path:
    """把 YAML 文本/对象写到 tmp_path/name，返回路径。"""
    p = tmp_path / name
    text = doc if isinstance(doc, str) else yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    p.write_text(text, encoding="utf-8")
    return p

"""test_web_registry.py —— per-project web server 端口登记（``<runs_dir>/.orca-web.json``）。

SPEC web-attach §5a（spec-review 闭环）。覆盖意图：
  - ``read_registry``：缺失 / 损坏 JSON / 非 dict → ``None``（自愈）。
  - ``write_registry`` → ``read_registry`` roundtrip（字段 ``{port, runs_dir_fp}``，无 pid——H1）。
  - 原子写（tmp + os.replace）：无半写 ``.tmp`` 残留。
  - ``registry_path`` 落 ``<runs_dir>/.orca-web.json``。
"""

from __future__ import annotations

import json
from pathlib import Path

from orca.iface.cli.web_registry import (
    REGISTRY_NAME,
    read_registry,
    registry_path,
    write_registry,
)


def test_registry_path_location(tmp_path: Path):
    """登记文件落 ``<runs_dir>/.orca-web.json``。"""
    runs_dir = tmp_path / "runs"
    assert registry_path(runs_dir) == runs_dir / REGISTRY_NAME
    assert registry_path(runs_dir).name == ".orca-web.json"


def test_read_registry_missing_returns_none(tmp_path: Path):
    """无登记文件 → None（调用方按「无登记」处理）。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    assert read_registry(runs_dir) is None


def test_read_registry_corrupt_json_returns_none(tmp_path: Path):
    """损坏 JSON → None（自愈，不抛）。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    registry_path(runs_dir).write_text("{not valid json", encoding="utf-8")
    assert read_registry(runs_dir) is None


def test_read_registry_non_dict_returns_none(tmp_path: Path):
    """合法 JSON 但非 dict（如 list）→ None。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    registry_path(runs_dir).write_text("[1, 2, 3]", encoding="utf-8")
    assert read_registry(runs_dir) is None


def test_write_then_read_roundtrip(tmp_path: Path):
    """write → read roundtrip；字段 ``{port, runs_dir_fp}``，**无 pid**（H1）。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    write_registry(runs_dir, port=7429, runs_dir_fp="abc123def456")
    reg = read_registry(runs_dir)
    assert reg == {"port": 7429, "runs_dir_fp": "abc123def456"}
    # H1：registry 不存 pid（Popen.pid 可能是 wrapper pid，潜在错误数据）。
    assert "pid" not in reg


def test_write_registry_atomic_no_tmp_residue(tmp_path: Path):
    """原子写：成功后无 ``.tmp`` 残留。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    write_registry(runs_dir, port=8000, runs_dir_fp="fp")
    assert (runs_dir / ".orca-web.json").is_file()
    assert not (runs_dir / ".orca-web.json.tmp").exists()


def test_write_registry_creates_parent(tmp_path: Path):
    """``mkdir`` 兜底：runs_dir 不存在时建之（``--tape`` 指 runs/ 外的边界）。"""
    runs_dir = tmp_path / "runs"  # 不 mkdir
    write_registry(runs_dir, port=9000, runs_dir_fp="fp")
    assert read_registry(runs_dir) == {"port": 9000, "runs_dir_fp": "fp"}


def test_write_registry_overwrites_previous(tmp_path: Path):
    """二次写覆盖（spawn 后更新 port）。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    write_registry(runs_dir, port=7000, runs_dir_fp="old")
    write_registry(runs_dir, port=7001, runs_dir_fp="new")
    reg = read_registry(runs_dir)
    assert reg == {"port": 7001, "runs_dir_fp": "new"}

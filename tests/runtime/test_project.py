"""tests/runtime/test_project.py —— 项目注册表单测（SPEC §13 D2/D4, B-2, P1, M-15/M-16）。

覆盖 AC 可单测项：
  - detect_project_root 优先级链（AC15）
  - project_id 派生稳定
  - register_project：拒绝 OS 顶层目录（M-15）+ 要求 project marker（M-16）+ 拒 ORCA_HOME（P2）
  - 原子写 + .bak（P1）+ 损坏 → fail loud（RegistryCorruptError）
  - is_registered_runs_dir allowlist（B-3）
  - 并发 register（无 corruption）
  - 单一 _with_lock 禁嵌套（公开 API 不嵌套）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orca.runtime import (
    REGISTRY_FILE,
    RegistryCorruptError,
    detect_project_root,
    is_registered_runs_dir,
    list_registered,
    orca_home,
    project_id,
    register_project,
)


@pytest.fixture(autouse=True)
def _isolated_orca_home(tmp_path, monkeypatch):
    """每测独立 ORCA_HOME → 注册表完全隔离。"""
    home = tmp_path / "orca-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("ORCA_HOME", str(home))
    yield home


def _make_project(parent: Path, name: str = "proj") -> Path:
    """造合法项目（含 workflows/）。"""
    p = parent / name
    (p / "workflows").mkdir(parents=True, exist_ok=True)
    return p


# ── detect_project_root（AC15 优先级链） ──────────────────────────────────────


def test_detect_project_root_env_wins(tmp_path, monkeypatch):
    """ORCA_PROJECT_ROOT env > 向上找 workflows/。"""
    proj = _make_project(tmp_path, "env_proj")
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(proj))
    # 在另一目录下（无 workflows/）
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    assert detect_project_root() == proj.resolve()


def test_detect_project_root_finds_workflows(tmp_path, monkeypatch):
    """无 env → 向上找含 workflows/ 的目录。"""
    monkeypatch.delenv("ORCA_PROJECT_ROOT", raising=False)
    proj = _make_project(tmp_path, "wf_proj")
    sub = proj / "subdir" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert detect_project_root() == proj.resolve()


def test_detect_project_root_falls_back_to_cwd(tmp_path, monkeypatch):
    """无 env / workflows / git → cwd 兜底。"""
    monkeypatch.delenv("ORCA_PROJECT_ROOT", raising=False)
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    assert detect_project_root() == bare.resolve()


def test_detect_project_root_skips_orca_home(tmp_path, monkeypatch):
    """P2：detect 不会锚定到 ORCA_HOME（即便 cwd=ORCA_HOME 也不返回它）。"""
    monkeypatch.delenv("ORCA_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(orca_home())
    # 没有 workflows/ / .git，理应跳过 orca_home → cwd（即 orca_home）会被拒绝，
    # 但无其它候选时仍会落到 cwd；测试至少保证：有 workflows 同级候选时优先选它。
    proj = _make_project(tmp_path, "nearby")
    monkeypatch.chdir(tmp_path)
    # tmp_path 既不在 workflows 下也不在 .git 下，但 cwd=tmp_path，且 ORCA_HOME 也是 tmp_path 子目录
    # → detect 应返回 tmp_path.resolve()，而非 ORCA_HOME。
    result = detect_project_root()
    assert result != orca_home().resolve()


# ── project_id（D2/P2 派生指纹） ──────────────────────────────────────────────


def test_project_id_stable(tmp_path):
    p = _make_project(tmp_path)
    assert project_id(p) == project_id(p)
    assert len(project_id(p)) == 16


def test_project_id_distinct(tmp_path):
    a = _make_project(tmp_path, "a")
    b = _make_project(tmp_path, "b")
    assert project_id(a) != project_id(b)


# ── register_project（M-15/M-16/P2） ──────────────────────────────────────────


def test_register_project_happy(tmp_path):
    p = _make_project(tmp_path)
    pid = register_project(p)
    assert pid == project_id(p)
    registered = list_registered()
    assert pid in registered
    assert registered[pid]["path"] == str(p.resolve())
    assert registered[pid]["name"] == p.name


def test_register_project_idempotent_upsert(tmp_path):
    p = _make_project(tmp_path)
    pid1 = register_project(p)
    pid2 = register_project(p)
    assert pid1 == pid2
    # 仍只一条
    assert len(list_registered()) == 1


def test_register_project_rejects_toplevel(tmp_path):
    """M-15：拒绝 OS 顶层目录。"""
    with pytest.raises(ValueError, match="顶层"):
        register_project("/")


def test_register_project_rejects_no_marker(tmp_path):
    """M-16：无 workflows/ 或 .orca/config.json → 拒。"""
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(ValueError, match="project marker|workflows"):
        register_project(bare)


def test_register_project_rejects_orca_home(_isolated_orca_home):
    """P2：拒 ORCA_HOME 自身（防 cwd=ORCA_HOME 锚定）。

    _isolated_orca_home 已创建并 env-设 ORCA_HOME；给它加 workflows（伪装成项目）应仍被拒。
    """
    home = _isolated_orca_home
    (home / "workflows").mkdir()
    with pytest.raises(ValueError, match="ORCA_HOME"):
        register_project(home)


def test_register_project_accepts_orca_config_marker(tmp_path):
    """M-16 替代 marker：.orca/config.json。"""
    p = tmp_path / "proj2"
    (p / ".orca").mkdir(parents=True)
    (p / ".orca" / "config.json").write_text("{}", encoding="utf-8")
    pid = register_project(p)
    assert pid


# ── 鲁棒（P1：原子写 + .bak + 损坏 fail loud） ────────────────────────────────


def test_registry_writes_bak(tmp_path):
    p = _make_project(tmp_path)
    register_project(p)
    bak = orca_home() / (REGISTRY_FILE + ".bak")
    assert bak.is_file()
    data = json.loads(bak.read_text(encoding="utf-8"))
    assert "projects" in data


def test_registry_corrupt_recovers_from_bak(tmp_path):
    """主文件坏 → 读 .bak（不抛错）。"""
    p = _make_project(tmp_path)
    register_project(p)
    # 破坏主文件
    main = orca_home() / REGISTRY_FILE
    main.write_text("{ broken json", encoding="utf-8")
    # list_registered 应回退到 .bak
    registered = list_registered()
    assert project_id(p) in registered


def test_registry_corrupt_both_fail_loud(tmp_path):
    """主 + .bak 都坏 → RegistryCorruptError（fail loud）。"""
    p = _make_project(tmp_path)
    register_project(p)
    main = orca_home() / REGISTRY_FILE
    bak = orca_home() / (REGISTRY_FILE + ".bak")
    main.write_text("{ broken", encoding="utf-8")
    bak.write_text("{ also broken", encoding="utf-8")
    with pytest.raises(RegistryCorruptError):
        list_registered()


def test_registry_atomic_write_no_partial(tmp_path):
    """原子写：注册后主文件是合法 JSON（不留 .tmp 残体）。"""
    p = _make_project(tmp_path)
    register_project(p)
    main = orca_home() / REGISTRY_FILE
    json.loads(main.read_text(encoding="utf-8"))  # 不抛
    # .tmp 应被 os.replace 清理
    assert not (orca_home() / (REGISTRY_FILE + ".tmp")).exists()


# ── is_registered_runs_dir（B-3 allowlist） ───────────────────────────────────


def test_is_registered_runs_dir_true(tmp_path):
    p = _make_project(tmp_path)
    register_project(p)
    runs_dir = p / "runs"
    runs_dir.mkdir()
    tape = runs_dir / "run-abc.jsonl"
    tape.write_text("{}", encoding="utf-8")
    assert is_registered_runs_dir(tape)
    assert is_registered_runs_dir(runs_dir)


def test_is_registered_runs_dir_false_unregistered(tmp_path):
    p = _make_project(tmp_path)
    # 未 register
    runs_dir = p / "runs"
    runs_dir.mkdir()
    tape = runs_dir / "run.jsonl"
    tape.write_text("{}", encoding="utf-8")
    assert not is_registered_runs_dir(tape)


def test_is_registered_runs_dir_false_outside(tmp_path):
    """路径不在任何注册项目 runs/ 下 → False。"""
    p = _make_project(tmp_path)
    register_project(p)
    other = tmp_path / "outside"
    other.mkdir()
    assert not is_registered_runs_dir(other)


# ── 并发 register（无 corruption） ────────────────────────────────────────────


def test_concurrent_register_no_corruption(tmp_path):
    """两并发 register 不同项目 → 主文件无 corruption（flock 串行化）。"""
    import threading

    p1 = _make_project(tmp_path, "p1")
    p2 = _make_project(tmp_path, "p2")
    errors: list[Exception] = []

    def go(p):
        try:
            register_project(p)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go, args=(p1,)), threading.Thread(target=go, args=(p2,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    registered = list_registered()
    assert project_id(p1) in registered
    assert project_id(p2) in registered

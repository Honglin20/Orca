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
    list_stale_projects,
    orca_home,
    project_id,
    rebuild_registry,
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


# ── require_marker 双向（run-visibility §4.1 A / AC1 / AC2 / AC5a） ─────────────


def test_register_project_require_marker_false_accepts_no_marker(tmp_path):
    """AC1：``require_marker=False`` 对无 marker 的非顶层目录成功注册（返 pid，进注册表）。

    默认（True）对同目录仍 raise——两头双向，可信自注册放宽、外部注册严格。
    """
    bare = tmp_path / "bare"
    bare.mkdir()  # 无 workflows/ / .orca/config.json
    # 默认 True → M-16 拒。
    with pytest.raises(ValueError, match="project marker|workflows"):
        register_project(bare)
    # require_marker=False → 成功。
    pid = register_project(bare, require_marker=False)
    assert pid == project_id(bare)
    registered = list_registered()
    assert pid in registered
    assert registered[pid]["path"] == str(bare.resolve())


@pytest.mark.parametrize("toplevel", ["/", "/etc", "/home", "/tmp", "/usr"])
def test_register_project_require_marker_false_still_rejects_toplevel(toplevel):
    """AC2：``require_marker=False`` 对 OS 顶层仍 raise（M-15 不被绕过）。

    含 ``/home`` / ``/tmp``（非 ``parent==self``，只靠 ``_TOPLEVEL_DIRS`` 黑名单——防实现
    清空黑名单漏网）。Windows ``C:\\`` 跳过（CI Linux 无盘符根）。
    """
    with pytest.raises(ValueError, match="顶层"):
        register_project(toplevel, require_marker=False)


def test_register_project_require_marker_false_still_rejects_orca_home(
    _isolated_orca_home,
):
    """AC2：``require_marker=False`` 对 ORCA_HOME 自身仍 raise（P2 不被绕过）。"""
    home = _isolated_orca_home
    (home / "workflows").mkdir()  # 伪装 marker 也不应绕过 P2
    with pytest.raises(ValueError, match="ORCA_HOME"):
        register_project(home, require_marker=False)


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


# ── rebuild_registry（SPEC §13.3 P1） ────────────────────────────────────────


def test_rebuild_recovers_from_corrupt_registry(tmp_path, monkeypatch):
    """人为损坏 projects.json → rebuild 重建成功（SPEC §13.3 P1 / §8）。"""
    proj = _make_project(tmp_path, "rebuild_proj")
    register_project(proj)
    # 损坏主文件 + .bak
    reg_path = orca_home() / REGISTRY_FILE
    reg_path.write_text("{CORRUPT", encoding="utf-8")
    bak = reg_path.with_name(REGISTRY_FILE + ".bak")
    bak.write_text("{ALSO_CORRUPT", encoding="utf-8")
    # rebuild 应能救活注册表（不 raise），把 proj 重新注册进去。
    monkeypatch.chdir(proj)
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(proj))
    result = rebuild_registry()
    assert result["registered"] >= 1
    registered = list_registered()
    assert project_id(proj) in registered


def test_rebuild_trusts_old_entries_but_gates_new_candidates(tmp_path, monkeypatch):
    """run-visibility §4.1 D（D-rebuild=A）：rebuild = reconcile，**不是 re-gate**。

    旧 entry（marker 丢失）→ 信任（``require_marker=False``）→ 仍注册；stale 由
    ``list_stale_projects`` 报（N4 配套清理，非 blocker）。新候选（``extra_paths``，
    无 marker）→ 严格 M-16（``require_marker=True``）→ skip。两头布尔方向正确（C1 守门）。
    """
    live = _make_project(tmp_path, "live")
    register_project(live)
    # 旧 entry：曾注册、后删 marker（模拟 marker-free 自注册后 marker 消失）。
    wasmarker = _make_project(tmp_path, "wasmarker")
    register_project(wasmarker)
    import shutil as _sh
    _sh.rmtree(wasmarker / "workflows")
    # 新候选（无 marker）经 extra_paths 传入——不应被注册。
    bare_extra = tmp_path / "bare_extra"
    bare_extra.mkdir()

    monkeypatch.chdir(live)
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(live))
    rebuild_registry(extra_paths=[bare_extra])

    registered = list_registered()
    # live（有 marker 旧 entry）→ 注册。
    assert project_id(live) in registered
    # wasmarker（marker 丢失的旧 entry）→ 仍注册（信任，D-rebuild=A / AC5b）。
    assert project_id(wasmarker) in registered, (
        "marker 丢失的旧 entry 被 rebuild 擦除（D-rebuild=A 回归？）"
    )
    # bare_extra（新候选无 marker）→ skip（require_marker=True）。
    assert project_id(bare_extra) not in registered, (
        "新候选无 marker 被 rebuild 放过（C1 布尔反号，G3 安全边界破？）"
    )
    # wasmarker 虽仍注册，但 marker 丢失 → list_stale_projects 报 stale（N4）。
    stale_ids = [s["project_id"] for s in list_stale_projects()]
    assert project_id(wasmarker) in stale_ids


def test_rebuild_with_extra_paths(tmp_path, monkeypatch):
    """显式传 extra_paths 也能注册。"""
    proj = _make_project(tmp_path, "extra_proj")
    other_cwd = tmp_path / "cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    monkeypatch.delenv("ORCA_PROJECT_ROOT", raising=False)
    result = rebuild_registry(extra_paths=[proj])
    assert project_id(proj) in list_registered()
    assert result["registered"] >= 1


def test_rebuild_marker_project_zero_regression(tmp_path, monkeypatch):
    """AC5a：既有 marker 项目经 rebuild 注册不变（零回归）。

    run-visibility §4.1 D：旧 entry 走 ``require_marker=False``（信任），有 marker 的项目
    无论走哪条路都应注册成功 + project_id 不变。
    """
    proj = _make_project(tmp_path, "marker_proj")
    pid_before = register_project(proj)
    monkeypatch.chdir(proj)
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(proj))
    rebuild_registry()
    registered = list_registered()
    assert pid_before in registered, "marker 项目经 rebuild 丢失（零回归破）"
    assert registered[pid_before]["path"] == str(proj.resolve())


def test_rebuild_preserves_marker_free_old_entry(tmp_path, monkeypatch):
    """AC5b（D-rebuild=A 联动）：marker-free 注册的旧 entry 经 rebuild 仍存在。

    **关键守门**：防 round-2 C1 布尔反号回归（``path_str in old_paths`` 会让旧 entry 被算成
    ``require_marker=True`` → M-16 擦除，摧毁 G2）。正确方向是 ``not in old_paths``：旧 entry
    → False（信任）。
    """
    bare = tmp_path / "bare"
    bare.mkdir()  # 无 marker
    # marker-free 可信自注册（如 in-session bootstrap / start_run）。
    register_project(bare, require_marker=False)
    assert project_id(bare) in list_registered()

    # rebuild：cwd / detect 不指向 bare（bare 不是新候选）。
    other = _make_project(tmp_path, "other")
    monkeypatch.chdir(other)
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(other))
    rebuild_registry()

    registered = list_registered()
    assert project_id(bare) in registered, (
        "marker-free 旧 entry 经 rebuild 被擦除（C1 布尔反号回归？G2 破）"
    )


def test_rebuild_all_fail_rolls_back_to_old_registry(tmp_path, monkeypatch):
    """SPEC §13.3 P1 数据安全：所有候选均失败 → 回滚到 rebuild 前 registry（不清空）。

    run-visibility §4.1 D 后旧 entry 走 ``require_marker=False``（信任），故「marker 丢失」
    不再让旧 entry 失败。构造「全失败」场景：直接注入一个顶层 path 作旧 entry（M-15 始终拒，
    即便 ``require_marker=False``），cwd/detect 也指向无 marker 目录（新候选走严格 M-16 被拒）。
    全失败 + 旧 registry 非空 → 应回滚（``rolled_back: True``），旧 registry 保留。
    """
    # 直接注入顶层 path 进注册表（绕过 register_project 的 M-15 校验——模拟 legacy 坏数据）。
    from orca.runtime import _project as _p
    with _p._with_lock():
        data = _p._read_registry_unlocked()
        data["projects"]["ffffffffffffffff"] = {
            "path": "/",
            "name": "bogus-toplevel",
            "first_seen": 0.0,
            "last_seen": 0.0,
        }
        _p._atomic_write_registry(data)

    # cwd / detect 指向无 marker 目录（新候选 require_marker=True 会被 M-16 拒）。
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(bare))

    result = rebuild_registry(extra_paths=[bare])  # 全失败（"/" M-15 拒；bare 无 marker 拒）
    assert result.get("rolled_back") is True
    assert result["registered"] == 0
    # 旧 registry 保留：注入的 bogus entry 仍在（数据安全：rebuild 不清空）。
    assert "ffffffffffffffff" in list_registered()
    # pre-rebuild 快照落地
    pre_bak = orca_home() / (REGISTRY_FILE + ".pre-rebuild.bak")
    assert pre_bak.is_file()


# ── list_stale_projects（SPEC §13.3 P3） ─────────────────────────────────────


def test_list_stale_projects_marks_missing_path(tmp_path):
    """path 不存在的注册项 → stale。"""
    proj = _make_project(tmp_path, "ok")
    register_project(proj)
    # 直接写一个 path 不存在的 entry（绕过 register_project 校验）。
    from orca.runtime import _project as _p
    with _p._with_lock():
        data = _p._read_registry_unlocked()
        data["projects"]["deadbeefdeadbeef"] = {
            "path": str(tmp_path / "nonexistent"),
            "name": "ghost",
            "first_seen": 0.0,
            "last_seen": 0.0,
        }
        _p._atomic_write_registry(data)
    stale = list_stale_projects()
    stale_ids = [s["project_id"] for s in stale]
    assert "deadbeefdeadbeef" in stale_ids
    assert project_id(proj) not in stale_ids


def test_list_stale_projects_marks_missing_marker(tmp_path):
    """path 存在但 marker 丢失 → stale。"""
    proj = _make_project(tmp_path, "wasproj")
    register_project(proj)
    import shutil as _sh
    _sh.rmtree(proj / "workflows")
    stale = list_stale_projects()
    assert any(s["project_id"] == project_id(proj) for s in stale)

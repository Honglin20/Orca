"""tests/iface/in_session/test_runs_resolution.py —— runs 目录解析鲁棒化测试。

覆盖 ``orca.runtime.resolve_runs_dir`` 两级解析 + in-session CLI 接线
（``_default_tape_path`` / ``_default_rundir`` / ``_write_orca_env``）+
``orca status`` 空 run hint。

核心不变式（Rule 9：测意图）：
  1. ORCA_PROJECT_ROOT 未设 → CWD 相对 ``Path("runs")``（零回归，与今天逐字节一致）。
  2. ORCA_PROJECT_ROOT 设 → ``<root>/runs``（子代理 source env 后无论 CWD 都对）。
  3. 隔离：``resolve_runs_dir`` 绝不调 ``detect_project_root``（不回溯祖先、不跨注册项目搜索）。
  4. ``_write_orca_env`` 产物含 ``export ORCA_PROJECT_ROOT=<abs project_root>``（per-run 常量）。
  5. ``status`` 空 markers → JSON 新增可选 ``hint`` 字段（不破坏既有 ``runs`` 契约）。
  6. 既有 ``bg_runner.default_tape_path`` 不受影响（锁定 ``Path("runs")/<id>.jsonl``）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.in_session import cli as cli_mod
from orca.iface.in_session.cli import app, _default_rundir, _default_tape_path, _write_orca_env
from orca.iface.cli.bg_runner import default_tape_path
from orca.runtime import resolve_runs_dir, RUNS_DIRNAME


# ── 公共 fixture ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _scrub_orca_project_root(monkeypatch: pytest.MonkeyPatch):
    """每测前清 ``ORCA_PROJECT_ROOT``，避免环境泄漏污染断言（本模块全用例都敏感于此 env）。"""
    monkeypatch.delenv("ORCA_PROJECT_ROOT", raising=False)


# ── resolve_runs_dir 两级解析 ───────────────────────────────────────────────


def test_resolve_runs_dir_cwd_relative_when_env_unset():
    """不变式 1：``ORCA_PROJECT_ROOT`` 未设 → ``Path("runs")``（CWD 相对，零回归）。"""
    assert resolve_runs_dir() == Path(RUNS_DIRNAME)


def test_resolve_runs_dir_env_scoped_when_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """不变式 2：``ORCA_PROJECT_ROOT=/tmp/proj`` → ``Path("/tmp/proj/runs")``。

    子代理 source env 后无论 CWD 落哪个子目录，都回到此根 → ``runs/`` 解析正确。
    """
    proj = tmp_path / "proj"
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(proj))
    assert resolve_runs_dir() == proj / RUNS_DIRNAME


def test_resolve_runs_dir_empty_env_falls_back_to_cwd(monkeypatch: pytest.MonkeyPatch):
    """空串 env（falsy）→ CWD 相对回落（``if env_root:`` 守门，空串不当真值）。"""
    monkeypatch.setenv("ORCA_PROJECT_ROOT", "")
    assert resolve_runs_dir() == Path(RUNS_DIRNAME)


def test_resolve_runs_dir_bad_env_fails_loud(monkeypatch: pytest.MonkeyPatch):
    """env 值坏（``_resolve_strict`` 失败）→ raise ValueError（铁律 4：不静默退化）。

    ``_resolve_strict`` 对不可 resolve 的路径抛 ``ValueError``；``resolve_runs_dir`` 捕获后
    用更清晰的消息重抛——子代理拿到坏 env 不应悄悄回到 CWD 相对（会写到错误项目的 ``runs/``）。

    实现注：无法用真实坏路径触发（Linux ``Path.resolve(strict=False)`` 对不存在路径不抛），
    故 monkeypatch ``_resolve_strict`` 抛 ``ValueError``，直接验证 ``resolve_runs_dir`` 的
    try/except 包装行为（intent：测 fail-loud 语义，非测 OS 边界）。
    """
    from orca.runtime import _project as proj_mod

    def _boom(_p):
        raise ValueError("模拟 resolve 失败")

    monkeypatch.setenv("ORCA_PROJECT_ROOT", "/some/path")
    monkeypatch.setattr(proj_mod, "_resolve_strict", _boom)
    with pytest.raises(ValueError, match="ORCA_PROJECT_ROOT 解析失败"):
        resolve_runs_dir()


def test_resolve_runs_dir_does_not_call_detect_project_root(monkeypatch: pytest.MonkeyPatch):
    """不变式 3（隔离）：``resolve_runs_dir`` 绝不调 ``detect_project_root``。

    刻意不回溯祖先——上一轮 visibility bug 根因就是 ``detect_project_root`` 跳到 cwd 祖先
    与 tape 落点脱节。本测试 monkeypatch ``detect_project_root`` 为「被调即爆炸」，
    验证两个分支（env 钉死 / CWD 回落）都不触发它。
    """
    from orca.runtime import _project as proj_mod

    def _explode(*args, **kwargs):
        raise AssertionError(
            "resolve_runs_dir 不应调 detect_project_root（不回溯祖先不变式）"
        )

    monkeypatch.setattr(proj_mod, "detect_project_root", _explode)

    # CWD 回落分支不应触发。
    assert resolve_runs_dir() == Path(RUNS_DIRNAME)

    # env 钉死分支也不应触发。
    monkeypatch.setenv("ORCA_PROJECT_ROOT", "/another/proj")
    assert resolve_runs_dir() == Path("/another/proj/runs")


# ── _default_tape_path / _default_rundir 接线 ──────────────────────────────


def test_default_tape_path_cwd_relative_when_env_unset():
    """不变式 1 接线：未设 env → ``Path("runs")/<id>.jsonl``（与今天逐字节一致）。"""
    assert _default_tape_path("x") == Path("runs") / "x.jsonl"


def test_default_tape_path_env_scoped_when_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """不变式 2 接线：设 env → ``<env_root>/runs/<id>.jsonl``。"""
    proj = tmp_path / "proj"
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(proj))
    assert _default_tape_path("r-1") == proj / "runs" / "r-1.jsonl"


def test_default_rundir_follows_tape_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """``_default_rundir`` 派生自 ``_default_tape_path`` → 自动跟随 env 钉死。

    覆盖两分支：env 未设 → ``Path("runs")``；env 设 → ``<root>/runs``。
    """
    # env 未设：CWD 相对。
    assert _default_rundir() == Path("runs")

    # env 设：跟随到项目根 runs。
    proj = tmp_path / "proj"
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(proj))
    assert _default_rundir() == proj / "runs"


def test_bg_runner_default_tape_path_unchanged():
    """不变式 6：``bg_runner.default_tape_path`` **不**随 in-session 改动而变（锁定测试钉死）。

    ``bg_runner`` 服务 ``tars run --background`` daemon 路径（子进程继承正确 CWD），
    本次改动**没碰**它——锁定 ``Path("runs")/<id>.jsonl`` 既有契约，回归零影响。
    """
    assert default_tape_path("r1") == Path("runs") / "r1.jsonl"


# ── _write_orca_env 含 ORCA_PROJECT_ROOT ───────────────────────────────────


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir 到 tmp_path：``_write_orca_env`` 内 ``resolve_runs_dir().resolve()`` 走 CWD 相对时
    需确定性落点。env 文件写到 tmp_path 下，测试结束 pytest 自动清。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_write_orca_env_contains_project_root_cwd_relative(
    cwd_tmp: Path, tmp_path: Path
):
    """不变式 4（CWD 回落分支）：env 文件含 ``export ORCA_PROJECT_ROOT=<cwd 绝对路径>``。

    未设 env → ``resolve_runs_dir() = Path("runs")`` → ``.resolve().parent = cwd``。
    字面值是绝对路径（``resolve()`` 后），子代理 source 后 ``$ORCA_PROJECT_ROOT`` 钉死项目根。
    """
    env_path = tmp_path / "runs" / "r-aaa" / "orca_env.sh"
    _write_orca_env(
        env_path,
        run_id="r-aaa",
        node="n1",
        session_id="sess-1",
        sock_path=Path("/tmp/sock"),
        resources_root=None,
        artifacts_dir=tmp_path / "runs" / "r-aaa" / "artifacts",
    )
    assert env_path.is_file(), "env 文件未写到磁盘"
    content = env_path.read_text(encoding="utf-8")

    expected_root = str(cwd_tmp.resolve())
    m = re.search(r"^export ORCA_PROJECT_ROOT=(.+)$", content, re.MULTILINE)
    assert m, f"env 文件缺 ORCA_PROJECT_ROOT 行：\n{content}"
    literal_value = m.group(1).strip().strip("'\"")
    assert literal_value == expected_root, (
        f"ORCA_PROJECT_ROOT 字面值应为 cwd resolve 路径 {expected_root!r}，实际 {literal_value!r}"
    )
    assert Path(literal_value).is_absolute(), (
        f"ORCA_PROJECT_ROOT 必须绝对路径（resolve 后），实际 {literal_value!r}"
    )


def test_write_orca_env_contains_project_root_env_scoped(
    cwd_tmp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """不变式 4（env 钉死分支）：设 env → env 文件写 ``<env_root>``（幂等，与 CWD 无关）。

    主 session 已 source 过 env（``ORCA_PROJECT_ROOT`` 钉死）→ next 重写 env 文件时
    ``resolve_runs_dir`` 走 env 分支 → ``.resolve().parent`` 仍是同一项目根（幂等）。
    """
    proj = tmp_path / "anchored-proj"
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(proj))

    env_path = tmp_path / "runs" / "r-bbb" / "orca_env.sh"
    _write_orca_env(
        env_path,
        run_id="r-bbb",
        node="n1",
        session_id="sess-2",
        sock_path=Path("/tmp/sock"),
        resources_root=None,
        artifacts_dir=tmp_path / "runs" / "r-bbb" / "artifacts",
    )
    content = env_path.read_text(encoding="utf-8")
    m = re.search(r"^export ORCA_PROJECT_ROOT=(.+)$", content, re.MULTILINE)
    assert m, f"env 文件缺 ORCA_PROJECT_ROOT 行：\n{content}"
    literal_value = m.group(1).strip().strip("'\"")
    assert literal_value == str(proj.resolve()), (
        f"env 钉死分支应写 env_root={proj.resolve()!r}，实际 {literal_value!r}"
    )


def test_write_orca_env_project_root_idempotent_across_rewrites(
    cwd_tmp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """幂等性：bootstrap（CWD=root）写一次 + next（已 source env）重写 → ``ORCA_PROJECT_ROOT`` 一致。

    防漂移：bootstrap 时 CWD 相对 → resolve 得 ``<cwd>``；next 时 env 已设 → resolve 得同一路径。
    两次写的字面值必须相同（否则子代理重新 source 会换项目根 → run 飘）。
    """
    # bootstrap：CWD 相对（cwd_tmp = 项目根）。
    env_path = tmp_path / "runs" / "r-ccc" / "orca_env.sh"
    _write_orca_env(
        env_path,
        run_id="r-ccc", node="n1", session_id="s1",
        sock_path=Path("/tmp/sock"), resources_root=None,
        artifacts_dir=tmp_path / "runs" / "r-ccc" / "artifacts",
    )
    first = env_path.read_text(encoding="utf-8")

    # next：模拟子代理 source 后 ``ORCA_PROJECT_ROOT`` 已设。
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(cwd_tmp.resolve()))
    _write_orca_env(
        env_path,
        run_id="r-ccc", node="n2", session_id="s2",
        sock_path=Path("/tmp/sock"), resources_root=None,
        artifacts_dir=tmp_path / "runs" / "r-ccc" / "artifacts",
    )
    second = env_path.read_text(encoding="utf-8")

    def _extract_root(text: str) -> str:
        m = re.search(r"^export ORCA_PROJECT_ROOT=(.+)$", text, re.MULTILINE)
        assert m, f"缺 ORCA_PROJECT_ROOT：\n{text}"
        return m.group(1).strip().strip("'\"")

    assert _extract_root(first) == _extract_root(second) == str(cwd_tmp.resolve()), (
        "两次写的 ORCA_PROJECT_ROOT 不一致（非幂等 → run 跨 next 会飘项目根）"
    )


# ── status 空 run hint ─────────────────────────────────────────────────────


@pytest.fixture
def isolated_orca_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """独立 ``ORCA_HOME`` 隔离注册表（status hint 读注册表，防污染真实注册表 / 被真实项目干扰）。"""
    home = tmp_path / ".orca_home"
    home.mkdir(parents=True)
    monkeypatch.setenv("ORCA_HOME", str(home))
    return home


def _stub_daemon_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    """stub daemon spawn（status 不触发 spawn，但 CliRunner invoke 全程隔离 detached 子进程）。"""
    monkeypatch.setattr(cli_mod, "_spawn_chart_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_spawn_sidechain_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_wait_for_sock", lambda *a, **kw: True)
    monkeypatch.setattr(cli_mod, "_spawn_open_web", lambda *a, **kw: None)


def test_status_empty_runs_hint_when_registry_nonempty(
    cwd_tmp: Path, isolated_orca_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """不变式 5a：空 markers + registry 非空 → JSON ``hint`` 提示注册项目 path + 三种修复。

    模拟子代理 CWD 迷路：注册了一个项目（path=tmp/proj），但子代理 CWD 在 tmp/（无 ``runs/``）→
    status 扫不到 marker → hint 引导用户回项目根 / source env / 设 env。
    """
    _stub_daemon_subprocesses(monkeypatch)
    from orca.runtime import register_project

    proj = cwd_tmp / "registered-proj"
    (proj / "workflows").mkdir(parents=True)
    register_project(proj)  # 注册到 isolated ORCA_HOME

    runner = CliRunner()
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])

    assert reply["runs"] == [], "空扫描应返空 runs 列表"
    hint = reply.get("hint")
    assert hint is not None, "空 markers + registry 非空应提供 hint"
    assert str(proj.resolve()) in hint, f"hint 应含注册项目 path，实际：{hint}"
    assert "ORCA_PROJECT_ROOT" in hint, f"hint 应提 ORCA_PROJECT_ROOT 修复，实际：{hint}"
    assert "orca_env.sh" in hint, f"hint 应提 source env 修复，实际：{hint}"


def test_status_empty_runs_hint_when_registry_empty(
    cwd_tmp: Path, isolated_orca_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """不变式 5b：空 markers + registry 空 → JSON ``hint`` 退化到通用文案。

    全新环境（无注册项目）→ hint 不提具体 path，只给通用修复（source env / 设 env）。
    """
    _stub_daemon_subprocesses(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])

    assert reply["runs"] == []
    hint = reply.get("hint")
    assert hint is not None, "空 markers 应提供 hint（子目录迷路场景常见）"
    assert "orca_env.sh" in hint or "ORCA_PROJECT_ROOT" in hint, (
        f"registry 空 hint 仍应提 source env / 设 env 修复，实际：{hint}"
    )


def test_status_no_hint_when_markers_present(
    cwd_tmp: Path, isolated_orca_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """不变式 5c：markers 非空（有活跃 run）→ JSON **不**含 ``hint``（既有契约不破坏）。

    用户在正确项目根、有活跃 run → 无需 hint。验证 hint 只在空 markers 时出现。
    """
    _stub_daemon_subprocesses(monkeypatch)
    # 造一个活跃 marker（schema 见 ``ActivationMarker``：3 字段 run_id/model/no_output_count）。
    from orca.iface.in_session.marker import write_marker, ActivationMarker, marker_path

    runs_dir = cwd_tmp / "runs"
    runs_dir.mkdir(parents=True)
    run_id = "orca-test-aaa"
    write_marker(
        marker_path(runs_dir, run_id),
        ActivationMarker(run_id=run_id, model="m", no_output_count=0),
    )
    # 也造对应 tape（status 扫 marker 后会查 tape 存在性）。
    tape_path = runs_dir / f"{run_id}.jsonl"
    tape_path.write_text(
        json.dumps({
            "seq": 1, "type": "workflow_started", "timestamp": 1.0,
            "node": None, "session_id": None,
            "data": {"workflow_name": "w"},
        }) + "\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])

    assert len(reply["runs"]) >= 1, f"应扫到活跃 run，实际 reply={reply}"
    assert "hint" not in reply, (
        f"markers 非空不应出现 hint（既有 JSON 契约），实际 reply={reply}"
    )


def test_status_text_output_shows_hint_for_empty_runs(
    cwd_tmp: Path, isolated_orca_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """不变式 5d（文本输出）：空 markers → ``orca status`` 文本输出也含 hint（与 JSON 一致）。

    主 session 不带 ``--json`` 直接看 stdout 时也应有提示（UX 一致）。
    """
    _stub_daemon_subprocesses(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "(无活跃 run)" in result.output
    assert "ORCA_PROJECT_ROOT" in result.output or "orca_env.sh" in result.output, (
        f"文本输出应含修复提示，实际：\n{result.output}"
    )


def test_status_hint_degrades_when_registry_corrupt(
    cwd_tmp: Path, isolated_orca_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """``_build_empty_runs_hint`` fail-soft：registry 坏（``RegistryCorruptError``）→ 退化到通用文案。

    registry 坏由 ``doctor`` 另行检测；status 只保证不崩 + 给出子目录迷路修复指引。
    monkeypatch ``orca.runtime.list_registered``（``_build_empty_runs_hint`` 内 lazy import 的名字）
    抛 ``RegistryCorruptError`` → hint 走 ``except`` 分支 → 退化文案（不含具体 path）。
    """
    _stub_daemon_subprocesses(monkeypatch)
    from orca.runtime import RegistryCorruptError
    import orca.runtime as runtime_mod

    def _boom():
        raise RegistryCorruptError("模拟注册表损坏")

    # patch re-exported name（``_build_empty_runs_hint`` 内 ``from orca.runtime import list_registered``
    # 在调用时查 ``orca.runtime`` 模块对象的属性 → patch 这层才生效）。
    monkeypatch.setattr(runtime_mod, "list_registered", _boom)

    runner = CliRunner()
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, (
        f"registry 坏时 status 不应崩（fail-soft），实际 exit={result.exit_code}：\n{result.output}"
    )
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["runs"] == []
    hint = reply.get("hint")
    assert hint is not None, "registry 坏也应给出 hint（子目录迷路修复指引仍有效）"
    # 退化文案不含具体 path（list_registered 坏了拿不到），但仍含修复指引。
    assert "orca_env.sh" in hint or "ORCA_PROJECT_ROOT" in hint, (
        f"退化 hint 仍应含 source env / 设 env 修复，实际：{hint}"
    )

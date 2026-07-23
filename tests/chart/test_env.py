"""tests/chart/test_env.py —— ``load_run_env_from_artifacts`` 单测（SPEC 2026-07-23 §3.1/§6）。

覆盖 SPEC §6 验收标准：

- **AC1a（ledger anchor）**：tmp run_dir + ``orca_env.sh``（含 4 键）+ ``ledger.jsonl``，
  清空 ``ORCA_*`` env → 调用后 4 键被补进 ``os.environ``。
- **AC1b（champions anchor）**：同 AC1a 但 anchor 用 ``champions.jsonl``（step6 compare 模式用的），
  断言行为一致（防 step6 锚点失效）。
- **AC2（幂等）**：``os.environ`` 已含 ``ORCA_CHART_SOCK`` 时 no-op，不改 env。
- **AC3（找不到 fallback）**：anchor 指向无 ``orca_env.sh`` 的 tmp 目录（或 ``orca_env.sh`` 存在
  但内容**不含** ``^export ORCA_CHART_SOCK=`` 行）→ 返 ``{}``，``os.environ`` 不变。

依赖：仅 stdlib + ``orca.chart._env``（light-touch，零 Orca runtime 依赖）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orca.chart._env import load_run_env_from_artifacts


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_orca_env(monkeypatch: pytest.MonkeyPatch):
    """清空所有 ORCA_* env，返清空前的快照（teardown 时还原）。

    必须清空：测试可能在已有 ``ORCA_*`` 的 dev shell 里跑（IDE / orca-run 内），不清空会让
    幂等短路假阳性。``monkeypatch.delenv`` 自动 teardown 还原。
    """
    for k in list(os.environ):
        if k.startswith("ORCA_"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def run_dir_with_env(tmp_path: Path) -> Path:
    """造一个 tmp run_dir，含 ``orca_env.sh``（4 键 + artifacts + kb）+ ``ledger.jsonl``。

    结构（与 ``cli._write_orca_env`` 落盘形态一致，``shlex.quote`` 包成单引号）::

        <tmp>/run-abc/
            orca_env.sh
            ledger.jsonl
            champions.jsonl
    """
    rd = tmp_path / "run-abc"
    rd.mkdir()
    env_file = rd / "orca_env.sh"
    # 模拟 cli._write_orca_env 的输出（shlex.quote 把值包成单引号）。
    env_file.write_text(
        "export ORCA_RUN_ID='run-abc-2026'\n"
        "export ORCA_NODE='curator'\n"
        "export ORCA_SESSION_ID='sess-deadbeef'\n"
        "export ORCA_CHART_SOCK='/tmp/orca-deadbeef.sock'\n"
        "export ORCA_ARTIFACTS_DIR='/tmp/run-abc/artifacts/'\n"
        "export ORCA_KB_DIR='/tmp/kb'\n",
        encoding="utf-8",
    )
    (rd / "ledger.jsonl").write_text('{"id":"c1"}\n', encoding="utf-8")
    (rd / "champions.jsonl").write_text('{"id":"baseline"}\n', encoding="utf-8")
    return rd


# ── AC1a：ledger anchor ────────────────────────────────────────────────────


def test_ac1a_loads_four_keys_from_ledger_anchor(run_dir_with_env: Path, clean_orca_env):
    """AC1a：anchor=ledger.jsonl，从其父目录的 ``orca_env.sh`` 补 4 个身份键。"""
    ledger = run_dir_with_env / "ledger.jsonl"
    injected = load_run_env_from_artifacts(ledger)

    # SPEC §3.1：仅补 4 键（ORCA_RUN_ID/NODE/SESSION_ID/CHART_SOCK）。
    assert set(injected.keys()) == {
        "ORCA_RUN_ID", "ORCA_NODE", "ORCA_SESSION_ID", "ORCA_CHART_SOCK"
    }
    # 写进 os.environ
    assert os.environ["ORCA_RUN_ID"] == "run-abc-2026"
    assert os.environ["ORCA_NODE"] == "curator"
    assert os.environ["ORCA_SESSION_ID"] == "sess-deadbeef"
    assert os.environ["ORCA_CHART_SOCK"] == "/tmp/orca-deadbeef.sock"


def test_ac1a_does_not_inject_non_identity_keys(run_dir_with_env: Path, clean_orca_env):
    """SPEC §3.1：「只补 4 键」——``ORCA_ARTIFACTS_DIR`` / ``ORCA_KB_DIR`` 不在本函数职责。

    意图：守住单一职责——本函数只补 render_chart 强依赖的身份键；其他 env（产物目录 / KB）
    由 setup 节点 prompt 已处理其缺失回退，本函数不越界。
    """
    load_run_env_from_artifacts(run_dir_with_env / "ledger.jsonl")
    assert "ORCA_ARTIFACTS_DIR" not in os.environ
    assert "ORCA_KB_DIR" not in os.environ


# ── AC1b：champions anchor（step6 compare 模式用的）────────────────────────


def test_ac1b_loads_four_keys_from_champions_anchor(run_dir_with_env: Path, clean_orca_env):
    """AC1b：anchor=champions.jsonl（step6 ``--mode compare`` 用的路径），行为同 AC1a。

    意图：防 step6 锚点失效（SPEC §6 AC1b：防 compare 模式自加载链断）。
    """
    champions = run_dir_with_env / "champions.jsonl"
    injected = load_run_env_from_artifacts(champions)
    assert "ORCA_CHART_SOCK" in injected
    assert os.environ["ORCA_CHART_SOCK"] == "/tmp/orca-deadbeef.sock"
    assert os.environ["ORCA_NODE"] == "curator"


# ── AC2：幂等 no-op ────────────────────────────────────────────────────────


def test_ac2_noop_when_sock_already_set(run_dir_with_env: Path, monkeypatch: pytest.MonkeyPatch):
    """AC2：``os.environ`` 已含 ``ORCA_CHART_SOCK`` → no-op，不改 env。

    意图：真 orca-run / ClaudeExecutor spawn 路径下 env 已注 → 自加载短路，零影响。
    SPEC §3.1「no-op 短路假设」：4 键同源同注，half-injection 不解（KISS）。
    """
    # 预设一个 SOCK（模拟真 orca-run spawn）
    monkeypatch.setenv("ORCA_CHART_SOCK", "/tmp/preset-by-orca-run.sock")
    monkeypatch.setenv("ORCA_RUN_ID", "preset-run")

    injected = load_run_env_from_artifacts(run_dir_with_env / "ledger.jsonl")
    assert injected == {}
    # env 不被 orca_env.sh 内容覆盖
    assert os.environ["ORCA_CHART_SOCK"] == "/tmp/preset-by-orca-run.sock"
    assert os.environ["ORCA_RUN_ID"] == "preset-run"


# ── AC3：找不到 fallback ───────────────────────────────────────────────────


def test_ac3_returns_empty_when_no_env_file(tmp_path: Path, clean_orca_env):
    """AC3：anchor 所在目录树无 ``orca_env.sh`` → 返 ``{}``。

    场景：headless / 非 orca-run / 自定义 output_dir。
    """
    # tmp_path 下没有任何 orca_env.sh（pytest tmp_path 在 /tmp/.../pytest-XX/ 下，
    # 父级 /tmp 也不应有 orca_env.sh；保险起见用独立子目录）
    rd = tmp_path / "noenv-run"
    rd.mkdir()
    ledger = rd / "ledger.jsonl"
    ledger.write_text("{}", encoding="utf-8")
    injected = load_run_env_from_artifacts(ledger)
    assert injected == {}


def test_ac3_returns_empty_when_env_file_lacks_sock_marker(tmp_path: Path, clean_orca_env):
    """AC3 关键边界：``orca_env.sh`` 存在但**内容不含** ``^export ORCA_CHART_SOCK=`` 行 → 返 ``{}``。

    SPEC §3.1「单一标志 + 内容校验」：防用户项目根碰巧有同名 ``orca_env.sh`` 误匹配。
    场景：上游写 env 文件中途失败 / 旧版 cli 写出无 SOCK 的 env / 用户自定义同名文件。
    """
    rd = tmp_path / "broken-run"
    rd.mkdir()
    # 故意只写 RUN_ID / NODE，不写 SOCK
    (rd / "orca_env.sh").write_text(
        "export ORCA_RUN_ID='x'\nexport ORCA_NODE='y'\n", encoding="utf-8"
    )
    ledger = rd / "ledger.jsonl"
    ledger.write_text("{}", encoding="utf-8")
    injected = load_run_env_from_artifacts(ledger)
    assert injected == {}
    assert "ORCA_RUN_ID" not in os.environ  # 不该被部分注入


def test_ac3_ignores_env_file_in_unrelated_ancestor(tmp_path: Path, clean_orca_env):
    """AC3：祖先目录有同名 ``orca_env.sh`` 但内容无 SOCK 行 → 不误匹配，继续向上找。

    意图：钉死「内容校验」契约——同名文件存在 ≠ 匹配，必须含 SOCK 标志行。
    场景：用户项目根碰巧有同名 ``orca_env.sh``（如旧的 spike 残留）。
    """
    # tmp_path 下造一个无 SOCK 的同名文件（模拟用户项目根残留）
    (tmp_path / "orca_env.sh").write_text(
        "# user custom file, no SOCK\nexport FOO='bar'\n", encoding="utf-8"
    )
    rd = tmp_path / "real-run"
    rd.mkdir()
    # 真正的 run env 在子目录，但内容也无 SOCK → 整体找不到
    (rd / "orca_env.sh").write_text("export ORCA_RUN_ID='r'\n", encoding="utf-8")
    ledger = rd / "ledger.jsonl"
    ledger.write_text("{}", encoding="utf-8")
    injected = load_run_env_from_artifacts(ledger)
    assert injected == {}


# ── 边界：anchor 是目录而非文件 ──────────────────────────────────────────


def test_accepts_directory_anchor(run_dir_with_env: Path, clean_orca_env):
    """anchor 是目录（不是文件）时也能正确向上找。

    意图：调用方可能传 ``Path(<run_dir>/artifacts/)`` 这种目录路径，本函数应能从该目录起向上搜。
    """
    # 传 run_dir 本身（目录）作为 anchor
    injected = load_run_env_from_artifacts(run_dir_with_env)
    assert "ORCA_CHART_SOCK" in injected


def test_load_is_idempotent_on_repeated_calls(run_dir_with_env: Path, clean_orca_env):
    """二次调用：第一次注入后 SOCK 已在 env → 第二次幂等 no-op。

    意图：防同一进程内多次自加载产生副作用（如 viz_struct 在异常重试路径下重复调）。
    """
    ledger = run_dir_with_env / "ledger.jsonl"
    first = load_run_env_from_artifacts(ledger)
    assert "ORCA_CHART_SOCK" in first
    # 第二次：env 已含 SOCK → no-op
    second = load_run_env_from_artifacts(ledger)
    assert second == {}


def test_does_not_overwrite_existing_partial_env(run_dir_with_env: Path, monkeypatch: pytest.MonkeyPatch):
    """SPEC §3.1「已存在的 env 不覆盖」——只补缺失键，已在 env 的键保持原值。

    意图：显式 spawn env 优先；防本进程外层已注入更新值（如 dev shell 调试）被文件值覆盖。
    """
    # 预设一个不同的 NODE
    monkeypatch.setenv("ORCA_NODE", "preset-by-shell")
    # SOCK 不设 → 不触发 no-op 短路，走文件加载
    monkeypatch.delenv("ORCA_CHART_SOCK", raising=False)
    monkeypatch.delenv("ORCA_RUN_ID", raising=False)
    monkeypatch.delenv("ORCA_SESSION_ID", raising=False)

    injected = load_run_env_from_artifacts(run_dir_with_env / "ledger.jsonl")
    # RUN_ID / SESSION_ID / SOCK 被补，NODE 不被覆盖（不在 injected 里）
    assert "ORCA_RUN_ID" in injected
    assert "ORCA_CHART_SOCK" in injected
    assert "ORCA_NODE" not in injected
    # env 中 NODE 保持原值
    assert os.environ["ORCA_NODE"] == "preset-by-shell"

"""tests/workflows/test_viz_struct_robustness.py —— viz_struct 鲁棒出图单测（SPEC 2026-07-23 §3.2/§6）。

覆盖 SPEC §6 验收标准：

- **AC4（reason 分类）**：mock render_chart 抛 socket 类（FileNotFoundError / ConnectionRefusedError
  被 ``_render.py`` 转写为 ``RuntimeError("无法连接 …")``）→ ``socket_unreachable``；
  ack 类（``ack 超时`` / ``拒收``）→ ``ack_failed``；其余 → ``generic:<Type>:<msg>``。
  数据不足（ledger < 2 行 / 无有效点）→ ``data_insufficient``。
- **AC5a（脚本 stdout 字段断言）**：各失败路径（env_missing / socket_unreachable / data_insufficient
  / ``_main`` 兜底 generic）下 stdout JSON 含 ``viz_env_status`` + ``charts[name].reason`` 字段
  且分类正确；``--mode compare`` 路径下 ``charts.compare_bar`` 同款断言。
- **额外（_main 兜底 B1 命门）**：``_main`` 异常路径 stdout **必有合法 JSON**
  （``viz_env_status="generic"`` + 所有图 ``reason="generic:<Type>:<msg>"``），exit 2。

不依赖 ``ts_quant`` / ``torch_npu`` / ``orca.run`` —— 仅 mock ``orca.chart.render_chart``。
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STRUCT_SCRIPTS = REPO / "workflows" / "agents" / "_struct_scripts"


# ── helper：mock orca.chart + orca.chart._env，force-reload viz_struct ─────


class _VizHarness:
    """统一 mock orca.chart / orca.chart._env 并 reload viz_struct。

    SPEC §3.2：viz_struct 的两个外部依赖是 ``orca.chart.render_chart``（推图）和
    ``orca.chart._env.load_run_env_from_artifacts``（自加载）。本 harness 让测试可控地：
    - 把 ``_orca_render_chart`` 替换成捕获 / 抛异常的 mock；
    - 把 ``_load_run_env`` 保留真函数（用真 ``orca.chart._env``，测试在 tmp 造 ``orca_env.sh``）；
    - 或整体隐藏（``_orca_render_chart=None`` → import_failed 路径）。
    """

    def __init__(self, *, render_impl=None, hide_orca_chart=False):
        self._saved_orca = sys.modules.get("orca")
        self._saved_orca_chart = sys.modules.get("orca.chart")
        self._saved_orca_chart_env = sys.modules.get("orca.chart._env")
        self._saved_viz = sys.modules.get("viz_struct")
        self._saved_render = None
        self._saved_load_env = None
        self.calls: list[dict] = []

        if hide_orca_chart:
            # 直接置 ``_orca_render_chart`` / ``_load_run_env`` 为 None 模拟 import 失败
            # （pip 装好的 orca.chart 无法靠清 sys.modules 阻断；改模块属性更可靠）。
            sys.path.insert(0, str(STRUCT_SCRIPTS))
            for m in [m for m in sys.modules if m == "viz_struct"]:
                del sys.modules[m]
            import viz_struct
            self.viz_struct = importlib.reload(viz_struct)
            self._saved_render = self.viz_struct._orca_render_chart
            self._saved_load_env = self.viz_struct._load_run_env
            self.viz_struct._orca_render_chart = None
            self.viz_struct._load_run_env = None
            return

        mock_chart = types.ModuleType("orca.chart")
        if render_impl is None:
            def _default(**kw):
                self.calls.append(kw)
                return len(self.calls)
            mock_chart.render_chart = _default
        else:
            mock_chart.render_chart = render_impl
        # 保留真实的 _env（stdlib-only，无 Orca runtime 依赖，可安全用）
        # 若 orca.chart._env 未加载则强制 import
        try:
            import orca.chart._env as real_env  # noqa: F401
        except ImportError:
            real_env = None
        sys.modules["orca"] = types.ModuleType("orca")
        sys.modules["orca.chart"] = mock_chart
        if real_env is not None:
            sys.modules["orca.chart._env"] = real_env
        sys.path.insert(0, str(STRUCT_SCRIPTS))
        # 清掉旧 viz_struct 缓存以让 lazy import 重新生效
        for m in [m for m in sys.modules if m == "viz_struct"]:
            del sys.modules[m]
        import viz_struct
        self.viz_struct = importlib.reload(viz_struct)

    def teardown(self):
        # 还原 hide_orca_chart 路径下的模块属性
        if self._saved_render is not None and "viz_struct" in sys.modules:
            sys.modules["viz_struct"]._orca_render_chart = self._saved_render
        if self._saved_load_env is not None and "viz_struct" in sys.modules:
            sys.modules["viz_struct"]._load_run_env = self._saved_load_env
        for m in [m for m in sys.modules if m in ("viz_struct",)]:
            del sys.modules[m]
        for key, saved in (
            ("orca", self._saved_orca),
            ("orca.chart", self._saved_orca_chart),
            ("orca.chart._env", self._saved_orca_chart_env),
        ):
            if saved is not None:
                sys.modules[key] = saved
            elif key in sys.modules:
                del sys.modules[key]


@pytest.fixture
def viz_harness():
    """默认 harness：render_chart mock 捕获调用。"""
    h = _VizHarness()
    yield h
    h.teardown()


@pytest.fixture
def clean_orca_env(monkeypatch: pytest.MonkeyPatch):
    """清空 ORCA_* env（防 dev shell 残留干扰）。"""
    for k in list(os.environ):
        if k.startswith("ORCA_"):
            monkeypatch.delenv(k, raising=False)


# ── 数据 fixture ────────────────────────────────────────────────────────────


def _write_run(tmp_path: Path, *, n_rows: int = 3) -> tuple[Path, Path]:
    """造 ledger.jsonl + champions.jsonl（n_rows 行有效行）。"""
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"id": f"c{i}", "parent": "baseline", "path": "p", "round": i,
         "status": "SUCCESS", "tag": "structural",
         "latency_ms": 10.0 - i, "accuracy": 0.9 + i * 0.005,
         "met_accuracy": True, "snapshot": "/x", "onnx": "/x",
         "diff_summary": "d", "hypothesis": "h"}
        for i in range(1, n_rows + 1)
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    champions = tmp_path / "champions.jsonl"
    champions.write_text(
        json.dumps({"round": 0, "id": "baseline", "latency_ms": 12.0,
                    "accuracy": 0.88, "snapshot": "/x"}) + "\n",
        encoding="utf-8",
    )
    return ledger, champions


def _write_env_file(run_dir: Path, sock: str = "/tmp/orca-test.sock"):
    """在 run_dir 下造 orca_env.sh（4 键 + artifacts）。"""
    (run_dir / "orca_env.sh").write_text(
        f"export ORCA_RUN_ID='run-x'\n"
        f"export ORCA_NODE='curator'\n"
        f"export ORCA_SESSION_ID='sess-x'\n"
        f"export ORCA_CHART_SOCK='{sock}'\n",
        encoding="utf-8",
    )


# ── AC4 + AC5a：reason 分类 ────────────────────────────────────────────────


def test_ac5a_happy_path_emits_viz_env_status_and_empty_reasons(tmp_path, viz_harness, clean_orca_env, monkeypatch):
    """AC5a happy path：env_ok + 三图全推成功 → ``viz_env_status`` 字段 + 各图 reason==''。"""
    ledger, champions = _write_run(tmp_path)
    _write_env_file(tmp_path)
    # preset env (so _resolve_env_status returns "ok")。用 monkeypatch.setenv 而非
    # 直接 os.environ[]=，让 teardown 自动还原（防跨测试污染）。
    monkeypatch.setenv("ORCA_CHART_SOCK", "/tmp/preset.sock")
    monkeypatch.setenv("ORCA_RUN_ID", "r")
    monkeypatch.setenv("ORCA_NODE", "n")
    monkeypatch.setenv("ORCA_SESSION_ID", "s")

    result = viz_harness.viz_struct.render_all(
        ledger_path=str(ledger), champions_path=str(champions),
        baseline_latency_ms=12.0, baseline_accuracy=0.88,
        target_latency_ms=10.0, accuracy_target=0.87,
    )
    assert result["viz_env_status"] == "ok"
    assert set(result["charts"].keys()) == {"champion_trace", "champion_accuracy_trace", "pareto", "candidate_table"}
    for name, info in result["charts"].items():
        assert info["pushed"] is True, f"{name} not pushed"
        assert info["reason"] == "", f"{name} reason should be empty"
    # 四张图都真调了 render_chart（P7 三张 + 2026-07-24 P2-1 accuracy champion trace）
    assert len(viz_harness.calls) == 4


def test_ac5a_env_missing_when_no_env_file(tmp_path, viz_harness, clean_orca_env):
    """AC5a env_missing：env 缺 + 无 ``orca_env.sh`` → ``viz_env_status=env_missing``，
    每图 ``reason=env_missing``，不调 render_chart（SPEC §3.2）。"""
    ledger, champions = _write_run(tmp_path)
    # 不写 orca_env.sh，env 已清空（fixture）

    result = viz_harness.viz_struct.render_all(
        ledger_path=str(ledger), champions_path=str(champions),
        baseline_latency_ms=12.0, baseline_accuracy=0.88,
        target_latency_ms=10.0, accuracy_target=0.87,
    )
    assert result["viz_env_status"] == "env_missing"
    for name, info in result["charts"].items():
        assert info == {"pushed": False, "reason": "env_missing"}, f"{name}: {info}"
    # render_chart 必须不被调用（env_missing 时不调）
    assert viz_harness.calls == []


def test_ac5a_env_loaded_from_file_when_orca_env_sh_present(tmp_path, viz_harness, clean_orca_env):
    """AC5a env_loaded_from_file：env 缺但 ``orca_env.sh`` 在 anchor 父目录 → 自加载成功。"""
    ledger, champions = _write_run(tmp_path)
    _write_env_file(tmp_path)

    result = viz_harness.viz_struct.render_all(
        ledger_path=str(ledger), champions_path=str(champions),
        baseline_latency_ms=12.0, baseline_accuracy=0.88,
        target_latency_ms=10.0, accuracy_target=0.87,
    )
    assert result["viz_env_status"] == "env_loaded_from_file"
    # 自加载后 env 已注 → 三图正常推送
    assert os.environ.get("ORCA_CHART_SOCK") == "/tmp/orca-test.sock"
    for name, info in result["charts"].items():
        assert info["pushed"] is True


def test_ac4_socket_unreachable_reason(tmp_path, viz_harness, clean_orca_env):
    """AC4：render_chart 抛 ``无法连接`` 类 RuntimeError → ``reason=socket_unreachable``。"""
    ledger, champions = _write_run(tmp_path)
    _write_env_file(tmp_path)

    def _raise(**kw):
        raise RuntimeError("无法连接 Orca chart socket（/tmp/x.sock）：文件不存在。")
    viz_harness.viz_struct._orca_render_chart = _raise

    result = viz_harness.viz_struct.render_all(
        ledger_path=str(ledger), champions_path=str(champions),
        baseline_latency_ms=12.0, baseline_accuracy=0.88,
        target_latency_ms=10.0, accuracy_target=0.87,
    )
    for name, info in result["charts"].items():
        assert info["pushed"] is False
        assert info["reason"] == "socket_unreachable", f"{name}: {info}"


def test_ac4_ack_failed_reason(tmp_path, viz_harness, clean_orca_env):
    """AC4：render_chart 抛 ack 类（``ack 超时`` / ``拒收`` / ``缺 seq``）→ ``reason=ack_failed``。"""
    ledger, champions = _write_run(tmp_path)
    _write_env_file(tmp_path)

    # 子测试：三种 ack 类消息都归 ack_failed
    ack_messages = [
        "Orca chart socket ack 超时（10s）。",
        "Orca 拒收 chart：malformed message",
        "Orca chart ack 缺 seq 字段",
    ]
    for msg in ack_messages:
        def _raise(_msg=msg, **kw):
            raise RuntimeError(_msg)
        viz_harness.viz_struct._orca_render_chart = _raise
        result = viz_harness.viz_struct.render_all(
            ledger_path=str(ledger), champions_path=str(champions),
            baseline_latency_ms=12.0, baseline_accuracy=0.88,
            target_latency_ms=10.0, accuracy_target=0.87,
        )
        for name, info in result["charts"].items():
            assert info["reason"] == "ack_failed", f"msg={msg!r} name={name}: {info}"


def test_ac4_generic_reason_for_unclassified_exception(tmp_path, viz_harness, clean_orca_env):
    """AC4：render_chart 抛未分类异常（如 ValueError）→ ``reason=generic:<Type>:<msg>``。"""
    ledger, champions = _write_run(tmp_path)
    _write_env_file(tmp_path)

    def _raise(**kw):
        raise ValueError("chart payload 过大（5000000 > 2097152 字节）")
    viz_harness.viz_struct._orca_render_chart = _raise

    result = viz_harness.viz_struct.render_all(
        ledger_path=str(ledger), champions_path=str(champions),
        baseline_latency_ms=12.0, baseline_accuracy=0.88,
        target_latency_ms=10.0, accuracy_target=0.87,
    )
    for name, info in result["charts"].items():
        assert info["pushed"] is False
        assert info["reason"].startswith("generic:ValueError:"), f"{name}: {info}"
        assert "chart payload 过大" in info["reason"]


def test_ac4_data_insufficient_when_ledger_too_short(tmp_path, viz_harness, clean_orca_env):
    """AC4：ledger < ``_MIN_ROWS``（=2）→ ``reason=data_insufficient``（设计内，非错误）。"""
    ledger, champions = _write_run(tmp_path, n_rows=1)  # 只有 1 行
    _write_env_file(tmp_path)

    result = viz_harness.viz_struct.render_all(
        ledger_path=str(ledger), champions_path=str(champions),
        baseline_latency_ms=12.0, baseline_accuracy=0.88,
        target_latency_ms=10.0, accuracy_target=0.87,
    )
    assert result["viz_env_status"] == "env_loaded_from_file"
    for name, info in result["charts"].items():
        assert info == {"pushed": False, "reason": "data_insufficient"}, f"{name}: {info}"
    # data_insufficient 不调 render_chart
    assert viz_harness.calls == []


# ── AC5a：import_failed 路径 ───────────────────────────────────────────────


def test_ac5a_import_failed_when_orca_chart_unavailable(tmp_path, clean_orca_env):
    """AC5a import_failed：``orca.chart`` 不可用 → ``viz_env_status=import_failed``，每图 reason=import_failed。"""
    h = _VizHarness(hide_orca_chart=True)
    try:
        ledger, champions = _write_run(tmp_path)
        result = h.viz_struct.render_all(
            ledger_path=str(ledger), champions_path=str(champions),
            baseline_latency_ms=12.0, baseline_accuracy=0.88,
            target_latency_ms=10.0, accuracy_target=0.87,
        )
        assert result["viz_env_status"] == "import_failed"
        for name, info in result["charts"].items():
            assert info == {"pushed": False, "reason": "import_failed"}, f"{name}: {info}"
    finally:
        h.teardown()


# ── AC5a：--mode compare 的 compare_bar 字段 ───────────────────────────────


def test_ac5a_compare_mode_emits_compare_bar(tmp_path, viz_harness, clean_orca_env):
    """AC5a compare 模式：``charts`` 含 ``compare_bar`` 项，env ok 时 pushed=true。"""
    _, champions = _write_run(tmp_path)
    _write_env_file(tmp_path)

    result = viz_harness.viz_struct.render_compare(
        champions_path=str(champions),
        baseline_latency_ms=12.0, baseline_accuracy=0.88,
        final_latency_ms=7.5, final_accuracy=0.92,
    )
    assert result["viz_env_status"] == "env_loaded_from_file"
    assert set(result["charts"].keys()) == {"compare_bar"}
    assert result["charts"]["compare_bar"] == {"pushed": True, "reason": ""}
    # render_chart 收到一张 bar，title 唯一
    assert len(viz_harness.calls) == 1
    assert viz_harness.calls[0]["chart_type"] == "bar"
    assert viz_harness.calls[0]["title"] == "Baseline vs Champion vs Final"
    # data 含三行 baseline/champion/final
    stages = [row["stage"] for row in viz_harness.calls[0]["data"]]
    assert stages == ["baseline", "champion", "final"]
    # final latency/accuracy 来自 CLI 参数（不替换 inline 占位）
    final_row = next(r for r in viz_harness.calls[0]["data"] if r["stage"] == "final")
    assert final_row["latency_ms"] == 7.5
    assert final_row["accuracy"] == 0.92


def test_ac5a_compare_mode_env_missing(tmp_path, viz_harness, clean_orca_env):
    """AC5a compare 模式 env_missing：``compare_bar`` reason=env_missing。"""
    _, champions = _write_run(tmp_path)
    # 不写 env file

    result = viz_harness.viz_struct.render_compare(
        champions_path=str(champions),
        baseline_latency_ms=12.0, baseline_accuracy=0.88,
        final_latency_ms=7.5, final_accuracy=0.92,
    )
    assert result["viz_env_status"] == "env_missing"
    assert result["charts"]["compare_bar"] == {"pushed": False, "reason": "env_missing"}


# ── B1 命门：_main 异常兜底 ────────────────────────────────────────────────


def test_ac5a_main_fallback_emits_generic_json_on_exception(tmp_path, viz_harness, clean_orca_env, capsys):
    """AC5a / B1 命门：``_main`` 异常路径 stdout 必有合法 JSON（generic + 全图 generic:<Type>:<msg>）。

    意图：进程级异常（如 ledger 读 I/O 硬错）下，agent 仍能 dumb copy stdout JSON（不依赖 LLM 合成）。
    """
    _, champions = _write_run(tmp_path)
    _write_env_file(tmp_path)

    # 把 viz_struct.render_all 替换成抛异常（模拟进程级 I/O 硬错）
    def _boom(**kw):
        raise OSError("disk full")
    viz_harness.viz_struct.render_all = _boom

    # 模拟 CLI 调用：直接调 _main（替换 argv）
    ledger = tmp_path / "ledger.jsonl"
    sys.argv = [
        "viz_struct.py",
        "--ledger", str(ledger),
        "--champions", str(champions),
        "--baseline_latency_ms", "12.0",
        "--baseline_accuracy", "0.88",
        "--target_latency_ms", "10.0",
        "--accuracy_target", "0.87",
    ]
    exit_code = viz_harness.viz_struct._main()
    captured = capsys.readouterr()

    # 退出码 2（进程级失败信号）
    assert exit_code == 2
    # stdout 必有合法 JSON
    out = json.loads(captured.out)
    assert out["viz_env_status"] == "generic"
    for name, info in out["charts"].items():
        assert info["pushed"] is False
        assert info["reason"].startswith("generic:OSError:disk full"), f"{name}: {info}"
    assert set(out["charts"].keys()) == {"champion_trace", "champion_accuracy_trace", "pareto", "candidate_table"}


def test_main_fallback_compare_mode_emits_compare_bar_generic(tmp_path, viz_harness, clean_orca_env, capsys):
    """B1 兜底 compare 模式：异常路径 stdout 含 ``compare_bar`` 单图 generic fallback。"""
    _, champions = _write_run(tmp_path)
    _write_env_file(tmp_path)

    def _boom(**kw):
        raise OSError("disk full")
    viz_harness.viz_struct.render_compare = _boom

    sys.argv = [
        "viz_struct.py", "--mode", "compare",
        "--champions", str(champions),
        "--baseline_latency_ms", "12.0", "--baseline_accuracy", "0.88",
        "--final_latency_ms", "7.5", "--final_accuracy", "0.92",
    ]
    exit_code = viz_harness.viz_struct._main()
    captured = capsys.readouterr()
    assert exit_code == 2
    out = json.loads(captured.out)
    assert out["viz_env_status"] == "generic"
    assert set(out["charts"].keys()) == {"compare_bar"}
    assert out["charts"]["compare_bar"]["reason"].startswith("generic:OSError:")


def test_main_happy_path_default_mode_exits_zero(tmp_path, viz_harness, clean_orca_env, capsys):
    """``_main`` happy path：exit 0 + stdout 合法 JSON（含 viz_env_status + charts）。"""
    ledger, champions = _write_run(tmp_path)
    _write_env_file(tmp_path)

    sys.argv = [
        "viz_struct.py",
        "--ledger", str(ledger), "--champions", str(champions),
        "--baseline_latency_ms", "12.0", "--baseline_accuracy", "0.88",
        "--target_latency_ms", "10.0", "--accuracy_target", "0.87",
    ]
    exit_code = viz_harness.viz_struct._main()
    captured = capsys.readouterr()
    assert exit_code == 0
    out = json.loads(captured.out)
    assert out["viz_env_status"] == "env_loaded_from_file"
    assert set(out["charts"].keys()) == {"champion_trace", "champion_accuracy_trace", "pareto", "candidate_table"}


def test_main_requires_ledger_in_default_mode(capsys, tmp_path, viz_harness, clean_orca_env):
    """``--mode default`` 缺 ``--ledger`` → argparse ``parser.error`` exit 2 + stderr 提示。

    意图：钉死模式专属必填参数的跨参数校验（argparse 无法表达「mode=A 时 X 必填」）。
    """
    _, champions = _write_run(tmp_path)
    sys.argv = [
        "viz_struct.py",  # default mode
        "--champions", str(champions),
        "--baseline_latency_ms", "12.0", "--baseline_accuracy", "0.88",
        "--target_latency_ms", "10.0", "--accuracy_target", "0.87",
        # 故意不传 --ledger
    ]
    with pytest.raises(SystemExit) as exc:
        viz_harness.viz_struct._main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--ledger" in err


def test_main_requires_final_values_in_compare_mode(capsys, tmp_path, viz_harness, clean_orca_env):
    """``--mode compare`` 缺 ``--final_latency_ms`` / ``--final_accuracy`` → exit 2 + stderr 提示。"""
    _, champions = _write_run(tmp_path)
    sys.argv = [
        "viz_struct.py", "--mode", "compare",
        "--champions", str(champions),
        "--baseline_latency_ms", "12.0", "--baseline_accuracy", "0.88",
        # 故意不传 --final_*
    ]
    with pytest.raises(SystemExit) as exc:
        viz_harness.viz_struct._main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--final_latency_ms" in err
    assert "--final_accuracy" in err


# ── 补强（code-reviewer 🟡-1/🟡-2/🟡-3/🟡-5 + 🟢-1/🟢-2/🟢-3/🟢-4）─────────


def test_ac4_ack_failed_covers_eof_and_invalid_json(tmp_path, viz_harness, clean_orca_env):
    """AC4 补强：覆盖 _render.py:189-200 的两类 ack 消息（socket 关闭无 ack / ack 非 JSON）。

    code-reviewer 🟡-1：原仅 3/5 类 ack 消息被钉死，未来若消息改写易漏分类为 generic。
    """
    ledger, champions = _write_run(tmp_path)
    _write_env_file(tmp_path)
    ack_messages = [
        "Orca chart socket 关闭，未收到 ack（sock=/tmp/x.sock）",  # _render.py:189-193 EOF
        "Orca chart ack 非 JSON：b'not a json'",  # _render.py:195-200
    ]
    for msg in ack_messages:
        def _raise(_msg=msg, **kw):
            raise RuntimeError(_msg)
        viz_harness.viz_struct._orca_render_chart = _raise
        result = viz_harness.viz_struct.render_all(
            ledger_path=str(ledger), champions_path=str(champions),
            baseline_latency_ms=12.0, baseline_accuracy=0.88,
            target_latency_ms=10.0, accuracy_target=0.87,
        )
        for name, info in result["charts"].items():
            assert info["reason"] == "ack_failed", f"msg={msg!r} name={name}: {info}"


def test_ac4_socket_unreachable_also_covers_connection_refused_message(
    tmp_path, viz_harness, clean_orca_env,
):
    """AC4 补强：socket_unreachable 同时覆盖「文件不存在」与「连接被拒」两种消息。

    code-reviewer 🟢-1：_render.py:174-182 产生两类「无法连接」消息，原仅覆盖 FileNotFoundError 变体。
    """
    ledger, champions = _write_run(tmp_path)
    _write_env_file(tmp_path)
    refused_messages = [
        "无法连接 Orca chart socket（/tmp/x.sock）：文件不存在。",
        "无法连接 Orca chart socket（/tmp/x.sock）：连接被拒。",
    ]
    for msg in refused_messages:
        def _raise(_msg=msg, **kw):
            raise RuntimeError(_msg)
        viz_harness.viz_struct._orca_render_chart = _raise
        result = viz_harness.viz_struct.render_all(
            ledger_path=str(ledger), champions_path=str(champions),
            baseline_latency_ms=12.0, baseline_accuracy=0.88,
            target_latency_ms=10.0, accuracy_target=0.87,
        )
        for name, info in result["charts"].items():
            assert info["reason"] == "socket_unreachable", f"msg={msg!r} name={name}: {info}"


def test_ac4_half_injection_classified_as_env_missing(tmp_path, viz_harness, clean_orca_env, monkeypatch):
    """AC4 补强（R4）：SOCK 已注但其他 3 键缺 → ``_render.py:97-101`` raise → ``env_missing``。

    code-reviewer R4：SPEC §3.1 KISS 决策只在 _resolve_env_status 看 SOCK 单键，但实际
    「SOCK 注了 RUN_ID/NODE/SESSION_ID 没注」的 half-injection 场景会进 render_chart 后才 raise，
    原归类落到 generic:RuntimeError，应更精确归 env_missing。
    """
    ledger, champions = _write_run(tmp_path)
    # 只注 SOCK（模拟 half-injection），其他键不注
    monkeypatch.setenv("ORCA_CHART_SOCK", "/tmp/preset.sock")
    # 不写 orca_env.sh，让自加载也找不到（保证只有 SOCK 在 env）

    def _raise(**kw):
        raise RuntimeError(
            "render_chart 不在 Orca run 上下文中（缺 ORCA_* env: ORCA_RUN_ID, ORCA_NODE, "
            "ORCA_SESSION_ID）。本函数仅可由 Orca 编排的 script 子进程调用。"
        )
    viz_harness.viz_struct._orca_render_chart = _raise
    # 注意：env 含 SOCK → _resolve_env_status 返 ok → pusher 会真调 render_chart → 触发上面 raise
    result = viz_harness.viz_struct.render_all(
        ledger_path=str(ledger), champions_path=str(champions),
        baseline_latency_ms=12.0, baseline_accuracy=0.88,
        target_latency_ms=10.0, accuracy_target=0.87,
    )
    for name, info in result["charts"].items():
        assert info["reason"] == "env_missing", f"{name}: {info}"


def test_ac4_data_insufficient_when_all_rows_invalid(tmp_path, viz_harness, clean_orca_env):
    """AC4 补强（🟡-3）：ledger 行数足但**无有效数据点**（全 FAIL_export latency=-1）
    → ``reason=data_insufficient``（``not data`` 分支，区别于 ``len(ledger) < _MIN_ROWS``）。

    意图：覆盖 SPEC §0 背景提到的 FAIL_latency/FAIL_export 真实场景——所有行 latency 缺失。
    """
    ledger = tmp_path / "ledger.jsonl"
    # 3 行全 FAIL_export（latency_ms=-1 → _to_float 视为有效但 lat<0 被过滤）
    rows = [
        {"id": f"c{i}", "parent": "b", "path": "p", "round": i,
         "status": "FAIL_export", "tag": "structural",
         "latency_ms": -1, "accuracy": -1,
         "met_accuracy": False, "snapshot": "/x", "onnx": "/x",
         "diff_summary": "d", "hypothesis": "h"}
        for i in range(1, 4)
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    champions = tmp_path / "champions.jsonl"
    champions.write_text('{}\n', encoding="utf-8")
    _write_env_file(tmp_path)

    result = viz_harness.viz_struct.render_all(
        ledger_path=str(ledger), champions_path=str(champions),
        baseline_latency_ms=12.0, baseline_accuracy=0.88,
        target_latency_ms=10.0, accuracy_target=0.87,
    )
    # champion_trace / pareto 因「无有效数据点」走 data_insufficient（lat<0 全过滤）；
    # candidate_table 不按 latency 过滤（表展示所有行，含 FAIL_export），仍 pushed=true。
    assert result["charts"]["champion_trace"] == {"pushed": False, "reason": "data_insufficient"}
    assert result["charts"]["pareto"] == {"pushed": False, "reason": "data_insufficient"}
    assert result["charts"]["candidate_table"]["pushed"] is True
    # 只调了 candidate_table 一次（两图 data_insufficient 不调）
    assert len(viz_harness.calls) == 1
    assert viz_harness.calls[0]["title"] == "Candidate Ledger (per change)"


def test_ac1a_render_chart_can_read_env_after_self_load(tmp_path, clean_orca_env, monkeypatch):
    """AC1a 补强（🟡-2）：SPEC §6 AC1a 原文「mock render_chart 断言其能读到 env」。

    意图：钉死「自加载 → render_chart 读 env」端到端链路（原 test_env.py 只验证写入 env 这半）。
    用真 ``orca.chart._render.render_chart`` + mock socket，验自加载后 render_chart 不再 raise env-missing。
    """
    # 造 run_dir + env 文件
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    ledger = run_dir / "ledger.jsonl"
    ledger.write_text('{"id":"c1"}\n', encoding="utf-8")
    (run_dir / "orca_env.sh").write_text(
        "export ORCA_RUN_ID='run-x-2026'\n"
        "export ORCA_NODE='curator'\n"
        "export ORCA_SESSION_ID='sess-x'\n"
        "export ORCA_CHART_SOCK='/tmp/orca-mock-ack.sock'\n",
        encoding="utf-8",
    )
    # 清空 ORCA_* env
    for k in list(os.environ):
        if k.startswith("ORCA_"):
            monkeypatch.delenv(k, raising=False)

    from orca.chart._env import load_run_env_from_artifacts
    injected = load_run_env_from_artifacts(ledger)
    assert "ORCA_CHART_SOCK" in injected  # 自加载成功

    # mock socket（让 render_chart 走到 ack 收到 seq=42），验 env 不再缺
    import unittest.mock as _mock
    sock_mock = _mock.MagicMock()
    sock_mock.__enter__.return_value = sock_mock
    sock_mock.__exit__.return_value = False
    makefile_mock = _mock.MagicMock()
    makefile_mock.__enter__.return_value = makefile_mock
    makefile_mock.__exit__.return_value = False
    makefile_mock.readline.return_value = b'{"ok": true, "seq": 42}\n'
    sock_mock.makefile.return_value = makefile_mock

    from orca.chart._render import render_chart
    with _mock.patch("orca.chart._render.socket.socket", return_value=sock_mock):
        seq = render_chart(chart_type="line", data=[{"x": 1, "y": 2.0}], label="g", title="t")
    assert seq == 42  # 走到 ack 路径 = env 读成功（缺 env 会在 ack 前 raise）


def test_compare_bar_data_insufficient_when_champions_empty(tmp_path, viz_harness, clean_orca_env):
    """补强（🟢-3）：空 champions（全失败 run）→ compare_bar 返 data_insufficient，不推误导图。

    意图：覆盖 ``_push_compare_bar`` 显式防御分支 ``if not champions: return False, "data_insufficient"``。
    code-reviewer R3：原 latency=0 占位有「不可能的优秀值」误导，改返 data_insufficient。
    """
    champions = tmp_path / "champions.jsonl"
    champions.write_text("", encoding="utf-8")  # 空
    _write_env_file(tmp_path)

    result = viz_harness.viz_struct.render_compare(
        champions_path=str(champions),
        baseline_latency_ms=12.0, baseline_accuracy=0.88,
        final_latency_ms=7.5, final_accuracy=0.92,
    )
    assert result["charts"]["compare_bar"] == {"pushed": False, "reason": "data_insufficient"}
    assert viz_harness.calls == []


def test_main_happy_path_compare_mode_exits_zero(tmp_path, viz_harness, clean_orca_env, capsys):
    """补强（🟡-5）：``_main`` + ``--mode compare`` + happy path → exit 0 + stdout 含 compare_bar。

    意图：覆盖 argparse 的 --final_* 解析 + 路由到 render_compare + 写入 stdout 的完整链路
    （原仅覆盖 render_compare 直调 + _main compare error path，缺 happy path 组合）。
    """
    _, champions = _write_run(tmp_path)
    _write_env_file(tmp_path)
    sys.argv = [
        "viz_struct.py", "--mode", "compare",
        "--champions", str(champions),
        "--baseline_latency_ms", "12.0", "--baseline_accuracy", "0.88",
        "--final_latency_ms", "7.5", "--final_accuracy", "0.92",
    ]
    exit_code = viz_harness.viz_struct._main()
    captured = capsys.readouterr()
    assert exit_code == 0
    out = json.loads(captured.out)
    assert out["viz_env_status"] == "env_loaded_from_file"
    assert set(out["charts"].keys()) == {"compare_bar"}
    assert out["charts"]["compare_bar"]["pushed"] is True


def test_ac3_env_file_with_comment_or_empty_sock_value(tmp_path, clean_orca_env):
    """补强（🟢-2）：marker 边界——注释行不匹配；空 SOCK 值匹配 marker 但解析后不注入。

    意图：钉死 ``_env.py`` 的 ``_SOCK_MARK_PATTERN``（^export ORCA_CHART_SOCK=）行为契约。
    """
    from orca.chart._env import load_run_env_from_artifacts
    for k in list(os.environ):
        if k.startswith("ORCA_"):
            os.environ.pop(k, None)

    # 边界 1：marker 行是注释（# export ORCA_CHART_SOCK=...）→ 不匹配
    rd1 = tmp_path / "case-comment"
    rd1.mkdir()
    (rd1 / "orca_env.sh").write_text(
        "# export ORCA_CHART_SOCK=/tmp/x.sock\nexport ORCA_RUN_ID='x'\n",
        encoding="utf-8",
    )
    (rd1 / "ledger.jsonl").write_text("{}", encoding="utf-8")
    assert load_run_env_from_artifacts(rd1 / "ledger.jsonl") == {}
    assert "ORCA_CHART_SOCK" not in os.environ

    # 边界 2：marker 行存在但值空（``export ORCA_CHART_SOCK=``）→ 匹配 marker（找到文件），
    # 但 shlex.split('')=[] → 不注入该键。仍返 {}（无任何键被注入）。
    for k in list(os.environ):
        if k.startswith("ORCA_"):
            os.environ.pop(k, None)
    rd2 = tmp_path / "case-empty-sock"
    rd2.mkdir()
    (rd2 / "orca_env.sh").write_text(
        "export ORCA_CHART_SOCK=\nexport ORCA_RUN_ID='r'\n", encoding="utf-8",
    )
    (rd2 / "ledger.jsonl").write_text("{}", encoding="utf-8")
    # RUN_ID 仍被注入（其他键正常），但 SOCK 不被注入（空值）
    injected = load_run_env_from_artifacts(rd2 / "ledger.jsonl")
    assert "ORCA_CHART_SOCK" not in injected
    assert "ORCA_RUN_ID" in injected  # 其他键照常

"""tests/workflows/test_workflow_viz_audit_fixes.py —— 2026-07-24 workflow viz 审计修复的纯 helper 单测。

覆盖审计 12 项（5 P1 + 7 P2）中的**纯数学 / 解析 helper**（Rule 9：钉死意图）。
不依赖 ts_quant / torch / orca.chart runtime —— 仅 stdlib + mock。

覆盖项：
- P1-1：``nas_agent.search.problem._infeasible_result`` —— 超约束候选 death-penalty 保留 latency
- P1-2：``tail_metrics._axis_direction`` —— cost→min / quality→max
- P1-3：``run_bit_curve._load_bit_trend_layer_bits`` —— 多 schema 形态 fail-soft 解析
- P1-4：``run_bit_curve._cumulative_best`` —— 评估序累计最优（max/min 两向 + 空集 + 单点）
- P2-5：``_quant_scripts/_common`` 下沉 helper ——
        ``is_better`` / ``load_env_file`` / ``load_adapter`` / ``dump_json`` / ``free_model`` /
        ``resolve_eval``（业务 / teacher-student 两路径 + 字段缺失 fail loud）

P1-5 / P2-1 / P2-2 / P2-3 / P2-4 / P2-6 / P2-7：非纯 helper（side-effect / IO / 推图），
按 SPEC §13 「render_chart 推图本身只在 Orca run 上下文可跑、不做离线推图测试」原则，
留待 E2E 真机验证（commented refs in release note）。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import textwrap
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_QUANT_SCRIPTS = REPO / "workflows" / "agents" / "_quant_scripts"
if str(_QUANT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_QUANT_SCRIPTS))


# 本测试文件的 helper 会 stub ts_quant / orca.chart / _device / _common 到 sys.modules；
# autouse fixture 快照+还原，防泄漏到同 session 的其他测试（尤其依赖真 orca.chart 的
# viz_struct / viz_kd 测试）。
_STUB_KEYS = (
    "ts_quant",
    "orca",
    "orca.chart",
    "_device",
    "_common",
    "run_bit_curve_under_test",
)


@pytest.fixture(autouse=True)
def _isolate_sys_modules():
    saved = {k: sys.modules.get(k) for k in _STUB_KEYS}
    saved_path = list(sys.path)
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    sys.path[:] = saved_path


# ───────────────────────────────── helpers ─────────────────────────


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── P1-1：problem._infeasible_result ────────────────────────────────────
# 直接加载 problem.py 会触发 `import torch` + `import ray`；不假设它们可用。
# 改走「文件级 AST exec 只取纯函数」太脆弱——torch/ray 在 Orca CI 镜像里其实常装，
# 但本测试不依赖它们。我们用 importorskip + try/except 加载 problem.py 的两个纯 helper
# 并通过 exec 局部命名空间取它们（避免触发 ray.init）。


def _extract_helper_src(path: Path, names: list[str]) -> str:
    """从源文件抽若干顶层函数 / 常量的真实源码段（AST 定位 + 原文切片）。

    用途：测试需要加载纯 helper（不依赖 torch/ray），但 ``import problem`` 会触发
    顶层 ``import ray`` / ``import torch`` 失败。本 helper 直接从源文件字符串切片取
    真实定义（不 exec 整个模块），保证测试钉的是真源码而非手抄副本（Rule 9）。

    Args:
        path: 源文件路径。
        names: 顶层符号名（函数 / 常量赋值），按这些名字定位 AST 节点取行号区间。
    """
    src_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    tree = ast.parse("".join(src_lines))
    line_ranges: list[tuple[int, int]] = []
    for node in ast.iter_child_nodes(tree):
        target_name: str | None = None
        if isinstance(node, ast.FunctionDef):
            target_name = node.name
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    target_name = t.id
                    break
        if target_name in names:
            # ast line numbers are 1-based; end_lineno inclusive.
            line_ranges.append((node.lineno, node.end_lineno or node.lineno))
    if not line_ranges:
        raise AssertionError(f"未在 {path} 找到 helpers: {names}")
    line_ranges.sort()
    out: list[str] = []
    for s, e in line_ranges:
        out.extend(src_lines[s - 1 : e])
    return "".join(out)


def _load_problem_helpers():
    """从 problem.py 抽 ``_infeasible_result`` / ``_is_preserved_objective`` 真实源码并 exec。

    钉的是**真源码**（AST 切片），非手抄——若未来 problem.py 改了 token / 映射逻辑，
    本测试自动跟随（Rule 9 真意图验证）。helper 本身不依赖 torch/ray，只依赖一个
    module-级 WORST_FITNESS 常量，故在 namespace 注入该常量后直接 exec 安全。
    """
    problem_path = REPO / "nas-agent" / "nas_agent" / "search" / "problem.py"
    helper_src = _extract_helper_src(
        problem_path,
        ["_PRESERVED_OBJECTIVE_TOKENS", "_is_preserved_objective", "_infeasible_result"],
    )
    ns: dict = {"WORST_FITNESS": 3.4028235e38}
    exec(helper_src, ns)
    return ns["_infeasible_result"], ns["_is_preserved_objective"]


class TestP11InfeasibleResult:
    """P1-1：超 latency 约束的候选 death-penalty 保留 latency 实测值。"""

    def setup_method(self):
        self._infeasible_result, self._is_preserved = _load_problem_helpers()

    def test_latency_objective_preserved(self):
        out = self._infeasible_result(latency=12.5, objective_names=["acc", "latency", "params"])
        # latency 保留实测
        assert out["latency"] == 12.5
        # 其它目标走 death-penalty
        assert out["acc"] == 3.4028235e38
        assert out["params"] == 3.4028235e38

    def test_multiple_latency_like_objectives_preserved(self):
        # 泛化：任何名字含 'lat' 的目标都保留实测 latency（如 npu_latency）。
        out = self._infeasible_result(
            latency=99.0, objective_names=["acc", "latency", "npu_latency", "mse"]
        )
        assert out["latency"] == 99.0
        assert out["npu_latency"] == 99.0
        assert out["acc"] == 3.4028235e38
        assert out["mse"] == 3.4028235e38

    def test_no_latency_objective_all_worst(self):
        # 罕见：objective_names 完全没 latency-like → 全 WORST（旧行为）。
        out = self._infeasible_result(latency=5.0, objective_names=["acc", "mse"])
        assert out == {"acc": 3.4028235e38, "mse": 3.4028235e38}

    def test_is_preserved_objective_pure(self):
        assert self._is_preserved("latency") is True
        assert self._is_preserved("Latency") is True  # 大小写不敏感
        assert self._is_preserved("npu_latency") is True
        assert self._is_preserved("accuracy") is False
        assert self._is_preserved("") is False


# ── P1-2：tail_metrics._axis_direction ─────────────────────────────────


class TestP12AxisDirection:
    def _load_tail_metrics_helper(self):
        # tail_metrics.py 顶层 ``from orca.chart import render_chart`` 会 import-error
        # 当不在 Orca runtime。用 AST 切片抽 ``_axis_direction`` 真实源码（非手抄，
        # Rule 9）。
        path = REPO / "workflows" / "agents" / "nas-train-runner" / "scripts" / "tail_metrics.py"
        helper_src = _extract_helper_src(path, ["_axis_direction"])
        ns: dict = {}
        exec(helper_src, ns)
        return ns["_axis_direction"]

    def test_cost_to_min(self):
        f = self._load_tail_metrics_helper()
        assert f("cost") == "min"

    def test_quality_to_max(self):
        f = self._load_tail_metrics_helper()
        assert f("quality") == "max"

    def test_unknown_defaults_to_min(self):
        # 未知 kind 当 cost 处理（保守 = 越小越好，与 _classify_obj 的兜底一致）。
        f = self._load_tail_metrics_helper()
        assert f("unknown") == "min"
        assert f("") == "min"


# ── P1-3：run_bit_curve._load_bit_trend_layer_bits ────────────────────


def _load_run_bit_curve_module():
    """加载 run_bit_curve 模块（mock ts_quant / orca.chart，避免 runtime 依赖）。

    run_bit_curve.py 顶层 try-import ts_quant → _TS_QUANT_OK=False（不阻断 import）；
    本函数把 ts_quant stub 成可调用（避免真实依赖），并 stub _device（已在 sys.path）
    实际上 _device 只需要 add_device_seed_args/resolve_device_and_seed/wrap_forward_with_device
    的存在性——也一并 stub。返回加载好的模块对象。

    注意：``_device`` / ``_common`` 是同名跨目录模块（``_struct_scripts`` / ``_quant_scripts``）；
    必须先清 sys.modules 缓存并把 ``_quant_scripts`` 放 sys.path[0]，否则 struct 测试
    先跑后留下的 ``_device`` 缓存会让本模块拿到错的实现（struct 版无 ``add_device_seed_args``）。
    """
    # 清同名 module 缓存，防止 struct 测试的 _device/_common 泄漏。
    for mod_name in ("_device", "_common", "run_bit_curve_under_test"):
        sys.modules.pop(mod_name, None)
    # _quant_scripts 放 sys.path[0]（_load_module 在本文件顶部已加，但同 session 中
    # struct 测试可能把 _struct_scripts 插到更前；重置一次）。
    if str(_QUANT_SCRIPTS) in sys.path:
        sys.path.remove(str(_QUANT_SCRIPTS))
    sys.path.insert(0, str(_QUANT_SCRIPTS))

    # stub ts_quant
    ts_quant_stub = types.ModuleType("ts_quant")
    for name in (
        "MixPrecisionSearchConfig",
        "MetricSpec",
        "QConfig",
        "TSQuantizer",
        "quantize_model",
        "search_mix_precision",
    ):
        setattr(ts_quant_stub, name, type(name, (), {"__init__": lambda self, *a, **k: None}))
    sys.modules["ts_quant"] = ts_quant_stub
    # stub orca.chart（viz 内部 try import）
    orca_stub = types.ModuleType("orca")
    orca_chart_stub = types.ModuleType("orca.chart")
    orca_chart_stub.render_chart = lambda **kw: None
    sys.modules["orca"] = orca_stub
    sys.modules["orca.chart"] = orca_chart_stub

    path = REPO / "workflows" / "agents" / "bit-curve-searcher" / "scripts" / "run_bit_curve.py"
    return _load_module(path, "run_bit_curve_under_test")


class TestP13BitTrendParser:
    """bit_trend.json schema 未稳定 → 多形态 fail-soft 解析。"""

    def test_file_missing_returns_none(self, tmp_path):
        mod = _load_run_bit_curve_module()
        out = mod._load_bit_trend_layer_bits(tmp_path / "absent.json")
        assert out is None

    def test_shape_flat_dict_scalar(self, tmp_path):
        mod = _load_run_bit_curve_module()
        p = tmp_path / "bit_trend.json"
        p.write_text(json.dumps({"layer0": 8, "layer1": 4, "layer2": 8}), encoding="utf-8")
        out = mod._load_bit_trend_layer_bits(p)
        assert out == [("layer0", 8), ("layer1", 4), ("layer2", 8)]

    def test_shape_flat_dict_nbits(self, tmp_path):
        mod = _load_run_bit_curve_module()
        p = tmp_path / "bit_trend.json"
        p.write_text(
            json.dumps({"layer0": {"n_bits": 4}, "layer1": {"w_n_bits": 8}}),
            encoding="utf-8",
        )
        out = mod._load_bit_trend_layer_bits(p)
        assert out == [("layer0", 4), ("layer1", 8)]

    def test_shape_records_list(self, tmp_path):
        mod = _load_run_bit_curve_module()
        p = tmp_path / "bit_trend.json"
        p.write_text(
            json.dumps([{"layer": "a", "bit": 4}, {"name": "b", "n_bits": 8}]),
            encoding="utf-8",
        )
        out = mod._load_bit_trend_layer_bits(p)
        assert out == [("a", 4), ("b", 8)]

    def test_shape_nested_layers_key(self, tmp_path):
        mod = _load_run_bit_curve_module()
        p = tmp_path / "bit_trend.json"
        p.write_text(
            json.dumps({"layers": [{"name": "a", "bit": 4}]}),
            encoding="utf-8",
        )
        out = mod._load_bit_trend_layer_bits(p)
        assert out == [("a", 4)]

    def test_invalid_json_returns_none(self, tmp_path):
        mod = _load_run_bit_curve_module()
        p = tmp_path / "bit_trend.json"
        p.write_text("{not valid json", encoding="utf-8")
        out = mod._load_bit_trend_layer_bits(p)
        assert out is None

    def test_zero_bit_filtered(self, tmp_path):
        # bit=0 不合法（位宽必须 >0）→ 该层剔除
        mod = _load_run_bit_curve_module()
        p = tmp_path / "bit_trend.json"
        p.write_text(json.dumps({"a": 4, "b": 0}), encoding="utf-8")
        out = mod._load_bit_trend_layer_bits(p)
        assert out == [("a", 4)]

    def test_unrecognized_schema_returns_none(self, tmp_path):
        mod = _load_run_bit_curve_module()
        p = tmp_path / "bit_trend.json"
        # schema 完全不匹配 → None + stderr warn（caller 跳过）
        p.write_text(json.dumps({"unrelated": "string_value"}), encoding="utf-8")
        out = mod._load_bit_trend_layer_bits(p)
        assert out is None


# ── P1-4：run_bit_curve._cumulative_best ──────────────────────────────


class TestP14CumulativeBest:
    def test_empty_records(self):
        mod = _load_run_bit_curve_module()
        assert mod._cumulative_best([], "max") == []

    def test_single_record(self):
        mod = _load_run_bit_curve_module()
        out = mod._cumulative_best([{"primary_metric": 0.5}], "max")
        assert out == [{"order": 0.0, "best": 0.5}]

    def test_running_max(self):
        mod = _load_run_bit_curve_module()
        recs = [
            {"primary_metric": 0.3},
            {"primary_metric": 0.7},
            {"primary_metric": 0.5},
            {"primary_metric": 0.9},
        ]
        out = mod._cumulative_best(recs, "max")
        bests = [r["best"] for r in out]
        assert bests == [0.3, 0.7, 0.7, 0.9]
        assert [r["order"] for r in out] == [0.0, 1.0, 2.0, 3.0]

    def test_running_min(self):
        mod = _load_run_bit_curve_module()
        recs = [
            {"primary_metric": 0.5},
            {"primary_metric": 0.2},
            {"primary_metric": 0.8},
            {"primary_metric": 0.4},
        ]
        out = mod._cumulative_best(recs, "min")
        bests = [r["best"] for r in out]
        assert bests == [0.5, 0.2, 0.2, 0.2]

    def test_fallback_score_key(self):
        # _point_metric 兜底走 'score' 键
        mod = _load_run_bit_curve_module()
        out = mod._cumulative_best([{"score": 1.0}, {"score": 2.0}], "max")
        assert [r["best"] for r in out] == [1.0, 2.0]

    def test_default_zero_metric(self):
        # 既无 primary_metric 也无 score → _point_metric 返回 0.0；纳入累计。
        mod = _load_run_bit_curve_module()
        out = mod._cumulative_best([{}, {}], "max")
        assert [r["best"] for r in out] == [0.0, 0.0]


# ── P2-5：_common helper ─────────────────────────────────────────────


class TestP25CommonIsBetter:
    def test_higher_is_better_gt(self):
        from _common import is_better
        assert is_better(0.8, 0.5, higher_is_better=True) is True
        assert is_better(0.5, 0.8, higher_is_better=True) is False

    def test_lower_is_better_lt(self):
        from _common import is_better
        assert is_better(0.1, 0.5, higher_is_better=False) is True
        assert is_better(0.5, 0.1, higher_is_better=False) is False

    def test_equal_returns_false(self):
        from _common import is_better
        assert is_better(0.5, 0.5, higher_is_better=True) is False
        assert is_better(0.5, 0.5, higher_is_better=False) is False


class TestP25CommonLoadEnvFile:
    def test_parses_exports(self, tmp_path, monkeypatch):
        from _common import load_env_file
        # 清干净可能预存在的 key
        for k in ("ORCA_TEST_A", "ORCA_TEST_B"):
            monkeypatch.delenv(k, raising=False)
        env_file = tmp_path / "orca_env.sh"
        env_file.write_text(
            textwrap.dedent("""
                # comment
                export ORCA_TEST_A=value_a
                export ORCA_TEST_B="quoted_b"
                not_an_export_line=ignored
            """).strip(),
            encoding="utf-8",
        )
        load_env_file(str(env_file), log_prefix="[test] ")
        assert os.environ["ORCA_TEST_A"] == "value_a"
        assert os.environ["ORCA_TEST_B"] == "quoted_b"

    def test_existing_env_not_overwritten(self, tmp_path, monkeypatch):
        from _common import load_env_file
        monkeypatch.setenv("ORCA_TEST_EXISTING", "original")
        env_file = tmp_path / "orca_env.sh"
        env_file.write_text('export ORCA_TEST_EXISTING="new"', encoding="utf-8")
        load_env_file(str(env_file), log_prefix="[test] ")
        # setdefault → 已存在的 env 不覆盖
        assert os.environ["ORCA_TEST_EXISTING"] == "original"

    def test_missing_file_noop(self, tmp_path):
        from _common import load_env_file
        # 缺文件 → 静默 return（stderr warn），不抛
        load_env_file(str(tmp_path / "absent.sh"), log_prefix="[test] ")

    def test_empty_path_noop(self):
        from _common import load_env_file
        load_env_file("", log_prefix="[test] ")  # 空路径 → no-op


class TestP25CommonDumpJson:
    def test_atomic_write(self, tmp_path):
        from _common import dump_json
        p = tmp_path / "out.json"
        dump_json({"a": 1, "b": "x"}, p)
        assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "b": "x"}

    def test_default_str_for_unserializable(self, tmp_path):
        from _common import dump_json

        class Obj:
            def __str__(self):
                return "OBJ_STR"

        p = tmp_path / "out.json"
        dump_json({"o": Obj()}, p)
        # default=str → 对象被 str() 化
        assert json.loads(p.read_text(encoding="utf-8"))["o"] == "OBJ_STR"

    def test_no_tmp_file_left(self, tmp_path):
        from _common import dump_json
        p = tmp_path / "out.json"
        dump_json({"a": 1}, p)
        # 原子写后 tmp 文件不应残留
        assert not (tmp_path / "out.json.tmp").exists()


class TestP25CommonLoadAdapter:
    def test_loads_adapter_module(self, tmp_path):
        from _common import load_adapter
        adapter_path = tmp_path / "adapter.py"
        adapter_path.write_text(
            "VALUE = 42\ndef get():\n    return VALUE\n",
            encoding="utf-8",
        )
        mod = load_adapter(str(adapter_path), "test_adapter_unique", log_prefix="[test] ")
        assert mod.VALUE == 42
        assert mod.get() == 42

    def test_missing_file_exits_2(self, tmp_path):
        from _common import load_adapter
        with pytest.raises(SystemExit) as ei:
            load_adapter(str(tmp_path / "absent.py"), "x", log_prefix="[test] ")
        assert ei.value.code == 2


class TestP25CommonFreeModel:
    def test_none_is_noop(self):
        from _common import free_model
        # 不抛
        free_model(None)

    def test_object_deleted(self):
        from _common import free_model

        class Live:
            pass

        # 不抛（del + gc.collect 路径）
        free_model(Live())


class TestP25CommonResolveEval:
    """_common.resolve_eval 三脚本共享契约（业务 / teacher-student 两路径）。"""

    def test_business_path(self, monkeypatch):
        # resolve_eval 内部 ``from ts_quant.eval import build_teacher_student_eval_fn``
        # 是局部 import；业务路径不会走到那。mock 一下 ts_quant 防御。
        from _common import resolve_eval

        sentinel = object()

        class Adapter:
            def get_eval_fn(self):
                return lambda qm: {"acc": 0.9}

            def get_metric_spec(self):
                return {"primary_metric": "acc", "higher_is_better": True}

        fn, kind, hib = resolve_eval(
            Adapter(), fp_model=None, eval_loader=None, forward_fn=None, log_prefix="[t] "
        )
        assert kind == "acc"
        assert hib is True
        assert fn(None) == {"acc": 0.9}

    def test_business_path_missing_metric_spec_exits_2(self):
        from _common import resolve_eval

        class Adapter:
            def get_eval_fn(self):
                return lambda qm: {}

            # 故意不提供 get_metric_spec

        with pytest.raises(SystemExit) as ei:
            resolve_eval(Adapter(), None, None, None, log_prefix="[t] ")
        assert ei.value.code == 2

    def test_business_path_empty_primary_metric_exits_2(self):
        from _common import resolve_eval

        class Adapter:
            def get_eval_fn(self):
                return lambda qm: {}

            def get_metric_spec(self):
                return {"higher_is_better": True}  # 缺 primary_metric

        with pytest.raises(SystemExit) as ei:
            resolve_eval(Adapter(), None, None, None, log_prefix="[t] ")
        assert ei.value.code == 2

    def test_teacher_student_path_requires_forward_fn_exits_2(self):
        # 无业务 eval_fn + 无 forward_fn → exit 2（不要走 teacher-student 误算）
        from _common import resolve_eval

        class Adapter:
            pass  # 无 get_eval_fn / get_metric_spec / forward_fn

        with pytest.raises(SystemExit) as ei:
            resolve_eval(Adapter(), fp_model=None, eval_loader=None, forward_fn=None, log_prefix="[t] ")
        assert ei.value.code == 2

    def test_teacher_student_path_with_ts_quant_mock(self, monkeypatch):
        # mock ts_quant.eval.build_teacher_student_eval_fn → 验证返回 (fn, 'mse', False)
        from _common import resolve_eval

        fake_ts_quant = types.ModuleType("ts_quant")
        fake_ts_quant_eval = types.ModuleType("ts_quant.eval")
        fake_ts_quant_eval.build_teacher_student_eval_fn = lambda **kw: lambda qm: {"mse": 0.1}
        fake_ts_quant.eval = fake_ts_quant_eval
        monkeypatch.setitem(sys.modules, "ts_quant", fake_ts_quant)
        monkeypatch.setitem(sys.modules, "ts_quant.eval", fake_ts_quant_eval)

        class Adapter:
            pass

        fn, kind, hib = resolve_eval(
            Adapter(),
            fp_model=object(),
            eval_loader=[],
            forward_fn=lambda *a, **k: None,
            log_prefix="[t] ",
        )
        assert kind == "mse"
        assert hib is False
        assert fn(None) == {"mse": 0.1}


class TestP25BitwidthPresets:
    """_common.BITWIDTH_PRESETS —— 三脚本共享位宽表，钉死 w4a16 真正语义。"""

    def test_all_expected_keys_present(self):
        from _common import BITWIDTH_PRESETS
        assert set(BITWIDTH_PRESETS.keys()) == {
            "w4a4-mx",
            "w4a8-mx",
            "w8a8-mx",
            "w8a8-int",
            "w4a16",
        }

    def test_w4a16_a_quant_disabled(self):
        # w4a16 真正语义：weight-only INT4 + 激活 bypass fake-quant（a_quant_enabled=False）
        from _common import BITWIDTH_PRESETS
        assert BITWIDTH_PRESETS["w4a16"]["a_quant_enabled"] is False
        assert BITWIDTH_PRESETS["w4a16"]["method"] == "int"
        assert BITWIDTH_PRESETS["w4a16"]["w_n_bits"] == 4

    def test_mx_block_size_is_16(self):
        from _common import BITWIDTH_PRESETS
        for key in ("w4a4-mx", "w4a8-mx", "w8a8-mx"):
            assert BITWIDTH_PRESETS[key]["block_size"] == 16
            assert BITWIDTH_PRESETS[key]["method"] == "mx"

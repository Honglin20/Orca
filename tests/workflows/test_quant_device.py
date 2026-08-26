"""test_quant_device.py —— 验证 _quant_scripts/_device.py 的 device / seed / batch 搬移逻辑。

P5 共享模块测试（plan §P5：单一真相源 device 解析逻辑）。
不依赖 ts_quant / orca.chart（纯 torch + Python stdlib）。

覆盖：
- resolve_device：auto / 显式 cuda:n / cpu / 非法值
- is_npu_available：find_spec 路径（torch_npu 未装时 False）
- set_seed：random / numpy / torch RNG 都被固定（两次同 seed 结果一致）
- move_batch_to_device：dict / tuple / list / Tensor / scalar 全覆盖
- wrap_forward_with_device：forward_fn 被 transparently 包，batch 在 device 上
"""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pytest

# 把 _quant_scripts/ 加进 sys.path
# tests/workflows/test_quant_device.py → parents[2] = repo root
_QUANT_SCRIPTS = Path(__file__).resolve().parents[2] / "workflows" / "agents" / "_quant_scripts"
sys.path.insert(0, str(_QUANT_SCRIPTS))

torch = pytest.importorskip("torch")

from _device import (  # noqa: E402
    is_npu_available,
    move_batch_to_device,
    resolve_device,
    set_seed,
    wrap_forward_with_device,
)


# ─────────────────────────────────────────────────────────────────
# resolve_device
# ─────────────────────────────────────────────────────────────────

class TestResolveDevice:
    def test_explicit_cpu(self):
        d = resolve_device("cpu")
        assert d.type == "cpu"
        assert d.index is None

    def test_explicit_cuda_with_index(self):
        # 不假设 cuda 可用——torch.device 解析不触发 runtime init
        d = resolve_device("cuda:0")
        assert d.type == "cuda"
        assert d.index == 0

    def test_explicit_cuda_no_index_binds_local_rank(self):
        d = resolve_device("cuda", local_rank=2)
        assert d.type == "cuda"
        assert d.index == 2

    def test_empty_arg_is_auto(self):
        # 空 → auto 探测；至少能返回一个合法 device
        d = resolve_device("")
        assert d.type in ("cuda", "npu", "cpu")

    def test_auto_explores_available(self):
        d = resolve_device("auto")
        if torch.cuda.is_available():
            assert d.type == "cuda"
        else:
            # 非cuda 环境：cpu 或 npu（取决于 torch_npu 是否装）
            assert d.type in ("npu", "cpu")

    def test_invalid_device_fails_loud(self):
        # fail loud：非法值（如 "tpu"）应 raise，绝不静默退 cpu
        with pytest.raises(Exception):
            resolve_device("tpu")


# ─────────────────────────────────────────────────────────────────
# is_npu_available
# ─────────────────────────────────────────────────────────────────

def test_is_npu_available_returns_bool():
    assert isinstance(is_npu_available(), bool)


def test_is_npu_available_no_torch_npu_in_ci():
    """CI 通常无 torch_npu；find_spec=None → False。装了的环境也应是 bool。"""
    if importlib.util.find_spec("torch_npu") is None:
        assert is_npu_available() is False


# ─────────────────────────────────────────────────────────────────
# set_seed
# ─────────────────────────────────────────────────────────────────

class TestSetSeed:
    def test_reproducible_torch(self):
        set_seed(42)
        a = torch.randn(3, 3)
        set_seed(42)
        b = torch.randn(3, 3)
        assert torch.equal(a, b)

    def test_reproducible_random(self):
        set_seed(7)
        a = [random.random() for _ in range(5)]
        set_seed(7)
        b = [random.random() for _ in range(5)]
        assert a == b

    def test_reproducible_numpy(self):
        np = pytest.importorskip("numpy")
        set_seed(123)
        a = np.random.rand(3)
        set_seed(123)
        b = np.random.rand(3)
        assert np.array_equal(a, b)

    def test_different_seed_diverges(self):
        set_seed(1)
        a = torch.randn(5)
        set_seed(2)
        b = torch.randn(5)
        assert not torch.equal(a, b)


# ─────────────────────────────────────────────────────────────────
# move_batch_to_device
# ─────────────────────────────────────────────────────────────────

class TestMoveBatchToDevice:
    def test_tensor(self):
        t = torch.randn(2, 2)
        out = move_batch_to_device(t, torch.device("cpu"))
        assert isinstance(out, torch.Tensor)
        assert out.device.type == "cpu"

    def test_dict(self):
        batch = {"input": torch.randn(2), "label": torch.tensor([1, 2])}
        out = move_batch_to_device(batch, torch.device("cpu"))
        assert set(out.keys()) == {"input", "label"}
        assert torch.equal(out["input"], batch["input"])

    def test_tuple_preserves_type(self):
        batch = (torch.randn(2), torch.tensor([1.0, 2.0]))
        out = move_batch_to_device(batch, torch.device("cpu"))
        assert isinstance(out, tuple)
        assert len(out) == 2

    def test_list_preserves_type(self):
        batch = [torch.randn(2), torch.tensor([1.0, 2.0])]
        out = move_batch_to_device(batch, torch.device("cpu"))
        assert isinstance(out, list)
        assert len(out) == 2

    def test_nested(self):
        batch = {"x": [torch.randn(2), {"y": torch.tensor([1, 2])}]}
        out = move_batch_to_device(batch, torch.device("cpu"))
        assert isinstance(out, dict)
        assert isinstance(out["x"], list)
        assert isinstance(out["x"][1], dict)
        assert torch.equal(out["x"][1]["y"], batch["x"][1]["y"])

    def test_scalar_passthrough(self):
        assert move_batch_to_device(42, torch.device("cpu")) == 42
        assert move_batch_to_device("hello", torch.device("cpu")) == "hello"
        assert move_batch_to_device(None, torch.device("cpu")) is None

    def test_idempotent(self):
        t = torch.randn(2)
        once = move_batch_to_device(t, torch.device("cpu"))
        twice = move_batch_to_device(once, torch.device("cpu"))
        assert torch.equal(twice, t)


# ─────────────────────────────────────────────────────────────────
# wrap_forward_with_device
# ─────────────────────────────────────────────────────────────────

class TestWrapForwardWithDevice:
    def test_none_returns_none(self):
        assert wrap_forward_with_device(None, torch.device("cpu")) is None

    def test_wrapped_calls_raw_with_moved_batch(self):
        seen_devices = []
        seen_modules = []

        def raw_fn(module, batch):
            seen_devices.append(batch.device.type)
            seen_modules.append(module)
            return batch.sum()

        wrapped = wrap_forward_with_device(raw_fn, torch.device("cpu"))
        result = wrapped("fake_module", torch.randn(3))
        assert result is not None
        assert seen_devices == ["cpu"]
        # 透传：raw_fn 收到的 module 就是 caller 传入的（transparent 转发契约）
        assert seen_modules == ["fake_module"]

    def test_wrapped_handles_dict_batch(self):
        def raw_fn(module, batch):
            return batch["x"].sum() + batch["y"].sum()

        wrapped = wrap_forward_with_device(raw_fn, torch.device("cpu"))
        batch = {"x": torch.randn(3), "y": torch.randn(3)}
        result = wrapped(None, batch)
        expected = batch["x"].sum() + batch["y"].sum()
        assert torch.allclose(result, expected)


# ─────────────────────────────────────────────────────────────────
# resolve_device — gap tests (review SHOULD FIX)
# ─────────────────────────────────────────────────────────────────

class TestResolveDeviceGaps:
    def test_none_arg_is_auto(self):
        """None（调用方没传）→ auto 探测，绝不 raise。"""
        d = resolve_device(None)
        assert d.type in ("cuda", "npu", "cpu")

    def test_default_seed_zero_reproducible(self):
        """set_seed(0) 是 input 默认值；两次调用 torch RNG 必须等价。"""
        set_seed(0)
        a = torch.randn(4)
        set_seed(0)
        b = torch.randn(4)
        assert torch.equal(a, b)


# ─────────────────────────────────────────────────────────────────
# _compute_bake_metric_relative_diff —— 纯 math（plan §P5 N7 fail-loud 防线）
# ─────────────────────────────────────────────────────────────────
# 直接从 run_bit_curve.py 按 importlib 加载（脚本顶层 try-import ts_quant 失败时
# 不 raise，仅打 _TS_QUANT_OK=False，可安全 import 整个模块）。

def _load_run_bit_curve_module():
    """按文件路径 import run_bit_curve.py 模块（ts_quant 缺也能 import）。"""
    script = (
        Path(__file__).resolve().parents[2]
        / "workflows" / "agents" / "bit-curve-searcher" / "scripts" / "run_bit_curve.py"
    )
    import importlib.util
    spec = importlib.util.spec_from_file_location("_run_bit_curve_under_test", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rbc_module():
    return _load_run_bit_curve_module()


class TestComputeBakeMetricRelativeDiff:
    """验证 _compute_bake_metric_relative_diff 的 6 条分支（review MUST FIX）。"""

    def test_both_none_returns_none(self, rbc_module):
        assert rbc_module._compute_bake_metric_relative_diff(None, None) is None

    def test_reeval_none_returns_none(self, rbc_module):
        assert rbc_module._compute_bake_metric_relative_diff(None, 1.0) is None

    def test_final_none_returns_none(self, rbc_module):
        assert rbc_module._compute_bake_metric_relative_diff(1.0, None) is None

    def test_within_tol_returns_small_value(self, rbc_module):
        # 1.0000001 vs 1.0 → rel_diff = 1e-7（< 1e-4 tol）
        rel = rbc_module._compute_bake_metric_relative_diff(1.0000001, 1.0)
        assert rel is not None
        assert rel < rbc_module._BAKE_METRIC_REL_TOL

    def test_exceeds_tol_returns_large_value(self, rbc_module):
        # 1.001 vs 1.0 → rel_diff = 1e-3（> 1e-4 tol）
        rel = rbc_module._compute_bake_metric_relative_diff(1.001, 1.0)
        assert rel is not None
        assert rel > rbc_module._BAKE_METRIC_REL_TOL

    def test_final_zero_uses_abs_floor(self, rbc_module):
        """final=0 时走 abs_floor 兜底（防除零），rel_diff = 1e-6/1e-12 = 1e6 → fail loud。"""
        rel = rbc_module._compute_bake_metric_relative_diff(1e-6, 0.0)
        assert rel is not None
        assert rel > rbc_module._BAKE_METRIC_REL_TOL
        # 兜底确实生效：没除零，结果是个大数
        assert rel == pytest.approx(1e6, rel=1e-3)

    def test_bad_types_return_none(self, rbc_module):
        """非数值类型（str/None/dict）→ 解析失败 → None（caller WARN 跳过，不阻断）。"""
        assert rbc_module._compute_bake_metric_relative_diff("abc", 1.0) is None
        assert rbc_module._compute_bake_metric_relative_diff(1.0, "xyz") is None

    def test_negative_metrics_handled(self, rbc_module):
        """metric 可能为负（如 normalized mse），abs() 正确处理。"""
        rel = rbc_module._compute_bake_metric_relative_diff(-1.0, -1.0001)
        assert rel is not None
        # |(-1.0) - (-1.0001)| = 0.0001; max(|-1.0001|, floor) = 1.0001
        assert rel == pytest.approx(0.0001 / 1.0001, rel=1e-6)


class TestCheckBakeMetricConsistencySideEffects:
    """_check_bake_metric_consistency 的副作用层：stderr + exit code。

    验证 plan §P5 N7 三条契约：
      - 任一 None → WARN 跳过（不 exit）
      - 超 tol → exit 3（fail loud）
      - tol 内 → 不 exit
    """

    def test_within_tol_no_exit(self, rbc_module, capsys):
        rbc_module._check_bake_metric_consistency(1.00000001, 1.0, "mse")
        captured = capsys.readouterr()
        assert "FAIL LOUD" not in captured.err

    def test_exceeds_tol_exits_3(self, rbc_module, capsys):
        with pytest.raises(SystemExit) as excinfo:
            rbc_module._check_bake_metric_consistency(1.001, 1.0, "mse")
        assert excinfo.value.code == 3
        captured = capsys.readouterr()
        assert "FAIL LOUD" in captured.err
        # 格式化 intent：值用 :.8f 显示（8 位小数），不是裸 repr
        assert "1.00100000" in captured.err
        assert "1.00000000" in captured.err

    def test_both_none_skips_with_warn(self, rbc_module, capsys):
        rbc_module._check_bake_metric_consistency(None, None, "mse")
        captured = capsys.readouterr()
        assert "对账跳过" in captured.err
        assert "FAIL LOUD" not in captured.err

    def test_one_none_skips_with_warn(self, rbc_module, capsys):
        rbc_module._check_bake_metric_consistency(0.5, None, "mse")
        captured = capsys.readouterr()
        assert "对账跳过" in captured.err

    def test_final_zero_triggers_abs_floor_fail_loud(self, rbc_module, capsys):
        """final=0 + 任何非零 reeval → abs_floor 兜底后 rel_diff 巨大 → exit 3。"""
        with pytest.raises(SystemExit) as excinfo:
            rbc_module._check_bake_metric_consistency(0.001, 0.0, "mse")
        assert excinfo.value.code == 3

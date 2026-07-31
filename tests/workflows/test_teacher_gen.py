"""test_teacher_gen.py —— teacher-gen agent 关键不变量测试。

覆盖：

- ``validate_teacher.py`` PASS / FAIL 路径（确定性硬校验，fail loud）
  - DUMMY_INPUT 逐字一致 / 深度轴 ×3 / 宽度轴 ×2 / 其余 KNOBS 不变 / 容量上升
- ``measure_latency.py`` 副本与 model-flatten 原版字节对齐（防漂移）
- teacher-gen/agent.md 结构契约（output_schema、SKILL.md / scripts 引用、强执行指令）
- teacher-gen/SKILL.md 4-step 派生工作流 + verifier prompt 框架
- E2E：用真实 baseline（``examples/kd-nas-demo/baseline_model.py``）派生 teacher wrapper，
  跑双重硬校验 + teacher ``__main__``，验 CORRECTNESS + LATENCY_MS（统一契约）
- wrapper 模式：teacher 经 ``importlib.util.spec_from_file_location`` 加载 baseline，build_model
  委托不拷贝架构代码

不依赖 GPU/真硬件（CPU Identity + Conv2d 即可）；latency 集成测试用 onnxruntime，逐测试 skipif gate。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEACHER_GEN_DIR = REPO / "workflows" / "agents" / "teacher-gen"
FLATTEN_DIR = REPO / "workflows" / "agents" / "model-flatten"
VALIDATE_TEACHER = TEACHER_GEN_DIR / "scripts" / "validate_teacher.py"
VALIDATE_CONTRACT = FLATTEN_DIR / "scripts" / "validate_contract.py"
MEASURE_COPY = TEACHER_GEN_DIR / "scripts" / "measure_latency.py"
MEASURE_FLATTEN = FLATTEN_DIR / "scripts" / "measure_latency.py"
DEMO_BASELINE = REPO / "examples" / "kd-nas-demo" / "baseline_model.py"
DEMO_PROVIDER = REPO / "examples" / "kd-nas-demo" / "latency_provider.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# onnxruntime / numpy 是 latency 测量的硬依赖；逐测试 skipif gate（不在模块顶层 importorskip）。
_ORT_OK = (
    importlib.util.find_spec("onnxruntime") is not None
    and importlib.util.find_spec("numpy") is not None
)
needs_ort = pytest.mark.skipif(
    not _ORT_OK, reason="onnxruntime+numpy required for latency measurement（本测试 skip 不伪造）"
)


# ── 契约 fixture 写入 helpers（参数化 baseline + teacher wrapper）──────────────────


def _write_parametric_baseline(
    p: Path,
    *,
    num_blocks_default: int = 2,
    embed_dim_default: int = 8,
    num_blocks_min: int = 1,
    embed_dim_min: int = 4,
) -> None:
    """写一个参数化 baseline 契约（input proj + n 个 Conv2d block + output proj）。

    模型有参数（n * c^2 量级），所以 teacher 调大 n/c 后容量严格上升。
    DUMMY_INPUT=[1,8,4,4] 固定（input proj 把 in_ch=8 映射到内部 c）。
    """
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1, 8, 4, 4], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        f"KNOBS = {{'num_blocks': {{'default': {num_blocks_default}, 'min': {num_blocks_min}, 'step': -1, 'leverage': 'high'}},\n"
        f"         'embed_dim': {{'default': {embed_dim_default}, 'min': {embed_dim_min}, 'step': -2, 'leverage': 'medium'}}}}\n"
        "def build_model(**cfg):\n"
        "    n = int(cfg.get('num_blocks', KNOBS['num_blocks']['default']))\n"
        "    c = int(cfg.get('embed_dim', KNOBS['embed_dim']['default']))\n"
        "    return nn.Sequential(\n"
        "        nn.Conv2d(8, c, 1),\n"
        "        *[nn.Conv2d(c, c, 1) for _ in range(n)],\n"
        "        nn.Conv2d(c, 8, 1),\n"
        "    )\n",
        encoding="utf-8",
    )


_TEACHER_TEMPLATE = """\
\"\"\"teacher wrapper — derived from {baseline_name} by pure parametric derivation.\"\"\"

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any

_BASELINE_CONTRACT_PATH = {baseline_path!r}


def _load_baseline_module() -> Any:
    p = os.path.abspath(_BASELINE_CONTRACT_PATH)
    if not os.path.isfile(p):
        raise FileNotFoundError(p)
    parent = os.path.dirname(p)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec = importlib.util.spec_from_file_location(
        f"_teacher_baseline_{{os.path.basename(p).replace('.', '_')}}", p
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_baseline = _load_baseline_module()
_baseline_build_model = _baseline.build_model

DUMMY_INPUT = {dummy_input!r}
BUILD_FN = "build_model"

DEPTH_AXIS = {depth_axis!r}
WIDTH_AXIS = {width_axis!r}

KNOBS = {knobs!r}


def build_model(**cfg):
    return _baseline_build_model(**cfg)


if __name__ == "__main__":
    import argparse
    import torch

    _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    _defaults = {{k: v["default"] for k, v in KNOBS.items()}}
    _model = build_model(**_defaults).to(_device).eval()
    _shape = list(DUMMY_INPUT["shape"])
    _dtype = getattr(torch, DUMMY_INPUT.get("dtype", "float32"))
    _dummy = torch.randn(*_shape, dtype=_dtype, device=_device)
    with torch.no_grad():
        _out = _model(_dummy)
    print(f"CORRECTNESS: OK | input={{_shape}} output={{list(_out.shape)}}")

    _ap = argparse.ArgumentParser(add_help=False)
    _ap.add_argument("--latency_provider", default={latency_provider!r})
    _ap.add_argument("--device", default="auto")
    _ap.add_argument("--seed", type=int, default=0)
    _ap.add_argument("--repeats", type=int, default=3)
    _ap.add_argument("--opset", type=int, default=17)
    _args, _ = _ap.parse_known_args()

    _resources = os.environ.get("ORCA_AGENT_RESOURCES", "")
    _helper = os.path.join(_resources, "scripts", "measure_latency.py") if _resources else ""
    if _helper and os.path.isfile(_helper):
        sys.path.insert(0, os.path.dirname(_helper))
        from measure_latency import measure_contract_latency
        _r = measure_contract_latency(
            contract_path=__file__,
            latency_provider=_args.latency_provider,
            device=_args.device, seed=_args.seed, opset=_args.opset, repeats=_args.repeats,
        )
        print(f"LATENCY_MS: {{_r['latency_ms_median']:.6f}}")
        print(f"LATENCY_SOURCE: {{_r['source']}}")
        print(f"LATENCY_CONFIDENCE: {{_r['confidence']}}")
    else:
        print("LATENCY_SKIPPED: helper 未找到")
"""


def _write_teacher_wrapper(
    p: Path,
    baseline_path: Path,
    *,
    depth_axis: str = "num_blocks",
    width_axis: str = "embed_dim",
    knobs: dict | None = None,
    dummy_input: dict | None = None,
    latency_provider: str = "",
) -> None:
    """按 SKILL.md 模板写 teacher wrapper 文件。"""
    if knobs is None:
        # 默认：baseline num_blocks=2/embed_dim=8 → teacher num_blocks=6/embed_dim=16
        knobs = {
            "num_blocks": {"default": 6, "min": 1, "step": -1, "leverage": "high"},
            "embed_dim": {"default": 16, "min": 4, "step": -2, "leverage": "medium"},
        }
    if dummy_input is None:
        dummy_input = {"shape": [1, 8, 4, 4], "dtype": "float32"}
    p.write_text(
        _TEACHER_TEMPLATE.format(
            baseline_name=baseline_path.name,
            baseline_path=str(baseline_path),
            dummy_input=dummy_input,
            depth_axis=depth_axis,
            width_axis=width_axis,
            knobs=knobs,
            latency_provider=latency_provider,
        ),
        encoding="utf-8",
    )


# ── validate_teacher.py CLI：PASS 路径 ──────────────────────────────────────────


def test_validate_teacher_pass_default(tmp_path):
    """PASS：baseline + teacher（深度×3/宽度×2 + DUMMY_INPUT 一致 + 容量上升）→ exit 0。"""
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline)
    _write_teacher_wrapper(teacher, baseline)
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher),
         "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"
    assert "VALIDATION: PASS" in r.stdout
    assert "DEPTH_AXIS: num_blocks" in r.stdout
    assert "WIDTH_AXIS: embed_dim" in r.stdout
    assert "BASELINE_PARAMS:" in r.stdout
    assert "TEACHER_PARAMS:" in r.stdout
    assert "CAPACITY_RATIO:" in r.stdout
    # 容量比 > 1（teacher 比 baseline 大）
    ratio_line = next(l for l in r.stdout.splitlines() if l.startswith("CAPACITY_RATIO:"))
    ratio = float(ratio_line.split(":", 1)[1].strip())
    assert ratio > 1.0, f"teacher 容量应 > baseline（ratio={ratio}）"
    # FAIL_REASON 不应出现
    assert "FAIL_REASON" not in r.stdout


# ── validate_teacher.py CLI：FAIL 路径（fail loud，exit 2）──────────────────────


def test_validate_teacher_fail_missing_baseline(tmp_path):
    """FAIL：baseline 文件不存在 → exit 2 + FAIL_REASON。"""
    teacher = tmp_path / "teacher.py"
    _write_teacher_wrapper(teacher, tmp_path / "nope.py")  # baseline path will be embedded but file missing
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(tmp_path / "nope.py"),
         "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "VALIDATION: FAIL" in r.stdout
    assert "FAIL_REASON:" in r.stdout
    assert "baseline" in r.stdout.lower()


def test_validate_teacher_fail_missing_teacher(tmp_path):
    """FAIL：teacher 文件不存在 → exit 2 + FAIL_REASON。"""
    baseline = tmp_path / "baseline.py"
    _write_parametric_baseline(baseline)
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline),
         "--teacher", str(tmp_path / "nope.py"), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "VALIDATION: FAIL" in r.stdout
    assert "teacher" in r.stdout.lower() or "baseline" in r.stdout.lower()


def test_validate_teacher_fail_dummy_input_mismatch(tmp_path):
    """FAIL：teacher.DUMMY_INPUT != baseline.DUMMY_INPUT（KD 硬约束——shape 必须逐字一致）。"""
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline)
    # teacher DUMMY_INPUT shape 与 baseline 不一致（channels 改了——KD 不允许）
    _write_teacher_wrapper(
        teacher, baseline,
        dummy_input={"shape": [1, 16, 4, 4], "dtype": "float32"},  # 16 != baseline 的 8
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "DUMMY_INPUT" in r.stdout
    assert "逐字一致" in r.stdout or "!=" in r.stdout


def test_validate_teacher_fail_depth_not_scaled(tmp_path):
    """FAIL：深度轴 default < baseline.default × 3（×2 写错成 ×3，不配当 teacher）。"""
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline, num_blocks_default=4)  # baseline 4 → teacher 应 12
    # teacher num_blocks=8 (= 4*2) < 12 (= 4*3) → FAIL
    _write_teacher_wrapper(
        teacher, baseline,
        knobs={
            "num_blocks": {"default": 8, "min": 1, "step": -1, "leverage": "high"},
            "embed_dim": {"default": 16, "min": 4, "step": -2, "leverage": "medium"},
        },
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "深度轴" in r.stdout
    assert "12" in r.stdout  # baseline.default × 3 = 12


def test_validate_teacher_fail_width_not_scaled(tmp_path):
    """FAIL：宽度轴 default < baseline.default × 2。"""
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline, embed_dim_default=8)  # baseline 8 → teacher 应 16
    # teacher embed_dim=10 (< 16) → FAIL
    _write_teacher_wrapper(
        teacher, baseline,
        knobs={
            "num_blocks": {"default": 6, "min": 1, "step": -1, "leverage": "high"},
            "embed_dim": {"default": 10, "min": 4, "step": -2, "leverage": "medium"},
        },
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "宽度轴" in r.stdout
    assert "16" in r.stdout  # baseline.default × 2 = 16


def test_validate_teacher_fail_other_knob_default_changed(tmp_path):
    """FAIL：非轴 knob 的 default 被改（违反纯调参派生——只动深度/宽度轴）。"""
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline, num_blocks_default=2, embed_dim_default=8)
    # 加第三个 knob num_heads（非轴），teacher 改了它的 default
    baseline_fixed = tmp_path / "baseline.py"
    baseline_fixed.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1, 8, 4, 4], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'num_blocks': {'default': 2, 'min': 1, 'step': -1, 'leverage': 'high'},\n"
        "         'embed_dim': {'default': 8, 'min': 4, 'step': -2, 'leverage': 'medium'},\n"
        "         'num_heads': {'default': 4, 'min': 1, 'step': -1, 'leverage': 'low'}}\n"
        "def build_model(**cfg):\n"
        "    n = int(cfg.get('num_blocks', 2))\n"
        "    c = int(cfg.get('embed_dim', 8))\n"
        "    return nn.Sequential(\n"
        "        nn.Conv2d(8, c, 1),\n"
        "        *[nn.Conv2d(c, c, 1) for _ in range(n)],\n"
        "        nn.Conv2d(c, 8, 1),\n"
        "    )\n",  # num_heads 不影响 forward（占位 knob）
        encoding="utf-8",
    )
    _write_teacher_wrapper(
        teacher, baseline_fixed,
        knobs={
            "num_blocks": {"default": 6, "min": 1, "step": -1, "leverage": "high"},
            "embed_dim": {"default": 16, "min": 4, "step": -2, "leverage": "medium"},
            # num_heads 非轴，应保持 baseline default=4；这里故意改成 8 → FAIL
            "num_heads": {"default": 8, "min": 1, "step": -1, "leverage": "low"},
        },
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline_fixed), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "num_heads" in r.stdout
    assert "非轴" in r.stdout or "被改" in r.stdout


def test_validate_teacher_fail_axis_min_changed(tmp_path):
    """FAIL：轴 knob 的 min 被改（teacher 只调 default，min/step/leverage 须继承 baseline）。"""
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline, num_blocks_default=2, num_blocks_min=1)
    # teacher num_blocks.min=2（应继承 baseline 的 1）→ FAIL
    _write_teacher_wrapper(
        teacher, baseline,
        knobs={
            "num_blocks": {"default": 6, "min": 2, "step": -1, "leverage": "high"},  # min 改了
            "embed_dim": {"default": 16, "min": 4, "step": -2, "leverage": "medium"},
        },
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "min" in r.stdout
    assert "继承 baseline" in r.stdout or "被改" in r.stdout


def test_validate_teacher_fail_axis_not_in_knobs(tmp_path):
    """FAIL：DEPTH_AXIS 引用了不存在的 knob 名。"""
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline)
    _write_teacher_wrapper(
        teacher, baseline,
        depth_axis="nonexistent_knob",  # 不在 KNOBS 里
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "DEPTH_AXIS" in r.stdout
    assert "nonexistent_knob" in r.stdout


def test_validate_teacher_fail_axis_collision(tmp_path):
    """FAIL：DEPTH_AXIS == WIDTH_AXIS（同一 knob 不能既是深度又是宽度）。"""
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline)
    _write_teacher_wrapper(
        teacher, baseline,
        depth_axis="num_blocks",
        width_axis="num_blocks",  # collision
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "DEPTH_AXIS == WIDTH_AXIS" in r.stdout or "不能是同一个 knob" in r.stdout


def test_validate_teacher_fail_capacity_not_greater(tmp_path):
    """FAIL：teacher 容量未上升（wrapper bug 或退化模型——params 不增 → 不配当 teacher）。

    构造退化 baseline：build_model 返回 nn.Identity()（0 params，与 cfg 无关）。
    teacher 也返回 Identity（0 params）。容量未严格上升 → FAIL。
    """
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    baseline.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1, 8, 4, 4], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'num_blocks': {'default': 2, 'min': 1, 'step': -1, 'leverage': 'high'},\n"
        "         'embed_dim': {'default': 8, 'min': 4, 'step': -2, 'leverage': 'medium'}}\n"
        "def build_model(**cfg):\n"
        "    return nn.Identity()\n",  # 退化：0 params
        encoding="utf-8",
    )
    _write_teacher_wrapper(teacher, baseline)  # wrapper 也委托到 Identity
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "容量" in r.stdout
    assert "上升" in r.stdout


def test_validate_teacher_fail_knobs_keyset_mismatch(tmp_path):
    """FAIL：teacher.KNOBS 键集合 != baseline（多/少 knob = 改契约 schema）。"""
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline)
    # teacher 缺 embed_dim（少一个 knob）
    _write_teacher_wrapper(
        teacher, baseline,
        knobs={
            "num_blocks": {"default": 6, "min": 1, "step": -1, "leverage": "high"},
            # 缺 embed_dim
        },
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    # 收紧断言：精确匹配「键集合」reason 串（reviewer 指出 OR 分支过宽会误判 stderr 噪音通过）
    assert "键集合" in r.stdout
    assert "embed_dim" in r.stdout  # missing knob name in reason


# ── validate_teacher.py：BLOCKER 覆盖——wrapper 委托失败（L215-218 + 容量退化）────


def test_validate_teacher_fail_wrapper_instantiation_broken(tmp_path):
    """FAIL: teacher.build_model(**defaults) 实例化抛异常（wrapper 委托链断裂）。

    构造 teacher：build_model body 调用不存在的 ``_baseline_build``（typo，漏 ``_model`` 后缀）
    → NameError。validate_teacher.py L215-218 捕获，FAIL_REASON 含「wrapper 是否正确委托」
    （LLM 修 teacher 时靠这条诊断定位委托链问题）。
    """
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline)
    _write_teacher_wrapper(teacher, baseline)
    # 覆盖 build_model body：引入 typo（_baseline_build 而非 _baseline_build_model）
    src = teacher.read_text(encoding="utf-8")
    src = src.replace(
        "    return _baseline_build_model(**cfg)\n",
        "    return _baseline_build(**cfg)  # typo: 漏 _model 后缀\n",
    )
    teacher.write_text(src, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    # 关键诊断：reason 含「wrapper 是否正确委托」（架构级失败模式的归因）
    assert "wrapper" in r.stdout.lower() or "委托" in r.stdout
    assert "NameError" in r.stdout or "实例化失败" in r.stdout


def test_validate_teacher_fail_wrapper_degenerates_to_identity(tmp_path):
    """FAIL: teacher.build_model 退化为非委托（如返回 nn.Identity）→ 容量未上升。

    构造 teacher：build_model body 返回 ``nn.Identity()``（不调 ``_baseline_build_model``）。
    teacher 实例 0 params，baseline > 0 → 容量未严格上升 → FAIL（wrapper bug 防护触发）。
    """
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline)
    _write_teacher_wrapper(teacher, baseline)
    # 覆盖 build_model body：退化为 Identity（不委托）
    src = teacher.read_text(encoding="utf-8")
    src = src.replace(
        "    return _baseline_build_model(**cfg)\n",
        "    import torch.nn as _nn; return _nn.Identity()  # wrapper 退化\n",
    )
    teacher.write_text(src, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "容量" in r.stdout
    assert "上升" in r.stdout


# ── validate_teacher.py：MAJOR 覆盖——空轴 / 非数值 default / WIDTH_AXIS 错名 ─────


def test_validate_teacher_pass_with_empty_depth_axis(tmp_path):
    """PASS: DEPTH_AXIS='' （baseline 无深度模式）→ 跳过深度放大，只放大宽度。

    SKILL.md Step 2 明示的 low-confidence 路径：baseline KNOBS 名字均不匹配深度轴模式时，
    teacher 写 ``DEPTH_AXIS=""``。validate_teacher 须容忍（``if depth_axis and ...`` 短路），
    仅校验宽度 ×2 + 容量上升。
    """
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    # baseline 只有 embed_dim 一个 knob（无深度轴）
    baseline.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1, 8, 4, 4], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'embed_dim': {'default': 8, 'min': 4, 'step': -2, 'leverage': 'medium'}}\n"
        "def build_model(**cfg):\n"
        "    c = int(cfg.get('embed_dim', 8))\n"
        "    return nn.Sequential(nn.Conv2d(8, c, 1), nn.Conv2d(c, c, 1), nn.Conv2d(c, 8, 1))\n",
        encoding="utf-8",
    )
    _write_teacher_wrapper(
        teacher, baseline,
        depth_axis="",  # 空深度轴（baseline 无深度模式）
        width_axis="embed_dim",
        knobs={
            "embed_dim": {"default": 16, "min": 4, "step": -2, "leverage": "medium"},  # ×2
        },
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"
    assert "VALIDATION: PASS" in r.stdout
    assert "DEPTH_AXIS:" in r.stdout  # 空串也 emit（可审计）
    assert "WIDTH_AXIS: embed_dim" in r.stdout


def test_validate_teacher_fail_string_default_in_knob(tmp_path):
    """FAIL: teacher.KNOBS[k].default 是字符串（LLM 高频错：写 ``"6"`` 而非 ``6``）→ FAIL。

    validate_teacher.py L171-172 显式排除 bool + 非数值；本测试守护该分支。
    """
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline)
    _write_teacher_wrapper(
        teacher, baseline,
        knobs={
            "num_blocks": {"default": "6", "min": 1, "step": -1, "leverage": "high"},  # str
            "embed_dim": {"default": 16, "min": 4, "step": -2, "leverage": "medium"},
        },
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "default" in r.stdout
    assert "数值" in r.stdout


def test_validate_teacher_fail_width_axis_not_in_knobs(tmp_path):
    """FAIL: WIDTH_AXIS 引用不存在的 knob 名（独立 if 分支，与 DEPTH_AXIS 同款但独立）。"""
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline)
    _write_teacher_wrapper(
        teacher, baseline,
        width_axis="nonexistent_width_knob",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "WIDTH_AXIS" in r.stdout
    assert "nonexistent_width_knob" in r.stdout


@pytest.mark.parametrize("field,bad_value", [
    ("min", 2),                 # baseline min=1 → teacher min=2（drift）
    ("step", -2),               # baseline step=-1 → teacher step=-2（drift）
    ("leverage", "low"),        # baseline leverage=high → teacher leverage=low（drift）
])
def test_validate_teacher_fail_axis_inherited_field_changed(tmp_path, field, bad_value):
    """FAIL: 轴 knob 的 min / step / leverage 任一字段被改（teacher 只调 default）。

    参数化覆盖 ``validate_teacher.py`` L190-192 循环的三个字段（min/step/leverage）——
    之前只测 min；step / leverage 同代码分支但语义独立，参数化保证三者都被守护。
    """
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline, num_blocks_default=2, num_blocks_min=1)
    # 构造 teacher KNOBS：num_blocks 是 DEPTH_AXIS，default 调大，但指定 field 改值
    knob = {"default": 6, "min": 1, "step": -1, "leverage": "high"}
    knob[field] = bad_value  # 改指定字段
    _write_teacher_wrapper(
        teacher, baseline,
        knobs={
            "num_blocks": knob,
            "embed_dim": {"default": 16, "min": 4, "step": -2, "leverage": "medium"},
        },
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(baseline), "--teacher", str(teacher), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert field in r.stdout
    assert "继承 baseline" in r.stdout or "被改" in r.stdout


# ── validate_teacher.py：纯函数单元（in-process，无 subprocess）────────────────


def test_validate_teacher_function_returns_ok_dict(tmp_path):
    """``validate_teacher(...)`` 函数返回 dict（ok=True + 字段齐全），不打印。"""
    vt = _load(VALIDATE_TEACHER, "_vt_fn_ok")
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline)
    _write_teacher_wrapper(teacher, baseline)
    result = vt.validate_teacher(
        str(baseline), str(teacher), device_arg="cpu", seed=0
    )
    assert result["ok"] is True
    assert result["reason"] == ""
    assert result["depth_axis"] == "num_blocks"
    assert result["width_axis"] == "embed_dim"
    assert result["baseline_params"] > 0
    assert result["teacher_params"] > result["baseline_params"]
    assert result["capacity_ratio"] > 1.0


def test_validate_teacher_function_returns_fail_dict(tmp_path):
    """``validate_teacher(...)`` 函数对 DUMMY_INPUT 不一致返回 ok=False + reason（不 raise）。"""
    vt = _load(VALIDATE_TEACHER, "_vt_fn_fail")
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline)
    _write_teacher_wrapper(
        teacher, baseline,
        dummy_input={"shape": [1, 16, 4, 4], "dtype": "float32"},
    )
    result = vt.validate_teacher(
        str(baseline), str(teacher), device_arg="cpu", seed=0
    )
    assert result["ok"] is False
    assert "DUMMY_INPUT" in result["reason"]


# ── measure_latency 副本与 model-flatten 字节对齐（防漂移，DRY）──────────────────


def test_measure_latency_copy_sync_with_flatten():
    """``teacher-gen/scripts/measure_latency.py`` 与 ``model-flatten/scripts/measure_latency.py``
    实现须字节对齐（除允许的 agent 标识差异：docstring 头部 + CLI description 串 + ``_emit``
    docstring + fallback WARN 内的 "flatten/teacher-gen agent" 字样）。

    同步由本测试守门——任何 flatten 侧的 measure_latency 改动须同步到 teacher-gen 副本。
    未来可抽到共享位置（见 release note Open Questions）。
    """
    flatten_src = MEASURE_FLATTEN.read_text(encoding="utf-8")
    copy_src = MEASURE_COPY.read_text(encoding="utf-8")
    # 抽出 import 区以下（``from __future__ import annotations`` 起）做对比
    flatten_body = flatten_src[flatten_src.index("from __future__ import annotations"):]
    copy_body = copy_src[copy_src.index("from __future__ import annotations"):]
    # agent 标识差异统一成占位符（4 处：CLI description + _emit docstring + fallback WARN + fallback 注释）
    flatten_norm = (
        flatten_body
        .replace(
            'description="model-flatten 契约默认 cfg latency 测量（fail loud；自包含）"',
            'description="<AGENT> 契约默认 cfg latency 测量（fail loud；自包含）"',
        )
        .replace("（flatten agent bash awk 解析）", "（<AGENT> agent bash awk 解析）")
        .replace("flatten agent 自身容错为通用性", "<AGENT> agent 自身容错为通用性")
    )
    copy_norm = (
        copy_body
        .replace(
            'description="teacher-gen 契约默认 cfg latency 测量（fail loud；自包含）"',
            'description="<AGENT> 契约默认 cfg latency 测量（fail loud；自包含）"',
        )
        .replace("（teacher-gen agent bash awk 解析）", "（<AGENT> agent bash awk 解析）")
        .replace("teacher-gen agent 自身容错为通用性", "<AGENT> agent 自身容错为通用性")
    )
    assert copy_norm == flatten_norm, (
        "teacher-gen/scripts/measure_latency.py 的实现 body 与 model-flatten 版本漂移——"
        "请同步两者（flatten 侧改动须传到 teacher-gen 副本）。"
    )


def test_measure_latency_copy_standalone_no_internal_imports():
    """``measure_latency.py`` 副本须自包含（与 flatten 同款），不 import _kd_scripts /
    nas_agent / _struct_scripts（teacher-gen 保 standalone，防漂移）。"""
    src = MEASURE_COPY.read_text(encoding="utf-8")
    for forbidden in ("from _kd_scripts", "import _kd_scripts",
                      "from nas_agent", "import nas_agent",
                      "from _struct_scripts", "import _struct_scripts"):
        assert forbidden not in src, (
            f"measure_latency.py 副本不应含内部跨包 import {forbidden!r}（teacher-gen standalone 契约）"
        )


# ── teacher-gen agent.md 结构契约 ─────────────────────────────────────────────


def test_teacher_gen_agent_md_has_strong_directive():
    """teacher-gen/agent.md 开头须有「唯一产出」+「严禁」红线（对齐 flatten 的 BUG-1 抗 spec-审查）。"""
    text = (TEACHER_GEN_DIR / "agent.md").read_text(encoding="utf-8")
    head = text[:2000]
    assert "唯一产出" in head, "teacher-gen/agent.md 开头缺「唯一产出」执行指令"
    assert "严禁" in head, "teacher-gen/agent.md 开头缺「严禁」红线"
    assert "❌" in head, "teacher-gen/agent.md 缺 ❌ 红线列表"
    assert "fail loud" in text.lower() or "失败" in text


def test_teacher_gen_agent_md_output_schema_before_bash():
    """output JSON schema 段须在第一个 bash 块之前（前置，对齐 flatten）。"""
    text = (TEACHER_GEN_DIR / "agent.md").read_text(encoding="utf-8")
    schema_offset = text.find("输出 JSON schema")
    bash_offset = text.find("```bash")
    assert schema_offset >= 0 and bash_offset >= 0
    assert schema_offset < bash_offset, (
        f"output JSON schema（offset={schema_offset}）应在第一个 bash 块（offset={bash_offset}）之前"
    )


def test_teacher_gen_agent_md_consumes_flatten_baseline_contract():
    """v4：teacher-gen 嵌入 workflow，从 flatten.output 取 baseline 契约路径
    （不再用 inputs.baseline_contract_path——那是独立阶段的写法）。"""
    text = (TEACHER_GEN_DIR / "agent.md").read_text(encoding="utf-8")
    assert "{{ flatten.output.baseline_contract_path }}" in text, (
        "teacher-gen/agent.md 应从 flatten.output.baseline_contract_path 取 baseline（v4 嵌入后）"
    )
    # 反向：不应再用 inputs.baseline_contract_path（独立阶段写法已退役）
    assert "{{ inputs.baseline_contract_path }}" not in text, (
        "teacher-gen/agent.md 不应再用 inputs.baseline_contract_path（v4 嵌入后改 flatten.output）"
    )


def test_teacher_gen_agent_md_refs_flatten_output():
    """v4：teacher-gen 嵌入 workflow，引用 flatten.output.*（上游节点）是预期行为
    （独立阶段「无上游引用」的旧断言已退役）。"""
    import re
    text = (TEACHER_GEN_DIR / "agent.md").read_text(encoding="utf-8")
    node_pat = re.compile(r"\{\{\s*flatten\.output\.")
    refs = node_pat.findall(text)
    assert refs, (
        "teacher-gen/agent.md 应引用 flatten.output.*（v4 嵌入 workflow，baseline 来自 flatten）"
    )


def test_teacher_gen_agent_md_has_two_validation_gates():
    """agent.md bash 块须跑两道硬校验：model-flatten validate_contract + teacher-gen validate_teacher。"""
    text = (TEACHER_GEN_DIR / "agent.md").read_text(encoding="utf-8")
    # 1. model-flatten validate_contract.py（跨 agent 复用）
    assert "model-flatten/scripts/validate_contract.py" in text, (
        "agent.md 须复用 model-flatten/scripts/validate_contract.py（KD 变体契约通用校验）"
    )
    # 2. teacher-gen validate_teacher.py（专属）
    assert "$ORCA_AGENT_RESOURCES/scripts/validate_teacher.py" in text, (
        "agent.md 须跑 teacher-gen 自己的 validate_teacher.py（teacher 专属断言）"
    )
    # 3. teacher __main__ 测 latency（统一契约）
    assert 'python3 "$CONTRACT"' in text
    assert "LATENCY_MS" in text
    # 4. output schema 含 depth_axis / width_axis（可审计）
    assert "depth_axis" in text and "width_axis" in text


def test_teacher_gen_agent_md_mentions_wrapper_semantics():
    """agent.md 须明确 teacher 是 wrapper（委托 baseline.build_model，不拷贝架构代码）。"""
    text = (TEACHER_GEN_DIR / "agent.md").read_text(encoding="utf-8")
    assert "wrapper" in text.lower()
    assert "build_model" in text
    assert "委托" in text or "delegate" in text.lower()


# ── teacher-gen SKILL.md 结构契约 ─────────────────────────────────────────────


def test_teacher_gen_skill_md_has_4_step_workflow():
    """SKILL.md 须有 4 个 step（读 baseline → 识别轴 → 写 teacher → 双重校验）。"""
    text = (TEACHER_GEN_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Step 1" in text and "Read Baseline Contract" in text
    assert "Step 2" in text and "Identify Depth and Width Axes" in text
    assert "Step 3" in text and "Write the Teacher File" in text
    assert "Step 4" in text and "Hard Validation" in text


def test_teacher_gen_skill_md_has_axis_pattern_guidance():
    """SKILL.md Step 2 须给出深度/宽度轴的名字模式表（LLM 判断依据，对齐 task spec）。"""
    text = (TEACHER_GEN_DIR / "SKILL.md").read_text(encoding="utf-8")
    # 深度轴模式关键词
    for kw in ("block", "layer", "stage", "depth", "num_layers"):
        assert kw in text, f"SKILL.md 缺深度轴模式关键词 {kw!r}"
    # 宽度轴模式关键词
    for kw in ("channel", "embed_dim", "hidden", "width", "feature"):
        assert kw in text, f"SKILL.md 缺宽度轴模式关键词 {kw!r}"


def test_teacher_gen_skill_md_has_verifier_prompt_scaffold():
    """SKILL.md Step 4b 须有 teacher-gen-verifier 子 agent prompt 框架。"""
    text = (TEACHER_GEN_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "teacher-gen-verifier" in text
    assert "BLOCKER" in text and "MAJOR" in text and "MINOR" in text
    # 三维校验：axis identification / wrapper purity / latency wiring
    assert "Axis identification" in text or "axis" in text.lower()
    assert "Wrapper purity" in text or "wrapper" in text.lower()
    assert "Latency" in text and "__main__" in text


def test_teacher_gen_skill_md_has_main_template_copied_from_flatten():
    """SKILL.md Step 3 的 teacher 文件模板须含 ``__main__`` latency 块（measure_contract_latency）——
    逐字照 model-flatten/SKILL.md Step 3 模板。"""
    text = (TEACHER_GEN_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "measure_contract_latency" in text
    assert "LATENCY_MS" in text
    assert "LATENCY_SKIPPED" in text
    assert "CORRECTNESS: OK" in text
    # 占位符说明（rendered value，非 Jinja 串）
    assert "<FILL" in text or "inputs.latency_provider" in text


def test_teacher_gen_skill_md_no_hardcoded_specific_architecture():
    """SKILL.md 的 Step 3 teacher 文件**模板代码块**里**不应写死** SignalTransformer /
    model8 等具体架构名——teacher-gen 必须模型无关。

    SKILL.md 正文把这些名字作为「禁止写死」的红线示例是允许的（教学意图）；但模板代码块
    （LLM 照填的 python 模板）里绝不能把它们当具体架构引用。
    """
    import re
    text = (TEACHER_GEN_DIR / "SKILL.md").read_text(encoding="utf-8")
    step3_idx = text.find("### Step 3")
    assert step3_idx >= 0, "SKILL.md 缺 Step 3"
    step3_block = text[step3_idx:text.find("### Step 4", step3_idx)]
    # 抽出 Step 3 里所有 ```python 代码块
    code_blocks = re.findall(r"```python\n(.*?)\n```", step3_block, re.DOTALL)
    assert code_blocks, "Step 3 应至少有一个 python 代码块（teacher 文件模板）"
    for cb in code_blocks:
        for forbidden_arch in ("SignalTransformer", "SignalProcessingTransformer"):
            assert forbidden_arch not in cb, (
                f"SKILL.md Step 3 模板代码块不应写死具体架构名 {forbidden_arch!r}（teacher-gen 须模型无关）"
            )


# ── validate_teacher.py standalone 守门（不 import _kd_scripts / nas_agent）─────


def test_validate_teacher_standalone_no_internal_imports():
    """``validate_teacher.py`` 须自包含，不 import _kd_scripts / nas_agent / _struct_scripts
    （teacher-gen 保 standalone，与 flatten 同款）。"""
    src = VALIDATE_TEACHER.read_text(encoding="utf-8")
    for forbidden in ("from _kd_scripts", "import _kd_scripts",
                      "from nas_agent", "import nas_agent",
                      "from _struct_scripts", "import _struct_scripts"):
        assert forbidden not in src, (
            f"validate_teacher.py 不应含内部跨包 import {forbidden!r}（teacher-gen standalone 契约）"
        )


def test_validate_teacher_depth_width_factors():
    """validate_teacher.py 的 _DEPTH_FACTOR=3 / _WIDTH_FACTOR=2（task spec 字面常量）。"""
    vt = _load(VALIDATE_TEACHER, "_vt_factors")
    assert vt._DEPTH_FACTOR == 3
    assert vt._WIDTH_FACTOR == 2


# ── teacher-gen 自有 measure_latency.py 副本的 CLI 单点测试 ──────────────────────
# （不完全依赖 test_measure_latency_copy_sync_with_flatten 间接覆盖；若 sync test 被人为放宽，
# teacher-gen 侧仍有直接回归网——reviewer 指出的健壮性加固）


@needs_ort
def test_teacher_gen_measure_latency_cli_cpu_fallback(tmp_path):
    """``teacher-gen/scripts/measure_latency.py`` CLI 直接调：空 latency_provider →
    ONNXRT-CPU fallback + WARN + confidence=low + exit 0。

    不依赖 flatten 侧 CLI 测试或字节同步——teacher-gen 副本独立回归。
    """
    p = tmp_path / "ok_contract.py"
    _write_parametric_baseline(p)  # 任何合规 KD 变体契约都行（measure_latency 只读 DUMMY_INPUT/KNOBS）
    r = subprocess.run(
        [sys.executable, str(MEASURE_COPY), "--contract", str(p),
         "--device", "cpu", "--repeats", "2"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "LATENCY_MS:" in r.stdout
    assert "LATENCY_SOURCE: cpu-fallback" in r.stdout
    assert "LATENCY_CONFIDENCE: low" in r.stdout
    assert "fallback" in r.stderr.lower() or "WARN" in r.stderr


@needs_ort
def test_teacher_gen_measure_latency_cli_with_provider(tmp_path):
    """``teacher-gen/scripts/measure_latency.py`` CLI：给 demo provider → source=provider + confidence=high。"""
    p = tmp_path / "ok_contract.py"
    _write_parametric_baseline(p)
    r = subprocess.run(
        [sys.executable, str(MEASURE_COPY), "--contract", str(p),
         "--latency_provider", f"{DEMO_PROVIDER}::measure",
         "--device", "cpu", "--repeats", "2"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "LATENCY_MS:" in r.stdout
    assert "LATENCY_SOURCE: provider" in r.stdout
    assert "LATENCY_CONFIDENCE: high" in r.stdout
    line = next(l for l in r.stdout.splitlines() if l.startswith("LATENCY_MS:"))
    val = float(line.split(":", 1)[1].strip())
    assert val > 0.0


# ── E2E：用真实 baseline_model.py 派生 teacher wrapper，跑双重校验 + __main__ ────


def test_e2e_validate_contract_passes_on_real_baseline_teacher(tmp_path):
    """E2E：用 examples/kd-nas-demo/baseline_model.py 作 baseline，派生 teacher wrapper，
    跑 model-flatten validate_contract.py 必须 PASS（teacher 是合规 KD 变体契约）。

    验证 wrapper 委托不影响契约合规性（build_model + KNOBS schema + forward shape）。
    """
    if not DEMO_BASELINE.is_file():
        pytest.skip(f"demo baseline 不存在：{DEMO_BASELINE}")
    # baseline_model.py 的 KNOBS: num_blocks default=4, embed_dim default=12
    # teacher 应: num_blocks=12 (=4*3), embed_dim=24 (=12*2)
    teacher = tmp_path / "baseline_model_teacher.py"
    _write_teacher_wrapper(
        teacher, DEMO_BASELINE,
        depth_axis="num_blocks",
        width_axis="embed_dim",
        knobs={
            "num_blocks": {"default": 12, "min": 2, "step": -1, "leverage": "high"},
            "embed_dim": {"default": 24, "min": 4, "step": -2, "leverage": "medium"},
        },
        dummy_input={"shape": [1, 4, 48, 64, 1], "dtype": "float32"},  # baseline 的真实 I/O
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_CONTRACT),
         "--contract", str(teacher), "--device", "cpu", "--seed", "0"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"validate_contract FAIL:\nstdout={r.stdout}\nstderr={r.stderr}"
    assert "VALIDATION: PASS" in r.stdout
    assert "SHAPE_MATCH: true" in r.stdout


def test_e2e_validate_teacher_passes_on_real_baseline_teacher(tmp_path):
    """E2E：teacher-gen validate_teacher.py 对真实 baseline 派生的 teacher PASS。"""
    if not DEMO_BASELINE.is_file():
        pytest.skip(f"demo baseline 不存在：{DEMO_BASELINE}")
    teacher = tmp_path / "baseline_model_teacher.py"
    _write_teacher_wrapper(
        teacher, DEMO_BASELINE,
        depth_axis="num_blocks",
        width_axis="embed_dim",
        knobs={
            "num_blocks": {"default": 12, "min": 2, "step": -1, "leverage": "high"},
            "embed_dim": {"default": 24, "min": 4, "step": -2, "leverage": "medium"},
        },
        dummy_input={"shape": [1, 4, 48, 64, 1], "dtype": "float32"},
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE_TEACHER),
         "--baseline", str(DEMO_BASELINE), "--teacher", str(teacher),
         "--device", "cpu", "--seed", "0"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"validate_teacher FAIL:\nstdout={r.stdout}\nstderr={r.stderr}"
    assert "VALIDATION: PASS" in r.stdout
    assert "DEPTH_AXIS: num_blocks" in r.stdout
    assert "WIDTH_AXIS: embed_dim" in r.stdout
    # teacher 真实容量严格大于 baseline（reviewer 指出 ``> 1.0`` 过松：subtle wrapper bug
    # 如只放大 1.1x 仍会通过）。baseline num_blocks=4→12 (×3), embed_dim=12→24 (×2)：
    # Conv1d 主导参数 ~ O(embed_dim² × num_blocks)，理论比 ~ 3 × 2² = 12×；收紧到 > 5.0
    # 兼顾 ONNX export 中不可训练参数（LayerNorm 等）的稀释。
    ratio_line = next(l for l in r.stdout.splitlines() if l.startswith("CAPACITY_RATIO:"))
    ratio = float(ratio_line.split(":", 1)[1].strip())
    assert ratio > 5.0, f"teacher 容量应显著 > baseline（理论 ~12×；得到 ratio={ratio}，疑似 wrapper 委托不完整）"


@needs_ort
def test_e2e_teacher_main_runs_correctness_and_latency(tmp_path):
    """E2E：teacher 文件 __main__ 跑出 CORRECTNESS: OK + LATENCY_MS（统一契约 happy path）。

    用真实 baseline_model.py 派生 teacher，$ORCA_AGENT_RESOURCES=teacher-gen dir，
    latency_provider 默认值渲染为 demo provider。
    """
    if not DEMO_BASELINE.is_file():
        pytest.skip(f"demo baseline 不存在：{DEMO_BASELINE}")
    teacher = tmp_path / "baseline_model_teacher.py"
    _write_teacher_wrapper(
        teacher, DEMO_BASELINE,
        depth_axis="num_blocks",
        width_axis="embed_dim",
        knobs={
            "num_blocks": {"default": 12, "min": 2, "step": -1, "leverage": "high"},
            "embed_dim": {"default": 24, "min": 4, "step": -2, "leverage": "medium"},
        },
        dummy_input={"shape": [1, 4, 48, 64, 1], "dtype": "float32"},
        latency_provider=f"{DEMO_PROVIDER}::measure",  # 渲染后的 provider 串
    )
    env = dict(os.environ)
    env["ORCA_AGENT_RESOURCES"] = str(TEACHER_GEN_DIR)
    r = subprocess.run(
        [sys.executable, str(teacher), "--device", "cpu", "--repeats", "2"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"
    assert "CORRECTNESS: OK" in r.stdout
    assert "LATENCY_MS:" in r.stdout
    assert "LATENCY_SOURCE: provider" in r.stdout
    # latency > 0（真实测量）
    line = next(l for l in r.stdout.splitlines() if l.startswith("LATENCY_MS:"))
    val = float(line.split(":", 1)[1].strip())
    assert val > 0.0, f"latency 应 >0（真实测量），得到 {val}"


def test_e2e_teacher_main_latency_skipped_without_resources(tmp_path):
    """E2E：未注入 $ORCA_AGENT_RESOURCES → LATENCY_SKIPPED（不伪造）。

    correctness 仍 OK（teacher 文件 standalone：import 不依赖 helper）。
    """
    if not DEMO_BASELINE.is_file():
        pytest.skip(f"demo baseline 不存在：{DEMO_BASELINE}")
    teacher = tmp_path / "baseline_model_teacher.py"
    _write_teacher_wrapper(
        teacher, DEMO_BASELINE,
        depth_axis="num_blocks",
        width_axis="embed_dim",
        knobs={
            "num_blocks": {"default": 12, "min": 2, "step": -1, "leverage": "high"},
            "embed_dim": {"default": 24, "min": 4, "step": -2, "leverage": "medium"},
        },
        dummy_input={"shape": [1, 4, 48, 64, 1], "dtype": "float32"},
        latency_provider="",
    )
    env = dict(os.environ)
    env.pop("ORCA_AGENT_RESOURCES", None)
    r = subprocess.run(
        [sys.executable, str(teacher)],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"
    assert "CORRECTNESS: OK" in r.stdout
    assert "LATENCY_SKIPPED" in r.stdout
    # 关键：不产出 LATENCY_MS（绝不伪造）
    assert "LATENCY_MS:" not in r.stdout


@needs_ort
def test_e2e_teacher_main_cli_overrides_empty_default(tmp_path):
    """E2E：teacher __main__ 默认 latency_provider 空但 CLI 传 ``--latency_provider`` → 仍走 provider。

    覆盖 agent.md bash 块的 belt-and-suspenders 兜底——``python3 "$CONTRACT" --latency_provider ...``
    即使 LLM 忘渲染默认值，CLI 覆盖一次保险。对齐 flatten 的 ``test_flat_main_cli_overrides_empty_default``。
    """
    if not DEMO_BASELINE.is_file():
        pytest.skip(f"demo baseline 不存在：{DEMO_BASELINE}")
    teacher = tmp_path / "baseline_model_teacher.py"
    _write_teacher_wrapper(
        teacher, DEMO_BASELINE,
        depth_axis="num_blocks",
        width_axis="embed_dim",
        knobs={
            "num_blocks": {"default": 12, "min": 2, "step": -1, "leverage": "high"},
            "embed_dim": {"default": 24, "min": 4, "step": -2, "leverage": "medium"},
        },
        dummy_input={"shape": [1, 4, 48, 64, 1], "dtype": "float32"},
        latency_provider="",  # 默认空（模拟 LLM 未渲染）
    )
    env = dict(os.environ)
    env["ORCA_AGENT_RESOURCES"] = str(TEACHER_GEN_DIR)
    r = subprocess.run(
        [sys.executable, str(teacher),
         "--latency_provider", f"{DEMO_PROVIDER}::measure",  # CLI 兜底覆盖
         "--device", "cpu", "--repeats", "2"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"
    assert "LATENCY_MS:" in r.stdout
    assert "LATENCY_SOURCE: provider" in r.stdout  # CLI 覆盖走 provider 路径（非 fallback）


def test_wrapper_teacher_delegates_not_copies(tmp_path):
    """wrapper 模式：teacher 文件 build_model 委托给 baseline（不拷贝 baseline 的 nn.Module 类）。

    构造一个 baseline，其 nn.Module 类带一个特殊方法 ``_test_marker``；teacher wrapper
    实例化的 model 应该有这个方法（证明是 baseline 的类实例，不是 teacher 自实现）。
    """
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    baseline.write_text(
        textwrap.dedent("""\
        import torch.nn as nn

        DUMMY_INPUT = {'shape': [1, 8, 4, 4], 'dtype': 'float32'}
        BUILD_FN = 'build_model'
        KNOBS = {'num_blocks': {'default': 2, 'min': 1, 'step': -1, 'leverage': 'high'},
                 'embed_dim': {'default': 8, 'min': 4, 'step': -2, 'leverage': 'medium'}}

        class _MarkerModel(nn.Module):
            def __init__(self, num_blocks, embed_dim):
                super().__init__()
                self.layers = nn.Sequential(*[nn.Conv2d(8, 8, 1) for _ in range(num_blocks)])

            def forward(self, x):
                return self.layers(x)

            def _test_marker(self):
                return "baseline-class-instance"

        def build_model(**cfg):
            return _MarkerModel(
                num_blocks=int(cfg.get('num_blocks', 2)),
                embed_dim=int(cfg.get('embed_dim', 8)),
            )
        """),
        encoding="utf-8",
    )
    _write_teacher_wrapper(
        teacher, baseline,
        knobs={
            "num_blocks": {"default": 6, "min": 1, "step": -1, "leverage": "high"},
            "embed_dim": {"default": 16, "min": 4, "step": -2, "leverage": "medium"},
        },
    )
    # 跑一段 python：import teacher，build_model，验证 model 是 baseline 类的实例
    checker = tmp_path / "_check.py"
    checker.write_text(
        textwrap.dedent("""\
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("_t", sys.argv[1])
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        model = m.build_model()
        assert hasattr(model, "_test_marker"), "teacher build_model 应返回 baseline 类实例"
        assert model._test_marker() == "baseline-class-instance"
        print("WRAPPER_OK")
        """),
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(checker), str(teacher)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "WRAPPER_OK" in r.stdout


def test_wrapper_teacher_independent_of_sys_path(tmp_path):
    """wrapper 用 importlib.util.spec_from_file_location（绝对路径），不污染 sys.path 找 baseline。

    teacher 文件被 import 时，自身 _load_baseline_module 内部管理 sys.path（仅临时 insert
    baseline 目录），不依赖外部 cwd / sys.path 状态。
    """
    baseline = tmp_path / "baseline.py"
    teacher = tmp_path / "baseline_teacher.py"
    _write_parametric_baseline(baseline)
    _write_teacher_wrapper(teacher, baseline)
    # cwd 不在 tmp_path，sys.path 也不含 tmp_path（baseline 不在 path 上）
    # teacher 仍能 import baseline（绝对路径加载）
    checker = tmp_path / "_check2.py"
    checker.write_text(
        textwrap.dedent("""\
        import importlib.util, sys, os
        # 模拟外部 cwd（不在 teacher/baseline 目录）
        os.chdir(os.path.expanduser("~"))
        spec = importlib.util.spec_from_file_location("_t", sys.argv[1])
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        model = m.build_model()
        print(f"MODEL_OK: {type(model).__name__}")
        """),
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(checker), str(teacher)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "MODEL_OK" in r.stdout

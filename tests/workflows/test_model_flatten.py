"""test_model_flatten.py —— model-flatten agent 关键不变量测试。

覆盖：
- ``validate_contract.py`` PASS / FAIL 路径（确定性硬校验，fail loud）
- model-flatten/agent.md 结构契约（output_schema 字段、SKILL.md / scripts 引用）
- flatten 节点在 kd-nas.yaml DAG 里的 entry + 路由

不跑真模型（torch 仅在 forward 校验里用，CPU Identity 即可），不依赖 GPU/真硬件。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FLATTEN_DIR = REPO / "workflows" / "agents" / "model-flatten"
VALIDATE = FLATTEN_DIR / "scripts" / "validate_contract.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── validate_contract.py 纯函数：PASS 路径 ──────────────────────────────────────


def _write_valid_contract(p: Path, *, knobs: dict | None = None) -> None:
    """写一个最小合规的 KD 变体契约 .py（Identity 模型 + 1 knob）。"""
    if knobs is None:
        knobs = {"num_blocks": {"default": 3, "min": 1, "step": -1, "leverage": "high"}}
    p.write_text(
        "import torch\nimport torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1, 4, 48, 64, 1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        f"KNOBS = {knobs!r}\n"
        "def build_model(**cfg):\n"
        "    return nn.Identity()\n",
        encoding="utf-8",
    )


def test_validate_contract_pass_minimal(tmp_path):
    """PASS：合规契约 → exit 0 + emit VALIDATION: PASS + 全字段。"""
    p = tmp_path / "ok_flat.py"
    _write_valid_contract(p)
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu", "--seed", "0"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "VALIDATION: PASS" in r.stdout
    assert "BUILD_FN: build_model" in r.stdout
    assert "DUMMY_INPUT:" in r.stdout
    assert "KNOBS:" in r.stdout
    assert "FORWARD_SHAPE:" in r.stdout
    assert "SHAPE_MATCH: true" in r.stdout
    # FAIL_REASON 不应出现
    assert "FAIL_REASON" not in r.stdout


def test_validate_contract_pass_multi_knobs(tmp_path):
    """PASS：多 knob（num_blocks + embed_dim，与 receiver 变体同款）→ exit 0。"""
    p = tmp_path / "multi_flat.py"
    _write_valid_contract(p, knobs={
        "num_blocks": {"default": 3, "min": 1, "step": -1, "leverage": "high"},
        "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"},
    })
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "VALIDATION: PASS" in r.stdout


# ── validate_contract.py 纯函数：FAIL 路径（fail loud，exit 2）──────────────────


def test_validate_contract_fail_missing_file(tmp_path):
    """FAIL：契约文件不存在 → exit 2 + FAIL_REASON。"""
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(tmp_path / "nope.py"), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "VALIDATION: FAIL" in r.stdout
    assert "FAIL_REASON:" in r.stdout
    assert "不存在" in r.stdout or "FileNotFoundError" in r.stdout


def test_validate_contract_fail_import_error(tmp_path):
    """FAIL：契约 import 抛异常 → FAIL_REASON 含异常类名。"""
    p = tmp_path / "boom_flat.py"
    p.write_text(
        "raise RuntimeError('intentional import boom')\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "VALIDATION: FAIL" in r.stdout
    assert "intentional import boom" in r.stdout or "RuntimeError" in r.stdout


def test_validate_contract_fail_build_fn_not_string(tmp_path):
    """FAIL：BUILD_FN != 'build_model' 字面量 → FAIL。"""
    p = tmp_path / "wrong_fn.py"
    p.write_text(
        "import torch.nn as nn\n"
        "BUILD_FN = 'create_model'\n"  # 不是 'build_model'
        "DUMMY_INPUT = {'shape': [1], 'dtype': 'float32'}\n"
        "KNOBS = {'n': {'default': 1, 'min': 1, 'step': -1, 'leverage': 'high'}}\n"
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "BUILD_FN" in r.stdout and "build_model" in r.stdout


def test_validate_contract_fail_build_model_missing(tmp_path):
    """FAIL：缺 callable build_model → FAIL。"""
    p = tmp_path / "no_build.py"
    p.write_text(
        "DUMMY_INPUT = {'shape': [1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': {'default': 1, 'min': 1, 'step': -1, 'leverage': 'high'}}\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "callable build_model" in r.stdout


def test_validate_contract_fail_dummy_input_no_shape(tmp_path):
    """FAIL：DUMMY_INPUT.shape 缺失 → FAIL（BLK-4：禁硬编码回退）。"""
    p = tmp_path / "no_shape.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'dtype': 'float32'}\n"  # 缺 shape
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': {'default': 1, 'min': 1, 'step': -1, 'leverage': 'high'}}\n"
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "shape" in r.stdout


def test_validate_contract_fail_empty_knobs(tmp_path):
    """FAIL：KNOBS={} 空字典 → FAIL（flatten 必须识别至少一个可调维度）。"""
    p = tmp_path / "empty_knobs.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {}\n"
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "KNOBS" in r.stdout and "非空" in r.stdout


def test_validate_contract_fail_knobs_missing_field(tmp_path):
    """FAIL：knob 缺字段（无 'step'）→ FAIL。"""
    p = tmp_path / "knob_no_step.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': {'default': 1, 'min': 1, 'leverage': 'high'}}\n"  # 缺 step
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "step" in r.stdout


def test_validate_contract_fail_step_positive(tmp_path):
    """FAIL：step>=0 → FAIL（缩容方向必须是负数，CONTRACTS §1）。"""
    p = tmp_path / "pos_step.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': {'default': 3, 'min': 1, 'step': 1, 'leverage': 'high'}}\n"  # step>=0
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "step" in r.stdout and "<0" in r.stdout


def test_validate_contract_fail_bad_leverage(tmp_path):
    """FAIL：leverage∉{high,medium,low} → FAIL。"""
    p = tmp_path / "bad_lev.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': {'default': 3, 'min': 1, 'step': -1, 'leverage': 'extreme'}}\n"
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "leverage" in r.stdout


def test_validate_contract_fail_forward_shape_mismatch(tmp_path):
    """FAIL：build_model forward 成功但输出 shape != DUMMY_INPUT.shape（契约头条不变量）。

    用 Conv2d(4, 8, kernel_size=1) + 4D DUMMY_INPUT：forward OK，输出 [1,8,8,8] != 声明 [1,4,8,8]。
    """
    p = tmp_path / "shape_mismatch.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1, 4, 8, 8], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': {'default': 1, 'min': 1, 'step': -1, 'leverage': 'high'}}\n"
        "def build_model(**c):\n"
        "    # 输入通道 4 → 输出通道 8（forward OK 但 shape 不匹配 DUMMY_INPUT.shape）\n"
        "    return nn.Conv2d(4, 8, kernel_size=1)\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "VALIDATION: FAIL" in r.stdout
    assert "forward shape" in r.stdout
    assert "[1, 8, 8, 8]" in r.stdout and "[1, 4, 8, 8]" in r.stdout


def test_validate_contract_fail_forward_exception(tmp_path):
    """FAIL：forward 抛异常（如 Conv2d 收到 5D 输入）→ FAIL_REASON 含 'forward 失败'。"""
    p = tmp_path / "fwd_exc.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1, 4, 48, 64, 1], 'dtype': 'float32'}\n"  # 5D
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': {'default': 1, 'min': 1, 'step': -1, 'leverage': 'high'}}\n"
        "def build_model(**c):\n"
        "    return nn.Conv2d(4, 8, kernel_size=1)\n"  # 期望 4D，5D 输入 → RuntimeError
        "",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "forward" in r.stdout and "RuntimeError" in r.stdout


def test_validate_contract_fail_build_model_instantiation(tmp_path):
    """FAIL：build_model(**defaults) 实例化抛异常（knob 名与构造参数不一致）→ FAIL。

    最常见 LLM-flatten 失败模式：KNOBS 写了 embed_dim 但 build_model 只接受 num_blocks。
    """
    p = tmp_path / "inst_fail.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'num_blocks': {'default': 3, 'min': 1, 'step': -1, 'leverage': 'high'},\n"
        "         'embed_dim': {'default': 16, 'min': 8, 'step': -4, 'leverage': 'medium'}}\n"
        # 没有 **cfg，且不接 embed_dim → TypeError: unexpected keyword argument 'embed_dim'
        "def build_model(num_blocks):\n"
        "    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "build_model(**defaults) 实例化失败" in r.stdout
    assert "TypeError" in r.stdout or "embed_dim" in r.stdout


def test_validate_contract_fail_non_numeric_default(tmp_path):
    """FAIL：default 是字符串 → FAIL（default/min 须为数值）。"""
    p = tmp_path / "str_default.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': {'default': '3', 'min': 1, 'step': -1, 'leverage': 'high'}}\n"  # str
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "default" in r.stdout and "数值" in r.stdout


def test_validate_contract_fail_bool_default(tmp_path):
    """FAIL：default=True → FAIL（bool 是 int 子类但语义非法，须显式排除）。"""
    p = tmp_path / "bool_default.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': {'default': True, 'min': 1, 'step': -1, 'leverage': 'high'}}\n"
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "default" in r.stdout


def test_validate_contract_fail_knobs_value_not_dict(tmp_path):
    """FAIL：KNOBS[k] 不是 dict（如字符串）→ FAIL。"""
    p = tmp_path / "knob_str.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': 'not-a-dict'}\n"
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "KNOBS" in r.stdout and "不是 dict" in r.stdout


def test_validate_contract_fail_dummy_input_missing_dtype(tmp_path):
    """FAIL：DUMMY_INPUT 缺 dtype 键 → FAIL（CONTRACTS §1 要求显式声明，不默认）。"""
    p = tmp_path / "no_dtype.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1]}\n"  # 缺 dtype
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': {'default': 1, 'min': 1, 'step': -1, 'leverage': 'high'}}\n"
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "dtype" in r.stdout


def test_validate_contract_fail_bad_dtype_name(tmp_path):
    """FAIL：DUMMY_INPUT.dtype='float999' 不是合法 torch dtype → FAIL。"""
    p = tmp_path / "bad_dtype.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1], 'dtype': 'float999'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': {'default': 1, 'min': 1, 'step': -1, 'leverage': 'high'}}\n"
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "dtype" in r.stdout


# ── validate_contract.py：纯函数单元（in-process，无 subprocess）────────────────


def test_validate_contract_function_returns_ok_dict(tmp_path):
    """``validate_contract(...)`` 函数返回 dict（ok=True + 字段齐全），不打印。"""
    vc = _load(VALIDATE, "_vc_fn")
    p = tmp_path / "ok2.py"
    _write_valid_contract(p)
    result = vc.validate_contract(str(p), device_arg="cpu", seed=0)
    assert result["ok"] is True
    assert result["reason"] == ""
    assert result["build_fn"] == "build_model"
    assert result["forward_shape"] == [1, 4, 48, 64, 1]
    assert "num_blocks" in result["knobs"]


def test_validate_contract_function_returns_fail_dict(tmp_path):
    """``validate_contract(...)`` 函数对非法契约返回 ok=False + reason（不 raise）。"""
    vc = _load(VALIDATE, "_vc_fn2")
    p = tmp_path / "bad.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'wrong'\n"  # 不合规
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    result = vc.validate_contract(str(p), device_arg="cpu", seed=0)
    assert result["ok"] is False
    assert "BUILD_FN" in result["reason"]


# ── model-flatten agent.md 结构契约 ─────────────────────────────────────────────


def test_model_flatten_agent_md_has_strong_directive():
    """model-flatten/agent.md 必须有强执行指令 + output_schema 前置 + bash 块
    （BUG-1 抗 spec-审查：deepseek-v4-flash 容易把 agent.md 当 spec 评判）。"""
    text = (FLATTEN_DIR / "agent.md").read_text(encoding="utf-8")
    head = text[:2000]
    assert "唯一产出" in head, "model-flatten/agent.md 开头缺「唯一产出」执行指令"
    assert "严禁" in head, "model-flatten/agent.md 开头缺「严禁」红线"
    assert "❌" in head, "model-flatten/agent.md 缺 ❌ 红线列表"
    assert "fail loud" in text.lower() or "失败" in text
    # output_schema 段前置：在第一个 bash 块之前
    schema_offset = text.find("输出 JSON schema")
    bash_offset = text.find("```bash")
    assert schema_offset >= 0 and bash_offset >= 0
    assert schema_offset < bash_offset, (
        f"output JSON schema（offset={schema_offset}）应在第一个 bash 块"
        f"（offset={bash_offset}）之前"
    )
    # 显式「执行：」标签：agent.md 末尾硬校验 bash 块须带「执行：」（BUG-1 抗 spec-审查）
    assert text.count("执行：") >= 1, (
        "model-flatten/agent.md 必须有显式「执行：」bash 块标签（BUG-1：deepseek-v4-flash 易 spec-审查）"
    )


def test_model_flatten_agent_md_refs_flatten_output():
    """model-flatten/agent.md 引用的 ``{{ ... }```` 必须只引 inputs.* + flatten 内部
    资源（不引 setup/gate/train —— flatten 是入口，没有上游）。"""
    import re
    text = (FLATTEN_DIR / "agent.md").read_text(encoding="utf-8")
    # 引用 flatten 上游节点（setup/gate/train）= 配置错误（flatten 是 entry，没上游）
    upstream_pat = re.compile(r"\{\{\s*(setup|gate|train)\.output\.")
    bad = upstream_pat.findall(text)
    assert not bad, f"model-flatten/agent.md 不应引用上游 node output（flatten 是 entry）：{bad}"


def test_model_flatten_agent_md_consumes_baseline_model_path_input():
    """正向断言：flatten 必须消费 ``{{ inputs.baseline_model_path }}``（它的入口 input）。
    m3：避免有人误删该 jinja 引用导致 flatten 不知道展平什么文件。"""
    text = (FLATTEN_DIR / "agent.md").read_text(encoding="utf-8")
    assert "{{ inputs.baseline_model_path }}" in text, (
        "model-flatten/agent.md 必须消费 inputs.baseline_model_path（flatten 的模型入口 input）"
    )


def test_flatten_agent_md_output_dir_co_rooted_with_setup():
    """flatten output_dir 必须与下游 setup ``kd_artifacts_dir`` 同根
    （``${PROJECT_ROOT}/artifacts/kd-nas/``），不再落 per-run ``$ORCA_ARTIFACTS_DIR``
    （2026-08-04 drift 修复守护：flatten 早于 setup 写入，曾误用 P9 旧约定 per-run 目录）。

    R1 闭环（code-reviewer）：step3 去后缀必须落定为**确定性 python 片段**（``split(' (low-confidence')``
    + ``os.path.abspath``，与 ``kd-setup/agent.md`` 逐字对齐），非 prose（Rule 5）。
    本测试守「契约 prose + 确定性代码」两层；runtime 同根性靠两边 python 片段逐字对齐保证。"""
    text = (FLATTEN_DIR / "agent.md").read_text(encoding="utf-8")
    assert "${PROJECT_ROOT}/artifacts/kd-nas/models/baseline/" in text, (
        "flatten output_dir 必须落 ${PROJECT_ROOT}/artifacts/kd-nas/models/baseline/"
        "（与 setup kd_artifacts_dir 同根，跨 run 持久）"
    )
    # 产物目录的两处指定（输入段「输出目录:」+ 准备 step3「确定输出目录」）不应再用
    # $ORCA_ARTIFACTS_DIR（per-run runs/<run_id>/）作产物目录。
    for anchor in ("输出目录:", "确定输出目录"):
        idx = text.find(anchor)
        assert idx >= 0, f"agent.md 缺 {anchor!r} 段"
        block = text[idx:idx + 400]
        assert "$ORCA_ARTIFACTS_DIR" not in block, (
            f"{anchor!r} 段不应再用 $ORCA_ARTIFACTS_DIR（per-run）作产物目录；"
            f"已改 ${PROJECT_ROOT}/artifacts/kd-nas/ 同根合流"
        )
    # R1：step3 去后缀必须用确定性代码（与 setup kd-setup/agent.md 逐字对齐），非 prose。
    step3_idx = text.find("确定输出目录")
    assert step3_idx >= 0
    step3_block = text[step3_idx:step3_idx + 1500]
    assert "split(' (low-confidence')" in step3_block, (
        "flatten step3 必须含确定性去后缀代码 split(' (low-confidence')（与 setup 对齐，Rule 5："
        "deterministic 用代码不用 prose）"
    )
    assert "os.path.abspath" in step3_block, (
        "flatten step3 去后缀后必须 os.path.abspath（与 setup 对齐，保证两边路径字面一致）"
    )


def test_model_flatten_skill_md_only_step1_no_supernet():
    """SKILL.md 只搬 p-m-o Step 1（展平 + KNOBS + 校验），剥掉 Step 2-7
    （optimize_rules / supernet / SearchSpace —— NAS 专用，KD 用不到）。"""
    text = (FLATTEN_DIR / "SKILL.md").read_text(encoding="utf-8")
    # 应有 Step 1（collect task context）+ KNOBS 识别 + 校验
    assert "Step 1" in text and "Collect Task Context" in text
    assert "KNOBS" in text and "leverage" in text
    assert "validate_contract.py" in text
    # 不应有 NAS 专用术语
    for forbidden in ("supernet", "SearchSpace", "optimize_rules", "model_type.json"):
        assert forbidden not in text, (
            f"SKILL.md 不应含 NAS 专用术语 {forbidden!r}（应剥掉 Step 2-7）"
        )


def test_model_flatten_skill_md_has_verifier_prompt_scaffold():
    """SKILL.md Step 6b 必须有 flatten-verifier 子 agent prompt 框架
    （用户已定：脚本硬校验 + LLM 复核迭代到 PASS）。"""
    text = (FLATTEN_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "flatten-verifier" in text
    assert "BLOCKER" in text and "MAJOR" in text and "MINOR" in text
    # 迭代到 PASS 的语义（明确优先级：PASS 必须在，且 iteration 或「迭代」至少一个在）
    assert "PASS" in text
    assert "iteration" in text.lower() or "迭代" in text


def test_validate_contract_rank_sync_with_kd_common():
    """m1：validate_contract.py 的 RANK 与 _kd_scripts/kd_common.RANK 必须一致
    （本地复制无机器强制 → 用测试守门，防漂移）。"""
    sys.path.insert(0, str(REPO / "workflows" / "agents" / "_kd_scripts"))
    for m in [n for n in sys.modules if n == "kd_common"]:
        del sys.modules[m]
    from kd_common import RANK as KD_RANK  # noqa: E402
    vc = _load(VALIDATE, "_vc_rank_sync")
    assert vc.RANK == KD_RANK, (
        f"validate_contract.RANK != kd_common.RANK（漂移）：{vc.RANK} vs {KD_RANK}"
    )
    assert vc._VALID_LEVERAGE == set(KD_RANK)


# ── validate_contract.py：min forward 自检（M2：SKILL.md 宣称「Step 6 catch invalid min」）──


def test_validate_contract_fail_min_breaks_forward(tmp_path):
    """FAIL：build_model(**mins) forward 失败 → FAIL（min 不是合法结构地板）。

    default=2 forward OK；min=0 触发 build_model 内部 raise → Step 8 min 自检 FAIL。
    验证 SKILL.md Step 5「Step 6 hard-validation will catch invalid min」由 validate_contract 闭环。
    """
    p = tmp_path / "bad_min.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1, 4, 8, 8], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'num_layers': {'default': 2, 'min': 0, 'step': -1, 'leverage': 'high'}}\n"
        "def build_model(**cfg):\n"
        "    n = int(cfg.get('num_layers', 2))\n"
        "    if n == 0:\n"
        "        raise ValueError('num_layers=0 不是合法结构地板')\n"  # min=0 触发异常
        "    return nn.Sequential(*[nn.Conv2d(4, 4, 1) for _ in range(n)])\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "mins" in r.stdout or "结构地板" in r.stdout
    assert "ValueError" in r.stdout


# ── kd-nas.yaml DAG：flatten 是 entry，4 节点链 ─────────────────────────────────


@pytest.mark.skip(reason="obsolete after 2026-08-03 kd-nas serial rework: yaml drops batch gate/train/select nodes in favor of serial gen_student/distill/decide loop")
def test_kd_nas_entry_is_flatten():
    """kd-nas.yaml entry 是 flatten（不再是 setup）；v4 DAG flatten routes to teacher_gen。"""
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    assert wf.entry == "flatten"
    flatten = next(n for n in wf.nodes if n.name == "flatten")
    assert flatten.agent == "model-flatten"
    # v4：flatten → teacher_gen（不再直连 setup）
    assert [r.to for r in flatten.routes] == ["teacher_gen"]


def test_kd_nas_baseline_model_path_description_updated():
    """baseline_model_path description 应明确「flatten agent 会展平成 KD 变体契约」
    （用户不再被要求自带契约）。"""
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    desc = (wf.inputs or {})["baseline_model_path"].description
    assert "flatten" in desc.lower(), (
        f"baseline_model_path description 应提 flatten agent：{desc!r}"
    )


# ── measure_latency.py：flatten __main__ 的 latency 测量 helper ──────────────────

MEASURE = FLATTEN_DIR / "scripts" / "measure_latency.py"
# demo latency_provider（path::func），用于 provider 路径集成测试。
DEMO_PROVIDER = REPO / "examples" / "kd-nas-demo" / "latency_provider.py"


def _write_measureable_contract(p: Path) -> None:
    """写一个可导 ONNX + 可测 latency 的最小契约（Identity 模型）。"""
    p.write_text(
        "import torch\nimport torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1, 4, 48, 64, 1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'num_blocks': {'default': 3, 'min': 1, 'step': -1, 'leverage': 'high'}}\n"
        "def build_model(**cfg):\n"
        "    return nn.Identity()\n",
        encoding="utf-8",
    )


# onnxruntime / numpy 是 latency 测量的硬依赖；用 skipif 装饰器**逐测试**gate，
# 绝不在模块顶层 importorskip（否则无 onnxruntime 的 CI 会静默 skip 全部 validate_contract
# 测试——那些只需 torch，与 latency 无关）。
import importlib.util as _ilu
_ORT_OK = _ilu.find_spec("onnxruntime") is not None and _ilu.find_spec("numpy") is not None
needs_ort = pytest.mark.skipif(
    not _ORT_OK, reason="onnxruntime+numpy required for latency measurement（本测试 skip 不伪造）"
)


def test_measure_latency_cli_cpu_fallback(tmp_path):
    """CLI：latency_provider 空 → ONNXRT-CPU fallback + WARN + confidence=low + exit 0。"""
    p = tmp_path / "ok_flat.py"
    _write_measureable_contract(p)
    r = subprocess.run(
        [sys.executable, str(MEASURE), "--contract", str(p),
         "--device", "cpu", "--repeats", "2"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "LATENCY_US:" in r.stdout
    assert "LATENCY_SOURCE: cpu-fallback" in r.stdout
    assert "LATENCY_CONFIDENCE: low" in r.stdout
    # WARN 在 stderr（非用户真硬件提示）
    assert "fallback" in r.stderr.lower() or "WARN" in r.stderr


def test_measure_latency_cli_with_provider(tmp_path):
    """CLI：latency_provider 给 demo provider → source=provider + confidence=high + exit 0。"""
    p = tmp_path / "ok_flat.py"
    _write_measureable_contract(p)
    r = subprocess.run(
        [sys.executable, str(MEASURE), "--contract", str(p),
         "--latency_provider", f"{DEMO_PROVIDER}::measure",
         "--device", "cpu", "--repeats", "2"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "LATENCY_US:" in r.stdout
    assert "LATENCY_SOURCE: provider" in r.stdout
    assert "LATENCY_CONFIDENCE: high" in r.stdout
    # LATENCY_US 是正数（真实测量，>0）
    line = next(l for l in r.stdout.splitlines() if l.startswith("LATENCY_US:"))
    val = float(line.split(":", 1)[1].strip())
    assert val > 0.0, f"latency 应 >0（真实测量），得到 {val}"


def test_measure_latency_cli_fail_missing_contract(tmp_path):
    """CLI：契约文件不存在 → exit 2 + stderr FAIL。绝不伪造（stdout 无 LATENCY_US）。"""
    r = subprocess.run(
        [sys.executable, str(MEASURE), "--contract", str(tmp_path / "nope.py")],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "FileNotFoundError" in r.stderr or "不存在" in r.stderr
    # 绝不伪造（CONTRACTS §6）：失败路径不产出 LATENCY_US
    assert "LATENCY_US:" not in r.stdout


def test_measure_latency_cli_fail_bad_provider(tmp_path):
    """CLI：latency_provider 文件不存在 → exit 2（fail loud，绝不伪造 latency）。"""
    p = tmp_path / "ok_flat.py"
    _write_measureable_contract(p)
    r = subprocess.run(
        [sys.executable, str(MEASURE), "--contract", str(p),
         "--latency_provider", "/nope/missing.py::measure"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "latency_provider 文件不存在" in r.stderr or "FileNotFoundError" in r.stderr
    # 关键：不产出 LATENCY_US（绝不伪造）
    assert "LATENCY_US:" not in r.stdout


def test_measure_latency_cli_fail_bad_provider_format(tmp_path):
    """CLI：latency_provider 非 path::func 形态 → exit 2（fail loud）。绝不伪造。"""
    p = tmp_path / "ok_flat.py"
    _write_measureable_contract(p)
    r = subprocess.run(
        [sys.executable, str(MEASURE), "--contract", str(p),
         "--latency_provider", "no-double-colon"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "path::func" in r.stderr or "ValueError" in r.stderr
    # 绝不伪造（CONTRACTS §6）：失败路径不产出 LATENCY_US
    assert "LATENCY_US:" not in r.stdout


def test_measure_latency_cli_fail_no_dummy_shape(tmp_path):
    """CLI：契约缺 DUMMY_INPUT.shape → exit 2（无法导 ONNX）。绝不伪造。"""
    p = tmp_path / "no_shape.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'dtype': 'float32'}\n"  # 缺 shape
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {'n': {'default': 1, 'min': 1, 'step': -1, 'leverage': 'high'}}\n"
        "def build_model(**c):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(MEASURE), "--contract", str(p), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "DUMMY_INPUT.shape" in r.stderr
    # 绝不伪造（CONTRACTS §6）：失败路径不产出 LATENCY_US
    assert "LATENCY_US:" not in r.stdout


def test_measure_latency_cli_fail_provider_func_missing(tmp_path):
    """CLI：latency_provider 文件存在但函数名不存在 → exit 2（_load_measure AttributeError）。"""
    p = tmp_path / "ok_flat.py"
    _write_measureable_contract(p)
    # 写一个 provider 文件，但不含 `measure` 函数（含 `other`）
    prov = tmp_path / "bad_prov.py"
    prov.write_text(
        "def other(onnx_path):\n    return 1.0\n",  # 无 measure 函数
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(MEASURE), "--contract", str(p),
         "--latency_provider", f"{prov}::measure"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "无函数" in r.stderr or "AttributeError" in r.stderr
    assert "LATENCY_US:" not in r.stdout


@needs_ort
def test_measure_latency_function_returns_dict_cpu_fallback(tmp_path):
    """``measure_contract_latency(...)`` 函数：空 provider → cpu-fallback dict（不打印）。"""
    ml = _load(MEASURE, "_ml_fn_cpu")
    p = tmp_path / "ok.py"
    _write_measureable_contract(p)
    res = ml.measure_contract_latency(
        contract_path=str(p), latency_provider="", device="cpu", repeats=2,
    )
    assert res["source"] == "cpu-fallback"
    assert res["confidence"] == "low"
    assert res["latency_us_median"] > 0.0
    assert res["onnx_path"].endswith(".onnx")


@needs_ort
def test_measure_latency_function_returns_dict_with_provider(tmp_path):
    """``measure_contract_latency(...)`` 函数：给 provider → source=provider + confidence=high。"""
    ml = _load(MEASURE, "_ml_fn_prov")
    p = tmp_path / "ok.py"
    _write_measureable_contract(p)
    res = ml.measure_contract_latency(
        contract_path=str(p),
        latency_provider=f"{DEMO_PROVIDER}::measure",
        device="cpu", repeats=2,
    )
    assert res["source"] == "provider"
    assert res["confidence"] == "high"
    assert res["latency_us_median"] > 0.0


@needs_ort
def test_measure_latency_provider_without_device_kwarg(tmp_path):
    """provider 的 measure 函数无 ``device`` 形参 → 走 ``measure(onnx)`` 单参调用分支。

    覆盖 ``_measure_with_provider`` 的 ``accepts_device=False`` else 分支（签名检测）。
    契约：latency_provider docstring 明示 ``device`` 形参可选，调用方按需注入。
    """
    ml = _load(MEASURE, "_ml_fn_no_dev")
    p = tmp_path / "ok.py"
    _write_measureable_contract(p)
    # provider 不含 device 形参（单参 measure）
    prov = tmp_path / "nodev_prov.py"
    prov.write_text(
        "def measure(onnx_path):\n"
        "    import onnxruntime as ort, time, statistics\n"
        "    s = ort.InferenceSession(onnx_path, providers=['CPUExecutionProvider'])\n"
        "    inp = {i.name: __import__('numpy').zeros([d if isinstance(d,int) else 1 for d in i.shape], dtype='float32') for i in s.get_inputs()}\n"
        "    s.run(None, inp)\n"
        "    t = time.perf_counter(); s.run(None, inp)\n"
        "    return (time.perf_counter()-t)*1000.0\n",
        encoding="utf-8",
    )
    res = ml.measure_contract_latency(
        contract_path=str(p),
        latency_provider=f"{prov}::measure",
        device="cpu", repeats=2,
    )
    assert res["source"] == "provider"
    assert res["confidence"] == "high"
    assert res["latency_us_median"] > 0.0


@needs_ort
def test_measure_latency_empty_knobs_defaults(tmp_path):
    """``measure_contract_latency``：KNOBS={} → defaults={} → build_model() 零参（容错分支）。

    实际流程 validate_contract 先 PASS（KNOBS 必非空），此处锁定 defensive 容错不崩。
    """
    ml = _load(MEASURE, "_ml_fn_empty_knobs")
    p = tmp_path / "no_knobs.py"
    p.write_text(
        "import torch.nn as nn\n"
        "DUMMY_INPUT = {'shape': [1, 4, 48, 64, 1], 'dtype': 'float32'}\n"
        "BUILD_FN = 'build_model'\n"
        "KNOBS = {}\n"  # 空 KNOBS（defensive 路径）
        "def build_model(**cfg):\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    res = ml.measure_contract_latency(
        contract_path=str(p), latency_provider="", device="cpu", repeats=1,
    )
    assert res["latency_us_median"] > 0.0
    assert res["source"] == "cpu-fallback"


@needs_ort
def test_measure_latency_onnx_out_custom_path(tmp_path):
    """``--onnx_out`` 自定义路径 → ONNX 落到指定位置（非默认 <stem>_baseline.onnx）。"""
    p = tmp_path / "ok_flat.py"
    _write_measureable_contract(p)
    custom = tmp_path / "sub" / "custom.onnx"
    r = subprocess.run(
        [sys.executable, str(MEASURE), "--contract", str(p),
         "--device", "cpu", "--repeats", "1", "--onnx_out", str(custom)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert custom.is_file(), "ONNX 应落到 --onnx_out 指定路径"


@needs_ort
def test_measure_latency_cli_seed_opset_non_default(tmp_path):
    """``--seed`` / ``--opset`` 非默认值 → 仍 exit 0 + ONNX 落盘（参数透传不崩）。"""
    p = tmp_path / "ok_flat.py"
    _write_measureable_contract(p)
    r = subprocess.run(
        [sys.executable, str(MEASURE), "--contract", str(p),
         "--device", "cpu", "--repeats", "1", "--seed", "42", "--opset", "14"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "LATENCY_US:" in r.stdout
    assert "LATENCY_STD:" in r.stdout
    assert "ONNX:" in r.stdout


# ── flatten __main__ 端到端：跑 flat 文件 = 正确性 + latency（统一契约）──────────────


# flat 文件 __main__ 模板（对齐 SKILL.md Step 3）：正确性 + helper 测 latency。
_FLAT_MAIN_TEMPLATE = """\
import torch
import torch.nn as nn

DUMMY_INPUT = {{'shape': [1, 4, 48, 64, 1], 'dtype': 'float32'}}
BUILD_FN = 'build_model'
KNOBS = {{'num_blocks': {{'default': 3, 'min': 1, 'step': -1, 'leverage': 'high'}}}}

def build_model(**cfg):
    return nn.Identity()

if __name__ == '__main__':
    import argparse
    import os
    import sys

    _device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    _defaults = {{k: v['default'] for k, v in KNOBS.items()}}
    _model = build_model(**_defaults).to(_device).eval()
    _shape = list(DUMMY_INPUT['shape'])
    _dtype = getattr(torch, DUMMY_INPUT.get('dtype', 'float32'))
    _dummy = torch.randn(*_shape, dtype=_dtype, device=_device)
    with torch.no_grad():
        _out = _model(_dummy)
    print(f'CORRECTNESS: OK | input={{_shape}} output={{list(_out.shape)}}')

    _ap = argparse.ArgumentParser(add_help=False)
    _ap.add_argument('--latency_provider', default={default_lp!r})
    _ap.add_argument('--device', default='auto')
    _ap.add_argument('--seed', type=int, default=0)
    _ap.add_argument('--repeats', type=int, default=3)
    _ap.add_argument('--opset', type=int, default=17)
    _args, _ = _ap.parse_known_args()

    _resources = os.environ.get('ORCA_AGENT_RESOURCES', '')
    _helper = os.path.join(_resources, 'scripts', 'measure_latency.py') if _resources else ''
    if _helper and os.path.isfile(_helper):
        sys.path.insert(0, os.path.dirname(_helper))
        from measure_latency import measure_contract_latency
        _r = measure_contract_latency(
            contract_path=__file__,
            latency_provider=_args.latency_provider,
            device=_args.device, seed=_args.seed, opset=_args.opset, repeats=_args.repeats,
        )
        print(f"LATENCY_US: {{_r['latency_us_median']:.6f}}")
        print(f"LATENCY_SOURCE: {{_r['source']}}")
        print(f"LATENCY_CONFIDENCE: {{_r['confidence']}}")
    else:
        print('LATENCY_SKIPPED: helper 未找到')
"""


@needs_ort
def test_flat_main_runs_correctness_and_latency_with_provider(tmp_path, monkeypatch):
    """flat 文件 __main__：ORCA_AGENT_RESOURCES 注入 + provider 默认值 → 跑出
    CORRECTNESS: OK + LATENCY_US + LATENCY_SOURCE: provider（统一契约 happy path）。"""
    # flat 文件写入 output_dir；latency_provider 默认值 = 渲染后的 provider 路径
    flat = tmp_path / "demo_flat.py"
    flat.write_text(
        _FLAT_MAIN_TEMPLATE.format(default_lp=f"{DEMO_PROVIDER}::measure"),
        encoding="utf-8",
    )
    # ORCA_AGENT_RESOURCES 指向 model-flatten skill 目录（helper 在 scripts/）
    env = dict(os.environ)
    env["ORCA_AGENT_RESOURCES"] = str(FLATTEN_DIR)
    r = subprocess.run(
        [sys.executable, str(flat), "--device", "cpu", "--repeats", "2"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "CORRECTNESS: OK" in r.stdout
    assert "LATENCY_US:" in r.stdout
    assert "LATENCY_SOURCE: provider" in r.stdout


@needs_ort
def test_flat_main_cli_overrides_empty_default(tmp_path):
    """flat 文件 __main__：默认值空但 CLI 传 ``--latency_provider`` → 仍走 provider 路径。

    覆盖 agent.md bash 块的 belt-and-suspenders 路径（LLM 忘渲染默认值时 CLI 兜底）。
    """
    flat = tmp_path / "demo_flat.py"
    flat.write_text(
        _FLAT_MAIN_TEMPLATE.format(default_lp=""),  # 默认值空（模拟 LLM 未渲染）
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["ORCA_AGENT_RESOURCES"] = str(FLATTEN_DIR)
    r = subprocess.run(
        [sys.executable, str(flat),
         "--latency_provider", f"{DEMO_PROVIDER}::measure",
         "--device", "cpu", "--repeats", "2"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "LATENCY_US:" in r.stdout
    assert "LATENCY_SOURCE: provider" in r.stdout


def test_flat_main_latency_skipped_without_resources(tmp_path, monkeypatch):
    """flat 文件 __main__：未注入 ORCA_AGENT_RESOURCES → LATENCY_SKIPPED（不伪造）。

    correctness 仍 OK（契约 standalone：import 不依赖 helper）。
    """
    flat = tmp_path / "demo_flat.py"
    flat.write_text(
        _FLAT_MAIN_TEMPLATE.format(default_lp=""),  # 空 provider default
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("ORCA_AGENT_RESOURCES", None)  # 模拟非 orca 编排上下文
    r = subprocess.run(
        [sys.executable, str(flat)],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "CORRECTNESS: OK" in r.stdout
    assert "LATENCY_SKIPPED" in r.stdout
    # 关键：不产出 LATENCY_US（绝不伪造）
    assert "LATENCY_US:" not in r.stdout


def test_flat_main_validate_contract_unaffected_by_main_block(tmp_path):
    """flat 文件带 __main__ latency 块 → validate_contract.py import 时 __main__ 不执行
    （契约 standalone：import 只跑顶层，__main__ 延迟到显式执行）。

    验证「__main__ 加 latency 不破坏 contract import 可达性」——validate_contract 仍 PASS。
    """
    flat = tmp_path / "demo_flat.py"
    flat.write_text(
        _FLAT_MAIN_TEMPLATE.format(default_lp=""),
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(VALIDATE), "--contract", str(flat), "--device", "cpu"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "VALIDATION: PASS" in r.stdout


# ── flatten agent.md / SKILL.md：latency 装配的文档契约 ─────────────────────────


def test_flatten_agent_md_has_latency_inputs_and_output():
    """agent.md 必须声明 latency_provider input + baseline_latency_us output + 跑 __main__ 的 bash。"""
    text = (FLATTEN_DIR / "agent.md").read_text(encoding="utf-8")
    # input 声明 latency_provider（写入 flat 文件 __main__ 默认值）
    assert "{{ inputs.latency_provider }}" in text, (
        "agent.md 必须声明 latency_provider input（写入 flat __main__ 默认值）"
    )
    # output schema 含 baseline_latency_us
    assert "baseline_latency_us" in text, (
        "agent.md output JSON schema 必须含 baseline_latency_us"
    )
    # bash 块跑 __main__ 测 latency + 解析 LATENCY_US
    assert "LATENCY_US" in text, "agent.md 必须跑 flat __main__ 并解析 LATENCY_US"
    assert 'python3 "$CONTRACT"' in text, (
        "agent.md 必须直接跑 flat 文件 __main__（python3 $CONTRACT）测 latency"
    )


def test_flatten_skill_md_main_template_has_latency_block():
    """SKILL.md Step 3 的 __main__ 模板必须含 latency 测量块（measure_contract_latency）。"""
    text = (FLATTEN_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "measure_contract_latency" in text, (
        "SKILL.md __main__ 模板必须调 measure_contract_latency（统一正确性 + latency）"
    )
    assert "LATENCY_US" in text
    assert "LATENCY_SKIPPED" in text, (
        "SKILL.md __main__ 模板必须覆盖 helper 未找到的降级路径（不伪造）"
    )
    # latency_provider 占位符替换说明（rendered value，非 Jinja 串）
    assert "<LATENCY_PROVIDER_DEFAULT>" in text or "inputs.latency_provider" in text


def test_flatten_skill_md_verifier_checks_latency_wiring():
    """SKILL.md Step 6b flatten-verifier 必须校验 __main__ 真用了 latency_provider
    （给了却走 fallback → BLOCKER，违反「latency 必用用户脚本」铁律）。"""
    text = (FLATTEN_DIR / "SKILL.md").read_text(encoding="utf-8")
    # 第三维校验：Latency __main__ wiring
    assert "Latency" in text and "__main__" in text
    assert "latency_provider" in text and "BLOCKER" in text
    # 必须明确：input 给了 provider 但 __main__ default 空 → BLOCKER
    assert "ONNXRT-CPU fallback" in text or "fallback" in text.lower()
    # 必须明确：default 须是 rendered 值（非 Jinja 模板串）
    assert "rendered" in text.lower() or "渲染" in text, (
        "verifier 须明确 --latency_provider default 是 rendered 值（非 Jinja 模板串）"
    )


# ── kd-setup step2：读 flatten.output.baseline_latency_us，不再调 tune_latency ────


def test_kd_setup_step2_reads_flatten_latency_not_tune():
    """kd-setup step2：baseline_latency_us 来源 = flatten.output（不再调 tune_latency 重测）。"""
    text = (REPO / "workflows" / "agents" / "kd-setup" / "agent.md").read_text(encoding="utf-8")
    # 读 flatten.output.baseline_latency_us
    assert "flatten.output.baseline_latency_us" in text, (
        "kd-setup step2 必须从 flatten.output.baseline_latency_us 读 latency（下沉到 flatten）"
    )
    # step2 块：baseline 来源标注 + 不调 tune_latency
    step2_idx = text.find("## step 2")
    assert step2_idx >= 0
    step2_block = text[step2_idx:text.find("## step 3", step2_idx)]
    assert "flatten.output" in step2_block and "baseline_latency_us" in step2_block, (
        "kd-setup step2 必须显式标注 baseline_latency_us 来源 = flatten.output"
    )
    # step2 不再调 tune_latency.py（baseline 测量已下沉）
    assert "tune_latency.py" not in step2_block, (
        "kd-setup step2 不应再调 tune_latency.py（baseline latency 已在 flatten __main__ 测过）"
    )


# ── measure_latency.py standalone 守门（不 import _kd_scripts / _struct_scripts）────


def test_measure_latency_standalone_no_internal_imports():
    """measure_latency.py 须自包含（与 validate_contract.py 同款），不 import
    _kd_scripts / nas_agent / _struct_scripts（flatten 保 standalone，防漂移）。"""
    src = MEASURE.read_text(encoding="utf-8")
    for forbidden in ("from _kd_scripts", "import _kd_scripts",
                      "from nas_agent", "import nas_agent",
                      "from _struct_scripts", "import _struct_scripts"):
        assert forbidden not in src, (
            f"measure_latency.py 不应含内部跨包 import {forbidden!r}（flatten standalone 契约）"
        )

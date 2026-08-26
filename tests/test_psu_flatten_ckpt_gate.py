"""test_psu_flatten_ckpt_gate.py —— flatten 期 load_pretrained 生成契约 + check_flatten §7 测试。

flatten agent.md 里的 load_pretrained.py 生成指令是 LLM 指令不是代码；这里测的是
check_flatten.sh 的**脚本逻辑**（§7 = 调用生成产物 ``python3 load_pretrained.py`` 的
ckpt 冒烟），用 toy fixture 模拟生成产物：

  - 合规产物（strict 载入 + probe forward 冒烟通过）→ check_flatten 全 7 检查 PASS。
  - ckpt 冒烟失败版（载入前丢键 → strict load_state_dict RuntimeError）→ §7 FAIL、
    整体 exit 1（fail loud，不静默跳过）。
  - load_pretrained.py 缺失 → §7 FAIL（Step 4 产物强制存在）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHECK_FLATTEN_SH = REPO / "workflows" / "agents" / "psu_flatten" / "scripts" / "check_flatten.sh"
sys.path.insert(0, str(REPO / "tests"))

from _psu_test_fixtures import run_script, write_toy_flatten_artifacts  # noqa: E402


def _run_flatten_check(artifacts_dir: Path) -> dict:
    return run_script(["bash", str(CHECK_FLATTEN_SH)], artifacts_dir)


def test_check_flatten_passes_with_ckpt_smoke(tmp_path):
    """合规 toy flatten 产物：7 检查全过（含 §7 ckpt 冒烟）。"""
    write_toy_flatten_artifacts(tmp_path)
    res = _run_flatten_check(tmp_path)
    assert res["rc"] == 0, res["stdout"] + res["stderr"]
    assert "load_pretrained ckpt smoke OK" in res["stdout"]
    assert "PASS: check_flatten" in res["stdout"]


def test_check_flatten_fails_when_ckpt_key_mismatch(tmp_path):
    """ckpt 冒烟失败（strict 载入缺键 fail loud）→ §7 FAIL + exit 1。"""
    write_toy_flatten_artifacts(tmp_path, bad_loader=True)
    res = _run_flatten_check(tmp_path)
    assert res["rc"] == 1
    out = res["stdout"] + res["stderr"]
    assert "ckpt smoke failed" in out


def test_check_flatten_fails_when_load_pretrained_missing(tmp_path):
    """load_pretrained.py 缺失 → §7 FAIL（一等确定性资产强制存在）。"""
    write_toy_flatten_artifacts(tmp_path)
    (tmp_path / "load_pretrained.py").unlink()
    res = _run_flatten_check(tmp_path)
    assert res["rc"] == 1
    assert "load_pretrained.py missing" in res["stdout"]


def test_bad_loader_fails_loud_with_key_list(tmp_path):
    """失败版 loader 的 fail loud 形态：strict 载入直接抛 RuntimeError 并列缺失键。"""
    import importlib.util

    write_toy_flatten_artifacts(tmp_path, bad_loader=True)
    spec = importlib.util.spec_from_file_location("_bad_lp", tmp_path / "load_pretrained.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_bad_lp"] = mod
    sys.path.insert(0, str(tmp_path))  # toy loader 平铺 import toy_flat，须可解析
    try:
        spec.loader.exec_module(mod)
        mod.build_pretrained_model()
        raise AssertionError("缺键 strict 载入应 RuntimeError（fail loud）")
    except RuntimeError as exc:
        assert "head.weight" in str(exc)  # 未匹配键进报错信息
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("_bad_lp", None)

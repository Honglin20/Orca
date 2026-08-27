"""test_psu_check_equivalence.py —— PSU 等价 gate（check_equivalence.py）测试。

toy 模型 + toy ckpt（torch 临时文件，CPU）覆盖 pass 与 fail 两路径：
  - pass：合规 toy supernet（权重继承 + freeze 分组 + 物化键契约）→ exit 0，
    ``.equivalence.json`` passed=true。
  - fail：层 1 original 分支权重被扰动 + embed 漏冻结 → exit 1，``.equivalence.json``
    照常落盘（passed=false），失败清单含键名与 freeze 归组定位。
  - 前置缺失（load_pretrained.py 不存在）→ fail loud + JSON 落盘。
  - mask 用例要求：probe inputs 全部无 mask 时 gate FAIL（_MASK_KEYS 适配 bug 须可见）。
  - check_expand.sh 端到端：toy 产物全绿（检查 0-5 含 5a/5b）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PSU_EXPAND = REPO / "workflows" / "puzzle-supernet" / "agents" / "psu_expand_supernet"
CHECK_EQUIV = PSU_EXPAND / "scripts" / "check_equivalence.py"
CHECK_EXPAND_SH = PSU_EXPAND / "scripts" / "check_expand.sh"
sys.path.insert(0, str(REPO / "tests"))

from _psu_test_fixtures import (  # noqa: E402
    TOY_LOAD_PRETRAINED_PY,
    TOY_SUPERNET_PY,
    run_script,
    write_toy_expand_artifacts,
)


def _run_equiv(artifacts_dir: Path) -> dict:
    return run_script([sys.executable, str(CHECK_EQUIV), "--artifacts-dir", str(artifacts_dir)],
                      artifacts_dir)


def _read_marker(artifacts_dir: Path) -> dict:
    return json.loads((artifacts_dir / ".equivalence.json").read_text(encoding="utf-8"))


def test_equivalence_pass_path(tmp_path):
    """合规 toy supernet：gate 全绿 + marker passed=true。"""
    write_toy_expand_artifacts(tmp_path)
    res = _run_equiv(tmp_path)
    assert res["rc"] == 0, res["stdout"] + res["stderr"]

    marker = _read_marker(tmp_path)
    assert marker["passed"] is True
    assert marker["checks"]["materialized_key_contract"] == "ok"
    assert marker["checks"]["forward_equivalence"] == "ok"
    assert marker["checks"]["freeze_groups"] == "ok"
    assert marker["checks"]["probe_mask_case"] == "ok"
    assert marker["stats"]["n_probe_cases"] == 2
    assert marker["stats"]["n_missing"] == 0
    assert marker["stats"]["n_value_mismatch"] == 0
    # freeze 分组统计：2 slot × (original 冻结 + 2 变体可训) + 固定模块参数
    assert marker["stats"]["n_original"] > 0
    assert marker["stats"]["n_variant"] > 0
    assert marker["stats"]["n_fixed"] > 0


def test_equivalence_fail_path_perturbed_weight_and_freeze(tmp_path):
    """缺陷 supernet：键值不等 + forward 漂移 + freeze 漏配 → fail loud 清单 + marker 落盘。"""
    write_toy_expand_artifacts(tmp_path, bad_supernet=True)
    res = _run_equiv(tmp_path)
    assert res["rc"] == 1

    marker = _read_marker(tmp_path)  # fail 也必须落盘
    assert marker["passed"] is False
    assert marker["checks"]["materialized_key_contract"] == "failed"
    assert marker["checks"]["forward_equivalence"] == "failed"
    assert marker["checks"]["freeze_groups"] == "failed"

    failures = "\n".join(marker["failures"])
    assert "layers.1.fc1.weight" in failures  # 未匹配/错配键显式列名
    assert "键值不等于父权重" in failures
    assert "freeze 分组" in failures and "embed" in failures


def test_equivalence_missing_load_pretrained_fails_loud(tmp_path):
    """load_pretrained.py 缺失 → 前置缺失 fail loud，marker 仍落盘。"""
    write_toy_expand_artifacts(tmp_path)
    (tmp_path / "load_pretrained.py").unlink()
    res = _run_equiv(tmp_path)
    assert res["rc"] == 1
    marker = _read_marker(tmp_path)
    assert marker["passed"] is False
    assert any("前置缺失" in f for f in marker["failures"])


def test_equivalence_requires_mask_case_when_signature_has_mask(tmp_path):
    """原层 forward 含 attn_mask 但 probe 用例全无 mask → gate FAIL（mask 适配 bug 须可见）。"""
    write_toy_expand_artifacts(tmp_path)
    # 篡改 build_probe_inputs：剥掉 mask 用例（保留无 mask 用例）。
    bad_lp = TOY_LOAD_PRETRAINED_PY.replace(
        """    cases.append({"tokens": torch.randint(0, 50, (2, 8), generator=g),
                  "attn_mask": causal})""",
        "    pass  # mask 用例被剥掉",
    )
    assert "mask 用例被剥掉" in bad_lp  # replace 命中卫语句（防静默 no-op）
    (tmp_path / "load_pretrained.py").write_text(bad_lp, encoding="utf-8")
    res = _run_equiv(tmp_path)
    assert res["rc"] == 1
    marker = _read_marker(tmp_path)
    assert marker["checks"]["probe_mask_case"] == "failed"
    assert any("mask" in f for f in marker["failures"])


def test_check_expand_sh_end_to_end(tmp_path):
    """check_expand.sh 全量（含 5a choice 契约 + 5b 等价 gate）在 toy 产物上 PASS。"""
    write_toy_expand_artifacts(tmp_path)
    res = run_script(["bash", str(CHECK_EXPAND_SH)], tmp_path)
    assert res["rc"] == 0, res["stdout"] + res["stderr"]
    out = res["stdout"]
    assert "choice contract" in out and "equivalence gate" in out
    assert "result: PASS" in out


def test_check_expand_sh_fails_on_bad_supernet(tmp_path):
    """缺陷 supernet → check 5b 失败 → check_expand 整体 FAIL（重印 gate 逐条原因）。"""
    write_toy_expand_artifacts(tmp_path, bad_supernet=True)
    res = run_script(["bash", str(CHECK_EXPAND_SH)], tmp_path)
    assert res["rc"] == 1
    combined = res["stdout"] + res["stderr"]
    assert "equivalence gate failed" in combined
    assert "键值不等于父权重" in combined  # gate 原因被重印供 agent 定位


def test_toy_supernet_source_is_spec_conformant_shape(tmp_path):
    """toy supernet 本身符合 spec 关键形态（分支集含 original、全 original 物化键 == 原模型）。

    这是对 fixture 自身的卫哨：若 fixture 坏掉，上面所有 gate 测试的结论都失效。
    """
    import importlib.util

    write_toy_expand_artifacts(tmp_path, with_inspect=False)
    sys.path.insert(0, str(tmp_path))  # load_pretrained sibling-import toy_flat
    for name in ("toy_flat", "supernet"):
        spec = importlib.util.spec_from_file_location(f"_{name}_guard", tmp_path / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"_{name}_guard"] = mod
        spec.loader.exec_module(mod)

    lp_spec = importlib.util.spec_from_file_location("_lp_guard", tmp_path / "load_pretrained.py")
    lp = importlib.util.module_from_spec(lp_spec)
    sys.modules["_lp_guard"] = lp
    lp_spec.loader.exec_module(lp)

    model = lp.build_pretrained_model()
    supernet = mod.build_supernet(pretrained_state=model.state_dict())
    ss = mod.SearchSpace()
    supernet.set_sample_config(ss.all_original())
    subnet = supernet.get_active_subnet()

    # 物化键契约：全 original 物化子网键集合 == 原模型键集合。
    assert set(subnet.state_dict()) == set(model.state_dict())
    # forward 等价（无 mask + causal mask 两用例）。
    import torch
    with torch.no_grad():
        for case in lp.build_probe_inputs():
            assert torch.allclose(model(**case), supernet(**case), atol=1e-5, rtol=1e-4)
    # freeze 分组：original 冻结 / 变体可训 / 固定模块冻结。
    for layer in supernet.layers:
        for bname, branch in layer.branches.items():
            for p in branch.parameters():
                assert p.requires_grad == (bname != "original")
    for p in supernet.embed.parameters():
        assert not p.requires_grad
    # TOY_SUPERNET_PY 与 good body 同源（bad 版另有专门测试）。
    assert "defect" not in TOY_SUPERNET_PY and "缺陷注入" not in TOY_SUPERNET_PY

"""test_kd_redesign.py —— KD-NAS 重构关键不变量测试（脚本 + YAML 级，无 GPU/真硬件）。

覆盖 spec-review 高优 finding：
- BLK-8：tune_latency 最小缩量（mock latency∝cfg 体量，断言刚跨 target 即停；贪心跳步实现会挂）
- BLK-17：distill_dispatch gate（noop|train）
- BLK-1/2：pick_variant KNOBS 校验（leverage rank / step<0）
- MED-4：pick_variant FAIL_latency 在 target 变化时重试；target-monotonic
- HI-2：tune_latency 每 build_model 前 seed（确定性，可复现）
- HI-11：kd agent.md 每个 `{{ <node>.output.<field> }}` 的 field ∈ 该 node output_schema
- teacher：10 层 t1/t2 交替
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KD = REPO / "workflows" / "agents" / "_kd_scripts"
KBDIR = REPO / "knowledge_base" / "families" / "receiver"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── teacher：10 层 t1/t2 交替 ──────────────────────────────────────────────────


def test_teacher_ten_blocks_alternating():
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "teacher_model"]:
        del sys.modules[m]
    from teacher_model import build_model, DUMMY_INPUT, BUILD_FN
    import torch
    t = build_model()
    blocks = list(t.main)
    assert len(blocks) == 10
    mt = [b.m_a.m_type for b in blocks]
    assert mt == ["t1", "t2"] * 5, f"非 t1/t2 交替: {mt}"
    assert BUILD_FN == "build_model"
    out = t(torch.randn(*DUMMY_INPUT["shape"]))
    assert out.shape == torch.Size(DUMMY_INPUT["shape"])
    assert t.feature_hook_names()  # KD feature 对齐


# ── BLK-17：distill_dispatch gate ──────────────────────────────────────────────


def test_distill_dispatch_gate():
    dd = _load(KD / "distill_dispatch.py", "_dd_test")
    assert dd.dispatch("ACCEPTED") == "train"
    assert dd.dispatch("FAIL_latency") == "noop"
    with pytest.raises(ValueError):
        dd.dispatch("BOGUS")


# ── BLK-1/2：pick_variant KNOBS 校验 ──────────────────────────────────────────


def test_pick_variant_rejects_bad_knobs(tmp_path, monkeypatch):
    pv = _load(KD / "pick_variant.py", "_pv_test")
    # 造一个 KNOBS 非法的变体（step>=0）
    bad = tmp_path / "bad_knobs.py"
    bad.write_text(
        "DUMMY_INPUT={'shape':[1,4,48,64,1],'dtype':'float32'}\n"
        "BUILD_FN='build_model'\n"
        "KNOBS={'num_blocks':{'default':3,'min':1,'step':1,'leverage':'high'}}\n"  # step>=0 非法
        "def build_model(**c):\n"
        "    import torch.nn as nn\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    # _validate_variant 应 raise（step>=0）
    import importlib.util
    spec = importlib.util.spec_from_file_location("bad_knobs", str(bad))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    with pytest.raises(ValueError, match="step"):
        pv._validate_variant(mod, str(bad))


def test_pick_variant_leverage_rank_order():
    """BLK-1：RANK 排序不是字母序（'low'<'medium'<'high' 反了）。"""
    from kd_common import RANK
    assert RANK["high"] < RANK["medium"] < RANK["low"]
    sorted_leverages = sorted(["high", "low", "medium"], key=lambda lv: RANK[lv])
    assert sorted_leverages == ["high", "medium", "low"], \
        "leverage 须按 high→medium→low 排，不是字母序"


# ── MED-4：FAIL_latency 在 target 变化时重试；target-monotonic ─────────────────


def test_pick_variant_fail_latency_retried_when_target_changes(tmp_path):
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "kd_common"]:
        del sys.modules[m]
    from kd_common import is_variant_done
    vsha = "abc"
    pid = "prov|1234"
    rows = [{"variant_id": "v1", "variant_sha256": vsha, "latency_provider_id": pid,
             "status": "FAIL_latency", "target_latency_ms": 8.0}]
    # 同 target(8) → done（跳过）
    assert is_variant_done(rows, 8.0, pid, vsha) is True
    # 改 target(5) → 不 done（重试）
    assert is_variant_done(rows, 5.0, pid, vsha) is False


def test_pick_variant_target_monotonic_success(tmp_path):
    """MED-4：SUCCESS 行 latency ≤ 当前 target → skip；target 调低到低于 latency → 重试。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "kd_common"]:
        del sys.modules[m]
    from kd_common import is_variant_done
    vsha = "abc"; pid = "p|1"
    ckpt = tmp_path / "c.pt"; ckpt.write_bytes(b"x" * 10)
    rows = [{"variant_id": "v1", "variant_sha256": vsha, "latency_provider_id": pid,
             "status": "SUCCESS", "latency_ms_median": 7.0, "ckpt": str(ckpt),
             "target_latency_ms": 10.0}]
    assert is_variant_done(rows, 8.0, pid, vsha) is True   # 7.0 ≤ 8 → skip
    assert is_variant_done(rows, 5.0, pid, vsha) is False  # 7.0 > 5 → 重试


def test_pick_variant_done_requires_sha_and_provider_match(tmp_path):
    """BLK-12/HI-12：variant_sha256 / latency_provider_id 不匹配 → 不 done（重做）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "kd_common"]:
        del sys.modules[m]
    from kd_common import is_variant_done
    ckpt = tmp_path / "c.pt"; ckpt.write_bytes(b"x" * 10)
    rows = [{"variant_id": "v1", "variant_sha256": "oldsha", "latency_provider_id": "p|1",
             "status": "SUCCESS", "latency_ms_median": 5.0, "ckpt": str(ckpt)}]
    assert is_variant_done(rows, 8.0, "p|1", "newsha") is False   # sha 不匹配
    assert is_variant_done(rows, 8.0, "p|2", "oldsha") is False   # provider 不匹配


# ── BLK-8：tune_latency 最小缩量（mock，无真 ONNX/硬件）────────────────────────


def test_tune_minimal_shrink_stops_at_first_crossing(tmp_path, monkeypatch):
    """BLK-8：latency∝cfg 体量，断言刚跨 target 即停（不过度缩）。贪心跳步实现会挂。"""
    tune = _load(KD / "tune_latency.py", "_tune_test")

    def magnitude(cfg):
        # num_blocks 权重远大于 embed_dim（模拟 block 数对 latency 的高 leverage）
        return cfg.get("num_blocks", 3) * 100000 + cfg.get("embed_dim", 16) * 1000

    # mock export_onnx：写一个 size ∝ magnitude 的假 onnx 文件
    fake_exp = types.ModuleType("export_onnx")
    def _fake_export(model_path, build_fn, dummy_input, opset, out, device="auto",
                     no_external_data=True, seed=0, build_kwargs=None):
        cfg = build_kwargs or {}
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"x" * magnitude(cfg))
        return out
    fake_exp.export_onnx = _fake_export
    monkeypatch.setitem(sys.modules, "export_onnx", fake_exp)

    # mock measure：latency = onnx 文件 size / 100000 → = num_blocks + embed_dim/100
    def _mock_measure(onnx, device=None):
        return os.path.getsize(onnx) / 100000.0
    monkeypatch.setattr(tune, "_load_measure", lambda provider: _mock_measure)

    knobs = {"num_blocks": {"default": 3, "min": 1, "step": -1, "leverage": "high"},
             "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"}}
    # default latency = 3 + 16/100 = 3.16；target=2.0 → 缩 num_blocks 到 1（latency 1.16）即停。
    res = tune.tune_latency(
        variant_path=str(KBDIR / "spt_t1.py"), build_fn="build_model",
        dummy_input='{"shape":[1,4,48,64,1],"dtype":"float32"}', knobs=knobs,
        target_latency_ms=2.0, latency_provider="mock::measure",
        artifacts_dir=str(tmp_path), max_measurements=40, measure_repeats=1,
        device="cpu", seed=0, opset=17,
    )
    assert res["status"] == "ACCEPTED"
    # 最小缩量：num_blocks 缩到 1 即跨 target（1.16 ≤ 2），embed_dim **不应**被缩（仍 16）。
    assert res["accepted_cfg"]["num_blocks"] == 1, res
    assert res["accepted_cfg"]["embed_dim"] == 16, f"过度缩容 embed_dim：{res['accepted_cfg']}"


def test_tune_fail_latency_when_unreachable(tmp_path, monkeypatch):
    """target 低于所有可达 latency → FAIL_latency（耗尽 knob 地板）。"""
    tune = _load(KD / "tune_latency.py", "_tune_test2")
    fake_exp = types.ModuleType("export_onnx")
    fake_exp.export_onnx = lambda **kw: Path(kw["out"]).write_text("x") or kw["out"]
    monkeypatch.setitem(sys.modules, "export_onnx", fake_exp)
    monkeypatch.setattr(tune, "_load_measure", lambda p: (lambda onnx, device=None: 100.0))  # 恒高
    knobs = {"num_blocks": {"default": 3, "min": 1, "step": -1, "leverage": "high"}}
    res = tune.tune_latency(
        variant_path=str(KBDIR / "spt_t1.py"), build_fn="build_model",
        dummy_input='{"shape":[1,4,48,64,1],"dtype":"float32"}', knobs=knobs,
        target_latency_ms=0.5, latency_provider="mock::measure",
        artifacts_dir=str(tmp_path), max_measurements=40, measure_repeats=1,
        device="cpu", seed=0, opset=17,
    )
    assert res["status"] == "FAIL_latency"


# ── HI-11：kd agent.md field 引用 ∈ output_schema ─────────────────────────────


def test_kd_agent_md_output_refs_in_schema():
    """每个 kd agent.md 的 `{{ <node>.output.<field> }}` 的 field 须 ∈ 该 node output_schema。"""
    import re
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    schema_fields = {}
    node_names = set()
    for n in wf.nodes:
        node_names.add(n.name)
        props = set((n.output_schema or {}).get("properties", {}).keys())
        schema_fields[n.name] = props
    ref_pat = re.compile(r"\{\{\s*(\w+)\.output\.(\w+)\s*\}\}")
    agent_dir = REPO / "workflows" / "agents"
    bad = []
    for agent_md in agent_dir.glob("kd-*/agent.md"):
        text = agent_md.read_text(encoding="utf-8")
        for node, field in ref_pat.findall(text):
            if node not in node_names:
                continue  # 非 kd 节点引用（如 workflow.outputs）忽略
            if field not in schema_fields.get(node, set()):
                bad.append((agent_md.name, node, field))
    assert not bad, f"agent.md 引用了 output_schema 外的字段：{bad}"


# ── code-reviewer 🔴 回归守门 ─────────────────────────────────────────────────


def test_train_adapter_no_student_family_regression():
    """🔴 回归：train_adapter_template 不再引用已删的 args.student_family（ckpt 改用 variant_id）。"""
    src = (KD / "train_adapter_template.py").read_text(encoding="utf-8")
    assert "args.student_family" not in src, \
        "student_family 残留（--student_family 已删，ckpt 保存会 AttributeError）"
    assert '"variant_id": args.variant_id' in src


def test_train_adapter_loop_no_placeholder_leak():
    """🔴 + BLK-4：训练循环不链入硬编码 shape 的 placeholder dataloader。"""
    src = (KD / "train_adapter_template.py").read_text(encoding="utf-8")
    for_lines = [l for l in src.splitlines() if "for batch_idx" in l]
    assert for_lines, "缺少训练循环"
    assert all("_placeholder_user_dataloader" not in l for l in for_lines), \
        f"训练循环链入 placeholder（BLK-4 硬编码 shape 泄漏）：{for_lines}"
    assert any("iter(dl)" in l for l in for_lines), "主循环应直接 iter(dl) 每 epoch 重新迭代"


def test_kd_setup_ledger_not_truncated():
    """🔴 回归：kd-setup agent.md 不含无条件 ledger 截断（`: > ledger` 会让跨 run 复用失效）。"""
    src = (REPO / "workflows" / "agents" / "kd-setup" / "agent.md").read_text(encoding="utf-8")
    # create-if-absent 守卫必须在
    assert '[ -f "$LEDGER_PATH" ] || : > "$LEDGER_PATH"' in src, "应用 create-if-absent 守卫"
    # 不应有独立的「无条件截断」整行
    standalone = [l.strip() for l in src.splitlines() if l.strip() == ': > "$LEDGER_PATH"']
    assert not standalone, f"无条件截断 ledger（破坏跨 run 复用）：{standalone}"


def test_acquire_run_lock_idempotent_and_rejects_other(tmp_path):
    """BLK-13：同 run_id 幂等刷新；异 run_id（新鲜）拒绝。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "kd_common"]:
        del sys.modules[m]
    from kd_common import acquire_run_lock
    art = tmp_path / "art"
    art.mkdir()
    acquire_run_lock(str(art), "runA")
    acquire_run_lock(str(art), "runA")  # 同 run_id 幂等
    with pytest.raises(RuntimeError, match="另一 run"):
        acquire_run_lock(str(art), "runB")  # 异 run_id 新鲜 → 拒绝


# ── 并行训练脚本：FAIL_latency 编排 + ledger 写 + done-skip ──────────────────────


def test_parallel_script_fail_latency_orchestration(tmp_path):
    """train_variants_parallel：并发跑 2 变体（mock 高 latency → 全 FAIL_latency，不触发 train），
    验证并行编排 + 共享 ledger 写 + 身份字段 + 二次跑 done-skip。"""
    import subprocess
    # mock 高 latency provider（target 低 → FAIL_latency，不走 train_kd/teacher_cache）
    prov = tmp_path / "_mock_hi.py"
    prov.write_text("def measure(onnx, device=None):\n    return 100.0\n", encoding="utf-8")
    artifacts = tmp_path / "art"; artifacts.mkdir()
    per_run = tmp_path / "per"; per_run.mkdir()
    ledger = artifacts / "ledger.jsonl"
    common = [
        "--receiver_dir", str(KBDIR), "--ledger", str(ledger),
        "--target_latency_ms", "0.5", "--latency_provider", str(prov) + "::measure",
        "--accuracy_baseline", "0.02", "--test_command", "echo NMSE: 0.02",
        "--teacher_cache", "/dev/null/nonexistent.pt",
        "--kd_scripts_dir", str(KD), "--artifacts_dir", str(artifacts),
        "--per_run_artifacts_dir", str(per_run), "--project_root", str(tmp_path),
        "--concurrency", "2", "--max_measurements", "5", "--measure_repeats", "1", "--device", "cpu",
    ]
    r1 = subprocess.run([sys.executable, str(KD / "train_variants_parallel.py")] + common,
                        capture_output=True, text=True)
    assert r1.returncode == 0, r1.stderr
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 2, f"expect 2 rows, got {len(rows)}"
    assert {r["variant_id"] for r in rows} == {"spt_t1", "spt_alt"}
    assert all(r["status"] == "FAIL_latency" for r in rows)
    assert all(r["variant_sha256"] and r["latency_provider_id"] for r in rows), "缺跨 run 身份字段"

    # 二次跑：done 谓词应跳过，ledger 不增长
    r2 = subprocess.run([sys.executable, str(KD / "train_variants_parallel.py")] + common,
                        capture_output=True, text=True)
    assert r2.returncode == 0, r2.stderr
    rows2 = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows2) == 2, f"2nd run should skip done (ledger 仍 2 行)，got {len(rows2)}"
    assert "SKIPPED_DONE" in (r2.stdout + r2.stderr), "2nd run 应报 SKIPPED_DONE"

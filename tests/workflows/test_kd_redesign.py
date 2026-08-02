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


# ── 回归：student feature_hook_names 恒与 teacher 等长 ─────────────────────────
# _model8_blocks.feature_hook_names 在 num_blocks=1 时曾返回 1 个 hook，与固定
# 2-hook 的 teacher 长度不等 → compose.prepare 对 OFD/FitNets/RKD raise length-mismatch。
# 修复后 student 恒返回 2 个（n=1 时第二个重复 main.0，单 block 无中间层）。

def test_student_feature_hooks_match_teacher_length():
    for m in [n for n in sys.modules if n in ("_model8_blocks", "spt_t1", "teacher_model")]:
        del sys.modules[m]
    sys.path.insert(0, str(KD))
    sys.path.insert(0, str(KBDIR))
    from teacher_model import build_model as build_teacher
    from spt_t1 import build_model as build_student

    t_hooks = build_teacher().feature_hook_names()
    for n_blocks in (1, 2, 3):
        s = build_student(num_blocks=n_blocks)
        hooks = s.feature_hook_names()
        assert len(hooks) == len(t_hooks), (
            f"num_blocks={n_blocks}: student {len(hooks)} hooks ≠ teacher {len(t_hooks)}; "
            f"OFD/FitNets/RKD 的 prepare 会 raise length-mismatch"
        )
        assert "main.0" in hooks


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
    """🔴 回归（v4 退役文件）：_deprecated/train_adapter_template 不再引用已删的 args.student_family
    （ckpt 改用 variant_id）。文件已退役到 _deprecated/，属性仍守门防回潮。"""
    src = (KD / "_deprecated" / "train_adapter_template.py").read_text(encoding="utf-8")
    assert "args.student_family" not in src, \
        "student_family 残留（--student_family 已删，ckpt 保存会 AttributeError）"
    assert '"variant_id": args.variant_id' in src


def test_train_adapter_loop_no_placeholder_leak():
    """🔴 + BLK-4（v4 退役文件）：训练循环不链入硬编码 shape 的 placeholder dataloader。"""
    src = (KD / "_deprecated" / "train_adapter_template.py").read_text(encoding="utf-8")
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


# ── v2 gate_all.py：串行 gate + FAIL_latency 增量落账 + manifest + done-skip ─────


def _fake_receiver(tmp_path: Path, names: tuple[str, ...] = ("v_a", "v_b")) -> Path:
    """造一个临时 receiver 目录（避免跑全部 11 个真变体；不带 _model8_blocks 依赖）。"""
    recv = tmp_path / "receiver"
    recv.mkdir()
    for name in names:
        (recv / f"{name}.py").write_text(
            "DUMMY_INPUT={'shape':[1,4,48,64,1],'dtype':'float32'}\n"
            "BUILD_FN='build_model'\n"
            "KNOBS={'num_blocks':{'default':3,'min':1,'step':-1,'leverage':'high'}}\n"
            "def build_model(**c):\n"
            "    import torch.nn as nn\n    return nn.Identity()\n",
            encoding="utf-8",
        )
    return recv


def test_gate_all_fail_latency_incremental_ledger_and_manifest(tmp_path):
    """gate_all：2 假变体 + mock 高 latency → 全 FAIL_latency → 增量 ledger 行 + 空 manifest。
    二次跑：done-skip（SKIPPED_DONE），ledger 不增长。验证 v2 「串行 gate + 当场落账 + manifest」意图。"""
    import subprocess
    recv = _fake_receiver(tmp_path)
    prov = tmp_path / "_mock_hi.py"
    prov.write_text("def measure(onnx, device=None):\n    return 100.0\n", encoding="utf-8")
    artifacts = tmp_path / "art"; artifacts.mkdir()
    ledger = artifacts / "ledger.jsonl"
    manifest = artifacts / "gate_manifest.json"

    common = [
        "--receiver_dir", str(recv), "--ledger", str(ledger),
        "--target_latency_ms", "0.5", "--latency_provider", str(prov) + "::measure",
        "--artifacts_dir", str(artifacts), "--kd_scripts_dir", str(KD),
        "--accuracy_baseline", "0.02",
        "--latency_tune_budget", "5", "--measure_repeats", "1",
        "--device", "cpu", "--manifest_out", str(manifest),
    ]
    r1 = subprocess.run([sys.executable, str(KD / "gate_all.py")] + common,
                        capture_output=True, text=True)
    assert r1.returncode == 0, r1.stderr
    # stdout emit 契约
    assert "N_ACCEPTED: 0" in r1.stdout, f"应无 ACCEPTED：{r1.stdout}"
    assert "N_FAIL_LATENCY: 2" in r1.stdout, f"应 2 FAIL_latency：{r1.stdout}"
    assert "ALL_PROCESSED: true" in r1.stdout
    assert f"ACCEPTED_MANIFEST_PATH: {manifest}" in r1.stdout
    # ledger 增量落账：2 行 FAIL_latency + 跨 run 身份字段
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 2, f"expect 2 rows, got {len(rows)}"
    assert {r["variant_id"] for r in rows} == {"v_a", "v_b"}
    assert all(r["status"] == "FAIL_latency" for r in rows)
    assert all(r["variant_sha256"] and r["latency_provider_id"] for r in rows), "缺跨 run 身份字段"
    assert all(r["target_latency_ms"] == 0.5 for r in rows)
    # manifest 是空 list（v_a/v_b 全 FAIL_latency，无 ACCEPTED）
    assert json.loads(manifest.read_text(encoding="utf-8")) == []

    # 二次跑：done 谓词应跳过，ledger 不增长
    r2 = subprocess.run([sys.executable, str(KD / "gate_all.py")] + common,
                        capture_output=True, text=True)
    assert r2.returncode == 0, r2.stderr
    assert "SKIPPED_DONE: 2" in r2.stdout, "2nd run 应报 SKIPPED_DONE: 2"
    rows2 = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows2) == 2, f"2nd run should skip done，got {len(rows2)}"


def test_gate_all_accepted_collects_into_manifest(tmp_path):
    """gate_all：target 极高 → 全部 ACCEPTED → 不落账，manifest 收集 2 entries（字段齐全）。
    验证 ACCEPTED 分支：「不写 ledger，entry 进 manifest 交 train」。"""
    import subprocess
    recv = _fake_receiver(tmp_path, names=("v_a", "v_b"))
    # latency = onnx 文件大小 / 100（Identity 模型 onnx 大小稳定可控）
    prov = tmp_path / "_mock_sz.py"
    prov.write_text(
        "import os\n"
        "def measure(onnx, device=None):\n"
        "    return os.path.getsize(onnx) / 100.0\n",
        encoding="utf-8",
    )
    artifacts = tmp_path / "art"; artifacts.mkdir()
    ledger = artifacts / "ledger.jsonl"
    manifest = artifacts / "gate_manifest.json"
    common = [
        "--receiver_dir", str(recv), "--ledger", str(ledger),
        "--target_latency_ms", "1000000.0",  # 极高 → latency 永远 < target → 全 ACCEPTED
        "--latency_provider", str(prov) + "::measure",
        "--artifacts_dir", str(artifacts), "--kd_scripts_dir", str(KD),
        "--accuracy_baseline", "0.02",
        "--latency_tune_budget", "5", "--measure_repeats", "1",
        "--device", "cpu", "--manifest_out", str(manifest),
    ]
    r = subprocess.run([sys.executable, str(KD / "gate_all.py")] + common,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # target 极高 → 全部 ACCEPTED，无 FAIL_latency 落账
    assert "N_ACCEPTED: 2" in r.stdout, f"应 2 ACCEPTED：{r.stdout}"
    assert "N_FAIL_LATENCY: 0" in r.stdout
    # ledger 不增长（ACCEPTED 不落账，交 train）
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if ledger.exists() else []
    assert rows == [], f"ACCEPTED 不应落账，got {rows}"
    # manifest 收集 2 entries，字段齐全
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    assert len(entries) == 2
    by_id = {e["variant_id"]: e for e in entries}
    for vid in ("v_a", "v_b"):
        e = by_id[vid]
        for field in ("variant_id", "variant_path", "variant_sha256", "accepted_cfg",
                       "latency_ms_median", "latency_ms_std", "build_fn", "dummy_input", "knobs"):
            assert field in e, f"manifest[{vid}] 缺 {field}"
        assert e["latency_ms_median"] > 0


# ── v2 gpu_probe.py：并发公式 + round-robin + fail-soft + 契约校验 ─────────────────


_GB = 1024 ** 3


def test_gpu_probe_compute_concurrency_formula():
    """concurrency = max(1, floor(free*safety/per_variant))，cap 到 min(variants, max_conc)。"""
    gp = _load(KD / "gpu_probe.py", "_gp_formula")
    # free=20GB, per=4GB, safety=0.8 → floor(16/4)=4
    assert gp.compute_concurrency(total_free_bytes=20 * _GB, per_variant_bytes=4 * _GB,
                                  safety=0.8, variants_count=10, max_concurrency=8) == 4
    # cap 到 variants_count=2
    assert gp.compute_concurrency(total_free_bytes=10 ** 12, per_variant_bytes=1,
                                  safety=0.8, variants_count=2, max_concurrency=8) == 2
    # cap 到 max_concurrency=3
    assert gp.compute_concurrency(total_free_bytes=10 ** 12, per_variant_bytes=1,
                                  safety=0.8, variants_count=100, max_concurrency=3) == 3
    # per_variant 极大 → floor=0 → max(1,0)=1
    assert gp.compute_concurrency(total_free_bytes=1000, per_variant_bytes=10 ** 12,
                                  safety=0.8, variants_count=10, max_concurrency=8) == 1


def test_gpu_probe_build_device_plan_round_robin():
    """多卡 round-robin：3 worker × 2 卡 → [cuda:0, cuda:1, cuda:0]。"""
    gp = _load(KD / "gpu_probe.py", "_gp_plan")
    assert gp.build_device_plan(concurrency=3, n_gpus=2, backend="cuda") == \
        ["cuda:0", "cuda:1", "cuda:0"]
    # 单卡 → 全 cuda:0
    assert gp.build_device_plan(concurrency=4, n_gpus=1, backend="cuda") == \
        ["cuda:0"] * 4
    # 0 卡 → [""] * concurrency（fail-soft 串行）
    assert gp.build_device_plan(concurrency=2, n_gpus=0, backend="cuda") == ["", ""]


def test_gpu_probe_fail_soft_on_cpu_device(tmp_path):
    """--device cpu → 立即 fail-soft（无 VRAM 概念），exit 0 + CONCURRENCY=1。"""
    import subprocess
    tc = tmp_path / "tc.pt"; tc.write_bytes(b"x" * 10)
    r = subprocess.run([
        sys.executable, str(KD / "gpu_probe.py"),
        "--teacher_cache", str(tc),
        "--representative_variant", str(KBDIR / "spt_t1.py"),
        "--variants_count", "5", "--device", "cpu",
    ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "CONCURRENCY: 1" in r.stdout
    assert 'DEVICE_PLAN: [""]' in r.stdout
    assert "PER_VARIANT_VRAM_BYTES: 0" in r.stdout
    assert "WARN" in r.stdout  # fail-soft 必含 WARN


def test_gpu_probe_fail_soft_on_auto_no_cuda(tmp_path):
    """--device auto 但无 CUDA（CI 环境）→ fail-soft exit 0。"""
    import subprocess
    tc = tmp_path / "tc.pt"; tc.write_bytes(b"x" * 10)
    r = subprocess.run([
        sys.executable, str(KD / "gpu_probe.py"),
        "--teacher_cache", str(tc),
        "--representative_variant", str(KBDIR / "spt_t1.py"),
        "--variants_count", "5", "--device", "auto",
    ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "CONCURRENCY: 1" in r.stdout
    assert "WARN" in r.stdout


def test_gpu_probe_contract_violation_representative_missing_build_model(tmp_path):
    """representative 缺 build_model → AttributeError（输入契约不符 → fail loud 在 _main 包成 exit 2）。"""
    gp = _load(KD / "gpu_probe.py", "_gp_contract")
    bad = tmp_path / "no_build.py"
    bad.write_text("DUMMY_INPUT={'shape':[1,4,48,64,1],'dtype':'float32'}\n", encoding="utf-8")
    mod = gp._load_variant_module(str(bad))
    with pytest.raises(AttributeError, match="build_model"):
        gp._build_representative(mod, str(bad))


def test_gpu_probe_contract_violation_missing_dummy_shape(tmp_path):
    """representative 缺 DUMMY_INPUT.shape → ValueError（契约不符）。"""
    gp = _load(KD / "gpu_probe.py", "_gp_contract2")
    bad = tmp_path / "no_shape.py"
    bad.write_text("def build_model():\n    import torch.nn as nn\n    return nn.Identity()\n",
                   encoding="utf-8")
    mod = gp._load_variant_module(str(bad))
    with pytest.raises(ValueError, match="shape"):
        gp._dummy_input(mod, str(bad))


# ── v2 train_pool.py：VRAM 再校验纯函数 + 空 manifest + 增量账本 ────────────────────


def test_train_pool_revalidate_vram_no_cuda_trusts_setup():
    """device_plan 无 cuda → 信任 setup（effective = len(plan)，warn 空）。"""
    tp = _load(KD / "train_pool.py", "_tp_reval_1")
    eff, warn = tp.revalidate_vram(["", ""], per_variant_vram_bytes=10 ** 9, safety=0.8)
    assert eff == 2
    assert warn == ""


def test_train_pool_revalidate_vram_per_variant_zero_trusts_setup():
    """per_variant_vram_bytes<=0（setup 无 CUDA）→ 信任 setup + warn。"""
    tp = _load(KD / "train_pool.py", "_tp_reval_2")
    eff, warn = tp.revalidate_vram(["cuda:0"], per_variant_vram_bytes=0, safety=0.8)
    assert eff == 1
    assert "per_variant_vram_bytes<=0" in warn


def test_train_pool_revalidate_vram_degrades_when_low(monkeypatch):
    """mock mem_get_info：free 不足 per_variant → effective 降级到 floor(free*safety/per_variant)。"""
    tp = _load(KD / "train_pool.py", "_tp_reval_3")
    fake_torch = types.ModuleType("torch")
    fake_cuda = types.ModuleType("torch.cuda")
    fake_cuda.is_available = lambda: True
    # 每卡 free=10GB，per_variant=4GB，safety=0.8 → 每卡 floor(8/4)=2，2 卡合计 4
    fake_cuda.mem_get_info = lambda idx: (10 * _GB, 20 * _GB)
    fake_torch.cuda = fake_cuda
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    eff, warn = tp.revalidate_vram(["cuda:0", "cuda:1"],
                                   per_variant_vram_bytes=4 * _GB, safety=0.8)
    assert eff == 4  # 2 per card * 2 cards
    assert warn == ""


def test_train_pool_revalidate_vram_zero_when_oversubscribed(monkeypatch):
    """free 连 1 个 variant 都放不下 → effective=0（caller train_pool _main fail loud 退 2）。"""
    tp = _load(KD / "train_pool.py", "_tp_reval_4")
    fake_torch = types.ModuleType("torch")
    fake_cuda = types.ModuleType("torch.cuda")
    fake_cuda.is_available = lambda: True
    fake_cuda.mem_get_info = lambda idx: (1 * _GB, 20 * _GB)  # 1GB free per card
    fake_torch.cuda = fake_cuda
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    # per_variant=2GB, safety=0.8 → floor(0.8/2)=0 per card → 0 total
    eff, warn = tp.revalidate_vram(["cuda:0", "cuda:1"],
                                   per_variant_vram_bytes=2 * _GB, safety=0.8)
    assert eff == 0


def test_train_pool_empty_manifest_emits_success(tmp_path):
    """gate 全 FAIL_latency（manifest=[]）→ train_pool 空批不算错，emit SWEEP_STATUS: SUCCESS。"""
    import subprocess
    artifacts = tmp_path / "art"; artifacts.mkdir()
    ledger = artifacts / "ledger.jsonl"
    manifest = artifacts / "gate_manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    r = subprocess.run([
        sys.executable, str(KD / "train_pool.py"),
        "--manifest", str(manifest), "--ledger", str(ledger),
        "--teacher_cache", "/dev/null/nonexistent.pt",
        "--kd_scripts_dir", str(KD), "--artifacts_dir", str(artifacts),
        "--per_run_artifacts_dir", str(tmp_path),
        "--project_root", str(tmp_path),
        "--accuracy_baseline", "0.02",
        "--latency_provider", str(tmp_path / "p.py::m"),
        "--target_latency_ms", "8",
        "--concurrency", "1", "--device_plan", '[""]',
        "--per_variant_vram_bytes", "0",
        "--train_pipeline_path", str(tmp_path / "train_pipeline.py"),
    ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "SWEEP_STATUS: SUCCESS" in r.stdout
    assert "VARIANTS_DONE: 0" in r.stdout


def test_train_pool_incremental_ledger_helper(tmp_path):
    """kd_common.append_ledger_row 逐行 write+flush（crash-safe）：3 行各自原子 append。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "kd_common"]:
        del sys.modules[m]
    from kd_common import append_ledger_row
    ledger = tmp_path / "ledger.jsonl"
    for i in range(3):
        append_ledger_row(str(ledger), {"variant_id": f"v{i}", "status": "SUCCESS"})
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for i, line in enumerate(lines):
        row = json.loads(line)
        assert row["variant_id"] == f"v{i}"


def test_train_pool_main_injects_orca_kd_scripts_dir(tmp_path, monkeypatch):
    """v4：train_pool._main 必须设 os.environ['ORCA_KD_SCRIPTS_DIR'] = kd_scripts_dir，
    让 worker 子进程（train_pipeline.py，落盘在 per-run artifacts）能 import kd.*。
    run_subproc 继承 os.environ，故 env 注入是 distill 模式能跑的唯一机制。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n in ("train_pool", "kd_common")]:
        del sys.modules[m]
    import train_pool as tp

    monkeypatch.delenv("ORCA_KD_SCRIPTS_DIR", raising=False)
    # 空 manifest → _main 早退（n_accepted=0），但 ctx 构建 + env 注入在 pool 之前
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "train_pool.py",
        "--manifest", str(manifest), "--ledger", str(tmp_path / "ledger.jsonl"),
        "--teacher_cache", "/dev/null/nonexistent.pt",
        "--kd_scripts_dir", str(KD), "--artifacts_dir", str(tmp_path),
        "--per_run_artifacts_dir", str(tmp_path), "--project_root", str(tmp_path),
        "--train_pipeline_path", str(tmp_path / "train_pipeline.py"),
        "--accuracy_baseline", "0.02",
        "--latency_provider", str(tmp_path / "p.py::m"),
        "--target_latency_ms", "8",
        "--concurrency", "1", "--device_plan", '[""]',
        "--per_variant_vram_bytes", "0",
    ])
    rc = tp._main()
    assert rc == 0
    import os
    assert os.environ.get("ORCA_KD_SCRIPTS_DIR") == str(KD), (
        "train_pool._main 必须注入 ORCA_KD_SCRIPTS_DIR（worker 子进程 import kd.* 依赖此 env）"
    )


# ── v2 DAG：yaml 节点 + 路由（n_accepted==0 → $end）─────────────────────────────


def test_kd_dag_flatten_setup_gate_train():
    """DAG：7 节点 flatten→teacher-gen→train-script-gen→setup→gate→train→select→$end。

    flatten 是入口；teacher-gen 纯调参派生 teacher；train-script-gen 生成统一 train_pipeline.py；
    setup 跑 teacher 训 + teacher_cache + GPU 预检；select（finalize 新增）读 ledger 出最终报告。
    """
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    names = [n.name for n in wf.nodes]
    assert names == ["flatten", "teacher_gen", "train_script_gen", "setup", "gate",
                     "train", "select"]
    # entry 是 flatten
    assert wf.entry == "flatten"
    # flatten 恒到 teacher_gen
    assert [r.to for r in wf.nodes[0].routes] == ["teacher_gen"]
    # teacher_gen 恒到 train_script_gen
    assert [r.to for r in wf.nodes[1].routes] == ["train_script_gen"]
    # train_script_gen 恒到 setup
    assert [r.to for r in wf.nodes[2].routes] == ["setup"]
    # setup 恒到 gate
    assert [r.to for r in wf.nodes[3].routes] == ["gate"]
    # gate 首条 when = n_accepted==0 → $end；兜底 → train
    gate_routes = wf.nodes[4].routes
    assert gate_routes[0].to.endswith("end") or gate_routes[0].to == "$end"
    assert "n_accepted" in (gate_routes[0].when or "")
    assert gate_routes[1].to == "train"
    # train 恒到 select（finalize：train → select → $end）
    assert [r.to for r in wf.nodes[5].routes] == ["select"]
    # select 恒到 $end（末尾节点）
    assert [r.to for r in wf.nodes[6].routes] == ["$end"]


def test_kd_dag_teacher_gen_and_train_script_gen_output_schemas():
    """v4：teacher-gen + train-script-gen 节点 output_schema 必须暴露下游消费的字段。

    teacher-gen → setup 消费 teacher_model_path + teacher_latency_ms；
    train-script-gen → setup 消费 train_pipeline_path + train 消费 train_pipeline_path。
    """
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    by_name = {n.name: n for n in wf.nodes}
    # teacher_gen output_schema（节点名用下划线——Jinja 标识符不能含连字符）
    tgen = by_name["teacher_gen"]
    tgen_props = set(tgen.output_schema.get("properties", {}).keys())
    for f in ("teacher_model_path", "teacher_latency_ms", "project_root",
              "depth_axis", "width_axis"):
        assert f in tgen_props, f"teacher_gen output_schema 缺 {f}"
    # train_script_gen output_schema
    tsg = by_name["train_script_gen"]
    tsg_props = set(tsg.output_schema.get("properties", {}).keys())
    assert "train_pipeline_path" in tsg_props, "train_script_gen output_schema 缺 train_pipeline_path"


def test_kd_dag_flatten_output_schema_contract():
    """flatten output_schema 必须暴露 baseline_contract_path / project_root / model_name /
    flat_artifacts_dir / baseline_latency_ms 五字段——kd-setup step1/step2 直接取
    baseline_contract_path + project_root + baseline_latency_ms（latency 下沉到 flatten __main__）。"""
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    flatten = wf.nodes[0]
    assert flatten.name == "flatten"
    props = set(flatten.output_schema.get("properties", {}).keys())
    required = set(flatten.output_schema.get("required", []))
    for f in ("baseline_contract_path", "project_root", "model_name",
              "flat_artifacts_dir", "baseline_latency_ms"):
        assert f in props, f"flatten output_schema 缺 {f}"
        assert f in required, f"flatten output_schema 应 required {f}（下游 setup 强依赖）"
    # baseline_latency_ms 是 number（latency 实测值，不编造）
    assert props and flatten.output_schema["properties"]["baseline_latency_ms"]["type"] == "number"


def test_kd_inputs_slammed_remove_advanced_defaults():
    """输入瘦身（用户已定）：seed / kd_artifacts_dir / latency_tune_budget /
    kd_force_rerun 不再从 inputs 注入——下游 CLI 用脚本默认。防止静默回潮。

    注意 ``accuracy_baseline_kind`` 在 KD-NAS finalize（2026-07-31）**加回** inputs：
    方向须用户显式声明（measure_student / viz_kd / kd-select 三处同源，禁 auto 猜，
    防 -20dB 误判优于 -22dB 的方向反转）——故它现在是必填 [ask]，不在 removed 集合里。
    """
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    actual = set((wf.inputs or {}).keys())
    removed = {
        "seed", "kd_artifacts_dir", "latency_tune_budget", "kd_force_rerun",
    }
    leaked = actual & removed
    assert not leaked, (
        f"kd-nas.yaml inputs 含已下沉 input {sorted(leaked)}（应改为下游 CLI 默认）。"
    )
    # 必填 Tier A 不动（v4: teacher_train_command 改名 user_train_script）
    for must in ("user_train_script", "target_latency_ms",
                 "accuracy_baseline", "accuracy_baseline_kind",
                 "baseline_model_path", "latency_provider"):
        assert must in actual, f"必填 input {must} 被误删"
    # accuracy_baseline_kind 必填（finalize 加回：显式方向驱动，单一真相源）
    assert wf.inputs["accuracy_baseline_kind"].required is True, (
        "accuracy_baseline_kind 必须 required=True（finalize：方向禁 auto 猜）"
    )
    # v4: teacher_train_command 已改名 user_train_script（防回潮）
    assert "teacher_train_command" not in actual, (
        "teacher_train_command 已改名 user_train_script（v4），不应残留"
    )
    # advanced 保留：device + full_epochs
    assert "device" in actual and "full_epochs" in actual


def test_kd_setup_agent_md_consumes_flatten_output():
    """kd-setup step1/step2 必须从 flatten.output 取 baseline_contract_path
    （而非 inputs.baseline_model_path——该 input 现在是 flatten 的入口，setup 不直消费）。"""
    text = (REPO / "workflows" / "agents" / "kd-setup" / "agent.md").read_text(encoding="utf-8")
    assert "flatten.output.baseline_contract_path" in text, (
        "kd-setup/agent.md 必须从 flatten.output.baseline_contract_path 取 baseline（而非 inputs）"
    )
    # 反向断言：不应再直接消费 inputs.baseline_model_path（被 flatten 取代）
    # 允许在注释里提到历史 input 名，但实际取值必须走 flatten.output.*
    for line in text.splitlines():
        if "baseline_model_path" in line and "inputs.baseline_model_path" in line:
            # 允许：注释行（# 开头）解释迁移
            assert line.strip().startswith("#") or "改" in line or "迁移" in line, (
                f"kd-setup/agent.md 仍直接消费 inputs.baseline_model_path（应改 flatten.output）：{line!r}"
            )


def test_kd_setup_emits_concurrency_fields():
    """setup output_schema 含 v2 新增并发字段（concurrency / device_plan / per_variant_vram_bytes）。"""
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    setup = next(n for n in wf.nodes if n.name == "setup")
    setup_props = set(setup.output_schema.get("properties", {}).keys())
    for f in ("concurrency", "device_plan", "per_variant_vram_bytes", "gpu_report"):
        assert f in setup_props, f"setup output_schema 缺 {f}"
    # 必填不含 gpu_report（fail-soft 可空）
    required = set(setup.output_schema.get("required", []))
    assert "gpu_report" not in required
    assert "concurrency" in required  # setup 必算（即便 1）
    # v4 反向守门：user_train_import / user_loss_fn 已从 setup output 移除
    # （loss/dataloader 适配下沉给 train-script-gen，setup 不再 grep-user-train）。防回潮。
    for removed in ("user_train_import", "user_loss_fn"):
        assert removed not in setup_props, (
            f"setup output_schema 不应再有 {removed}（v4 下沉给 train-script-gen）"
        )


# ── 🔴 回归守门：wf.outputs 在 gate→$end（n_accepted==0）路由下不崩 ──────────────
# code-reviewer 🔴-1：原 yaml 引用 train.output.X，gate 路由 $end 时 train.output 不存在
# （不是 None，是 missing）→ StrictUndefined raise → workflow_failed。v2 修复：outputs 只引
# setup + gate（恒跑）。本测试驱动 render 模拟 gate→$end（train missing）断言不崩。


def test_wf_outputs_renders_when_gate_routes_to_end():
    """gate→$end 路径（n_accepted==0，train 未跑）：wf.outputs 各模板都能渲染，不 raise。"""
    from orca.compile.parser import load_workflow
    from orca.exec.render import render_template
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    # 模拟 setup + gate 都跑了，train 被路由跳过（outputs_acc 无 train key）
    setup_output = {
        "ledger_path": "/tmp/ledger.jsonl", "kd_artifacts_dir": "/tmp/art/",
        "baseline_latency_ms": 7.3, "concurrency": 2,
        "device_plan": '["cuda:0","cuda:1"]',
        "teacher_model_path": "/tmp/art/teacher_model.py",
    }
    gate_output = {"n_accepted": 0, "n_fail_latency": 3, "all_variants_count": 3,
                   "all_processed": True, "accepted_manifest_path": "/tmp/art/gate_manifest.json"}
    outputs_acc = {
        "setup": {"output": setup_output},
        "gate": {"output": gate_output},
        # 注意：train key 不存在（gate 路由到 $end，train 从未 visit）
    }
    inputs = {"target_latency_ms": "8.0", "latency_provider": "p::m"}
    from orca.run.step import _build_ctx
    ctx = _build_ctx(wf, outputs_acc, inputs, "test-run")
    # 每个 wf.outputs 模板必须能渲染（不 raise UndefinedError / ExecError）
    for key, tpl in wf.outputs.items():
        rendered = render_template(tpl, ctx)
        assert rendered is not None, f"outputs.{key} 渲染返回 None"


def test_wf_outputs_does_not_reference_train():
    """wf.outputs 的模板不得引用 train.output.*（train 可能被 gate 路由跳过 → render 崩）。"""
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    for key, tpl in wf.outputs.items():
        assert "train.output" not in tpl, (
            f"outputs.{key} 引用 train.output（gate→$end 路径会崩）：{tpl!r}"
        )


# ── 🟡 worker exception handler：FAIL_train 行字段齐全（CONTRACTS §5）──────────────


def test_train_pool_worker_exception_handler_row_schema(tmp_path, monkeypatch):
    """train_pool worker 异常 handler（_main as_completed try/except）产的 FAIL_train 行字段齐全。
    in-process 调 _main（mock _train_one raise），断言落账行 ⊇ CONTRACTS §5 必备字段 + 不杀整批。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n in ("train_pool", "kd_common")]:
        del sys.modules[m]
    import train_pool as tp

    # 一个 ACCEPTED manifest entry（模拟 gate 产出）
    entry = {
        "variant_id": "v_x", "variant_path": "/tmp/v_x.py", "variant_sha256": "abc",
        "accepted_cfg": {"num_blocks": 2}, "latency_ms_median": 5.0, "latency_ms_std": 0.1,
        "build_fn": "build_model", "dummy_input": {"shape": [1], "dtype": "float32"},
        "knobs": {"num_blocks": {"default": 3, "min": 1, "step": -1, "leverage": "high"}},
    }
    artifacts = tmp_path / "art"; artifacts.mkdir()
    ledger = artifacts / "ledger.jsonl"
    manifest = artifacts / "gate_manifest.json"
    manifest.write_text(json.dumps([entry]), encoding="utf-8")

    # mock _train_one 抛异常（验证 handler 兜底，不杀整批）
    def boom(ctx, e, dev):
        raise RuntimeError("simulated worker boom")
    monkeypatch.setattr(tp, "_train_one", boom)
    # 屏蔽 viz_kd（n_accepted>0 会触发；mock 掉 subprocess.run 让它 noop）
    import subprocess as _sp
    real_run = _sp.run
    def fake_run(argv, **kw):
        if any("viz_kd.py" in str(a) for a in argv):
            class _R:
                returncode = 0; stdout = ""; stderr = ""
            return _R()
        return real_run(argv, **kw)
    monkeypatch.setattr(_sp, "run", fake_run)

    # 构造 argv，in-process 调 _main
    monkeypatch.setattr(sys, "argv", [
        "train_pool.py",
        "--manifest", str(manifest), "--ledger", str(ledger),
        "--teacher_cache", "/dev/null/nonexistent.pt",
        "--kd_scripts_dir", str(KD), "--artifacts_dir", str(artifacts),
        "--per_run_artifacts_dir", str(artifacts),
        "--project_root", str(artifacts),
        "--accuracy_baseline", "0.02",
        "--latency_provider", str(artifacts / "p.py::m"),
        "--target_latency_ms", "8",
        "--concurrency", "1", "--device_plan", '[""]',
        "--per_variant_vram_bytes", "0",
        "--train_pipeline_path", str(artifacts / "train_pipeline.py"),
    ])
    rc = tp._main()
    assert rc == 0

    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1, f"应 1 行 FAIL_train，got {len(rows)}"
    row = rows[0]
    # CONTRACTS §5 必备字段
    required_fields = {
        "variant_id", "variant_path", "variant_sha256", "accepted_cfg", "cfg_hash",
        "status", "latency_ms_median", "latency_ms_std", "accuracy", "accuracy_kind",
        "met_latency", "met_accuracy", "ckpt", "target_latency_ms", "accuracy_baseline",
        "latency_provider_id", "run_id", "fail_reason",
    }
    missing = required_fields - set(row.keys())
    assert not missing, f"FAIL_train 行缺字段：{missing}（row={row}）"
    assert row["status"] == "FAIL_train"
    assert "simulated worker boom" in row["fail_reason"]
    assert row["met_latency"] is True  # gate 已 ACCEPTED，latency 达标


# ── E2E 暴露的 bug + reviewer findings 修复回归（2026-07-24）─────────────────────
# 覆盖：gate_all 空 KB WARN（R3）/ gate_all 单变体 tune rc!=0→FAIL_train /
#       gate_all dispatch rc!=0→FAIL_train / train_pool _train_one SUCCESS 字段齐 /
#       train_pool BLK-11 ckpt 缺失→FAIL_train / train_pool measure rc!=0→FAIL_accuracy /
#       gpu_probe NPU 探测失败 fail-soft（R1，无 GPU 路径用 mock）/ setup_helpers（R4）


def test_gate_all_empty_kb_warns_not_silent(tmp_path):
    """R3：gate_all 收到空 receiver_dir → 静默 N_ACCEPTED:0 是隐藏 bug（用户 99% 是
    ORCA_KB_DIR 指错 / families/receiver/ 无 .py）。stderr WARN 让用户能定位。"""
    import subprocess
    recv = tmp_path / "empty_recv"  # 故意不 mkdir（空目录）
    recv.mkdir()
    artifacts = tmp_path / "art"; artifacts.mkdir()
    ledger = artifacts / "ledger.jsonl"
    manifest = artifacts / "gate_manifest.json"
    # 写一个 dummy latency_provider（不会被调，因 KB 无变体）
    prov = tmp_path / "p.py"
    prov.write_text("def measure(onnx, device=None):\n    return 1.0\n", encoding="utf-8")
    r = subprocess.run([
        sys.executable, str(KD / "gate_all.py"),
        "--receiver_dir", str(recv), "--ledger", str(ledger),
        "--target_latency_ms", "5.0", "--latency_provider", str(prov) + "::measure",
        "--artifacts_dir", str(artifacts), "--kd_scripts_dir", str(KD),
        "--manifest_out", str(manifest),
    ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # 关键：stderr 必有 WARN（不再静默）
    assert "WARN" in r.stderr and "无 .py 变体" in r.stderr, (
        f"空 KB 应 stderr WARN，got stderr={r.stderr!r}"
    )
    assert "N_ACCEPTED: 0" in r.stdout
    assert "ALL_VARIANTS_COUNT: 0" in r.stdout


def test_gate_all_tune_failure_marks_fail_train(tmp_path):
    """gate_all：单变体 tune_latency rc!=0 → 记 FAIL_train 行 + 不杀整批（CONTRACTS §5）。

    构造一个会 import 失败的变体（语法合法但 import 缺失）让 tune_latency 子进程崩 →
    断言 ledger 落 FAIL_train 行，gate 仍 emit ALL_PROCESSED。
    """
    import subprocess
    recv = tmp_path / "recv"; recv.mkdir()
    # 变体合法但 latency_provider 文件不存在 → tune_latency 子进程 fail loud（rc!=0）
    (recv / "v_x.py").write_text(
        "DUMMY_INPUT={'shape':[1,4,48,64,1],'dtype':'float32'}\n"
        "BUILD_FN='build_model'\n"
        "KNOBS={'num_blocks':{'default':3,'min':1,'step':-1,'leverage':'high'}}\n"
        "def build_model(**c):\n"
        "    import torch.nn as nn\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    artifacts = tmp_path / "art"; artifacts.mkdir()
    ledger = artifacts / "ledger.jsonl"
    manifest = artifacts / "gate_manifest.json"
    r = subprocess.run([
        sys.executable, str(KD / "gate_all.py"),
        "--receiver_dir", str(recv), "--ledger", str(ledger),
        "--target_latency_ms", "5.0",
        "--latency_provider", str(tmp_path / "nonexistent.py::measure"),  # 文件不存在
        "--artifacts_dir", str(artifacts), "--kd_scripts_dir", str(KD),
        "--manifest_out", str(manifest),
        "--measure_repeats", "1", "--latency_tune_budget", "3",
    ], capture_output=True, text=True)
    # gate_all 自身 exit 0（单变体崩不杀整批，fail-soft at gate level）
    assert r.returncode == 0, r.stderr
    # ledger 落 FAIL_train 行（tune_latency rc!=0 → FAIL_train，不是 FAIL_latency）
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1, f"应 1 行 FAIL_train，got {len(rows)}"
    assert rows[0]["status"] == "FAIL_train"
    assert "tune_latency rc=" in rows[0]["fail_reason"], rows[0]["fail_reason"]


def test_gate_all_dispatch_failure_marks_fail_train(tmp_path, monkeypatch):
    """gate_all：tune_latency 成功但 distill_dispatch rc!=0 → 记 FAIL_train 行。

    mock run_subproc 让 tune_latency 首次调用返 rc=0 + ACCEPTED，让 distill_dispatch 第二次返 rc!=0。
    """
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n in ("gate_all", "kd_common", "pick_variant")]:
        del sys.modules[m]
    import gate_all as ga

    recv = tmp_path / "recv"; recv.mkdir()
    (recv / "v_y.py").write_text(
        "DUMMY_INPUT={'shape':[1,4,48,64,1],'dtype':'float32'}\n"
        "BUILD_FN='build_model'\n"
        "KNOBS={'num_blocks':{'default':3,'min':1,'step':-1,'leverage':'high'}}\n"
        "def build_model(**c):\n"
        "    import torch.nn as nn\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    artifacts = tmp_path / "art"; artifacts.mkdir()
    ledger = artifacts / "ledger.jsonl"
    manifest = artifacts / "gate_manifest.json"

    call_count = {"n": 0}

    def fake_subproc(argv):
        call_count["n"] += 1
        # 第 1 次：tune_latency 成功返 ACCEPTED + cfg + latency
        if call_count["n"] == 1:
            return 0, (
                "TUNE_STATUS: ACCEPTED\n"
                "ACCEPTED_CFG: {\"num_blocks\": 3}\n"
                "LATENCY_MS_MEDIAN: 2.5\nLATENCY_MS_STD: 0.1\n"
            ), ""
        # 第 2 次：distill_dispatch rc!=0
        return 1, "", "simulated dispatch crash"

    monkeypatch.setattr(ga, "run_subproc", fake_subproc)

    # in-process 调 _main：构造 sys.argv（subprocess 不便 mock run_subproc）
    monkeypatch.setattr(sys, "argv", [
        "gate_all.py",
        "--receiver_dir", str(recv), "--ledger", str(ledger),
        "--target_latency_ms", "5.0", "--latency_provider", str(tmp_path / "p.py::m"),
        "--artifacts_dir", str(artifacts), "--kd_scripts_dir", str(KD),
        "--manifest_out", str(manifest),
        "--measure_repeats", "1", "--latency_tune_budget", "3",
    ])
    rc = ga._main()
    assert rc == 0
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    # dispatch rc!=0 → FAIL_train（语义更准，CONTRACTS：dispatch 异常记 FAIL_train 不是 FAIL_latency）
    assert rows[0]["status"] == "FAIL_train"
    assert "distill_dispatch rc=1" in rows[0]["fail_reason"]
    # manifest 为空（dispatch 失败的不进 ACCEPTED）
    assert json.loads(manifest.read_text(encoding="utf-8")) == []


def test_gate_all_variant_import_exception_caught(tmp_path):
    """gate_all：变体 .py 缺 build_model / DUMMY_INPUT → _validate_variant raise →
    单变体 except 兜底记 FAIL_train 行 + all_processed=false（CONTRACTS §5）。"""
    import subprocess
    recv = tmp_path / "recv"; recv.mkdir()
    # 变体无 DUMMY_INPUT（_validate_variant 会 raise）
    (recv / "v_bad.py").write_text(
        "BUILD_FN='build_model'\n"
        "def build_model(**c):\n"
        "    import torch.nn as nn\n    return nn.Identity()\n",
        encoding="utf-8",
    )
    artifacts = tmp_path / "art"; artifacts.mkdir()
    ledger = artifacts / "ledger.jsonl"
    manifest = artifacts / "gate_manifest.json"
    r = subprocess.run([
        sys.executable, str(KD / "gate_all.py"),
        "--receiver_dir", str(recv), "--ledger", str(ledger),
        "--target_latency_ms", "5.0", "--latency_provider", str(tmp_path / "p.py::m"),
        "--artifacts_dir", str(artifacts), "--kd_scripts_dir", str(KD),
        "--manifest_out", str(manifest),
    ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # 单变体异常被 except 兜底：ledger 落 FAIL_train 行
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["status"] == "FAIL_train"
    assert "gate exception" in rows[0]["fail_reason"]
    # all_processed=false（有异常未走完）
    assert "ALL_PROCESSED: false" in r.stdout


# ── train_pool _train_one：SUCCESS / BLK-11 / FAIL_accuracy 单测（不调真 train/measure）──


def _train_one_entry():
    """构造一个 ACCEPTED manifest entry（_train_one 单测用）。"""
    return {
        "variant_id": "v_ok", "variant_path": "/fake/v_ok.py", "variant_sha256": "abc",
        "accepted_cfg": {"num_blocks": 2}, "latency_ms_median": 5.0, "latency_ms_std": 0.1,
        "build_fn": "build_model", "dummy_input": {"shape": [1], "dtype": "float32"},
        "knobs": {"num_blocks": {"default": 3, "min": 1, "step": -1, "leverage": "high"}},
    }


def _train_one_ctx(tmp_path):
    """构造 _train_one 的 ctx（_main 内部 ctx 字段全集）。"""
    return {
        "target_latency_ms": "8.0",
        "provider_id": "prov|1234",
        "accuracy_baseline": "0.02",
        "accuracy_baseline_kind": "nmse",
        "teacher_cache": "/fake/tc.pt",
        "kd_scripts_dir": str(KD),
        "artifacts_dir": str(tmp_path),
        "per_run_artifacts_dir": str(tmp_path),
        "project_root": str(tmp_path),
        "epochs": 1,
        "seed": 0,
        "train_pipeline_path": str(tmp_path / "train_pipeline.py"),
        "eval_device": "cpu",
    }


def test_train_one_success_row_full_fields(tmp_path, monkeypatch):
    """_train_one SUCCESS 路径：train rc=0 + ckpt 生成 + measure rc=0 + met_acc=true
    → ledger 行 status=SUCCESS + 字段齐全（CONTRACTS §5）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n in ("train_pool", "kd_common")]:
        del sys.modules[m]
    import train_pool as tp

    entry = _train_one_entry()
    ctx = _train_one_ctx(tmp_path)
    ckpt = tmp_path / "ckpts" / "v_ok.pt"
    ckpt.parent.mkdir(parents=True)
    ckpt.write_bytes(b"x" * 100)

    captured_argv = []
    def fake_subproc(argv):
        captured_argv.append(argv)
        argv_str = str(argv)
        # train_pipeline.py --mode distill → rc=0（训练产 ckpt）
        # train_pipeline.py --mode eval → rc=0 + STUDENT_ACCURACY 协议（取代 measure_student）
        if "train_pipeline.py" in argv_str:
            if "--mode" in argv and argv[argv.index("--mode") + 1] == "eval":
                return 0, "STUDENT_ACCURACY: 0.018\nSTUDENT_ACCURACY_KIND: nmse\nMET_ACCURACY: true", ""
            return 0, "", ""
        return 0, "", ""
    monkeypatch.setattr(tp, "run_subproc", fake_subproc)

    row = tp._train_one(ctx, entry, device="")
    assert row["status"] == "SUCCESS"
    assert row["met_latency"] is True
    assert row["met_accuracy"] is True
    assert row["accuracy"] == 0.018
    assert row["accuracy_kind"] == "nmse"
    assert row["ckpt"] == str(ckpt)
    assert row["fail_reason"] == ""
    # CONTRACTS §5 必备字段
    for f in ("variant_id", "variant_path", "variant_sha256", "accepted_cfg", "cfg_hash",
              "latency_ms_median", "latency_ms_std", "target_latency_ms", "accuracy_baseline",
              "latency_provider_id", "run_id"):
        assert f in row, f"SUCCESS 行缺 {f}"
    # v4：device=""（device_plan [""] fail-soft）必须归一化为 "cpu" 传给 train_pipeline.py
    # （train_pipeline._resolve_device("") 会 torch.device("") raise）。
    train_argv = next(a for a in captured_argv if "train_pipeline.py" in str(a))
    dev_idx = train_argv.index("--device")
    assert train_argv[dev_idx + 1] == "cpu", (
        f"device='' 应归一化为 'cpu' 传给 train_pipeline（_resolve_device 不接受空串）；"
        f"got --device {train_argv[dev_idx + 1]!r}"
    )
    # v4：train_pool worker 必须以 --mode distill 调 train_pipeline（防误删 mode → 跑成 teacher 模式）
    mode_idx = train_argv.index("--mode")
    assert train_argv[mode_idx + 1] == "distill", (
        f"worker 必须传 --mode distill；got --mode {train_argv[mode_idx + 1]!r}"
    )
    # 关键 distill 契约参数齐全（防漏传 → train_pipeline 退 placeholder / 报错）
    for flag in ("--student_model_path", "--teacher_cache", "--build_cfg",
                 "--kd_config", "--out_ckpt", "--variant_id"):
        assert flag in train_argv, f"worker argv 缺 {flag}（distill 契约参数）"
    # eval argv 契约（取代旧 measure_student argv；train_pipeline.py --mode eval 测精度，
    # 增量 D：--accuracy_baseline_kind 透传是 met_accuracy 判门的关键——漏传 → unknown →
    # fail-soft + WARN → met_accuracy=false 误杀合格 student）。
    eval_argv = next(a for a in captured_argv
                     if "train_pipeline.py" in str(a) and "--mode" in a
                     and a[a.index("--mode") + 1] == "eval")
    for flag in ("--accuracy_baseline", "--accuracy_baseline_kind", "--student_ckpt",
                 "--build_cfg", "--student_model_path"):
        assert flag in eval_argv, f"eval argv 缺 {flag}（精度测量契约参数）"
    kind_idx = eval_argv.index("--accuracy_baseline_kind")
    assert eval_argv[kind_idx + 1] == "nmse", (
        f"--accuracy_baseline_kind 应透传 ctx 值 nmse；got {eval_argv[kind_idx + 1]!r}"
    )


def test_train_one_missing_ckpt_marks_fail_train(tmp_path, monkeypatch):
    """BLK-11：train rc=0 但 ckpt 文件没生成（或空）→ FAIL_train + fail_reason 含 'ckpt 缺失/空'。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n in ("train_pool", "kd_common")]:
        del sys.modules[m]
    import train_pool as tp

    entry = _train_one_entry()
    ctx = _train_one_ctx(tmp_path)
    # 故意不创建 ckpt 文件

    def fake_subproc(argv):
        if "train_pipeline.py" in str(argv):
            return 0, "", ""  # rc=0 但没写真 ckpt（模拟用户脚本静默失败）
        return 0, "", ""
    monkeypatch.setattr(tp, "run_subproc", fake_subproc)

    row = tp._train_one(ctx, entry, device="")
    assert row["status"] == "FAIL_train"
    assert "ckpt 缺失/空" in row["fail_reason"]
    assert row["ckpt"] == ""


def test_train_one_measure_failure_marks_fail_accuracy(tmp_path, monkeypatch):
    """measure rc!=0 → FAIL_accuracy + fail_reason 含 'measure rc='（CONTRACTS §5）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n in ("train_pool", "kd_common")]:
        del sys.modules[m]
    import train_pool as tp

    entry = _train_one_entry()
    ctx = _train_one_ctx(tmp_path)
    ckpt = tmp_path / "ckpts" / "v_ok.pt"
    ckpt.parent.mkdir(parents=True)
    ckpt.write_bytes(b"x" * 100)

    def fake_subproc(argv):
        argv_str = str(argv)
        if "train_pipeline.py" in argv_str:
            if "--mode" in argv and argv[argv.index("--mode") + 1] == "eval":
                return 1, "", "eval crashed"  # eval 失败 → FAIL_accuracy
            return 0, "", ""  # distill rc=0
        return 0, "", ""
    monkeypatch.setattr(tp, "run_subproc", fake_subproc)

    row = tp._train_one(ctx, entry, device="")
    assert row["status"] == "FAIL_accuracy"
    assert "eval rc=1" in row["fail_reason"]
    assert row["ckpt"] == str(ckpt)  # train 成功了，ckpt 有


# ── setup_helpers：R4 确定性 teacher_ckpt 解析 + user_train grep ─────────────────


def test_setup_helpers_parse_out_from_command():
    """find-teacher-ckpt 的 --out 解析覆盖各种命令形态（确定性，rule 5）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "setup_helpers"]:
        del sys.modules[m]
    from setup_helpers import _parse_out_from_command
    assert _parse_out_from_command("python train.py --out /a/b.pt") == "/a/b.pt"
    assert _parse_out_from_command("python train.py --out=/c/d.ckpt") == "/c/d.ckpt"
    assert _parse_out_from_command("python train.py --output e.pt") == "e.pt"
    assert _parse_out_from_command("python train.py --ckpt-path ckpts/x.pth") == "ckpts/x.pth"
    assert _parse_out_from_command("python train.py") is None
    assert _parse_out_from_command("") is None


def test_setup_helpers_find_teacher_ckpt_via_out_flag(tmp_path):
    """teacher_train_command 含 --out → 直接用（无歧义首选）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "setup_helpers"]:
        del sys.modules[m]
    from setup_helpers import find_teacher_ckpt
    # 造一个源 ckpt
    src = tmp_path / "src.pt"
    src.write_bytes(b"x" * 50)
    target = tmp_path / "art" / "teacher_ckpt.pt"
    target_abs, src_abs = find_teacher_ckpt(
        project_root=str(tmp_path),
        train_command=f"python train.py --out {src}",
        target=str(target),
    )
    assert target_abs == str(target.resolve())
    assert src_abs == str(src.resolve())
    # 拷贝成功（target 文件存在且大小一致）
    assert target.is_file() and target.stat().st_size == 50


def test_setup_helpers_find_teacher_ckpt_scan_when_no_out(tmp_path):
    """无 --out → 扫 project_root 最新 .pt（排除 kd-nas-artifacts/ckpts 等假候选）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "setup_helpers"]:
        del sys.modules[m]
    from setup_helpers import find_teacher_ckpt
    # 用户 project_root 下散落几个 .pt（不应被选：在 ckpts/ 子目录）
    (tmp_path / "ckpts").mkdir()
    (tmp_path / "ckpts" / "old.pt").write_bytes(b"old")
    # 真候选：项目根 latest.pt（mTime 最新）
    import time
    latest = tmp_path / "latest.pt"
    latest.write_bytes(b"latest")
    # 强制 mTime 比 old.pt 新（防 filesystem 抖动）
    later = time.time() + 100
    import os
    os.utime(latest, (later, later))
    os.utime(tmp_path / "ckpts" / "old.pt", (later - 200, later - 200))

    target = tmp_path / "out.pt"
    target_abs, src_abs = find_teacher_ckpt(
        project_root=str(tmp_path),
        train_command="python train.py",  # 无 --out
        target=str(target),
    )
    assert src_abs == str(latest.resolve())
    assert target.is_file()


def test_setup_helpers_find_teacher_ckpt_fail_loud_when_no_candidate(tmp_path):
    """扫不到任何 .pt/.ckpt → FileNotFoundError（caller exit 2 fail loud）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "setup_helpers"]:
        del sys.modules[m]
    from setup_helpers import find_teacher_ckpt
    import pytest as _pt
    with _pt.raises(FileNotFoundError, match="无 .pt"):
        find_teacher_ckpt(
            project_root=str(tmp_path),
            train_command="python train.py",
            target=str(tmp_path / "out.pt"),
        )


def test_setup_helpers_grep_user_train_demo(tmp_path):
    """grep-user-train AST 解析能从 demo 风格 train.py 抽 compute_loss（确定性，rule 5）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "setup_helpers"]:
        del sys.modules[m]
    from setup_helpers import grep_user_train
    # 造一个 demo 风格 train.py
    (tmp_path / "train.py").write_text(
        "import torch\n"
        "import torch.nn as nn\n"
        "def compute_loss(s_out, y):\n"
        "    return nn.functional.mse_loss(s_out, y)\n"
        "def build_dataloader():\n"
        "    return []\n",
        encoding="utf-8",
    )
    train_import, loss_fn, sentinel = grep_user_train(
        project_root=str(tmp_path), train_command="python train.py",
    )
    assert train_import == str((tmp_path / "train.py").resolve())
    assert loss_fn == "compute_loss"
    assert sentinel is None


def test_setup_helpers_grep_user_train_sentinel_when_no_loss_fn(tmp_path):
    """train.py 无 loss callable → emit ask-user 哨兵（不编造，rule 5）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "setup_helpers"]:
        del sys.modules[m]
    from setup_helpers import grep_user_train
    (tmp_path / "train.py").write_text(
        "import torch\n"
        "def forward(x):\n"
        "    return x\n"  # 无 loss / compute_loss / *_loss 命名
        "",
        encoding="utf-8",
    )
    train_import, loss_fn, sentinel = grep_user_train(
        project_root=str(tmp_path), train_command="python train.py",
    )
    assert train_import == "" and loss_fn == ""
    assert sentinel is not None
    assert sentinel["_sentinel"] == "orca_ask_user_v1"
    assert "_orca_ask_user" in sentinel


# ── agent.md：BUG-1 执行指令强化（存在性 + 关键短语）────────────────────────────


def test_kd_agent_md_has_strong_execution_directive():
    """BUG-1：deepseek-v4-flash 把 agent.md 当 spec 审查而不执行。修复后每个 kd agent.md
    开头必须有强执行指令 + output schema 前置 + bash 块标 '执行：'。"""
    import re
    agent_dir = REPO / "workflows" / "agents"
    for name in ("kd-setup", "kd-gate", "kd-train"):
        text = (agent_dir / name / "agent.md").read_text(encoding="utf-8")
        # 1) 强执行指令（开头 2000 字符内必须有「唯一产出」「严禁」「JSON」）
        head = text[:2000]
        assert "唯一产出" in head, f"{name}/agent.md 开头缺「唯一产出」执行指令"
        assert "严禁" in head, f"{name}/agent.md 开头缺「严禁」红线"
        assert "JSON" in head, f"{name}/agent.md 开头缺 JSON 终点声明"
        # 2) ❌ 红线列表存在（BUG-1 关键：抗「审查/spec 评判」倾向）
        assert "❌" in head, f"{name}/agent.md 缺 ❌ 红线（BUG-1 抗审查倾向关键）"
        # 3) 「fail loud」契约段（BUG-1 关键：失败时上抛 stderr，不假装成功）
        assert "fail loud" in text.lower() or "失败" in text, (
            f"{name}/agent.md 缺「失败 = fail loud」契约段"
        )
        # 4) output schema 段前置：JSON schema 段 offset < 第一个 bash 块 offset
        #    （验证 schema 真的「前置」，而不是只在末尾）
        schema_offset = text.find("JSON schema")
        if schema_offset < 0:
            schema_offset = text.find("输出 JSON")
        bash_fence_offset = text.find("```bash")
        assert schema_offset >= 0, f"{name}/agent.md 缺 JSON schema 段"
        assert bash_fence_offset >= 0, f"{name}/agent.md 缺 ```bash 块"
        assert schema_offset < bash_fence_offset, (
            f"{name}/agent.md JSON schema 段（offset={schema_offset}）应在第一个 bash 块"
            f"（offset={bash_fence_offset}）之前——前置 schema 才能让 LLM 一开始就知道终点是 JSON"
        )
        # 5) bash 块明确标「执行：」（count 必须 ≥1，不是「或」逻辑）
        exec_marker_count = text.count("执行：")
        assert exec_marker_count >= 1, (
            f"{name}/agent.md 必须有显式「执行：」bash 块标签（BUG-1）"
        )
        # 6) 不再含 spec-审查框架词（违反 rule 5 的旧风格）
        assert "## 职责（按序，fail loud）" not in text, (
            f"{name}/agent.md 仍含旧「职责」spec-审查段（BUG-1 未修）"
        )


def test_kd_gate_agent_md_uses_setup_receiver_dir():
    """BUG-3：kd-gate/agent.md 必须从 setup.output.receiver_dir 取（与 train 对称），
    不依赖 $ORCA_KB_DIR env（in-session next 链里 ORCA_KB_DIR 会被重置 → glob 0）。"""
    text = (REPO / "workflows" / "agents" / "kd-gate" / "agent.md").read_text(encoding="utf-8")
    assert "--receiver_dir" in text, "kd-gate/agent.md 缺 --receiver_dir 参数"
    assert "setup.output.receiver_dir" in text, (
        "kd-gate/agent.md 应从 setup.output.receiver_dir 取（不依赖 $ORCA_KB_DIR env）"
    )
    # 反向断言：不应再用 ${ORCA_KB_DIR}/families/receiver 作 receiver_dir
    assert "${ORCA_KB_DIR}/families/receiver" not in text, (
        "kd-gate/agent.md 仍依赖 $ORCA_KB_DIR env（BUG-3 未闭环到 gate）"
    )


def test_kd_setup_agent_md_emits_receiver_dir():
    """BUG-3：kd-setup/agent.md 必须探测 RECEIVER_DIR 并写进 output JSON
    （train_pool 经 setup.output.receiver_dir 取，不依赖 ORCA_KB_DIR env）。"""
    text = (REPO / "workflows" / "agents" / "kd-setup" / "agent.md").read_text(encoding="utf-8")
    assert "RECEIVER_DIR" in text, "kd-setup/agent.md 缺 RECEIVER_DIR 探测"
    assert "receiver_dir" in text, "kd-setup/agent.md 缺 receiver_dir output 字段"


def test_kd_train_agent_md_passes_receiver_dir():
    """BUG-3：kd-train/agent.md 必须显式传 --receiver_dir（setup 探测的绝对路径）。"""
    text = (REPO / "workflows" / "agents" / "kd-train" / "agent.md").read_text(encoding="utf-8")
    assert "--receiver_dir" in text, "kd-train/agent.md 缺 --receiver_dir 参数"
    assert "setup.output.receiver_dir" in text, "kd-train/agent.md 应从 setup.output.receiver_dir 取"


def test_kd_train_agent_md_passes_baseline_latency_ms():
    """review #6-1：kd-train/agent.md 必须显式传 --baseline_latency_ms（latency bar baseline 参考行）。

    与 --receiver_dir 守门对称：防 agent.md 这行被漏写（HI-11 schema-chain test 只校验引用字段
    在 output_schema 内，不防「整段 flag 被删」）。来源链 setup.output.baseline_latency_ms 必填。
    """
    text = (REPO / "workflows" / "agents" / "kd-train" / "agent.md").read_text(encoding="utf-8")
    assert "--baseline_latency_ms" in text, "kd-train/agent.md 缺 --baseline_latency_ms 参数"
    assert "setup.output.baseline_latency_ms" in text, (
        "kd-train/agent.md 应从 setup.output.baseline_latency_ms 取（latency bar baseline 来源）")


def test_kd_setup_agent_md_no_longer_calls_setup_helpers():
    """v4：kd-setup/agent.md 不再调 setup_helpers find-teacher-ckpt / grep-user-train
    （teacher 训练改调 train_pipeline.py --mode teacher 固定 --out_ckpt；
    loss 适配下沉给 train-script-gen）。反向守门：防旧逻辑回潮。

    允许在注释/迁移说明里提到这些名字（解释变更），但不应作为命令调用（`python3 ... setup_helpers.py`）。
    """
    text = (REPO / "workflows" / "agents" / "kd-setup" / "agent.md").read_text(encoding="utf-8")
    # 反向断言：不应有 setup_helpers.py 的 python 调用命令行
    assert 'python3 "$KD_SCRIPTS_DIR/setup_helpers.py" find-teacher-ckpt' not in text, (
        "v4: step5 不应再调 setup_helpers.py find-teacher-ckpt（改用 train_pipeline.py --mode teacher 固定 --out_ckpt）"
    )
    assert 'python3 "$KD_SCRIPTS_DIR/setup_helpers.py" grep-user-train' not in text, (
        "v4: step6 grep-user-train 已删（loss 适配下沉给 train-script-gen）"
    )
    # 正向断言：step5 改调 train_pipeline.py --mode teacher
    assert "train_pipeline.py" in text or "TRAIN_PIPELINE_PATH" in text, (
        "v4: step5 应改调 train_pipeline.py --mode teacher"
    )
    # 正向断言：teacher latency 从 teacher_gen.output 透传（不再 teacher_setup 自测）
    assert "teacher_gen.output.teacher_latency_ms" in text, (
        "v4: teacher_setup latency 应从 teacher_gen.output 透传"
    )


# ── R1/R2 回归守门：reviewer 显式 finding 的修复线必有直接测试 ─────────────────────


def test_gpu_probe_r1_npu_zero_per_variant_fails_soft_no_estimation(
    tmp_path, monkeypatch, capsys,
):
    """R1：NPU 后端 ``max_memory_allocated`` 返 0 时，**不**用 ``total_free // 4`` 估算驱动并发
    （旧实现破坏 fail loud）。改 emit ``PER_VARIANT_VRAM_BYTES: 0`` + ``CONCURRENCY: 1`` +
    stderr WARN 「不估算驱动并发」。mock 可行（无需真 NPU 硬件）。
    """
    gp = _load(KD / "gpu_probe.py", "_gp_r1")

    tc = tmp_path / "tc.pt"; tc.write_bytes(b"x" * 10)
    # mock is_npu_available=True 让走 npu 路径
    monkeypatch.setattr(gp, "is_npu_available", lambda: True)
    # mock _probe_per_variant_vram 返 per_variant=0（NPU max_memory_allocated 返 0 的场景）
    monkeypatch.setattr(gp, "_probe_per_variant_vram",
                        lambda **kw: (0, "npu:0", 1, [10 * _GB]))

    monkeypatch.setattr(sys, "argv", [
        "gpu_probe.py",
        "--teacher_cache", str(tc),
        "--representative_variant", str(KBDIR / "spt_t1.py"),
        "--variants_count", "5", "--device", "npu",
    ])
    rc = gp._main()
    captured = capsys.readouterr()
    assert rc == 0, f"NPU per_variant=0 应 fail-soft exit 0，got rc={rc} stderr={captured.err}"
    # R1 关键断言：不估算，PER_VARIANT_VRAM_BYTES=0 + CONCURRENCY=1
    assert "PER_VARIANT_VRAM_BYTES: 0" in captured.out, (
        f"R1：NPU per_variant=0 应 emit PER_VARIANT_VRAM_BYTES: 0（不估算），got stdout={captured.out}"
    )
    assert "CONCURRENCY: 1" in captured.out, (
        f"R1：NPU per_variant=0 应退 CONCURRENCY: 1（不估算并发），got stdout={captured.out}"
    )
    # stderr WARN 必含「不估算驱动并发」（明确 fail-soft 原因）
    assert "不估算驱动并发" in captured.err, (
        f"R1：stderr WARN 应含「不估算驱动并发」标识，got stderr={captured.err}"
    )
    # 反向断言：不应含 ``total_free // 4`` 估算后的非零 per_variant（旧 R1 漏洞）
    # GPU_REPORT 应含 [probe failed] / WARN 标识
    assert "WARN" in captured.out or "probe failed" in captured.out, (
        f"R1：GPU_REPORT 应标 WARN / probe failed，got stdout={captured.out}"
    )


def test_train_pool_r2_viz_kd_nonzero_rc_warns_not_silent(tmp_path, monkeypatch, capsys):
    """R2：viz_kd rc!=0 不静默吞——stderr 打印尾部 300 字让用户能定位「图为什么没推」。
    复用 worker exception 路径驱动 viz_kd 调用，但 mock viz_kd rc=1 + 非空 stderr。
    """
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n in ("train_pool", "kd_common")]:
        del sys.modules[m]
    import train_pool as tp

    entry = _train_one_entry()
    artifacts = tmp_path / "art"; artifacts.mkdir()
    ledger = artifacts / "ledger.jsonl"
    manifest = artifacts / "gate_manifest.json"
    manifest.write_text(json.dumps([entry]), encoding="utf-8")

    # mock _train_one 抛异常驱动 as_completed handler（与 worker exception 测试同款）
    def boom(ctx, e, dev):
        raise RuntimeError("simulated worker boom")
    monkeypatch.setattr(tp, "_train_one", boom)

    # mock viz_kd rc=1 + 非空 stderr（验证 R2：rc!=0 不静默吞）
    import subprocess as _sp
    captured_viz_argv = []
    def fake_run(argv, **kw):
        if any("viz_kd.py" in str(a) for a in argv):
            captured_viz_argv.append(argv)
            class _R:
                returncode = 1
                stdout = ""
                stderr = "KeyError: 'latency_ms_median' in viz_kd._render_scatter"
            return _R()
        class _R0:
            returncode = 0; stdout = ""; stderr = ""
        return _R0()
    monkeypatch.setattr(_sp, "run", fake_run)

    monkeypatch.setattr(sys, "argv", [
        "train_pool.py",
        "--manifest", str(manifest), "--ledger", str(ledger),
        "--teacher_cache", "/dev/null/nonexistent.pt",
        "--kd_scripts_dir", str(KD), "--artifacts_dir", str(artifacts),
        "--per_run_artifacts_dir", str(artifacts),
        "--project_root", str(artifacts),
        "--accuracy_baseline", "0.02",
        "--accuracy_baseline_kind", "nmse",
        "--latency_provider", str(artifacts / "p.py::m"),
        "--target_latency_ms", "8",
        "--concurrency", "1", "--device_plan", '[""]',
        "--per_variant_vram_bytes", "0",
        "--train_pipeline_path", str(artifacts / "train_pipeline.py"),
    ])
    rc = tp._main()
    captured = capsys.readouterr()
    assert rc == 0  # viz 失败不阻断 sweep
    # R2 关键断言：stderr 含 viz_kd rc=1 WARN + 原 stderr 尾部
    assert "viz_kd rc=1" in captured.err, (
        f"R2：viz_kd rc!=0 应 stderr WARN 含 rc，got stderr={captured.err}"
    )
    assert "不阻断" in captured.err, (
        f"R2：viz_kd rc!=0 WARN 应含「不阻断」语义，got stderr={captured.err}"
    )
    # viz 原 stderr 尾部应被透传（让用户能定位）
    assert "latency_ms_median" in captured.err or "KeyError" in captured.err, (
        f"R2：viz_kd 原 stderr 尾部应透传让用户定位，got stderr={captured.err}"
    )
    # 增量 C+D：viz argv 必须透传 --variants_total（progress 图分母）+ --accuracy_baseline_kind
    # （方向标注 / pareto 方向）。漏传 → progress 分母显示「未知」+ pareto 方向退默认（静默退化）。
    assert captured_viz_argv, "viz_kd 应被调用一次"
    viz_argv = captured_viz_argv[0]
    assert "--variants_total" in viz_argv, "viz argv 缺 --variants_total（progress 图分母）"
    assert "--accuracy_baseline_kind" in viz_argv, "viz argv 缺 --accuracy_baseline_kind（方向驱动）"
    kind_idx = viz_argv.index("--accuracy_baseline_kind")
    assert viz_argv[kind_idx + 1] == "nmse", (
        f"--accuracy_baseline_kind 应透传 inputs 值 nmse；got {viz_argv[kind_idx + 1]!r}"
    )


def test_train_pool_viz_argv_passes_baseline_latency_ms(tmp_path, monkeypatch):
    """review #6-1：train_pool --baseline_latency_ms 透传给 viz_kd（latency bar 的 baseline 参考行）。

    setup.output.baseline_latency_ms 经 agent.md → train_pool --baseline_latency_ms → viz_argv。
    漏传 → viz_kd latency bar 永远缺 baseline 那根（viz_kd 已支持该 flag，仅 train_pool 未透传）。
    """
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n in ("train_pool", "kd_common")]:
        del sys.modules[m]
    import train_pool as tp

    entry = _train_one_entry()
    artifacts = tmp_path / "art"; artifacts.mkdir()
    ledger = artifacts / "ledger.jsonl"
    manifest = artifacts / "gate_manifest.json"
    manifest.write_text(json.dumps([entry]), encoding="utf-8")

    # mock _train_one 抛异常驱动 as_completed handler（与 R2 测试同款，只为触发 viz 调用）
    monkeypatch.setattr(tp, "_train_one", lambda ctx, e, dev: (_ for _ in ()).throw(
        RuntimeError("simulated boom")))

    captured_viz_argv = []
    import subprocess as _sp
    def fake_run(argv, **kw):
        if any("viz_kd.py" in str(a) for a in argv):
            captured_viz_argv.append(argv)
            class _R:
                returncode = 0; stdout = ""; stderr = ""
            return _R()
        class _R0:
            returncode = 0; stdout = ""; stderr = ""
        return _R0()
    monkeypatch.setattr(_sp, "run", fake_run)

    monkeypatch.setattr(sys, "argv", [
        "train_pool.py",
        "--manifest", str(manifest), "--ledger", str(ledger),
        "--teacher_cache", "/dev/null/nonexistent.pt",
        "--kd_scripts_dir", str(KD), "--artifacts_dir", str(artifacts),
        "--per_run_artifacts_dir", str(artifacts),
        "--project_root", str(artifacts),
        "--accuracy_baseline", "0.02",
        "--accuracy_baseline_kind", "nmse",
        "--latency_provider", str(artifacts / "p.py::m"),
        "--target_latency_ms", "8",
        "--baseline_latency_ms", "7.5",  # review #6-1：从 setup.output 透传
        "--concurrency", "1", "--device_plan", '[""]',
        "--per_variant_vram_bytes", "0",
        "--train_pipeline_path", str(artifacts / "train_pipeline.py"),
    ])
    rc = tp._main()
    assert rc == 0
    assert captured_viz_argv, "viz_kd 应被调用一次"
    viz_argv = captured_viz_argv[0]
    assert "--baseline_latency_ms" in viz_argv, "viz argv 缺 --baseline_latency_ms（latency bar baseline）"
    idx = viz_argv.index("--baseline_latency_ms")
    assert viz_argv[idx + 1] == "7.5", (
        f"--baseline_latency_ms 应透传 7.5；got {viz_argv[idx + 1]!r}")


def test_train_pool_viz_argv_omits_baseline_latency_ms_when_none(tmp_path, monkeypatch):
    """review #6-1：--baseline_latency_ms 未给（None）→ viz_argv 不含该 flag（viz_kd default=None 跳过）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n in ("train_pool", "kd_common")]:
        del sys.modules[m]
    import train_pool as tp

    entry = _train_one_entry()
    artifacts = tmp_path / "art"; artifacts.mkdir()
    ledger = artifacts / "ledger.jsonl"
    manifest = artifacts / "gate_manifest.json"
    manifest.write_text(json.dumps([entry]), encoding="utf-8")
    monkeypatch.setattr(tp, "_train_one", lambda ctx, e, dev: (_ for _ in ()).throw(
        RuntimeError("simulated boom")))

    captured_viz_argv = []
    import subprocess as _sp
    def fake_run(argv, **kw):
        if any("viz_kd.py" in str(a) for a in argv):
            captured_viz_argv.append(argv)
            class _R:
                returncode = 0; stdout = ""; stderr = ""
            return _R()
        class _R0:
            returncode = 0; stdout = ""; stderr = ""
        return _R0()
    monkeypatch.setattr(_sp, "run", fake_run)

    # 不传 --baseline_latency_ms（default None）
    monkeypatch.setattr(sys, "argv", [
        "train_pool.py",
        "--manifest", str(manifest), "--ledger", str(ledger),
        "--teacher_cache", "/dev/null/nonexistent.pt",
        "--kd_scripts_dir", str(KD), "--artifacts_dir", str(artifacts),
        "--per_run_artifacts_dir", str(artifacts),
        "--project_root", str(artifacts),
        "--accuracy_baseline", "0.02",
        "--accuracy_baseline_kind", "nmse",
        "--latency_provider", str(artifacts / "p.py::m"),
        "--target_latency_ms", "8",
        "--concurrency", "1", "--device_plan", '[""]',
        "--per_variant_vram_bytes", "0",
        "--train_pipeline_path", str(artifacts / "train_pipeline.py"),
    ])
    rc = tp._main()
    assert rc == 0
    viz_argv = captured_viz_argv[0]
    assert "--baseline_latency_ms" not in viz_argv, (
        "baseline_latency_ms=None 时不应加该 flag（viz_kd default=None 跳过 baseline 行）")


# ── train_pool _train_one：train rc!=0 / measure rc=0+met_acc=false 两条负路径 ────


def test_train_one_train_failure_marks_fail_train(tmp_path, monkeypatch):
    """_train_one：train_adapter rc!=0 → FAIL_train + fail_reason 含 'train_kd rc='
    （CONTRACTS §5）。原负路径无测试覆盖（仅 SUCCESS/BLK-11/measure 失败）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n in ("train_pool", "kd_common")]:
        del sys.modules[m]
    import train_pool as tp

    entry = _train_one_entry()
    ctx = _train_one_ctx(tmp_path)

    def fake_subproc(argv):
        if "train_pipeline.py" in str(argv):
            return 1, "", "OOM: cuda out of memory"
        return 0, "", ""
    monkeypatch.setattr(tp, "run_subproc", fake_subproc)

    row = tp._train_one(ctx, entry, device="")
    assert row["status"] == "FAIL_train"
    assert "train_pipeline rc=1" in row["fail_reason"]
    assert "OOM" in row["fail_reason"]
    assert row["ckpt"] == ""  # train 失败，没产 ckpt
    assert row["met_accuracy"] is False


def test_train_one_measure_accuracy_not_met_marks_fail_accuracy(tmp_path, monkeypatch):
    """_train_one：train rc=0 + measure rc=0 + met_acc=false → FAIL_accuracy（精度不达标）。
    CONTRACTS §5 最常见负样本路径（训练成功、measure 成功、但精度不达标）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n in ("train_pool", "kd_common")]:
        del sys.modules[m]
    import train_pool as tp

    entry = _train_one_entry()
    ctx = _train_one_ctx(tmp_path)
    ckpt = tmp_path / "ckpts" / "v_ok.pt"
    ckpt.parent.mkdir(parents=True)
    ckpt.write_bytes(b"x" * 100)

    def fake_subproc(argv):
        argv_str = str(argv)
        if "train_pipeline.py" in argv_str:
            if "--mode" in argv and argv[argv.index("--mode") + 1] == "eval":
                # eval 成功但精度不达标（met_acc=false）
                return 0, "STUDENT_ACCURACY: 0.05\nSTUDENT_ACCURACY_KIND: nmse\nMET_ACCURACY: false", ""
            return 0, "", ""  # distill rc=0
        return 0, "", ""
    monkeypatch.setattr(tp, "run_subproc", fake_subproc)

    row = tp._train_one(ctx, entry, device="")
    assert row["status"] == "FAIL_accuracy"
    assert row["accuracy"] == 0.05
    assert row["met_accuracy"] is False
    assert row["ckpt"] == str(ckpt)  # train 成功了 ckpt 有，但精度不过


def test_setup_helpers_walk_with_prune_skips_venv(tmp_path):
    """_walk_with_prune 必须跳过 .venv/site-packages（实测 Orca repo 33k 文件会让
    rglob 卡 >30s；setup_helpers.py WSL2-prune 设计的核心动机）。"""
    sys.path.insert(0, str(KD))
    for m in [n for n in sys.modules if n == "setup_helpers"]:
        del sys.modules[m]
    from setup_helpers import _walk_with_prune, _PRUNE_DIRS
    # 造一个含 .venv/ 的 project_root（venv 下造 100 个假文件）
    pr = tmp_path / "proj"
    (pr / ".venv" / "lib" / "site-packages").mkdir(parents=True)
    for i in range(50):
        ((pr / ".venv") / f"fake{i}.py").write_text("x", encoding="utf-8")
    # 真候选：项目根的 train.pt
    (pr / "real_train.pt").write_bytes(b"real")
    # llm_artifacts 子目录也应剪枝
    (pr / "llm_artifacts").mkdir()
    (pr / "llm_artifacts" / "skip.pt").write_bytes(b"skip")

    walked_files = [(rel_parts, fname) for _, rel_parts, fname in _walk_with_prune(pr)]
    fnames = {fname for _, fname in walked_files}
    # venv 内的假文件全不应出现
    assert not any(name.startswith("fake") for name in fnames), (
        f"_walk_with_prune 应剪掉 .venv，got fake files: {[n for n in fnames if n.startswith('fake')]}"
    )
    # llm_artifacts/skip.pt 不应出现
    assert "skip.pt" not in fnames, "_walk_with_prune 应剪掉 llm_artifacts"
    # real_train.pt 应保留
    assert "real_train.pt" in fnames


def test_setup_helpers_no_dead_code_loss_regex():
    """code-reviewer finding：``_LOSS_RE`` / ``_NN_LOSS_ASSIGN`` 死代码已删（YAGNI）。"""
    src = (KD / "setup_helpers.py").read_text(encoding="utf-8")
    assert "_LOSS_RE" not in src, "死代码 _LOSS_RE 应已删（grep 改 AST 后无引用）"
    assert "_NN_LOSS_ASSIGN" not in src, "死代码 _NN_LOSS_ASSIGN 应已删"


# ═══════════════════════════════════════════════════════════════════════════════
# KD-NAS finalize（2026-07-31）：指标方向单一真相源 + 防假 + select 确定性选择
# ═══════════════════════════════════════════════════════════════════════════════

SELECT_SCRIPT = REPO / "workflows" / "agents" / "kd-select" / "scripts" / "select_and_report.py"


def _load_kd_common():
    sys.path.insert(0, str(KD))
    import kd_common
    return kd_common


def test_accuracy_direction_single_source_of_truth():
    """accuracy_baseline_kind → best 方向（防 -20dB 误判优于 -22dB 的方向反转）。

    单一真相源 kd_common.accuracy_direction：measure_student / viz_kd / select 三处同源。
    """
    kc = _load_kd_common()
    assert kc.accuracy_direction("nmse") == "min"
    assert kc.accuracy_direction("mse") == "min"
    assert kc.accuracy_direction("ber") == "min"
    assert kc.accuracy_direction("db") == "min"   # 新增（KD-NAS finalize）
    assert kc.accuracy_direction("snr") == "max"
    assert kc.accuracy_direction("acc") == "max"
    # 未知 / 空 → 空串（caller 必须 fail loud，不许 auto 猜）
    assert kc.accuracy_direction("") == ""
    assert kc.accuracy_direction("unknown") == ""
    assert kc.accuracy_direction("MSE") == "min"  # 大小写不敏感


# ── review #5：is_measured_row 直接单测（"真测 vs 哨兵"唯一裁判，决定帕累托画哪些行）──


def test_is_measured_row_real_success():
    """SUCCESS + accuracy_kind 非空 → 真测（保留）。"""
    kc = _load_kd_common()
    assert kc.is_measured_row(
        {"status": "SUCCESS", "accuracy": 0.018, "accuracy_kind": "nmse"}) is True


def test_is_measured_row_real_fail_accuracy():
    """FAIL_accuracy + accuracy_kind 非空 → 真测（measure rc==0 跑到解析，真值可能恰为 0.0）。"""
    kc = _load_kd_common()
    # accuracy=0.0 但 accuracy_kind 非空 → measure emit 了 STUDENT_ACCURACY_KIND，真测
    assert kc.is_measured_row(
        {"status": "FAIL_accuracy", "accuracy": 0.0, "accuracy_kind": "nmse"}) is True


def test_is_measured_row_fail_latency_sentinel():
    """FAIL_latency → 哨兵（accuracy=0、accuracy_kind 空：gate 阶段落账，accuracy 未测）。"""
    kc = _load_kd_common()
    assert kc.is_measured_row(
        {"status": "FAIL_latency", "accuracy": 0, "accuracy_kind": ""}) is False


def test_is_measured_row_fail_train_sentinel():
    """FAIL_train → 哨兵（训练崩 / 无 ckpt，accuracy 未测）。"""
    kc = _load_kd_common()
    assert kc.is_measured_row(
        {"status": "FAIL_train", "accuracy": 0, "accuracy_kind": ""}) is False


def test_is_measured_row_measure_fail_sentinel():
    """FAIL_accuracy + accuracy_kind 空 → 哨兵（measure rc!=0，accuracy=0 是 fallback 哨兵）。

    这是 C1 防假的关键：accuracy=0 在 min 方向 kind 下会虚假占据帕累托前沿。
    """
    kc = _load_kd_common()
    assert kc.is_measured_row(
        {"status": "FAIL_accuracy", "accuracy": 0, "accuracy_kind": ""}) is False


def test_is_measured_row_success_empty_kind_is_sentinel():
    """SUCCESS 但 accuracy_kind 空 → 视为哨兵（不符合 emit 契约；防 status-only 伪造）。"""
    kc = _load_kd_common()
    assert kc.is_measured_row(
        {"status": "SUCCESS", "accuracy": 0.02, "accuracy_kind": ""}) is False


def test_is_measured_row_unknown_status():
    """status 不在 {SUCCESS, FAIL_accuracy} → 非真测（FAIL_export / 其他终态）。"""
    kc = _load_kd_common()
    assert kc.is_measured_row(
        {"status": "FAIL_export", "accuracy": 0.01, "accuracy_kind": "nmse"}) is False
    assert kc.is_measured_row({"status": "UNKNOWN", "accuracy_kind": "snr"}) is False


def test_measure_student_db_kind_lower_better():
    """measure_student 的绝对基线判定按显式 kind：db（越低越好）→ student ≤ baseline 才达标。"""
    sys.path.insert(0, str(KD))
    import measure_student
    # db 方向：student=-22 ≤ baseline=-20 → met=True（更低的 dB 更好，不许反转）
    met, used, conf = measure_student._compute_met_accuracy_absolute(
        -22.0, "db", -20.0, "db")
    assert met is True and used == "db" and conf == "high"
    # student=-18（比 -20 差）→ met=False
    met2, _, _ = measure_student._compute_met_accuracy_absolute(-18.0, "db", -20.0, "db")
    assert met2 is False
    # snr（越高越好）：student=22 ≥ baseline=20 → met=True
    met3, _, _ = measure_student._compute_met_accuracy_absolute(22.0, "snr", 20.0, "snr")
    assert met3 is True


def test_train_pool_classify_final_sweep_anti_fake():
    """Increment E：n_accepted>0 但 0 SUCCESS → SWEEP_STATUS=FAIL（避免「全 FAIL 但 SUCCESS」）。"""
    sys.path.insert(0, str(KD))
    import train_pool
    cfs = train_pool.classify_final_sweep
    # 1) incoming fail_reason（VRAM 降级）→ 保持 FAIL，原因不覆盖
    s, r = cfs([{"status": "SUCCESS"}], 3, "VRAM 降级 3->1")
    assert (s, r) == ("FAIL", "VRAM 降级 3->1")
    # 2) n_accepted==0（空批，gate 全 FAIL_latency）→ SUCCESS
    assert cfs([], 0, "") == ("SUCCESS", "")
    # 3) n_accepted>0 + 0 SUCCESS（全 FAIL_accuracy/FAIL_train）→ FAIL（防假）
    s, r = cfs([{"status": "FAIL_accuracy"}, {"status": "FAIL_train"}], 2, "")
    assert s == "FAIL" and "无一 SUCCESS" in r, (s, r)
    # 4) n_accepted>0 + ≥1 SUCCESS → SUCCESS
    assert cfs([{"status": "SUCCESS"}, {"status": "FAIL_accuracy"}], 2, "") == ("SUCCESS", "")


def _load_select_module():
    """加载 select_and_report.py（其内部按相对路径注入 _kd_scripts）。"""
    sys.path.insert(0, str(KD))
    return _load(SELECT_SCRIPT, "kd_select_and_report")


def test_select_best_student_direction_aware():
    """select 选最优 student 严格按 kind 方向（max/min），平局取 latency 更小者。"""
    m = _load_select_module()
    qualified = [
        {"variant_id": "v_a", "latency_ms_median": 6.0, "accuracy": 0.020},
        {"variant_id": "v_b", "latency_ms_median": 7.0, "accuracy": 0.018},  # nmse 最低
        {"variant_id": "v_c", "latency_ms_median": 7.0, "accuracy": 0.018},  # 同 acc 同 lat（平局）
    ]
    # nmse（min）：0.018 最好 → v_b（平局取 vid 字典序最小 = v_b）
    best_min = m._best_student(qualified, "min")
    assert best_min["variant_id"] == "v_b"
    # snr（max）：0.020 最好 → v_a
    best_max = m._best_student(qualified, "max")
    assert best_max["variant_id"] == "v_a"
    # 空 → None
    assert m._best_student([], "max") is None


def test_select_measured_and_qualified_filters():
    """select 过滤：真实测量行 = status ∈ {SUCCESS, FAIL_accuracy} ∧ accuracy_kind 非空。

    C1 防假：FAIL_latency 行的 latency 是**真测**（tune_latency 即便 FAIL 也 emit 测得值）、
    accuracy=0 是哨兵、accuracy_kind="" —— 必须剔除，否则 min 方向 kind 下会以 acc=0 虚假占据前沿。
    """
    m = _load_select_module()
    rows = [
        {"variant_id": "ok", "latency_ms_median": 5.0, "accuracy": 0.01, "accuracy_kind": "nmse",
         "met_latency": True, "met_accuracy": True, "status": "SUCCESS"},
        {"variant_id": "failacc_real", "latency_ms_median": 5.0, "accuracy": 0.05, "accuracy_kind": "nmse",
         "met_latency": True, "met_accuracy": False, "status": "FAIL_accuracy"},  # measure rc==0 真测
        # 以下三类都是哨兵行，必须剔除：
        {"variant_id": "faillat", "latency_ms_median": 15.0, "accuracy": 0, "accuracy_kind": "",
         "met_latency": False, "met_accuracy": False, "status": "FAIL_latency"},  # 真测 lat + 哨兵 acc
        {"variant_id": "failacc_sentinel", "latency_ms_median": 5.0, "accuracy": 0, "accuracy_kind": "",
         "met_latency": True, "met_accuracy": False, "status": "FAIL_accuracy"},  # measure rc!=0
        {"variant_id": "failtrain", "latency_ms_median": 6.0, "accuracy": 0, "accuracy_kind": "",
         "met_latency": True, "met_accuracy": False, "status": "FAIL_train"},
    ]
    measured = m._measured_rows(rows)
    assert [r["variant_id"] for r in measured] == ["ok", "failacc_real"], (
        f"哨兵行（acc=0 + accuracy_kind 空）必须剔除；got {[r['variant_id'] for r in measured]}"
    )
    qualified = m._qualified_rows(measured)
    assert [r["variant_id"] for r in qualified] == ["ok"]


def test_select_pareto_front_excludes_fail_latency_sentinel():
    """C1 回归：FAIL_latency 行（真测 lat=3.0 + 哨兵 acc=0）不得占据 min 方向帕累托前沿。

    若 ``_measured_rows`` 漏过滤，acc=0 在 nmse(min) 下会支配所有真测点 → 虚假入前沿。
    """
    m = _load_select_module()
    rows = [
        {"variant_id": "real_a", "latency_ms_median": 5.0, "accuracy": 0.02, "accuracy_kind": "nmse",
         "status": "SUCCESS"},
        {"variant_id": "faillat", "latency_ms_median": 3.0, "accuracy": 0, "accuracy_kind": "",
         "status": "FAIL_latency"},  # lat 比 real_a 更小、acc=0 哨兵 → 若混入会虚假支配
    ]
    measured = m._measured_rows(rows)
    assert [r["variant_id"] for r in measured] == ["real_a"], "FAIL_latency 哨兵行不得进 measured"
    front = m._pareto_front(measured, "min")
    assert front == [0]  # 只剩 real_a


def test_select_pareto_front_direction_aware():
    """帕累托前沿方向感知：latency(min) × accuracy(direction)。

    三点：pt0(lat=5,acc=0.02) pt1(lat=6,acc=0.01) pt2(lat=7,acc=0.03)
    """
    m = _load_select_module()
    pts = [
        {"latency_ms_median": 5.0, "accuracy": 0.02},  # pt0
        {"latency_ms_median": 6.0, "accuracy": 0.01},  # pt1
        {"latency_ms_median": 7.0, "accuracy": 0.03},  # pt2
    ]
    # nmse(min)：两轴都越小越好。pt0(lat 最小, acc 中) 与 pt1(acc 最小) 互不支配；
    # pt2(lat 最大且 acc 最大=最差) 被 pt0 双轴支配 → 不在前沿。
    front_min = set(m._pareto_front(pts, "min"))
    assert front_min == {0, 1}, front_min
    # snr(max)：acc 取负后越小越好。pt0(lat 最小) 与 pt2(acc 最大=最好) 互不支配；
    # pt1(lat 中, acc 中) 被 pt0 双轴支配 → 不在前沿。
    front_max = set(m._pareto_front(pts, "max"))
    assert front_max == {0, 2}, front_max
    # 新增一个被支配的点 pt3(lat=6.5, acc=0.02)：被 pt0(5<=6.5, 0.02<=0.02, lat 严格更小) 支配。
    pts_dom = pts + [{"latency_ms_median": 6.5, "accuracy": 0.02}]
    front_dom = set(m._pareto_front(pts_dom, "min"))
    assert 3 not in front_dom, "pt3 应被 pt0 支配（lat 更小 acc 相同）"
    assert front_dom == {0, 1}


def test_select_and_report_end_to_end_via_subprocess(tmp_path):
    """脚本本级 E2E：select_and_report.py 读 ledger → final_report + 正确选择（零 LLM）。"""
    import subprocess
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"variant_id": "v_good", "variant_sha256": "a", "latency_ms_median": 6.0,
         "latency_ms_std": 0.1, "accuracy": 0.020, "accuracy_kind": "nmse",
         "met_latency": True, "met_accuracy": True, "status": "SUCCESS", "accepted_cfg": {}},
        {"variant_id": "v_best", "variant_sha256": "b", "latency_ms_median": 7.0,
         "latency_ms_std": 0.1, "accuracy": 0.018, "accuracy_kind": "nmse",
         "met_latency": True, "met_accuracy": True, "status": "SUCCESS", "accepted_cfg": {}},
        {"variant_id": "v_failacc", "variant_sha256": "c", "latency_ms_median": 6.5,
         "latency_ms_std": 0.1, "accuracy": 0.030, "accuracy_kind": "nmse",
         "met_latency": True, "met_accuracy": False, "status": "FAIL_accuracy", "accepted_cfg": {}},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    artifacts = tmp_path / "kd_artifacts"
    artifacts.mkdir()
    out = subprocess.run(
        [sys.executable, str(SELECT_SCRIPT), "--ledger", str(ledger),
         "--kd_artifacts_dir", str(artifacts) + "/",
         "--accuracy_baseline", "0.025", "--accuracy_baseline_kind", "nmse",
         "--target_latency_ms", "8"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    assert "N_SELECTED: 2" in out.stdout
    assert "BEST_VARIANT: v_best" in out.stdout  # nmse 最低
    assert "SELECTION_OK: true" in out.stdout
    report = (artifacts / "final_report.md").read_text(encoding="utf-8")
    assert "最优 student" in report and "v_best" in report


def test_select_and_report_no_qualified_not_fabricated(tmp_path):
    """无达标 student → 报告标「无 student 达标」，不假装选出（exit 0, selection_ok=false）。"""
    import subprocess
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"variant_id": "v1", "variant_sha256": "a", "latency_ms_median": 6.0,
         "latency_ms_std": 0.1, "accuracy": 0.05, "accuracy_kind": "nmse",
         "met_latency": True, "met_accuracy": False, "status": "FAIL_accuracy", "accepted_cfg": {}},
    ]
    ledger.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    artifacts = tmp_path / "kd_artifacts"
    artifacts.mkdir()
    out = subprocess.run(
        [sys.executable, str(SELECT_SCRIPT), "--ledger", str(ledger),
         "--kd_artifacts_dir", str(artifacts) + "/",
         "--accuracy_baseline", "0.025", "--accuracy_baseline_kind", "nmse",
         "--target_latency_ms", "8"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr  # 无达标 = 设计内，非错误
    assert "N_SELECTED: 0" in out.stdout
    assert "SELECTION_OK: false" in out.stdout
    assert "BEST_VARIANT: " in out.stdout  # 空串
    report = (artifacts / "final_report.md").read_text(encoding="utf-8")
    assert "无 student 达标" in report


def test_select_and_report_unknown_kind_fail_loud(tmp_path):
    """未知 accuracy_baseline_kind → fail loud（exit 2 + 报告标注），不 auto 猜方向。"""
    import subprocess
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(
        {"variant_id": "v1", "latency_ms_median": 5.0, "accuracy": 0.01,
         "met_latency": True, "met_accuracy": True, "status": "SUCCESS"}) + "\n",
        encoding="utf-8")
    artifacts = tmp_path / "kd_artifacts"
    artifacts.mkdir()
    out = subprocess.run(
        [sys.executable, str(SELECT_SCRIPT), "--ledger", str(ledger),
         "--kd_artifacts_dir", str(artifacts) + "/",
         "--accuracy_baseline", "0.025", "--accuracy_baseline_kind", "foobar",
         "--target_latency_ms", "8"],
        capture_output=True, text=True,
    )
    assert out.returncode == 2, "未知 kind 必须 fail loud"
    assert "SELECTION_OK: false" in out.stdout
    report = (artifacts / "final_report.md").read_text(encoding="utf-8")
    assert "Selection FAILED" in report


def test_select_and_report_empty_ledger_fail_loud(tmp_path):
    """空 ledger → fail loud（exit 2 + 报告标注），不假装选出（用户显式列名的 hard 校验）。"""
    import subprocess
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")  # 空 ledger
    artifacts = tmp_path / "kd_artifacts"
    artifacts.mkdir()
    out = subprocess.run(
        [sys.executable, str(SELECT_SCRIPT), "--ledger", str(ledger),
         "--kd_artifacts_dir", str(artifacts) + "/",
         "--accuracy_baseline", "0.025", "--accuracy_baseline_kind", "nmse",
         "--target_latency_ms", "8"],
        capture_output=True, text=True,
    )
    assert out.returncode == 2, "空 ledger 必须 fail loud（非 0 退出）"
    assert "N_SELECTED: 0" in out.stdout
    assert "SELECTION_OK: false" in out.stdout
    assert "ledger 为空" in out.stderr or "ledger 为空" in out.stdout, (
        f"应在 stderr/stdout 标注空 ledger；stderr={out.stderr[-300:]}"
    )
    report = (artifacts / "final_report.md").read_text(encoding="utf-8")
    assert "Selection FAILED" in report


def test_select_and_report_corrupt_ledger_fail_loud(tmp_path):
    """坏 JSON 行 ledger → kd_common.read_ledger raise → select 捕获 exit 2（BLK-16 fail loud）。"""
    import subprocess
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"variant_id":"ok","status":"SUCCESS","latency_ms_median":5.0,'
                      '"accuracy":0.02,"accuracy_kind":"nmse","met_accuracy":true}\n'
                      '这不是合法 JSON 行\n', encoding="utf-8")
    artifacts = tmp_path / "kd_artifacts"
    artifacts.mkdir()
    out = subprocess.run(
        [sys.executable, str(SELECT_SCRIPT), "--ledger", str(ledger),
         "--kd_artifacts_dir", str(artifacts) + "/",
         "--accuracy_baseline", "0.025", "--accuracy_baseline_kind", "nmse",
         "--target_latency_ms", "8"],
        capture_output=True, text=True,
    )
    assert out.returncode == 2, "坏 JSON ledger 必须 fail loud"
    assert "SELECTION_OK: false" in out.stdout


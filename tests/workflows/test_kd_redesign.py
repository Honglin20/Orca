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
        "--test_command", "echo NMSE: 0.02",
        "--accuracy_baseline", "0.02",
        "--latency_provider", str(tmp_path / "p.py::m"),
        "--target_latency_ms", "8",
        "--concurrency", "1", "--device_plan", '[""]',
        "--per_variant_vram_bytes", "0",
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


# ── v2 DAG：yaml 节点 + 路由（n_accepted==0 → $end）─────────────────────────────


def test_kd_dag_setup_gate_train():
    """v2 DAG：3 节点 setup→gate→train→$end，无 selector/distill/recorder。"""
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    names = [n.name for n in wf.nodes]
    assert names == ["setup", "gate", "train"]
    # setup 恒到 gate
    assert [r.to for r in wf.nodes[0].routes] == ["gate"]
    # gate 首条 when = n_accepted==0 → $end；兜底 → train
    gate_routes = wf.nodes[1].routes
    assert gate_routes[0].to.endswith("end") or gate_routes[0].to == "$end"
    assert "n_accepted" in (gate_routes[0].when or "")
    assert gate_routes[1].to == "train"
    # train 恒到 $end
    assert [r.to for r in wf.nodes[2].routes] == ["$end"]


def test_kd_setup_emits_concurrency_fields():
    """setup output_schema 含 v2 新增并发字段（concurrency / device_plan / per_variant_vram_bytes）。"""
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    setup_props = set(wf.nodes[0].output_schema.get("properties", {}).keys())
    for f in ("concurrency", "device_plan", "per_variant_vram_bytes", "gpu_report"):
        assert f in setup_props, f"setup output_schema 缺 {f}"
    # 必填不含 gpu_report（fail-soft 可空）
    required = set(wf.nodes[0].output_schema.get("required", []))
    assert "gpu_report" not in required
    assert "concurrency" in required  # setup 必算（即便 1）


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
        "--test_command", "echo NMSE: 0.02",
        "--accuracy_baseline", "0.02",
        "--latency_provider", str(artifacts / "p.py::m"),
        "--target_latency_ms", "8",
        "--concurrency", "1", "--device_plan", '[""]',
        "--per_variant_vram_bytes", "0",
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

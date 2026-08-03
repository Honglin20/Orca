"""test_kd_redesign.py —— KD-NAS 重构关键不变量测试（脚本 + YAML 级，无 GPU/真硬件）。

覆盖 spec-review 高优 finding：
- BLK-8：tune_latency 最小缩量（mock latency∝cfg 体量，断言刚跨 target 即停；贪心跳步实现会挂）
- BLK-1/2：pick_variant KNOBS 校验（leverage rank / step<0）
- MED-4：pick_variant FAIL_latency 在 target 变化时重试；target-monotonic
- HI-2：tune_latency 每 build_model 前 seed（确定性，可复现）
- HI-11：kd agent.md 每个 `{{ <node>.output.<field> }}` 的 field ∈ 该 node output_schema
- teacher：10 层 t1/t2 交替（fixture 用法见 test_kd_train_script.py / TestTeacherSetup*）
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
             "status": "FAIL_latency", "target_latency_us": 8.0}]
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
             "status": "SUCCESS", "latency_us_median": 7.0, "ckpt": str(ckpt),
             "target_latency_us": 10.0}]
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
             "status": "SUCCESS", "latency_us_median": 5.0, "ckpt": str(ckpt)}]
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
        target_latency_us=2.0, latency_provider="mock::measure",
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
        target_latency_us=0.5, latency_provider="mock::measure",
        artifacts_dir=str(tmp_path), max_measurements=40, measure_repeats=1,
        device="cpu", seed=0, opset=17,
    )
    assert res["status"] == "FAIL_latency"


# ── HI-11：kd agent.md field 引用 ∈ output_schema ─────────────────────────────


# ── code-reviewer 🔴 回归守门 ─────────────────────────────────────────────────


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


# ── v2 DAG：yaml 节点 + 路由（n_accepted==0 → $end）─────────────────────────────


# ── 🔴 回归守门：wf.outputs 在 gate→$end（n_accepted==0）路由下不崩 ──────────────
# code-reviewer 🔴-1：原 yaml 引用 train.output.X，gate 路由 $end 时 train.output 不存在
# （不是 None，是 missing）→ StrictUndefined raise → workflow_failed。v2 修复：outputs 只引
# setup + gate（恒跑）。本测试驱动 render 模拟 gate→$end（train missing）断言不崩。


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
    # 串行 kd-nas 删除 kd-gate / kd-train / kd-select 后，强执行指令检查仅适用活跃节点。
    for name in ("kd-setup",):
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


# ── setup_helpers：R4 _walk_with_prune + 死代码守护 ──────────────────────────────


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
# KD-NAS finalize（2026-07-31）：指标方向单一真相源 + 防假（select 已删，留下 kd_common 不变量）
# ═══════════════════════════════════════════════════════════════════════════════


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



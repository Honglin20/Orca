"""test_struct_kd_p7.py —— P7 struct/kd 重设计关键不变量 smoke test。

覆盖 code-reviewer 标出的 P7 关键契约（无端到端 workflow 执行，仅脚本 + YAML 级别）：
- struct/kd `_device.py`：resolve_device + ort_providers（cuda/npu/cpu + NPU CANN 顺位）
- viz_struct.py：Pareto 过滤 accuracy is None（FAIL_latency）行——P7 修的 y=0 根因
- viz_struct.py：删 Round Ledger + Exploration Tree（只剩 3 图）
- viz_kd.py：round 模式 db_gap/met_acc 不在默认 columns
- viz_kd.py：teacher_accuracy_known=false → final_compare caption 含警告
- measure_student.py：既无 --eval_command 又无 --eval_dataset → latency-only 模式（db_gap sentinel -1）
- latency_onnxrt.py / export_onnx.py / profile_onnx.py / teacher_setup.py / measure_student.py
  / measure_baseline.py CLI：--device / --seed / --no-external-data / --strict-accuracy 全暴露
- teacher_setup.py `_parse_accuracy`：解析失败 → (0.0, "unknown", "low")（不静默造假）
- struct/kd workflow YAML：P7 后节点数 = 6（不是原计划 headline 的 7）
- kd-nas.yaml candidate_eval：latency-first 顺序契约在 prompt 里（Step A→B→C，B 失败 skip C）

不依赖 orca.chart / ts_quant / torch_npu（纯 stdlib + mock）；tars validate 在 conftest 里跑。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STRUCT_SCRIPTS = REPO / "workflows" / "agents" / "_struct_scripts"
KD_SCRIPTS = REPO / "workflows" / "agents" / "_kd_scripts"


# ───────────────────────── helpers ─────────────────────────


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _yaml_nodes(yaml_path: Path) -> list[str]:
    """Tiny YAML parser-free node counter：抓 `  - name: <X>` 顶层节点。"""
    nodes = []
    in_nodes = False
    for line in yaml_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("nodes:"):
            in_nodes = True
            continue
        if in_nodes:
            if line.startswith("  - name:"):
                nodes.append(line.split(":", 1)[1].strip())
            elif line and not line.startswith(" ") and not line.startswith("#"):
                in_nodes = False
    return nodes


# ───────────────────────── _device.py ─────────────────────────


def _purge_device_modules():
    """清掉 _device 缓存——quant/struct/kd 各有一份同名 _device.py，不 purge 会撞模块名。"""
    for mod_name in [m for m in sys.modules if m == "_device" or m.endswith("._device")]:
        del sys.modules[mod_name]


class TestDevice:
    def test_resolve_device_cpu_explicit(self):
        _purge_device_modules()
        sys.path.insert(0, str(STRUCT_SCRIPTS))
        from _device import resolve_device, ort_providers, describe_device

        d = resolve_device("cpu")
        assert str(d) == "cpu"
        # ort_providers(cpu) 只返 CPUExecutionProvider（CUDA/CANN 可能也在 available 里，
        # 但 cpu 模式应过滤掉）
        provs = ort_providers("cpu")
        assert provs == ["CPUExecutionProvider"], f"cpu providers should be CPU only, got {provs}"

    def test_resolve_device_auto_fallback(self):
        _purge_device_modules()
        # 在无 CUDA/NPU 的测试环境，auto 应退到 cpu
        sys.path.insert(0, str(STRUCT_SCRIPTS))
        from _device import resolve_device

        d = resolve_device("auto")
        assert str(d) in ("cpu", "cuda:0", "npu:0"), f"unexpected device {d}"

    def test_ort_providers_npu_lists_cann(self, monkeypatch):
        _purge_device_modules()
        # Mock onnxruntime.get_available_providers 返 CANN 可用 → npu 模式应优先 CANN
        fake_ort = types.ModuleType("onnxruntime")
        fake_ort.get_available_providers = lambda: ["CANNExecutionProvider", "CPUExecutionProvider"]
        monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
        sys.path.insert(0, str(STRUCT_SCRIPTS))
        from _device import ort_providers

        provs = ort_providers("npu")
        assert provs == ["CANNExecutionProvider", "CPUExecutionProvider"], provs

    def test_ort_providers_cuda_lists_cuda_first(self, monkeypatch):
        _purge_device_modules()
        fake_ort = types.ModuleType("onnxruntime")
        fake_ort.get_available_providers = lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"]
        monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
        sys.path.insert(0, str(STRUCT_SCRIPTS))
        from _device import ort_providers

        provs = ort_providers("cuda")
        assert provs[0] == "CUDAExecutionProvider"

    def test_kd_copy_identical_to_struct(self):
        # 共享单源决策已 surface：两份 _device.py 内容相同（不引跨包依赖是用户约束）
        struct_src = (STRUCT_SCRIPTS / "_device.py").read_text(encoding="utf-8")
        kd_src = (KD_SCRIPTS / "_device.py").read_text(encoding="utf-8")
        assert struct_src == kd_src, "_device.py copies diverged"


# ───────────────────────── viz_struct.py ─────────────────────────


class TestVizStructP7:
    """P7 图表根因修复：Pareto 过滤 accuracy=None，删 Round Ledger + Exploration Tree。"""

    def setup_method(self, _):
        # 备份 sys.modules 中 orca 相关模块（mock 后必须还原，否则污染同 session 后续测试）
        self._saved_orca = sys.modules.get("orca")
        self._saved_orca_chart = sys.modules.get("orca.chart")
        self._saved_orca_chart_env = sys.modules.get("orca.chart._env")
        # 备份 ORCA_* env（2026-07-23 鲁棒出图后 viz_struct 走 _resolve_env_status；
        # 不设 env 会进入 env_missing 路径不调 render_chart → 老 P7 测试全 fail）
        self._saved_env = {k: v for k, v in os.environ.items() if k.startswith("ORCA_")}
        for k in list(os.environ):
            if k.startswith("ORCA_"):
                del os.environ[k]
        # preset env 让 _resolve_env_status 返 ok（mock 路径下不真连 socket）
        os.environ["ORCA_RUN_ID"] = "test-run"
        os.environ["ORCA_NODE"] = "test-node"
        os.environ["ORCA_SESSION_ID"] = "test-sess"
        os.environ["ORCA_CHART_SOCK"] = "/tmp/test-orca-p7.sock"
        # Mock orca.chart.render_chart to capture calls
        self.calls = []
        mock_chart = types.ModuleType("orca.chart")
        mock_chart.render_chart = lambda **kw: self.calls.append(kw)
        # 保留真 orca.chart._env（2026-07-23 SPEC 后 viz_struct 还 lazy-import 它；
        # 不注册会让 from orca.chart._env import 失败 → _load_run_env=None → import_failed）
        try:
            import orca.chart._env as real_env  # noqa: F401
        except ImportError:
            real_env = None
        sys.modules["orca"] = types.ModuleType("orca")
        sys.modules["orca.chart"] = mock_chart
        if real_env is not None:
            sys.modules["orca.chart._env"] = real_env
        sys.path.insert(0, str(STRUCT_SCRIPTS))
        # Force reload
        for mod_name in [m for m in sys.modules if m == "viz_struct"]:
            del sys.modules[mod_name]
        import viz_struct
        self.viz_struct = importlib.reload(viz_struct)

    def teardown_method(self, _):
        # 还原 orca 模块（防 mock 泄漏污染后续测试）
        for mod_name in [m for m in sys.modules if m in ("viz_struct", "viz_kd")]:
            del sys.modules[mod_name]
        if self._saved_orca is not None:
            sys.modules["orca"] = self._saved_orca
        elif "orca" in sys.modules:
            del sys.modules["orca"]
        if self._saved_orca_chart is not None:
            sys.modules["orca.chart"] = self._saved_orca_chart
        elif "orca.chart" in sys.modules:
            del sys.modules["orca.chart"]
        if self._saved_orca_chart_env is not None:
            sys.modules["orca.chart._env"] = self._saved_orca_chart_env
        elif "orca.chart._env" in sys.modules:
            del sys.modules["orca.chart._env"]
        # 还原 ORCA_* env（防 preset 泄漏到后续测试）
        for k in list(os.environ):
            if k.startswith("ORCA_"):
                del os.environ[k]
        os.environ.update(self._saved_env)

    def _write_ledger(self, tmp_path, rows, champions=None):
        ledger = tmp_path / "ledger.jsonl"
        champs = tmp_path / "champions.jsonl"
        with ledger.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        with champs.open("w") as f:
            for c in (champions or []):
                f.write(json.dumps(c) + "\n")
        return str(ledger), str(champs)

    def test_pareto_filters_none_accuracy(self, tmp_path):
        """FAIL_latency 的 accuracy=-1 → _to_float 返 None → 必须剔除（防 y=0 伪点）。"""
        rows = [
            {"id": "c1", "parent": "b", "path": "p1", "round": 1, "status": "SUCCESS",
             "tag": "structural", "latency_ms": 10.0, "accuracy": 0.9,
             "met_accuracy": True, "snapshot": "/x", "onnx": "/x",
             "diff_summary": "d", "hypothesis": "h"},
            {"id": "c2", "parent": "c1", "path": "p1", "round": 2, "status": "FAIL_latency",
             "tag": "structural", "latency_ms": 15.0, "accuracy": -1,  # None after _to_float
             "met_accuracy": False, "snapshot": "/x", "onnx": "/x",
             "diff_summary": "d", "hypothesis": "h"},
        ]
        champions = [{"round": 0, "id": "baseline", "latency_ms": 12.0,
                      "accuracy": 0.88, "delta_vs_baseline_ms": 0, "snapshot": "/x"}]
        ledger, champs = self._write_ledger(tmp_path, rows, champions)

        self.calls.clear()
        self.viz_struct.render_all(
            ledger_path=ledger, champions_path=champs,
            baseline_latency_ms=12.0, baseline_accuracy=0.88,
            target_latency_ms=10.0, accuracy_target=0.87,
        )

        pareto = next((c for c in self.calls if c.get("title") == "Latency-Accuracy Pareto"), None)
        assert pareto is not None, "pareto chart not pushed"
        ids = [row["candidate_id"] for row in pareto["data"]]
        assert ids == ["c1"], f"Pareto should only keep valid-accuracy row; got {ids}"

    def test_only_three_charts_no_round_ledger_or_exploration_tree(self, tmp_path):
        """P7 根因清理：删 Round Ledger + Exploration Tree。2026-07-24 P2-1 加 accuracy 维度 → 4 图。"""
        rows = [
            {"id": f"c{i}", "parent": "baseline", "path": "p1", "round": i,
             "status": "SUCCESS", "tag": "structural", "latency_ms": 10.0 - i,
             "accuracy": 0.9, "met_accuracy": True, "snapshot": "/x", "onnx": "/x",
             "diff_summary": "d", "hypothesis": "h"}
            for i in range(1, 4)
        ]
        champions = [{"round": 0, "id": "baseline", "latency_ms": 12.0,
                      "accuracy": 0.88, "delta_vs_baseline_ms": 0, "snapshot": "/x"}]
        ledger, champs = self._write_ledger(tmp_path, rows, champions)

        self.calls.clear()
        self.viz_struct.render_all(
            ledger_path=ledger, champions_path=champs,
            baseline_latency_ms=12.0, baseline_accuracy=0.88,
            target_latency_ms=10.0, accuracy_target=0.87,
        )
        titles = sorted(c["title"] for c in self.calls)
        assert titles == [
            "Candidate Ledger (per change)",
            "Champion Trace",
            "Champion Trace — Accuracy",
            "Latency-Accuracy Pareto",
        ], f"should push 4 charts (P7 三张 + P2-1 accuracy 维度); got {titles}"


# ───────────────────────── viz_kd.py（重构版：sweep 散点 + 表 + latency bar）─────────


class TestVizKd:
    def setup_method(self, _):
        self._saved_orca = sys.modules.get("orca")
        self._saved_orca_chart = sys.modules.get("orca.chart")
        self.calls = []
        mock_chart = types.ModuleType("orca.chart")
        mock_chart.render_chart = lambda **kw: self.calls.append(kw)
        sys.modules["orca"] = types.ModuleType("orca")
        sys.modules["orca.chart"] = mock_chart
        sys.path.insert(0, str(KD_SCRIPTS))
        for mod_name in [m for m in sys.modules if m == "viz_kd"]:
            del sys.modules[mod_name]
        import viz_kd
        self.viz_kd = importlib.reload(viz_kd)

    def teardown_method(self, _):
        for mod_name in [m for m in sys.modules if m in ("viz_kd", "viz_struct")]:
            del sys.modules[mod_name]
        if self._saved_orca is not None:
            sys.modules["orca"] = self._saved_orca
        elif "orca" in sys.modules:
            del sys.modules["orca"]
        if self._saved_orca_chart is not None:
            sys.modules["orca.chart"] = self._saved_orca_chart
        elif "orca.chart" in sys.modules:
            del sys.modules["orca.chart"]

    @staticmethod
    def _variant_row(vid, lat, acc, met_acc=True, status="SUCCESS"):
        return {"variant_id": vid, "status": status, "latency_ms_median": lat,
                "accuracy": acc, "met_accuracy": met_acc, "met_latency": True,
                "accepted_cfg": {"num_blocks": 3}}

    def test_sweep_scatter_pushed_with_points(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        with ledger.open("w") as f:
            f.write(json.dumps(self._variant_row("spt_t1", 7.0, 0.02)) + "\n")
            f.write(json.dumps(self._variant_row("spt_alt", 9.0, 0.03, met_acc=False, status="FAIL_accuracy")) + "\n")
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_ms=8.0,
                               target_latency_ms=10.0, accuracy_baseline=0.02,
                               accuracy_baseline_kind="nmse", env_anchor="")
        scatter = next((c for c in self.calls if "latency vs accuracy" in c.get("title", "")), None)
        assert scatter is not None, "sweep scatter not pushed"
        ids = sorted(p["id"] for p in scatter["data"])
        assert ids == ["spt_alt", "spt_t1"], f"both variants should be points; got {ids}"

    def test_ledger_table_has_new_schema_columns(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        with ledger.open("w") as f:
            f.write(json.dumps(self._variant_row("spt_t1", 7.0, 0.02)) + "\n")
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_ms=None,
                               target_latency_ms=None, accuracy_baseline=None,
                               accuracy_baseline_kind="", env_anchor="")
        table = next((c for c in self.calls if "Distill Ledger" in c.get("title", "")), None)
        assert table is not None
        cols = table.get("columns", [])
        assert "variant_id" in cols and "latency_ms" in cols and "accuracy" in cols
        # 旧 schema 字段已删
        assert "proxy_mse" not in cols and "db_gap" not in cols and "family" not in cols


# ───────────────────────── measure_student.py 绝对精度基线（SR3）─────────────────


class TestMeasureStudentAbsoluteBaseline:
    """新设计：精度对比用户绝对基线，方向由 kind 决定（不再 teacher-relative dB gap）。"""

    def test_nmse_lower_is_better_met(self):
        sys.path.insert(0, str(KD_SCRIPTS))
        from measure_student import _compute_met_accuracy_absolute
        met, kind, conf = _compute_met_accuracy_absolute(0.02, "nmse", 0.03, "")
        assert met is True and kind == "nmse" and conf == "high"

    def test_nmse_not_met_when_above_baseline(self):
        sys.path.insert(0, str(KD_SCRIPTS))
        from measure_student import _compute_met_accuracy_absolute
        met, kind, conf = _compute_met_accuracy_absolute(0.05, "nmse", 0.03, "")
        assert met is False

    def test_snr_higher_is_better_met(self):
        sys.path.insert(0, str(KD_SCRIPTS))
        from measure_student import _compute_met_accuracy_absolute
        met, _, conf = _compute_met_accuracy_absolute(20.0, "snr", 18.0, "")
        assert met is True and conf == "high"

    def test_unknown_kind_never_silent_pass(self):
        sys.path.insert(0, str(KD_SCRIPTS))
        from measure_student import _compute_met_accuracy_absolute
        met, _, conf = _compute_met_accuracy_absolute(0.02, "unknown", 0.03, "")
        assert met is False and conf == "low"

    def test_kind_override_locks_direction(self):
        """SR3：override 锁方向；与 detected 不符 → WARN 但用 override。"""
        sys.path.insert(0, str(KD_SCRIPTS))
        from measure_student import _compute_met_accuracy_absolute
        # detected=mse(越低越好) 但 override=snr(越高越好) → 用 snr 判定
        met, kind, _ = _compute_met_accuracy_absolute(20.0, "mse", 18.0, "snr")
        assert kind == "snr" and met is True


# ───────────────────────── teacher_setup.py ─────────────────────────


class TestTeacherSetupParse:
    """teacher_setup.py `_parse_accuracy`：解析失败 → (0.0, unknown, low)，不静默造假。"""

    def test_parse_garbage_returns_low_confidence(self):
        sys.path.insert(0, str(KD_SCRIPTS))
        from teacher_setup import _parse_accuracy
        acc, kind, conf = _parse_accuracy("garbage output no metrics")
        assert acc == 0.0
        assert kind == "unknown"
        assert conf == "low"

    def test_parse_nmse_returns_high_confidence(self):
        sys.path.insert(0, str(KD_SCRIPTS))
        from teacher_setup import _parse_accuracy
        acc, kind, conf = _parse_accuracy("epoch 10 done\nNMSE: 0.0234")
        assert kind == "nmse"
        assert conf == "high"


# ───────────────────────── CLI surface ─────────────────────────


@pytest.mark.parametrize("script_rel,args,required_flags", [
    ("_struct_scripts/latency_onnxrt.py", [], ["--device", "--seed"]),
    ("_struct_scripts/export_onnx.py", [], ["--no-external-data", "--allow-external-data", "--device", "--seed", "--build_cfg"]),
    ("_struct_scripts/measure_baseline.py", [], ["--device", "--seed"]),
    ("_kd_scripts/profile_onnx.py", [], ["--device", "--seed"]),
    ("_kd_scripts/measure_student.py", [], ["--device", "--seed", "--skip_latency", "--accuracy_baseline"]),
    ("_kd_scripts/teacher_setup.py", [], ["--device", "--seed", "--strict-accuracy"]),
    ("_kd_scripts/pick_variant.py", [], ["--target_latency_ms", "--latency_provider", "--force_rerun"]),
    ("_kd_scripts/tune_latency.py", [], ["--device", "--seed", "--max_measurements"]),
    ("_kd_scripts/distill_dispatch.py", [], ["--tune_status"]),
    ("_kd_scripts/viz_kd.py", [], ["--baseline_latency_ms", "--target_latency_ms", "--env_anchor"]),
    ("_kd_scripts/train_variants_parallel.py", [], ["--concurrency", "--teacher_cache", "--ledger"]),
])
def test_cli_flags_exposed(script_rel, args, required_flags):
    """P7：所有脚本 CLI 暴露 --device / --seed（+ export 的 external-data / teacher_setup 的 strict-accuracy）。"""
    script_path = REPO / "workflows" / "agents" / script_rel
    r = subprocess.run(
        ["python3", str(script_path), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, f"{script_rel} --help failed: {r.stderr}"
    for flag in required_flags:
        assert flag in r.stdout, f"{script_rel} missing {flag} in --help output"


# ───────────────────────── workflow YAML structure ─────────────────────────


def test_struct_workflow_has_six_nodes():
    """P7：struct workflow 11→6 节点（不是 7；plan headline off-by-one）。"""
    nodes = _yaml_nodes(REPO / "workflows" / "agent-struct-exploration.yaml")
    expected = ["setup", "hypothesizer", "engineer", "evaluator", "curator", "finalize"]
    assert nodes == expected, f"struct nodes mismatch: {nodes}"


def test_kd_workflow_has_four_nodes():
    """重构：kd workflow 4 节点 setup→selector→distill→recorder（无 finalize/engineer/hypothesizer/curator）。"""
    nodes = _yaml_nodes(REPO / "workflows" / "kd-nas.yaml")
    expected = ["setup", "selector", "distill", "recorder"]
    assert nodes == expected, f"kd nodes mismatch: {nodes}"


def test_kd_latency_provider_required_no_default():
    """BLK-3/10：latency_provider 必填无默认（用户真硬件 latency 脚本）。"""
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    idef = wf.inputs["latency_provider"]
    assert idef.required is True, "latency_provider 必须 required=true"
    assert idef.default is None, "latency_provider 必须无 default"


def test_kd_no_finalize_no_proxy():
    """重构：无 finalize 节点；无 proxy_mse / accuracy_gap_db 输入（旧搜索语义全砍）。"""
    nodes = _yaml_nodes(REPO / "workflows" / "kd-nas.yaml")
    assert "finalize" not in nodes, "kd-nas 不应有 finalize 节点"
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    declared = set(wf.inputs.keys())
    assert "proxy_mse" not in declared and "accuracy_gap_db" not in declared, \
        f"旧搜索语义输入应已移除；declared={sorted(declared)}"


def test_kd_setup_node_exposes_path_fields():
    """重构：setup output_schema 暴露新路径字段（kd_artifacts_dir 稳定根 + ledger + ckpts + teacher + ...）。"""
    yaml_text = (REPO / "workflows" / "kd-nas.yaml").read_text(encoding="utf-8")
    for field in ["kd_artifacts_dir:", "per_run_artifacts_dir:", "ledger_path:",
                  "ckpts_dir:", "teacher_cache:", "teacher_meta:", "kd_scripts_dir:",
                  "baseline_latency_ms:"]:
        assert field in yaml_text, f"kd setup output_schema missing {field}"


def test_struct_setup_node_exposes_path_fields():
    """P9b：新增 struct_scripts_dir（原 input 下沉为 setup output）。"""
    yaml_text = (REPO / "workflows" / "agent-struct-exploration.yaml").read_text(encoding="utf-8")
    required_fields = [
        "output_dir:", "snapshots_dir:", "worktree_root:",
        "ledger_path:", "champions_path:",
        # P9b 新增（原 inputs.struct_scripts_dir 下沉）
        "struct_scripts_dir:",
    ]
    for field in required_fields:
        assert field in yaml_text, f"struct setup output_schema missing {field}"


def test_no_string_concat_output_dir_in_agent_md():
    """P2/P7 收口：agent.md 不再有 `{{ <node>.output.output_dir }}<suffix>` 字符串拼接。

    唯一例外：`setup.output.output_dir` 是 P7 合并后的单一真相源，其 output_schema 描述
    明确要求「末尾必须带 /」（setup 节点内部用 `os.path.abspath(...) + "/"` 计算一次），
    所以下游 `{{ setup.output.output_dir }}<filename>` 拼接是安全的。
    任何其它节点的 output_dir 都不应被字符串拼接（无尾斜杠保证 → 兄弟孤儿目录根因）。
    """
    import re
    # 匹配 `{{ <X>.output.output_dir }}<filename-char>`，但排除 setup.output.output_dir（安全）
    pattern = re.compile(
        r"\{\{\s*(?!setup\.output\.output_dir)[\w.]+\.output\.output_dir\s*\}\}[a-zA-Z_/.]"
    )
    agent_dir = REPO / "workflows" / "agents"
    for agent_md in agent_dir.rglob("agent.md"):
        if ("struct-" in str(agent_md) or "kd-" in str(agent_md)
                or agent_md.parent.name == "kd-setup"):
            text = agent_md.read_text(encoding="utf-8")
            matches = pattern.findall(text)
            assert not matches, f"{agent_md.name}: found output_dir concat pattern {matches}"


# P9b：production workflow inputs slim 后的契约守门。
# 现有 compile validator 对「未声明 inputs.X 引用」只 warn 不 error（设计如此），
# 故 `load_workflow` 不会捕获「移除 input 漏改 agent.md Jinja」。本测试用正则扫所有
# production workflow + struct/kd agent.md 的 `{{ inputs.X }}` 引用，断言 X 在 declared inputs 内——
# 未来同类 slim 改动漏改 Jinja 时，本测试当场红（render 期 StrictUndefined 才崩太晚）。
@pytest.mark.parametrize(
    "wf_path",
    sorted((REPO / "workflows").glob("*.yaml")),
    ids=lambda p: p.name,
)
def test_no_jinja_ref_to_undeclared_input(wf_path):
    """每个 workflow 的 yaml + 关联 agent.md 的 `{{ inputs.X }}` 必须只引用 declared inputs。"""
    import re
    import yaml
    from orca.compile.parser import load_workflow

    wf = load_workflow(wf_path)  # schema + parse + Jinja2 syntax 校验（抛错即红）
    declared = set(wf.inputs.keys())

    # 收集 yaml 内 + 各 agent.md 内的 `{{ inputs.X }}` 引用
    ref_pattern = re.compile(r"\{\{\s*inputs\.(\w+)")
    refs = set(ref_pattern.findall(wf_path.read_text(encoding="utf-8")))

    # 关联 agent.md（workflows/agents/<wf-relevant>/*.md）；保守起见扫所有 agent.md
    # 中的「同 workflow input 引用」——按 yaml 的 agent: <name> 字段定位更准但成本高，
    # 此处采用「扫所有 struct/kd/quant/nas agent.md，过滤掉 declared 不在当前 wf 的」。
    agent_dir = REPO / "workflows" / "agents"
    for agent_md in agent_dir.rglob("agent.md"):
        text = agent_md.read_text(encoding="utf-8")
        for ref in ref_pattern.findall(text):
            # 只关心本 workflow declared 的 input key（其它 workflow 的同名 input 不算违规）
            if ref in declared:
                refs.add(ref)

    undeclared = refs - declared
    assert not undeclared, (
        f"{wf_path.name}: `{{{{ inputs.X }}}}` 引用了未声明的 input {sorted(undeclared)}；"
        f"declared = {sorted(declared)}"
    )

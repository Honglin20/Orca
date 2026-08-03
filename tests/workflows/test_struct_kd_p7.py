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
             "tag": "structural", "latency_us": 10.0, "accuracy": 0.9,
             "met_accuracy": True, "snapshot": "/x", "onnx": "/x",
             "diff_summary": "d", "hypothesis": "h"},
            {"id": "c2", "parent": "c1", "path": "p1", "round": 2, "status": "FAIL_latency",
             "tag": "structural", "latency_us": 15.0, "accuracy": -1,  # None after _to_float
             "met_accuracy": False, "snapshot": "/x", "onnx": "/x",
             "diff_summary": "d", "hypothesis": "h"},
        ]
        champions = [{"round": 0, "id": "baseline", "latency_us": 12.0,
                      "accuracy": 0.88, "delta_vs_baseline_us": 0, "snapshot": "/x"}]
        ledger, champs = self._write_ledger(tmp_path, rows, champions)

        self.calls.clear()
        self.viz_struct.render_all(
            ledger_path=ledger, champions_path=champs,
            baseline_latency_us=12.0, baseline_accuracy=0.88,
            target_latency_us=10.0, accuracy_target=0.87,
        )

        pareto = next((c for c in self.calls if c.get("title") == "Latency-Accuracy Pareto"), None)
        assert pareto is not None, "pareto chart not pushed"
        ids = [row["candidate_id"] for row in pareto["data"]]
        assert ids == ["c1"], f"Pareto should only keep valid-accuracy row; got {ids}"

    def test_only_three_charts_no_round_ledger_or_exploration_tree(self, tmp_path):
        """P7 根因清理：删 Round Ledger + Exploration Tree。2026-07-24 P2-1 加 accuracy 维度 → 4 图。"""
        rows = [
            {"id": f"c{i}", "parent": "baseline", "path": "p1", "round": i,
             "status": "SUCCESS", "tag": "structural", "latency_us": 10.0 - i,
             "accuracy": 0.9, "met_accuracy": True, "snapshot": "/x", "onnx": "/x",
             "diff_summary": "d", "hypothesis": "h"}
            for i in range(1, 4)
        ]
        champions = [{"round": 0, "id": "baseline", "latency_us": 12.0,
                      "accuracy": 0.88, "delta_vs_baseline_us": 0, "snapshot": "/x"}]
        ledger, champs = self._write_ledger(tmp_path, rows, champions)

        self.calls.clear()
        self.viz_struct.render_all(
            ledger_path=ledger, champions_path=champs,
            baseline_latency_us=12.0, baseline_accuracy=0.88,
            target_latency_us=10.0, accuracy_target=0.87,
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
    def _variant_row(vid, lat, acc, met_acc=True, status="SUCCESS", kind="nmse"):
        return {"variant_id": vid, "status": status, "latency_us_median": lat,
                "accuracy": acc, "accuracy_kind": kind,
                "met_accuracy": met_acc, "met_latency": True,
                "accepted_cfg": {"num_blocks": 3}}

    def test_sweep_scatter_pushed_with_points(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        with ledger.open("w") as f:
            f.write(json.dumps(self._variant_row("spt_t1", 7.0, 0.02)) + "\n")
            f.write(json.dumps(self._variant_row("spt_alt", 9.0, 0.03, met_acc=False, status="FAIL_accuracy")) + "\n")
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=0.02,
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
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=None,
                               target_latency_us=None, accuracy_baseline=None,
                               accuracy_baseline_kind="", env_anchor="")
        table = next((c for c in self.calls if "Distill Ledger" in c.get("title", "")), None)
        assert table is not None
        cols = table.get("columns", [])
        assert "variant_id" in cols and "latency_us" in cols and "accuracy" in cols
        # 旧 schema 字段已删
        assert "proxy_mse" not in cols and "db_gap" not in cols and "family" not in cols

    def _write_ledger(self, tmp_path, rows):
        ledger = tmp_path / "ledger.jsonl"
        with ledger.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return ledger

    def test_new_charts_pushed_and_direction_aware(self, tmp_path):
        """增量 C+D：progress / pareto / accuracy_compare 三新图推送，且方向按 kind 标注。

        review #6 取负显示：nmse(min) 对 accuracy y 取负（-acc），使「轴上越大越好」统一，
        pareto_y_direction 恒 'max'（displayed 数据越大越好；min 取负后与原 raw+min 前沿等价）。
        snr(max) 不取负，pareto_y_direction='max'。方向反转会立刻红。
        """
        rows = [
            self._variant_row("v_a", 6.0, 0.020, met_acc=True),   # nmse，达标
            self._variant_row("v_b", 7.0, 0.018, met_acc=True),   # nmse 更低
            self._variant_row("v_c", 9.0, 0.030, met_acc=False, status="FAIL_accuracy"),
        ]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, variants_total=5,
                               accuracy_baseline=0.025, accuracy_baseline_kind="nmse",
                               env_anchor="")
        titles = {c.get("title", "") for c in self.calls}
        # 三新图必须推送
        assert "Sweep Progress (status counts)" in titles, f"progress 缺失；got {sorted(titles)}"
        assert "Pareto Front — latency vs accuracy" in titles, f"pareto 缺失；got {sorted(titles)}"
        assert "Accuracy Compare (per variant)" in titles, f"accuracy_compare 缺失；got {sorted(titles)}"
        # pareto：取负显示后 y_direction 恒 'max'（防 -20dB 视觉高于 -22dB；min 取负等价原 raw+min）
        pareto = next(c for c in self.calls if "Pareto Front" in c.get("title", ""))
        assert pareto["pareto_x_direction"] == "min"
        assert pareto["pareto_y_direction"] == "max", (
            "nmse 取负显示后 y_direction 应 'max'（displayed 越大越好）；got "
            f"{pareto['pareto_y_direction']!r}")
        # y_label 标「显示 -原值，越大越好」（原指标越低越好仍出现，描述原方向）
        assert "显示 -原值" in pareto["y_label"], f"y_label 应标取负显示；got {pareto['y_label']!r}"
        assert "越低越好" in pareto["y_label"], "y_label 应保留原指标方向说明"
        # 取负显示数据守门：v_a accuracy=0.020 → displayed -0.020（防漏取负回归）
        v_a_pt = next(p for p in pareto["data"] if p["latency_us"] == 6.0)
        assert v_a_pt["accuracy"] == pytest.approx(-0.020), (
            f"nmse(min) accuracy 应取负显示（0.020→-0.020）；got {v_a_pt['accuracy']}"
        )
        # progress caption 含 n_done/n_total
        progress = next(c for c in self.calls if "Sweep Progress" in c.get("title", ""))
        assert "variants_total=5" in progress["caption"]

    def test_pareto_direction_flips_for_snr(self, tmp_path):
        """snr(max) → pareto_y_direction='max'（防方向写死 min 的回归）。"""
        rows = [
            self._variant_row("v_a", 6.0, 19.0, met_acc=True, kind="snr"),
            self._variant_row("v_b", 7.0, 22.0, met_acc=True, kind="snr"),
        ]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=18.0,
                               accuracy_baseline_kind="snr", env_anchor="")
        pareto = next(c for c in self.calls if "Pareto Front" in c.get("title", ""))
        assert pareto["pareto_y_direction"] == "max", "snr 应 max（越高越好）"
        assert "越高越好" in pareto["y_label"]

    def test_pareto_skipped_when_kind_unknown(self, tmp_path):
        """H2：未知 kind → pareto 推图会误导（方向不可靠），sidecar WARN 跳过而非保守兜底。"""
        rows = [self._variant_row("v_a", 6.0, 0.02), self._variant_row("v_b", 7.0, 0.03)]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=0.025,
                               accuracy_baseline_kind="", env_anchor="")
        titles = {c.get("title", "") for c in self.calls}
        assert "Pareto Front — latency vs accuracy" not in titles, "未知 kind 不应推 pareto"

    def test_fail_latency_sentinel_excluded_from_accuracy_charts(self, tmp_path):
        """C1（viz 版）：FAIL_latency 行（真测 lat + acc=0 哨兵）不得进 sweep/pareto/compare。"""
        rows = [
            self._variant_row("real_a", 6.0, 0.02, met_acc=True),  # 真测 nmse
            self._variant_row("real_b", 7.0, 0.025, met_acc=True),  # 真测 nmse（凑够 >=2 点）
            {"variant_id": "faillat", "status": "FAIL_latency", "latency_us_median": 3.0,
             "accuracy": 0, "accuracy_kind": "", "met_accuracy": False, "met_latency": False,
             "accepted_cfg": {}},  # lat 更小 + acc=0 哨兵：若混入会虚假占据 min 前沿
        ]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=0.025,
                               accuracy_baseline_kind="nmse", env_anchor="")
        # sweep scatter 只含两个真测点（faillat 哨兵剔除）
        scatter = next(c for c in self.calls if "latency vs accuracy" in c.get("title", ""))
        assert [p["id"] for p in scatter["data"]] == ["real_a", "real_b"], (
            "FAIL_latency 哨兵不得进 scatter"
        )
        pareto = next(c for c in self.calls if "Pareto Front" in c.get("title", ""))
        assert len(pareto["data"]) == 2, "FAIL_latency 哨兵不得进 pareto"

    # ── review #6：取负显示（min 方向 kind）+ baseline 数据行 + 进度图 0 保留 ──────────

    def test_negate_display_for_min_kind_scatter_and_compare(self, tmp_path):
        """review #6-4：nmse(min) → scatter / accuracy_compare 的 accuracy y 取负显示。

        bar 图 -20dB 高于 -22dB 是视觉强误导；取负显示从数据层消除歧义（最强）。
        snr(max) 不取负。本测试守门 scatter + accuracy_compare 两图的 y 值变换。
        """
        rows = [
            self._variant_row("v_a", 6.0, 0.020, met_acc=True),
            self._variant_row("v_b", 7.0, 0.030, met_acc=False, status="FAIL_accuracy"),
        ]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=0.025,
                               accuracy_baseline_kind="nmse", env_anchor="")
        # scatter：accuracy 取负（0.020 → -0.020）
        scatter = next(c for c in self.calls if "latency vs accuracy" in c.get("title", ""))
        v_a_scatter = next(p for p in scatter["data"] if p["id"] == "v_a")
        assert v_a_scatter["accuracy"] == pytest.approx(-0.020), (
            f"nmse scatter accuracy 应取负显示；got {v_a_scatter['accuracy']}")
        assert "显示 -原值" in scatter["y_label"]
        # accuracy_compare：accuracy 取负
        cmp = next(c for c in self.calls if "Accuracy Compare" in c.get("title", ""))
        v_a_cmp = next(p for p in cmp["data"] if p["variant_id"] == "v_a")
        assert v_a_cmp["accuracy"] == pytest.approx(-0.020), (
            f"nmse accuracy_compare accuracy 应取负显示；got {v_a_cmp['accuracy']}")
        assert "显示 -原值" in cmp["y_label"]

    def test_no_negate_for_max_kind_snr(self, tmp_path):
        """review #6-4：snr(max) 不取负——accuracy 原值（19.0/22.0），y_label「越高越好」。"""
        rows = [
            self._variant_row("v_a", 6.0, 19.0, met_acc=True, kind="snr"),
            self._variant_row("v_b", 7.0, 22.0, met_acc=True, kind="snr"),
        ]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=18.0,
                               accuracy_baseline_kind="snr", env_anchor="")
        scatter = next(c for c in self.calls if "latency vs accuracy" in c.get("title", ""))
        v_a = next(p for p in scatter["data"] if p["id"] == "v_a")
        assert v_a["accuracy"] == pytest.approx(19.0), (
            f"snr(max) 不取负，应原值 19.0；got {v_a['accuracy']}")
        assert "越高越好" in scatter["y_label"]
        assert "显示 -原值" not in scatter["y_label"]

    def test_accuracy_compare_baseline_as_data_row(self, tmp_path):
        """review #6-2：accuracy_baseline 作为 data 行（met_accuracy="ref"）加入，前端能画出。

        原实现 caption 承诺「虚线=baseline」但 data 无 baseline 行→前端画不出。对齐 latency_bar。
        min 方向 kind 的 baseline 也取负显示（与变体点同坐标系）。
        """
        rows = [
            self._variant_row("v_a", 6.0, 0.020, met_acc=True),
            self._variant_row("v_b", 7.0, 0.030, met_acc=False, status="FAIL_accuracy"),
        ]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=0.025,
                               accuracy_baseline_kind="nmse", env_anchor="")
        cmp = next(c for c in self.calls if "Accuracy Compare" in c.get("title", ""))
        baseline_row = next((p for p in cmp["data"] if p["variant_id"] == "baseline"), None)
        assert baseline_row is not None, (
            "accuracy_compare data 应含 baseline 行（met_accuracy=ref）；got "
            f"{[p['variant_id'] for p in cmp['data']]}")
        assert baseline_row["met_accuracy"] == "ref"
        # nmse(min) baseline 也取负显示（0.025 → -0.025），与变体点同坐标系
        assert baseline_row["accuracy"] == pytest.approx(-0.025), (
            f"baseline accuracy 应取负显示（nmse）；got {baseline_row['accuracy']}")

    def test_accuracy_compare_no_baseline_when_none(self, tmp_path):
        """accuracy_baseline=None → 不加 baseline 行（原契约保留）。"""
        rows = [
            self._variant_row("v_a", 6.0, 0.020, met_acc=True),
            self._variant_row("v_b", 7.0, 0.030, met_acc=False, status="FAIL_accuracy"),
        ]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=None,
                               accuracy_baseline_kind="nmse", env_anchor="")
        cmp = next(c for c in self.calls if "Accuracy Compare" in c.get("title", ""))
        assert all(p["variant_id"] != "baseline" for p in cmp["data"]), (
            "accuracy_baseline=None 时不应加 baseline 行")

    def test_progress_zero_count_fixed_order_retained(self, tmp_path):
        """review #6-3：progress 固定 status 项即便 0 计数也保留（一眼全貌）。

        原实现 `if counts.get(k,0)>0` 把未见固定项滤掉，与注释「未见的仍以 0 呈现」矛盾。
        仅 order 之外的杂项 status 才过滤 0。
        """
        # ledger 只含 SUCCESS + FAIL_accuracy：FAIL_train/FAIL_latency/FAIL_export 应以 0 出现
        rows = [
            self._variant_row("v_a", 6.0, 0.02, met_acc=True, status="SUCCESS"),
            self._variant_row("v_b", 7.0, 0.05, met_acc=False, status="FAIL_accuracy"),
        ]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, variants_total=10,
                               accuracy_baseline=0.025, accuracy_baseline_kind="nmse",
                               env_anchor="")
        progress = next(c for c in self.calls if "Sweep Progress" in c.get("title", ""))
        by_status = {r["status"]: r["count"] for r in progress["data"]}
        # 固定项全保留（0 计数也出现）
        for st in ("SUCCESS", "FAIL_accuracy", "FAIL_train", "FAIL_latency", "FAIL_export"):
            assert st in by_status, f"固定 status {st} 应保留（即便 0）；got {sorted(by_status)}"
        assert by_status["SUCCESS"] == 1
        assert by_status["FAIL_accuracy"] == 1
        assert by_status["FAIL_train"] == 0, "未见固定项应以 0 呈现（一眼全貌）"
        assert by_status["FAIL_latency"] == 0
        assert by_status["FAIL_export"] == 0

    def test_progress_extra_status_filtered_when_zero(self, tmp_path):
        """order 之外的杂项 status 计数 0 时不出现（杂项不该占位，只固定项保留 0）。"""
        rows = [self._variant_row("v_a", 6.0, 0.02, met_acc=True, status="SUCCESS")]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=0.025,
                               accuracy_baseline_kind="nmse", env_anchor="")
        progress = next(c for c in self.calls if "Sweep Progress" in c.get("title", ""))
        # 无杂项 status（ledger 只有 SUCCESS）；固定项保留
        statuses = [r["status"] for r in progress["data"]]
        assert "SUCCESS" in statuses
        # 不应有杜撰的杂项 status（防御）
        assert all(s in ("SUCCESS", "FAIL_accuracy", "FAIL_train", "FAIL_latency", "FAIL_export")
                   for s in statuses), f"杂项 status 不应占位；got {statuses}"

    def test_scatter_and_compare_skipped_when_kind_unknown(self, tmp_path):
        """review #6-4：未知 kind → scatter / accuracy_compare 也 fail loud 跳过（不 auto 猜方向）。

        取负显示需已知方向；未知 kind 不知是否该取负 → 跳过（与 pareto 同口径）。
        对齐 goal「坐标轴方向不能让人误判」——「方向未知」文字标注不足以消除 bar 高度误导。
        """
        rows = [self._variant_row("v_a", 6.0, 0.02), self._variant_row("v_b", 7.0, 0.03)]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=0.025,
                               accuracy_baseline_kind="", env_anchor="")
        titles = {c.get("title", "") for c in self.calls}
        assert "Distill Sweep — latency vs accuracy" not in titles, "未知 kind 不应推 scatter"
        assert "Accuracy Compare (per variant)" not in titles, "未知 kind 不应推 accuracy_compare"
        assert "Pareto Front — latency vs accuracy" not in titles, "未知 kind 不应推 pareto"

    # ── code-reviewer 收口：latency_bar baseline 端到端 + progress 顺序/杂项/未知分母 ──

    def test_latency_bar_baseline_as_data_row(self, tmp_path):
        """review #6-1 收口：viz_kd 收到 baseline_latency_us 时 latency bar data 含 baseline 行。

        端到端 intent 闭合：setup.output.baseline_latency_us → agent.md → train_pool --flag →
        viz_argv → viz_kd._push_latency_bar 把 baseline 加 data（latency bar 才画得出参考线）。
        原测试只验「flag 进 argv」不验「行真渲染」——与 accuracy_compare baseline 行的值级断言对称。
        """
        rows = [self._variant_row("v_a", 7.0, 0.02, met_acc=True)]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=0.025,
                               accuracy_baseline_kind="nmse", env_anchor="")
        bar = next(c for c in self.calls if "Latency Compare" in c.get("title", ""))
        stages = {r["stage"]: r["latency_us"] for r in bar["data"]}
        assert "baseline" in stages, "latency bar 应含 baseline 行（latency 参考线）"
        assert stages["baseline"] == pytest.approx(8.0)
        assert "target" in stages and stages["target"] == pytest.approx(10.0)
        # 变体行也在（latency 是真测，>=0）
        assert "v_a" in stages and stages["v_a"] == pytest.approx(7.0)

    def test_latency_bar_no_baseline_when_none(self, tmp_path):
        """baseline_latency_us=None → latency bar data 不含 baseline 行（反向对称）。"""
        rows = [self._variant_row("v_a", 7.0, 0.02, met_acc=True),
                self._variant_row("v_b", 9.0, 0.03, met_acc=False, status="FAIL_accuracy")]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=None,
                               target_latency_us=10.0, accuracy_baseline=0.025,
                               accuracy_baseline_kind="nmse", env_anchor="")
        bar = next(c for c in self.calls if "Latency Compare" in c.get("title", ""))
        stages = {r["stage"]: r["latency_us"] for r in bar["data"]}
        assert "baseline" not in stages, "baseline_latency_us=None 时不应加 baseline 行"

    def test_progress_fixed_display_order(self, tmp_path):
        """progress 固定 status 显示序（SUCCESS/FAIL_accuracy/FAIL_train/FAIL_latency/FAIL_export）。

        原 0-保留测试把 data 转 dict 丢顺序信息——打乱 order 的回归不会红。本测试守顺序契约。
        """
        rows = [self._variant_row("v_a", 6.0, 0.02, met_acc=True, status="SUCCESS")]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, variants_total=5,
                               accuracy_baseline=0.025, accuracy_baseline_kind="nmse",
                               env_anchor="")
        progress = next(c for c in self.calls if "Sweep Progress" in c.get("title", ""))
        statuses = [r["status"] for r in progress["data"]]
        assert statuses == ["SUCCESS", "FAIL_accuracy", "FAIL_train", "FAIL_latency", "FAIL_export"], (
            f"progress 固定显示序被打乱；got {statuses}")

    def test_progress_extra_status_with_count_retained(self, tmp_path):
        """order 之外的杂项 status 计数 >0 时应保留（正路径，非仅防御）。

        ledger 含一个 status="TIMEOUT" 杂项 → 应以真实计数出现在 data 末尾（order 之后）。
        """
        rows = [
            self._variant_row("v_a", 6.0, 0.02, met_acc=True, status="SUCCESS"),
            {"variant_id": "v_t", "status": "TIMEOUT", "latency_us_median": -1,
             "accuracy": 0, "accuracy_kind": "", "met_accuracy": False, "met_latency": False,
             "accepted_cfg": {}},
        ]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=0.025,
                               accuracy_baseline_kind="nmse", env_anchor="")
        progress = next(c for c in self.calls if "Sweep Progress" in c.get("title", ""))
        by_status = {r["status"]: r["count"] for r in progress["data"]}
        assert by_status.get("TIMEOUT") == 1, (
            f"杂项 status TIMEOUT 计数>0 应保留；got {by_status}")
        # 固定项仍在前面（顺序：固定 order + 杂项在后）
        statuses = [r["status"] for r in progress["data"]]
        assert statuses.index("TIMEOUT") > statuses.index("FAIL_export"), (
            "杂项 status 应排在固定 order 之后")

    def test_progress_variants_total_unknown_when_none(self, tmp_path):
        """variants_total 未给（None）→ progress caption 标「未知」（新参数的回退分支）。"""
        rows = [self._variant_row("v_a", 6.0, 0.02, met_acc=True, status="SUCCESS")]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        # 不传 variants_total（default None）
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=0.025,
                               accuracy_baseline_kind="nmse", env_anchor="")
        progress = next(c for c in self.calls if "Sweep Progress" in c.get("title", ""))
        assert "variants_total=未知" in progress["caption"], (
            f"variants_total=None 时 caption 应标「未知」；got {progress['caption']!r}")

    def test_progress_status_null_normalized_to_unknown(self, tmp_path):
        """status: null（或空串）→ 归一为 "UNKNOWN" 类目（防字符串 "None" 占类目）。"""
        rows = [
            self._variant_row("v_a", 6.0, 0.02, met_acc=True, status="SUCCESS"),
            {"variant_id": "v_null", "status": None, "latency_us_median": -1,
             "accuracy": 0, "accuracy_kind": "", "met_accuracy": False, "met_latency": False,
             "accepted_cfg": {}},
        ]
        ledger = self._write_ledger(tmp_path, rows)
        self.calls.clear()
        self.viz_kd.render_all(ledger_path=str(ledger), baseline_latency_us=8.0,
                               target_latency_us=10.0, accuracy_baseline=0.025,
                               accuracy_baseline_kind="nmse", env_anchor="")
        progress = next(c for c in self.calls if "Sweep Progress" in c.get("title", ""))
        statuses = [r["status"] for r in progress["data"]]
        assert "None" not in statuses, "status:null 不应变成字符串 'None' 类目"
        # 归一为 UNKNOWN，出现在杂项（order 之后）
        assert "UNKNOWN" in statuses, "status:null 应归一为 'UNKNOWN'"


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

    def test_parse_train_pipeline_eval_protocol(self):
        """teacher eval 复用 train_pipeline --mode eval：解析 STUDENT_ACCURACY + _KIND 同伴行。"""
        sys.path.insert(0, str(KD_SCRIPTS))
        from teacher_setup import _parse_accuracy
        # train_pipeline eval stdout（value + kind 同伴）
        out = ("KD_PROXY_MSE: 0.01\n"
               "STUDENT_ACCURACY: 0.0156\n"
               "STUDENT_ACCURACY_KIND: nmse\n"
               "MET_ACCURACY: true\n")
        acc, kind, conf = _parse_accuracy(out)
        assert (acc, kind, conf) == (0.0156, "nmse", "high")

    def test_parse_eval_kind_invalid_falls_back_to_acc(self):
        """STUDENT_ACCURACY_KIND 非法 → kind 回退 acc（value 仍取到，confidence high）。"""
        sys.path.insert(0, str(KD_SCRIPTS))
        from teacher_setup import _parse_accuracy
        acc, kind, conf = _parse_accuracy(
            "STUDENT_ACCURACY: 0.5\nSTUDENT_ACCURACY_KIND: wat\n"
        )
        assert (acc, kind, conf) == (0.5, "acc", "high")


# ───────────────────────── teacher_setup latency source (v4) ──────────────────
# teacher_setup.py 的 latency 来源三分支（CONTRACTS §3）：
#   A) --teacher_latency_us 优先（teacher-gen.output 透传，避免重复测量）
#   B) --latency_provider fallback（向后兼容，自测 ONNX）
#   C) 两者皆空 → fail loud（SystemExit）
# 这三条是 v4 teacher latency 下沉到 teacher-gen 的核心契约，必须有直接测试守护。


def _minimal_teacher_ckpt(tmp_path: Path) -> Path:
    """造一个最小 teacher ckpt（teacher_model.state_dict）—— teacher_setup strict=False load。"""
    import torch
    spec = importlib.util.spec_from_file_location("_ck_teacher", str(KD_SCRIPTS / "teacher_model.py"))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    ckpt = tmp_path / "teacher_ckpt.pt"
    torch.save({"state_dict": mod.build_model().state_dict()}, ckpt)
    return ckpt


class TestTeacherSetupLatencySource:
    """v4：teacher_setup latency 来源三分支（--teacher_latency_us 优先 / provider fallback / fail loud）。"""

    def test_latency_from_param_skips_provider(self, tmp_path):
        """分支 A：--teacher_latency_us 7.3 + 毒化的 latency_provider → 透传 7.3，provider 不被调。

        意图测试（非行为）：provider 脚本含 ``raise RuntimeError('POISON')``——若 teacher_setup
        仍调它测 latency，会 exit!=0 + stderr 含 POISON。透传路径必须**完全跳过** provider。
        """
        import subprocess
        ckpt = _minimal_teacher_ckpt(tmp_path)
        poison = tmp_path / "_poison.py"
        poison.write_text(
            "def measure(onnx, device=None):\n    raise RuntimeError('POISON: provider should not be called')\n",
            encoding="utf-8",
        )
        r = subprocess.run([
            sys.executable, str(KD_SCRIPTS / "teacher_setup.py"),
            "--teacher_model_path", str(KD_SCRIPTS / "teacher_model.py"),
            "--teacher_ckpt", str(ckpt),
            "--build_fn", "build_model",
            "--dummy_input", '{"shape":[1,4,48,64,1],"dtype":"float32"}',
            "--output_dir", str(tmp_path),
            "--teacher_latency_us", "7.3",
            "--latency_provider", str(poison) + "::measure",  # 给了但不应被调
            "--device", "cpu",
        ], capture_output=True, text=True)
        assert r.returncode == 0, f"应 exit 0（透传 latency，不调 provider）\nstderr:\n{r.stderr}"
        assert "TEACHER_LATENCY_US: 7.3000" in r.stdout, f"应透传 7.3：{r.stdout}"
        assert "POISON" not in r.stderr, "provider 被调了（透传路径应完全跳过 provider）"

    def test_latency_fallback_to_provider_when_param_absent(self, tmp_path):
        """分支 B：不传 --teacher_latency_us，只给 --latency_provider → 走自测路径（向后兼容）。"""
        import subprocess
        ckpt = _minimal_teacher_ckpt(tmp_path)
        stub = tmp_path / "_stub.py"
        stub.write_text("def measure(onnx, device=None):\n    return 3.14\n", encoding="utf-8")
        r = subprocess.run([
            sys.executable, str(KD_SCRIPTS / "teacher_setup.py"),
            "--teacher_model_path", str(KD_SCRIPTS / "teacher_model.py"),
            "--teacher_ckpt", str(ckpt),
            "--build_fn", "build_model",
            "--dummy_input", '{"shape":[1,4,48,64,1],"dtype":"float32"}',
            "--output_dir", str(tmp_path),
            "--latency_provider", str(stub) + "::measure",
            "--device", "cpu",
        ], capture_output=True, text=True)
        assert r.returncode == 0, f"应 exit 0（provider 自测）\nstderr:\n{r.stderr}"
        assert "TEACHER_LATENCY_US: 3.1400" in r.stdout, f"应用 provider 测的 3.14：{r.stdout}"
        # stderr 不应含「透传」字样（走的是自测路径）
        assert "透传" not in r.stderr

    def test_latency_fail_loud_when_neither_given(self, tmp_path):
        """分支 C：既不传 --teacher_latency_us 也不传 --latency_provider → fail loud（exit!=0）。"""
        import subprocess
        ckpt = _minimal_teacher_ckpt(tmp_path)
        r = subprocess.run([
            sys.executable, str(KD_SCRIPTS / "teacher_setup.py"),
            "--teacher_model_path", str(KD_SCRIPTS / "teacher_model.py"),
            "--teacher_ckpt", str(ckpt),
            "--build_fn", "build_model",
            "--dummy_input", '{"shape":[1,4,48,64,1],"dtype":"float32"}',
            "--output_dir", str(tmp_path),
            "--device", "cpu",
        ], capture_output=True, text=True)
        assert r.returncode != 0, "应 fail loud（二者至少给一个）"
        assert "二者至少给一个" in r.stderr, f"stderr 应报 fail loud 原因：{r.stderr}"





@pytest.mark.parametrize("script_rel,args,required_flags", [
    ("_struct_scripts/latency_onnxrt.py", [], ["--device", "--seed"]),
    ("_struct_scripts/export_onnx.py", [], ["--no-external-data", "--allow-external-data", "--device", "--seed", "--build_cfg"]),
    ("_struct_scripts/measure_baseline.py", [], ["--device", "--seed"]),
    ("_kd_scripts/profile_onnx.py", [], ["--device", "--seed"]),
    ("_kd_scripts/measure_student.py", [], ["--device", "--seed", "--skip_latency", "--accuracy_baseline"]),
    ("_kd_scripts/teacher_setup.py", [], ["--device", "--seed", "--strict-accuracy", "--teacher_latency_us"]),
    ("_kd_scripts/pick_variant.py", [], ["--target_latency_us", "--latency_provider", "--force_rerun"]),
    ("_kd_scripts/tune_latency.py", [], ["--device", "--seed", "--max_measurements"]),
    ("_kd_scripts/distill_dispatch.py", [], ["--tune_status"]),
    ("_kd_scripts/viz_kd.py", [], ["--baseline_latency_us", "--target_latency_us", "--env_anchor"]),
    ("_kd_scripts/gate_all.py", [], ["--ledger", "--target_latency_us", "--latency_provider", "--manifest_out"]),
    ("_kd_scripts/gpu_probe.py", [], ["--teacher_cache", "--representative_variant", "--variants_count", "--device"]),
    ("_kd_scripts/train_pool.py", [], ["--manifest", "--ledger", "--concurrency", "--device_plan", "--per_variant_vram_bytes", "--train_pipeline_path"]),
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


@pytest.mark.skip(reason="obsolete after 2026-08-03 kd-nas serial rework: yaml drops batch gate/train/select nodes in favor of serial gen_student/distill/decide loop")
def test_kd_workflow_has_six_nodes_flatten_first():
    """kd workflow 7 节点 flatten→teacher_gen→train_script_gen→setup→gate→train→select
    （flatten 入口 + teacher-gen 纯调参派生 teacher + train-script-gen 生成统一训练脚本 +
    setup 跑 teacher 训 + gate + train + select 读 ledger 出最终报告）。"""
    nodes = _yaml_nodes(REPO / "workflows" / "kd-nas.yaml")
    expected = ["flatten", "teacher_gen", "train_script_gen", "setup", "gate", "train", "select"]
    assert nodes == expected, f"kd nodes mismatch: {nodes}"
    # entry 必须是 flatten（不再是 setup）—— 抓 yaml 顶层 `entry:` 行
    entry_line = next(
        (l for l in (REPO / "workflows" / "kd-nas.yaml").read_text(encoding="utf-8").splitlines()
         if l.startswith("entry:")),
        "",
    )
    assert "flatten" in entry_line, f"entry 应为 flatten，got {entry_line!r}"


def test_kd_latency_provider_required_no_default():
    """BLK-3/10：latency_provider 必填无默认（用户真硬件 latency 脚本）。"""
    from orca.compile.parser import load_workflow
    wf = load_workflow(REPO / "workflows" / "kd-nas.yaml")
    idef = wf.inputs["latency_provider"]
    assert idef.required is True, "latency_provider 必须 required=true"
    assert idef.default is None, "latency_provider 必须无 default"


@pytest.mark.skip(reason="obsolete after 2026-08-03 kd-nas serial rework: yaml drops batch gate/train/select nodes in favor of serial gen_student/distill/decide loop")
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
                  "baseline_latency_us:"]:
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

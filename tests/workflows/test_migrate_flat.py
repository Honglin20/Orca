"""tests/workflows/test_migrate_flat.py —— migrate_flat.py 单测（Rule 9：测意图）。

覆盖 plan §3.4 + §9.2c：
- 5 步原子迁移：copy → rewrite（relative_to 算法）→ 行数校验 → os.replace → sentinel → rmtree。
- 全字段 rewrite：ledger.{ckpt, student_path} + champions.{snapshot} +
  teacher_meta.{teacher_onnx, teacher_cache, teacher_ckpt}（路径 under kd_old 才改）。
- per-run 字段不 rewrite：teacher_meta.teacher_model_path（不在 kd_old 子树，原样保留）。
- 行数保持：迁移后 ledger / champions 行数不变。
- sentinel 幂等（E1）：sentinel 缺 → 从 copy 重跑；sentinel 在 → 校验 flat → rmtree 旧。
- --dry-run：只报告，不动文件系统。
- 旧 kd-nas/ 子树迁移后被 rmtree。
- tune_cache.json 不迁移（R2：路径键失效，删旧重建）。
- flat 后 is_variant_done 仍工作（ckpt 路径指 flat 新位置，文件存在 + 路径字面一致）。

D-3.4 行数 + 路径字段一致性是迁移正确性的硬指标；rewrite 算法用 relative_to（非裸 string
replace），防 ``kd-nas-artifacts`` 同前缀 / 项目根含 "kd-nas" 误伤。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KD_SCRIPTS = REPO / "workflows" / "agents" / "_kd_scripts"


def _load_migrate():
    spec = importlib.util.spec_from_file_location("mf_under_test", KD_SCRIPTS / "migrate_flat.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mf_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_old_layout(project_root: Path) -> Path:
    """构造旧 ``<project>/artifacts/kd-nas/`` 完整布局 + 示例数据。

    含 ledger / champions / teacher_meta / checkpoints / models / onnx / meta/tune_cache.json。
    """
    kd_old = project_root / "artifacts" / "kd-nas"
    (kd_old / "checkpoints").mkdir(parents=True)
    (kd_old / "meta").mkdir(parents=True)
    (kd_old / "models" / "students").mkdir(parents=True)
    (kd_old / "models" / "baseline").mkdir(parents=True)
    (kd_old / "models" / "teacher").mkdir(parents=True)
    (kd_old / "onnx" / "tune").mkdir(parents=True)
    (kd_old / "reports").mkdir(parents=True)

    # ckpt 占位文件（验证 rewrite 后路径指 flat 新位置 + 文件存在）
    (kd_old / "checkpoints" / "teacher_ckpt.pt").write_bytes(b"\x00ckpt")
    (kd_old / "checkpoints" / "r1_student.pt").write_bytes(b"\x00r1")
    (kd_old / "models" / "students" / "r1_student_model.py").write_text("# r1", encoding="utf-8")
    (kd_old / "models" / "baseline" / "baseline.py").write_text("# base", encoding="utf-8")
    (kd_old / "onnx" / "teacher.onnx").write_bytes(b"\x00onnx")
    # R2: tune_cache.json 不迁移
    (kd_old / "meta" / "tune_cache.json").write_text(json.dumps({"old_key": "old"}), encoding="utf-8")
    # onnx/tune 临时文件不迁移（仅 onnx/ 浅层保留）
    (kd_old / "onnx" / "tune" / "r1_student.onnx").write_bytes(b"\x00tmp")

    ledger = [
        {
            "variant_id": "r1_student",
            "student_path": str(kd_old / "models" / "students" / "r1_student_model.py"),
            "round": 1, "parent": "baseline", "latency_us": 7.0, "accuracy": 0.021,
            "met_latency": True, "met_accuracy": True, "accuracy_kind": "nmse",
            "direction_id": "scale_1_layer", "hypothesis": "shrink",
            "accepted_cfg": {"num_blocks": 2}, "cfg_hash": "abc123",
            "ckpt": str(kd_old / "checkpoints" / "r1_student.pt"),
            "status": "SUCCESS",
        },
    ]
    (kd_old / "ledger.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in ledger), encoding="utf-8"
    )
    champions = [
        {"round": 0, "id": "baseline", "latency_us": 10.0, "accuracy": 0.02,
         "delta_vs_baseline_us": 0.0, "snapshot": str(kd_old / "models" / "baseline" / "baseline.py")},
        {"round": 1, "id": "r1_student", "latency_us": 7.0, "accuracy": 0.021,
         "delta_vs_baseline_us": -3.0,
         "snapshot": str(kd_old / "models" / "students" / "r1_student_model.py")},
    ]
    (kd_old / "champions.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in champions), encoding="utf-8"
    )
    teacher_meta = {
        # 须 rewrite 的 durable 字段
        "teacher_onnx": str(kd_old / "onnx" / "teacher.onnx"),
        "teacher_cache": str(kd_old / "checkpoints" / "teacher_cache.pt"),
        "teacher_ckpt": str(kd_old / "checkpoints" / "teacher_ckpt.pt"),
        # per-run 字段（禁 rewrite：不在 kd-nas 子树）
        "teacher_model_path": "/tmp/per-run/teacher_wrapper.py",
        "teacher_latency_us": 12.0,
        "teacher_accuracy": 0.019,
    }
    # teacher_cache.pt 占位
    (kd_old / "checkpoints" / "teacher_cache.pt").write_bytes(b"\x00cache")
    (kd_old / "meta" / "teacher_meta.json").write_text(
        json.dumps(teacher_meta, indent=2), encoding="utf-8"
    )
    # reports 也应被 copy（静态文本，无 rewrite）
    (kd_old / "reports" / "final_report.md").write_text("# final", encoding="utf-8")
    return kd_old


# ── 5 步原子迁移 + 全字段 rewrite ──────────────────────────────────────────


def test_migrate_full_happy_path(tmp_path):
    """完整迁移：copy + rewrite + 行数保持 + os.replace + sentinel + rmtree 旧。"""
    mod = _load_migrate()
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    flat_new = project / "artifacts"

    result = mod.migrate(Path(kd_old), Path(flat_new), dry_run=False)

    assert result["action"] == "migrated"
    assert not kd_old.exists(), "旧 kd-nas/ 必须被 rmtree"
    # sentinel
    assert (flat_new / ".migration_done").is_file()
    # ledger / champions 行数保持
    ledger = [json.loads(l) for l in (flat_new / "ledger.jsonl").read_text().splitlines() if l.strip()]
    champions = [json.loads(l) for l in (flat_new / "champions.jsonl").read_text().splitlines() if l.strip()]
    assert len(ledger) == 1
    assert len(champions) == 2
    # 路径 rewrite：flat 后 ckpt / student_path 指向 flat_new（去 kd-nas 层）
    assert ledger[0]["ckpt"] == str(flat_new / "checkpoints" / "r1_student.pt")
    assert ledger[0]["student_path"] == str(flat_new / "models" / "students" / "r1_student_model.py")
    # champions.snapshot 同步 rewrite
    assert champions[1]["snapshot"] == str(flat_new / "models" / "students" / "r1_student_model.py")
    assert champions[0]["snapshot"] == str(flat_new / "models" / "baseline" / "baseline.py")


def test_migrate_teacher_meta_rewrite_durable_only(tmp_path):
    """teacher_meta：durable 字段 rewrite；per-run teacher_model_path 原样保留（A4/E2）。"""
    mod = _load_migrate()
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    flat_new = project / "artifacts"

    mod.migrate(Path(kd_old), Path(flat_new), dry_run=False)

    meta = json.loads((flat_new / "meta" / "teacher_meta.json").read_text(encoding="utf-8"))
    # durable 字段 rewrite（去 kd-nas 层）
    assert meta["teacher_onnx"] == str(flat_new / "onnx" / "teacher.onnx")
    assert meta["teacher_cache"] == str(flat_new / "checkpoints" / "teacher_cache.pt")
    assert meta["teacher_ckpt"] == str(flat_new / "checkpoints" / "teacher_ckpt.pt")
    # per-run 字段原样（不在 kd-nas 子树）
    assert meta["teacher_model_path"] == "/tmp/per-run/teacher_wrapper.py"
    # teacher_cache.pt 文件被 copy（durable 二进制）
    assert (flat_new / "checkpoints" / "teacher_cache.pt").is_file()


def test_migrate_tune_cache_not_migrated(tmp_path):
    """R2：tune_cache.json 不迁移（路径键失效，删旧重建）。"""
    mod = _load_migrate()
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    flat_new = project / "artifacts"

    mod.migrate(Path(kd_old), Path(flat_new), dry_run=False)

    # flat_new 不应含旧 tune_cache.json
    assert not (flat_new / "meta" / "tune_cache.json").is_file()
    # flat_new/meta/ 目录存在（teacher_meta.json 在此）
    assert (flat_new / "meta" / "teacher_meta.json").is_file()


def test_migrate_preserves_row_counts(tmp_path):
    """校验行数 == 旧（manifest 内 old/new 一致）。"""
    mod = _load_migrate()
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    flat_new = project / "artifacts"

    result = mod.migrate(Path(kd_old), Path(flat_new), dry_run=False)

    m = result["manifest"]
    assert m["ledger"]["old_count"] == m["ledger"]["new_count"] == 1
    assert m["champions"]["old_count"] == m["champions"]["new_count"] == 2


# ── sentinel 幂等（E1）─────────────────────────────────────────────────────


def test_migrate_idempotent_sentinel_present(tmp_path):
    """sentinel 在 → 校验 flat 文件存在 → 直接 rmtree kd_old（幂等）。"""
    mod = _load_migrate()
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    flat_new = project / "artifacts"

    # 第一次迁移
    mod.migrate(Path(kd_old), Path(flat_new), dry_run=False)
    assert not kd_old.exists()

    # 重新构造旧 kd-nas/（模拟用户「迁移后又跑了一次旧版 setup」的极端）
    kd_old2 = _make_old_layout(project)
    # sentinel 已在 → 第二次迁移应直接 rmtree kd_old2，不重 copy / rewrite
    result = mod.migrate(Path(kd_old2), Path(flat_new), dry_run=False)
    assert result["action"] == "sentinel_present_rmtree_old"
    assert not kd_old2.exists()


def test_migrate_idempotent_rerun_when_sentinel_missing(tmp_path):
    """sentinel 缺 → 从 copy 重跑（覆盖语义读未动的 kd_old 原始）。"""
    mod = _load_migrate()
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    flat_new = project / "artifacts"
    flat_new.mkdir(parents=True, exist_ok=True)

    # 模拟 partial 中间态：flat_new 已有部分文件（来自之前中断的迁移），但无 sentinel
    (flat_new / "ledger.jsonl").write_text(json.dumps({"stale": True}) + "\n", encoding="utf-8")

    # 重新跑：copy dirs_exist_ok=True 覆盖 stale；rewrite 读未动的 kd_old 原始
    result = mod.migrate(Path(kd_old), Path(flat_new), dry_run=False)

    assert result["action"] == "migrated"
    assert (flat_new / ".migration_done").is_file()
    # stale ledger 被覆盖（不含 stale 字段）
    ledger_text = (flat_new / "ledger.jsonl").read_text(encoding="utf-8")
    assert "stale" not in ledger_text
    # 行数正确（来自 kd_old 原始，非 stale）
    ledger = [json.loads(l) for l in ledger_text.splitlines() if l.strip()]
    assert len(ledger) == 1
    assert ledger[0]["variant_id"] == "r1_student"


# ── dry-run ───────────────────────────────────────────────────────────────


def test_migrate_dry_run_does_not_mutate(tmp_path):
    """--dry-run：只报告，不动文件系统。"""
    mod = _load_migrate()
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    flat_new = project / "artifacts"

    result = mod.migrate(Path(kd_old), Path(flat_new), dry_run=True)

    assert result["action"] == "dry_run"
    # kd_old 不动
    assert kd_old.is_dir()
    assert (kd_old / "ledger.jsonl").is_file()
    # flat_new 不应被写入（无 sentinel / 无 ledger）
    assert not (flat_new / ".migration_done").is_file()
    # would_copy 含预期子目录文件
    would_copy = result["would_copy"]
    assert any("ledger.jsonl" in p for p in would_copy)
    assert any("checkpoints" in p for p in would_copy)
    # tune_cache.json 不在 would_copy
    assert not any("tune_cache.json" in p for p in would_copy)


# ── fail loud：路径布局校验 ─────────────────────────────────────────────────


def test_migrate_rejects_wrong_layout(tmp_path):
    """flat_new 非 kd_old.parent → fail loud（防误用）。"""
    mod = _load_migrate()
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    wrong_flat = tmp_path / "elsewhere"
    wrong_flat.mkdir()

    with pytest.raises(ValueError, match="flat_new 必须是 kd_old 的 parent"):
        mod.migrate(Path(kd_old), Path(wrong_flat), dry_run=False)


def test_migrate_rejects_wrong_kd_old_name(tmp_path):
    """kd_old.name != 'kd-nas' → fail loud（约定）。"""
    mod = _load_migrate()
    project = tmp_path / "proj"
    # 构造非 kd-nas 名的旧目录
    wrong_old = project / "artifacts" / "wrong-name"
    (wrong_old / "checkpoints").mkdir(parents=True)
    flat_new = project / "artifacts"

    with pytest.raises(ValueError, match="kd_old 末段须为 'kd-nas'"):
        mod.migrate(Path(wrong_old), Path(flat_new), dry_run=False)


# ── flat 后 is_variant_done 仍工作（契约守护）────────────────────────────────


def test_migrate_flat_paths_consistent_for_is_variant_done(tmp_path):
    """迁移后 ledger.ckpt 字面值与 flat 文件存在性对齐——is_variant_done 仍能判 SUCCESS+文件存在。

    kd_common.is_variant_done 检查 SUCCESS 行的 ckpt 文件存在；迁移 rewrite 后字面指向 flat 新位置，
    文件被 copy 到该位置 → 路径与文件系统一致，谓词仍工作。
    """
    mod = _load_migrate()
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    flat_new = project / "artifacts"

    mod.migrate(Path(kd_old), Path(flat_new), dry_run=False)

    ledger = [
        json.loads(l)
        for l in (flat_new / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    success_row = next(r for r in ledger if r["status"] == "SUCCESS")
    # 路径字面与 flat 文件系统对齐
    assert success_row["ckpt"].startswith(str(flat_new))
    assert Path(success_row["ckpt"]).is_file(), "迁移后 ckpt 文件应在 flat 路径字面位置存在"


# ── CLI 子进程 smoke（端到端）──────────────────────────────────────────────


def test_migrate_cli_subprocess_dry_run(tmp_path):
    """CLI 子进程：--dry-run 退出 0 + DRY_RUN: 1 + 行数对账。"""
    import subprocess
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    flat_new = project / "artifacts"

    r = subprocess.run(
        [sys.executable, str(KD_SCRIPTS / "migrate_flat.py"),
         "--kd_old", str(kd_old), "--flat_new", str(flat_new), "--dry-run"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr:\n{r.stderr}"
    assert "DRY_RUN: 1" in r.stdout
    assert "LEDGER_COUNTS: old=1 new=1" in r.stdout
    assert "CHAMPIONS_COUNTS: old=2 new=2" in r.stdout
    # kd_old 不动
    assert kd_old.is_dir()


def test_migrate_cli_subprocess_full(tmp_path):
    """CLI 子进程：真跑成功 + MIGRATION_DONE: 1 + 旧 kd-nas 被 rmtree。"""
    import subprocess
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    flat_new = project / "artifacts"

    r = subprocess.run(
        [sys.executable, str(KD_SCRIPTS / "migrate_flat.py"),
         "--kd_old", str(kd_old), "--flat_new", str(flat_new)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr:\n{r.stderr}"
    assert "MIGRATION_DONE: 1" in r.stdout
    assert not kd_old.exists()
    assert (flat_new / ".migration_done").is_file()


# ── fail loud：坏 JSON 行 + teacher_meta 缺失分支（code-reviewer R4/R5）────────


def test_migrate_rejects_corrupt_ledger_jsonl(tmp_path):
    """坏 JSON 行 → fail loud（exit 2 + stderr 含 '非合法 JSON'）。

    守护 ``_read_jsonl`` 关键 fail loud 路径（plan §3.4：ledger 是 domain 真相源，
    corruption → 误 skip/重跑，必须 raise）。
    """
    import subprocess
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    flat_new = project / "artifacts"
    # 覆写 ledger 第二行为坏 JSON
    (kd_old / "ledger.jsonl").write_text(
        '{"variant_id":"r1_student"}\n{not valid json}\n', encoding="utf-8"
    )
    r = subprocess.run(
        [sys.executable, str(KD_SCRIPTS / "migrate_flat.py"),
         "--kd_old", str(kd_old), "--flat_new", str(flat_new)],
        capture_output=True, text=True,
    )
    assert r.returncode != 0, "坏 JSON 行必须 fail loud（exit 2）"
    assert "非合法 JSON" in r.stderr, f"stderr 应报坏行原因：{r.stderr}"
    # 旧 kd_old 不动（迁移失败前 rmtree 未执行）
    assert kd_old.is_dir()
    # sentinel 未写
    assert not (flat_new / ".migration_done").is_file()


def test_migrate_without_teacher_meta(tmp_path):
    """teacher_meta.json 缺失 → 迁移正常完成；manifest.teacher_meta.exists=False。

    场景：旧 kd-nas/ 还没跑过 teacher（无 teacher_setup 阶段产物）——
    迁移不应因 teacher_meta 缺失而崩。
    """
    mod = _load_migrate()
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    (kd_old / "meta" / "teacher_meta.json").unlink()  # 模拟 teacher 未跑
    flat_new = project / "artifacts"

    result = mod.migrate(Path(kd_old), Path(flat_new), dry_run=False)

    assert result["action"] == "migrated"
    assert result["manifest"]["teacher_meta"]["exists"] is False
    assert not (flat_new / ".migration_done").is_file() is False  # sentinel 已写
    # ledger / champions 仍迁移
    assert (flat_new / "ledger.jsonl").is_file()
    assert (flat_new / "champions.jsonl").is_file()


# ── sentinel 数据安全契约（code-reviewer R3）──────────────────────────────


def test_migrate_sentinel_present_rejects_changed_kd_old(tmp_path):
    """sentinel 在但 kd_old 内容已变（用户后续写入新数据）→ fail loud 拒绝 rmtree。

    数据安全契约：静默 rmtree 会丢未迁移新数据。
    """
    mod = _load_migrate()
    project = tmp_path / "proj"
    kd_old = _make_old_layout(project)
    flat_new = project / "artifacts"

    # 第一次迁移（写 sentinel，rmtree kd_old）
    mod.migrate(Path(kd_old), Path(flat_new), dry_run=False)

    # 模拟「迁移后又跑了一次旧版 setup」——kd_old 复活 + 新增 ledger 行
    kd_old2 = _make_old_layout(project)
    with (kd_old2 / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "variant_id": "r2_student", "student_path": "/x", "round": 2,
            "parent": "r1_student", "latency_us": 6.0, "accuracy": 0.020,
            "met_latency": True, "met_accuracy": True, "accuracy_kind": "nmse",
            "direction_id": "ffn_pointwise", "hypothesis": "ffn",
            "accepted_cfg": {}, "cfg_hash": "deadbeef",
            "ckpt": "/x.pt", "status": "SUCCESS",
        }) + "\n")

    # 第二次：sentinel 在 + kd_old 行数变了（1 → 2）→ 必须 raise，不 rmtree
    with pytest.raises(ValueError, match="kd_old/ledger.jsonl 行数"):
        mod.migrate(Path(kd_old2), Path(flat_new), dry_run=False)
    # kd_old2 必须保留（拒绝 rmtree）
    assert kd_old2.is_dir(), "数据不一致时必须保留 kd_old 等人工处理"


# ── 路径字段 rewrite 算法（relative_to，禁裸 string replace）─────────────────


def test_rewrite_path_uses_relative_to_not_string_replace(tmp_path):
    """E3：``Path.relative_to`` 算法——防 'kd-nas-artifacts' 同前缀误伤。

    构造一个路径 ``/foo/kd-nas-artifacts/x.pt``（含 'kd-nas' 子串但不在 kd_old 子树），
    rewrite 应原样返回（不误改）。
    """
    mod = _load_migrate()
    kd_old = tmp_path / "proj" / "artifacts" / "kd-nas"
    kd_old.mkdir(parents=True)
    flat_new = tmp_path / "proj" / "artifacts"
    # 同前缀但不在 kd_old 子树
    p_other = str(tmp_path / "proj" / "artifacts" / "kd-nas-artifacts" / "x.pt")
    assert mod._rewrite_path(p_other, kd_old, flat_new) == p_other

    # 在 kd_old 子树 → rewrite 为 flat_new / rel
    p_inside = str(kd_old / "checkpoints" / "x.pt")
    rewritten = mod._rewrite_path(p_inside, kd_old, flat_new)
    assert rewritten == str(flat_new / "checkpoints" / "x.pt")
    assert "kd-nas" not in rewritten.replace(str(flat_new), "")  # rel 部分无 kd-nas

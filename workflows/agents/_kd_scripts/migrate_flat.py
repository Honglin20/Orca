"""migrate_flat.py —— KD-NAS durable artifacts 拍平迁移。

把旧 ``<project>/artifacts/kd-nas/`` 拍平到 ``<project>/artifacts/``（去 kd-nas 层）。

**5 步原子迁移 + 全字段 rewrite + sentinel 幂等**：

  1. **copy**（非 move，``dirs_exist_ok=True`` 总覆盖语义）旧 ``checkpoints/`` / ``meta/`` /
     ``models/`` / ``onnx/`` / ``reports/`` + 根级 ``ledger.jsonl`` / ``champions.jsonl``
     → 拍平新位置。``meta/tune_cache.json`` 不迁移（latency 缓存删旧重建，换路径
     即作废，迁移无意义）。
  2. **rewrite 路径字段 → ``.new`` 文件**（``Path.relative_to(kd_old) → flat_new / rel``，
     **禁裸 string replace**——防 ``kd-nas-artifacts`` 同前缀 / 项目根含 "kd-nas" 误伤）：
     - ``ledger.{ckpt, student_path}``（``kd_reducer._LEDGER_REQUIRED``）
     - ``champions.{snapshot}``（``_CHAMPIONS_REQUIRED``）
     - ``teacher_meta.{teacher_onnx, teacher_cache, teacher_ckpt}``
     **禁 rewrite**：``teacher_meta.teacher_model_path``（teacher wrapper .py 落 per-run
     ``$ORCA_ARTIFACTS_DIR``，run scope，不在 kd-nas 子树）；``teacher_cache.pt`` 内部
     无路径字段，无需 pickle rewrite。
  3. **校验** 新 ledger / champions 行数 == 旧。
  4. **``os.replace``** 原子替换（逐文件 ``.new`` → 正名）。
  5. **sentinel ``.migration_done``**（manifest 含文件清单 + 行数 + sha256）作最后一步
     原子 touch。
  6. sentinel 成功后才 ``shutil.rmtree`` 旧 ``kd-nas/`` 子树。

**幂等**：sentinel 缺 → 从 copy 重跑（``dirs_exist_ok=True`` 覆盖语义读未动的 kd_old
原始，flat 中间态被覆盖；多文件 partial-replace 后续步骤幂等）。sentinel 在 → 校验 flat
文件存在 → 直接进步骤 6 删旧。

**磁盘峰值 ≈ 2x**：迁移期间旧 + 新 checkpoints 同时存在；迁移完 rmtree 释放。

CLI::

    migrate_flat.py --kd_old <abs artifacts/kd-nas> --flat_new <abs artifacts/>
                    [--dry-run]

stdout ``KEY: value``（最后非空行 ``MIGRATION_DONE: 1`` 表成功；``DRY_RUN: 1`` 表 dry-run）。
fail loud：路径非法 / kd_old 不存在 / 文件损坏 → 非零退出 + stderr。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any


# ── 须 rewrite 的字段拓扑（实施前 grep ``_LEDGER_REQUIRED`` + ``_CHAMPIONS_REQUIRED``
#    + teacher_meta keys 锁死；全字段清单）──────────────────────────────────
_LEDGER_PATH_FIELDS = ("ckpt", "student_path")
_CHAMPIONS_PATH_FIELDS = ("snapshot",)
_TEACHER_META_PATH_FIELDS = ("teacher_onnx", "teacher_cache", "teacher_ckpt")

# copy 的子目录（checkpoints/meta/models/onnx/reports）；logs/ 不迁移（日志走 per-run）。
_COPY_SUBDIRS = ("checkpoints", "meta", "models", "onnx", "reports")
# copy 的根级文件（jsonl 真相源——经 rewrite 步骤改写后落 flat 根）。
_COPY_ROOT_FILES = ("ledger.jsonl", "champions.jsonl")

# tune_cache.json 不迁移（路径键失效，删旧重建）。
_NO_MIGRATE_CACHE = "tune_cache.json"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _rewrite_path(p_str: str, kd_old: Path, flat_new: Path) -> str:
    """``Path(p).relative_to(kd_old) → flat_new / rel``；不在 kd_old 子树 → 原样返回。

    禁裸 string replace（防 ``kd-nas-artifacts`` 同前缀 / 项目根含 "kd-nas" 误伤）。
    """
    p_str = p_str.strip() if isinstance(p_str, str) else p_str
    if not p_str:
        return p_str
    p = Path(p_str)
    try:
        rel = p.relative_to(kd_old)
    except ValueError:
        # 不在 kd_old 子树（如 per-run teacher_model_path）→ 原样返回，不误伤。
        return p_str
    return str(flat_new / rel)


def _rewrite_ledger_rows(
    rows: list[dict[str, Any]], kd_old: Path, flat_new: Path
) -> list[dict[str, Any]]:
    """rewrite ledger 行的 ckpt / student_path（仅在 kd_old 子树下的才改）。"""
    out = []
    for r in rows:
        nr = dict(r)
        for k in _LEDGER_PATH_FIELDS:
            v = nr.get(k, "")
            if isinstance(v, str) and v:
                nr[k] = _rewrite_path(v, kd_old, flat_new)
        out.append(nr)
    return out


def _rewrite_champions_rows(
    rows: list[dict[str, Any]], kd_old: Path, flat_new: Path
) -> list[dict[str, Any]]:
    """rewrite champions 行的 snapshot。"""
    out = []
    for r in rows:
        nr = dict(r)
        for k in _CHAMPIONS_PATH_FIELDS:
            v = nr.get(k, "")
            if isinstance(v, str) and v:
                nr[k] = _rewrite_path(v, kd_old, flat_new)
        out.append(nr)
    return out


def _rewrite_teacher_meta(
    obj: dict[str, Any], kd_old: Path, flat_new: Path
) -> dict[str, Any]:
    """rewrite teacher_meta.json 的 teacher_onnx/teacher_cache/teacher_ckpt。

    **禁 rewrite** ``teacher_model_path``（per-run scope，不在 kd-nas 子树）。
    """
    out = dict(obj)
    for k in _TEACHER_META_PATH_FIELDS:
        v = out.get(k, "")
        if isinstance(v, str) and v:
            out[k] = _rewrite_path(v, kd_old, flat_new)
    return out


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读 jsonl；空文件 → 空列表。fail loud：坏行 / 非 object → raise。"""
    if not path.is_file():
        return []
    out = []
    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{path} 第 {lineno} 行非合法 JSON：{e}\n原文：{s[:200]!r}"
            ) from e
        if not isinstance(obj, dict):
            raise ValueError(f"{path} 第 {lineno} 行非 object：{type(obj).__name__}")
        out.append(obj)
    return out


def _write_text_atomic(path: Path, text: str) -> None:
    """tmpfile + os.replace 原子写（防 partial-write）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _validate_layout(kd_old: Path, flat_new: Path) -> None:
    """校验 kd_old / flat_new 是预期的拍平关系（kd_old = flat_new / 'kd-nas'）。

    防误用：flat_new 必须是 kd_old 的 parent（即拍平去 kd-nas 层）；否则拒绝。
    """
    if not kd_old.is_dir():
        raise FileNotFoundError(f"kd_old 不存在或非目录：{kd_old}")
    if not flat_new.is_dir():
        # flat_new 允许不存在——但预期是 artifacts/ 父目录，setup 已 mkdir；
        # 此处兜底 mkdir（迁移先于 setup 的极端时序，或 setup 委托本脚本建根）。
        flat_new.mkdir(parents=True, exist_ok=True)
    expected_parent = kd_old.parent.resolve()
    actual_parent = flat_new.resolve()
    if expected_parent != actual_parent:
        raise ValueError(
            f"flat_new 必须是 kd_old 的 parent（拍平去 kd-nas 层）："
            f"expected={expected_parent} actual_parent={actual_parent}"
        )
    if kd_old.name != "kd-nas":
        raise ValueError(
            f"kd_old 末段须为 'kd-nas'（约定）；实际：{kd_old.name!r}"
        )


def _copy_subdirs_and_root_files(
    kd_old: Path, flat_new: Path, dry_run: bool
) -> list[str]:
    """copy checkpoints/meta/models/onnx/reports 子目录 + 根级 jsonl → flat_new。

    ``dirs_exist_ok=True`` 总覆盖语义（幂等：重跑覆盖 partial 中间态）。
    ``meta/tune_cache.json`` 删除（latency 缓存路径键失效，删旧重建）。
    返回实际 copy 的文件相对路径列表（manifest 用）。

    **fail loud 适配**：必需子目录（checkpoints/meta/models）+
    根 jsonl（ledger/champions）缺失 → stderr WARN（不 raise，因旧实例可能真空；
    但须让 operator 看到——否则 flat 后路径字段指向不存在文件，is_variant_done 突然
    返 False 触发无必要重训，无任何错误信号）。可选子目录（onnx/reports）缺失静默。
    """
    # 必需子目录 / 根 jsonl 缺失 → WARN（迁移是「不动旧 + fail loud」；
    # 这里数据真空非契约违反，不 raise，但必须可观测——fail loud ≠ 静默）。
    _REQUIRED_SUBS = ("checkpoints", "meta", "models")
    _REQUIRED_ROOT_FILES = ("ledger.jsonl", "champions.jsonl")
    for sub in _REQUIRED_SUBS:
        if not (kd_old / sub).is_dir():
            print(
                f"[migrate_flat] WARN: 必需子目录缺失：kd_old/{sub}/"
                f"（迁移继续；但 flat 后路径字段可能指向不存在文件——is_variant_done 失效）",
                file=sys.stderr,
            )
    for name in _REQUIRED_ROOT_FILES:
        if not (kd_old / name).is_file():
            print(
                f"[migrate_flat] WARN: 必需根 jsonl 缺失：kd_old/{name}"
                f"（迁移继续；flat 后 ledger/champions 为空）",
                file=sys.stderr,
            )

    copied: list[str] = []
    for sub in _COPY_SUBDIRS:
        src = kd_old / sub
        if not src.is_dir():
            continue
        dst = flat_new / sub
        if dry_run:
            for f in src.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(kd_old)
                    # tune_cache.json 不入 manifest（与真跑分支一致）
                    if rel.name == _NO_MIGRATE_CACHE and rel.parent.name == "meta":
                        continue
                    copied.append(str(rel))
            continue
        shutil.copytree(src, dst, dirs_exist_ok=True)
        # 删 tune_cache.json（latency 缓存路径键失效，删旧重建）
        cache_in_dst = dst / "tune_cache.json" if sub == "meta" else None
        if cache_in_dst and cache_in_dst.is_file():
            cache_in_dst.unlink()
        for f in src.rglob("*"):
            if f.is_file():
                rel = f.relative_to(kd_old)
                # tune_cache.json 不入 manifest（未迁移）
                if rel.name == _NO_MIGRATE_CACHE and rel.parent.name == "meta":
                    continue
                copied.append(str(rel))
    # 根级 jsonl（rewrite 阶段会读 kd_old 原版改写——此处先 copy 保 mkdir 一致性；
    # 若担心 .new 中间态与 copy 重复，步骤 4 os.replace 会覆盖到正名）。
    for name in _COPY_ROOT_FILES:
        src = kd_old / name
        if src.is_file():
            copied.append(name)
            if not dry_run:
                shutil.copy2(src, flat_new / name)
    return copied


def _step_rewrite(
    kd_old: Path, flat_new: Path, dry_run: bool
) -> dict[str, Any]:
    """rewrite ledger / champions / teacher_meta 路径字段 → .new 文件。

    Returns: manifest 段（行数 + sha256 of .new）。
    """
    manifest: dict[str, Any] = {}

    # ── ledger ──
    old_ledger_path = kd_old / "ledger.jsonl"
    old_ledger = _read_jsonl(old_ledger_path)
    new_ledger = _rewrite_ledger_rows(old_ledger, kd_old, flat_new)
    new_ledger_path = flat_new / "ledger.jsonl"
    new_ledger_tmp = flat_new / "ledger.jsonl.new"
    if not dry_run:
        new_ledger_tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in new_ledger),
            encoding="utf-8",
        )
    manifest["ledger"] = {
        "old_count": len(old_ledger),
        "new_count": len(new_ledger),
        "sha256": _sha256_file(new_ledger_tmp) if not dry_run else None,
    }

    # ── champions ──
    old_champions_path = kd_old / "champions.jsonl"
    old_champions = _read_jsonl(old_champions_path)
    new_champions = _rewrite_champions_rows(old_champions, kd_old, flat_new)
    new_champions_tmp = flat_new / "champions.jsonl.new"
    if not dry_run:
        new_champions_tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in new_champions),
            encoding="utf-8",
        )
    manifest["champions"] = {
        "old_count": len(old_champions),
        "new_count": len(new_champions),
        "sha256": _sha256_file(new_champions_tmp) if not dry_run else None,
    }

    # ── teacher_meta ──
    old_meta_path = kd_old / "meta" / "teacher_meta.json"
    tm_entry: dict[str, Any] = {"exists": old_meta_path.is_file()}
    if old_meta_path.is_file():
        try:
            old_meta = json.loads(old_meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"teacher_meta.json 非合法 JSON：{e}") from e
        if not isinstance(old_meta, dict):
            raise ValueError(
                f"teacher_meta.json 非 object：{type(old_meta).__name__}"
            )
        new_meta = _rewrite_teacher_meta(old_meta, kd_old, flat_new)
        new_meta_tmp = flat_new / "meta" / "teacher_meta.json.new"
        if not dry_run:
            new_meta_tmp.parent.mkdir(parents=True, exist_ok=True)
            new_meta_tmp.write_text(
                json.dumps(new_meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        tm_entry["sha256"] = _sha256_file(new_meta_tmp) if not dry_run else None
    manifest["teacher_meta"] = tm_entry
    return manifest


def _verify_counts(manifest: dict[str, Any]) -> None:
    """校验 rewrite 后行数不变（ledger / champions）。"""
    for name in ("ledger", "champions"):
        m = manifest.get(name, {})
        if m.get("old_count") != m.get("new_count"):
            raise ValueError(
                f"{name} 行数不一致：old={m.get('old_count')} new={m.get('new_count')}"
            )


def _atomic_replace(flat_new: Path) -> None:
    """逐文件 os.replace ``.new`` → 正名（ledger / champions / teacher_meta）。"""
    for name in ("ledger.jsonl", "champions.jsonl"):
        tmp = flat_new / (name + ".new")
        if tmp.is_file():
            os.replace(tmp, flat_new / name)
    tm_tmp = flat_new / "meta" / "teacher_meta.json.new"
    if tm_tmp.is_file():
        os.replace(tm_tmp, flat_new / "meta" / "teacher_meta.json")


def _write_sentinel(flat_new: Path, manifest: dict[str, Any]) -> Path:
    """sentinel ``.migration_done``（manifest 含文件清单 + 行数 + sha256）作最后一步原子 touch。"""
    sentinel = flat_new / ".migration_done"
    payload = {
        "version": 1,
        "migrated_at": None,  # 由调度器写；脚本禁用 time.time（确定性约定宽松此处，
        # 但为幂等校验而存在；下次 setup 读它只看是否存在 + manifest keys）。
        "manifest": manifest,
    }
    _write_text_atomic(sentinel, json.dumps(payload, ensure_ascii=False, indent=2))
    return sentinel


def _validate_sentinel(flat_new: Path) -> bool:
    """sentinel 存在且指向的 flat 关键文件齐全 → True（直接进步骤 6 删旧）。"""
    sentinel = flat_new / ".migration_done"
    if not sentinel.is_file():
        return False
    # 关键 flat 文件存在性校验（防 sentinel 残留但 flat 文件被外部删的极端）。
    required = [
        flat_new / "ledger.jsonl",
        flat_new / "champions.jsonl",
    ]
    for f in required:
        if not f.is_file():
            return False
    return True


def migrate(kd_old: Path, flat_new: Path, dry_run: bool = False) -> dict[str, Any]:
    """主入口。返回结果 dict（含 manifest / 动作记录）。

    幂等：sentinel 缺 → 从 copy 重跑；sentinel 在 → 校验 flat → rmtree kd_old。
    """
    _validate_layout(kd_old, flat_new)

    # 幂等分支：sentinel 已在 → 校验 flat 文件存在 + kd_old 内容与已迁移时一致 → rmtree kd_old。
    # 数据安全契约：sentinel 在但 kd_old 又出现时，必须确认 kd_old 内容
    # 与已迁移时相同——否则可能含用户后续写入的新数据（旧版 setup 误跑 / 备份恢复），
    # 静默 rmtree 会丢数据。行数不一致 → fail loud（契约：「不动旧、不替换、fail loud」）。
    if not dry_run and _validate_sentinel(flat_new):
        if kd_old.exists():
            sentinel_data = json.loads(
                (flat_new / ".migration_done").read_text(encoding="utf-8")
            )
            sent_manifest = sentinel_data.get("manifest", {})
            cur_old_ledger = _read_jsonl(kd_old / "ledger.jsonl")
            cur_old_champions = _read_jsonl(kd_old / "champions.jsonl")
            sent_ledger_cnt = sent_manifest.get("ledger", {}).get("old_count")
            sent_champions_cnt = sent_manifest.get("champions", {}).get("old_count")
            if sent_ledger_cnt is not None and len(cur_old_ledger) != sent_ledger_cnt:
                raise ValueError(
                    f"sentinel 在但 kd_old/ledger.jsonl 行数（{len(cur_old_ledger)}）"
                    f"与已迁移时（{sent_ledger_cnt}）不一致；可能含未迁移新数据；"
                    f"拒绝 rmtree——请人工核对后删除 sentinel 重跑或清理 kd_old。"
                )
            if (
                sent_champions_cnt is not None
                and len(cur_old_champions) != sent_champions_cnt
            ):
                raise ValueError(
                    f"sentinel 在但 kd_old/champions.jsonl 行数（{len(cur_old_champions)}）"
                    f"与已迁移时（{sent_champions_cnt}）不一致；拒绝 rmtree（同上）。"
                )
            shutil.rmtree(kd_old)
        return {
            "action": "sentinel_present_rmtree_old",
            "manifest": json.loads(
                (flat_new / ".migration_done").read_text(encoding="utf-8")
            ).get("manifest", {}),
        }

    if dry_run:
        # dry-run：报告将要 copy 哪些 + 行数 + rewrite 路径数；不动文件系统。
        copied = _copy_subdirs_and_root_files(kd_old, flat_new, dry_run=True)
        manifest = _step_rewrite(kd_old, flat_new, dry_run=True)
        _verify_counts(manifest)
        return {
            "action": "dry_run",
            "would_copy": copied,
            "manifest": manifest,
        }

    # 步骤 1：copy（dirs_exist_ok=True 覆盖语义）
    copied = _copy_subdirs_and_root_files(kd_old, flat_new, dry_run=False)

    # 步骤 2：rewrite → .new 文件（Path.relative_to 算法）
    manifest = _step_rewrite(kd_old, flat_new, dry_run=False)
    manifest["copied_files"] = copied

    # 步骤 3：行数校验
    _verify_counts(manifest)

    # 步骤 4：os.replace 原子替换（逐文件）
    _atomic_replace(flat_new)

    # 步骤 5：sentinel（原子 touch，最后一步——之前的步骤全 OK 才 touch）
    sentinel = _write_sentinel(flat_new, manifest)

    # 步骤 6：sentinel 成功 → rmtree 旧 kd-nas/
    shutil.rmtree(kd_old)

    return {
        "action": "migrated",
        "sentinel": str(sentinel),
        "manifest": manifest,
    }


def _main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "KD-NAS durable artifacts 拍平迁移："
            "把旧 <project>/artifacts/kd-nas/ 拍平到 <project>/artifacts/。"
            "5 步原子迁移 + 全字段 rewrite + sentinel 幂等。"
        )
    )
    p.add_argument(
        "--kd_old",
        required=True,
        help="旧 durable 根（<project>/artifacts/kd-nas/）绝对路径",
    )
    p.add_argument(
        "--flat_new",
        required=True,
        help="新拍平 durable 根（<project>/artifacts/）绝对路径",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只报告计划（copy 哪些 + rewrite 行数），不动文件系统",
    )
    args = p.parse_args()

    try:
        kd_old = Path(args.kd_old).resolve()
        flat_new = Path(args.flat_new).resolve()
        result = migrate(kd_old, flat_new, dry_run=args.dry_run)
    except Exception as e:
        print(f"[migrate_flat] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    action = result.get("action", "")
    manifest = result.get("manifest", {})
    print(f"ACTION: {action}")
    if "ledger" in manifest:
        print(
            f"LEDGER_COUNTS: old={manifest['ledger'].get('old_count')} "
            f"new={manifest['ledger'].get('new_count')}"
        )
    if "champions" in manifest:
        print(
            f"CHAMPIONS_COUNTS: old={manifest['champions'].get('old_count')} "
            f"new={manifest['champions'].get('new_count')}"
        )
    if "teacher_meta" in manifest:
        tm = manifest["teacher_meta"]
        print(f"TEACHER_META_MIGRATED: {int(tm.get('exists', False))}")
    if args.dry_run:
        print(f"WOULD_COPY: {len(result.get('would_copy', []))} files")
        print("DRY_RUN: 1")
    else:
        print("MIGRATION_DONE: 1")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

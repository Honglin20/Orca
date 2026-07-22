"""direction_coverage.py —— KB direction 目录覆盖感知（plan sprightly-questing-donut §2.1）。

回答 struct-exploration「选取没有尝试过的结构」的确定性基准：拿 KB 本族 direction 目录作
「结构方向」枚举集，读 ledger 历史算出 tried / untried，喂给 hypothesizer 软闸 prompt
（优先选 untried 方向；超参仅在 catalog 耗尽 / champion 接近目标时兜底）。

**纯函数式、确定性**（[[deterministic-over-model-mediated]]）：
  - 读 KB ``index.json`` + ``families/<family>/meta.json``（tiers 族）枚举 direction 目录
  - 读 ``ledger.jsonl`` 收集历史候选的 ``direction_id``（tried）
  - stdout JSON 输出覆盖信号（不写任何文件、不调 LLM/网络/时钟/随机）

catalog 来源（确定性）：
  - **tiers 族**（如 wireless_receiver，有 ``meta.json`` + ``directions``）：catalog =
    ``meta.json["directions"]`` 的 key（D0..D21），每条带 name + 标签。
  - **单层族**（cnn/transformer，无 ``meta.json``）：catalog = []——无结构化 direction 目录，
    direction 覆盖闸 N/A（hypothesizer 回退靠 latency_moves 提结构，hyperparam 仅 near_target 兜底）。

CLI：
    direction_coverage.py \\
      --ledger <path> \\
      --kb-dir <path=$ORCA_KB_DIR> \\
      --family <family> \\
      [--target-latency-ms <float>] \\
      [--accuracy-target <float>] \\
      [--near-band <float=1.15>]

stdout JSON（curator 透传进 output，供下轮 hypothesizer 读 + viz 可见）：
    {"family":..., "catalog":[{"id":"D0","name":...},...], "catalog_size":N,
     "tried":["D0",...], "untried":["D5",...], "coverage_ratio":0.x,
     "all_exhausted":bool, "near_target":bool}

  - ``all_exhausted`` = catalog 非空且 untried 为空（catalog 方向全试过 → 允许 hyperparam 兜底）。
  - ``near_target`` = champion 已接近目标带（latency <= target*near_band，且 met_accuracy）→
    超参微调可达成，允许 hyperparam。无 target 输入 → False。

退出码：0 正常（stdout JSON）；1 fail loud（KB 缺 / ledger 损 / 异常，stderr 带详情）。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

# champion 接近目标的 latency 容忍带（near_target 判定：champion_latency <= target * NEAR_BAND）。
# 语义：champion 已在 striking distance，超参微调（通道数 / kernel 等）有望补齐剩余 gap。
_NEAR_BAND_DEFAULT = 1.15


def _resolve_kb_dir(cli_kb_dir: str) -> Path:
    """KB 根：CLI --kb-dir > $ORCA_KB_DIR。两者都空 → fail loud（不应发生，run 启动已预检）。"""
    kb = cli_kb_dir.strip() or os.environ.get("ORCA_KB_DIR", "").strip()
    if not kb:
        raise ValueError(
            "KB 根未指定：--kb-dir 与 $ORCA_KB_DIR 均空。workflow 应声明 requires:[knowledge_base] "
            "由 run 启动预检 fail-loud，不应进到本脚本。"
        )
    p = Path(kb)
    if not p.is_dir():
        raise ValueError(f"KB 根不存在：{p}（--kb-dir / $ORCA_KB_DIR 指向失效路径）")
    return p.resolve()


def _load_catalog(kb_dir: Path, family: str) -> list[dict[str, Any]]:
    """枚举本族 direction 目录（确定性）。

    tiers 族（有 ``families/<family>/meta.json`` 含 ``directions``）→ 返回
    ``[{id, name, ascend, latency_tier, risk, attention}, ...]``（按 id 排序）。
    单层族（无 meta.json）→ 返回 ``[]``（无结构化目录，覆盖闸 N/A）。
    """
    meta_path = kb_dir / "families" / family / "meta.json"
    if not meta_path.is_file():
        return []  # 单层族：无 direction 目录
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"meta.json 解析失败（{meta_path}）：{e}") from e
    directions = meta.get("directions")
    if not isinstance(directions, dict) or not directions:
        return []
    catalog: list[dict[str, Any]] = []
    for did, meta_d in directions.items():
        if not isinstance(meta_d, dict):
            continue
        catalog.append({
            "id": did,
            "name": meta_d.get("name", ""),
            "ascend": meta_d.get("ascend", ""),
            "latency_tier": meta_d.get("latency_tier", ""),
            "risk": meta_d.get("risk", ""),
            "attention": meta_d.get("attention", ""),
        })
    catalog.sort(key=lambda d: d["id"])
    return catalog


def _read_ledger_direction_ids(ledger_path: str) -> tuple[set[str], list[dict[str, Any]]]:
    """读 ledger.jsonl，收集所有候选的 ``direction_id``（tried 集合）+ 返回全行（供 champion 查找）。

    direction_id 是 plan §2.2 新增的**可选**字段（旧 ledger 无 → 跳过，向后兼容）。值形如
    ``"D5"``（命中 catalog）或 ``"off_catalog:<指纹>"``（catalog 外新结构）。
    """
    p = Path(ledger_path)
    if not p.is_file():
        return set(), []
    rows: list[dict[str, Any]] = []
    tried: set[str] = set()
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            raise ValueError(f"ledger {ledger_path} 第 {lineno} 行非合法 JSON：{e}") from e
        if not isinstance(obj, dict):
            continue
        rows.append(obj)
        did = obj.get("direction_id")
        if isinstance(did, str) and did.strip():
            tried.add(did.strip())
    return tried, rows


def _champion_latency(rows: list[dict[str, Any]]) -> tuple[float, bool] | None:
    """从 ledger 行找当前 champion（全局 min-latency 且 SUCCESS & met_accuracy）的 (latency_ms, met_accuracy)。

    镜像 ledger_reducer._global_best_champion 语义（§3 step 6）。无达标候选 → None。
    """
    best: dict[str, Any] | None = None
    for r in rows:
        if r.get("status") != "SUCCESS" or not r.get("met_accuracy"):
            continue
        lat = r.get("latency_ms")
        if not isinstance(lat, (int, float)):
            continue
        if best is None or lat < best["latency_ms"]:
            best = r
    if best is None:
        return None
    return float(best["latency_ms"]), bool(best.get("met_accuracy"))


def compute_coverage(
    *, ledger: str, kb_dir: Path, family: str,
    target_latency_ms: float | None, near_band: float,
) -> dict[str, Any]:
    """纯函数：算 direction 覆盖信号（无副作用，可单测）。"""
    catalog = _load_catalog(kb_dir, family)
    catalog_ids = [d["id"] for d in catalog]
    tried, rows = _read_ledger_direction_ids(ledger)

    tried_in_catalog = [did for did in catalog_ids if did in tried]
    untried = [did for did in catalog_ids if did not in tried]
    catalog_size = len(catalog_ids)
    coverage_ratio = (len(tried_in_catalog) / catalog_size) if catalog_size else 0.0
    all_exhausted = catalog_size > 0 and len(untried) == 0

    # near_target：champion 已在 striking distance（latency <= target*near_band & met_accuracy）
    near_target = False
    if target_latency_ms is not None and target_latency_ms > 0:
        champ = _champion_latency(rows)
        if champ is not None:
            champ_lat, champ_met = champ
            near_target = champ_met and champ_lat <= target_latency_ms * near_band

    return {
        "family": family,
        "catalog": catalog,
        "catalog_size": catalog_size,
        "tried": sorted(tried),
        "tried_in_catalog": tried_in_catalog,
        "untried": untried,
        "coverage_ratio": round(coverage_ratio, 4),
        "all_exhausted": all_exhausted,
        "near_target": near_target,
    }


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="KB direction 目录覆盖感知（确定性）。")
    parser.add_argument("--ledger", required=True, help="ledger.jsonl 路径")
    parser.add_argument("--kb-dir", default="", help=f"KB 根（默认 $ORCA_KB_DIR={os.environ.get('ORCA_KB_DIR','')!r}）")
    parser.add_argument("--family", required=True, help="命中族名（如 wireless_receiver）")
    parser.add_argument("--target-latency-ms", type=float, default=None, help="目标时延（near_target 判定用；省略 → near_target=False）")
    parser.add_argument("--accuracy-target", type=float, default=None, help="（保留，暂未参与判定）")
    parser.add_argument("--near-band", type=float, default=_NEAR_BAND_DEFAULT, help=f"near_target latency 容忍带（默认 {_NEAR_BAND_DEFAULT}）")
    args = parser.parse_args(argv)

    try:
        kb_dir = _resolve_kb_dir(args.kb_dir)
        result = compute_coverage(
            ledger=args.ledger, kb_dir=kb_dir, family=args.family,
            target_latency_ms=args.target_latency_ms, near_band=args.near_band,
        )
    except ValueError as e:  # KB 缺 / ledger 损 → fail loud
        print(f"[direction_coverage] FAIL: {e}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001
        print("[direction_coverage] 异常：", file=sys.stderr)
        traceback.print_exc()
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

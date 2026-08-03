"""kd_reducer.py —— KD-NAS 串行迭代确定性 reducer（KD 专版，**非复用** struct 的 ledger_reducer）。

职责（SPEC §6.9 + §13 逐条对应）：
  1. append ledger.jsonl（KD schema：variant_id/student_path/round/parent/latency_ms/
     accuracy/met_*/accuracy_kind/direction_id/hypothesis/accepted_cfg/cfg_hash/ckpt/status）
  2. champion ratchet（min-latency）：准入 = ``met_latency ∧ met_accuracy ∧ status==SUCCESS``；
     准入集合内按 latency 最小 ratchet；**tie 不 ratchet（FIFO 最早）**（N12）。
     无达标 → 维持 baseline（setup seed 的 round=0 baseline）。
  3. continue_loop 决策：admitted 集合非空（champion_met）→ false, reason="target_met"；
     round ≥ max_rounds → false, reason="max_rounds"；否则 true。

**纯函数式、确定性**：
  - 读 ledger.jsonl + champions.jsonl + 本轮 candidate
  - 写 ledger.jsonl（append）+ champions.jsonl（仅新 champion 时 append）
  - stdout JSON 输出 decide output_schema 字段
  - 不读时钟、不读随机、不调 LLM、不调网络

与 struct ``ledger_reducer.py`` 的差异（为何不复用）：
  - ledger schema 不同：KD 无 tag/path/diff_summary（KD 不做 AST diff），有 student_path/
    accepted_cfg/cfg_hash/ckpt/direction_id/hypothesis。
  - 排序键不同：struct 准入 = ``SUCCESS ∧ met_accuracy``；KD 准入 = ``SUCCESS ∧ met_latency ∧ met_accuracy``
    （latency 是 KD 第一目标，需显式 met_latency；struct 时延门在 evaluator 已过）。
  - tie 语义不同：struct 用 ``best_after["id"] != prev_best_id`` 严改进；KD 显式 FIFO tiebreak
    （SPEC §13 N12「tie 不 ratchet」）。schema 与语义都不同 → 复用会字节漂移，重写。

CLI：
    kd_reducer.py \\
      --ledger <path> \\
      --champions <path> \\
      --candidate <json-string-or-@file> \\
      --target_latency_ms <float> \\
      --accuracy_baseline <float> \\
      --accuracy_baseline_kind <nmse|mse|ber|db|snr|acc> \\
      --max_rounds <int> \\
      --baseline_latency_ms <float> \\
      --baseline_accuracy <float> \\
      [--baseline_id baseline] \\
      [--baseline_snapshot ""] \\
      [--dry-run]

stdout（JSON）：decide output_schema + 本轮写入证据
    {
      "round": int,
      "continue_loop": bool,
      "champion_id": str,
      "champion_latency_ms": float,
      "champion_accuracy": float,
      "terminate_reason": "target_met" | "max_rounds" | "",
      "new_champion_this_round": bool,
      "ledger_entry_written": bool,
      "champions_entry_written": bool,
      "candidate_id": str,
      "status_final": str
    }

fail loud：candidate schema 缺字段 / ledger 文件损坏（非合法 JSON 行）/ 类型错 →
非零退出 + stderr。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any


# ── 常量 ────────────────────────────────────────────────────────────────────

# KD ledger.jsonl 每行必备字段（SPEC §6.9）。
_LEDGER_REQUIRED = (
    "variant_id",
    "student_path",
    "round",
    "parent",
    "latency_ms",
    "accuracy",
    "met_latency",
    "met_accuracy",
    "accuracy_kind",
    "direction_id",
    "hypothesis",
    "accepted_cfg",
    "cfg_hash",
    "ckpt",
    "status",
)
# champions.jsonl 每行必备字段（id==variant_id；snapshot==student_path）。
_CHAMPIONS_REQUIRED = ("round", "id", "latency_ms", "accuracy", "delta_vs_baseline_ms", "snapshot")

# status 合法值（KD 串行版）。
_LEDGER_STATUS = {"SUCCESS", "FAIL_latency", "FAIL_train", "FAIL_build"}


# ── I/O 工具 ────────────────────────────────────────────────────────────────


def _read_jsonl(path: str, *, schema_required: tuple[str, ...], kind: str) -> list[dict[str, Any]]:
    """读 jsonl，每行 JSON parse + 必备字段校验。文件不存在 → 视为空（首行）。fail loud。"""
    p = Path(path)
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            raise ValueError(f"{kind} {path} 第 {lineno} 行非合法 JSON：{e}") from e
        if not isinstance(obj, dict):
            raise ValueError(f"{kind} {path} 第 {lineno} 行非 object：{type(obj).__name__}")
        missing = [k for k in schema_required if k not in obj]
        if missing:
            raise ValueError(
                f"{kind} {path} 第 {lineno} 行缺字段：{missing}；现有 keys={sorted(obj)}"
            )
        out.append(obj)
    return out


def _append_jsonl(path: str, obj: dict[str, Any]) -> None:
    """append 一行 JSON（ensure_ascii=False，紧凑）。父目录不存在则建。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ── 核心纯函数 ──────────────────────────────────────────────────────────────


def _load_candidate(spec: str) -> dict[str, Any]:
    """candidate 来自 --candidate 'JSON' 或 --candidate @file / 文件路径。fail loud。"""
    if not spec:
        raise ValueError("candidate 为空")
    if spec.startswith("@"):
        text = Path(spec[1:]).read_text(encoding="utf-8")
    elif os.path.isfile(spec):
        text = Path(spec).read_text(encoding="utf-8")
    else:
        text = spec
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"candidate 非合法 JSON：{e}\n原文：{text!r}") from e
    if not isinstance(obj, dict):
        raise ValueError(f"candidate 必须是 JSON object（得到 {type(obj).__name__}）")
    return obj


def _validate_candidate(cand: dict[str, Any]) -> None:
    """校验 candidate 必备字段 + 类型。fail loud 给清晰错误。"""
    missing = [k for k in _LEDGER_REQUIRED if k not in cand]
    if missing:
        raise ValueError(f"candidate 缺字段：{missing}；现有 keys={sorted(cand)}")
    if cand["status"] not in _LEDGER_STATUS:
        raise ValueError(
            f"candidate.status 非法：{cand['status']!r}；合法：{sorted(_LEDGER_STATUS)}"
        )
    # latency_ms / accuracy 可为 -1（FAIL_train / FAIL_build 未训练或未测），允许数字。
    for k in ("latency_ms", "accuracy"):
        if not isinstance(cand[k], (int, float)) or isinstance(cand[k], bool):
            raise ValueError(f"candidate.{k} 必须是数字（得到 {type(cand[k]).__name__}）")
    if not isinstance(cand["round"], int) or isinstance(cand["round"], bool):
        raise ValueError(f"candidate.round 必须是 int（得到 {type(cand['round']).__name__}）")
    for k in ("met_latency", "met_accuracy"):
        if not isinstance(cand[k], bool):
            raise ValueError(f"candidate.{k} 必须是 bool（得到 {type(cand[k]).__name__}）")


def _current_champion(champions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """当前 champion = champions.jsonl 最后一行（setup 已 seed baseline）。"""
    if not champions:
        return None
    return champions[-1]


def _admitted(ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """准入门：SUCCESS ∧ met_latency ∧ met_accuracy（SPEC §13 admitted 集合）。

    FAIL_train 即使 met_latency=true 也不准入（met_accuracy=false 必然）；
    FAIL_latency / FAIL_build 同理。准入 = 三条件全 true。
    """
    return [
        e for e in ledger
        if e.get("status") == "SUCCESS"
        and e.get("met_latency") is True
        and e.get("met_accuracy") is True
    ]


def _min_latency_champion(
    ledger_after: list[dict[str, Any]], baseline: dict[str, Any]
) -> dict[str, Any]:
    """admitted 集合内 latency 最小；**FIFO tiebreak（首个出现的，不 ratchet tie）**（N12）。

    无 admitted → 返回 baseline（setup seed 的 round=0 baseline）。
    FIFO tiebreak：admitted 中遍历按 ledger 顺序，``<=`` 严格小于才替换 →
    相等 latency 不替换 → 首个（最早的）胜出。稳定且确定性。
    """
    admitted = _admitted(ledger_after)
    if not admitted:
        return baseline
    best = admitted[0]
    for e in admitted[1:]:
        # 严格小于才替换：tie 不 ratchet（保 FIFO 最早，N12）。
        if e["latency_ms"] < best["latency_ms"]:
            best = e
    return best


def _to_champion_record(
    champion_entry: dict[str, Any], baseline_latency_ms: float
) -> dict[str, Any]:
    """把 ledger entry（或 baseline seed）规范成 champions.jsonl 一行。"""
    return {
        "round": champion_entry.get("round", 0),
        "id": champion_entry.get("variant_id", champion_entry.get("id", "baseline")),
        "latency_ms": champion_entry["latency_ms"],
        "accuracy": champion_entry["accuracy"],
        "delta_vs_baseline_ms": round(
            champion_entry["latency_ms"] - baseline_latency_ms, 6
        ),
        "snapshot": champion_entry.get("student_path", champion_entry.get("snapshot", "")),
    }


def reduce_ledger(
    *,
    ledger_path: str,
    champions_path: str,
    candidate: dict[str, Any],
    target_latency_ms: float,
    accuracy_baseline: float,
    accuracy_baseline_kind: str,
    max_rounds: int,
    baseline_latency_ms: float,
    baseline_accuracy: float,
    baseline_id: str = "baseline",
    baseline_snapshot: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """KD reducer 主入口。纯函数式（除 append 副作用），确定性。

    Returns: decide output_schema + 写入证据字段（见模块 docstring）。
    """
    _validate_candidate(candidate)

    ledger = _read_jsonl(ledger_path, schema_required=_LEDGER_REQUIRED, kind="ledger")
    champions = _read_jsonl(
        champions_path, schema_required=_CHAMPIONS_REQUIRED, kind="champions"
    )

    # baseline champion（若 champions.jsonl 未 seed，用入参构造一个虚拟 baseline）。
    if champions:
        baseline_champion = champions[0]  # 首行 = setup seed 的 round=0 baseline
    else:
        baseline_champion = {
            "round": 0,
            "id": baseline_id,
            "latency_ms": baseline_latency_ms,
            "accuracy": baseline_accuracy,
            "delta_vs_baseline_ms": 0,
            "snapshot": baseline_snapshot,
        }

    cur_champion = _current_champion(champions) or baseline_champion

    # ── Step 1：构造 ledger entry，timestamp=null（脚本禁用 Date.now）──────
    delta_latency_ms = round(candidate["latency_ms"] - cur_champion["latency_ms"], 6)

    ledger_entry: dict[str, Any] = {
        "variant_id": candidate["variant_id"],
        "student_path": candidate["student_path"],
        "round": candidate["round"],
        "parent": candidate["parent"],
        "latency_ms": candidate["latency_ms"],
        "accuracy": candidate["accuracy"],
        "delta_latency_ms": delta_latency_ms,
        "met_latency": candidate["met_latency"],
        "met_accuracy": candidate["met_accuracy"],
        "accuracy_kind": candidate["accuracy_kind"],
        "direction_id": candidate["direction_id"],
        "hypothesis": candidate["hypothesis"],
        "accepted_cfg": candidate["accepted_cfg"],
        "cfg_hash": candidate["cfg_hash"],
        "ckpt": candidate["ckpt"],
        "status": candidate["status"],
        "timestamp": None,  # 由调度器写，脚本禁用 Date.now
    }

    # ── 模拟 append 后的 ledger（用于全局 champion 计算；dry_run 时不真写）──────
    ledger_after = ledger + [ledger_entry]

    # ── Step 2：champion ratchet（min-latency，FIFO tiebreak）──────────────────
    best_after = _min_latency_champion(ledger_after, baseline=baseline_champion)
    prev_best_id = cur_champion["id"]
    # 本轮 candidate 是否成为新 champion：
    #   (a) 全局 best 就是 candidate；
    #   (b) candidate 不是 baseline（避免 baseline 自身被算作「新 champion」）；
    #   (c) candidate.latency **严格小于** cur_champion.latency（tie 不 ratchet，N12）。
    new_champion_this_round = (
        best_after.get("variant_id", best_after.get("id")) == candidate["variant_id"]
        and candidate["variant_id"] != prev_best_id
        and candidate["latency_ms"] < cur_champion["latency_ms"]
    )

    if best_after.get("variant_id", best_after.get("id")) != prev_best_id:
        champion_now = best_after
    else:
        champion_now = cur_champion  # ratchet 只降不升：未改进则维持

    # champion 规范成 champions.jsonl 一行。
    if champion_now.get("id") == baseline_champion.get("id") and not champions:
        champion_record = baseline_champion
    elif "delta_vs_baseline_ms" in champion_now and "snapshot" in champion_now:
        # 已是 champions 格式（如 cur_champion 来自 champions.jsonl）。
        champion_record = champion_now
    else:
        champion_record = _to_champion_record(champion_now, baseline_latency_ms)

    # ── Step 3：continue_loop 决策（驱动 DAG 循环）──────────────────────────
    round_num = candidate["round"]
    # admitted 非空 = champion 是真 student 非 baseline（SPEC §13 champion_met）。
    admitted_after = _admitted(ledger_after)
    champion_met = bool(admitted_after)
    if champion_met:
        continue_loop = False
        terminate_reason = "target_met"
    elif round_num >= max_rounds:
        continue_loop = False
        terminate_reason = "max_rounds"
    else:
        continue_loop = True
        terminate_reason = ""

    # ── 副作用：append ledger + champions（dry_run 跳过）─────────────────────
    ledger_written = False
    champions_written = False
    if not dry_run:
        # 首次（champions 为空）→ seed baseline 行，保 setup 不变量。
        if not champions:
            _append_jsonl(champions_path, baseline_champion)
            champions = [baseline_champion]
        _append_jsonl(ledger_path, ledger_entry)
        ledger_written = True
        if new_champion_this_round:
            _append_jsonl(champions_path, champion_record)
            champions_written = True

    return {
        "round": round_num,
        "continue_loop": continue_loop,
        "champion_id": champion_record["id"],
        "champion_latency_ms": champion_record["latency_ms"],
        "champion_accuracy": champion_record["accuracy"],
        "terminate_reason": terminate_reason,
        "new_champion_this_round": new_champion_this_round,
        "ledger_entry_written": ledger_written,
        "champions_entry_written": champions_written,
        "candidate_id": candidate["variant_id"],
        "status_final": candidate["status"],
        "delta_latency_ms": delta_latency_ms,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────


def _main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "KD-NAS 串行迭代 reducer：append ledger + champion ratchet (min-latency, FIFO "
            "tiebreak) + continue_loop 决策。"
        )
    )
    parser.add_argument("--ledger", required=True, help="ledger.jsonl 路径")
    parser.add_argument("--champions", required=True, help="champions.jsonl 路径")
    parser.add_argument(
        "--candidate",
        required=True,
        help="本轮 candidate JSON 字符串 / 文件路径 / @file",
    )
    parser.add_argument("--target_latency_ms", type=float, required=True)
    parser.add_argument("--accuracy_baseline", type=float, required=True)
    parser.add_argument(
        "--accuracy_baseline_kind",
        required=True,
        help="nmse/mse/ber/db（越低越好）| snr/acc（越高越好）",
    )
    parser.add_argument("--max_rounds", type=int, required=True)
    parser.add_argument("--baseline_latency_ms", type=float, required=True)
    parser.add_argument("--baseline_accuracy", type=float, required=True)
    parser.add_argument(
        "--baseline_id", default="baseline", help="baseline id（与 champions seed 对齐）"
    )
    parser.add_argument(
        "--baseline_snapshot",
        default="",
        help="baseline 快照路径（champions.jsonl 未 seed 时用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只算不写（不 append ledger/champions）；fixture 自检用",
    )
    args = parser.parse_args()

    try:
        candidate = _load_candidate(args.candidate)
        result = reduce_ledger(
            ledger_path=args.ledger,
            champions_path=args.champions,
            candidate=candidate,
            target_latency_ms=args.target_latency_ms,
            accuracy_baseline=args.accuracy_baseline,
            accuracy_baseline_kind=args.accuracy_baseline_kind,
            max_rounds=args.max_rounds,
            baseline_latency_ms=args.baseline_latency_ms,
            baseline_accuracy=args.baseline_accuracy,
            baseline_id=args.baseline_id,
            baseline_snapshot=args.baseline_snapshot,
            dry_run=args.dry_run,
        )
    except Exception as e:
        print(f"[kd_reducer] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

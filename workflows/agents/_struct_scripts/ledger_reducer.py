"""ledger_reducer.py —— Curator 确定性核心 reducer（草稿 §3 / §11 / §9.2）。

职责（草稿 struct-curator agent.md 逐条）：
  1. append ledger.jsonl（§11.1 schema，timestamp 永远 null）
  2. 严格模式（§9.2）：reject_hyperparam_only=true 且 tag=hyperparam → status=REJECT_struct
  3. champion ratchet（§3 step 6）：**全局** min-latency 且 accuracy 达标的 candidate
  4. explore/exploit 二值路由（§3 step 8）：本轮新 champion → exploit；否则 → explore
  5. continue_loop 决策：达标 → champion_met；round ≥ max_rounds → max_rounds；否则 true

**纯函数式、确定性**（[[deterministic-over-model-mediated]]）：
  - 读 ledger.jsonl + champions.jsonl + 本轮 candidate
  - 写 ledger.jsonl（append）+ champions.jsonl（仅新 champion 时 append）
  - stdout JSON 输出 curator output_schema 字段
  - **不读时钟、不读随机、不调 LLM、不调网络**

champion 读取范围 = 全局（跨 path，为 §8 多路径预留）：从 ledger 取全局 min-latency
且 accuracy 达标（SUCCESS & met_accuracy）的 candidate，**不限于本 path**。

CLI：
    ledger_reducer.py \\
      --ledger <path> \\
      --champions <path> \\
      --candidate <json-string-or-@file> \\
      --target_latency_ms <float> \\
      --accuracy_target <float> \\
      --max_rounds <int> \\
      --baseline_latency_ms <float> \\
      --baseline_accuracy <float> \\
      --structural_slot_ratio <float> \\
      [--reject_hyperparam_only] \\
      [--structural_slot_enforce] \\
      [--dry-run]

stdout（JSON）：curator output_schema + 本轮 ledger/champions 写入证据
    {
      "round": int,
      "continue_loop": bool,
      "champion_id": str,
      "champion_latency_ms": float,
      "champion_accuracy": float,
      "route_mode": "exploit" | "explore",
      "terminate_reason": "champion_met" | "max_rounds" | "budget" | "",
      "new_champion_this_round": bool,
      "structural_ratio": float,            # 仅供诊断：当前 ledger 的结构 tag 占比
      "slot_warning": str | "",             # 配额软告警（structural_ratio < ratio 时）
      "ledger_entry_written": bool,
      "champions_entry_written": bool,
      "candidate_id": str,
      "status_final": str                    # 入账后最终 status（含 REJECT 改写）
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

# ledger.jsonl 每行必备字段（§11.1）。
_LEDGER_REQUIRED = (
    "id",
    "parent",
    "path",
    "round",
    "status",
    "tag",
    "latency_ms",
    "accuracy",
    "met_accuracy",
    "snapshot",
    "onnx",
    "diff_summary",
    "hypothesis",
)
# champions.jsonl 每行必备字段（§11.2）。
_CHAMPIONS_REQUIRED = ("round", "id", "latency_ms", "accuracy", "delta_vs_baseline_ms", "snapshot")

# status 合法值（§11.1 / §9.2）。
_LEDGER_STATUS = {
    "SUCCESS",
    "FAIL_latency",
    "FAIL_accuracy",
    "FAIL_export",
    "REJECT_struct",
}
# tag 合法值（§9.1）。
_TAG_VALUES = {"structural", "hyperparam", "mixed"}

# 算 SUCCESS 且 met_accuracy 才能当 champion（§3 step 6 / §4）。
_CHAMPION_OK_STATUS = {"SUCCESS"}


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
    """candidate 来自 --candidate 'JSON' 或 --candidate @file。fail loud。"""
    if not spec:
        raise ValueError("candidate 为空")
    text: str
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
        raise ValueError(
            f"candidate 缺字段：{missing}；现有 keys={sorted(cand)}"
        )
    if cand["status"] not in _LEDGER_STATUS - {"REJECT_struct"}:
        # evaluator 上游不会发 REJECT_struct（由本 reducer 改写）；其余 4 个合法。
        raise ValueError(
            f"candidate.status 非法：{cand['status']!r}；合法：SUCCESS/FAIL_latency/FAIL_accuracy/FAIL_export"
        )
    if cand["tag"] not in _TAG_VALUES:
        raise ValueError(
            f"candidate.tag 非法：{cand['tag']!r}；合法：{sorted(_TAG_VALUES)}"
        )
    # latency_ms / accuracy 可为 -1（FAIL_export / 未训练），允许数字。
    for k in ("latency_ms", "accuracy"):
        if not isinstance(cand[k], (int, float)) or isinstance(cand[k], bool):
            raise ValueError(f"candidate.{k} 必须是数字（得到 {type(cand[k]).__name__}）")
    if not isinstance(cand["round"], int) or isinstance(cand["round"], bool):
        raise ValueError(f"candidate.round 必须是 int（得到 {type(cand['round']).__name__}）")
    if not isinstance(cand["met_accuracy"], bool):
        raise ValueError(
            f"candidate.met_accuracy 必须是 bool（得到 {type(cand['met_accuracy']).__name__}）"
        )


def _current_champion(champions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """当前 champion = champions.jsonl 最后一行（family_detect 已 seed baseline）。"""
    if not champions:
        return None
    return champions[-1]


def _global_best_champion(
    ledger: list[dict[str, Any]], baseline: dict[str, Any]
) -> dict[str, Any]:
    """全局最优 champion：跨所有 path 从 ledger 取 SUCCESS & met_accuracy 的 min-latency。

    没有任何 SUCCESS 达标 candidate → 返回 baseline（family_detect seed 的 round=0 baseline）。
    平局（多个 candidate 同 latency）→ 取最早出现的（稳定：FIFO tiebreak，确定性）。
    """
    candidates = [
        e for e in ledger
        if e.get("status") in _CHAMPION_OK_STATUS and e.get("met_accuracy") is True
    ]
    if not candidates:
        return baseline
    # min by latency_ms；FIFO tiebreak 用 ledger 顺序（stable sort 保序）。
    best = min(candidates, key=lambda e: e["latency_ms"])
    return best


def _to_champion_record(
    champion_entry: dict[str, Any], baseline_latency_ms: float
) -> dict[str, Any]:
    """把 ledger entry（或 baseline seed）规范成 champions.jsonl 一行（§11.2 schema）。"""
    return {
        "round": champion_entry.get("round", 0),
        "id": champion_entry["id"],
        "latency_ms": champion_entry["latency_ms"],
        "accuracy": champion_entry["accuracy"],
        "delta_vs_baseline_ms": round(
            champion_entry["latency_ms"] - baseline_latency_ms, 6
        ),
        "snapshot": champion_entry.get("snapshot", ""),
    }


def _structural_ratio(ledger: list[dict[str, Any]]) -> float:
    """本轮为止 ledger 中 tag=structural 占比（§9.2 软配额诊断）。"""
    if not ledger:
        return 0.0
    n_struct = sum(1 for e in ledger if e.get("tag") == "structural")
    return n_struct / len(ledger)


def reduce_ledger(
    *,
    ledger_path: str,
    champions_path: str,
    candidate: dict[str, Any],
    target_latency_ms: float,
    accuracy_target: float,
    max_rounds: int,
    baseline_latency_ms: float,
    baseline_accuracy: float,
    baseline_id: str = "baseline",
    baseline_snapshot: str = "",
    structural_slot_ratio: float = 0.5,
    reject_hyperparam_only: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Curator reducer 主入口。纯函数式（除 append 副作用），确定性。

    Returns: curator output_schema + 写入证据字段（见模块 docstring）。
    """
    _validate_candidate(candidate)

    ledger = _read_jsonl(ledger_path, schema_required=_LEDGER_REQUIRED, kind="ledger")
    champions = _read_jsonl(
        champions_path, schema_required=_CHAMPIONS_REQUIRED, kind="champions"
    )

    # baseline champion（若 champions.jsonl 未 seed，用入参构造一个虚拟 baseline）。
    if champions:
        baseline_champion = champions[0]  # 首行 = family_detect seed 的 round=0 baseline
    else:
        baseline_champion = {
            "round": 0,
            "id": baseline_id,
            "latency_ms": baseline_latency_ms,
            "accuracy": baseline_accuracy,
            "delta_vs_baseline_ms": 0,
            "snapshot": baseline_snapshot,
        }
        # 首次没 champions.jsonl 时，等会儿同步 seed（保持 §3 Setup 不变量）。

    cur_champion = _current_champion(champions) or baseline_champion

    # ── Step 1：构造 ledger entry（§11.1），timestamp=null（脚本禁用 Date.now）──────
    status_final = candidate["status"]
    if reject_hyperparam_only and candidate["tag"] == "hyperparam":
        status_final = "REJECT_struct"  # §9.2 严格模式

    # delta_latency_ms：相对**当前 champion**（ratchet 基准），非相对 baseline。
    delta_latency_ms = round(candidate["latency_ms"] - cur_champion["latency_ms"], 6)

    ledger_entry: dict[str, Any] = {
        "id": candidate["id"],
        "parent": candidate["parent"],
        "path": candidate["path"],
        "round": candidate["round"],
        "status": status_final,
        "tag": candidate["tag"],
        "latency_ms": candidate["latency_ms"],
        "accuracy": candidate["accuracy"],
        "delta_latency_ms": delta_latency_ms,
        "met_accuracy": candidate["met_accuracy"],
        "snapshot": candidate["snapshot"],
        "onnx": candidate["onnx"],
        "diff_summary": candidate["diff_summary"],
        "hypothesis": candidate["hypothesis"],
        "timestamp": None,  # §11.1 注：由调度器写，脚本内禁用 Date.now
    }
    # plan sprightly-questing-donut §2.2：direction_id 可选透传（hypothesizer 声明的 KB direction，
    # 供 direction_coverage.py 算 tried/untried）。不在 _LEDGER_REQUIRED（旧 ledger 向后兼容）。
    did = candidate.get("direction_id")
    if isinstance(did, str) and did.strip():
        ledger_entry["direction_id"] = did.strip()

    # ── 模拟 append 后的 ledger（用于全局 champion 计算；dry_run 时不真写）──────
    ledger_after = ledger + [ledger_entry]

    # ── Step 3：champion ratchet（全局 min-latency 达标）────────────────────────
    best_after = _global_best_champion(
        ledger_after, baseline=baseline_champion
    )
    prev_best_id = cur_champion["id"]
    # 本轮 candidate 是否成为新 champion（仅当它就是全局 best 且替换了前一个）。
    new_champion_this_round = (
        best_after["id"] == candidate["id"]
        and best_after["id"] != prev_best_id
        and best_after["latency_ms"] < cur_champion["latency_ms"]
    )
    # 也可能 baseline 已是 best 但 candidate 与 baseline tie — 不算新 champion（无改进）。
    # 若全局 best 仍是 baseline（无 SUCCESS candidate）→ champion 不变。

    if best_after["id"] != prev_best_id:
        champion_now = best_after
    else:
        champion_now = cur_champion  # ratchet 只降不升：未改进则维持

    # champion 规范成 champions.jsonl 一行（baseline 已是该格式；ledger entry 需转换）。
    if champion_now.get("id") == baseline_champion.get("id") and not champions:
        champion_record = baseline_champion
    elif "delta_vs_baseline_ms" in champion_now and "snapshot" in champion_now:
        # 已是 champions 格式（如 cur_champion 来自 champions.jsonl）。
        champion_record = champion_now
    else:
        champion_record = _to_champion_record(champion_now, baseline_latency_ms)

    # ── Step 5：explore/exploit 二值路由（§3 step 8）──────────────────────────
    route_mode = "exploit" if new_champion_this_round else "explore"

    # ── Step 2（诊断）：structural 配额软告警（不驳回，只标记）───────────────────
    ratio_after = _structural_ratio(ledger_after)
    slot_warning = ""
    if ratio_after < structural_slot_ratio:
        slot_warning = (
            f"structural 占比 {ratio_after:.2f} < 配额 {structural_slot_ratio}；"
            "下轮建议补结构假设（软配额，§9.2）"
        )

    # ── Step 6：continue_loop 决策（驱动 DAG 循环）──────────────────────────
    round_num = candidate["round"]
    target_met = (
        champion_record["latency_ms"] <= target_latency_ms
        and champion_record["accuracy"] >= accuracy_target
    )
    if target_met:
        continue_loop = False
        terminate_reason = "champion_met"
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
        # 首次（champions 为空）→ seed baseline 行，保证 §3 Setup 不变量。
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
        "route_mode": route_mode,
        "terminate_reason": terminate_reason,
        # 写入证据 / 诊断（供 curator agent 透传 / fail loud 检查）：
        "new_champion_this_round": new_champion_this_round,
        "structural_ratio": round(ratio_after, 4),
        "slot_warning": slot_warning,
        "ledger_entry_written": ledger_written,
        "champions_entry_written": champions_written,
        "candidate_id": candidate["id"],
        "status_final": status_final,
        "delta_latency_ms": delta_latency_ms,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse_bool(s: str) -> bool:
    return s.strip().lower() in {"1", "true", "yes", "y", "on"}


def _main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Curator 确定性 reducer：append ledger + champion ratchet + explore/exploit "
            "+ continue_loop 决策（草稿 §3/§11/§9.2）。"
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
    parser.add_argument("--accuracy_target", type=float, required=True)
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
        "--structural_slot_ratio", type=float, default=0.5, help="§9.2 软配额（默认 0.5）"
    )
    parser.add_argument(
        "--reject_hyperparam_only",
        type=_parse_bool,
        default=False,
        help="严格模式（默认 false）：true 时 tag=hyperparam → REJECT_struct",
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
            accuracy_target=args.accuracy_target,
            max_rounds=args.max_rounds,
            baseline_latency_ms=args.baseline_latency_ms,
            baseline_accuracy=args.baseline_accuracy,
            baseline_id=args.baseline_id,
            baseline_snapshot=args.baseline_snapshot,
            structural_slot_ratio=args.structural_slot_ratio,
            reject_hyperparam_only=args.reject_hyperparam_only,
            dry_run=args.dry_run,
        )
    except Exception as e:
        print(f"[ledger_reducer] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

#!/usr/bin/env python3
"""mfu_adapter.py — mfu_benchmark.py raw products -> PROFILER_CONTRACT four-piece.

Deterministic converter for the mfu (real-evaluation) profiling mode. The
EXECUTION layer (submitting the evaluation, waiting, downloading) belongs to
the mfu-analyzer subagent; this adapter NEVER runs an evaluation — it only
reads the raw products the benchmark left under <profile_dir>/<onnx_stem>/
and maps them, field by field, into the four contract artifacts
(PROFILER_CONTRACT.md) written directly into <profile_dir>:

    raw <stem>_taskgraph.json   -> taskgraph.json structure: name / op_type /
        task_id / pipeline / depends_on / output_memory / output_dimensions
        verbatim; `latency` is joined from subgraph_0_tasks.json; `onnx_nodes`
        = [name] per the contract's 1:1-profiler rule.
    raw subgraph_0_tasks.json   -> per-task `cycles` = the operator latency
        (joined by task_id, name cross-checked).
    raw <chip>_<stem>.csv       -> cross-check only: per-name cycles must equal
        the subgraph value (both artifacts claim the same measurement).
    raw schedule_result.json    -> makespan_cycles = `parallel_cycles`
        (the CANONICAL makespan).

schedule.json assignments are DERIVED (the benchmark reports totals only): an
ASAP layout over depends_on x latency, uniformly shifted so that
max(end_cycle) == parallel_cycles exactly. Every assignment keeps
end - start == latency; derived timing never overrides a measured number.

Fail loud (rc=2, stderr names the missing file / field / contradiction):
missing raw products, ambiguous raw dirs, absent or non-integer fields,
duplicate ids, CSV-vs-subgraph cycles mismatch, dependency cycle, unknown
depends_on name, or parallel_cycles below the dependency critical path
(mathematically impossible — inconsistent products).

CLI:
    mfu_adapter.py --profile-dir <dir> [--onnx <path>]
        --onnx overrides the `onnx` absolute-path field of the output
        (default: the raw taskgraph's own `onnx` value).

Idempotent: re-running overwrites the four files with identical content.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 1

# fields mapped verbatim from the raw mfu taskgraph into the contract
# taskgraph (latency/onnx_nodes are the two exceptions: joined / derived)
RAW_OPERATOR_KEYS = ("name", "op_type", "task_id", "pipeline", "depends_on",
                     "output_memory", "output_dimensions")


class AdapterError(RuntimeError):
    """Raw products missing / inconsistent — never fabricate around it."""


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AdapterError(f"missing raw artifact: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"{path} unreadable/not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AdapterError(f"{path} must be a JSON object")
    return data


def _int_value(value, where: str, what: str) -> int:
    """integer cycles/bytes; an integral float is accepted, anything else fails"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or int(value) != value:
        raise AdapterError(f"{where}: {what} must be an integer, got {value!r}")
    return int(value)


def _find_raw_dir(profile_dir: Path) -> Path:
    hits = sorted(p.parent for p in profile_dir.glob("*/schedule_result.json"))
    if not hits:
        raise AdapterError(
            f"no mfu_benchmark raw products under {profile_dir} — expected "
            f"<profile_dir>/<onnx_stem>/schedule_result.json (dispatch the "
            f"mfu-analyzer subagent first; placeholder fallback is forbidden "
            f"in mfu mode)")
    if len(hits) > 1:
        raise AdapterError(
            f"ambiguous raw product dirs under {profile_dir}: "
            f"{[str(h.name) for h in hits]} — exactly one schedule_result.json "
            f"is expected")
    return hits[0]


def _sole_glob(raw_dir: Path, pattern: str, what: str) -> Path:
    hits = sorted(raw_dir.glob(pattern))
    if len(hits) != 1:
        raise AdapterError(f"expected exactly one {what} matching "
                           f"{raw_dir / pattern}, found {len(hits)}")
    return hits[0]


def _load_operators(raw_dir: Path) -> tuple[list[dict], str]:
    tg_path = _sole_glob(raw_dir, "*_taskgraph.json", "taskgraph json")
    data = _load_json(tg_path)
    ops = data.get("operators")
    if not isinstance(ops, list) or not ops:
        raise AdapterError(f"{tg_path}: 'operators' must be a non-empty list")
    onnx_ref = data.get("onnx")
    if not isinstance(onnx_ref, str) or not onnx_ref:
        raise AdapterError(f"{tg_path}: 'onnx' must be a non-empty string")
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise AdapterError(f"{tg_path}: operators[{i}] is not an object")
        missing = [k for k in RAW_OPERATOR_KEYS if k not in op]
        if missing:
            raise AdapterError(
                f"{tg_path}: operators[{i}] ({op.get('name', '?')!r}) is "
                f"missing field(s) {missing} — a missing field cannot be "
                f"fabricated")
        if not isinstance(op["name"], str) or not op["name"]:
            raise AdapterError(f"{tg_path}: operators[{i}].name must be a "
                               f"non-empty string")
        if not isinstance(op["task_id"], str) or not op["task_id"]:
            raise AdapterError(f"{tg_path}: operators[{i}].task_id must be a "
                               f"non-empty string")
        for key in ("op_type", "pipeline"):
            if not isinstance(op[key], str) or not op[key]:
                raise AdapterError(f"{tg_path}: operators[{i}].{key} must be "
                                   f"a non-empty string")
        deps = op["depends_on"]
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise AdapterError(f"{tg_path}: operators[{i}] ({op['name']!r}) "
                               f"depends_on must be a list of strings")
        dims = op["output_dimensions"]
        if not isinstance(dims, list) or \
                not all(isinstance(d, int) and not isinstance(d, bool) for d in dims):
            raise AdapterError(f"{tg_path}: operators[{i}] ({op['name']!r}) "
                               f"output_dimensions must be an int list "
                               f"(empty = scalar output)")
        _int_value(op["output_memory"],
                   f"{tg_path} operators[{i}] ({op['name']!r})", "output_memory")
    return ops, onnx_ref


def _load_subgraph_tasks(raw_dir: Path) -> dict[str, tuple[str, int]]:
    """task_id -> (name, cycles) from subgraph_0_tasks.json."""
    tasks_path = raw_dir / "subgraph_0_tasks.json"
    data = _load_json(tasks_path)
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise AdapterError(f"{tasks_path}: 'tasks' must be a non-empty list")
    out: dict[str, tuple[str, int]] = {}
    for i, task in enumerate(tasks):
        if not isinstance(task, dict) or "task_id" not in task:
            raise AdapterError(f"{tasks_path}: tasks[{i}] has no task_id")
        if task["task_id"] in out:
            raise AdapterError(f"{tasks_path}: duplicate task_id "
                               f"{task['task_id']!r} — ambiguous raw products")
        name = task.get("name")
        if not isinstance(name, str) or not name:
            raise AdapterError(f"{tasks_path}: tasks[{i}].name must be a "
                               f"non-empty string")
        out[task["task_id"]] = (name, _int_value(
            task.get("cycles"), f"{tasks_path} tasks[{i}] ({name!r})",
            "cycles"))
    return out


def _cross_check_csv(raw_dir: Path, chip: str, ops: list[dict],
                     latency: dict[str, int]) -> None:
    csv_path = _sole_glob(raw_dir, f"{chip}_*.csv", "operator latency csv")
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise AdapterError(f"{csv_path} is empty")
    if rows[0][:3] != ["name", "op_type", "cycles"]:
        raise AdapterError(f"{csv_path}: header must start with "
                           f"name,op_type,cycles (got {rows[0]})")
    by_name = {row[0]: row for row in rows[1:] if row}
    for op in ops:
        row = by_name.get(op["name"])
        if row is None:
            raise AdapterError(f"{csv_path}: no row for operator "
                               f"{op['name']!r} (present in the taskgraph — "
                               f"the two artifacts disagree)")
        raw_cycles = str(row[2]).strip()  # csv cells are always strings
        try:
            csv_cycles = int(raw_cycles)
        except ValueError as exc:
            raise AdapterError(f"{csv_path} row {op['name']!r}: cycles must "
                               f"be an integer, got {row[2]!r}") from exc
        if csv_cycles != latency[op["task_id"]]:
            raise AdapterError(
                f"inconsistent cycles for {op['name']!r}: {csv_path} says "
                f"{csv_cycles}, subgraph_0_tasks.json says "
                f"{latency[op['task_id']]} — both claim the same measurement")


def _topo_order(ops: list[dict]) -> list[dict]:
    """Kahn topological order (deterministic: taskgraph order among ready ops);
    fails loud on unknown depends_on names and on dependency cycles."""
    by_name = {op["name"]: op for op in ops}
    if len(by_name) != len(ops):
        raise AdapterError("duplicate operator name in the taskgraph")
    for op in ops:
        for dep in op["depends_on"]:
            if dep not in by_name:
                raise AdapterError(f"operator {op['name']!r} depends on "
                                   f"unknown operator {dep!r}")
    dependents: dict[str, list[str]] = {op["name"]: [] for op in ops}
    indegree: dict[str, int] = {op["name"]: 0 for op in ops}
    for op in ops:
        for dep in op["depends_on"]:
            dependents[dep].append(op["name"])
            indegree[op["name"]] += 1
    order: list[dict] = []
    ready = [op for op in ops if indegree[op["name"]] == 0]  # taskgraph order
    while ready:
        op = ready.pop(0)
        order.append(op)
        for succ in dependents[op["name"]]:
            indegree[succ] -= 1
            if indegree[succ] == 0:
                ready.append(by_name[succ])
    if len(order) != len(ops):
        stuck = sorted(n for n, d in indegree.items() if d > 0)
        raise AdapterError(f"taskgraph has a dependency cycle involving {stuck}")
    return order


def _derive_assignments(ops: list[dict], latency: dict[str, int],
                        makespan: int) -> list[dict]:
    """ASAP layout over depends_on x latency, uniformly shifted so the last
    end_cycle lands exactly on the canonical makespan (end - start == latency
    holds for every assignment; the shift distributes idle time, it never
    rescales a measured latency)."""
    end_at: dict[str, int] = {}
    start_at: dict[str, int] = {}
    for op in _topo_order(ops):
        start = max((end_at[d] for d in op["depends_on"]), default=0)
        start_at[op["name"]] = start
        end_at[op["name"]] = start + latency[op["task_id"]]
    critical = max(end_at.values())
    if critical > makespan:
        raise AdapterError(
            f"schedule_result parallel_cycles ({makespan}) is below the "
            f"dependency critical path ({critical}) — mathematically "
            f"impossible, the raw products are inconsistent")
    slack = makespan - critical
    return [
        {"task_id": op["task_id"], "operator": op["name"],
         "pipeline": op["pipeline"],
         "start_cycle": start_at[op["name"]] + slack,
         "end_cycle": end_at[op["name"]] + slack}
        for op in ops  # output in taskgraph order (deterministic)
    ]


def adapt(profile_dir: Path, onnx_override: str | None) -> dict:
    raw_dir = _find_raw_dir(profile_dir)
    ops, onnx_ref = _load_operators(raw_dir)
    subtasks = _load_subgraph_tasks(raw_dir)
    latency = {tid: cycles for tid, (_, cycles) in subtasks.items()}

    # join: every taskgraph task_id must exist in the subgraph tasks, and the
    # task_id -> name pairing must agree in both artifacts
    for op in ops:
        if op["task_id"] not in subtasks:
            raise AdapterError(
                f"subgraph_0_tasks.json has no task for task_id "
                f"{op['task_id']!r} (operator {op['name']!r}) — the artifacts "
                f"describe different graphs")
        if subtasks[op["task_id"]][0] != op["name"]:
            raise AdapterError(
                f"task_id {op['task_id']!r} is {op['name']!r} in the taskgraph "
                f"but {subtasks[op['task_id']][0]!r} in subgraph_0_tasks.json")

    schedule_result = _load_json(raw_dir / "schedule_result.json")
    if "parallel_cycles" not in schedule_result or "serial_cycles" not in schedule_result:
        raise AdapterError("schedule_result.json: missing parallel_cycles / "
                           "serial_cycles")
    makespan = _int_value(schedule_result["parallel_cycles"],
                          "schedule_result.json", "parallel_cycles")
    serial = _int_value(schedule_result["serial_cycles"],
                        "schedule_result.json", "serial_cycles")
    if makespan < 0 or serial < makespan:
        raise AdapterError(
            f"schedule_result.json: needs serial_cycles ({serial}) >= "
            f"parallel_cycles ({makespan}) >= 0")

    chip = schedule_result.get("chip")
    if not isinstance(chip, str) or not chip:
        raise AdapterError("schedule_result.json: 'chip' must be a non-empty "
                           "string (needed to locate the latency csv)")
    _cross_check_csv(raw_dir, chip, ops, latency)

    assignments = _derive_assignments(ops, latency, makespan)
    onnx_field = str(Path(onnx_override).resolve()) if onnx_override else onnx_ref

    taskgraph = {
        "schema_version": SCHEMA_VERSION,
        "onnx": onnx_field,
        "operators": [
            {"name": op["name"], "op_type": op["op_type"],
             "task_id": op["task_id"], "pipeline": op["pipeline"],
             "latency": latency[op["task_id"]],
             "depends_on": list(op["depends_on"]),
             "output_memory": int(op["output_memory"]),
             "output_dimensions": [int(d) for d in op["output_dimensions"]],
             "onnx_nodes": [op["name"]]}
            for op in ops
        ],
    }
    (profile_dir / "taskgraph.json").write_text(
        json.dumps(taskgraph, indent=2), encoding="utf-8")

    with open(profile_dir / "ops.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "op_type", "task_id", "pipeline", "latency",
                    "depends_on", "output_memory", "output_dimensions",
                    "onnx_nodes"])
        for op in taskgraph["operators"]:
            w.writerow([
                op["name"], op["op_type"], op["task_id"], op["pipeline"],
                op["latency"], ";".join(op["depends_on"]),
                op["output_memory"], "x".join(str(d) for d in op["output_dimensions"]),
                ";".join(op["onnx_nodes"]),
            ])

    (profile_dir / "schedule.json").write_text(
        json.dumps({"schema_version": SCHEMA_VERSION,
                    "makespan_cycles": makespan,
                    "assignments": assignments}, indent=2), encoding="utf-8")

    summary = {"schema_version": SCHEMA_VERSION, "onnx": onnx_field,
               "makespan_cycles": makespan, "op_count": len(ops)}
    (profile_dir / "profile_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")

    return {"profile_dir": str(profile_dir.resolve()),
            "raw_dir": str(raw_dir.resolve()),
            "makespan_cycles": makespan, "op_count": len(ops)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile-dir", required=True)
    ap.add_argument("--onnx", default=None,
                    help="override the onnx absolute-path field (default: "
                         "the raw taskgraph's own onnx value)")
    ns = ap.parse_args()
    try:
        result = adapt(Path(ns.profile_dir), ns.onnx)
    except AdapterError as exc:
        print(f"mfu_adapter: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — fail loud with a clear cause
        print(f"mfu_adapter: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

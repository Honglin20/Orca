"""Select retrain-ready subnet architectures from NAS Pareto JSONL outputs.

The tradeoff objectives are read directly from each JSONL record's `objs` keys
and are treated as smaller-is-better, matching the search config convention.
A constraint is an optional Python expression over objective names from the
JSONL `objs` payload. Because the search minimizes all objectives, metrics
like accuracy are stored as negated values (e.g. `-0.95`); constraint
expressions must account for this (e.g. `acc < -0.9` to require accuracy
above 90%). The constraint is applied before reconstructing the feasible
Pareto front and selecting high-tradeoff points.

Example:
    nas-select-architecture --config search_config.yaml --input search.jsonl --constraints "latency<=15.0 and acc < -0.9" --arch_output_dir selected_arches -n 1
"""

import argparse
import ast
import json
import math
import sys
import types
from pathlib import Path
from typing import Any

import torch
from evox.operators.selection import non_dominate_rank
from omegaconf import OmegaConf

from nas_agent.search.arch_utils import hash_arch, serialize_arch
from nas_agent.search.dynamic_import import load_generated_component
from nas_agent.search.problem import WORST_FITNESS


def normalize_objectives(
    objective_values: torch.Tensor,
    ideal_point: torch.Tensor | None = None,
    nadir_point: torch.Tensor | None = None,
    estimate_bounds_if_none: bool = True,
) -> torch.Tensor:
    """Normalize objective values using ideal and nadir points.

    Args:
        objective_values: A tensor of objective values to normalize.
        ideal_point: An optional tensor representing the ideal point.
        nadir_point: An optional tensor representing the nadir point.
        estimate_bounds_if_none: Whether to estimate bounds if ideal or nadir
            points are not provided.

    Returns:
        A tensor of normalized objective values.
    """
    normalized_values = objective_values.to(dtype=torch.float32).clone()

    if estimate_bounds_if_none:
        if ideal_point is None:
            ideal_point = torch.min(objective_values, dim=0).values
        if nadir_point is None:
            nadir_point = torch.max(objective_values, dim=0).values

    if ideal_point is not None:
        ideal_point = ideal_point.to(
            device=normalized_values.device,
            dtype=normalized_values.dtype,
        )
        normalized_values = normalized_values - ideal_point

    if nadir_point is not None:
        nadir_point = nadir_point.to(
            device=normalized_values.device,
            dtype=normalized_values.dtype,
        )
        lower_bound = (
            ideal_point if ideal_point is not None else torch.zeros_like(nadir_point)
        )
        value_range = nadir_point - lower_bound
        value_range = torch.where(
            torch.abs(value_range) < 1e-8,
            torch.ones_like(value_range),
            value_range,
        )
        normalized_values = normalized_values / value_range

    return normalized_values


class ObjectiveNeighborFinder:
    """Utility to find nearest neighbors based on objective values."""

    def __init__(
        self,
        objective_values: torch.Tensor,
        radius: float | None = 0.125,
        num_neighbors: int | None = None,
        min_neighbors: int | str | None = None,
    ) -> None:
        """Initialize the ObjectiveNeighborFinder.

        Args:
            objective_values: A tensor containing objective values.
            radius: Radius for neighborhood search.
            num_neighbors: Number of nearest neighbors to find.
            min_neighbors: Minimum number of neighbors ('auto', an integer, or None).

        Raises:
            ValueError: If fewer than 2 objectives are provided.
        """
        self.objective_values = objective_values
        self.radius = radius
        self.num_neighbors = num_neighbors
        num_points, num_objectives = objective_values.shape

        if num_objectives < 2:
            raise ValueError("At least 2 objectives must be provided.")

        if min_neighbors == "auto":
            self.min_neighbors = min(2 * num_objectives, max(num_points - 1, 0))
        elif min_neighbors is None:
            self.min_neighbors = None
        else:
            self.min_neighbors = int(min_neighbors)

        self.distances = torch.cdist(objective_values, objective_values)

    def find(self, point_index: int) -> torch.Tensor:
        """Find neighbors for a specific point index.

        Args:
            point_index: The index of the point to find neighbors for.

        Returns:
            A tensor containing the indices of the neighbors.

        Raises:
            ValueError: If neither radius nor number of neighbors is defined.
        """
        if self.radius is not None:
            neighbors = torch.nonzero(
                self.distances[point_index] <= self.radius,
                as_tuple=False,
            ).flatten()
        elif self.num_neighbors is not None:
            neighbor_count = min(
                self.num_neighbors + 1,
                self.objective_values.shape[0],
            )
            neighbors = torch.topk(
                self.distances[point_index],
                k=neighbor_count,
                largest=False,
            ).indices
        else:
            raise ValueError("Either define radius or number of neighbors.")

        min_count = None if self.min_neighbors is None else self.min_neighbors + 1
        if min_count is not None and neighbors.numel() < min_count:
            neighbor_count = min(min_count, self.objective_values.shape[0])
            neighbors = torch.topk(
                self.distances[point_index],
                k=neighbor_count,
                largest=False,
            ).indices

        return neighbors


def find_upper_tail_outliers(scores: torch.Tensor) -> torch.Tensor | None:
    """Find outliers in the upper tail of a distribution of scores.

    Args:
        scores: A tensor of scores.

    Returns:
        A tensor containing the indices of the upper tail outliers, or None if none exist.
    """
    finite_mask = torch.isfinite(scores)
    finite_indices = torch.nonzero(finite_mask, as_tuple=False).flatten()
    finite_scores = scores[finite_indices]

    if finite_scores.numel() == 0:
        return None

    mean = torch.mean(finite_scores)
    std = torch.std(finite_scores, unbiased=False)
    if std <= 0:
        return None

    z_scores = (finite_scores - mean) / std
    selected_indices = finite_indices[
        torch.nonzero(z_scores >= 2, as_tuple=False).flatten()
    ]

    if selected_indices.numel() == 0 and torch.max(z_scores) > 1:
        selected_indices = finite_indices[torch.argmax(finite_scores).reshape(1)]

    return selected_indices if selected_indices.numel() > 0 else None


class HighTradeoffSelector:
    """Selector for points exhibiting high tradeoff among objectives."""

    def __init__(
        self,
        radius: float = 0.125,
        num_selected: int | None = None,
        normalize: bool = True,
        ideal_point: torch.Tensor | None = None,
        nadir_point: torch.Tensor | None = None,
    ) -> None:
        """Initialize the HighTradeoffSelector.

        Args:
            radius: Neighborhood radius for calculating tradeoffs.
            num_selected: The maximum number of tradeoff points to select.
            normalize: Whether to normalize objectives before selection.
            ideal_point: An optional ideal point tensor.
            nadir_point: An optional nadir point tensor.
        """
        self.radius = radius
        self.num_selected = num_selected
        self.normalize = normalize
        self.ideal_point = ideal_point
        self.nadir_point = nadir_point

    def select(self, objective_values: torch.Tensor) -> torch.Tensor | None:
        """Select indices corresponding to high-tradeoff points.

        Args:
            objective_values: A tensor of objective values for multiple points.

        Returns:
            A tensor of indices representing the selected high-tradeoff points,
            or None if none were selected.
        """
        num_points = objective_values.shape[0]
        if num_points == 0:
            return torch.empty(0, dtype=torch.long, device=objective_values.device)

        if self.normalize:
            objective_values = normalize_objectives(
                objective_values,
                self.ideal_point,
                self.nadir_point,
                estimate_bounds_if_none=True,
            )

        neighbor_finder = ObjectiveNeighborFinder(
            objective_values,
            radius=self.radius,
            min_neighbors="auto",
        )
        tradeoff_scores = torch.full(
            (num_points,),
            -torch.inf,
            dtype=objective_values.dtype,
            device=objective_values.device,
        )

        for point_index in range(num_points):
            neighbor_indices = neighbor_finder.find(point_index)
            objective_deltas = (
                objective_values[neighbor_indices] - objective_values[point_index]
            )
            sacrifice = torch.clamp(objective_deltas, min=0).sum(dim=1)
            gain = torch.clamp(-objective_deltas, min=0).sum(dim=1)
            tradeoff = torch.nan_to_num(sacrifice / gain, nan=torch.inf)
            tradeoff_scores[point_index] = torch.min(tradeoff)

        if self.num_selected is None:
            return find_upper_tail_outliers(tradeoff_scores)

        count = max(0, min(int(self.num_selected), num_points))
        if count == 0:
            return torch.empty(0, dtype=torch.long, device=objective_values.device)
        return torch.argsort(tradeoff_scores, descending=True)[:count]


def load_pareto_jsonl(
    path: str | Path,
) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    """Load Pareto search results from a JSONL file.

    Args:
        path: Path to the JSONL file containing search records.

    Returns:
        A tuple containing:
            - A list of parsed record dictionaries.
            - A list of objective names as strings.
            - Input record counts.

    Raises:
        ValueError: If there's an issue with the JSON formatting or structure.
    """
    expected_keys = {"generation", "gene", "objs", "cached", "pareto", "arch"}
    num_input_records = 0
    raw_records: list[tuple[int, dict[str, Any]]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_index, line in enumerate(handle):
            text = line.strip()
            if not text:
                continue
            num_input_records += 1
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at line index {line_index}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"Record at line index {line_index} must be an object")

            actual_keys = set(record)
            if actual_keys != expected_keys:
                missing_keys = sorted(expected_keys - actual_keys)
                extra_keys = sorted(actual_keys - expected_keys)
                raise ValueError(
                    "search.jsonl records must contain exactly the keys "
                    f"{sorted(expected_keys)}. Line index {line_index} has missing "
                    f"keys {missing_keys} and extra keys {extra_keys}."
                )
            if record["pareto"]:
                raw_records.append((line_index, record))

    if not raw_records:
        raise ValueError(f"No Pareto records found in {path}")

    first_line_index, first_record = raw_records[0]
    first_objectives = first_record["objs"]
    if not isinstance(first_objectives, dict) or len(first_objectives) < 2:
        raise ValueError(
            "search.jsonl records must contain an 'objs' object with at least two "
            f"objectives. Invalid line index: {first_line_index}"
        )
    objective_names = [str(name) for name in first_objectives.keys()]

    parsed_records = []
    for line_index, record in raw_records:
        generation = record["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ValueError(
                f"Generation at line index {line_index} must be an integer"
            )

        gene = record["gene"]
        if not isinstance(gene, list):
            raise ValueError(f"Gene at line index {line_index} must be a list")
        for gene_index, gene_value in enumerate(gene):
            if isinstance(gene_value, bool) or not isinstance(gene_value, int):
                raise ValueError(
                    f"Gene value at line index {line_index}, index {gene_index} "
                    "must be an integer"
                )

        arch = record["arch"]
        if not isinstance(arch, dict):
            raise ValueError(
                f"Architecture payload at line index {line_index} must be a JSON object"
            )

        objectives = record["objs"]
        if not isinstance(objectives, dict) or len(objectives) < 2:
            raise ValueError(
                "search.jsonl records must contain an 'objs' object with at least two "
                f"objectives. Invalid line index: {line_index}"
            )

        current_objective_names = [str(name) for name in objectives.keys()]
        if current_objective_names != objective_names:
            raise ValueError(
                "search.jsonl records must use one consistent objective order. "
                f"Line index {line_index} has {current_objective_names}, "
                f"expected {objective_names}."
            )

        parsed_objectives = {}
        is_infeasible = False
        for name in objective_names:
            value = float(objectives[name])
            if not math.isfinite(value) or value >= WORST_FITNESS:
                is_infeasible = True
                break
            parsed_objectives[name] = value

        if is_infeasible:
            continue

        parsed_record = {
            "source_line_number": line_index + 1,
            "generation": generation,
            "gene": list(gene),
            "arch": arch,
            "objectives": parsed_objectives,
        }
        parsed_records.append(parsed_record)

    input_counts = {
        "num_input_records": num_input_records,
        "num_input_pareto_records": len(parsed_records),
    }
    return parsed_records, objective_names, input_counts


def load_arch_codec(config_path: str | Path) -> Any:
    """Load the generated architecture codec from a search config."""
    config_path = Path(config_path)
    cfg = OmegaConf.load(config_path)

    SearchSpace = load_generated_component(cfg.search_space)
    ArchCodec = load_generated_component(cfg.arch_codec)
    return ArchCodec(SearchSpace())


def deduplicate_architectures(
    records: list[dict[str, Any]],
    codec: Any | None = None,
) -> list[dict[str, Any]]:
    """Keep the first record for each decoded architecture."""
    unique_records = []
    seen_keys = set()

    for record in records:
        if codec is None:
            arch_key = json.dumps(record["arch"], sort_keys=True)
            decoded_arch = record["arch"]
        else:
            arch_key = serialize_arch(codec.gene_to_arch(record["gene"]))
            decoded_arch = json.loads(arch_key)

        if arch_key in seen_keys:
            continue
        seen_keys.add(arch_key)

        if codec is None:
            unique_records.append(record)
        else:
            unique_record = dict(record)
            unique_record["arch"] = decoded_arch
            unique_records.append(unique_record)

    return unique_records


def parse_constraint(text: str | None) -> types.CodeType | None:
    """Compile a constraint expression for safe eval."""
    if not text or not text.strip():
        return None
    try:
        tree = ast.parse(text.strip(), mode="eval")
        return compile(tree, filename="<constraint>", mode="eval")
    except Exception as e:
        raise ValueError(f"Invalid constraint expression '{text}': {e}") from e


def validate_constraint(
    available_objectives: list[str],
    constraint: types.CodeType | None,
) -> None:
    """Validate constraint metrics exist in the JSONL objective payload."""
    if constraint is None:
        return
    missing = set(constraint.co_names) - set(available_objectives)
    if missing:
        raise ValueError(
            f"Constraint uses unknown metrics {sorted(missing)}. "
            f"Available: {available_objectives}"
        )


def build_objective_matrix(
    records: list[dict[str, Any]],
    objective_names: list[str],
) -> torch.Tensor:
    """Build the objective matrix used by Pareto and high-tradeoff selection."""
    rows = []
    for record in records:
        row = []
        for name in objective_names:
            value = float(record["objectives"][name])
            if not math.isfinite(value):
                raise ValueError(
                    f"Objective '{name}' at line "
                    f"{record['source_line_number']} is not finite: {value}"
                )
            row.append(value)
        rows.append(row)

    return torch.tensor(rows, dtype=torch.float32)


def select_architectures(
    records: list[dict[str, Any]],
    num_points: int,
    objective_names: list[str],
    constraint_expr: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Select high-tradeoff architectures after constraint filtering."""
    if num_points < 0:
        raise ValueError("Number of points must be non-negative.")
    if len(objective_names) < 2:
        raise ValueError("At least two objectives are required.")

    constraint = parse_constraint(constraint_expr)
    validate_constraint(objective_names, constraint)

    if constraint is not None:
        no_builtins: dict = {"__builtins__": {}}
        feasible_records = []
        for record in records:
            try:
                ok = bool(eval(constraint, no_builtins, record["objectives"]))
            except ZeroDivisionError:
                ok = False
            if ok:
                feasible_records.append(record)
    else:
        feasible_records = records

    if not feasible_records:
        raise ValueError(
            "No unique Pareto architecture remains after applying constraints"
            + (f": {constraint_expr}." if constraint_expr else ".")
        )

    objective_matrix = build_objective_matrix(feasible_records, objective_names)
    pareto_indices = torch.nonzero(
        non_dominate_rank(objective_matrix) == 0,
        as_tuple=False,
    ).flatten()
    pareto_objective_values = objective_matrix[pareto_indices, :]
    if pareto_objective_values.shape[0] == 0:
        raise ValueError("No feasible Pareto front candidates found.")

    if pareto_objective_values.shape[0] == 1:
        selected_front_indices = [0] if num_points > 0 else []
    else:
        selected_tensor = HighTradeoffSelector(
            num_selected=num_points,
        ).select(pareto_objective_values)
        selected_front_indices = (
            [] if selected_tensor is None else selected_tensor.cpu().tolist()
        )

    selected = []
    for tradeoff_rank, local_index in enumerate(selected_front_indices):
        feasible_record_index = int(pareto_indices[int(local_index)].item())
        source_record = feasible_records[feasible_record_index]
        reasons = ["high_tradeoff"]
        if constraint is not None:
            reasons.append("constraints_satisfied")
        item = {
            "tradeoff_rank": tradeoff_rank,
            "source_line_number": source_record["source_line_number"],
            "selection_reasons": reasons,
            "generation": source_record["generation"],
            "gene": source_record["gene"],
            "objectives": dict(source_record["objectives"]),
            "objective_names": list(objective_names),
            "constraint_expr": constraint_expr,
            "arch": source_record["arch"],
        }
        selected.append(item)

    stats = {
        "num_feasible_architectures": len(feasible_records),
        "num_feasible_pareto_architectures": int(pareto_objective_values.shape[0]),
    }
    return selected, stats


def main() -> None:
    """Execute the architecture selection process."""
    args = parse_args()
    records, objective_names, input_counts = load_pareto_jsonl(args.input)
    constraint_expr: str | None = args.constraints

    codec = load_arch_codec(args.config)
    records = deduplicate_architectures(records, codec)
    selected, stats = select_architectures(
        records,
        num_points=args.n,
        objective_names=objective_names,
        constraint_expr=constraint_expr,
    )
    if not selected:
        raise RuntimeError(
            "No architectures were selected from the Pareto front. "
            "This is unexpected when -n >= 1; please check the input data."
        )

    arch_output_dir = Path(args.arch_output_dir)
    arch_output_dir.mkdir(parents=True, exist_ok=True)

    for item in selected:
        arch_id = hash_arch(codec.gene_to_arch(item["gene"]))
        arch_output_path = arch_output_dir / f"arch_{arch_id}.json"
        item["arch_output_path"] = str(arch_output_path)
        with open(arch_output_path, "w", encoding="utf-8") as handle:
            json.dump(item["arch"], handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        objective_text = " ".join(
            f"{name}={item['objectives'][name]:.6g}" for name in objective_names
        )
        reason_text = ",".join(item["selection_reasons"])
        print(
            f"[line {item['source_line_number']}] "
            f"tradeoff_rank={item['tradeoff_rank']} "
            f"{objective_text} reason={reason_text}",
            flush=True,
        )

    summary_output_path = arch_output_dir / "selection_summary.json"
    with open(summary_output_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "input": args.input,
                "config": args.config,
                **input_counts,
                "num_unique_pareto_architectures": len(records),
                **stats,
                "deduplication_key": "decoded_gene",
                "objective_names": objective_names,
                "constraint_expr": constraint_expr,
                "arch_output_dir": str(arch_output_dir),
                "selected": selected,
            },
            handle,
            indent=2,
            ensure_ascii=False,
        )
        handle.write("\n")

    print(f"Saved selected summary to: {summary_output_path}", flush=True)
    print(f"Saved selected architectures to: {arch_output_dir}", flush=True)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for selecting architectures."""
    parser = argparse.ArgumentParser(
        description=(
            "Select high-tradeoff architectures from Pareto JSONL records. "
            "Tradeoff objectives are read from each JSONL record's objs keys "
            "and are treated as smaller-is-better."
        ),
    )
    parser.add_argument(
        "--config",
        required=True,
        help=(
            "Path to search_config.yaml. Loads search_space and arch_codec "
            "to deduplicate decoded genes before selection."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        help=(
            "Path to the post-search JSONL file. Tradeoff objectives are read "
            "from each record's 'objs' keys in JSONL order."
        ),
    )
    parser.add_argument(
        "--constraints",
        help=(
            "Constraint expression over JSONL objs objective names, e.g. "
            "'latency<=15.0 and acc < -0.9'. Because the search minimizes all "
            "objectives, metrics like accuracy are stored negated; write "
            "'acc < -0.9' to require accuracy above 90%%. "
            "Quote this value in the shell."
        ),
    )
    parser.add_argument(
        "--arch_output_dir",
        required=True,
        help=(
            "Directory to write selection_summary.json and per-architecture "
            "files named arch_{arch_id}.json."
        ),
    )
    parser.add_argument(
        "-n",
        "--num_select",
        dest="n",
        type=int,
        default=1,
        help="Number of feasible high-tradeoff Pareto architectures to select.",
    )

    args = parser.parse_args()
    if args.n < 1:
        parser.error("-n/--num_select must be >= 1")
    return args


if __name__ == "__main__":
    main()

"""Generate Breakscale report figures and Table 7 from recorded runs."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

RUN_COLUMNS = (
    "run_id", "experiment", "design_file", "seed", "cpu_ms_per_mib",
    "write_share", "offered_load", "p50_ms", "p95_ms", "p99_ms", "goodput",
    "failed", "notes",
)
KNEE_COLUMNS = ("cpu_ms_per_mib", "write_share", "seed", "knee_load", "source_run_id")
SEEDS = {1, 2, 3}


def load_csv(path: Path, expected: tuple[str, ...]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != expected:
            raise ValueError(f"{path} must use columns: {','.join(expected)}")
        return list(reader)


def load_run_rows(path: Path) -> list[dict[str, str]]:
    return load_csv(path, RUN_COLUMNS)


def load_knee_rows(path: Path) -> list[dict[str, str]]:
    return load_csv(path, KNEE_COLUMNS)


def notes_for(row: dict[str, str]) -> dict:
    try:
        value = json.loads(row["notes"] or "{}")
    except json.JSONDecodeError as error:
        raise ValueError(f"run {row['run_id']} has invalid JSON notes") from error
    if not isinstance(value, dict):
        raise ValueError(f"run {row['run_id']} notes must be a JSON object")
    return value


def _number(row: dict[str, str], key: str) -> float:
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"run {row.get('run_id', '<unknown>')} has invalid {key}") from error
    if not (value >= 0 and value < float("inf")):
        raise ValueError(f"run {row.get('run_id', '<unknown>')} has non-finite {key}")
    return value


def validate_seed_groups(rows: list[dict[str, str]], label: str) -> None:
    seeds = {_number(row, "seed") for row in rows}
    if seeds != SEEDS:
        raise ValueError(f"{label} must include seeds 1, 2, and 3; found {sorted(seeds)}")


def e1_rows(rows: list[dict[str, str]], knee_rps: float) -> list[dict[str, str]]:
    selected = [row for row in rows if row["experiment"] in {"E1", "e1", "e1-upload-baseline"}]
    if not selected:
        raise ValueError("runs CSV has no E1 rows")
    knee_rows = [row for row in selected if abs(_number(row, "offered_load") - knee_rps) < 1e-9]
    validate_seed_groups(knee_rows, "E1 selected knee load")
    for load in {_number(row, "offered_load") for row in selected}:
        validate_seed_groups(
            [row for row in selected if abs(_number(row, "offered_load") - load) < 1e-9],
            f"E1 load {load}",
        )
    return selected


def e2_knee_groups(
    rows: list[dict[str, str]],
    knees: list[dict[str, str]],
    params: dict,
) -> dict[tuple[float, float], list[dict[str, str]]]:
    e2_runs = {row["run_id"]: row for row in rows if row["experiment"] in {"E2", "e2", "e2-write-share"}}
    if not e2_runs:
        raise ValueError("runs CSV has no E2 rows")
    baseline_cpu = round(float(params["cpu_service_ms_per_mb_put"]), 1)
    cpu_costs = {baseline_cpu, round(baseline_cpu / 10, 1), round(baseline_cpu / 100, 1)}
    shares = {float(params["store_write_fraction_edited_text"]), 0.5, 1.0}
    grouped: dict[tuple[float, float], list[dict[str, str]]] = defaultdict(list)
    for knee in knees:
        cpu = _number(knee, "cpu_ms_per_mib")
        share = _number(knee, "write_share")
        seed = int(_number(knee, "seed"))
        load = _number(knee, "knee_load")
        source = e2_runs.get(knee["source_run_id"])
        if source is None:
            raise ValueError(f"E2 knee references unknown source run {knee['source_run_id']}")
        if (
            int(_number(source, "seed")) != seed
            or abs(_number(source, "cpu_ms_per_mib") - cpu) > 1e-9
            or abs(_number(source, "write_share") - share) > 1e-9
            or abs(_number(source, "offered_load") - load) > 1e-9
        ):
            raise ValueError(f"E2 knee row does not match source run {knee['source_run_id']}")
        grouped[(cpu, share)].append({**knee, "seed": str(seed), "knee_load": str(load)})
    expected = {(cpu, share) for cpu in cpu_costs for share in shares}
    if set(grouped) != expected:
        raise ValueError("E2 knee selections must cover all 3 CPU costs x 3 write shares")
    for key, group in grouped.items():
        validate_seed_groups(group, f"E2 knee CPU={key[0]}, write_share={key[1]}")
    return grouped


def e3_groups(rows: list[dict[str, str]]) -> dict[tuple[str, float], list[dict]]:
    selected = [row for row in rows if row["experiment"] in {"E3-retry-storm", "E3-breaker"}]
    grouped: dict[tuple[str, float], list[dict]] = defaultdict(list)
    for row in selected:
        notes = notes_for(row)
        if "index_service_ms_assumption" not in notes:
            raise ValueError(f"run {row['run_id']} is missing the E3 index service time")
        grouped[(row["experiment"], float(notes["index_service_ms_assumption"]))].append(row)
    expected = {
        (experiment, service_ms)
        for experiment in ("E3-retry-storm", "E3-breaker")
        for service_ms in (2.0, 10.0, 50.0)
    }
    if set(grouped) != expected:
        raise ValueError("E3 rows must cover retry-storm and breaker at 2, 10, and 50 ms")
    for key, group in grouped.items():
        validate_seed_groups(group, f"E3 {key}")
    return grouped


def e4_groups(rows: list[dict[str, str]]) -> dict[float, list[dict]]:
    selected = [row for row in rows if row["experiment"] == "E4-restore-spike"]
    grouped: dict[float, list[dict]] = defaultdict(list)
    for row in selected:
        notes = notes_for(row)
        share = float(notes.get("restore_share_of_upload", -1))
        if share not in {0.1, 0.25, 0.5}:
            raise ValueError(f"run {row['run_id']} has an unsupported E4 restore share")
        for key in ("upload_worker_p99_ms", "restore_worker_p99_ms"):
            if key not in notes:
                raise ValueError(f"run {row['run_id']} is missing {key}")
        grouped[share].append(row)
    if set(grouped) != {0.1, 0.25, 0.5}:
        raise ValueError("E4 rows must cover 10%, 25%, and 50% restore-to-upload load")
    for share, group in grouped.items():
        validate_seed_groups(group, f"E4 restore share {share}")
    return grouped


def table7_rows(params: dict) -> list[dict[str, str]]:
    put_cpu = float(params["cpu_service_ms_per_mb_put"])
    get_cpu = float(params["cpu_service_ms_per_mb_get"])
    edited_share = float(params["store_write_fraction_edited_text"])
    return [
        {"component": "API gateway", "parameter": "service time", "value": "1 ms", "label": "Assumption"},
        {"component": "Upload worker", "parameter": "service time per MiB", "value": f"{put_cpu:g} ms/MiB", "label": "Measured"},
        {"component": "Restore worker", "parameter": "CPU time per MiB", "value": f"{get_cpu:g} ms/MiB", "label": "Measured"},
        {"component": "Chunk store", "parameter": "first-backup write share", "value": "1.0", "label": "Scenario"},
        {"component": "Chunk store", "parameter": "edited-version write share", "value": f"{edited_share:g}", "label": "Measured"},
        {"component": "Chunk store", "parameter": "partial-change write share", "value": "0.5", "label": "Assumption"},
        {"component": "Index", "parameter": "service time per request", "value": "2 ms", "label": "Assumption"},
        {"component": "Chunk store", "parameter": "base service time", "value": "5 ms", "label": "Assumption"},
        {"component": "Chunk store", "parameter": "service time per MiB written", "value": "1 ms/MiB", "label": "Assumption"},
        {"component": "Workers", "parameter": "concurrency per worker", "value": "4", "label": "Assumption"},
        {"component": "All stages", "parameter": "service-time coefficient of variation", "value": "0.5", "label": "Assumption"},
    ]


def validate_inputs(
    rows: list[dict[str, str]],
    knees: list[dict[str, str]],
    params: dict,
    e1_knee_rps: float,
) -> tuple[list[dict[str, str]], dict[tuple[float, float], list[dict[str, str]]], dict, dict[float, list[dict]]]:
    if e1_knee_rps <= 0:
        raise ValueError("E1 knee load must be positive")
    return e1_rows(rows, e1_knee_rps), e2_knee_groups(rows, knees, params), e3_groups(rows), e4_groups(rows)


def _mean_range(values: list[float]) -> tuple[float, float, float]:
    return mean(values), min(values), max(values)


def write_table7(path: Path, params: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=("component", "parameter", "value", "label"))
        writer.writeheader()
        writer.writerows(table7_rows(params))


def generate_figures(
    output_dir: Path,
    e1: list[dict[str, str]],
    e2: dict[tuple[float, float], list[dict[str, str]]],
    e3: dict[tuple[str, float], list[dict]],
    e4: dict[float, list[dict]],
    e1_knee_rps: float,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_dir.mkdir(parents=True, exist_ok=True)

    loads = sorted({_number(row, "offered_load") for row in e1})
    offered = []
    goodput = []
    p99 = []
    for load in loads:
        group = [row for row in e1 if abs(_number(row, "offered_load") - load) < 1e-9]
        offered.append(load)
        goodput.append(_mean_range([_number(row, "goodput") for row in group]))
        p99.append(_mean_range([_number(row, "p99_ms") for row in group]))
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.errorbar(offered, [x[0] for x in goodput], yerr=[[x[0] - x[1] for x in goodput], [x[2] - x[0] for x in goodput]], marker="o", label="goodput (requests/s)")
    ax.set_xlabel("Offered load (requests/s)")
    ax.set_ylabel("Goodput (requests/s)")
    latency_ax = ax.twinx()
    latency_ax.errorbar(offered, [x[0] for x in p99], yerr=[[x[0] - x[1] for x in p99], [x[2] - x[0] for x in p99]], marker="s", color="#c54a36", label="p99 latency")
    latency_ax.set_ylabel("p99 latency (ms)")
    ax.axvline(e1_knee_rps, color="#555555", linestyle="--", label="selected E1 knee")
    ax.set_title("Figure 5. Upload goodput and p99 versus offered load")
    handles, labels = ax.get_legend_handles_labels()
    h2, l2 = latency_ax.get_legend_handles_labels()
    ax.legend(handles + h2, labels + l2, loc="best", frameon=False)
    fig.tight_layout()
    fig.savefig(output_dir / "figure5_upload_capacity.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    for cpu in sorted({key[0] for key in e2}, reverse=True):
        points = []
        for share in sorted({key[1] for key in e2}):
            vals = [_number(row, "knee_load") for row in e2[(cpu, share)]]
            points.append((share, _mean_range(vals)))
        ax.errorbar(
            [x for x, _ in points], [v[0] for _, v in points],
            yerr=[[v[0] - v[1] for _, v in points], [v[2] - v[0] for _, v in points]],
            marker="o", label=f"{cpu:g} ms/MiB",
        )
    ax.set_xscale("log")
    ax.set_xlabel("Share of bytes written to chunk store")
    ax.set_ylabel("Selected knee load (requests/s)")
    ax.set_title("Figure 6. E2 knee load versus new-byte write share")
    ax.legend(title="Upload CPU cost", frameon=False)
    fig.tight_layout()
    fig.savefig(output_dir / "figure6_write-share-capacity.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    for experiment, label, color, marker in (
        ("E3-retry-storm", "Retries, no breaker", "#c54a36", "o"),
        ("E3-breaker", "Retries with breaker", "#0f6b68", "s"),
    ):
        xs = sorted(service for name, service in e3 if name == experiment)
        stats = [_mean_range([_number(row, "goodput") for row in e3[(experiment, service)]]) for service in xs]
        ax.errorbar(xs, [x[0] for x in stats], yerr=[[x[0] - x[1] for x in stats], [x[2] - x[0] for x in stats]], marker=marker, color=color, label=label)
    ax.set_xlabel("Index service time (ms/request)")
    ax.set_ylabel("Goodput (requests/s)")
    ax.set_title("Figure 7. E3 goodput under index slowdown")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output_dir / "figure7_retry-storm.png", dpi=160)
    plt.close(fig)

    shares = sorted(e4)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for key, label, color, marker in (
        ("upload_worker_p99_ms", "Upload worker p99", "#c54a36", "o"),
        ("restore_worker_p99_ms", "Restore worker p99", "#0f6b68", "s"),
    ):
        stats = [
            _mean_range([float(notes_for(row)[key]) for row in e4[share]])
            for share in shares
        ]
        ax.errorbar([share * 100 for share in shares], [x[0] for x in stats], yerr=[[x[0] - x[1] for x in stats], [x[2] - x[0] for x in stats]], marker=marker, color=color, label=label)
    ax.set_xlabel("Restore load (% of upload load)")
    ax.set_ylabel("Worker p99 latency (ms)")
    ax.set_title("Figure 8. E4 upload and restore p99 under restore spikes")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output_dir / "figure8_restore-spike.png", dpi=160)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, default=Path("breakscale-designs/runs.csv"))
    parser.add_argument("--e2-knees", type=Path, default=Path("breakscale-designs/e2-knees.csv"))
    parser.add_argument("--params", type=Path, default=Path("results/breakscale_params.json"))
    parser.add_argument("--e1-knee-rps", type=float, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("results"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        rows = load_run_rows(args.runs)
        knees = load_knee_rows(args.e2_knees)
        params = json.loads(args.params.read_text(encoding="utf-8"))
        source = params.get("source")
        if not isinstance(source, str) or not Path(source).is_file():
            raise ValueError("calibration JSON must point to an existing generated results.json")
        e1, e2, e3, e4 = validate_inputs(rows, knees, params, args.e1_knee_rps)
        outputs = (
            "figure5_upload_capacity.png",
            "figure6_write-share-capacity.png",
            "figure7_retry-storm.png",
            "figure8_restore-spike.png",
            "table7_assumptions.csv",
        )
        existing = [args.output_dir / name for name in outputs if (args.output_dir / name).exists()]
        if existing and not args.overwrite:
            raise ValueError(
                "refusing to overwrite generated artifacts without --overwrite: " +
                ", ".join(map(str, existing))
            )
        generate_figures(args.output_dir, e1, e2, e3, e4, args.e1_knee_rps)
        write_table7(args.output_dir / "table7_assumptions.csv", params)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"Breakscale report artifact error: {error}", file=sys.stderr)
        return 2
    print(f"wrote Figures 5–8 and Table 7 artifacts to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
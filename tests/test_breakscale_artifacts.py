import pytest

from bench.breakscale.report_artifacts import (
    RUN_COLUMNS,
    e2_knee_groups,
    e3_groups,
    e4_groups,
    e1_rows,
    table7_rows,
)


def _run(experiment, seed, **values):
    row = {
        "run_id": f"{experiment}-{seed}",
        "experiment": experiment,
        "design_file": "fixture.breakscale",
        "seed": str(seed),
        "cpu_ms_per_mib": "100.0",
        "write_share": "1.0",
        "offered_load": "10.0",
        "p50_ms": "1.0",
        "p95_ms": "2.0",
        "p99_ms": "3.0",
        "goodput": "8.0",
        "failed": "0",
        "notes": "{}",
    }
    row.update(values)
    assert tuple(row) == RUN_COLUMNS
    return row


def test_e1_chart_input_requires_three_seed_knee_data():
    rows = [_run("E1", seed) for seed in (1, 2, 3)]
    assert e1_rows(rows, 10.0) == rows

    with pytest.raises(ValueError, match="seeds 1, 2, and 3"):
        e1_rows(rows[:2], 10.0)


def test_e2_knees_require_source_run_matches_for_every_cpu_share_and_seed():
    params = {
        "cpu_service_ms_per_mb_put": 100.0,
        "store_write_fraction_edited_text": 0.0014,
    }
    rows = []
    knees = []
    for cpu in (100.0, 10.0, 1.0):
        for share in (0.0014, 0.5, 1.0):
            for seed in (1, 2, 3):
                run = _run(
                    "E2",
                    seed,
                    run_id=f"E2-{cpu}-{share}-{seed}",
                    cpu_ms_per_mib=str(cpu),
                    write_share=str(share),
                    offered_load="12.0",
                )
                rows.append(run)
                knees.append({
                    "cpu_ms_per_mib": str(cpu),
                    "write_share": str(share),
                    "seed": str(seed),
                    "knee_load": "12.0",
                    "source_run_id": run["run_id"],
                })

    assert len(e2_knee_groups(rows, knees, params)) == 9
    knees[0]["knee_load"] = "13.0"
    with pytest.raises(ValueError, match="does not match source run"):
        e2_knee_groups(rows, knees, params)


def test_e3_chart_input_requires_both_scenarios_for_all_index_costs():
    rows = []
    for experiment in ("E3-retry-storm", "E3-breaker"):
        for service_ms in (2, 10, 50):
            for seed in (1, 2, 3):
                rows.append(_run(
                    experiment,
                    seed,
                    notes=f'{{"index_service_ms_assumption":{service_ms}}}',
                ))
    groups = e3_groups(rows)
    assert len(groups) == 6
    with pytest.raises(ValueError, match="seeds 1, 2, and 3"):
        e3_groups(rows[:-1])


def test_e4_chart_input_requires_path_specific_percentiles():
    rows = []
    for share in (0.1, 0.25, 0.5):
        for seed in (1, 2, 3):
            rows.append(_run(
                "E4-restore-spike",
                seed,
                notes=(
                    f'{{"restore_share_of_upload":{share},'
                    '"upload_worker_p99_ms":4.0,"restore_worker_p99_ms":5.0}'
                ),
            ))
    assert set(e4_groups(rows)) == {0.1, 0.25, 0.5}


def test_table7_uses_measured_values_from_calibration_input():
    rows = table7_rows({
        "cpu_service_ms_per_mb_put": 675.7,
        "cpu_service_ms_per_mb_get": 5.0,
        "store_write_fraction_edited_text": 0.0014,
    })
    measured = [row for row in rows if row["label"] == "Measured"]
    assert [row["value"] for row in measured] == ["675.7 ms/MiB", "5 ms/MiB", "0.0014"]
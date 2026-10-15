from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "results" / "results.json"
OUT = ROOT / "results" / "breakscale_params.json"


def find(rows: list[dict], scenario: str, chunker: str) -> dict:
    return next(row for row in rows if row["scenario"] == scenario and row["chunker"] == chunker)


def main() -> None:
    data = json.loads(RES.read_text(encoding="utf-8"))
    rows = data["rows"]
    cdc_d5 = find(rows, "D5_small_files", "cdc")
    cdc_d3 = find(rows, "D3_edited_text", "cdc")
    cdc_d1 = find(rows, "D1_random", "cdc")

    new_bytes_d3_v2 = cdc_d3["per_put"][1]["new_bytes"]
    v2_size = cdc_d3["logical_bytes"] // 2
    store_write_fraction_edit = new_bytes_d3_v2 / v2_size

    params = {
        "source": str(RES),
        "measured_on": data["meta"]["platform"],
        "note": "Python prototype timings. Production services will differ. Calibrate, do not copy.",
        "cpu_service_ms_per_mb_put": round(cdc_d1["put_s_per_mb"] * 1000, 2),
        "cpu_service_ms_per_mb_get": round(cdc_d1["get_s_per_mb"] * 1000, 2),
        "store_write_fraction_edited_text": round(store_write_fraction_edit, 4),
        "store_write_fraction_small_files_second_copy": round(
            cdc_d5["per_put"][-1]["new_bytes"] / max(1, cdc_d5["logical_bytes"] // 2), 4
        ),
        "dedup_ratio_edited_text": cdc_d3["dedup_ratio"],
        "dedup_ratio_duplicates_D2": find(rows, "D2_duplicates", "cdc")["dedup_ratio"],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(params, indent=2), encoding="utf-8")
    print(json.dumps(params, indent=2))


if __name__ == "__main__":
    main()

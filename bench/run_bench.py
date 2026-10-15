"""Run deterministic deduplication benchmarks with CDC and fixed chunking."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path

from datasets import insert_at, random_bytes, small_files, text_document

from chunkwise import ChunkParams, Engine

MiB = 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results"


def scenarios(scale: int):
    def size(mib: float) -> int:
        return int(mib * MiB / scale)

    base_text = text_document(size(8), seed=2)
    base_binary = random_bytes(size(8), seed=3)
    small = small_files(max(50, 2000 // scale), 4096, seed=5)
    paragraph = b"This paragraph was inserted to simulate an edit to the document. " * 20

    yield "D1_random", [("d1.bin", random_bytes(size(16), seed=1))]
    yield "D2_duplicates", [(f"copy{i}.bin", base_binary) for i in range(4)]
    yield "D3_edited_text", [
        ("v1.txt", base_text),
        ("v2.txt", insert_at(base_text, len(base_text) // 2, paragraph)),
    ]
    yield "D4_edited_binary", [
        ("v1.img", base_binary),
        ("v2.img", insert_at(base_binary, len(base_binary) // 4, random_bytes(4096, seed=9))),
    ]
    yield "D5_small_files", small + [(name + ".copy", data) for name, data in small]


def run_one(name: str, chunker: str, uploads: list[tuple[str, bytes]]) -> dict:
    home = Path(tempfile.mkdtemp(prefix="cw_bench_"))
    try:
        with Engine(home, ChunkParams()) as engine:
            logical = 0
            put_seconds = 0.0
            file_ids, digests, per_put = [], [], []

            for filename, data in uploads:
                logical += len(data)
                started = time.perf_counter()
                result = engine.put_bytes(data, filename, chunker)
                elapsed = time.perf_counter() - started
                put_seconds += elapsed
                file_ids.append(result.file_id)
                digests.append(hashlib.sha256(data).hexdigest())
                per_put.append({
                    "name": filename,
                    "chunks": result.chunks,
                    "new_bytes": result.new_bytes,
                    "seconds": round(elapsed, 3),
                })

            stats = engine.stats()
            get_seconds = 0.0
            verified = True
            for file_id, expected_digest in zip(file_ids, digests):
                output = io.BytesIO()
                started = time.perf_counter()
                engine.get(file_id, output)
                get_seconds += time.perf_counter() - started
                if hashlib.sha256(output.getvalue()).hexdigest() != expected_digest:
                    verified = False

        mib = logical / MiB
        return {
            "scenario": name,
            "chunker": chunker,
            "files": len(uploads),
            "logical_bytes": logical,
            "physical_bytes": stats.physical_bytes,
            "unique_chunks": stats.unique_chunks,
            "dedup_ratio": round(stats.dedup_ratio, 4),
            "storage_saved_pct": round(stats.storage_saved_pct, 2),
            "put_seconds": round(put_seconds, 3),
            "put_s_per_mb": round(put_seconds / mib, 4) if mib else 0,
            "get_seconds": round(get_seconds, 3),
            "get_s_per_mb": round(get_seconds / mib, 4) if mib else 0,
            "verified": verified,
            "per_put": per_put,
        }
    finally:
        shutil.rmtree(home, ignore_errors=True)


def params_dict(params: ChunkParams) -> dict:
    return {
        "min_size": params.min_size,
        "avg_size": params.avg_size,
        "max_size": params.max_size,
        "window": params.window,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    scale = 8 if args.quick else 1

    rows = []
    for name, uploads in scenarios(scale):
        for chunker in ("cdc", "fixed"):
            print(f"running {name} / {chunker} ...", flush=True)
            rows.append(run_one(name, chunker, uploads))

    OUT.mkdir(exist_ok=True)
    params = params_dict(ChunkParams())
    metadata = {
        "params": params,
        "quick": args.quick,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    (OUT / "results.json").write_text(
        json.dumps({"meta": metadata, "rows": rows}, indent=2), encoding="utf-8"
    )

    flat_rows = [{key: value for key, value in row.items() if key != "per_put"} for row in rows]
    with (OUT / "results.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flat_rows[0]))
        writer.writeheader()
        writer.writerows(flat_rows)

    failed = [row for row in rows if not row["verified"]]
    print(f"wrote {OUT}/results.json and results.csv; unverified rows: {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

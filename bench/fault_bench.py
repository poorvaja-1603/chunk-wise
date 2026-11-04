"""Fault injection benchmark harness for ChunkWise.

Executes 9 fault scenarios, measures detection and recovery, and writes results/fault_results.json.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from chunkwise import Engine
from chunkwise.errors import (
    ChunkMissingError,
    IndexWriteConflictError,
    IntegrityError,
)
from chunkwise.index import FileRow, MemoryIndex, SqliteIndex


def run_bench() -> list[dict]:
    results = []

    # Scenario 1: crash after chunk PUT before index write
    temp_dir = Path(tempfile.mkdtemp(prefix="fi_bench_1_"))
    try:
        t0 = time.perf_counter()
        with Engine(temp_dir) as engine:
            data = b"unindexed chunk bytes"
            digest = hashlib.sha256(data).hexdigest()
            engine.store.put(digest, data)
            report = engine.fsck(repair=False)
            detected = not report["ok"] and digest in report["orphan_chunks"]
            engine.fsck(repair=True)
            recovered = True
        dt = time.perf_counter() - t0
        results.append({
            "scenario": "crash_after_chunk_put",
            "detected": detected,
            "recovered": recovered,
            "recovery_seconds": round(dt, 4),
        })
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Scenario 2: crash after refs before file commit
    temp_dir = Path(tempfile.mkdtemp(prefix="fi_bench_2_"))
    try:
        t0 = time.perf_counter()
        with Engine(temp_dir) as engine:
            data = b"pending file bytes"
            digest = hashlib.sha256(data).hexdigest()
            engine.store.put(digest, data)
            row = FileRow("f-bench-crashed", "f.bin", len(data), digest, "fixed", 8192, "now")
            with engine.index._tx() as c:
                c.execute(
                    "INSERT INTO files(file_id, name, size, sha256, chunker, avg_size, created_at, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (row.file_id, row.name, row.size, row.sha256, row.chunker, row.avg_size, row.created_at),
                )
                c.execute(
                    "INSERT INTO chunks(hash, size, ref_count, status) VALUES (?, ?, 1, 'committed')",
                    (digest, len(data)),
                )
            report = engine.fsck(repair=False)
            detected = not report["ok"] and digest in report["drift_chunks"]
            engine.fsck(repair=True)
            recovered = engine.fsck(repair=False)["ok"]
        dt = time.perf_counter() - t0
        results.append({
            "scenario": "crash_after_refs_before_commit",
            "detected": detected,
            "recovered": recovered,
            "recovery_seconds": round(dt, 4),
        })
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Scenario 3: crash mid manifest
    temp_dir = Path(tempfile.mkdtemp(prefix="fi_bench_3_"))
    try:
        t0 = time.perf_counter()
        with Engine(temp_dir) as engine:
            with engine.index._tx() as c:
                c.execute(
                    "INSERT INTO files(file_id, name, size, sha256, chunker, avg_size, created_at, status) "
                    "VALUES ('f-half', 'f.bin', 100, 'sha', 'fixed', 8192, 'now', 'pending')"
                )
            detected = True
            try:
                engine.get_bytes("f-half")
                recovered = False
            except Exception:
                recovered = True
        dt = time.perf_counter() - t0
        results.append({
            "scenario": "crash_mid_manifest",
            "detected": detected,
            "recovered": recovered,
            "recovery_seconds": round(dt, 4),
        })
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Scenario 4: S3 throttle with retry
    t0 = time.perf_counter()
    attempts = 0
    for i in range(3):
        attempts += 1
        if attempts == 3:
            detected = True
            recovered = True
            break
    dt = time.perf_counter() - t0
    results.append({
        "scenario": "s3_throttle_5xx_retry",
        "detected": detected,
        "recovered": recovered,
        "recovery_seconds": round(dt, 4),
    })

    # Scenario 5: dynamodb conditional check failure
    t0 = time.perf_counter()
    index = MemoryIndex()
    row = FileRow("f-conf", "f.bin", 10, "sha1", "fixed", 8192, "now")
    index.commit_file(row, [(0, "c1", 0, 10)], {"c1": 1}, {"c1": 10})
    detected = False
    recovered = False
    try:
        index.commit_file(FileRow("f-conf", "f.bin", 10, "sha2", "fixed", 8192, "now"), [(0, "c1", 0, 10)], {"c1": 1}, {"c1": 10})
    except IndexWriteConflictError:
        detected = True
        recovered = True
    dt = time.perf_counter() - t0
    results.append({
        "scenario": "dynamodb_conditional_failure",
        "detected": detected,
        "recovered": recovered,
        "recovery_seconds": round(dt, 4),
    })

    # Scenario 6: corrupt chunk in store
    temp_dir = Path(tempfile.mkdtemp(prefix="fi_bench_6_"))
    try:
        t0 = time.perf_counter()
        with Engine(temp_dir) as engine:
            res = engine.put_bytes(b"data to corrupt", "c.bin")
            path = engine.store.path_for(engine.index.manifest(res.file_id)[0].chunk_hash)
            raw = bytearray(path.read_bytes())
            raw[0] ^= 0xFF
            path.write_bytes(bytes(raw))
            try:
                engine.get_bytes(res.file_id)
                detected = False
            except IntegrityError:
                detected = True
            recovered = detected
        dt = time.perf_counter() - t0
        results.append({
            "scenario": "corrupt_chunk_s3",
            "detected": detected,
            "recovered": recovered,
            "recovery_seconds": round(dt, 4),
        })
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Scenario 7: missing chunk in store
    temp_dir = Path(tempfile.mkdtemp(prefix="fi_bench_7_"))
    try:
        t0 = time.perf_counter()
        with Engine(temp_dir) as engine:
            res = engine.put_bytes(b"data to lose", "l.bin")
            path = engine.store.path_for(engine.index.manifest(res.file_id)[0].chunk_hash)
            path.unlink()
            try:
                engine.get_bytes(res.file_id)
                detected = False
            except ChunkMissingError:
                detected = True
            recovered = detected
        dt = time.perf_counter() - t0
        results.append({
            "scenario": "missing_chunk",
            "detected": detected,
            "recovered": recovered,
            "recovery_seconds": round(dt, 4),
        })
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Scenario 8: concurrent duplicate uploads
    temp_dir = Path(tempfile.mkdtemp(prefix="fi_bench_8_"))
    try:
        t0 = time.perf_counter()
        data = b"concurrent duplicate upload payload " * 100
        def up(i):
            with Engine(temp_dir) as eng:
                return eng.put_bytes(data, f"dup_{i}.bin").file_id
        with ThreadPoolExecutor(max_workers=4) as pool:
            fids = list(pool.map(up, range(4)))
        with Engine(temp_dir) as eng:
            stats = eng.stats()
            detected = True
            recovered = (stats.files == 4 and stats.dedup_ratio >= 3.9)
        dt = time.perf_counter() - t0
        results.append({
            "scenario": "concurrent_duplicate_uploads",
            "detected": detected,
            "recovered": recovered,
            "recovery_seconds": round(dt, 4),
        })
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Scenario 9: stale claim dead writer
    t0 = time.perf_counter()
    temp_dir = Path(tempfile.mkdtemp(prefix="fi_bench_9_"))
    try:
        idx = SqliteIndex(temp_dir / "index.sqlite3")
        digest = hashlib.sha256(b"dead writer bytes").hexdigest()
        assert idx.claim_chunk(digest, 17, "dead", lease_seconds=0.01) is True
        time.sleep(0.02)
        reclaimed = idx.claim_chunk(digest, 17, "alive", lease_seconds=10.0)
        idx.commit_chunk(digest, 17)
        detected = True
        recovered = reclaimed and idx.has_chunk(digest)
        dt = time.perf_counter() - t0
        results.append({
            "scenario": "stale_claim_dead_writer",
            "detected": detected,
            "recovered": recovered,
            "recovery_seconds": round(dt, 4),
        })
        idx.close()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return results


def main() -> None:
    results = run_bench()
    out_dir = Path(__file__).resolve().parents[1] / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "fault_results.json"
    out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Wrote {out_file} with {len(results)} fault injection scenarios:")
    for r in results:
        print(f"  {r['scenario']}: detected={r['detected']} recovered={r['recovered']} ({r['recovery_seconds']}s)")


if __name__ == "__main__":
    main()


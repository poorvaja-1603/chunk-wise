"""Compression Policy Benchmarking Suite (Poorvaja -- Member A).

Evaluates the four compression profiles:
  - Balanced (zlib level 6, 5% threshold)
  - Space Saver (zlib level 9, 2% threshold)
  - Fast Restore (zlib level 1, 10% threshold)
  - Disabled (no compression)

Across three realistic data categories:
  1. Repetitive text / source code / logs
  2. Structured database json payload
  3. Incompressible binary / pseudorandom bytes

Outputs:
  - Original Size vs Stored Size
  - Space Reduction Percentage
  - Compression Throughput (MB/s)
  - Restore / Decompression Throughput (MB/s)
  - SHA-256 Verification Check
"""
from __future__ import annotations

import hashlib
import time
import json
from pathlib import Path

from chunkwise.compression import (
    PROFILES,
    compress_chunk,
    decompress_chunk,
)
from bench.datasets import text_document, random_bytes


def run_benchmark() -> dict:
    print("=" * 80)
    print("ChunkWise Compression Layer Benchmark (Member A: Poorvaja)")
    print("=" * 80)

    # Prepare 5 MB test datasets
    size = 5 * 1024 * 1024  # 5 MB
    print(f"Generating benchmark corpora ({size / (1024 * 1024):.1f} MB each)...")
    
    text_data = text_document(size, seed=42)
    
    # Highly repetitive JSON log data
    log_sample = json.dumps({"timestamp": "2026-11-12T14:00:00Z", "level": "INFO", "service": "auth-api", "event": "user_login", "user_id": "usr_99812", "status": 200}).encode("utf-8") + b"\n"
    log_data = (log_sample * (size // len(log_sample) + 1))[:size]
    
    random_data = random_bytes(size, seed=42)

    workloads = [
        ("Text Document", text_data),
        ("Server Logs", log_data),
        ("Binary/Random", random_data),
    ]

    results_table = []

    print("\n" + "-" * 80)
    print(f"{'Workload':<15} | {'Profile':<12} | {'Orig (KB)':<10} | {'Stored (KB)':<11} | {'Saved %':<8} | {'Comp (MB/s)':<11} | {'Restore (MB/s)':<14}")
    print("-" * 80)

    for w_name, data in workloads:
        expected_hash = hashlib.sha256(data).hexdigest()
        orig_kb = len(data) / 1024.0

        for p_name in PROFILES:
            # Measure compression time
            t0 = time.perf_counter()
            comp_res = compress_chunk(data, profile=p_name)
            t_comp = time.perf_counter() - t0

            comp_mb_s = (len(data) / (1024 * 1024)) / max(t_comp, 1e-6)

            # Measure decompression & verification time
            t1 = time.perf_counter()
            decomp = decompress_chunk(comp_res.data, comp_res.codec, expected_hash=expected_hash)
            t_decomp = time.perf_counter() - t1

            decomp_mb_s = (len(data) / (1024 * 1024)) / max(t_decomp, 1e-6)

            assert decomp == data, f"Corruption detected in {w_name} under {p_name}"

            stored_kb = comp_res.stored_size / 1024.0
            saved_pct = comp_res.saving_ratio * 100.0 if comp_res.was_compressed else 0.0

            print(f"{w_name:<15} | {p_name:<12} | {orig_kb:>10.1f} | {stored_kb:>11.1f} | {saved_pct:>7.1f}% | {comp_mb_s:>11.1f} | {decomp_mb_s:>14.1f}")

            results_table.append({
                "workload": w_name,
                "profile": p_name,
                "original_bytes": len(data),
                "stored_bytes": comp_res.stored_size,
                "saved_pct": round(saved_pct, 2),
                "compress_mb_s": round(comp_mb_s, 2),
                "restore_mb_s": round(decomp_mb_s, 2),
                "verified": True,
            })

    print("-" * 80)
    print("Benchmark complete. All chunks decompressed and SHA-256 verified successfully.\n")

    # Save summary artifact
    out_dir = Path("results")
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "compression_benchmark.json"
    out_file.write_text(json.dumps(results_table, indent=2), encoding="utf-8")
    print(f"Results recorded to {out_file}")
    return {"results": results_table}


if __name__ == "__main__":
    run_benchmark()

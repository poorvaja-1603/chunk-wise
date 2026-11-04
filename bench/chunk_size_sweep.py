"""Chunk size sweep benchmark (avg 4, 8, 16 KiB) on edited text and binary."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from datasets import insert_at, random_bytes, text_document

from chunkwise import ChunkParams, Engine

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results"
MiB = 1024 * 1024


def run_sweep() -> list[dict]:
    OUT.mkdir(exist_ok=True)
    base_text = text_document(8 * MiB, seed=2)
    paragraph = b"This paragraph was inserted to simulate an edit to the document. " * 20
    d3_uploads = [
        ("v1.txt", base_text),
        ("v2.txt", insert_at(base_text, len(base_text) // 2, paragraph)),
    ]

    base_binary = random_bytes(8 * MiB, seed=3)
    d4_uploads = [
        ("v1.img", base_binary),
        ("v2.img", insert_at(base_binary, len(base_binary) // 4, random_bytes(4096, seed=9))),
    ]

    datasets = [
        ("D3_edited_text", d3_uploads),
        ("D4_edited_binary", d4_uploads),
    ]

    sweep_configs = [
        (4, ChunkParams(min_size=1024, avg_size=4096, max_size=16384)),
        (8, ChunkParams(min_size=2048, avg_size=8192, max_size=32768)),
        (16, ChunkParams(min_size=4096, avg_size=16384, max_size=65536)),
    ]

    results = []
    # Estimated metadata: DynamoDB chunk ref item (~200 B) + manifest entry (~64 B) = 264 B per chunk
    METADATA_BYTES_PER_CHUNK = 264

    for ds_name, uploads in datasets:
        for avg_kib, params in sweep_configs:
            print(f"Running sweep on {ds_name} with avg {avg_kib} KiB ...", flush=True)
            home = Path(tempfile.mkdtemp(prefix="cw_sweep_"))
            try:
                with Engine(home, params) as engine:
                    logical = 0
                    total_chunks = 0
                    for filename, data in uploads:
                        logical += len(data)
                        res = engine.put_bytes(data, filename, "cdc")
                        total_chunks += res.chunks

                    stats = engine.stats()
                    metadata_overhead = stats.unique_chunks * METADATA_BYTES_PER_CHUNK
                    metadata_pct = (metadata_overhead / stats.physical_bytes) * 100 if stats.physical_bytes else 0.0

                    row = {
                        "dataset": ds_name,
                        "avg_chunk_size_kib": avg_kib,
                        "logical_bytes": logical,
                        "physical_bytes": stats.physical_bytes,
                        "unique_chunks": stats.unique_chunks,
                        "total_chunks": total_chunks,
                        "dedup_ratio": round(stats.dedup_ratio, 4),
                        "storage_saved_pct": round(stats.storage_saved_pct, 2),
                        "estimated_metadata_bytes": metadata_overhead,
                        "metadata_overhead_pct": round(metadata_pct, 3),
                    }
                    results.append(row)
            finally:
                shutil.rmtree(home, ignore_errors=True)

    (OUT / "chunk_size_sweep.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Wrote {OUT / 'chunk_size_sweep.json'}")

    # Generate figure
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4.5))

    kib_values = [4, 8, 16]
    for ds_name, color, marker in [("D3_edited_text", "#0f5f66", "o"), ("D4_edited_binary", "#c2593f", "s")]:
        subset = [r for r in results if r["dataset"] == ds_name]
        ratios = [r["dedup_ratio"] for r in subset]
        ax1.plot(kib_values, ratios, marker=marker, label=ds_name, color=color, linewidth=2)

        meta_pcts = [r["metadata_overhead_pct"] for r in subset]
        ax2.plot(kib_values, meta_pcts, marker=marker, label=ds_name, color=color, linewidth=2)

    ax1.set_xlabel("Target Average Chunk Size (KiB)")
    ax1.set_ylabel("Deduplication Ratio (Logical / Physical)")
    ax1.set_title("Dedup Ratio vs Chunk Size")
    ax1.set_xticks(kib_values)
    ax1.grid(True, linestyle="--", alpha=0.5)
    ax1.legend()

    ax2.set_xlabel("Target Average Chunk Size (KiB)")
    ax2.set_ylabel("Estimated Metadata Overhead (%)")
    ax2.set_title("Metadata Overhead vs Chunk Size")
    ax2.set_xticks(kib_values)
    ax2.grid(True, linestyle="--", alpha=0.5)
    ax2.legend()

    fig.suptitle("Figure 5. Chunk Size Trade-Off: Dedup Efficiency vs Metadata Overhead", fontsize=12)
    fig.tight_layout()
    chart_path = OUT / "figure_chunk_size_sweep.png"
    fig.savefig(chart_path, dpi=150)
    plt.close(fig)
    print(f"Wrote {chart_path}")
    return results


if __name__ == "__main__":
    run_sweep()


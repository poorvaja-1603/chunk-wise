"""Capped AWS upload warm-up followed by a seeded virtual load simulation."""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import struct
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .chunker import ChunkParams, iter_chunks
from .engine import Engine, PutResult, UploadPipeline
from .index import MemoryIndex
from .store import VirtualChunkStore

DEFAULT_REAL_BYTE_CAP = int(1.5 * 1024 * 1024 * 1024)
DEFAULT_REAL_CHUNK_CAP = 200_000
STAGES = (("1x", 1), ("10x", 10), ("100x", 100))


def _spool_chunks(
    source_name: str,
    spool_name: str,
    params: ChunkParams,
    chunker: str,
) -> tuple[str, int, int, float]:
    source = Path(source_name)
    started = time.perf_counter()
    chunks = size = 0
    with source.open("rb") as stream, Path(spool_name).open("wb") as spool:
        for data in iter_chunks(stream, chunker, params):
            spool.write(struct.pack(">I", len(data)))
            spool.write(data)
            chunks += 1
            size += len(data)
    return spool_name, chunks, size, time.perf_counter() - started


def _read_spool(path: str) -> Iterable[bytes]:
    with Path(path).open("rb") as spool:
        while header := spool.read(4):
            if len(header) != 4:
                raise OSError("truncated simulation spool")
            length = struct.unpack(">I", header)[0]
            data = spool.read(length)
            if len(data) != length:
                raise OSError("truncated simulation chunk")
            yield data


def _percentile(samples: Sequence[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 4)


class _MetricsWriter:
    def __init__(
        self,
        path: Path,
        on_row: Callable[[dict], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("w", encoding="utf-8", newline="\n")
        self.on_row = on_row

    def write(self, row: dict) -> None:
        line = json.dumps(row, separators=(",", ":"), sort_keys=True)
        self.stream.write(line + "\n")
        self.stream.flush()
        if self.on_row is not None:
            self.on_row(dict(row))
        prefix = "SIMULATED " if row["simulated"] else "REAL "
        print(prefix + line, flush=True)

    def close(self) -> None:
        self.stream.close()


class VirtualLoadGenerator:
    def __init__(
        self,
        params: ChunkParams,
        seed: int,
        duplicate_ratio: float,
        chunks_per_file: int,
    ) -> None:
        if not 0 <= duplicate_ratio <= 1:
            raise ValueError("duplicate ratio must be between 0 and 1")
        if chunks_per_file < 1:
            raise ValueError("chunks per file must be at least one")
        self.params = params
        self.rng = random.Random(seed)
        self.duplicate_ratio = duplicate_ratio
        self.chunks_per_file = chunks_per_file
        self._pool: list[tuple[str, int]] = []
        self._file_number = 0
        self._pool_limit = 4096

    def next_upload(self) -> tuple[str, list[tuple[str, int]]]:
        chunks: list[tuple[str, int]] = []
        for _ in range(self.chunks_per_file):
            if self._pool and self.rng.random() < self.duplicate_ratio:
                chunks.append(self.rng.choice(self._pool))
                continue
            length = max(
                self.params.min_size,
                min(
                    self.params.max_size,
                    round(self.rng.triangular(
                        self.params.min_size,
                        self.params.max_size,
                        self.params.avg_size,
                    )),
                ),
            )
            digest = f"{self.rng.getrandbits(256):064x}"
            item = (digest, length)
            chunks.append(item)
            if len(self._pool) < self._pool_limit:
                self._pool.append(item)
        name = f"virtual-{self._file_number:08d}.bin"
        self._file_number += 1
        return name, chunks


class HybridRunner:
    def __init__(
        self,
        home: Path,
        metrics_path: Path,
        params: ChunkParams | None = None,
        seed: int = 1,
        duplicate_ratio: float = 0.6,
        stage_seconds: int = 10,
        base_rps: float = 1,
        chunks_per_file: int = 8,
        cap_bytes: int = DEFAULT_REAL_BYTE_CAP,
        cap_chunks: int = DEFAULT_REAL_CHUNK_CAP,
        real_worker_count: int = 1,
        chunker: str = "cdc",
        simulated_store_latency_ms: float = 0.0,
        simulated_index_latency_ms: float = 0.1,
        realtime: bool = True,
        real_pipeline_factory: Callable[[], UploadPipeline] | None = None,
        metrics_callback: Callable[[dict], None] | None = None,
    ) -> None:
        if stage_seconds < 1 or base_rps <= 0 or real_worker_count < 1:
            raise ValueError("stage duration, request rate, and worker count must be positive")
        if cap_bytes < 0 or cap_chunks < 0:
            raise ValueError("real phase caps must not be negative")
        if chunker not in {"cdc", "fixed"}:
            raise ValueError("chunker must be cdc or fixed")
        if simulated_store_latency_ms < 0 or simulated_index_latency_ms < 0:
            raise ValueError("simulated latencies must not be negative")
        self.home = Path(home)
        self.metrics_path = Path(metrics_path)
        self.params = params or ChunkParams()
        self.seed = seed
        self.duplicate_ratio = duplicate_ratio
        self.stage_seconds = stage_seconds
        self.base_rps = base_rps
        self.chunks_per_file = chunks_per_file
        self.cap_bytes = cap_bytes
        self.cap_chunks = cap_chunks
        self.real_worker_count = real_worker_count
        self.chunker = chunker
        self.simulated_store_latency_ms = simulated_store_latency_ms
        self.simulated_index_latency_ms = simulated_index_latency_ms
        self.realtime = realtime
        self.real_pipeline_factory = real_pipeline_factory
        self.metrics_callback = metrics_callback

    def _real_pipeline(self) -> tuple[UploadPipeline, Engine | None]:
        if self.real_pipeline_factory is not None:
            return self.real_pipeline_factory(), None
        if os.environ.get("CHUNKWISE_BACKEND", "local").strip().lower() != "aws":
            raise ValueError(
                "real uploads require CHUNKWISE_BACKEND=aws; virtual mode makes no AWS calls"
            )
        engine = Engine(self.home, self.params)
        return engine.pipeline, engine

    def _write_real_metrics(
        self,
        writer: _MetricsWriter,
        events: list[dict],
    ) -> None:
        if not events:
            return
        buckets: dict[int, list[dict]] = {}
        for event in events:
            buckets.setdefault(event["second"], []).append(event)
        logical = physical = 0
        last_second = max(buckets)
        for second in range(last_second + 1):
            current = buckets.get(second, [])
            chunks = files = unique = duplicate = 0
            lookup: list[float] = []
            chunking_seconds = 0.0
            for event in current:
                result: PutResult = event["result"]
                chunks += result.chunks
                files += 1
                unique += result.new_chunks
                duplicate += result.chunks - result.new_chunks
                logical += result.size
                physical += result.new_bytes
                lookup.extend(event["lookup"])
                chunking_seconds += event["chunking_batch_seconds"]
            writer.write({
                "elapsed_second": second,
                "mode": "real",
                "stage": "real",
                "simulated": False,
                "label": "real",
                "worker_count": self.real_worker_count,
                "chunks_per_second": chunks,
                "files_per_second": files,
                "logical_bytes": logical,
                "physical_bytes": physical,
                "dedup_ratio": round(logical / physical, 4) if physical else 1.0,
                "unique_chunks": unique,
                "duplicate_chunks": duplicate,
                "index_lookup_latency_p50_ms": _percentile(lookup, 0.50),
                "index_lookup_latency_p95_ms": _percentile(lookup, 0.95),
                "index_lookup_latency_p99_ms": _percentile(lookup, 0.99),
                "chunking_chunks_per_second": (
                    round(sum(event["chunk_count"] for event in current) / chunking_seconds, 2)
                    if chunking_seconds
                    else 0.0
                ),
            })

    def _run_real(
        self,
        paths: Sequence[Path],
        writer: _MetricsWriter,
    ) -> tuple[bool, int, int, int]:
        if not paths or (self.cap_bytes == 0 or self.cap_chunks == 0):
            return bool(paths), 0, 0, 0
        byte_count = chunk_count = 0
        events: list[dict] = []
        started = time.monotonic()
        pipeline: UploadPipeline | None = None
        engine: Engine | None = None
        switched = False

        with tempfile.TemporaryDirectory(prefix="chunkwise-spool-") as directory:
            executor = (
                ProcessPoolExecutor(max_workers=self.real_worker_count)
                if self.real_worker_count > 1
                else None
            )
            try:
                for batch_start in range(0, len(paths), self.real_worker_count):
                    batch = paths[batch_start:batch_start + self.real_worker_count]
                    jobs = [
                        (
                            str(path),
                            str(Path(directory) / f"{batch_start + offset:08d}.spool"),
                            self.params,
                            self.chunker,
                        )
                        for offset, path in enumerate(batch)
                    ]
                    if executor is None:
                        chunking_started = time.perf_counter()
                        prepared = [_spool_chunks(*job) for job in jobs]
                    else:
                        chunking_started = time.perf_counter()
                        prepared = list(executor.map(_spool_chunks, *zip(*jobs)))
                    batch_chunking_seconds = time.perf_counter() - chunking_started
                    for offset, (path, spool) in enumerate(zip(batch, prepared)):
                        spool_name, file_chunks, file_size, _ = spool
                        if (
                            byte_count + file_size > self.cap_bytes
                            or chunk_count + file_chunks > self.cap_chunks
                        ):
                            switched = True
                            break
                        if pipeline is None:
                            pipeline, engine = self._real_pipeline()
                        result = pipeline.put_chunks(
                            _read_spool(spool_name),
                            path.name,
                            self.chunker,
                        )
                        finished = time.monotonic()
                        byte_count += result.new_bytes
                        chunk_count += result.new_chunks
                        events.append({
                            "second": int(finished - started),
                            "result": result,
                            "lookup": list(pipeline.lookup_latencies_ms),
                            "chunking_batch_seconds": (
                                batch_chunking_seconds if offset == 0 else 0.0
                            ),
                            "chunk_count": file_chunks,
                        })
                    if switched:
                        break
            finally:
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=True)
                if engine is not None:
                    engine.close()
        self._write_real_metrics(writer, events)
        all_lookups = [l for ev in events for l in ev.get("lookup", [])]
        if all_lookups:
            self.simulated_index_latency_ms = _percentile(all_lookups, 0.50)
        real_seconds = max((event["second"] for event in events), default=-1) + 1
        return switched or len(events) < len(paths), byte_count, chunk_count, real_seconds

    def _run_virtual(self, writer: _MetricsWriter, elapsed_start: int) -> int:
        index = MemoryIndex(self.simulated_index_latency_ms)
        store = VirtualChunkStore(self.simulated_store_latency_ms)
        pipeline = UploadPipeline(index, store, self.params)
        generator = VirtualLoadGenerator(
            self.params, self.seed, self.duplicate_ratio, self.chunks_per_file
        )
        elapsed_second = elapsed_start
        for stage, multiplier in STAGES:
            requests_per_second = max(1, round(self.base_rps * multiplier))
            for _ in range(self.stage_seconds):
                second_started = time.perf_counter()
                chunks = files = unique = duplicate = logical = written = 0
                lookup: list[float] = []
                for _ in range(requests_per_second):
                    name, generated_chunks = generator.next_upload()
                    result = pipeline.put_fingerprints(generated_chunks, name, "synthetic")
                    files += 1
                    chunks += result.chunks
                    unique += result.new_chunks
                    duplicate += result.chunks - result.new_chunks
                    logical += result.size
                    written += result.new_bytes
                    lookup.extend(pipeline.lookup_latencies_ms)
                cumulative_logical, cumulative_physical, _, _ = index.totals()
                writer.write({
                    "elapsed_second": elapsed_second,
                    "mode": "virtual",
                    "stage": stage,
                    "simulated": True,
                    "label": "simulated",
                    "worker_count": 0,
                    "chunks_per_second": chunks,
                    "files_per_second": files,
                    "logical_bytes": cumulative_logical,
                    "physical_bytes": cumulative_physical,
                    "dedup_ratio": (
                        round(cumulative_logical / cumulative_physical, 4)
                        if cumulative_physical
                        else 1.0
                    ),
                    "unique_chunks": unique,
                    "duplicate_chunks": duplicate,
                    "index_lookup_latency_p50_ms": _percentile(lookup, 0.50),
                    "index_lookup_latency_p95_ms": _percentile(lookup, 0.95),
                    "index_lookup_latency_p99_ms": _percentile(lookup, 0.99),
                    "simulated_store_latency_ms": round(
                        store.simulated_latency_ms * unique, 4
                    ),
                    "logical_bytes_this_second": logical,
                    "physical_bytes_this_second": written,
                    "duplicate_ratio_configured": self.duplicate_ratio,
                })
                elapsed_second += 1
                if self.realtime:
                    time.sleep(max(0.0, 1.0 - (time.perf_counter() - second_started)))
        return index.totals()[2]

    def run(self, real_files: Sequence[str | Path] = ()) -> dict:
        writer = _MetricsWriter(self.metrics_path, self.metrics_callback)
        try:
            paths = [Path(path) for path in real_files]
            switched, real_bytes, real_chunks, real_seconds = self._run_real(paths, writer)
            virtual_files = self._run_virtual(writer, real_seconds)
            return {
                "metrics_file": str(self.metrics_path),
                "real_bytes_written": real_bytes,
                "real_chunks_written": real_chunks,
                "switched_to_virtual": switched,
                "virtual": {
                    "label": "simulated",
                    "files": virtual_files,
                    "seed": self.seed,
                    "duplicate_ratio": self.duplicate_ratio,
                },
            }
        finally:
            writer.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chunkwise-simulate")
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("CHUNKWISE_HOME", ".chunkwise")))
    parser.add_argument("--metrics", type=Path, default=Path("simulation/metrics.jsonl"))
    parser.add_argument("--real-files", nargs="*", default=[])
    parser.add_argument("--real-worker-count", type=int, default=1)
    parser.add_argument("--cap-bytes", type=int, default=DEFAULT_REAL_BYTE_CAP)
    parser.add_argument("--cap-chunks", type=int, default=DEFAULT_REAL_CHUNK_CAP)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--duplicate-ratio", type=float, choices=(0.3, 0.6, 0.9), default=0.6)
    parser.add_argument("--stage-seconds", type=int, default=10)
    parser.add_argument("--base-rps", type=float, default=1)
    parser.add_argument("--chunks-per-file", type=int, default=8)
    parser.add_argument("--chunker", choices=("cdc", "fixed"), default="cdc")
    parser.add_argument("--simulated-store-latency-ms", type=float, default=0.0)
    parser.add_argument("--simulated-index-latency-ms", type=float, default=0.1)
    parser.add_argument("--no-realtime", action="store_true")
    arguments = parser.parse_args(argv)
    runner = HybridRunner(
        home=arguments.home,
        metrics_path=arguments.metrics,
        seed=arguments.seed,
        duplicate_ratio=arguments.duplicate_ratio,
        stage_seconds=arguments.stage_seconds,
        base_rps=arguments.base_rps,
        chunks_per_file=arguments.chunks_per_file,
        cap_bytes=arguments.cap_bytes,
        cap_chunks=arguments.cap_chunks,
        real_worker_count=arguments.real_worker_count,
        chunker=arguments.chunker,
        simulated_store_latency_ms=arguments.simulated_store_latency_ms,
        simulated_index_latency_ms=arguments.simulated_index_latency_ms,
        realtime=not arguments.no_realtime,
    )
    result = runner.run(arguments.real_files)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

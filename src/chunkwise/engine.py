"""Write, restore, verify, and account for deduplicated files."""
from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable

from .chunker import ChunkParams, iter_chunks
from .errors import (
    IdempotencyConflictError,
    IndexWriteConflictError,
    IntegrityError,
)
from .index import (
    ChunkIndex,
    DynamoIndex,
    FileRow,
    MemoryIndex,
    SqliteIndex,
)
from .store import ChunkStore, LocalChunkStore, S3ChunkStore, VirtualChunkStore


@dataclass(frozen=True)
class PutResult:
    file_id: str
    name: str
    size: int
    chunks: int
    new_chunks: int
    new_bytes: int
    chunker: str


@dataclass(frozen=True)
class Stats:
    logical_bytes: int
    physical_bytes: int
    dedup_ratio: float
    storage_saved_pct: float
    files: int
    unique_chunks: int


@dataclass(frozen=True)
class UploadMetrics:
    s3_gets: int = 0
    s3_puts: int = 0
    s3_heads: int = 0
    dynamo_reads: int = 0
    dynamo_writes: int = 0


class UploadPipeline:
    def __init__(
        self,
        index: ChunkIndex,
        store: ChunkStore,
        params: ChunkParams,
        max_workers: int = 8,
    ) -> None:
        self.index = index
        self.store = store
        self.params = params
        self.max_workers = max_workers
        self.uploader_id = uuid.uuid4().hex[:12]
        self.lookup_latencies_ms: list[float] = []
        self.last_metrics = UploadMetrics()

    def _snapshot_metrics(
        self,
        s3_before: tuple[int, int, int],
        dynamo_before: tuple[int, int],
    ) -> UploadMetrics:
        s3_gets = getattr(self.store, "s3_gets", 0) - s3_before[0]
        s3_puts = getattr(self.store, "s3_puts", 0) - s3_before[1]
        s3_heads = getattr(self.store, "s3_heads", 0) - s3_before[2]
        dynamo_reads = getattr(self.index, "dynamo_reads", 0) - dynamo_before[0]
        dynamo_writes = getattr(self.index, "dynamo_writes", 0) - dynamo_before[1]
        return UploadMetrics(
            s3_gets=max(0, s3_gets),
            s3_puts=max(0, s3_puts),
            s3_heads=max(0, s3_heads),
            dynamo_reads=max(0, dynamo_reads),
            dynamo_writes=max(0, dynamo_writes),
        )

    def put_fingerprints(
        self,
        fingerprints: Sequence[tuple[str, int]],
        name: str,
        chunker: str = "synthetic",
        idempotency_key: str | None = None,
        file_id: str | None = None,
    ) -> PutResult:
        if idempotency_key:
            existing = self.index.idempotent_upload(idempotency_key)
            if existing is not None:
                return PutResult(
                    existing.file_id,
                    existing.name,
                    existing.size,
                    len(self.index.manifest(existing.file_id)),
                    0,
                    0,
                    existing.chunker,
                )

        s3_before = (
            getattr(self.store, "s3_gets", 0),
            getattr(self.store, "s3_puts", 0),
            getattr(self.store, "s3_heads", 0),
        )
        dynamo_before = (
            getattr(self.index, "dynamo_reads", 0),
            getattr(self.index, "dynamo_writes", 0),
        )

        file_id = file_id or "f-" + uuid.uuid4().hex[:12]
        whole_hash = hashlib.sha256()
        manifest: list[tuple[int, str, int, int]] = []
        refs: dict[str, int] = {}
        sizes: dict[str, int] = {}
        offset = new_chunks = new_bytes = 0
        self.lookup_latencies_ms = []

        seen_hashes: set[str] = set()
        sequence = 0
        batch_size = 64

        for i in range(0, len(fingerprints), batch_size):
            batch = fingerprints[i:i + batch_size]
            unseen = [digest for digest, _ in batch if digest not in seen_hashes]

            committed_in_index: set[str] = set()
            if unseen:
                started = time.perf_counter()
                if hasattr(self.index, "has_chunks_batch"):
                    committed_in_index = self.index.has_chunks_batch(unseen)
                else:
                    committed_in_index = {d for d in unseen if self.index.has_chunk(d)}
                elapsed_ms = (time.perf_counter() - started) * 1000
                simulated_ms = getattr(self.index, "simulated_lookup_latency_ms", None)
                for _ in unseen:
                    self.lookup_latencies_ms.append(
                        elapsed_ms if simulated_ms is None else float(simulated_ms)
                    )

            for digest, length in batch:
                whole_hash.update(digest.encode("ascii"))
                manifest.append((sequence, digest, offset, length))
                refs[digest] = refs.get(digest, 0) + 1
                sizes[digest] = length
                offset += length
                sequence += 1

                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)

                if digest in committed_in_index:
                    continue

                claimed = (
                    self.index.claim_chunk(digest, length, self.uploader_id)
                    if hasattr(self.index, "claim_chunk")
                    else True
                )
                if claimed or not (hasattr(self.store, "exists") and self.store.exists(digest)):
                    written = self.store.put(digest, b"", size=length)
                    if written:
                        new_chunks += 1
                        new_bytes += length
                    if hasattr(self.index, "commit_chunk"):
                        self.index.commit_chunk(digest, length)

        row = FileRow(
            file_id=file_id,
            name=name,
            size=offset,
            sha256=whole_hash.hexdigest(),
            chunker=chunker,
            avg_size=self.params.avg_size,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.index.commit_file(row, manifest, refs, sizes, idempotency_key)
        self.last_metrics = self._snapshot_metrics(s3_before, dynamo_before)
        return PutResult(
            file_id, name, offset, len(manifest), new_chunks, new_bytes, chunker
        )

    def put_chunks(
        self,
        chunks: Iterable[bytes],
        name: str,
        chunker: str,
        idempotency_key: str | None = None,
        file_id: str | None = None,
    ) -> PutResult:
        s3_before = (
            getattr(self.store, "s3_gets", 0),
            getattr(self.store, "s3_puts", 0),
            getattr(self.store, "s3_heads", 0),
        )
        dynamo_before = (
            getattr(self.index, "dynamo_reads", 0),
            getattr(self.index, "dynamo_writes", 0),
        )

        file_id = file_id or "f-" + uuid.uuid4().hex[:12]
        whole_hash = hashlib.sha256()
        manifest: list[tuple[int, str, int, int]] = []
        refs: dict[str, int] = {}
        sizes: dict[str, int] = {}
        offset = new_chunks = new_bytes = 0
        self.lookup_latencies_ms = []

        seen_hashes: set[str] = set()
        pending_uploads: dict[str, bytes] = {}

        chunk_iterator = iter(chunks)
        batch_size = 64
        sequence = 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            while True:
                batch: list[bytes] = []
                for _ in range(batch_size):
                    try:
                        data = next(chunk_iterator)
                    except StopIteration:
                        break
                    if not data:
                        raise ValueError("chunk streams must not contain empty chunks")
                    batch.append(data)

                if not batch:
                    break

                batch_digests = [hashlib.sha256(data).hexdigest() for data in batch]
                unseen = [d for d in batch_digests if d not in seen_hashes]

                # Check index in batch if supported
                committed_in_index: set[str] = set()
                if unseen:
                    started = time.perf_counter()
                    if hasattr(self.index, "has_chunks_batch"):
                        committed_in_index = self.index.has_chunks_batch(unseen)
                    else:
                        committed_in_index = {d for d in unseen if self.index.has_chunk(d)}
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    simulated_ms = getattr(self.index, "simulated_lookup_latency_ms", None)
                    for _ in unseen:
                        self.lookup_latencies_ms.append(
                            elapsed_ms if simulated_ms is None else float(simulated_ms)
                        )

                # Process chunks in batch
                chunks_to_upload: list[tuple[str, bytes]] = []
                for data, digest in zip(batch, batch_digests):
                    whole_hash.update(data)
                    manifest.append((sequence, digest, offset, len(data)))
                    refs[digest] = refs.get(digest, 0) + 1
                    sizes[digest] = len(data)
                    offset += len(data)
                    sequence += 1

                    if digest in seen_hashes:
                        continue

                    seen_hashes.add(digest)

                    if digest in committed_in_index:
                        continue

                    claimed = (
                        self.index.claim_chunk(digest, len(data), self.uploader_id)
                        if hasattr(self.index, "claim_chunk")
                        else True
                    )
                    if claimed:
                        chunks_to_upload.append((digest, data))
                    else:
                        if hasattr(self.store, "exists") and not self.store.exists(digest):
                            chunks_to_upload.append((digest, data))

                # Upload new chunks concurrently
                if chunks_to_upload:
                    futures = [
                        (digest, len(data), executor.submit(self.store.put, digest, data))
                        for digest, data in chunks_to_upload
                    ]
                    for digest, length, future in futures:
                        written = future.result()
                        if written:
                            new_chunks += 1
                            new_bytes += length
                        if hasattr(self.index, "commit_chunk"):
                            self.index.commit_chunk(digest, length)

        row = FileRow(
            file_id=file_id,
            name=name,
            size=offset,
            sha256=whole_hash.hexdigest(),
            chunker=chunker,
            avg_size=self.params.avg_size,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.index.commit_file(row, manifest, refs, sizes, idempotency_key)
        self.last_metrics = self._snapshot_metrics(s3_before, dynamo_before)
        return PutResult(
            file_id, name, offset, len(manifest), new_chunks, new_bytes, chunker
        )


def _configured_backends(home: Path, fsync: bool) -> tuple[ChunkStore, ChunkIndex]:
    backend = os.environ.get("CHUNKWISE_BACKEND", "local").strip().lower()
    if backend == "local":
        return (
            LocalChunkStore(home / "chunks", fsync=fsync),
            SqliteIndex(home / "index.sqlite3"),
        )
    if backend == "aws":
        bucket = os.environ.get("CHUNKWISE_S3_BUCKET", "")
        table_name = os.environ.get("CHUNKWISE_DYNAMODB_TABLE", "")
        region = os.environ.get("CHUNKWISE_AWS_REGION") or None
        if not bucket or not table_name:
            raise ValueError(
                "AWS backend requires CHUNKWISE_S3_BUCKET and CHUNKWISE_DYNAMODB_TABLE"
            )
        return S3ChunkStore(bucket, region), DynamoIndex(table_name, region)
    if backend == "virtual":
        latency_ms = float(os.environ.get("CHUNKWISE_SIMULATED_STORE_LATENCY_MS", "0"))
        lookup_ms = float(os.environ.get("CHUNKWISE_SIMULATED_INDEX_LATENCY_MS", "0.1"))
        return VirtualChunkStore(latency_ms), MemoryIndex(lookup_ms)
    raise ValueError("CHUNKWISE_BACKEND must be local, aws, or virtual")


class _NullSink:
    def write(self, data: bytes) -> int:
        return len(data)


class Engine:
    def __init__(
        self,
        home: Path,
        params: ChunkParams | None = None,
        fsync: bool = False,
    ) -> None:
        self.home = Path(home)
        self.params = params or ChunkParams()
        if os.environ.get("CHUNKWISE_BACKEND", "local").strip().lower() != "virtual":
            self.home.mkdir(parents=True, exist_ok=True)
        self.store, self.index = _configured_backends(self.home, fsync)
        concurrency = int(os.environ.get("CHUNKWISE_UPLOAD_CONCURRENCY", "8"))
        self.pipeline = UploadPipeline(
            self.index, self.store, self.params, max_workers=concurrency
        )
        self.last_upload_metrics = UploadMetrics()

    def close(self) -> None:
        self.index.close()
        self.store.close()

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def put(self, stream: BinaryIO, name: str, chunker: str = "cdc") -> PutResult:
        started = time.perf_counter()
        result = self._put(stream, name, chunker)
        duration_ms = (time.perf_counter() - started) * 1000
        self.last_upload_metrics = self.pipeline.last_metrics
        from .observability import get_tracker, emit_log
        backend = os.environ.get("CHUNKWISE_BACKEND", "local").strip().lower()
        mode = "simulated" if backend == "virtual" else "real"
        get_tracker().record_upload(
            bytes_uploaded=result.size,
            chunks=result.chunks,
            new_chunks=result.new_chunks,
            duplicate_chunks=result.chunks - result.new_chunks,
            put_latency_ms=duration_ms,
            lookup_latencies=self.pipeline.lookup_latencies_ms,
            mode=mode,
            backend="aws" if backend == "aws" else "local",
            s3_requests=self.last_upload_metrics.s3_gets + self.last_upload_metrics.s3_puts + self.last_upload_metrics.s3_heads,
            dynamo_requests=self.last_upload_metrics.dynamo_reads + self.last_upload_metrics.dynamo_writes,
        )
        emit_log(
            "upload",
            file_id=result.file_id,
            name=result.name,
            size=result.size,
            chunks=result.chunks,
            new_chunks=result.new_chunks,
            new_bytes=result.new_bytes,
            duration_ms=round(duration_ms, 2),
        )
        return result

    def put_idempotent_bytes(
        self,
        data: bytes,
        name: str,
        idempotency_key: str,
        chunker: str = "cdc",
    ) -> PutResult:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("idempotency key must contain 1 to 200 characters")
        digest = hashlib.sha256(data).hexdigest()
        existing = self.index.idempotent_upload(idempotency_key)
        if existing is not None:
            return self._existing_idempotent_result(existing, digest)
        file_id = self.index.claim_idempotency(
            idempotency_key,
            digest,
            "f-" + uuid.uuid4().hex[:12],
        )
        try:
            result = self._put(
                io.BytesIO(data),
                name,
                chunker,
                idempotency_key,
                file_id,
            )
            self.last_upload_metrics = self.pipeline.last_metrics
            return result
        except (sqlite3.IntegrityError, IndexWriteConflictError):
            existing = self.index.idempotent_upload(idempotency_key)
            if existing is None:
                raise
            return self._existing_idempotent_result(existing, digest)

    def _existing_idempotent_result(
        self,
        existing: tuple[str, str],
        digest: str,
    ) -> PutResult:
        file_id, existing_digest = existing
        if digest != existing_digest:
            raise IdempotencyConflictError(
                "idempotency key was already used for different file content"
            )
        file_row = self.index.get_file(file_id)
        return PutResult(
            file_id=file_row.file_id,
            name=file_row.name,
            size=file_row.size,
            chunks=len(self.index.manifest(file_id)),
            new_chunks=0,
            new_bytes=0,
            chunker=file_row.chunker,
        )

    def _put(
        self,
        stream: BinaryIO,
        name: str,
        chunker: str = "cdc",
        idempotency_key: str | None = None,
        file_id: str | None = None,
    ) -> PutResult:
        return self.pipeline.put_chunks(
            iter_chunks(stream, chunker, self.params),
            name,
            chunker,
            idempotency_key,
            file_id,
        )

    def put_path(self, path: str | Path, chunker: str = "cdc") -> PutResult:
        source = Path(path)
        with source.open("rb") as stream:
            return self.put(stream, source.name, chunker)

    def put_bytes(self, data: bytes, name: str, chunker: str = "cdc") -> PutResult:
        return self.put(io.BytesIO(data), name, chunker)

    def get(self, file_id: str, out: BinaryIO) -> int:
        """Write a verified file to out; discard out if verification fails."""
        metadata = self.index.get_file(file_id)
        whole_hash = hashlib.sha256()
        total = 0
        for sequence, entry in enumerate(self.index.manifest(file_id)):
            if entry.seq != sequence or entry.byte_offset != total:
                raise IntegrityError(f"file {file_id} has an invalid manifest")
            data = self.store.get(entry.chunk_hash)
            if len(data) != entry.length or hashlib.sha256(data).hexdigest() != entry.chunk_hash:
                raise IntegrityError(
                    f"chunk {entry.chunk_hash} (seq {entry.seq}) failed verification"
                )
            written = out.write(data)
            if written != len(data):
                raise OSError("output stream did not accept the complete chunk")
            whole_hash.update(data)
            total += len(data)
        if total != metadata.size or whole_hash.hexdigest() != metadata.sha256:
            raise IntegrityError(f"file {file_id} failed whole-file verification")
        from .observability import emit_log
        emit_log("restore", file_id=file_id, name=metadata.name, size=total)
        return total

    def get_bytes(self, file_id: str) -> bytes:
        output = io.BytesIO()
        self.get(file_id, output)
        return output.getvalue()

    def verify(self, file_id: str) -> int:
        started = time.perf_counter()
        try:
            size = self.get(file_id, _NullSink())
            duration_ms = (time.perf_counter() - started) * 1000
            from .observability import emit_log
            emit_log("verify", file_id=file_id, size=size, verified=True, duration_ms=round(duration_ms, 2))
            return size
        except IntegrityError:
            from .observability import get_tracker, emit_log
            backend = os.environ.get("CHUNKWISE_BACKEND", "local").strip().lower()
            mode = "simulated" if backend == "virtual" else "real"
            get_tracker().record_error(is_integrity=True, mode=mode, backend="aws" if backend == "aws" else "local")
            duration_ms = (time.perf_counter() - started) * 1000
            emit_log("verify", file_id=file_id, verified=False, error="IntegrityError", duration_ms=round(duration_ms, 2))
            raise

    def stats(self) -> Stats:
        logical, physical, files, unique_chunks = self.index.totals()
        dedup_ratio = logical / physical if physical else 1.0
        storage_saved_pct = (1 - physical / logical) * 100 if logical else 0.0
        return Stats(
            logical, physical, dedup_ratio, storage_saved_pct, files, unique_chunks
        )

    def delete(self, file_id: str) -> dict:
        tombstoned = self.index.delete_file(file_id)
        from .observability import emit_log
        emit_log("delete", file_id=file_id, deleted=True, tombstoned_chunks=tombstoned)
        return {"file_id": file_id, "deleted": True, "tombstoned_chunks": tombstoned}

    def gc(self, grace_seconds: float = 86400.0) -> dict:
        expired = self.index.gc(grace_seconds=grace_seconds)
        reclaimed_bytes = 0
        for digest, size in expired:
            self.store.delete(digest)
            reclaimed_bytes += size
        from .observability import emit_log
        emit_log("gc", grace_seconds=grace_seconds, deleted_chunks=len(expired), reclaimed_bytes=reclaimed_bytes)
        return {
            "deleted_chunks": len(expired),
            "reclaimed_bytes": reclaimed_bytes,
        }

    def fsck(self, repair: bool = False, max_pending_age_seconds: float = 3600.0) -> dict:
        index_report = self.index.fsck(repair=repair, max_pending_age_seconds=max_pending_age_seconds)
        store_objects = self.store.list_objects()
        orphan_chunks = [d for d in store_objects if self.index.refcount(d) is None]
        drift_count = len(index_report["drift_chunks"])
        from .observability import get_tracker, emit_log
        backend = os.environ.get("CHUNKWISE_BACKEND", "local").strip().lower()
        mode = "simulated" if backend == "virtual" else "real"
        if drift_count > 0:
            get_tracker().record_fsck(drift_count, mode=mode, backend="aws" if backend == "aws" else "local")
        emit_log(
            "fsck",
            repair=repair,
            drift_chunks=drift_count,
            orphans=len(orphan_chunks),
            stale_pending_files=len(index_report["stale_pending_files"]),
            repaired=index_report["repaired"],
        )
        return {
            "ok": index_report["ok"] and len(orphan_chunks) == 0,
            "drift_chunks": index_report["drift_chunks"],
            "orphan_chunks": sorted(orphan_chunks),
            "stale_pending_files": index_report["stale_pending_files"],
            "repaired": index_report["repaired"],
        }

"""Fault injection test suite for ChunkWise.

Scenarios tested:
1. Crash after chunk PUT before index write
2. Crash after refs before file commit
3. Crash mid-manifest
4. S3 throttle/5xx with retry
5. DynamoDB conditional check failure
6. Corrupt chunk in store
7. Missing chunk in store
8. Concurrent duplicate uploads from N threads
9. Stale claim by a dead writer
"""
from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chunkwise import ChunkParams, Engine
from chunkwise.errors import (
    ChunkMissingError,
    IndexWriteConflictError,
    IntegrityError,
)
from chunkwise.index import FileRow, MemoryIndex, SqliteIndex
from chunkwise.store import LocalChunkStore


def test_scenario_1_crash_after_chunk_put(tmp_path):
    home = tmp_path / "fi_crash_put"
    with Engine(home) as engine:
        data = b"isolated chunk bytes"
        digest = hashlib.sha256(data).hexdigest()
        engine.store.put(digest, data)

        report = engine.fsck(repair=False)
        assert report["ok"] is False
        assert digest in report["orphan_chunks"]
        assert engine.index.has_chunk(digest) is False


def test_scenario_2_crash_after_refs_before_commit(tmp_path):
    home = tmp_path / "fi_crash_refs"
    with Engine(home) as engine:
        data = b"uncommitted file bytes"
        digest = hashlib.sha256(data).hexdigest()
        engine.store.put(digest, data)

        row = FileRow("f-crashed-refs", "crashed.bin", len(data), digest, "fixed", 8192, "2026-11-04T00:00:00Z")
        manifest = [(0, digest, 0, len(data))]

        # Manually simulate: write pending file, manifest, refs, then crash before commit
        with engine.index._tx() as c:
            c.execute(
                "INSERT INTO files(file_id, name, size, sha256, chunker, avg_size, created_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (row.file_id, row.name, row.size, row.sha256, row.chunker, row.avg_size, row.created_at),
            )
            c.execute(
                "INSERT INTO manifest(file_id, seq, chunk_hash, byte_offset, length) VALUES (?, ?, ?, ?, ?)",
                (row.file_id, 0, digest, 0, len(data)),
            )
            c.execute(
                "INSERT INTO chunks(hash, size, ref_count, status) VALUES (?, ?, 1, 'committed') "
                "ON CONFLICT(hash) DO UPDATE SET ref_count = ref_count + 1",
                (digest, len(data)),
            )

        # File is pending so it cannot be read
        with pytest.raises(Exception):
            engine.get_bytes(row.file_id)

        # fsck detects the drift (chunk has 1 ref recorded, but 0 committed files reference it)
        report = engine.fsck(repair=False)
        assert report["ok"] is False
        assert digest in report["drift_chunks"]
        assert report["drift_chunks"][digest] == (1, 0)

        # fsck repair fixes it
        engine.fsck(repair=True)
        clean = engine.fsck(repair=False)
        assert clean["ok"] is True
        assert engine.index.refcount(digest) == 0


def test_scenario_3_crash_mid_manifest(tmp_path):
    home = tmp_path / "fi_mid_manifest"
    with Engine(home) as engine:
        # If manifest was only half-written, file status is pending and get raises
        row = FileRow("f-half", "half.bin", 200, "sha-half", "fixed", 8192, "now")
        with engine.index._tx() as c:
            c.execute(
                "INSERT INTO files(file_id, name, size, sha256, chunker, avg_size, created_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (row.file_id, row.name, row.size, row.sha256, row.chunker, row.avg_size, row.created_at),
            )
            c.execute(
                "INSERT INTO manifest(file_id, seq, chunk_hash, byte_offset, length) VALUES (?, 0, 'h1', 0, 100)",
                (row.file_id,),
            )

        with pytest.raises(Exception):
            engine.get_bytes("f-half")


def test_scenario_4_s3_throttle_retry(tmp_path):
    home = tmp_path / "fi_throttle"
    call_attempts = {"count": 0}

    class FlakyStore(LocalChunkStore):
        def put(self, digest, data):
            call_attempts["count"] += 1
            if call_attempts["count"] < 3:
                # Simulate 500 error / SlowDown
                raise OSError("503 SlowDown / Service Unavailable")
            return super().put(digest, data)

    store = FlakyStore(home / "chunks")
    index = SqliteIndex(home / "index.sqlite3")
    try:
        data = b"retryable upload bytes"
        digest = hashlib.sha256(data).hexdigest()

        # Custom retry loop around store.put
        retries = 5
        stored = False
        for attempt in range(retries):
            try:
                stored = store.put(digest, data)
                break
            except OSError:
                time.sleep(0.01)

        assert stored is True
        assert call_attempts["count"] == 3
    finally:
        index.close()


def test_scenario_5_dynamodb_conditional_failure():
    index = MemoryIndex()
    row = FileRow("f-conflict", "f.bin", 10, "sha-conflict", "fixed", 8192, "now")
    manifest = [(0, "c1", 0, 10)]
    index.commit_file(row, manifest, {"c1": 1}, {"c1": 10})

    # Second commit with same file_id but different sha must raise IndexWriteConflictError
    row2 = FileRow("f-conflict", "f.bin", 10, "sha-DIFFERENT", "fixed", 8192, "now")
    with pytest.raises(IndexWriteConflictError):
        index.commit_file(row2, manifest, {"c1": 1}, {"c1": 10})


def test_scenario_6_corrupt_chunk_in_store(tmp_path):
    home = tmp_path / "fi_corrupt"
    data = b"chunk to be corrupted"
    with Engine(home) as engine:
        result = engine.put_bytes(data, "corrupt.bin")
        manifest = engine.index.manifest(result.file_id)
        path = engine.store.path_for(manifest[0].chunk_hash)

        # Corrupt one byte
        raw = bytearray(path.read_bytes())
        raw[0] ^= 0xFF
        path.write_bytes(bytes(raw))

        with pytest.raises(IntegrityError):
            engine.get_bytes(result.file_id)

        with pytest.raises(IntegrityError):
            engine.verify(result.file_id)


def test_scenario_7_missing_chunk_in_store(tmp_path):
    home = tmp_path / "fi_missing"
    data = b"chunk to be deleted"
    with Engine(home) as engine:
        result = engine.put_bytes(data, "missing.bin")
        manifest = engine.index.manifest(result.file_id)
        path = engine.store.path_for(manifest[0].chunk_hash)
        path.unlink()

        with pytest.raises(ChunkMissingError):
            engine.get_bytes(result.file_id)


def test_scenario_8_concurrent_duplicate_uploads(tmp_path):
    home = tmp_path / "fi_concurrent"
    data = b"concurrent duplicate upload payload " * 500

    def upload_worker(worker_id: int):
        with Engine(home) as engine:
            res = engine.put_bytes(data, f"worker_{worker_id}.bin")
            restored = engine.get_bytes(res.file_id)
            assert restored == data
            return res.file_id

    with ThreadPoolExecutor(max_workers=4) as pool:
        file_ids = list(pool.map(upload_worker, range(4)))

    assert len(file_ids) == 4
    with Engine(home) as engine:
        stats = engine.stats()
        # Exactly one copy physically stored
        assert stats.files == 4
        assert stats.dedup_ratio == pytest.approx(4.0, rel=0.01)


def test_scenario_9_stale_claim_dead_writer(tmp_path):
    index = SqliteIndex(tmp_path / "fi_stale.sqlite3")
    try:
        digest = hashlib.sha256(b"stale chunk").hexdigest()
        size = 11

        # Dead writer claims chunk with 0.1s lease
        assert index.claim_chunk(digest, size, "dead-writer", lease_seconds=0.05) is True
        time.sleep(0.08)

        # New writer reclaims expired lease
        assert index.claim_chunk(digest, size, "new-writer", lease_seconds=60.0) is True
        index.commit_chunk(digest, size)
        assert index.has_chunk(digest) is True
    finally:
        index.close()

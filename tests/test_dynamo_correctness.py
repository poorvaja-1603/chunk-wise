"""Tests for commit ordering, chunk lease states, and resumable uploads."""
import hashlib
import time
from pathlib import Path

import pytest

from chunkwise import ChunkParams, Engine
from chunkwise.errors import FileNotFoundInIndexError
from chunkwise.index import FileRow, MemoryIndex, SqliteIndex
from chunkwise.store import LocalChunkStore


def test_pending_file_is_invisible_until_committed(tmp_path):
    index = SqliteIndex(tmp_path / "test_index.sqlite3")
    try:
        data = b"content for pending test"
        digest = hashlib.sha256(data).hexdigest()
        row = FileRow("f-pending-1", "pending.bin", len(data), digest, "fixed", 8192, "2026-11-04T00:00:00Z")
        manifest = [(0, digest, 0, len(data))]

        # Manually insert as pending
        with index._tx() as c:
            c.execute(
                "INSERT INTO files(file_id, name, size, sha256, chunker, avg_size, created_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (row.file_id, row.name, row.size, row.sha256, row.chunker, row.avg_size, row.created_at),
            )
            c.execute(
                "INSERT INTO upload_idempotency(idempotency_key, file_id, sha256, status) "
                "VALUES ('key-1', ?, ?, 'pending')",
                (row.file_id, row.sha256),
            )

        # Pending file should not be visible in get_file, list_files, totals, or idempotent_upload
        with pytest.raises(FileNotFoundInIndexError):
            index.get_file(row.file_id)

        assert len(index.list_files()) == 0
        assert index.idempotent_upload("key-1") is None
        logical, physical, files, chunks = index.totals()
        assert files == 0

        # Now commit the file
        index.commit_file(row, manifest, {digest: 1}, {digest: len(data)}, "key-1")

        # Now it is visible
        committed_row = index.get_file(row.file_id)
        assert committed_row.file_id == row.file_id
        assert len(index.list_files()) == 1
        assert index.idempotent_upload("key-1") == (row.file_id, row.sha256)
        logical, physical, files, chunks = index.totals()
        assert files == 1
    finally:
        index.close()


def test_memory_index_pending_file_isolation():
    index = MemoryIndex()
    data = b"mem content"
    digest = hashlib.sha256(data).hexdigest()
    row = FileRow("f-mem-1", "mem.bin", len(data), digest, "fixed", 8192, "2026-11-04T00:00:00Z")
    manifest = [(0, digest, 0, len(data))]

    # Directly set to pending
    index.files[row.file_id] = (row, "pending")
    index.idempotency["mem-key"] = {"file_id": row.file_id, "sha256": digest, "status": "pending"}

    with pytest.raises(FileNotFoundInIndexError):
        index.get_file(row.file_id)

    assert len(index.list_files()) == 0
    assert index.idempotent_upload("mem-key") is None

    index.commit_file(row, manifest, {digest: 1}, {digest: len(data)}, "mem-key")
    assert index.get_file(row.file_id).file_id == row.file_id
    assert len(index.list_files()) == 1
    assert index.idempotent_upload("mem-key") == (row.file_id, digest)


def test_concurrent_chunk_claims_and_stale_reclaim(tmp_path):
    indexes = [SqliteIndex(tmp_path / "lease_index.sqlite3"), MemoryIndex()]
    for index in indexes:
        try:
            digest = hashlib.sha256(b"lease data").hexdigest()
            size = 10

            # First uploader claims
            assert index.claim_chunk(digest, size, "uploader-A", lease_seconds=1.0) is True

            # Second uploader cannot claim while lease is active
            assert index.claim_chunk(digest, size, "uploader-B", lease_seconds=1.0) is False

            # Same uploader re-claiming succeeds
            assert index.claim_chunk(digest, size, "uploader-A", lease_seconds=1.0) is True

            # Stale lease reclaim: simulate expiry
            if isinstance(index, SqliteIndex):
                with index._tx() as c:
                    c.execute("UPDATE chunks SET lease_expires = ? WHERE hash = ?", (time.time() - 10, digest))
            else:
                index.chunks[digest]["lease_expires"] = time.time() - 10

            # Uploader B can now reclaim
            assert index.claim_chunk(digest, size, "uploader-B", lease_seconds=10.0) is True

            # Once committed, neither can claim (already committed)
            index.commit_chunk(digest, size)
            assert index.claim_chunk(digest, size, "uploader-C", lease_seconds=10.0) is False
        finally:
            index.close()


def test_resumable_upload_via_idempotency_key(tmp_path):
    home = tmp_path / "cw_home"
    data = b"resumable payload with multiple chunks " * 1000

    # First session: simulate crash after chunks are partially stored
    with Engine(home) as engine:
        digest = hashlib.sha256(data).hexdigest()
        file_id = engine.index.claim_idempotency("resume-key-1", digest, "f-resume-test")
        # Store all chunks into store, but do not call commit_file yet (simulating crash before commit)
        for chunk in engine.pipeline.params.window and [data[:4096], data[4096:]]:
            h = hashlib.sha256(chunk).hexdigest()
            engine.store.put(h, chunk)

    # Second session: retry using put_idempotent_bytes
    with Engine(home) as engine:
        result = engine.put_idempotent_bytes(data, "resumed.bin", "resume-key-1")
        assert result.file_id == "f-resume-test"
        # Since chunks were already present in the store, new_bytes is minimal/zero
        assert engine.get_bytes(result.file_id) == data
        assert engine.verify(result.file_id) == len(data)


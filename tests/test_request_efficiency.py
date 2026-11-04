"""Tests for AWS request efficiency: batch index checks, threadpool uploads, and metrics."""
import hashlib
from pathlib import Path

from chunkwise import ChunkParams, Engine
from chunkwise.engine import UploadPipeline
from chunkwise.index import MemoryIndex, SqliteIndex
from chunkwise.store import LocalChunkStore, VirtualChunkStore


def test_has_chunks_batch_across_indexes(tmp_path):
    indexes = [SqliteIndex(tmp_path / "eff_index.sqlite3"), MemoryIndex()]
    for index in indexes:
        try:
            d1 = hashlib.sha256(b"chunk1").hexdigest()
            d2 = hashlib.sha256(b"chunk2").hexdigest()
            d3 = hashlib.sha256(b"chunk3").hexdigest()

            assert index.has_chunks_batch([d1, d2, d3]) == set()

            index.commit_chunk(d1, 6)
            index.commit_chunk(d3, 6)

            found = index.has_chunks_batch([d1, d2, d3])
            assert found == {d1, d3}
        finally:
            index.close()


def test_index_first_dedup_skips_store_put():
    store_puts = []

    class MockStore:
        def __init__(self):
            self.s3_gets = 0
            self.s3_puts = 0
            self.s3_heads = 0

        def put(self, digest, data):
            store_puts.append(digest)
            return True

        def get(self, digest):
            return b"data"

        def exists(self, digest):
            return False

        def close(self):
            pass

    index = MemoryIndex()
    d1 = hashlib.sha256(b"chunk1").hexdigest()
    # Chunk 1 is already committed in index
    index.commit_chunk(d1, 6)

    pipeline = UploadPipeline(index, MockStore(), ChunkParams())
    result = pipeline.put_chunks([b"chunk1", b"chunk2"], "file.bin", "cdc")

    d2 = hashlib.sha256(b"chunk2").hexdigest()
    # d1 was in index so store.put was skipped for d1; d2 was put
    assert d1 not in store_puts
    assert d2 in store_puts
    assert result.chunks == 2
    assert result.new_chunks == 1


def test_intra_file_duplicates_only_uploaded_once():
    store_puts = []

    class MockStore:
        def __init__(self):
            self.s3_gets = 0
            self.s3_puts = 0
            self.s3_heads = 0

        def put(self, digest, data):
            store_puts.append(digest)
            return True

        def get(self, digest):
            return b"dup"

        def exists(self, digest):
            return False

        def close(self):
            pass

    index = MemoryIndex()
    pipeline = UploadPipeline(index, MockStore(), ChunkParams())
    result = pipeline.put_chunks([b"dup", b"dup", b"dup"], "dups.bin", "fixed")

    assert len(store_puts) == 1
    assert result.chunks == 3
    assert result.new_chunks == 1


def test_threadpool_upload_preserves_manifest_order_and_bytes(tmp_path):
    home = tmp_path / "cw_threads"
    data = b"abcdefghijklmnopqrstuvwxyz0123456789" * 5000
    with Engine(home) as engine:
        result = engine.put_bytes(data, "threaded.bin")
        assert engine.get_bytes(result.file_id) == data
        assert engine.verify(result.file_id) == len(data)

        # Check manifest order
        manifest = engine.index.manifest(result.file_id)
        current_offset = 0
        for i, entry in enumerate(manifest):
            assert entry.seq == i
            assert entry.byte_offset == current_offset
            current_offset += entry.length
        assert current_offset == len(data)

        # Check metrics were populated
        metrics = engine.last_upload_metrics
        assert metrics.dynamo_writes > 0


def test_upload_metrics_tracking(tmp_path):
    home = tmp_path / "cw_metrics"
    with Engine(home) as engine:
        result = engine.put_bytes(b"some content to test metrics", "m.bin")
        metrics = engine.last_upload_metrics
        assert hasattr(metrics, "s3_puts")
        assert hasattr(metrics, "dynamo_writes")
        assert metrics.dynamo_writes >= 1


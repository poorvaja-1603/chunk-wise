import hashlib

import pytest

from chunkwise.chunker import ChunkParams
from chunkwise.engine import UploadPipeline
from chunkwise.errors import ChunkMissingError
from chunkwise.index import FileRow, MemoryIndex, SqliteIndex
from chunkwise.store import LocalChunkStore, S3ChunkStore, VirtualChunkStore


def test_virtual_store_counts_unique_bytes_and_never_writes_to_disk():
    store = VirtualChunkStore(simulated_latency_ms=7.5)
    data = b"virtual chunk"
    digest = hashlib.sha256(data).hexdigest()

    assert store.put(digest, data) is True
    assert store.put(digest, data) is False
    assert store.simulated_bytes_written == len(data)
    assert store.simulated_chunks_written == 1
    assert store.simulated_latency_ms == 7.5
    with pytest.raises(ChunkMissingError):
        store.get(digest)


def test_local_chunk_store_verifies_and_reuses_chunks(tmp_path):
    store = LocalChunkStore(tmp_path / "chunks")
    data = b"local chunk"
    digest = hashlib.sha256(data).hexdigest()

    assert store.put(digest, data) is True
    assert store.put(digest, data) is False
    assert store.get(digest) == data
    with pytest.raises(ValueError):
        store.path_for("../invalid")


def test_pipeline_stores_bytes_before_committing_any_reference():
    calls = []

    class Store:
        def put(self, digest, data):
            calls.append("store")
            return True

        def get(self, digest):
            raise AssertionError

        def close(self):
            return None

    class Index:
        def has_chunk(self, digest):
            calls.append("lookup")
            return False

        def commit_file(self, row, manifest, refs, sizes, idempotency_key=None):
            assert calls[-1] == "store"
            calls.append("commit")

    pipeline = UploadPipeline(Index(), Store(), ChunkParams())
    pipeline.put_chunks([b"chunk-data"], "file", "synthetic")

    assert calls == ["lookup", "store", "commit"]


def test_sqlite_and_memory_indexes_share_chunk_index_contract(tmp_path):
    digest = hashlib.sha256(b"data").hexdigest()
    row = FileRow("f-one", "one", 4, hashlib.sha256(b"data").hexdigest(), "fixed", 8192, "now")
    manifest = [(0, digest, 0, 4)]
    indexes = [SqliteIndex(tmp_path / "index.sqlite3"), MemoryIndex()]
    try:
        for index in indexes:
            assert index.has_chunk(digest) is False
            index.commit_file(row, manifest, {digest: 1}, {digest: 4})
            assert index.has_chunk(digest) is True
            assert index.refcount(digest) == 1
            assert index.totals() == (4, 4, 1, 1)
            assert index.manifest(row.file_id)[0].chunk_hash == digest
    finally:
        for index in indexes:
            index.close()


def test_s3_store_uses_content_addressed_key_and_skips_existing_object():
    data = b"s3 content"
    digest = hashlib.sha256(data).hexdigest()

    class FakeS3:
        def __init__(self):
            self.objects = {}
            self.put_calls = []

        def head_object(self, Bucket, Key):
            if Key not in self.objects:
                error = RuntimeError("missing")
                error.response = {
                    "Error": {"Code": "404"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                }
                raise error
            body, metadata = self.objects[Key]
            return {"ContentLength": len(body), "Metadata": metadata}

        def put_object(self, **kwargs):
            self.put_calls.append(kwargs)
            self.objects[kwargs["Key"]] = (kwargs["Body"], kwargs["Metadata"])

    store = S3ChunkStore.__new__(S3ChunkStore)
    store.bucket = "test-bucket"
    store.client = FakeS3()

    assert store.put(digest, data) is True
    assert store.put(digest, data) is False
    assert store.client.put_calls[0]["Key"] == f"objects/{digest[:2]}/{digest[2:]}"
    assert store.client.put_calls[0]["IfNoneMatch"] == "*"
    assert len(store.client.put_calls) == 1

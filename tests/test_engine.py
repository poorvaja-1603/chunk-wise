import random
import sqlite3

import pytest

from chunkwise import Engine
from chunkwise.errors import ChunkMissingError, FileNotFoundInIndexError, IntegrityError
from chunkwise.index import FileRow, Index


@pytest.fixture
def eng(tmp_path):
    engine = Engine(tmp_path / "home")
    yield engine
    engine.close()


def _rand(size: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(size)


def test_roundtrip_byte_identical(eng):
    data = _rand(3_000_000, 10)
    result = eng.put_bytes(data, "a.bin")
    assert eng.get_bytes(result.file_id) == data


def test_empty_file_roundtrip(eng):
    result = eng.put_bytes(b"", "empty")
    assert result.chunks == 0
    assert eng.get_bytes(result.file_id) == b""


def test_identical_upload_twice_stores_no_new_chunk_bytes(eng):
    data = _rand(2_000_000, 11)
    first = eng.put_bytes(data, "a")
    before = eng.stats()
    second = eng.put_bytes(data, "b")
    after = eng.stats()

    assert first.new_bytes > 0
    assert second.new_bytes == 0
    assert after.physical_bytes == before.physical_bytes
    assert after.logical_bytes == 2 * before.logical_bytes
    assert after.dedup_ratio == pytest.approx(2.0)
    first_entries = eng.index.manifest(first.file_id)
    for entry in eng.index.manifest(second.file_id):
        expected = sum(item.chunk_hash == entry.chunk_hash for item in first_entries) * 2
        assert eng.index.refcount(entry.chunk_hash) == expected


def test_insertion_locality_cdc_vs_fixed(eng):
    base = _rand(1_000_000, 12)
    edited = b"Z" + base

    def shared_fraction(chunker: str) -> float:
        first = eng.put_bytes(base, "base", chunker)
        second = eng.put_bytes(edited, "edited", chunker)
        first_hashes = {entry.chunk_hash for entry in eng.index.manifest(first.file_id)}
        entries = eng.index.manifest(second.file_id)
        return sum(entry.chunk_hash in first_hashes for entry in entries) / len(entries)

    assert shared_fraction("cdc") >= 0.9
    assert shared_fraction("fixed") < 0.05


def test_tampered_chunk_raises_integrity_error(eng):
    result = eng.put_bytes(_rand(500_000, 13), "t")
    entry = eng.index.manifest(result.file_id)[3]
    path = eng.store.path_for(entry.chunk_hash)
    raw = bytearray(path.read_bytes())
    raw[0] ^= 0xFF
    path.write_bytes(raw)

    with pytest.raises(IntegrityError):
        eng.get_bytes(result.file_id)


def test_missing_chunk_raises(eng):
    result = eng.put_bytes(_rand(500_000, 14), "m")
    entry = eng.index.manifest(result.file_id)[0]
    eng.store.path_for(entry.chunk_hash).unlink()

    with pytest.raises(ChunkMissingError):
        eng.get_bytes(result.file_id)


def test_unknown_file_id(eng):
    with pytest.raises(FileNotFoundInIndexError):
        eng.get_bytes("f-does-not-exist")


def test_verify_returns_length(eng):
    data = _rand(123_456, 15)
    result = eng.put_bytes(data, "v")
    assert eng.verify(result.file_id) == len(data)


def test_persists_across_reopen(tmp_path):
    home = tmp_path / "home"
    payload = b"hello chunkwise\n" * 1000
    first = Engine(home)
    result = first.put_bytes(payload, "h.txt")
    first.close()

    second = Engine(home)
    try:
        assert second.get_bytes(result.file_id) == payload
    finally:
        second.close()


def test_commit_file_rolls_back_refcounts_and_rows_on_failure(tmp_path):
    index = Index(tmp_path / "index.sqlite3")
    first_row = FileRow("f-same", "first", 1, "sha", "fixed", 8192, "now")
    first_manifest = [(0, "a" * 64, 0, 1)]
    index.commit_file(first_row, first_manifest, {"a" * 64: 1}, {"a" * 64: 1})
    committed_manifest = index.manifest("f-same")
    duplicate_row = FileRow("f-same", "duplicate", 2, "sha", "fixed", 8192, "now")

    try:
        with pytest.raises(sqlite3.IntegrityError):
            index.commit_file(
                duplicate_row,
                [(0, "a" * 64, 0, 1), (1, "b" * 64, 1, 1)],
                {"a" * 64: 1, "b" * 64: 1},
                {"a" * 64: 1, "b" * 64: 1},
            )
        assert index.refcount("a" * 64) == 1
        assert index.refcount("b" * 64) is None
        assert index.totals() == (1, 1, 1, 1)
        assert index.manifest("f-same") == committed_manifest
    finally:
        index.close()
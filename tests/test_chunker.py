import hashlib
import io
import random

import pytest

from chunkwise.chunker import BASE, MOD, ChunkParams, RollingFingerprint, cdc_chunks, fixed_chunks

P = ChunkParams()


def _naive_fp(window_bytes: bytes) -> int:
    fp = 0
    for byte in window_bytes:
        fp = (fp * BASE + byte) % MOD
    return fp


def _digests(chunks) -> list[str]:
    return [hashlib.sha256(chunk).hexdigest() for chunk in chunks]


def test_rolling_matches_naive_recompute():
    data = random.Random(0).randbytes(2000)
    rolling = RollingFingerprint(P.window)

    for index, byte in enumerate(data):
        fingerprint = rolling.push(byte)
        start = max(0, index + 1 - P.window)
        assert fingerprint == _naive_fp(data[start:index + 1])


def test_cdc_roundtrip_and_size_bounds():
    data = random.Random(1).randbytes(1_000_000)
    chunks = list(cdc_chunks(io.BytesIO(data), P))

    assert b"".join(chunks) == data
    assert all(P.min_size <= len(chunk) <= P.max_size for chunk in chunks[:-1])
    assert not chunks or len(chunks[-1]) <= P.max_size


def test_cdc_is_deterministic():
    data = random.Random(2).randbytes(300_000)
    first = _digests(cdc_chunks(io.BytesIO(data), P))
    second = _digests(cdc_chunks(io.BytesIO(data), P))

    assert first == second


def test_cdc_independent_of_read_size():
    data = random.Random(3).randbytes(200_000)
    whole = [len(chunk) for chunk in cdc_chunks(io.BytesIO(data), P, read_size=1 << 20)]
    small = [len(chunk) for chunk in cdc_chunks(io.BytesIO(data), P, read_size=777)]

    assert whole == small


def test_insertion_keeps_cdc_boundaries():
    data = random.Random(4).randbytes(1_000_000)
    before = set(_digests(cdc_chunks(io.BytesIO(data), P)))
    after = _digests(cdc_chunks(io.BytesIO(b"X" + data), P))
    shared = sum(digest in before for digest in after) / len(after)

    assert shared >= 0.9, f"only {shared:.2%} of chunks re-synchronized"


def test_insertion_breaks_fixed_chunks():
    data = random.Random(4).randbytes(1_000_000)
    before = set(_digests(fixed_chunks(io.BytesIO(data), P)))
    after = _digests(fixed_chunks(io.BytesIO(b"X" + data), P))
    shared = sum(digest in before for digest in after) / len(after)

    assert shared < 0.05


def test_fixed_roundtrip():
    data = random.Random(5).randbytes(100_001)

    assert b"".join(fixed_chunks(io.BytesIO(data), P)) == data


def test_params_validation():
    with pytest.raises(ValueError):
        ChunkParams(min_size=10, avg_size=8, max_size=64)
    with pytest.raises(ValueError):
        ChunkParams(avg_size=3000)

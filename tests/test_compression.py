"""Tests for the compression module (Poorvaja -- Member A)."""
from __future__ import annotations

import hashlib
import zlib

import pytest

from chunkwise.compression import (
    CODEC_NONE,
    CODEC_ZLIB,
    CompressionResult,
    compress_chunk,
    decompress_chunk,
    describe_result,
    PROFILES,
)
from chunkwise.errors import IntegrityError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_compressible(size: int = 16_384) -> bytes:
    """Return highly compressible bytes (repeated pattern)."""
    return b"ChunkWise test pattern repeated. " * (size // 32 + 1)


def make_incompressible(size: int = 4_096) -> bytes:
    """Return pseudo-random bytes that compress poorly."""
    import os
    return os.urandom(size)


# ---------------------------------------------------------------------------
# INVARIANT: SHA-256 must be computed on the raw bytes, never the compressed
# ---------------------------------------------------------------------------
def test_hash_invariant_balanced():
    raw = make_compressible(32_768)
    raw_hash = hashlib.sha256(raw).hexdigest()
    result = compress_chunk(raw, profile="balanced")
    # If we got compressed bytes, they should NOT match the raw hash
    if result.was_compressed:
        assert hashlib.sha256(result.data).hexdigest() != raw_hash
    # Decompressing stored bytes must yield data whose hash matches raw_hash
    recovered = decompress_chunk(result.data, result.codec, expected_hash=raw_hash)
    assert recovered == raw
    assert hashlib.sha256(recovered).hexdigest() == raw_hash


# ---------------------------------------------------------------------------
# Policy profiles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("profile", list(PROFILES.keys()))
def test_all_profiles_round_trip(profile):
    """Every profile must produce bytes that round-trip correctly."""
    raw = make_compressible(16_000)
    result = compress_chunk(raw, profile=profile)
    recovered = decompress_chunk(result.data, result.codec)
    assert recovered == raw


def test_balanced_profile_compresses_text():
    raw = make_compressible(32_768)
    result = compress_chunk(raw, profile="balanced")
    assert result.codec == CODEC_ZLIB
    assert result.was_compressed
    assert result.saving_ratio >= 0.05
    assert result.stored_size < result.original_size


def test_space_saver_has_low_threshold():
    # A 3% saving should still be retained under space_saver (threshold 2%)
    raw = make_compressible(10_000)
    result = compress_chunk(raw, profile="space_saver")
    assert result.was_compressed  # highly compressible data
    assert result.level == 9


def test_fast_restore_requires_10pct_saving():
    # Incompressible data should NOT be compressed under fast_restore
    raw = make_incompressible(8_192)
    result = compress_chunk(raw, profile="fast_restore")
    # Incompressible data may fail the 10% threshold
    if result.saving_ratio < 0.10:
        assert not result.was_compressed
        assert result.codec == CODEC_NONE


def test_disabled_profile_never_compresses():
    raw = make_compressible(32_768)
    result = compress_chunk(raw, profile="disabled")
    assert not result.was_compressed
    assert result.codec == CODEC_NONE
    assert result.data is raw  # exact same object, no copy


# ---------------------------------------------------------------------------
# Threshold override
# ---------------------------------------------------------------------------
def test_min_saving_override_prevents_compression():
    raw = make_compressible(16_000)
    # Require 100% savings -- impossible, should fall back to raw
    result = compress_chunk(raw, profile="balanced", min_saving=1.0)
    assert not result.was_compressed
    assert result.codec == CODEC_NONE


def test_level_override():
    raw = make_compressible(16_000)
    result = compress_chunk(raw, profile="balanced", level=1)
    assert result.level == 1


# ---------------------------------------------------------------------------
# decompress_chunk
# ---------------------------------------------------------------------------
def test_decompress_none_codec_passthrough():
    data = b"hello world uncompressed"
    result = decompress_chunk(data, CODEC_NONE)
    assert result == data


def test_decompress_zlib_round_trip():
    raw = make_compressible(8_192)
    compressed = zlib.compress(raw, 6)
    result = decompress_chunk(compressed, CODEC_ZLIB)
    assert result == raw


def test_decompress_hash_verification_pass():
    raw = make_compressible(4_096)
    expected = hashlib.sha256(raw).hexdigest()
    compressed = zlib.compress(raw, 6)
    result = decompress_chunk(compressed, CODEC_ZLIB, expected_hash=expected)
    assert result == raw


def test_decompress_hash_verification_fail():
    raw = make_compressible(4_096)
    tampered_hash = "a" * 64
    compressed = zlib.compress(raw, 6)
    with pytest.raises(IntegrityError, match="integrity check failed"):
        decompress_chunk(compressed, CODEC_ZLIB, expected_hash=tampered_hash)


def test_decompress_corrupt_zlib_raises_integrity_error():
    with pytest.raises(IntegrityError, match="zlib decompression failed"):
        decompress_chunk(b"not zlib data at all!!", CODEC_ZLIB)


def test_decompress_unknown_codec_raises():
    with pytest.raises(IntegrityError, match="Unknown codec"):
        decompress_chunk(b"data", "brotli")


# ---------------------------------------------------------------------------
# CompressionResult fields
# ---------------------------------------------------------------------------
def test_result_fields_uncompressed():
    raw = make_incompressible(4_096)
    result = compress_chunk(raw, profile="balanced")
    # Even if not compressed, sizes must be consistent
    assert result.original_size == len(raw)
    assert result.stored_size == len(result.data)


def test_result_fields_compressed():
    raw = make_compressible(32_768)
    result = compress_chunk(raw, profile="balanced")
    assert result.original_size == len(raw)
    assert result.stored_size == len(result.data)
    if result.was_compressed:
        assert result.stored_size < result.original_size
        expected_ratio = 1.0 - result.stored_size / result.original_size
        assert abs(result.saving_ratio - expected_ratio) < 1e-6


# ---------------------------------------------------------------------------
# Empty input guard
# ---------------------------------------------------------------------------
def test_compress_empty_raises():
    with pytest.raises(ValueError, match="empty bytes"):
        compress_chunk(b"", profile="balanced")


# ---------------------------------------------------------------------------
# describe_result helper
# ---------------------------------------------------------------------------
def test_describe_result_compressed():
    raw = make_compressible(16_000)
    result = compress_chunk(raw, profile="balanced")
    desc = describe_result(result)
    assert isinstance(desc, str) and len(desc) > 0


def test_describe_result_not_compressed():
    raw = make_incompressible(512)
    result = compress_chunk(raw, profile="fast_restore")
    desc = describe_result(result)
    assert "Stored uncompressed" in desc


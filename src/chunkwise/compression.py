"""Chunk-level compression layer for ChunkWise.

Design principles (see plan.md, Section A):
- SHA-256 is ALWAYS computed on the raw, uncompressed bytes.
- Compression is ONLY attempted for new unique chunks (after dedup lookup).
- Compression is retained only when the saving meets the configured threshold.
- Existing uncompressed chunks (codec='none') remain fully restorable.
- Decompression is verified by re-hashing against the stored digest.
"""
from __future__ import annotations

import zlib
import hashlib
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Codec identifiers
# ---------------------------------------------------------------------------
CODEC_NONE = "none"
CODEC_ZLIB = "zlib"


# ---------------------------------------------------------------------------
# Policy profiles (see plan.md Table: Policy Profiles)
# ---------------------------------------------------------------------------
PROFILES: dict[str, dict] = {
    "balanced": {
        "name": "Balanced",
        "description": "Standard trade-off between CPU consumption and storage footprint.",
        "codec": CODEC_ZLIB,
        "level": 6,
        "min_saving": 0.05,   # 5% minimum saving required to store compressed
    },
    "space_saver": {
        "name": "Space Saver",
        "description": "Maximum storage reduction; higher CPU overhead.",
        "codec": CODEC_ZLIB,
        "level": 9,
        "min_saving": 0.02,   # 2% minimum saving
    },
    "fast_restore": {
        "name": "Fast Restore",
        "description": "Lowest decompression overhead; prioritises restore latency.",
        "codec": CODEC_ZLIB,
        "level": 1,
        "min_saving": 0.10,   # 10% minimum saving (only compress if well worth it)
    },
    "disabled": {
        "name": "No Compression",
        "description": "Compression is fully disabled; chunks stored as-is.",
        "codec": CODEC_NONE,
        "level": 0,
        "min_saving": 1.0,    # Effectively never compress
    },
}

DEFAULT_PROFILE = "balanced"


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CompressionResult:
    """Outcome of a compress_chunk call."""
    data: bytes           # Bytes to store in S3 (may be raw or compressed)
    codec: str            # 'zlib' or 'none'
    original_size: int    # Raw byte count (always)
    stored_size: int      # Actual bytes stored in S3
    level: int            # zlib level used (0 for 'none')
    saving_ratio: float   # Fraction saved: 0.0 .. 1.0  (negative = expanded)
    was_compressed: bool  # True when zlib bytes were chosen


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def compress_chunk(
    raw: bytes,
    *,
    profile: str = DEFAULT_PROFILE,
    level: Optional[int] = None,
    min_saving: Optional[float] = None,
) -> CompressionResult:
    """Compress *raw* bytes according to the requested policy profile.

    Parameters
    ----------
    raw:
        The original, uncompressed chunk bytes.  SHA-256 must already have
        been computed on these bytes BEFORE calling this function.
    profile:
        One of the keys in ``PROFILES``.  Defaults to ``'balanced'``.
    level:
        Override the profile's zlib compression level (1-9).
    min_saving:
        Override the profile's minimum saving threshold (0.0 .. 1.0).

    Returns
    -------
    CompressionResult
        Contains the bytes to upload, the codec name, sizes, and diagnostics.
    """
    if not raw:
        raise ValueError("compress_chunk received empty bytes")

    cfg = PROFILES.get(profile, PROFILES[DEFAULT_PROFILE])
    effective_level = level if level is not None else cfg["level"]
    effective_threshold = min_saving if min_saving is not None else cfg["min_saving"]
    effective_codec = cfg["codec"]

    original_size = len(raw)

    # Short-circuit when compression is disabled by policy
    if effective_codec == CODEC_NONE or effective_level == 0:
        return CompressionResult(
            data=raw,
            codec=CODEC_NONE,
            original_size=original_size,
            stored_size=original_size,
            level=0,
            saving_ratio=0.0,
            was_compressed=False,
        )

    # Attempt compression
    compressed = zlib.compress(raw, effective_level)
    compressed_size = len(compressed)
    saving_ratio = 1.0 - (compressed_size / original_size)

    # Only keep compressed bytes when they meaningfully reduce size
    if saving_ratio >= effective_threshold:
        return CompressionResult(
            data=compressed,
            codec=CODEC_ZLIB,
            original_size=original_size,
            stored_size=compressed_size,
            level=effective_level,
            saving_ratio=saving_ratio,
            was_compressed=True,
        )

    # Compressed result is not smaller enough; store raw
    return CompressionResult(
        data=raw,
        codec=CODEC_NONE,
        original_size=original_size,
        stored_size=original_size,
        level=effective_level,
        saving_ratio=saving_ratio,
        was_compressed=False,
    )


def decompress_chunk(data: bytes, codec: str, expected_hash: Optional[str] = None) -> bytes:
    """Decompress stored chunk bytes and optionally verify the SHA-256 digest.

    Parameters
    ----------
    data:
        The bytes retrieved from S3.
    codec:
        The codec recorded in DynamoDB metadata (``'zlib'`` or ``'none'``).
    expected_hash:
        If provided, the decompressed bytes are rehashed with SHA-256 and
        compared.  Raises ``ChunkIntegrityError`` on mismatch.

    Returns
    -------
    bytes
        The original, uncompressed chunk bytes.
    """
    from .errors import IntegrityError  # local import to avoid circularity

    if codec == CODEC_ZLIB:
        try:
            raw = zlib.decompress(data)
        except zlib.error as exc:
            raise IntegrityError(
                f"zlib decompression failed: {exc}"
            ) from exc
    elif codec == CODEC_NONE or not codec:
        raw = data
    else:
        raise IntegrityError(f"Unknown codec '{codec}'; cannot decompress chunk")

    if expected_hash is not None:
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected_hash:
            raise IntegrityError(
                f"Chunk integrity check failed after decompression: "
                f"expected {expected_hash}, got {actual}"
            )

    return raw


# ---------------------------------------------------------------------------
# Helper: human-readable compression summary
# ---------------------------------------------------------------------------
def describe_result(result: CompressionResult) -> str:
    """Return a one-line human-readable summary of a CompressionResult."""
    if not result.was_compressed:
        return (
            f"Stored uncompressed ({result.original_size:,} B) -- "
            f"saving was only {result.saving_ratio * 100:.1f}%"
        )
    return (
        f"Stored compressed: {result.original_size:,} B -> "
        f"{result.stored_size:,} B "
        f"({result.saving_ratio * 100:.1f}% saved, zlib level {result.level})"
    )


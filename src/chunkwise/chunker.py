"""Content-defined chunking (Rabin-style polynomial rolling hash) and fixed-size chunking.

Boundary rule: a cut is taken after byte i when
    chunk_len >= min_size and (fingerprint(window ending at i) & mask) == 0,
or unconditionally when chunk_len == max_size.
The rolling window is NOT reset at cut points, so boundaries depend only on
local content and re-synchronise after an edit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterator

MOD = (1 << 61) - 1  # Mersenne prime 2^61 - 1
BASE = 257


@dataclass(frozen=True)
class ChunkParams:
    min_size: int = 2 * 1024
    avg_size: int = 8 * 1024
    max_size: int = 64 * 1024
    window: int = 48

    def __post_init__(self) -> None:
        if not (0 < self.min_size <= self.avg_size <= self.max_size):
            raise ValueError("require 0 < min_size <= avg_size <= max_size")
        if self.avg_size & (self.avg_size - 1):
            raise ValueError("avg_size must be a power of two")
        if self.window < 1:
            raise ValueError("window must be >= 1")

    @property
    def mask(self) -> int:
        return self.avg_size - 1


class RollingFingerprint:
    """Polynomial rolling hash over the last `window` bytes, modulo MOD.

    For k <= window bytes b_0..b_(k-1) (oldest first):
        fp = sum(b_j * BASE**(k-1-j)) mod MOD
    Sliding by one byte is O(1): subtract the outgoing term, multiply by BASE,
    then add the incoming byte.
    """

    def __init__(self, window: int) -> None:
        if window < 1:
            raise ValueError("window must be >= 1")
        self.window = window
        self._buf = bytearray(window)
        self._pos = 0
        self._filled = 0
        self._out_coef = pow(BASE, window - 1, MOD)
        self.fp = 0

    def push(self, byte: int) -> int:
        if not 0 <= byte <= 255:
            raise ValueError("byte must be in range 0..255")
        if self._filled == self.window:
            self.fp = (self.fp - self._buf[self._pos] * self._out_coef) % MOD
        else:
            self._filled += 1
        self.fp = (self.fp * BASE + byte) % MOD
        self._buf[self._pos] = byte
        self._pos = (self._pos + 1) % self.window
        return self.fp


def cdc_chunks(
    stream: BinaryIO, params: ChunkParams, read_size: int = 1 << 20
) -> Iterator[bytes]:
    """Yield content-defined chunks from a binary stream.

    The rolling window continues across chunk boundaries so a boundary is based
    only on nearby bytes, not on where the previous chunk happened to end.
    """
    if read_size < 1:
        raise ValueError("read_size must be >= 1")

    roll = RollingFingerprint(params.window)
    mask = params.mask
    chunk = bytearray()
    while True:
        block = stream.read(read_size)
        if not block:
            break
        for byte in block:
            fp = roll.push(byte)
            chunk.append(byte)
            size = len(chunk)
            if size >= params.max_size or (size >= params.min_size and (fp & mask) == 0):
                yield bytes(chunk)
                chunk = bytearray()
    if chunk:
        yield bytes(chunk)


def fixed_chunks(
    stream: BinaryIO, params: ChunkParams, read_size: int = 1 << 20
) -> Iterator[bytes]:
    """Yield fixed-size chunks of avg_size bytes (plus a final short chunk)."""
    if read_size < 1:
        raise ValueError("read_size must be >= 1")

    size = params.avg_size
    pending = bytearray()
    while True:
        block = stream.read(read_size)
        if not block:
            break
        pending.extend(block)
        while len(pending) >= size:
            yield bytes(pending[:size])
            del pending[:size]
    if pending:
        yield bytes(pending)


CHUNKERS: dict[str, Callable[..., Iterator[bytes]]] = {
    "cdc": cdc_chunks,
    "fixed": fixed_chunks,
}


def iter_chunks(stream: BinaryIO, method: str, params: ChunkParams) -> Iterator[bytes]:
    """Yield chunks using the named strategy (``cdc`` or ``fixed``)."""
    try:
        chunker = CHUNKERS[method]
    except KeyError:
        raise ValueError(f"unknown chunker {method!r}") from None
    return chunker(stream, params)

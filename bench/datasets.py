"""Deterministic synthetic datasets for repeatable benchmarks."""
from __future__ import annotations

import random

WORDS = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "
    "incididunt ut labore et dolore magna aliqua enim minim veniam quis nostrud"
).split()


def random_bytes(size: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(size)


def text_document(size: int, seed: int) -> bytes:
    rng = random.Random(seed)
    parts: list[str] = []
    generated = 0
    while generated < size:
        line = " ".join(rng.choice(WORDS) for _ in range(12)) + "\n"
        parts.append(line)
        generated += len(line)
    return "".join(parts).encode("ascii")[:size]


def insert_at(data: bytes, pos: int, payload: bytes) -> bytes:
    return data[:pos] + payload + data[pos:]


def small_files(count: int, size: int, seed: int) -> list[tuple[str, bytes]]:
    rng = random.Random(seed)
    return [(f"f{i:05d}.bin", rng.randbytes(size)) for i in range(count)]

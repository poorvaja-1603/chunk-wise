"""SQLite index for chunks, files, and ordered manifests."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from .errors import FileNotFoundInIndexError

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'committed'
);
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    chunker TEXT NOT NULL,
    avg_size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifest (
    file_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    chunk_hash TEXT NOT NULL,
    byte_offset INTEGER NOT NULL,
    length INTEGER NOT NULL,
    PRIMARY KEY (file_id, seq)
);
CREATE TABLE IF NOT EXISTS upload_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class FileRow:
    file_id: str
    name: str
    size: int
    sha256: str
    chunker: str
    avg_size: int
    created_at: str


@dataclass(frozen=True)
class ManifestEntry:
    seq: int
    chunk_hash: str
    byte_offset: int
    length: int


class Index:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def commit_file(
        self,
        row: FileRow,
        manifest: Sequence[tuple[int, str, int, int]],
        refs: dict[str, int],
        sizes: dict[str, int],
        idempotency_key: str | None = None,
    ) -> None:
        expected_refs: dict[str, int] = {}
        expected_sizes: dict[str, int] = {}
        offset = 0
        for expected_seq, (seq, digest, byte_offset, length) in enumerate(manifest):
            if seq != expected_seq or byte_offset != offset or length <= 0:
                raise ValueError("manifest entries must be ordered, contiguous, and non-empty")
            if digest in expected_sizes and expected_sizes[digest] != length:
                raise ValueError("the same chunk hash cannot have different lengths")
            expected_refs[digest] = expected_refs.get(digest, 0) + 1
            expected_sizes[digest] = length
            offset += length
        if offset != row.size or refs != expected_refs or sizes != expected_sizes:
            raise ValueError("file size, manifest, references, and chunk sizes must agree")

        with self._tx() as connection:
            for digest, size in sizes.items():
                existing = connection.execute(
                    "SELECT size FROM chunks WHERE hash = ?", (digest,)
                ).fetchone()
                if existing is not None and existing[0] != size:
                    raise ValueError(f"stored size for chunk {digest} does not match")
            connection.executemany(
                "INSERT INTO chunks(hash, size, ref_count, status) VALUES (?, ?, ?, 'committed') "
                "ON CONFLICT(hash) DO UPDATE SET ref_count = ref_count + excluded.ref_count",
                [(digest, sizes[digest], refs[digest]) for digest in refs],
            )
            connection.execute(
                "INSERT INTO files(file_id, name, size, sha256, chunker, avg_size, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row.file_id, row.name, row.size, row.sha256, row.chunker, row.avg_size, row.created_at),
            )
            connection.executemany(
                "INSERT INTO manifest(file_id, seq, chunk_hash, byte_offset, length) VALUES (?, ?, ?, ?, ?)",
                [(row.file_id, seq, digest, byte_offset, length)
                 for seq, digest, byte_offset, length in manifest],
            )
            if idempotency_key is not None:
                connection.execute(
                    "INSERT INTO upload_idempotency(idempotency_key, file_id, sha256) VALUES (?, ?, ?)",
                    (idempotency_key, row.file_id, row.sha256),
                )

    def list_files(self) -> list[FileRow]:
        rows = self.conn.execute(
            "SELECT file_id, name, size, sha256, chunker, avg_size, created_at "
            "FROM files ORDER BY created_at DESC, file_id DESC"
        ).fetchall()
        return [FileRow(*row) for row in rows]

    def idempotent_upload(self, key: str) -> tuple[str, str] | None:
        row = self.conn.execute(
            "SELECT file_id, sha256 FROM upload_idempotency WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def get_file(self, file_id: str) -> FileRow:
        result = self.conn.execute(
            "SELECT file_id, name, size, sha256, chunker, avg_size, created_at "
            "FROM files WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        if result is None:
            raise FileNotFoundInIndexError(f"no file {file_id}")
        return FileRow(*result)

    def manifest(self, file_id: str) -> list[ManifestEntry]:
        rows = self.conn.execute(
            "SELECT seq, chunk_hash, byte_offset, length FROM manifest "
            "WHERE file_id = ? ORDER BY seq",
            (file_id,),
        ).fetchall()
        return [ManifestEntry(*row) for row in rows]

    def refcount(self, digest: str) -> int | None:
        row = self.conn.execute(
            "SELECT ref_count FROM chunks WHERE hash = ?", (digest,)
        ).fetchone()
        return None if row is None else row[0]

    def totals(self) -> tuple[int, int, int, int]:
        """Return logical bytes, physical bytes, file count, and unique chunk count."""
        logical, files = self.conn.execute(
            "SELECT COALESCE(SUM(size), 0), COUNT(*) FROM files"
        ).fetchone()
        physical, chunks = self.conn.execute(
            "SELECT COALESCE(SUM(size), 0), COUNT(*) FROM chunks"
        ).fetchone()
        return logical, physical, files, chunks
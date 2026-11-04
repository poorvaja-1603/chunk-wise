"""Chunk metadata backends for local, AWS, and virtual runs."""
from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Protocol, Sequence

from .errors import (
    FileNotFoundInIndexError,
    IdempotencyConflictError,
    IndexWriteConflictError,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'committed',
    uploader_id TEXT,
    lease_expires REAL,
    tombstoned_at REAL
);
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    chunker TEXT NOT NULL,
    avg_size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'committed'
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
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
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


class ChunkIndex(Protocol):
    def has_chunk(self, digest: str) -> bool: ...

    def has_chunks_batch(self, digests: Sequence[str]) -> set[str]: ...

    def claim_chunk(
        self,
        digest: str,
        size: int,
        uploader_id: str,
        lease_seconds: float = 60.0,
    ) -> bool: ...

    def commit_chunk(self, digest: str, size: int) -> None: ...

    def commit_file(
        self,
        row: FileRow,
        manifest: Sequence[tuple[int, str, int, int]],
        refs: dict[str, int],
        sizes: dict[str, int],
        idempotency_key: str | None = None,
    ) -> None: ...

    def get_file(self, file_id: str) -> FileRow: ...

    def manifest(self, file_id: str) -> list[ManifestEntry]: ...

    def idempotent_upload(self, key: str) -> tuple[str, str] | None: ...

    def claim_idempotency(self, key: str, digest: str, file_id: str) -> str: ...

    def list_files(self) -> list[FileRow]: ...

    def refcount(self, digest: str) -> int | None: ...

    def totals(self) -> tuple[int, int, int, int]: ...

    def delete_file(self, file_id: str) -> list[str]: ...

    def gc(self, grace_seconds: float = 86400.0) -> list[tuple[str, int]]: ...

    def fsck(
        self,
        repair: bool = False,
        max_pending_age_seconds: float = 3600.0,
    ) -> dict: ...

    def close(self) -> None: ...


def _validate_commit(
    row: FileRow,
    manifest: Sequence[tuple[int, str, int, int]],
    refs: dict[str, int],
    sizes: dict[str, int],
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


class SqliteIndex:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.dynamo_reads = 0
        self.dynamo_writes = 0

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

    def has_chunk(self, digest: str) -> bool:
        self.dynamo_reads += 1
        row = self.conn.execute(
            "SELECT status FROM chunks WHERE hash = ?", (digest,)
        ).fetchone()
        return row is not None and row[0] == "committed"

    def has_chunks_batch(self, digests: Sequence[str]) -> set[str]:
        if not digests:
            return set()
        self.dynamo_reads += 1
        unique = list(set(digests))
        placeholders = ",".join("?" for _ in unique)
        rows = self.conn.execute(
            f"SELECT hash FROM chunks WHERE hash IN ({placeholders}) AND status = 'committed'",
            unique,
        ).fetchall()
        return {row[0] for row in rows}

    def claim_chunk(
        self,
        digest: str,
        size: int,
        uploader_id: str,
        lease_seconds: float = 60.0,
    ) -> bool:
        now = time.time()
        expires = now + lease_seconds
        with self._tx() as connection:
            row = connection.execute(
                "SELECT status, uploader_id, lease_expires FROM chunks WHERE hash = ?",
                (digest,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO chunks(hash, size, ref_count, status, uploader_id, lease_expires) "
                    "VALUES (?, ?, 0, 'pending', ?, ?)",
                    (digest, size, uploader_id, expires),
                )
                self.dynamo_writes += 1
                return True
            status, owner, lease_exp = row
            if status == "committed":
                return False
            if lease_exp is not None and lease_exp < now:
                connection.execute(
                    "UPDATE chunks SET uploader_id = ?, lease_expires = ?, status = 'pending', size = ? "
                    "WHERE hash = ?",
                    (uploader_id, expires, size, digest),
                )
                self.dynamo_writes += 1
                return True
            if owner == uploader_id:
                return True
            return False

    def commit_chunk(self, digest: str, size: int) -> None:
        self.dynamo_writes += 1
        with self._tx() as connection:
            connection.execute(
                "INSERT INTO chunks(hash, size, ref_count, status) VALUES (?, ?, 0, 'committed') "
                "ON CONFLICT(hash) DO UPDATE SET status = 'committed', size = excluded.size",
                (digest, size),
            )

    def commit_file(
        self,
        row: FileRow,
        manifest: Sequence[tuple[int, str, int, int]],
        refs: dict[str, int],
        sizes: dict[str, int],
        idempotency_key: str | None = None,
    ) -> None:
        _validate_commit(row, manifest, refs, sizes)
        with self._tx() as connection:
            for digest, size in sizes.items():
                existing = connection.execute(
                    "SELECT size FROM chunks WHERE hash = ?", (digest,)
                ).fetchone()
                if existing is not None and existing[0] != size:
                    raise ValueError(f"stored size for chunk {digest} does not match")

            existing_file = connection.execute(
                "SELECT sha256, size, status FROM files WHERE file_id = ?",
                (row.file_id,),
            ).fetchone()
            if existing_file is not None and existing_file[2] == "committed":
                if existing_file[0] == row.sha256 and existing_file[1] == row.size:
                    return
                # Duplicate committed file: attempting to insert will raise sqlite3.IntegrityError
                connection.execute(
                    "INSERT INTO files(file_id, name, size, sha256, chunker, avg_size, created_at, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (row.file_id, row.name, row.size, row.sha256, row.chunker, row.avg_size, row.created_at),
                )

            # 1. Write file as pending
            if existing_file is None:
                connection.execute(
                    "INSERT INTO files(file_id, name, size, sha256, chunker, avg_size, created_at, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (row.file_id, row.name, row.size, row.sha256, row.chunker, row.avg_size, row.created_at),
                )
            else:
                connection.execute(
                    "UPDATE files SET name = ?, size = ?, sha256 = ?, chunker = ?, avg_size = ?, created_at = ?, status = 'pending' "
                    "WHERE file_id = ?",
                    (row.name, row.size, row.sha256, row.chunker, row.avg_size, row.created_at, row.file_id),
                )

            # 2. Write manifest items
            connection.executemany(
                "INSERT OR REPLACE INTO manifest(file_id, seq, chunk_hash, byte_offset, length) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (row.file_id, seq, digest, byte_offset, length)
                    for seq, digest, byte_offset, length in manifest
                ],
            )

            # 3. Write chunk refs and mark chunks committed
            connection.executemany(
                "INSERT INTO chunks(hash, size, ref_count, status) VALUES (?, ?, ?, 'committed') "
                "ON CONFLICT(hash) DO UPDATE SET ref_count = ref_count + excluded.ref_count, status = 'committed'",
                [(digest, sizes[digest], refs[digest]) for digest in refs],
            )

            # 4. Flip file to committed
            connection.execute(
                "UPDATE files SET status = 'committed' WHERE file_id = ?",
                (row.file_id,),
            )

            if idempotency_key is not None:
                connection.execute(
                    "INSERT INTO upload_idempotency(idempotency_key, file_id, sha256, status) "
                    "VALUES (?, ?, ?, 'committed') "
                    "ON CONFLICT(idempotency_key) DO UPDATE SET file_id = excluded.file_id, sha256 = excluded.sha256, status = 'committed'",
                    (idempotency_key, row.file_id, row.sha256),
                )
            self.dynamo_writes += 1

    def list_files(self) -> list[FileRow]:
        self.dynamo_reads += 1
        rows = self.conn.execute(
            "SELECT file_id, name, size, sha256, chunker, avg_size, created_at "
            "FROM files WHERE status = 'committed' ORDER BY created_at DESC, file_id DESC"
        ).fetchall()
        return [FileRow(*row) for row in rows]

    def idempotent_upload(self, key: str) -> tuple[str, str] | None:
        self.dynamo_reads += 1
        row = self.conn.execute(
            "SELECT file_id, sha256 FROM upload_idempotency WHERE idempotency_key = ? AND status = 'committed'",
            (key,),
        ).fetchone()
        if row is None:
            return None
        file_row = self.conn.execute(
            "SELECT 1 FROM files WHERE file_id = ? AND status = 'committed'",
            (row[0],),
        ).fetchone()
        return (row[0], row[1]) if file_row is not None else None

    def claim_idempotency(self, key: str, digest: str, file_id: str) -> str:
        with self._tx() as connection:
            row = connection.execute(
                "SELECT file_id, sha256 FROM upload_idempotency WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                if row[1] != digest:
                    raise IdempotencyConflictError(
                        "idempotency key was already used for different file content"
                    )
                return row[0]
            connection.execute(
                "INSERT INTO upload_idempotency(idempotency_key, file_id, sha256, status) "
                "VALUES (?, ?, ?, 'pending')",
                (key, file_id, digest),
            )
            self.dynamo_writes += 1
            return file_id

    def get_file(self, file_id: str) -> FileRow:
        self.dynamo_reads += 1
        result = self.conn.execute(
            "SELECT file_id, name, size, sha256, chunker, avg_size, created_at "
            "FROM files WHERE file_id = ? AND status = 'committed'",
            (file_id,),
        ).fetchone()
        if result is None:
            raise FileNotFoundInIndexError(f"no file {file_id}")
        return FileRow(*result)

    def manifest(self, file_id: str) -> list[ManifestEntry]:
        self.get_file(file_id)
        self.dynamo_reads += 1
        rows = self.conn.execute(
            "SELECT seq, chunk_hash, byte_offset, length FROM manifest "
            "WHERE file_id = ? ORDER BY seq",
            (file_id,),
        ).fetchall()
        return [ManifestEntry(*row) for row in rows]

    def refcount(self, digest: str) -> int | None:
        self.dynamo_reads += 1
        row = self.conn.execute(
            "SELECT ref_count FROM chunks WHERE hash = ?", (digest,)
        ).fetchone()
        return None if row is None else row[0]

    def totals(self) -> tuple[int, int, int, int]:
        self.dynamo_reads += 1
        logical, files = self.conn.execute(
            "SELECT COALESCE(SUM(size), 0), COUNT(*) FROM files WHERE status = 'committed'"
        ).fetchone()
        physical, chunks = self.conn.execute(
            "SELECT COALESCE(SUM(size), 0), COUNT(*) FROM chunks WHERE status = 'committed' AND ref_count > 0"
        ).fetchone()
        return logical, physical, files, chunks

    def delete_file(self, file_id: str) -> list[str]:
        file_row = self.get_file(file_id)
        manifest_entries = self.manifest(file_id)
        tombstoned: list[str] = []
        now = time.time()
        with self._tx() as connection:
            # Count refs to decrement per chunk
            chunk_counts = defaultdict(int)
            for entry in manifest_entries:
                chunk_counts[entry.chunk_hash] += 1

            for chunk_hash, count in chunk_counts.items():
                connection.execute(
                    "UPDATE chunks SET ref_count = MAX(0, ref_count - ?) WHERE hash = ?",
                    (count, chunk_hash),
                )
                new_ref = connection.execute(
                    "SELECT ref_count FROM chunks WHERE hash = ?", (chunk_hash,)
                ).fetchone()
                if new_ref and new_ref[0] == 0:
                    connection.execute(
                        "UPDATE chunks SET status = 'tombstone', tombstoned_at = ? WHERE hash = ?",
                        (now, chunk_hash),
                    )
                    tombstoned.append(chunk_hash)

            # Mark file deleted or remove file record and manifest
            connection.execute("DELETE FROM manifest WHERE file_id = ?", (file_id,))
            connection.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
            connection.execute("DELETE FROM upload_idempotency WHERE file_id = ?", (file_id,))
            self.dynamo_writes += 1
        return tombstoned

    def gc(self, grace_seconds: float = 86400.0) -> list[tuple[str, int]]:
        now = time.time()
        cutoff = now - grace_seconds
        deleted: list[tuple[str, int]] = []
        with self._tx() as connection:
            rows = connection.execute(
                "SELECT hash, size, ref_count FROM chunks WHERE status = 'tombstone' AND tombstoned_at <= ?",
                (cutoff,),
            ).fetchall()
            for digest, size, ref_count in rows:
                if ref_count <= 0:
                    connection.execute("DELETE FROM chunks WHERE hash = ?", (digest,))
                    deleted.append((digest, size))
            self.dynamo_writes += 1
        return deleted

    def fsck(
        self,
        repair: bool = False,
        max_pending_age_seconds: float = 3600.0,
    ) -> dict:
        self.dynamo_reads += 1
        # 1. Compute actual references from committed files
        actual_rows = self.conn.execute(
            "SELECT m.chunk_hash, COUNT(*) "
            "FROM manifest m JOIN files f ON m.file_id = f.file_id "
            "WHERE f.status = 'committed' GROUP BY m.chunk_hash"
        ).fetchall()
        actual_refs = {digest: count for digest, count in actual_rows}

        # 2. Get recorded ref_counts
        chunk_rows = self.conn.execute(
            "SELECT hash, ref_count, status, size FROM chunks"
        ).fetchall()
        recorded_refs = {row[0]: (row[1], row[2], row[3]) for row in chunk_rows}

        drift: dict[str, tuple[int, int]] = {}
        for digest, (rec_count, status, size) in recorded_refs.items():
            act_count = actual_refs.get(digest, 0)
            if rec_count != act_count:
                drift[digest] = (rec_count, act_count)

        for digest, act_count in actual_refs.items():
            if digest not in recorded_refs:
                drift[digest] = (0, act_count)

        # 3. Find stale pending files
        now = time.time()
        pending_rows = self.conn.execute(
            "SELECT file_id, created_at FROM files WHERE status = 'pending'"
        ).fetchall()
        stale_pending: list[str] = []
        for file_id, created_at in pending_rows:
            # Check age
            stale_pending.append(file_id)

        # 4. Repair if requested
        if repair:
            with self._tx() as connection:
                for digest, (rec_count, act_count) in drift.items():
                    if act_count == 0:
                        connection.execute(
                            "UPDATE chunks SET ref_count = 0, status = 'tombstone', tombstoned_at = ? WHERE hash = ?",
                            (now, digest),
                        )
                    else:
                        connection.execute(
                            "UPDATE chunks SET ref_count = ?, status = 'committed' WHERE hash = ?",
                            (act_count, digest),
                        )
                for file_id in stale_pending:
                    connection.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
                    connection.execute("DELETE FROM manifest WHERE file_id = ?", (file_id,))
                self.dynamo_writes += 1

        return {
            "ok": len(drift) == 0,
            "drift_chunks": drift,
            "stale_pending_files": stale_pending,
            "repaired": repair,
        }


class MemoryIndex:
    def __init__(self, simulated_lookup_latency_ms: float = 0.1) -> None:
        if simulated_lookup_latency_ms < 0:
            raise ValueError("simulated lookup latency must not be negative")
        self.simulated_lookup_latency_ms = float(simulated_lookup_latency_ms)
        self.chunks: dict[str, dict] = {}
        self.files: dict[str, tuple[FileRow, str]] = {}
        self.manifests: dict[str, list[ManifestEntry]] = {}
        self.idempotency: dict[str, dict] = {}
        self.dynamo_reads = 0
        self.dynamo_writes = 0

    def close(self) -> None:
        return None

    def has_chunk(self, digest: str) -> bool:
        self.dynamo_reads += 1
        chunk = self.chunks.get(digest)
        return chunk is not None and chunk.get("status") == "committed"

    def has_chunks_batch(self, digests: Sequence[str]) -> set[str]:
        self.dynamo_reads += 1
        return {
            d for d in digests
            if self.chunks.get(d, {}).get("status") == "committed"
        }

    def claim_chunk(
        self,
        digest: str,
        size: int,
        uploader_id: str,
        lease_seconds: float = 60.0,
    ) -> bool:
        now = time.time()
        chunk = self.chunks.get(digest)
        self.dynamo_writes += 1
        if chunk is None:
            self.chunks[digest] = {
                "size": size,
                "ref_count": 0,
                "status": "pending",
                "uploader_id": uploader_id,
                "lease_expires": now + lease_seconds,
                "tombstoned_at": 0.0,
            }
            return True
        if chunk.get("status") == "committed":
            return False
        if chunk.get("lease_expires", 0) < now:
            chunk["uploader_id"] = uploader_id
            chunk["lease_expires"] = now + lease_seconds
            chunk["status"] = "pending"
            chunk["size"] = size
            return True
        if chunk.get("uploader_id") == uploader_id:
            return True
        return False

    def commit_chunk(self, digest: str, size: int) -> None:
        self.dynamo_writes += 1
        chunk = self.chunks.setdefault(
            digest, {"ref_count": 0, "uploader_id": None, "lease_expires": 0.0, "tombstoned_at": 0.0}
        )
        chunk["size"] = size
        chunk["status"] = "committed"

    def commit_file(
        self,
        row: FileRow,
        manifest: Sequence[tuple[int, str, int, int]],
        refs: dict[str, int],
        sizes: dict[str, int],
        idempotency_key: str | None = None,
    ) -> None:
        _validate_commit(row, manifest, refs, sizes)
        existing = self.files.get(row.file_id)
        if existing is not None and existing[1] == "committed":
            if existing[0].sha256 == row.sha256 and existing[0].size == row.size:
                return
            raise IndexWriteConflictError(f"file {row.file_id} already exists")

        self.dynamo_writes += 1
        # 1. File pending
        self.files[row.file_id] = (row, "pending")

        # 2. Manifest
        self.manifests[row.file_id] = [
            ManifestEntry(seq, digest, byte_offset, length)
            for seq, digest, byte_offset, length in manifest
        ]

        # 3. Chunk refs
        for digest, count in refs.items():
            chunk = self.chunks.setdefault(
                digest, {"size": sizes[digest], "ref_count": 0, "uploader_id": None, "lease_expires": 0.0, "tombstoned_at": 0.0}
            )
            chunk["size"] = sizes[digest]
            chunk["ref_count"] += count
            chunk["status"] = "committed"

        # 4. Flip to committed
        self.files[row.file_id] = (row, "committed")
        if idempotency_key is not None:
            self.idempotency[idempotency_key] = {
                "file_id": row.file_id,
                "sha256": row.sha256,
                "status": "committed",
            }

    def list_files(self) -> list[FileRow]:
        self.dynamo_reads += 1
        committed = [row for row, status in self.files.values() if status == "committed"]
        return sorted(committed, key=lambda row: (row.created_at, row.file_id), reverse=True)

    def idempotent_upload(self, key: str) -> tuple[str, str] | None:
        self.dynamo_reads += 1
        record = self.idempotency.get(key)
        if record is None or record.get("status") != "committed":
            return None
        file_id = record["file_id"]
        file_record = self.files.get(file_id)
        if file_record is None or file_record[1] != "committed":
            return None
        return file_id, record["sha256"]

    def claim_idempotency(self, key: str, digest: str, file_id: str) -> str:
        self.dynamo_writes += 1
        record = self.idempotency.get(key)
        if record is not None:
            if record["sha256"] != digest:
                raise IdempotencyConflictError(
                    "idempotency key was already used for different file content"
                )
            return record["file_id"]
        self.idempotency[key] = {
            "file_id": file_id,
            "sha256": digest,
            "status": "pending",
        }
        return file_id

    def get_file(self, file_id: str) -> FileRow:
        self.dynamo_reads += 1
        record = self.files.get(file_id)
        if record is None or record[1] != "committed":
            raise FileNotFoundInIndexError(f"no file {file_id}")
        return record[0]

    def manifest(self, file_id: str) -> list[ManifestEntry]:
        self.get_file(file_id)
        self.dynamo_reads += 1
        return list(self.manifests[file_id])

    def refcount(self, digest: str) -> int | None:
        self.dynamo_reads += 1
        chunk = self.chunks.get(digest)
        return None if chunk is None else chunk.get("ref_count")

    def totals(self) -> tuple[int, int, int, int]:
        self.dynamo_reads += 1
        committed_files = [row for row, status in self.files.values() if status == "committed"]
        committed_chunks = [
            chunk for chunk in self.chunks.values()
            if chunk.get("status") == "committed" and chunk.get("ref_count", 0) > 0
        ]
        logical = sum(row.size for row in committed_files)
        physical = sum(chunk["size"] for chunk in committed_chunks)
        return logical, physical, len(committed_files), len(committed_chunks)

    def delete_file(self, file_id: str) -> list[str]:
        self.get_file(file_id)
        manifest_entries = self.manifest(file_id)
        tombstoned: list[str] = []
        now = time.time()
        self.dynamo_writes += 1

        chunk_counts = defaultdict(int)
        for entry in manifest_entries:
            chunk_counts[entry.chunk_hash] += 1

        for digest, count in chunk_counts.items():
            chunk = self.chunks.get(digest)
            if chunk:
                chunk["ref_count"] = max(0, chunk["ref_count"] - count)
                if chunk["ref_count"] == 0:
                    chunk["status"] = "tombstone"
                    chunk["tombstoned_at"] = now
                    tombstoned.append(digest)

        del self.files[file_id]
        if file_id in self.manifests:
            del self.manifests[file_id]
        to_del = [k for k, v in self.idempotency.items() if v.get("file_id") == file_id]
        for k in to_del:
            del self.idempotency[k]
        return tombstoned

    def gc(self, grace_seconds: float = 86400.0) -> list[tuple[str, int]]:
        now = time.time()
        cutoff = now - grace_seconds
        deleted: list[tuple[str, int]] = []
        self.dynamo_writes += 1
        to_remove = []
        for digest, chunk in self.chunks.items():
            if (
                chunk.get("status") == "tombstone"
                and chunk.get("tombstoned_at", 0) <= cutoff
                and chunk.get("ref_count", 0) <= 0
            ):
                deleted.append((digest, chunk["size"]))
                to_remove.append(digest)
        for digest in to_remove:
            del self.chunks[digest]
        return deleted

    def fsck(
        self,
        repair: bool = False,
        max_pending_age_seconds: float = 3600.0,
    ) -> dict:
        self.dynamo_reads += 1
        actual_refs = defaultdict(int)
        for file_id, (row, status) in self.files.items():
            if status == "committed":
                for entry in self.manifests.get(file_id, []):
                    actual_refs[entry.chunk_hash] += 1

        drift: dict[str, tuple[int, int]] = {}
        for digest, chunk in self.chunks.items():
            rec_count = chunk.get("ref_count", 0)
            act_count = actual_refs.get(digest, 0)
            if rec_count != act_count:
                drift[digest] = (rec_count, act_count)

        for digest, act_count in actual_refs.items():
            if digest not in self.chunks:
                drift[digest] = (0, act_count)

        stale_pending: list[str] = [
            file_id for file_id, (row, status) in self.files.items()
            if status == "pending"
        ]

        if repair:
            self.dynamo_writes += 1
            now = time.time()
            for digest, (rec_count, act_count) in drift.items():
                chunk = self.chunks.setdefault(digest, {"size": 0, "status": "committed", "uploader_id": None, "lease_expires": 0.0, "tombstoned_at": 0.0})
                chunk["ref_count"] = act_count
                if act_count == 0:
                    chunk["status"] = "tombstone"
                    chunk["tombstoned_at"] = now
                else:
                    chunk["status"] = "committed"
            for file_id in stale_pending:
                self.files.pop(file_id, None)
                self.manifests.pop(file_id, None)

        return {
            "ok": len(drift) == 0,
            "drift_chunks": drift,
            "stale_pending_files": stale_pending,
            "repaired": repair,
        }


class DynamoIndex:
    def __init__(self, table_name: str, region: str | None = None) -> None:
        if not table_name:
            raise ValueError("a DynamoDB table name is required")
        try:
            import boto3
            from boto3.dynamodb.types import TypeSerializer
        except ImportError as error:
            raise RuntimeError("boto3 is required for the DynamoDB backend") from error
        self._serializer = TypeSerializer()
        self._resource = boto3.resource("dynamodb", region_name=region)
        self.table_name = table_name
        self._ensure_table()
        self.table = self._resource.Table(table_name)
        self.client = boto3.client("dynamodb", region_name=region)
        self.dynamo_reads = 0
        self.dynamo_writes = 0

    def _ensure_table(self) -> None:
        client = self._resource.meta.client
        try:
            client.describe_table(TableName=self.table_name)
        except client.exceptions.ResourceNotFoundException:
            self._resource.create_table(
                TableName=self.table_name,
                KeySchema=[
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "pk", "AttributeType": "S"},
                    {"AttributeName": "sk", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            ).wait_until_exists()

    def close(self) -> None:
        return None

    @staticmethod
    def _file_key(file_id: str) -> dict[str, str]:
        return {"pk": f"FILE#{file_id}", "sk": "META"}

    def _item(self, key: dict[str, str]) -> dict | None:
        self.dynamo_reads += 1
        return self.table.get_item(Key=key, ConsistentRead=True).get("Item")

    def has_chunk(self, digest: str) -> bool:
        item = self._item({"pk": f"CHUNK#{digest}", "sk": "META"})
        return item is not None and item.get("status") == "committed"

    def has_chunks_batch(self, digests: Sequence[str]) -> set[str]:
        if not digests:
            return set()
        found: set[str] = set()
        unique = list(set(digests))
        for index in range(0, len(unique), 100):
            batch = unique[index:index + 100]
            keys = [{"pk": f"CHUNK#{d}", "sk": "META"} for d in batch]
            self.dynamo_reads += 1
            response = self._resource.batch_get_item(
                RequestItems={
                    self.table_name: {
                        "Keys": keys,
                        "ConsistentRead": True,
                    }
                }
            )
            for item in response.get("Responses", {}).get(self.table_name, []):
                if item.get("status") == "committed":
                    found.add(item["pk"].removeprefix("CHUNK#"))
        return found

    def claim_chunk(
        self,
        digest: str,
        size: int,
        uploader_id: str,
        lease_seconds: float = 60.0,
    ) -> bool:
        now = Decimal(str(int(time.time())))
        expires = Decimal(str(int(time.time() + lease_seconds)))
        chunk_key = {"pk": f"CHUNK#{digest}", "sk": "META"}
        self.dynamo_writes += 1
        try:
            self.table.put_item(
                Item={
                    **chunk_key,
                    "kind": "chunk",
                    "size": size,
                    "ref_count": 0,
                    "status": "pending",
                    "uploader_id": uploader_id,
                    "lease_expires": expires,
                    "tombstoned_at": Decimal("0"),
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except self.client.exceptions.ConditionalCheckFailedException:
            pass

        self.dynamo_writes += 1
        try:
            self.table.update_item(
                Key=chunk_key,
                UpdateExpression="SET #status = :pending, #uploader = :uploader, #lease_expires = :expires, #size = :size",
                ConditionExpression="#status = :pending_status AND #lease_expires < :now",
                ExpressionAttributeNames={
                    "#status": "status",
                    "#uploader": "uploader_id",
                    "#lease_expires": "lease_expires",
                    "#size": "size",
                },
                ExpressionAttributeValues={
                    ":pending": "pending",
                    ":pending_status": "pending",
                    ":uploader": uploader_id,
                    ":expires": expires,
                    ":size": size,
                    ":now": now,
                },
            )
            return True
        except self.client.exceptions.ConditionalCheckFailedException:
            pass

        item = self._item(chunk_key)
        if item is not None:
            if item.get("status") == "committed":
                return False
            if item.get("uploader_id") == uploader_id:
                return True
        return False

    def commit_chunk(self, digest: str, size: int) -> None:
        chunk_key = {"pk": f"CHUNK#{digest}", "sk": "META"}
        self.dynamo_writes += 1
        self.table.update_item(
            Key=chunk_key,
            UpdateExpression="SET #status = :committed, #size = if_not_exists(#size, :size), #kind = :kind, #ref_count = if_not_exists(#ref_count, :zero)",
            ExpressionAttributeNames={
                "#status": "status",
                "#size": "size",
                "#kind": "kind",
                "#ref_count": "ref_count",
            },
            ExpressionAttributeValues={
                ":committed": "committed",
                ":size": size,
                ":kind": "chunk",
                ":zero": 0,
            },
        )

    def _serialize(self, value: object) -> dict:
        return self._serializer.serialize(value)

    def _wire_item(self, item: dict) -> dict:
        return {key: self._serialize(value) for key, value in item.items()}

    def _write_chunk_reference(self, file_id: str, digest: str, size: int, count: int) -> bool:
        ref_key = {"pk": f"REF#{file_id}", "sk": f"CHUNK#{digest}"}
        reference = self._item(ref_key)
        if reference is not None:
            if reference.get("size") != size or reference.get("refs") != count:
                raise IndexWriteConflictError("file chunk reference conflicts with a prior write")
            return False

        self.dynamo_writes += 1
        try:
            self.table.update_item(
                Key={"pk": f"CHUNK#{digest}", "sk": "META"},
                UpdateExpression="ADD #ref_count :count SET #size = if_not_exists(#size, :size), #status = :committed, #kind = :kind",
                ExpressionAttributeNames={
                    "#ref_count": "ref_count",
                    "#size": "size",
                    "#status": "status",
                    "#kind": "kind",
                },
                ExpressionAttributeValues={
                    ":count": count,
                    ":size": size,
                    ":committed": "committed",
                    ":kind": "chunk",
                },
            )
            self.table.put_item(
                Item={
                    **ref_key,
                    "kind": "chunk-reference",
                    "size": size,
                    "refs": count,
                }
            )
            return True
        except Exception:
            reference = self._item(ref_key)
            if reference is not None and reference.get("size") == size and reference.get("refs") == count:
                return False
            raise

    def claim_idempotency(self, key: str, digest: str, file_id: str) -> str:
        idem_key = {"pk": f"IDEMPOTENCY#{key}", "sk": "MAP"}
        self.dynamo_writes += 1
        try:
            self.table.put_item(
                Item={
                    **idem_key,
                    "kind": "idempotency",
                    "file_id": file_id,
                    "sha256": digest,
                    "status": "pending",
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            return file_id
        except self.client.exceptions.ConditionalCheckFailedException:
            existing = self._item(idem_key)
            if existing is None:
                raise
            if existing.get("sha256") != digest:
                raise IdempotencyConflictError(
                    "idempotency key was already used for different file content"
                )
            return existing["file_id"]

    def commit_file(
        self,
        row: FileRow,
        manifest: Sequence[tuple[int, str, int, int]],
        refs: dict[str, int],
        sizes: dict[str, int],
        idempotency_key: str | None = None,
    ) -> None:
        _validate_commit(row, manifest, refs, sizes)
        file_key = self._file_key(row.file_id)
        existing_file = self._item(file_key)
        if existing_file is not None and existing_file.get("status") == "committed":
            existing = self.get_file(row.file_id)
            if existing.sha256 == row.sha256 and existing.size == row.size:
                return
            raise IndexWriteConflictError(f"file {row.file_id} already exists")

        # 1. Write file as pending
        file_item_pending = {
            **file_key,
            "kind": "file",
            "name": row.name,
            "size": row.size,
            "sha256": row.sha256,
            "chunker": row.chunker,
            "avg_size": row.avg_size,
            "created_at": row.created_at,
            "status": "pending",
        }
        self.dynamo_writes += 1
        self.table.put_item(Item=file_item_pending)

        # 2. Write manifest items in batch
        with self.table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
            for seq, digest, byte_offset, length in manifest:
                self.dynamo_writes += 1
                batch.put_item(Item={
                    "pk": f"FILE#{row.file_id}",
                    "sk": f"MANIFEST#{seq:012d}",
                    "kind": "manifest",
                    "seq": seq,
                    "chunk_hash": digest,
                    "byte_offset": byte_offset,
                    "length": length,
                })

        # 3. Write chunk refs
        new_unique_chunks = 0
        new_physical_bytes = 0
        for digest, count in refs.items():
            is_new = self._write_chunk_reference(row.file_id, digest, sizes[digest], count)
            if is_new:
                chunk_item = self._item({"pk": f"CHUNK#{digest}", "sk": "META"})
                if chunk_item and chunk_item.get("ref_count") == count:
                    new_unique_chunks += 1
                    new_physical_bytes += sizes[digest]

        # Also write file list entry for query-based list_files
        self.dynamo_writes += 1
        self.table.put_item(
            Item={
                "pk": "FILES",
                "sk": f"{row.created_at}#{row.file_id}",
                "kind": "file-summary",
                "file_id": row.file_id,
                "name": row.name,
                "size": row.size,
                "sha256": row.sha256,
                "chunker": row.chunker,
                "avg_size": row.avg_size,
                "created_at": row.created_at,
                "status": "committed",
            }
        )

        # 4. Flip to committed and update atomic counters
        self.dynamo_writes += 1
        if idempotency_key is None:
            self.table.update_item(
                Key=file_key,
                UpdateExpression="SET #status = :committed",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":committed": "committed"},
            )
        else:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": self._wire_item(file_key),
                            "UpdateExpression": "SET #status = :committed",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": self._wire_item({":committed": "committed"}),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": self._wire_item({
                                "pk": f"IDEMPOTENCY#{idempotency_key}",
                                "sk": "MAP",
                            }),
                            "UpdateExpression": "SET #status = :committed",
                            "ConditionExpression": "file_id = :file_id AND sha256 = :sha256",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": self._wire_item({
                                ":committed": "committed",
                                ":file_id": row.file_id,
                                ":sha256": row.sha256,
                            }),
                        }
                    },
                ]
            )

        # Atomically update global totals counter
        self.dynamo_writes += 1
        self.table.update_item(
            Key={"pk": "COUNTERS", "sk": "TOTALS"},
            UpdateExpression="ADD logical_bytes :logical, physical_bytes :physical, files :files, unique_chunks :chunks",
            ExpressionAttributeValues={
                ":logical": row.size,
                ":physical": new_physical_bytes,
                ":files": 1,
                ":chunks": new_unique_chunks,
            },
        )

    def list_files(self) -> list[FileRow]:
        self.dynamo_reads += 1
        rows = []
        request = {
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": "FILES"},
            "ScanIndexForward": False,
        }
        while True:
            result = self.table.query(**request)
            rows.extend(result.get("Items", []))
            key = result.get("LastEvaluatedKey")
            if not key:
                break
            request["ExclusiveStartKey"] = key
        return [self._row_from_item(item) for item in rows]

    @staticmethod
    def _row_from_item(item: dict) -> FileRow:
        file_id = item.get("file_id")
        if file_id is None:
            file_id = item["pk"].removeprefix("FILE#")
        return FileRow(
            file_id=file_id,
            name=item["name"],
            size=int(item["size"]),
            sha256=item["sha256"],
            chunker=item["chunker"],
            avg_size=int(item["avg_size"]),
            created_at=item["created_at"],
        )

    def idempotent_upload(self, key: str) -> tuple[str, str] | None:
        item = self._item({"pk": f"IDEMPOTENCY#{key}", "sk": "MAP"})
        if item is None or item.get("status") != "committed":
            return None
        file_id = item["file_id"]
        try:
            self.get_file(file_id)
        except FileNotFoundInIndexError:
            return None
        return file_id, item["sha256"]

    def get_file(self, file_id: str) -> FileRow:
        item = self._item(self._file_key(file_id))
        if item is None or item.get("status") != "committed":
            raise FileNotFoundInIndexError(f"no file {file_id}")
        return self._row_from_item(item)

    def manifest(self, file_id: str) -> list[ManifestEntry]:
        self.get_file(file_id)
        self.dynamo_reads += 1
        rows = []
        request = {
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
            "ExpressionAttributeValues": {
                ":pk": f"FILE#{file_id}",
                ":prefix": "MANIFEST#",
            },
            "ConsistentRead": True,
        }
        while True:
            result = self.table.query(**request)
            rows.extend(result.get("Items", []))
            key = result.get("LastEvaluatedKey")
            if not key:
                break
            request["ExclusiveStartKey"] = key
        return [
            ManifestEntry(
                int(item["seq"]),
                item["chunk_hash"],
                int(item["byte_offset"]),
                int(item["length"]),
            )
            for item in rows
        ]

    def refcount(self, digest: str) -> int | None:
        item = self._item({"pk": f"CHUNK#{digest}", "sk": "META"})
        return None if item is None else int(item["ref_count"])

    def totals(self) -> tuple[int, int, int, int]:
        counter = self._item({"pk": "COUNTERS", "sk": "TOTALS"})
        if counter is not None:
            return (
                int(counter.get("logical_bytes", 0)),
                int(counter.get("physical_bytes", 0)),
                int(counter.get("files", 0)),
                int(counter.get("unique_chunks", 0)),
            )
        self.dynamo_reads += 1
        logical = physical = files = chunks = 0
        request: dict = {}
        while True:
            result = self.table.scan(**request)
            for item in result.get("Items", []):
                if item.get("kind") == "file" and item.get("status") == "committed":
                    logical += int(item["size"])
                    files += 1
                elif item.get("kind") == "chunk" and item.get("status") == "committed" and int(item.get("ref_count", 0)) > 0:
                    physical += int(item["size"])
                    chunks += 1
            key = result.get("LastEvaluatedKey")
            if not key:
                break
            request["ExclusiveStartKey"] = key
        return logical, physical, files, chunks

    def delete_file(self, file_id: str) -> list[str]:
        file_row = self.get_file(file_id)
        manifest_entries = self.manifest(file_id)
        tombstoned: list[str] = []
        now = Decimal(str(int(time.time())))
        self.dynamo_writes += 1

        chunk_counts = defaultdict(int)
        for entry in manifest_entries:
            chunk_counts[entry.chunk_hash] += 1

        for digest, count in chunk_counts.items():
            self.table.update_item(
                Key={"pk": f"CHUNK#{digest}", "sk": "META"},
                UpdateExpression="ADD #ref_count :minus_count",
                ExpressionAttributeNames={"#ref_count": "ref_count"},
                ExpressionAttributeValues={":minus_count": -count},
            )
            chunk_item = self._item({"pk": f"CHUNK#{digest}", "sk": "META"})
            if chunk_item and int(chunk_item.get("ref_count", 0)) <= 0:
                self.table.update_item(
                    Key={"pk": f"CHUNK#{digest}", "sk": "META"},
                    UpdateExpression="SET #status = :tombstone, tombstoned_at = :now, #ref_count = :zero",
                    ExpressionAttributeNames={"#status": "status", "#ref_count": "ref_count"},
                    ExpressionAttributeValues={":tombstone": "tombstone", ":now": now, ":zero": 0},
                )
                tombstoned.append(digest)

        # Delete file records and manifest
        self.table.delete_item(Key=self._file_key(file_id))
        self.table.delete_item(Key={"pk": "FILES", "sk": f"{file_row.created_at}#{file_id}"})
        with self.table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
            for entry in manifest_entries:
                batch.delete_item(Key={"pk": f"FILE#{file_id}", "sk": f"MANIFEST#{entry.seq:012d}"})

        # Decrement logical bytes and files in counters
        self.table.update_item(
            Key={"pk": "COUNTERS", "sk": "TOTALS"},
            UpdateExpression="ADD logical_bytes :minus_log, files :minus_file",
            ExpressionAttributeValues={":minus_log": -file_row.size, ":minus_file": -1},
        )
        return tombstoned

    def gc(self, grace_seconds: float = 86400.0) -> list[tuple[str, int]]:
        now = time.time()
        cutoff = Decimal(str(int(now - grace_seconds)))
        deleted: list[tuple[str, int]] = []
        self.dynamo_reads += 1
        # Scan for tombstoned chunks older than cutoff
        request = {
            "FilterExpression": "kind = :kind AND #status = :tombstone AND tombstoned_at <= :cutoff",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {":kind": "chunk", ":tombstone": "tombstone", ":cutoff": cutoff},
        }
        while True:
            result = self.table.scan(**request)
            for item in result.get("Items", []):
                ref_count = int(item.get("ref_count", 0))
                if ref_count <= 0:
                    digest = item["pk"].removeprefix("CHUNK#")
                    size = int(item.get("size", 0))
                    self.table.delete_item(Key={"pk": item["pk"], "sk": "META"})
                    deleted.append((digest, size))
            key = result.get("LastEvaluatedKey")
            if not key:
                break
            request["ExclusiveStartKey"] = key
        return deleted

    def fsck(
        self,
        repair: bool = False,
        max_pending_age_seconds: float = 3600.0,
    ) -> dict:
        self.dynamo_reads += 1
        actual_refs = defaultdict(int)
        files = self.list_files()
        for file_row in files:
            for entry in self.manifest(file_row.file_id):
                actual_refs[entry.chunk_hash] += 1

        drift: dict[str, tuple[int, int]] = {}
        # Scan chunks
        chunk_items = []
        request = {"FilterExpression": "kind = :kind", "ExpressionAttributeValues": {":kind": "chunk"}}
        while True:
            result = self.table.scan(**request)
            chunk_items.extend(result.get("Items", []))
            key = result.get("LastEvaluatedKey")
            if not key:
                break
            request["ExclusiveStartKey"] = key

        recorded = {item["pk"].removeprefix("CHUNK#"): int(item.get("ref_count", 0)) for item in chunk_items}
        for digest, rec_count in recorded.items():
            act_count = actual_refs.get(digest, 0)
            if rec_count != act_count:
                drift[digest] = (rec_count, act_count)

        for digest, act_count in actual_refs.items():
            if digest not in recorded:
                drift[digest] = (0, act_count)

        if repair:
            self.dynamo_writes += 1
            now = Decimal(str(int(time.time())))
            for digest, (rec_count, act_count) in drift.items():
                if act_count == 0:
                    self.table.update_item(
                        Key={"pk": f"CHUNK#{digest}", "sk": "META"},
                        UpdateExpression="SET #ref_count = :zero, #status = :tombstone, tombstoned_at = :now",
                        ExpressionAttributeNames={"#ref_count": "ref_count", "#status": "status"},
                        ExpressionAttributeValues={":zero": 0, ":tombstone": "tombstone", ":now": now},
                    )
                else:
                    self.table.update_item(
                        Key={"pk": f"CHUNK#{digest}", "sk": "META"},
                        UpdateExpression="SET #ref_count = :act, #status = :committed",
                        ExpressionAttributeNames={"#ref_count": "ref_count", "#status": "status"},
                        ExpressionAttributeValues={":act": act_count, ":committed": "committed"},
                    )

        return {
            "ok": len(drift) == 0,
            "drift_chunks": drift,
            "stale_pending_files": [],
            "repaired": repair,
        }


Index = SqliteIndex

"""Write, restore, verify, and account for deduplicated files."""
from __future__ import annotations

import hashlib
import io
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .chunker import ChunkParams, iter_chunks
from .errors import IdempotencyConflictError, IntegrityError
from .index import FileRow, Index
from .store import ChunkStore


@dataclass(frozen=True)
class PutResult:
    file_id: str
    name: str
    size: int
    chunks: int
    new_chunks: int
    new_bytes: int
    chunker: str


@dataclass(frozen=True)
class Stats:
    logical_bytes: int
    physical_bytes: int
    dedup_ratio: float
    storage_saved_pct: float
    files: int
    unique_chunks: int


class _NullSink:
    def write(self, data: bytes) -> int:
        return len(data)


class Engine:
    def __init__(
        self,
        home: Path,
        params: ChunkParams | None = None,
        fsync: bool = False,
    ) -> None:
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.params = params or ChunkParams()
        self.store = ChunkStore(self.home / "chunks", fsync=fsync)
        self.index = Index(self.home / "index.sqlite3")

    def close(self) -> None:
        self.index.close()

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def put(self, stream: BinaryIO, name: str, chunker: str = "cdc") -> PutResult:
        return self._put(stream, name, chunker)

    def put_idempotent_bytes(
        self,
        data: bytes,
        name: str,
        idempotency_key: str,
        chunker: str = "cdc",
    ) -> PutResult:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("idempotency key must contain 1 to 200 characters")
        digest = hashlib.sha256(data).hexdigest()
        existing = self.index.idempotent_upload(idempotency_key)
        if existing is not None:
            return self._existing_idempotent_result(existing, digest)
        try:
            return self._put(io.BytesIO(data), name, chunker, idempotency_key)
        except sqlite3.IntegrityError:
            existing = self.index.idempotent_upload(idempotency_key)
            if existing is None:
                raise
            return self._existing_idempotent_result(existing, digest)

    def _existing_idempotent_result(
        self,
        existing: tuple[str, str],
        digest: str,
    ) -> PutResult:
        file_id, existing_digest = existing
        if digest != existing_digest:
            raise IdempotencyConflictError(
                "idempotency key was already used for different file content"
            )
        file_row = self.index.get_file(file_id)
        return PutResult(
            file_id=file_row.file_id,
            name=file_row.name,
            size=file_row.size,
            chunks=len(self.index.manifest(file_id)),
            new_chunks=0,
            new_bytes=0,
            chunker=file_row.chunker,
        )

    def _put(
        self,
        stream: BinaryIO,
        name: str,
        chunker: str = "cdc",
        idempotency_key: str | None = None,
    ) -> PutResult:
        file_id = "f-" + uuid.uuid4().hex[:12]
        whole_hash = hashlib.sha256()
        manifest: list[tuple[int, str, int, int]] = []
        refs: dict[str, int] = {}
        sizes: dict[str, int] = {}
        offset = 0
        new_chunks = 0
        new_bytes = 0

        for sequence, data in enumerate(iter_chunks(stream, chunker, self.params)):
            digest = hashlib.sha256(data).hexdigest()
            whole_hash.update(data)
            if self.store.put(digest, data):
                new_chunks += 1
                new_bytes += len(data)
            manifest.append((sequence, digest, offset, len(data)))
            refs[digest] = refs.get(digest, 0) + 1
            sizes[digest] = len(data)
            offset += len(data)

        row = FileRow(
            file_id=file_id,
            name=name,
            size=offset,
            sha256=whole_hash.hexdigest(),
            chunker=chunker,
            avg_size=self.params.avg_size,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.index.commit_file(row, manifest, refs, sizes, idempotency_key)
        return PutResult(file_id, name, offset, len(manifest), new_chunks, new_bytes, chunker)

    def put_path(self, path: str | Path, chunker: str = "cdc") -> PutResult:
        source = Path(path)
        with source.open("rb") as stream:
            return self.put(stream, source.name, chunker)

    def put_bytes(self, data: bytes, name: str, chunker: str = "cdc") -> PutResult:
        return self.put(io.BytesIO(data), name, chunker)

    def get(self, file_id: str, out: BinaryIO) -> int:
        """Write a verified file to ``out``; discard ``out`` if verification fails."""
        metadata = self.index.get_file(file_id)
        whole_hash = hashlib.sha256()
        total = 0
        for sequence, entry in enumerate(self.index.manifest(file_id)):
            if entry.seq != sequence or entry.byte_offset != total:
                raise IntegrityError(f"file {file_id} has an invalid manifest")
            data = self.store.get(entry.chunk_hash)
            if len(data) != entry.length or hashlib.sha256(data).hexdigest() != entry.chunk_hash:
                raise IntegrityError(f"chunk {entry.chunk_hash} (seq {entry.seq}) failed verification")
            written = out.write(data)
            if written != len(data):
                raise OSError("output stream did not accept the complete chunk")
            whole_hash.update(data)
            total += len(data)
        if total != metadata.size or whole_hash.hexdigest() != metadata.sha256:
            raise IntegrityError(f"file {file_id} failed whole-file verification")
        return total

    def get_bytes(self, file_id: str) -> bytes:
        output = io.BytesIO()
        self.get(file_id, output)
        return output.getvalue()

    def verify(self, file_id: str) -> int:
        return self.get(file_id, _NullSink())

    def stats(self) -> Stats:
        logical, physical, files, unique_chunks = self.index.totals()
        dedup_ratio = logical / physical if physical else 1.0
        storage_saved_pct = (1 - physical / logical) * 100 if logical else 0.0
        return Stats(logical, physical, dedup_ratio, storage_saved_pct, files, unique_chunks)
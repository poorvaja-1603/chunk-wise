"""Content-addressed chunk storage on the local filesystem."""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from .errors import ChunkMissingError, IntegrityError


class ChunkStore:
    def __init__(self, root: Path, fsync: bool = False) -> None:
        self.root = Path(root)
        self.fsync = fsync
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("digest must be a lowercase SHA-256 hex string")
        return self.root / "objects" / digest[:2] / digest[2:]

    def exists(self, digest: str) -> bool:
        return self.path_for(digest).exists()

    def put(self, digest: str, data: bytes) -> bool:
        """Store a chunk; return False when a verified copy already exists."""
        path = self.path_for(digest)
        if path.exists():
            existing = path.read_bytes()
            if len(existing) != len(data) or hashlib.sha256(existing).hexdigest() != digest:
                raise IntegrityError(f"existing chunk {digest} failed verification")
            return False

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "tmp" / uuid.uuid4().hex
        try:
            with tmp.open("wb") as stream:
                stream.write(data)
                stream.flush()
                if self.fsync:
                    os.fsync(stream.fileno())
            os.replace(tmp, path)
            return True
        finally:
            tmp.unlink(missing_ok=True)

    def get(self, digest: str) -> bytes:
        try:
            return self.path_for(digest).read_bytes()
        except FileNotFoundError:
            raise ChunkMissingError(f"chunk {digest} missing from store") from None

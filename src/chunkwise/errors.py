"""Exception hierarchy for ChunkWise."""
from __future__ import annotations


class ChunkWiseError(Exception):
    """Base class for all ChunkWise errors."""


class IntegrityError(ChunkWiseError):
    """A chunk or a whole file failed hash verification on restore."""


class ChunkMissingError(ChunkWiseError):
    """The index references a chunk whose bytes are absent from the store."""


class FileNotFoundInIndexError(ChunkWiseError):
    """No committed file record exists for the requested file_id."""


class IdempotencyConflictError(ChunkWiseError):
    """An idempotency key was claimed for a different payload."""


class IndexWriteConflictError(ChunkWiseError):
    """A conditional index write failed because another writer conflicted."""


class ChunkClaimConflictError(ChunkWiseError):
    """A chunk lease claim could not be acquired because another writer holds it."""

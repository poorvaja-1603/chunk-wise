class ChunkWiseError(Exception):
    """Base class for ChunkWise errors."""


class IntegrityError(ChunkWiseError):
    """A chunk or file failed hash verification."""


class ChunkMissingError(ChunkWiseError):
    """A referenced chunk is missing from storage."""


class FileNotFoundInIndexError(ChunkWiseError):
    """No committed file record exists for the requested file ID."""


class IdempotencyConflictError(ChunkWiseError):
    """An upload idempotency key was reused for different content."""
